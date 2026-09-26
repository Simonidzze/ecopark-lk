import os
import unittest
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ecopark_sync.claims import (
    CLAIM_TOKENS,
    DEFAULT_CLAIM_BASIS,
    claim_template_path,
    claim_values,
    extract_cadastral_number,
    format_ru_money,
    render_pretrial_claim,
)
from ecopark_sync.models import Balance, Base, OwnerPlot, Plot, PretrialClaim
from ecopark_sync.web import create_app


class ClaimDocumentTest(unittest.TestCase):
    def sample_values(self, charge_basis="01.03.2026 № 3"):
        return claim_values(
            tsn_address="г. Новосибирск, ул. Тестовая, д. 1",
            tsn_phone="+7 383 000-00-00",
            tsn_email="info@example.test",
            plot_number="42А",
            owner_name="Иванов Иван Иванович",
            plot_address="Новосибирская область, участок 42А",
            cadastral_number="54:19:0123456:42",
            claim_number="17/26",
            claim_date=date(2026, 8, 29),
            charge_basis=charge_basis,
            account="000042",
            calculation_date=date(2026, 8, 28),
            debt_period_from=date(2026, 1, 1),
            debt_period_to=date(2026, 8, 28),
            principal_amount=Decimal("12345.67"),
            penalty_amount=Decimal("89.10"),
            penalty_basis="пункт 4 решения общего собрания",
            total_amount=Decimal("12434.77"),
        )

    def test_formats_money_and_extracts_cadastral_number(self):
        self.assertEqual(format_ru_money(Decimal("1234.5")), "1 234 руб. 50 коп.")
        self.assertEqual(
            extract_cadastral_number("участок, кадастровый № 54:19:0123456:42"),
            "54:19:0123456:42",
        )
        self.assertEqual(
            self.sample_values(charge_basis="")["CHARGE_BASIS"],
            DEFAULT_CLAIM_BASIS,
        )

    def test_renders_docx_and_preserves_package_parts(self):
        values = self.sample_values()
        generated = render_pretrial_claim(values)

        with ZipFile(claim_template_path(), "r") as template, ZipFile(generated, "r") as result:
            self.assertEqual(template.namelist(), result.namelist())
            self.assertEqual(template.read("word/footer1.xml"), result.read("word/footer1.xml"))
            document_xml = result.read("word/document.xml").decode("utf-8")
            settings_xml = result.read("word/settings.xml").decode("utf-8")

        for token in CLAIM_TOKENS:
            self.assertNotIn("{{" + token + "}}", document_xml)
        self.assertIn("Иванов Иван Иванович", document_xml)
        self.assertIn("12 345 руб. 67 коп.", document_xml)
        self.assertIn("54:19:0123456:42", document_xml)
        self.assertIn("285 руб./сотка", document_xml)
        self.assertIn("и членом ТСН", document_xml)
        self.assertIn("01.03.2026 № 3", document_xml)
        self.assertIn("размеры которых устанавливаются", document_xml)
        self.assertIn("п. 7.5 Устава ТСН", document_xml)
        self.assertIn("в связи с чем подлежит уплате", document_xml)
        self.assertNotIn("Сведения для сверки приведены ниже", document_xml)
        self.assertNotIn("размеры которых устанавливается", document_xml)
        self.assertNotIn("п.7.5 Уставом ТСН", document_xml)
        self.assertNotIn("в связи с чем, подлежит уплате", document_xml)
        self.assertNotIn('<w:br w:type="page"/>', document_xml)
        self.assertIn('<w:updateFields w:val="true"/>', settings_xml)


class ClaimDownloadRouteTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
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
                    organization="ТСН «МИКРОРАЙОН ЭКОПАРК»",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
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
                    penalty=Decimal("0"),
                    overpayment=Decimal("0"),
                    total=Decimal("12345.67"),
                    currency="RUB",
                    sync_run_id=1,
                    synced_at=synced_at,
                )
            )
            session.commit()

    def test_downloads_filled_claim_for_owner_plot(self):
        environment = {
            "TSN_LEGAL_ADDRESS": "г. Новосибирск, ул. Тестовая, д. 1",
            "TSN_PHONE": "+7 383 000-00-00",
            "TSN_EMAIL": "info@example.test",
            "TSN_CLAIM_BASIS": "01.03.2026 № 3",
        }
        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session), patch.dict(
            os.environ,
            environment,
            clear=False,
        ):
            client = create_app().test_client()
            response = client.post(
                "/admin/plots/owner-plot-1/pretrial-claim.docx",
                data={
                    "claim_date": "2026-08-29",
                    "debt_period_to": "2026-08-28",
                },
            )
            second_response = client.post(
                "/admin/plots/owner-plot-1/pretrial-claim.docx",
                data={
                    "claim_date": "2026-08-30",
                    "debt_period_to": "2026-08-28",
                },
            )
            history_response = client.get("/admin/plots/owner-plot-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("n-1", response.headers["Content-Disposition"])
        with ZipFile(BytesIO(response.data), "r") as document:
            xml = document.read("word/document.xml").decode("utf-8")
        self.assertIn("Иванов Иван Иванович", xml)
        self.assertIn("Исх. № 1", xml)
        self.assertIn("54:19:0123456:42", xml)
        self.assertIn("с 01.10.2025 по 28.08.2026", xml)
        self.assertIn("12 345 руб. 67 коп.", xml)
        self.assertIn("не начислены", xml)
        self.assertNotIn("{{", xml)

        self.assertEqual(second_response.status_code, 200)
        with ZipFile(BytesIO(second_response.data), "r") as document:
            second_xml = document.read("word/document.xml").decode("utf-8")
        self.assertIn("Исх. № 2", second_xml)
        self.assertIn("n-2", second_response.headers["Content-Disposition"])
        self.assertIn("Выданные претензии", history_response.get_data(as_text=True))

        with self.Session() as session:
            claim = session.get(PretrialClaim, 1)
            second_claim = session.get(PretrialClaim, 2)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.owner_plot_id, "owner-plot-1")
        self.assertEqual(claim.plot_number, "42А")
        self.assertEqual(claim.owner_name, "Иванов Иван Иванович")
        self.assertEqual(claim.total_amount, Decimal("12345.67"))
        self.assertIsNotNone(second_claim)
        self.assertEqual(second_claim.claim_date, date(2026, 8, 30))


if __name__ == "__main__":
    unittest.main()
