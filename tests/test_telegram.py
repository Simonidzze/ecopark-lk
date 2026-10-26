import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ecopark_sync.models import (
    Balance,
    Base,
    OwnerPlot,
    TelegramBinding,
    TelegramLinkRequest,
    TelegramProfile,
)
from ecopark_sync.telegram_bot import DEBT_BUTTON, handle_update
from ecopark_sync.web import create_app


class FakeTelegramClient:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append(
            {"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup}
        )


def update(text=None, *, contact=None, user_id=12345):
    message = {
        "chat": {"id": user_id, "type": "private"},
        "from": {
            "id": user_id,
            "username": "snt_user",
            "first_name": "Иван",
            "last_name": "Иванов",
        },
    }
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    return {"update_id": 1, "message": message}


class TelegramBotTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )
        synced_at = datetime(2026, 10, 26, 12, 0)
        with self.Session() as session:
            session.add(
                OwnerPlot(
                    id="owner-plot-1",
                    owner_id="owner-1",
                    owner="Иванов Иван Иванович",
                    phone="+7 999 000-00-00",
                    plot_id="plot-1",
                    plot_number="42А",
                    account="000042",
                    presentation="Иванов И.И., участок 42А",
                    organization_id="org-1",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
            session.add(
                Balance(
                    owner_plot_id="owner-plot-1",
                    owner_id="owner-1",
                    plot_id="plot-1",
                    plot_number="42А",
                    account="000042",
                    debt=Decimal("12345.67"),
                    penalty=Decimal("123.45"),
                    overpayment=Decimal("0"),
                    total=Decimal("12469.12"),
                    currency="RUB",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
            session.commit()
        self.client = FakeTelegramClient()

    def tearDown(self):
        self.engine.dispose()

    def submit_request(self):
        handle_update(update("/start"), self.client, self.Session)
        handle_update(update("Иванов Иван Иванович"), self.client, self.Session)
        handle_update(update("42А"), self.client, self.Session)
        handle_update(
            update(contact={"user_id": 12345, "phone_number": "+7 999 000-00-00"}),
            self.client,
            self.Session,
        )

    def test_registration_flow_creates_pending_request(self):
        self.submit_request()

        with self.Session() as session:
            profile = session.get(TelegramProfile, "12345")
            request_row = session.scalar(select(TelegramLinkRequest))

        self.assertEqual(profile.state, "idle")
        self.assertEqual(request_row.status, "pending")
        self.assertEqual(request_row.full_name, "Иванов Иван Иванович")
        self.assertEqual(request_row.plot_number, "42А")
        self.assertEqual(request_row.phone, "79990000000")
        self.assertIn("Заявка отправлена администратору", self.client.messages[-1]["text"])

    def test_admin_approval_enables_local_debt_lookup(self):
        self.submit_request()
        with self.Session() as session:
            request_id = session.scalar(select(TelegramLinkRequest.id))

        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            app = create_app()
            web_client = app.test_client()
            page = web_client.get("/admin/telegram")
            response = web_client.post(
                f"/admin/telegram/requests/{request_id}/approve",
                data={"owner_plot_id": "owner-plot-1"},
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Иванов Иван Иванович", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 302)
        with self.Session() as session:
            binding = session.get(TelegramBinding, "12345")
            request_row = session.get(TelegramLinkRequest, request_id)
        self.assertEqual(binding.owner_id, "owner-1")
        self.assertEqual(request_row.status, "approved")

        handle_update(update(DEBT_BUTTON), self.client, self.Session)
        debt_text = self.client.messages[-1]["text"]
        self.assertIn("Участок № 42А", debt_text)
        self.assertIn("12 345,67 ₽", debt_text)
        self.assertIn("12 469,12 ₽", debt_text)

    def test_admin_can_reject_request(self):
        self.submit_request()
        with self.Session() as session:
            request_id = session.scalar(select(TelegramLinkRequest.id))

        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            response = create_app().test_client().post(
                f"/admin/telegram/requests/{request_id}/reject",
                data={"review_comment": "Данные не совпали"},
            )

        self.assertEqual(response.status_code, 302)
        with self.Session() as session:
            request_row = session.get(TelegramLinkRequest, request_id)
            binding = session.get(TelegramBinding, "12345")
        self.assertEqual(request_row.status, "rejected")
        self.assertEqual(request_row.review_comment, "Данные не совпали")
        self.assertIsNone(binding)


if __name__ == "__main__":
    unittest.main()
