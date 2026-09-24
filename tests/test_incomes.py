import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from ecopark_sync.models import Income
from ecopark_sync.syncer import sync_incomes, synced_models_for_snapshot


class IncomeSyncTest(unittest.TestCase):
    @patch("ecopark_sync.syncer.upsert_many")
    def test_syncs_income_fields(self, upsert_many):
        synced_at = datetime(2026, 9, 24, 10, 0)
        snapshot = {
            "incomes": [
                {
                    "id": "bank:income-1",
                    "document_id": "income-1",
                    "document": "Поступление на расчетный счет",
                    "date": "2026-09-01T12:00:00",
                    "number": "000514",
                    "income_category": "Проценты по счёту",
                    "counterparty_id": "bank-1",
                    "counterparty": "Банк",
                    "purpose": "Выплата начисленных процентов",
                    "amount": 795.32,
                    "currency": "RUB",
                    "organization_id": "org-1",
                    "organization": "ТСН Экопарк",
                    "source": "ПоступлениеНаРасчетныйСчет",
                }
            ]
        }

        sync_incomes(object(), snapshot, 10, synced_at)

        rows = upsert_many.call_args.args[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["income_category"], "Проценты по счёту")
        self.assertEqual(rows[0]["amount"], Decimal("795.32"))
        self.assertEqual(rows[0]["date"], datetime(2026, 9, 1, 12, 0))
        self.assertEqual(rows[0]["sync_run_id"], 10)

    def test_old_snapshot_does_not_mark_incomes_as_stale(self):
        self.assertNotIn(Income, synced_models_for_snapshot({}))
        self.assertIn(Income, synced_models_for_snapshot({"incomes": []}))


if __name__ == "__main__":
    unittest.main()
