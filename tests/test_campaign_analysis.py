import unittest
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ecopark_sync.models import Base, CallAttempt, CallCampaign, OwnerPlot, Payment
from ecopark_sync.web import campaign_analysis


class CampaignAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)

        synced_at = datetime(2026, 6, 1)
        self.owner_plot = OwnerPlot(
            id="owner-plot-1",
            owner_id="owner-1",
            owner="Иванов Иван",
            phone="+7 999 000-00-01",
            plot_id="plot-1",
            plot_number="1",
            account="account-1",
            presentation="Участок 1",
            organization_id="org-1",
            sync_run_id=1,
            synced_at=synced_at,
        )
        self.first_campaign = self.add_campaign(
            external_id="campaign-1",
            called_at=datetime(2026, 6, 1, 9),
            attempt_at=datetime(2026, 6, 1, 10),
        )
        self.same_day_report = self.add_campaign(
            external_id="campaign-1-extra",
            called_at=datetime(2026, 6, 1, 15),
            attempt_at=datetime(2026, 6, 1, 16),
        )
        self.second_campaign = self.add_campaign(
            external_id="campaign-2",
            called_at=datetime(2026, 6, 10, 9),
            attempt_at=datetime(2026, 6, 10, 10),
        )
        self.session.add(self.owner_plot)
        self.add_payment("before-first-call", datetime(2026, 6, 1, 9, 30), "50")
        self.add_payment("after-first-call", datetime(2026, 6, 2), "100")
        self.add_payment("at-second-campaign", datetime(2026, 6, 10, 9), "150")
        self.add_payment("after-second-call", datetime(2026, 6, 11), "200")
        self.add_payment("future-payment", datetime(2026, 6, 21), "300")
        self.session.commit()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def add_campaign(self, external_id, called_at, attempt_at):
        campaign = CallCampaign(
            external_id=external_id,
            title=external_id,
            caller_phone="",
            called_at=called_at,
            report_created_at=called_at,
            total_calls=1,
            callbacks=0,
            source_file=f"{external_id}.csv",
            imported_at=called_at,
        )
        self.session.add(campaign)
        self.session.flush()
        self.session.add(
            CallAttempt(
                campaign_id=campaign.id,
                phone="+7 999 000-00-01",
                phone_normalized="79990000001",
                call_duration_seconds=60,
                manager_duration_seconds=30,
                called_at=attempt_at,
                cost=Decimal("5"),
                comment="",
                source_file=f"{external_id}.csv",
            )
        )
        return campaign

    def add_payment(self, payment_id, date, amount):
        self.session.add(
            Payment(
                id=payment_id,
                document_id=payment_id,
                document=payment_id,
                date=date,
                number=payment_id,
                owner_plot_id=self.owner_plot.id,
                amount=Decimal(amount),
                payment_type="Оплата",
                incoming_number="",
                incoming_date=date,
                registry_file="registry.csv",
                source="1c",
                sync_run_id=1,
                synced_at=datetime(2026, 6, 20),
            )
        )

    def test_old_campaign_stops_at_next_campaign(self):
        rows, _unmatched, stats = campaign_analysis(
            self.session,
            self.first_campaign,
            as_of=datetime(2026, 6, 20),
        )

        self.assertEqual(rows[0]["payment_count"], 1)
        self.assertEqual(rows[0]["payment_sum"], Decimal("100.00"))
        self.assertEqual(stats["paid_owner_plots"], 1)
        self.assertEqual(stats["payment_sum"], Decimal("100.00"))

    def test_another_report_from_same_day_does_not_end_payment_period(self):
        rows, _unmatched, stats = campaign_analysis(
            self.session,
            self.same_day_report,
            as_of=datetime(2026, 6, 20),
        )

        self.assertEqual(rows[0]["payment_count"], 1)
        self.assertEqual(rows[0]["payment_sum"], Decimal("100.00"))
        self.assertEqual(stats["payment_sum"], Decimal("100.00"))

    def test_latest_campaign_stops_at_current_time(self):
        rows, _unmatched, stats = campaign_analysis(
            self.session,
            self.second_campaign,
            as_of=datetime(2026, 6, 20),
        )

        self.assertEqual(rows[0]["payment_count"], 1)
        self.assertEqual(rows[0]["payment_sum"], Decimal("200.00"))
        self.assertEqual(stats["paid_owner_plots"], 1)
        self.assertEqual(stats["payment_sum"], Decimal("200.00"))


if __name__ == "__main__":
    unittest.main()
