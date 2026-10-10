import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ecopark_sync.models import (
    Base,
    Expense,
    ExpenseAllocation,
    ExpenseAllocationRule,
    ExpenseBudgetCategory,
)
from ecopark_sync.web import create_app


class ExpenseAllocationRoutesTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
        with self.Session() as session:
            session.add_all(
                [
                    ExpenseBudgetCategory(
                        id="road-snow",
                        name="Чистка дорог от снега",
                        sort_order=1,
                        active=True,
                    ),
                    ExpenseBudgetCategory(
                        id="electricity",
                        name="Электроэнергия",
                        sort_order=2,
                        active=True,
                    ),
                    ExpenseBudgetCategory(
                        id="security",
                        name="Охрана",
                        sort_order=3,
                        active=True,
                    ),
                    Expense(
                        id="expense-1",
                        document_id="document-1",
                        document="Списание с расчетного счета",
                        date=datetime(2026, 10, 1, 12, 0),
                        number="123",
                        expense_category_id="source-category",
                        expense_category="Оплата поставщикам",
                        counterparty_id="counterparty-1",
                        counterparty="ООО Подрядчик",
                        purpose="Смешанный расход",
                        receipt_items_json="[]",
                        allocation_mode="auto",
                        amount=Decimal("1000.00"),
                        currency="RUB",
                        organization_id="org-1",
                        organization="ТСН Экопарк",
                        source="1C",
                        sync_run_id=1,
                        synced_at=datetime(2026, 10, 1, 13, 0),
                    ),
                ]
            )
            session.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_saves_split_as_manual_and_renders_detail(self):
        with (
            patch("ecopark_sync.web.make_session_factory", return_value=self.Session),
            patch(
                "ecopark_sync.expense_report.make_session_factory",
                return_value=self.Session,
            ),
        ):
            client = create_app().test_client()
            response = client.post(
                "/admin/expenses/expense-1/allocations",
                data={
                    "allocation_road-snow": "600,00",
                    "allocation_electricity": "300.00",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Ручное разнесение сохранено", page)
        self.assertIn("Не распределено: 100.00", page)

        with self.Session() as session:
            expense = session.get(Expense, "expense-1")
            allocations = session.scalars(
                select(ExpenseAllocation).order_by(ExpenseAllocation.category_id)
            ).all()

        self.assertEqual(expense.allocation_mode, "manual")
        self.assertEqual(len(allocations), 2)
        self.assertEqual(
            sum((allocation.amount for allocation in allocations), Decimal("0")),
            Decimal("900.00"),
        )

    def test_creates_purpose_rule_and_recalculates_auto_expenses(self):
        with self.Session() as session:
            expense = session.get(Expense, "expense-1")
            expense.expense_category = "Выплата заработной платы"
            expense.purpose = "Заработная плата за Ильиных Петра Сергеевича"
            session.commit()

        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            response = create_app().test_client().post(
                "/admin/expense-rules",
                data={
                    "category_id": "security",
                    "purpose_contains": "Ильиных Петра Сергеевича",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Правило добавлено", page)
        self.assertIn("Ильиных Петра Сергеевича", page)

        with self.Session() as session:
            rule = session.scalar(select(ExpenseAllocationRule))
            allocation = session.scalar(select(ExpenseAllocation))

        self.assertEqual(rule.category_id, "security")
        self.assertEqual(allocation.category_id, "security")
        self.assertEqual(allocation.amount, Decimal("1000.00"))

    def test_rejects_allocation_above_expense_total(self):
        with patch("ecopark_sync.web.make_session_factory", return_value=self.Session):
            response = create_app().test_client().post(
                "/admin/expenses/expense-1/allocations",
                data={
                    "allocation_road-snow": "700.00",
                    "allocation_electricity": "400.00",
                },
            )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
