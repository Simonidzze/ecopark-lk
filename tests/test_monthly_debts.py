import unittest
from datetime import datetime
from decimal import Decimal

from ecopark_sync.web import build_monthly_debt_report


class MonthlyDebtReportTest(unittest.TestCase):
    def test_reconstructs_balances_at_completed_month_ends(self):
        owner_plots = [
            {
                "owner_plot_id": "plot-1",
                "owner_id": "owner-1",
                "debt": Decimal("160"),
                "overpayment": Decimal("0"),
            },
            {
                "owner_plot_id": "plot-2",
                "owner_id": "owner-2",
                "debt": Decimal("0"),
                "overpayment": Decimal("30"),
            },
        ]
        accruals = [
            ("plot-1", datetime(2026, 1, 10), Decimal("100")),
            ("plot-1", datetime(2026, 2, 10), Decimal("50")),
            ("plot-1", datetime(2026, 4, 5), Decimal("40")),
            ("plot-2", datetime(2026, 1, 10), Decimal("30")),
        ]
        payments = [
            ("plot-1", datetime(2026, 1, 20), Decimal("20")),
            ("plot-1", datetime(2026, 2, 20), Decimal("10")),
            ("plot-2", datetime(2026, 2, 20), Decimal("50")),
            ("plot-2", datetime(2026, 4, 5), Decimal("10")),
        ]

        rows = build_monthly_debt_report(
            owner_plots,
            accruals,
            payments,
            as_of=datetime(2026, 4, 15),
            income_rows=[
                (datetime(2026, 2, 5), Decimal("25")),
                (datetime(2026, 4, 5), Decimal("999")),
            ],
        )

        self.assertEqual([row["month"] for row in rows], ["2026-03", "2026-02", "2026-01"])
        self.assertEqual(rows[0]["debt"], Decimal("120"))
        self.assertEqual(rows[0]["overpayment"], Decimal("20"))
        self.assertEqual(rows[0]["debtors"], 1)
        self.assertEqual(rows[1]["accruals"], Decimal("50"))
        self.assertEqual(rows[1]["payments"], Decimal("60"))
        self.assertEqual(rows[1]["other_incomes"], Decimal("25"))
        self.assertEqual(rows[1]["total_incomes"], Decimal("85"))
        self.assertEqual(rows[1]["debt_change"], Decimal("10"))
        self.assertEqual(rows[2]["debt"], Decimal("110"))
        self.assertEqual(rows[2]["debtors"], 2)

    def test_uses_previous_month_for_a_partial_current_month(self):
        rows = build_monthly_debt_report(
            [
                {
                    "owner_plot_id": "plot-1",
                    "owner_id": "owner-1",
                    "debt": Decimal("75"),
                    "overpayment": Decimal("0"),
                }
            ],
            [],
            [],
            as_of=datetime(2026, 8, 15),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["month"], "2026-07")
        self.assertEqual(rows[0]["debt"], Decimal("75"))


if __name__ == "__main__":
    unittest.main()
