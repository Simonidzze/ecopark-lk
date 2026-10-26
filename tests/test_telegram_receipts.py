import unittest
from datetime import datetime
from decimal import Decimal
import os
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ecopark_sync.client import receipt_url
from ecopark_sync.models import (
    Balance,
    Base,
    OwnerPlot,
    TelegramBinding,
    TelegramReceiptDelivery,
)
from ecopark_sync.telegram_receipts import (
    format_receipt_message,
    process_next_telegram_receipt,
)
from ecopark_sync.telegram_bot import TelegramBotClient
from ecopark_sync.web import create_app


class FakeTelegramClient:
    def __init__(self):
        self.documents = []

    def send_document(self, chat_id, filename, document, caption=""):
        self.documents.append(
            {
                "chat_id": str(chat_id),
                "filename": filename,
                "document": document,
                "caption": caption,
            }
        )
        return {"message_id": 777}


class FakeResponse:
    status_code = 200
    ok = True

    def json(self):
        return {"ok": True, "result": {"message_id": 888}}


class FakeHTTPSession:
    def __init__(self):
        self.request = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return FakeResponse()


class TelegramReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )
        now = datetime(2026, 10, 26, 12, 0)
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
                    synced_at=now,
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
                    synced_at=now,
                )
            )
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

    def tearDown(self):
        self.engine.dispose()

    def test_admin_queues_selected_receipt(self):
        with patch(
            "ecopark_sync.telegram_receipt_web.make_session_factory",
            return_value=self.Session,
        ):
            client = create_app().test_client()
            page = client.get("/admin/telegram/receipts")
            response = client.post(
                "/admin/telegram/receipts/queue",
                data={
                    "scope": "debt",
                    "recipient": "12345|owner-plot-1",
                    "confirm": "yes",
                    "message_template": "{owner}, участок {plot_number}, к оплате {total}",
                },
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Иванов Иван Иванович", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 302)
        with self.Session() as session:
            delivery = session.scalar(select(TelegramReceiptDelivery))
        self.assertEqual(delivery.status, "queued")
        self.assertEqual(delivery.chat_id, "12345")
        self.assertEqual(delivery.owner_plot_id, "owner-plot-1")
        self.assertIn("12 469,12 ₽", delivery.message_text)

    def test_admin_rejects_unbound_recipient(self):
        with patch(
            "ecopark_sync.telegram_receipt_web.make_session_factory",
            return_value=self.Session,
        ):
            response = create_app().test_client().post(
                "/admin/telegram/receipts/queue",
                data={
                    "recipient": "99999|owner-plot-1",
                    "confirm": "yes",
                    "message_template": "Квитанция {plot_number}",
                },
            )
        self.assertEqual(response.status_code, 302)
        with self.Session() as session:
            self.assertIsNone(session.scalar(select(TelegramReceiptDelivery)))

    def test_preview_returns_pdf_from_onec(self):
        with (
            patch(
                "ecopark_sync.telegram_receipt_web.make_session_factory",
                return_value=self.Session,
            ),
            patch(
                "ecopark_sync.telegram_receipt_web.fetch_receipt_pdf",
                return_value=b"%PDF-1.7\ntest\n%%EOF",
            ),
        ):
            response = create_app().test_client().get(
                "/admin/telegram/receipts/owner-plot-1/preview.pdf"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_worker_sends_pdf_and_records_message_id(self):
        now = datetime(2026, 10, 26, 12, 0)
        with self.Session() as session:
            session.add(
                TelegramReceiptDelivery(
                    batch_id="receipt-test",
                    telegram_user_id="12345",
                    chat_id="12345",
                    owner_plot_id="owner-plot-1",
                    owner_name="Иванов Иван Иванович",
                    plot_number="42А",
                    message_text="Ваша квитанция",
                    filename="receipt.pdf",
                    status="queued",
                    attempts=0,
                    created_at=now,
                )
            )
            session.commit()
        client = FakeTelegramClient()

        result = process_next_telegram_receipt(
            client=client,
            session_factory=self.Session,
            receipt_loader=lambda _owner_plot_id: b"%PDF-1.7\n%%EOF",
        )

        self.assertEqual(result, "sent")
        self.assertEqual(client.documents[0]["chat_id"], "12345")
        with self.Session() as session:
            delivery = session.scalar(select(TelegramReceiptDelivery))
        self.assertEqual(delivery.status, "sent")
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.external_message_id, "777")

    def test_message_template_rejects_unknown_fields(self):
        with self.assertRaisesRegex(ValueError, "Неизвестное поле"):
            format_receipt_message(
                "{unknown}", owner="", plot_number="", total="", date=""
            )

    def test_receipt_url_is_derived_from_snapshot_url(self):
        with patch.dict(
            os.environ,
            {
                "ONEC_API_URL": "http://onec/ecopark/hs/ecopark-sync/snapshot?ignored=1",
                "ONEC_RECEIPT_URL": "",
            },
            clear=False,
        ):
            self.assertEqual(
                receipt_url(), "http://onec/ecopark/hs/ecopark-sync/receipt"
            )

    def test_telegram_client_sends_pdf_as_multipart_document(self):
        session = FakeHTTPSession()
        client = TelegramBotClient(token="test-token", session=session)

        result = client.send_document("12345", "receipt.pdf", b"%PDF-test", "Квитанция")

        self.assertEqual(result["message_id"], 888)
        self.assertEqual(session.request["data"]["chat_id"], "12345")
        self.assertEqual(session.request["files"]["document"][0], "receipt.pdf")
        self.assertEqual(session.request["files"]["document"][1], b"%PDF-test")


if __name__ == "__main__":
    unittest.main()
