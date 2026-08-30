import unittest
from datetime import datetime
from unittest.mock import patch

from ecopark_sync.syncer import sync_plots


class PlotSyncTest(unittest.TestCase):
    @patch("ecopark_sync.syncer.upsert_many")
    def test_sync_preserves_one_time_cadastral_number(self, upsert_many):
        snapshot = {
            "plots": [
                {
                    "id": "plot-1",
                    "plot_number": "42А",
                    "account": "000042",
                    "address": (
                        r"Российская Федерация, Новосибирская область, "
                        r"микрорайон \Экопарк\\\", з/у № 42А\""
                    ),
                    "cadastral_number": "this field must be ignored",
                    "organization_id": "org-1",
                    "organization": "ТСН «МИКРОРАЙОН ЭКОПАРК»",
                }
            ]
        }
        synced_at = datetime(2026, 8, 30, 12, 0)

        sync_plots(object(), snapshot, 7, synced_at)

        rows = upsert_many.call_args.args[2]
        self.assertNotIn("cadastral_number", rows[0])
        self.assertEqual(
            rows[0]["address"],
            "Российская Федерация, Новосибирская область, "
            "микрорайон Экопарк, з/у № 42А",
        )
        self.assertEqual(
            upsert_many.call_args.kwargs["preserve_columns"],
            ("cadastral_number",),
        )
        self.assertEqual(rows[0]["sync_run_id"], 7)
        self.assertEqual(rows[0]["synced_at"], synced_at)


if __name__ == "__main__":
    unittest.main()
