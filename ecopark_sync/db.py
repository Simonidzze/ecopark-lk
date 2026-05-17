from urllib.parse import quote_plus

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
except ModuleNotFoundError:
    create_engine = None
    sessionmaker = None

from .config import env, require_dependency


def database_url():
    require_dependency(create_engine, "SQLAlchemy")
    user = quote_plus(env("MYSQL_USER", required=True))
    password = quote_plus(env("MYSQL_PASSWORD", required=True))
    host = env("MYSQL_HOST", "127.0.0.1")
    port = int(env("MYSQL_PORT", "3306"))
    database = quote_plus(env("MYSQL_DATABASE", required=True))
    return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"


def get_engine():
    return create_engine(database_url(), pool_pre_ping=True, future=True)


def make_session_factory(engine=None):
    require_dependency(sessionmaker, "SQLAlchemy")
    return sessionmaker(bind=engine or get_engine(), autoflush=False, expire_on_commit=False, future=True)


def upsert_many(session, model, rows, key_columns=("id",)):
    if not rows:
        return

    from sqlalchemy.dialects.mysql import insert

    key_columns = set(key_columns)
    statement = insert(model).values(rows)
    updates = {
        column.name: statement.inserted[column.name]
        for column in model.__table__.columns
        if column.name not in key_columns
    }
    session.execute(statement.on_duplicate_key_update(**updates))
