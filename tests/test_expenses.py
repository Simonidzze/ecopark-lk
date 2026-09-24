import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

from ecopark_sync.expense_report import (
    expense_filter_values,
    render_expenses_xlsx,
)
from ecopark_sync.payment_report import XLSX_CONTENT_TYPE
from ecopark_sync.models import Expense
from ecopark_sync.syncer import sync_expenses, synced_models_for_snapshot
from ecopark_sync.web import create_app


class ExpenseSyncTest(unittest.TestCase):
    @patch("ecopark_sync.syncer.upsert_many")
    def test_syncs_expense_fields(self, upsert_many):
        synced_at = datetime(2026, 9, 23, 10, 0)
        snapshot = {
            "expenses": [
                {
                    "id": "expense-1",
                    "document_id": "doc-1",
                    "document": "Списание с расчетного счета",
                    "date": "2026-09-22T12:00:00",
                    "number": "000123",
                    "expense_category_id": "category-1",
                    "expense_category": "Обслуживание территории",
                    "counterparty_id": "counterparty-1",
                    "counterparty": "ООО Подрядчик",
                    "purpose": "Покос травы",
                    "amount": 25000.50,
                    "currency": "RUB",
                    "organization_id": "org-1",
                    "organization": "ТСН Экопарк",
                    "source": "1C",
                }
            ]
        }

        sync_expenses(object(), snapshot, 9, synced_at)

        rows = upsert_many.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "expense-1")
        self.assertEqual(rows[0]["date"], datetime(2026, 9, 22, 12, 0))
        self.assertEqual(rows[0]["amount"], Decimal("25000.50"))
        self.assertEqual(rows[0]["expense_category"], "Обслуживание территории")
        self.assertEqual(rows[0]["sync_run_id"], 9)

    def test_old_snapshot_does_not_mark_expenses_as_stale(self):
        self.assertNotIn(Expense, synced_models_for_snapshot({}))
        self.assertIn(Expense, synced_models_for_snapshot({"expenses": []}))


class ExpenseReportTest(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 9, 23, 10, 0)
        self.rows = [
            SimpleNamespace(
                date=datetime(2026, 9, 22, 12, 0),
                document="Списание с расчетного счета",
                number="000123",
                expense_category="Обслуживание территории",
                counterparty="ООО Подрядчик",
                purpose="Покос травы",
                amount=Decimal("25000.50"),
                currency="RUB",
                organization="ТСН Экопарк",
            )
        ]
        self.stats = {
            "count": 1,
            "total": Decimal("25000.50"),
            "categories": 1,
            "counterparties": 1,
        }
        self.filters = expense_filter_values(
            {
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
                "category": "Обслуживание территории",
                "q": "трава",
            }
        )

    def test_parses_filters_and_renders_typed_xlsx(self):
        self.assertEqual(self.filters["date_from_value"].isoformat(), "2026-09-01")
        report = render_expenses_xlsx(self.rows, self.stats, self.filters, self.as_of)
        workbook = load_workbook(report, data_only=False)
        sheet = workbook["Расходы"]

        self.assertEqual(sheet["A1"].value, "Расходы ТСН «МИКРОРАЙОН ЭКОПАРК»")
        self.assertEqual(sheet["B3"].value, self.as_of)
        self.assertEqual(sheet["A7"].value, "№")
        self.assertEqual(sheet["E7"].value, "Статья расхода")
        self.assertEqual(sheet["B8"].value, datetime(2026, 9, 22, 12, 0))
        self.assertEqual(sheet["H8"].value, 25000.5)
        self.assertIn("ExpensesTable", sheet.tables)
        self.assertEqual(sheet.freeze_panes, "E8")


class ExpenseRoutesTest(unittest.TestCase):
    def test_shows_page_and_downloads_current_filtered_xlsx(self):
        as_of = datetime(2026, 9, 23, 10, 0)
        stats = {
            "count": 0,
            "total": Decimal("0"),
            "categories": 0,
            "counterparties": 0,
        }
        with patch(
            "ecopark_sync.web.load_expense_report",
            return_value=([], stats, ["Электроэнергия"], as_of),
        ) as loader:
            client = create_app().test_client()
            page = client.get("/admin/expenses?category=Электроэнергия")
            response = client.get("/admin/expenses/report.xlsx?category=Электроэнергия")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Скачать Excel", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, XLSX_CONTENT_TYPE)
        self.assertIn("expenses-2026-09-23.xlsx", response.headers["Content-Disposition"])
        self.assertGreater(len(response.data), 1000)
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(loader.call_args.args[0]["category"], "Электроэнергия")


if __name__ == "__main__":
    unittest.main()
