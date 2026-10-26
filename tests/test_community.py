import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ecopark_sync.community import CommunityProcessor, seed_community_defaults
from ecopark_sync.models import (
    Base,
    CommunityUser,
    DirectoryEntry,
    NotificationDestination,
    NotificationSubscription,
    NotificationType,
    OutageEvent,
    ReputationEvent,
)
from ecopark_sync.outage_worker import check_notification_type
from ecopark_sync.web import create_app


class FakeSender:
    def __init__(self):
        self.messages = []
        self.callbacks = []

    def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.messages.append((str(chat_id), text, reply_markup, kwargs))
        return {"message_id": len(self.messages)}

    def answer_callback_query(self, callback_id):
        self.callbacks.append(callback_id)


class FakeOutageClient:
    def __init__(self, _timeout):
        pass

    def fetch(self, _source):
        return [
            {
                "region": "Новосибирская область",
                "city": "Экопарк",
                "street": "Центральная",
                "shutdownDate": "2026-10-27T08:00:00",
                "restoreDate": "2026-10-27T12:00:00",
            }
        ]


class CommunityTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )
        with self.Session() as session:
            seed_community_defaults(session)
            now = datetime(2026, 10, 26, 12, 0)
            destination = NotificationDestination(
                name="Чат Экопарка",
                chat_id="-100123",
                message_thread_id=None,
                reputation_enabled=True,
                directory_enabled=True,
                reputation_cooldown_hours=24,
                thanks_phrases="спасибо\nблагодарю",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(destination)
            session.flush()
            session.add(
                DirectoryEntry(
                    destination_id=destination.id,
                    name="Диспетчер",
                    category="Аварийные службы",
                    phone="112",
                    description="Круглосуточно",
                    url=None,
                    keywords="авария, помощь",
                    enabled=True,
                    position=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            type_id = session.scalar(select(NotificationType.id))
            session.add(
                NotificationSubscription(
                    notification_type_id=type_id,
                    destination_id=destination.id,
                    created_at=now,
                )
            )
            session.commit()
            self.destination_id = destination.id
            self.type_id = type_id

    def tearDown(self):
        self.engine.dispose()

    def test_directory_menu_and_callback(self):
        sender = FakeSender()
        with self.Session() as session:
            processor = CommunityProcessor(session, sender, bot_user_id=999)
            processor.process_update(
                {
                    "message": {
                        "message_id": 10,
                        "chat": {"id": -100123, "type": "supergroup"},
                        "from": {"id": 1, "first_name": "Анна"},
                        "text": "/contacts",
                    }
                }
            )
            session.commit()
            entry_id = session.scalar(select(DirectoryEntry.id))
            processor.process_update(
                {
                    "callback_query": {
                        "id": "callback-1",
                        "data": f"directory:{entry_id}",
                        "message": {
                            "message_id": 11,
                            "chat": {"id": -100123, "type": "supergroup"},
                        },
                    }
                }
            )

        self.assertIn("Полезные контакты", sender.messages[0][1])
        self.assertIn("Диспетчер", sender.messages[1][1])
        self.assertEqual(sender.callbacks, ["callback-1"])

    def test_thanks_awards_reputation_once(self):
        sender = FakeSender()
        update = {
            "message": {
                "message_id": 20,
                "chat": {"id": -100123, "type": "supergroup"},
                "from": {"id": 1, "first_name": "Анна"},
                "text": "Спасибо!",
                "reply_to_message": {
                    "message_id": 19,
                    "from": {"id": 2, "first_name": "Борис"},
                },
            }
        }
        with self.Session() as session:
            processor = CommunityProcessor(session, sender, bot_user_id=999)
            processor.process_update(update)
            processor.process_update(update)
            session.commit()
            receiver = session.scalar(
                select(CommunityUser).where(CommunityUser.telegram_user_id == 2)
            )
            event_count = len(session.scalars(select(ReputationEvent)).all())

        self.assertEqual(receiver.reputation, 1)
        self.assertEqual(event_count, 1)
        self.assertIn("благодарит", sender.messages[0][1])

    def test_outage_sources_are_grouped_into_one_delivery(self):
        sender = FakeSender()
        with self.Session() as session:
            result = check_notification_type(
                session,
                self.type_id,
                client_factory=FakeOutageClient,
                sender=sender,
            )
            events = session.scalars(select(OutageEvent)).all()

        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.matched, 2)
        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.sent, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("В Экопарке не будет электричества", sender.messages[0][1])

    def test_admin_page_and_force_check(self):
        with patch("ecopark_sync.community_web.make_session_factory", return_value=self.Session), patch(
            "ecopark_sync.web.make_session_factory", return_value=self.Session
        ):
            client = create_app().test_client()
            page = client.get("/admin/community")
            response = client.post("/admin/community/check-now")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Сообщество и оповещения", page.get_data(as_text=True))
        self.assertEqual(response.status_code, 302)
        with self.Session() as session:
            row = session.get(NotificationType, self.type_id)
            self.assertTrue(row.force_check_requested)


if __name__ == "__main__":
    unittest.main()
