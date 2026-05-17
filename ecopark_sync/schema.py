from sqlalchemy import inspect, text

from .db import get_engine
from .models import Base


def init_schema():
    engine = get_engine()
    Base.metadata.create_all(engine)
    apply_lightweight_upgrades(engine)


def apply_lightweight_upgrades(engine):
    inspector = inspect(engine)
    columns_by_table = {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
    }

    statements = []
    if "owners" in columns_by_table and "phone" not in columns_by_table["owners"]:
        statements.append("ALTER TABLE owners ADD COLUMN phone VARCHAR(100) NOT NULL DEFAULT '' AFTER name")
    if "owner_plots" in columns_by_table and "phone" not in columns_by_table["owner_plots"]:
        statements.append("ALTER TABLE owner_plots ADD COLUMN phone VARCHAR(100) NOT NULL DEFAULT '' AFTER owner")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
