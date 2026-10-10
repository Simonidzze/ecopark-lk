import json
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import delete, select

from .models import Expense, ExpenseAllocation, ExpenseAllocationRule
from .utils import now_utc_naive, parse_decimal


MONEY = Decimal("0.01")

BUDGET_CATEGORIES = (
    ("road-snow", "Чистка дорог от снега (по необходимости и заявкам)"),
    ("firebreak", "Противопожарная опашка периметра"),
    ("road-grading", "Отсыпка (выравнивание) дорог (без инертных материалов)"),
    ("roadside-mowing", "Покос обочин вдоль центральной дороги"),
    ("water-supply", "Системы ХВС, расходники"),
    ("waste-removal", "Вывоз мусора"),
    ("reserve-fund", "Резервный фонд"),
    ("street-lighting", "Обслуживание уличного освещения"),
    ("electricity", "Электроэнергия"),
    ("communications", "Оплата связи, Интернет, видеонаблюдение"),
    ("guard-house-rent", "Аренда домика охраны и пр."),
    ("it-platform", "Ай-Ти платформа для управления и личного кабинета"),
    ("target-contributions", "Целевые взносы (закуп насоса для скважины, резерв)"),
    ("security", "Охрана"),
    ("chairman", "Председатель"),
    ("accountant", "Бухгалтер"),
    ("plumber", "Сантехник"),
)

DEFAULT_PURPOSE_RULES = (
    ("chairman", "Дорогин Артем Владимирович"),
    ("accountant", "Костенко Татьяна Гергартовна"),
    ("security", "Ильиных Петр Сергеевич"),
    ("security", "Ильиных Петра Сергеевича"),
    ("security", "Леготин Юрий Валентинович"),
    ("security", "Черников Константин Васильевич"),
    ("security", "Южаков Василий Николаевич"),
    ("security", "Яковец Валентин Иванович"),
)

PAYROLL_EMPLOYEE_FRAGMENTS = frozenset(
    fragment for _category_id, fragment in DEFAULT_PURPOSE_RULES
)

CATEGORY_NAMES = dict(BUDGET_CATEGORIES)

AUTO_RULES = (
    (
        "target-contributions",
        (
            r"целев(?:ой|ые|ого|ому|ым|ых)?\s+(?:взнос|финанс)",
            r"оплат\w*\s+за\s+насос",
            r"насос.{0,40}скваж",
            r"скваж.{0,40}насос",
        ),
    ),
    (
        "guard-house-rent",
        (
            r"аренд\w*.{0,40}(?:домик|бытовк|охран)",
            r"(?:домик|бытовк).{0,40}аренд",
            r"оплат\w*\s+аренд",
        ),
    ),
    ("chairman", (r"председател",)),
    ("accountant", (r"бухгалтер",)),
    (
        "plumber",
        (
            r"зарплат\w*.{0,30}сантехник",
            r"оплат\w*.{0,30}сантехник",
            r"услуг\w*.{0,30}сантехник",
        ),
    ),
    (
        "road-snow",
        (
            r"чистк\w*\s+снег",
            r"снегоубор",
            r"уборк\w*.{0,20}снег",
            r"чистк\w*.{0,20}дорог.{0,20}снег",
        ),
    ),
    ("firebreak", (r"противопожар", r"опашк")),
    (
        "roadside-mowing",
        (
            r"покос",
            r"косил",
            r"скос\w*.{0,30}трав",
            r"обочин",
        ),
    ),
    (
        "waste-removal",
        (
            r"обращение\s+с\s+тко",
            r"вывоз\w*.{0,20}мусор",
            r"тверды\w*\s+коммунальн\w*\s+отход",
            r"\bтко\b",
            r"\bсах\b",
        ),
    ),
    (
        "it-platform",
        (
            r"личн\w*\s+кабинет",
            r"ай[\s-]?ти\s+платформ",
            r"\bit[\s-]?платформ",
            r"хостинг",
            r"домен",
            r"разработк\w*.{0,30}(?:сайт|кабинет|платформ|программ)",
        ),
    ),
    (
        "communications",
        (
            r"интернет",
            r"видео[\s-]?наблюд",
            r"камер\w*\s+(?:наблюден|видео)",
            r"оплат\w*.{0,20}связ",
            r"сим[\s-]?карт",
        ),
    ),
    (
        "security",
        (
            r"\bчоп\b",
            r"услуг\w*.{0,30}охран",
            r"оплат\w*.{0,30}охран",
            r"заработн\w*.{0,30}охран",
            r"сторож",
        ),
    ),
    (
        "street-lighting",
        (
            r"уличн\w*\s+освещ",
            r"освещен\w*.{0,30}(?:улиц|дорог|территор)",
            r"фонар",
            r"светильник",
            r"светодиодные\s+решения",
        ),
    ),
    (
        "electricity",
        (
            r"электроэнерг",
            r"электричеств",
            r"энергосбыт",
            r"киловат",
            r"новосибирскэнергосбыт",
        ),
    ),
    (
        "water-supply",
        (
            r"\bхвс\b",
            r"водоснабж",
            r"водопровод",
            r"скваж",
            r"фитинг",
            r"труб\w*.{0,25}(?:вод|пнд|кран|муфт)",
            r"сантехнич\w*\s+(?:материал|расходник|товар)",
        ),
    ),
    (
        "road-grading",
        (
            r"отсыпк",
            r"грейдер",
            r"выравнив\w*.{0,25}дорог",
            r"планировк\w*.{0,25}дорог",
            r"ремонт\w*.{0,25}дорог",
        ),
    ),
    ("reserve-fund", (r"резервн\w*\s+фонд",)),
)


def money(value):
    return Decimal(value or 0).quantize(MONEY, rounding=ROUND_HALF_UP)


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "").lower().replace("ё", "е")).strip()


def load_active_purpose_rules(session):
    rows = session.scalars(
        select(ExpenseAllocationRule).where(ExpenseAllocationRule.active.is_(True))
    ).all()
    return sorted(
        rows,
        key=lambda rule: (-len(normalize_text(rule.purpose_contains)), rule.id or 0),
    )


def is_payroll_employee_fragment(value):
    normalized_value = normalize_text(value)
    return any(
        normalized_value == normalize_text(fragment)
        for fragment in PAYROLL_EMPLOYEE_FRAGMENTS
    )


def is_payroll_expense(expense):
    expense_category = normalize_text(getattr(expense, "expense_category", ""))
    return "зарплат" in expense_category or "заработн" in expense_category


def is_payroll_detail(item):
    return normalize_text(item.get("kind")) in {"payroll", "payroll_tax"}


def match_purpose_rule(purpose, purpose_rules, allow_payroll_employee_rules=True):
    normalized_purpose = normalize_text(purpose)
    if not normalized_purpose:
        return None, ""
    for rule in purpose_rules or ():
        if isinstance(rule, dict):
            category_id = rule.get("category_id")
            fragment = rule.get("purpose_contains")
        elif isinstance(rule, (tuple, list)):
            category_id, fragment = rule[:2]
        else:
            category_id = getattr(rule, "category_id", None)
            fragment = getattr(rule, "purpose_contains", "")
        normalized_fragment = normalize_text(fragment)
        if (
            not allow_payroll_employee_rules
            and is_payroll_employee_fragment(normalized_fragment)
        ):
            continue
        if category_id and normalized_fragment and normalized_fragment in normalized_purpose:
            return category_id, str(fragment).strip()
    return None, ""


def choose_budget_category(*values):
    haystack = " ".join(normalize_text(value) for value in values if value)
    if not haystack:
        return None
    for category_id, patterns in AUTO_RULES:
        if any(re.search(pattern, haystack) for pattern in patterns):
            return category_id
    return None


def normalize_receipt_items(value):
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if isinstance(value, dict):
        for key in ("items", "positions", "lines", "goods"):
            nested = value.get(key)
            if isinstance(nested, list):
                value = nested
                break
        else:
            value = [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def serialize_receipt_items(value):
    return json.dumps(normalize_receipt_items(value), ensure_ascii=False, separators=(",", ":"))


def expense_receipt_items(expense):
    return normalize_receipt_items(getattr(expense, "receipt_items_json", "[]"))


def receipt_item_text(item):
    return " ".join(
        str(item.get(key) or "")
        for key in (
            "name",
            "title",
            "product",
            "item",
            "description",
            "purpose",
            "category",
            "department",
        )
    ).strip()


def receipt_item_amount(item):
    for key in ("amount", "sum", "total", "line_total"):
        if item.get(key) not in (None, ""):
            return money(parse_decimal(item.get(key)))
    quantity = parse_decimal(item.get("quantity"))
    price = parse_decimal(item.get("price"))
    if quantity and price:
        return money(quantity * price)
    return Decimal("0.00")


def build_auto_allocations(expense, purpose_rules=()):
    expense_amount = max(money(getattr(expense, "amount", 0)), Decimal("0.00"))
    if expense_amount <= 0:
        return {}, ""

    category_id, matched_fragment = match_purpose_rule(
        getattr(expense, "purpose", ""),
        purpose_rules,
        allow_payroll_employee_rules=is_payroll_expense(expense),
    )
    if category_id:
        return {
            category_id: {
                "amount": expense_amount,
                "details": f"Правило назначения: «{matched_fragment}»",
            }
        }, "purpose-rule"

    item_allocations = defaultdict(Decimal)
    item_names = defaultdict(list)
    for item in expense_receipt_items(expense):
        item_amount = receipt_item_amount(item)
        item_text = receipt_item_text(item)
        category_id, _matched_fragment = match_purpose_rule(
            item_text,
            purpose_rules,
            allow_payroll_employee_rules=is_payroll_detail(item),
        )
        if not category_id:
            category_id = choose_budget_category(item_text)
        if category_id and item_amount > 0:
            item_allocations[category_id] += item_amount
            if item_text:
                item_names[category_id].append(item_text)

    if item_allocations:
        allocated_total = sum(item_allocations.values(), Decimal("0.00"))
        if allocated_total > expense_amount:
            coefficient = expense_amount / allocated_total
            scaled = {
                category_id: money(amount * coefficient)
                for category_id, amount in item_allocations.items()
            }
            difference = expense_amount - sum(scaled.values(), Decimal("0.00"))
            if difference:
                largest = max(scaled, key=scaled.get)
                scaled[largest] += difference
            item_allocations = scaled
        return (
            {
                category_id: {
                    "amount": money(amount),
                    "details": "Детализация из 1С: " + "; ".join(item_names[category_id][:3]),
                }
                for category_id, amount in item_allocations.items()
                if amount > 0
            },
            "receipt",
        )

    category_id = choose_budget_category(
        getattr(expense, "expense_category", ""),
        getattr(expense, "counterparty", ""),
        getattr(expense, "purpose", ""),
        getattr(expense, "document", ""),
    )
    if not category_id:
        return {}, ""
    return {
        category_id: {
            "amount": expense_amount,
            "details": "Распознано по данным операции 1С",
        }
    }, "operation"


def refresh_auto_expense_allocations(
    session,
    expenses,
    force=False,
    purpose_rules=None,
):
    expenses = [
        expense
        for expense in expenses
        if force or getattr(expense, "allocation_mode", "auto") != "manual"
    ]
    if not expenses:
        return 0
    if purpose_rules is None:
        purpose_rules = load_active_purpose_rules(session)

    refreshed = 0
    for expense in expenses:
        expense.allocation_mode = "auto"
        session.execute(
            delete(ExpenseAllocation).where(ExpenseAllocation.expense_id == expense.id)
        )
        allocations, _reason = build_auto_allocations(
            expense,
            purpose_rules=purpose_rules,
        )
        timestamp = now_utc_naive()
        for category_id, allocation in allocations.items():
            session.add(
                ExpenseAllocation(
                    expense_id=expense.id,
                    category_id=category_id,
                    amount=allocation["amount"],
                    source="auto",
                    details=allocation["details"],
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        refreshed += 1
    return refreshed


def refresh_auto_expense_allocations_for_ids(session, expense_ids):
    expense_ids = [expense_id for expense_id in expense_ids if expense_id]
    if not expense_ids:
        return 0
    expenses = session.scalars(
        select(Expense).where(Expense.id.in_(expense_ids))
    ).all()
    return refresh_auto_expense_allocations(session, expenses)


def replace_manual_allocations(session, expense, amounts):
    session.execute(
        delete(ExpenseAllocation).where(ExpenseAllocation.expense_id == expense.id)
    )
    timestamp = now_utc_naive()
    for category_id, amount in amounts.items():
        amount = money(amount)
        if amount <= 0:
            continue
        session.add(
            ExpenseAllocation(
                expense_id=expense.id,
                category_id=category_id,
                amount=amount,
                source="manual",
                details="Ручное разнесение",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    expense.allocation_mode = "manual"
