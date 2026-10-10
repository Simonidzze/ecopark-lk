import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from ecopark_sync.expense_allocations import (
    build_auto_allocations,
    refresh_auto_expense_allocations,
    serialize_receipt_items,
)


class ExpenseAutoAllocationTest(unittest.TestCase):
    def expense(self, **values):
        defaults = {
            "id": "expense-1",
            "amount": Decimal("1000.00"),
            "expense_category": "Оплата поставщикам (подрядчикам)",
            "counterparty": "",
            "purpose": "",
            "document": "Списание с расчетного счета",
            "receipt_items_json": "[]",
            "allocation_mode": "auto",
        }
        defaults.update(values)
        return SimpleNamespace(**defaults)

    def test_classifies_real_onec_snow_purpose(self):
        allocations, reason = build_auto_allocations(
            self.expense(purpose="Оплата за чистку снега по счету 2/12")
        )

        self.assertEqual(reason, "operation")
        self.assertEqual(
            allocations["road-snow"]["amount"],
            Decimal("1000.00"),
        )

    def test_classifies_known_counterparties_and_target_purchase(self):
        electricity, _reason = build_auto_allocations(
            self.expense(counterparty="НОВОСИБИРСКЭНЕРГОСБЫТ АО")
        )
        pump, _reason = build_auto_allocations(
            self.expense(purpose="Оплата за насос по счету №155")
        )

        self.assertEqual(
            electricity["electricity"]["amount"],
            Decimal("1000.00"),
        )
        self.assertEqual(
            pump["target-contributions"]["amount"],
            Decimal("1000.00"),
        )

    def test_custom_purpose_rule_assigns_full_expense(self):
        allocations, reason = build_auto_allocations(
            self.expense(
                purpose="Заработная плата за Ильиных Петра Сергеевича",
                receipt_items_json=serialize_receipt_items(
                    [{"name": "Неизвестная позиция", "amount": "200.00"}]
                ),
            ),
            purpose_rules=[
                {
                    "category_id": "security",
                    "purpose_contains": "ильиных петра сергеевича",
                }
            ],
        )

        self.assertEqual(reason, "purpose-rule")
        self.assertEqual(
            allocations["security"]["amount"],
            Decimal("1000.00"),
        )
        self.assertIn("Ильиных", allocations["security"]["details"].title())

    def test_custom_rule_classifies_payroll_detail_item(self):
        allocations, reason = build_auto_allocations(
            self.expense(
                purpose="Заработная плата по реестру",
                receipt_items_json=serialize_receipt_items(
                    [
                        {
                            "name": "Зарплата: Костенко Татьяна Гергартовна",
                            "amount": "1000.00",
                            "kind": "payroll",
                        }
                    ]
                ),
            ),
            purpose_rules=[
                {
                    "category_id": "accountant",
                    "purpose_contains": "Костенко Татьяна Гергартовна",
                }
            ],
        )

        self.assertEqual(reason, "receipt")
        self.assertEqual(
            allocations["accountant"]["amount"],
            Decimal("1000.00"),
        )

    def test_splits_recognized_receipt_items_and_leaves_unknown_remainder(self):
        receipt_items_json = serialize_receipt_items(
            [
                {"name": "Фонарь уличный", "amount": "250.00"},
                {"name": "Фитинг для водопровода", "amount": "350.00"},
                {"name": "Неизвестный товар", "amount": "400.00"},
            ]
        )
        allocations, reason = build_auto_allocations(
            self.expense(receipt_items_json=receipt_items_json)
        )

        self.assertEqual(reason, "receipt")
        self.assertEqual(
            allocations["street-lighting"]["amount"],
            Decimal("250.00"),
        )
        self.assertEqual(
            allocations["water-supply"]["amount"],
            Decimal("350.00"),
        )
        self.assertEqual(
            sum(item["amount"] for item in allocations.values()),
            Decimal("600.00"),
        )

    def test_keeps_ambiguous_expense_unallocated(self):
        allocations, reason = build_auto_allocations(
            self.expense(
                expense_category="Выдача подотчетных сумм",
                counterparty="Дорогин Артем Владимирович",
                purpose="Перевод подотчетных средств",
            )
        )

        self.assertEqual(allocations, {})
        self.assertEqual(reason, "")

    def test_does_not_touch_manual_allocation_during_sync(self):
        session = Mock()
        expense = self.expense(allocation_mode="manual")

        refreshed = refresh_auto_expense_allocations(session, [expense])

        self.assertEqual(refreshed, 0)
        session.execute.assert_not_called()
        session.add.assert_not_called()


if __name__ == "__main__":
    unittest.main()
