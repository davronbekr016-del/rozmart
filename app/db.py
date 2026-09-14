import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_URL = f"sqlite:///{BASE_DIR / 'rozmart.db'}"

# При выкладке в облако достаточно задать переменную окружения DATABASE_URL,
# например postgresql+psycopg://user:pass@host/rozmart — код менять не нужно.
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)

# check_same_thread — параметр драйвера SQLite, для PostgreSQL он недопустим.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """SQLite по умолчанию не проверяет внешние ключи, PostgreSQL — проверяет.

    Без этого битые ссылки не всплывут при разработке и рухнут только в облаке.
    WAL и busy_timeout нужны потому, что запросы FastAPI идут в несколько потоков:
    иначе запись заказа блокирует чтение каталога с ошибкой «database is locked».
    """
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  — регистрирует модели в метаданных

    Base.metadata.create_all(engine)
