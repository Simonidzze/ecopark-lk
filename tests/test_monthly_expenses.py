import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from ecopark_sync.expense_report import build_monthly_expense_report
from ecopark_sync.web import build_cash_balance_summary, create_app


class MonthlyExpenseReportTest(unittest.TestCase):
    def test_aggregates_completed_months_and_fills_gaps(self):
        rows, stats = build_monthly_expense_report(
            [
                (datetime(2026, 1, 10), Decimal("100"), "Услуги", "ООО А"),
                (datetime(2026, 1, 20), Decimal("50"), "Услуги", "ООО Б"),
                (datetime(2026, 3, 5), Decimal("80"), "Банк", "Банк"),
                (datetime(2026, 4, 1), Decimal("999"), "Текущий месяц", "ООО В"),
            ],
            as_of=datetime(2026, 4, 15),
        )

        self.assertEqual([row["month"] for row in rows], ["2026-03", "2026-02", "2026-01"])
        self.assertEqual(rows[0]["amount"], Decimal("80"))
        self.assertEqual(rows[0]["change"], Decimal("80"))
        self.assertEqual(rows[1]["amount"], Decimal("0"))
        self.assertEqual(rows[1]["change"], Decimal("-150"))
        self.assertEqual(rows[2]["operations"], 2)
        self.assertEqual(rows[2]["category_count"], 1)
        self.assertEqual(rows[2]["counterparty_count"], 2)
        self.assertEqual(stats["total"], Decimal("230"))
        self.assertEqual(stats["operations"], 3)
        self.assertEqual(stats["average"], Decimal("230") / Decimal("3"))

    def test_builds_cash_balance_with_full_available_period(self):
        summary = build_cash_balance_summary(
            Decimal("1000"),
            Decimal("400"),
            datetime(2025, 10, 8),
            datetime(2026, 9, 23),
            datetime(2025, 10, 9),
            datetime(2026, 9, 22),
        )

        self.assertEqual(summary["cash_balance"], Decimal("600"))
        self.assertEqual(summary["cash_period_from"], datetime(2025, 10, 8))
        self.assertEqual(summary["cash_period_to"], datetime(2026, 9, 23))

    def test_includes_additional_incomes_in_cash_balance(self):
        summary = build_cash_balance_summary(
            Decimal("1000"),
            Decimal("400"),
            income_total=Decimal("125"),
            income_from=datetime(2026, 1, 5),
            income_to=datetime(2026, 2, 5),
        )

        self.assertEqual(summary["total_incomes"], Decimal("1125"))
        self.assertEqual(summary["cash_balance"], Decimal("725"))
        self.assertEqual(summary["cash_period_from"], datetime(2026, 1, 5))
        self.assertEqual(summary["cash_period_to"], datetime(2026, 2, 5))


class MonthlyExpenseRouteTest(unittest.TestCase):
    def test_shows_monthly_expense_tab(self):
        latest = {
            "month": "2026-08",
            "period_end": datetime(2026, 8, 31),
            "amount": Decimal("250"),
            "change": Decimal("50"),
            "operations": 2,
            "category_count": 2,
            "counterparty_count": 2,
        }
        stats = {
            "months": 1,
            "total": Decimal("250"),
            "average": Decimal("250"),
            "operations": 2,
        }
        with patch(
            "ecopark_sync.web.load_monthly_expense_report",
            return_value=([latest], latest, stats, datetime(2026, 9, 24, 8, 0)),
        ):
            response = create_app().test_client().get("/admin/expenses/monthly")

        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Расходы по месяцам", page)
        self.assertIn("08.2026", page)
        self.assertIn("250.00", page)


if __name__ == "__main__":
    unittest.main()
