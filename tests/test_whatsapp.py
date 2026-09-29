import json
import unittest
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ecopark_sync.models import Balance, Base, OwnerPlot, Plot, PretrialClaim, WhatsAppMessage
from ecopark_sync.web import create_app
from ecopark_sync.whatsapp import (
    format_whatsapp_message,
    normalize_whatsapp_phone,
    process_next_whatsapp_message,
)


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []

    def send_pdf(self, phone, message, filename, document):
        self.sent.append((phone, message, filename, document.read()))
        return {"message_id": "false_79990000000@test"}


class WhatsAppTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        synced_at = datetime(2026, 8, 28, 12, 0)
        with self.Session() as session:
            session.add(
                Plot(
                    id="plot-1",
                    plot_number="42А",
                    account="000042",
                    address="Новосибирская область, участок 42А",
                    cadastral_number="54:19:0123456:42",
                    organization_id="org-1",
                    organization="ТСН ЭКОПАРК",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
            session.add(
                OwnerPlot(
                    id="owner-plot-1",
                    owner_id="owner-1",
                    owner="Иванов Иван Иванович",
                    phone="8 (999) 000-00-00",
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
                    penalty=Decimal("0"),
                    overpayment=Decimal("0"),
                    total=Decimal("12345.67"),
                    currency="RUB",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_normalizes_russian_phone_and_formats_message(self):
        self.assertEqual(normalize_whatsapp_phone("8 (999) 000-00-00"), "79990000000")
        self.assertEqual(normalize_whatsapp_phone("9990000000"), "79990000000")
        self.assertEqual(
            format_whatsapp_message(
                "{owner}: участок {plot_number}, № {claim_number}",
                owner="Иванов",
                plot_number="42А",
                claim_number=7,
            ),
            "Иванов: участок 42А, № 7",
        )
        with self.assertRaises(ValueError):
            format_whatsapp_message("{unknown}", owner="", plot_number="", claim_number=1)

    def test_queues_single_claim_and_worker_marks_it_sent(self):
        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            client = create_app().test_client()
            response = client.post(
                "/admin/plots/owner-plot-1/whatsapp",
                data={
                    "whatsapp_phone": "8 (999) 000-00-00",
                    "whatsapp_message": "Претензия № {claim_number} по участку {plot_number}",
                    "claim_date": "2026-08-29",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/whatsapp", response.headers["Location"])
        with self.Session() as session:
            claim = session.scalar(select(PretrialClaim))
            queued = session.scalar(select(WhatsAppMessage))
            self.assertEqual(queued.status, "queued")
            self.assertEqual(queued.phone, "79990000000")
            self.assertEqual(queued.claim_number, claim.number)
            self.assertEqual(json.loads(queued.claim_values_json)["CLAIM_NUMBER"], str(claim.number))

        fake_client = FakeWhatsAppClient()
        with patch(
            "ecopark_sync.whatsapp.render_pretrial_claim_pdf",
            return_value=BytesIO(b"%PDF-1.7\n%%EOF\n"),
        ):
            result = process_next_whatsapp_message(
                client=fake_client, session_factory=self.Session
            )

        self.assertEqual(result, "sent")
        self.assertEqual(len(fake_client.sent), 1)
        with self.Session() as session:
            sent = session.scalar(select(WhatsAppMessage))
            self.assertEqual(sent.status, "sent")
            self.assertEqual(sent.external_message_id, "false_79990000000@test")

    def test_bulk_route_uses_current_filter_rows(self):
        report = ([{
            "owner": "Иванов Иван Иванович",
            "plots": [{"plot_number": "42А", "account": "000042", "owner_plot_id": "owner-plot-1"}],
            "phones": ["8 (999) 000-00-00"],
            "total_debt": Decimal("12345.67"),
            "monthly_accrual": Decimal("1000"),
            "debt_months": Decimal("12.3"),
            "last_payment_date": None,
            "last_payment_amount": Decimal("0"),
        }], {"count": 1, "total_debt": Decimal("12345.67")}, ["8 (999) 000-00-00"])
        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session), patch(
            "ecopark_sync.web.load_debtors_report", return_value=report
        ) as load_report:
            response = create_app().test_client().post(
                "/admin/debtors/whatsapp",
                data={
                    "months_from": "3",
                    "months_to": "12",
                    "confirm": "yes",
                    "whatsapp_message": "Участок {plot_number}, № {claim_number}",
                },
            )

        self.assertEqual(response.status_code, 302)
        load_report.assert_called_once_with(Decimal("3"), Decimal("12"))
        with self.Session() as session:
            queued = session.scalar(select(WhatsAppMessage))
            self.assertEqual(queued.plot_number, "42А")
            self.assertTrue(queued.batch_id.startswith("bulk-"))


if __name__ == "__main__":
    unittest.main()
