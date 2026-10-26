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
    TelegramReceiptDelivery,
)
from ecopark_sync.telegram_bot import (
    DEBT_BUTTON,
    MENU_MARKUP,
    RECEIPT_BUTTON,
    debt_message,
    handle_update,
)
from ecopark_sync.web import create_app


class FakeTelegramClient:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append(
            {"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup}
        )


class ConfigureTelegramClient:
    def __init__(self):
        self.calls = []

    def call(self, method, payload=None, timeout=None):
        self.calls.append((method, payload, timeout))


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

    def test_bot_configuration_removes_public_command_list(self):
        from ecopark_sync.telegram_bot import TelegramBotClient

        client = ConfigureTelegramClient()
        TelegramBotClient.configure(client)

        self.assertEqual(client.calls[0][0], "deleteWebhook")
        self.assertEqual(client.calls[1][0], "deleteMyCommands")
        self.assertNotIn("setMyCommands", [method for method, _payload, _timeout in client.calls])

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

    def test_multiple_telegram_accounts_can_share_owner_and_all_plots(self):
        self.submit_request()
        now = datetime(2026, 10, 26, 13, 0)
        with self.Session() as session:
            first_request_id = session.scalar(select(TelegramLinkRequest.id))
            session.add_all(
                [
                    OwnerPlot(
                        id="owner-plot-2",
                        owner_id="owner-1",
                        owner="Иванов Иван Иванович",
                        phone="+7 999 000-00-00",
                        plot_id="plot-2",
                        plot_number="43",
                        account="000043",
                        presentation="Иванов И.И., участок 43",
                        organization_id="org-1",
                        sync_run_id=1,
                        synced_at=now,
                    ),
                    Balance(
                        owner_plot_id="owner-plot-2",
                        owner_id="owner-1",
                        plot_id="plot-2",
                        plot_number="43",
                        account="000043",
                        debt=Decimal("500.00"),
                        penalty=Decimal("0"),
                        overpayment=Decimal("0"),
                        total=Decimal("500.00"),
                        currency="RUB",
                        sync_run_id=1,
                        synced_at=now,
                    ),
                    TelegramLinkRequest(
                        telegram_user_id="67890",
                        chat_id="67890",
                        username="family_member",
                        full_name="Иванова Мария Ивановна",
                        plot_number="42А",
                        phone="79991111111",
                        status="pending",
                        owner_id=None,
                        owner_plot_id=None,
                        review_comment="",
                        created_at=now,
                        reviewed_at=None,
                    ),
                ]
            )
            session.commit()
            second_request_id = session.scalar(
                select(TelegramLinkRequest.id).where(
                    TelegramLinkRequest.telegram_user_id == "67890"
                )
            )

        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            client = create_app().test_client()
            first_response = client.post(
                f"/admin/telegram/requests/{first_request_id}/approve",
                data={"owner_plot_id": "owner-plot-1"},
            )
            second_response = client.post(
                f"/admin/telegram/requests/{second_request_id}/approve",
                data={"owner_plot_id": "owner-plot-1"},
            )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        with self.Session() as session:
            bindings = session.scalars(
                select(TelegramBinding).where(TelegramBinding.owner_id == "owner-1")
            ).all()
            second_account_debt = debt_message(session, "67890")
        self.assertEqual(
            {binding.telegram_user_id for binding in bindings},
            {"12345", "67890"},
        )
        self.assertIn("Участок № 42А", second_account_debt)
        self.assertIn("Участок № 43", second_account_debt)

    def test_receipt_menu_button_queues_onec_pdf_delivery(self):
        now = datetime(2026, 10, 26, 12, 30)
        with self.Session() as session:
            session.add(
                TelegramBinding(
                    telegram_user_id="12345",
                    chat_id="12345",
                    owner_id="owner-1",
                    owner="Иванов Иван Иванович",
                    request_id=1,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        handle_update(update(RECEIPT_BUTTON), self.client, self.Session)

        with self.Session() as session:
            deliveries = session.scalars(select(TelegramReceiptDelivery)).all()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].owner_plot_id, "owner-plot-1")
        self.assertEqual(deliveries[0].status, "queued")
        self.assertIn("скоро придёт", self.client.messages[-1]["text"])
        self.assertEqual(
            [button["text"] for button in MENU_MARKUP["keyboard"][0]],
            [DEBT_BUTTON, RECEIPT_BUTTON],
        )

        handle_update(update(RECEIPT_BUTTON), self.client, self.Session)
        with self.Session() as session:
            deliveries = session.scalars(select(TelegramReceiptDelivery)).all()
        self.assertEqual(len(deliveries), 1)
        self.assertIn("уже ожидает", self.client.messages[-1]["text"])


if __name__ == "__main__":
    unittest.main()
