import unittest
from datetime import datetime
from io import StringIO

from ecopark_sync.calls import read_call_report


class CallReportTest(unittest.TestCase):
    def test_campaign_date_comes_from_report_creation_column(self):
        report = StringIO(
            "\n".join(
                [
                    '"Обзвон №123"',
                    '"Рассылка";"2026-06-01 09:00:00";"Дата создания";"2026-06-02 12:30:00"',
                    '"Номер";"Длительность";"Менеджер";"Дата звонка";"Стоимость";"Комментарий"',
                    '"+7 999 000-00-01";"60";"30";"2026-06-01 10:00:00";"5,25";""',
                ]
            )
        )

        campaign, attempts = read_call_report(report, source_file="calls.csv")

        self.assertEqual(campaign["called_at"], datetime(2026, 6, 2, 12, 30))
        self.assertEqual(campaign["report_created_at"], datetime(2026, 6, 2, 12, 30))
        self.assertEqual(attempts[0]["called_at"], datetime(2026, 6, 1, 10))


if __name__ == "__main__":
    unittest.main()
