"""Досоздание недостающих колонок.

Полноценных миграций в проекте нет: раньше база пересоздавалась с нуля, потому
что лежала в SQLite и ничего ценного не содержала. На сервере так больше нельзя
— там заказы покупателей и разобранный вручную каталог.

Alembic под три колонки избыточен, но и молча оставлять базу без новых полей
нельзя: create_all() создаёт недостающие таблицы и НЕ добавляет колонки
в существующие. Приложение поднимется и упадёт позже, на первом же запросе,
причём в неочевидном месте.

Здесь — список изменений, которые нужно донести до уже созданной базы.
Каждое проверяется по факту наличия колонки, поэтому запуск безопасен
и повторяем. Когда изменений станет больше десятка, это надо заменить
на Alembic, а не растить дальше.
"""
import logging

from sqlalchemy import inspect, text

from app.db import engine

log = logging.getLogger(__name__)

# (таблица, колонка, тип). Тип пишется совместимым синтаксисом: одно и то же
# определение должно приниматься и SQLite, и PostgreSQL.
COLUMNS = [
    ("variants", "regos_group_id", "INTEGER"),
    ("orders", "regos_document_id", "INTEGER"),
    ("orders", "regos_error", "TEXT"),
    ("orders", "regos_attempts", "INTEGER DEFAULT 0 NOT NULL"),
    ("orders", "telegram_username", "VARCHAR(64)"),
    # таблица подписчиков появилась раньше, чем в ней понадобилось @имя
    ("notify_subscribers", "username", "VARCHAR(64)"),
    # очередь сообщений появилась раньше, чем сообщения сотрудникам
    ("notifications", "chat_id", "BIGINT"),
    # @имя переехало к чатам, когда бот стал служебным
    ("staff_chats", "username", "VARCHAR(64)"),
    # точка на карте: и у заказа, и в профиле покупателя
    ("orders", "lat", "DOUBLE PRECISION"),
    ("orders", "lon", "DOUBLE PRECISION"),
    ("customers", "lat", "DOUBLE PRECISION"),
    ("customers", "lon", "DOUBLE PRECISION"),
    # дом, подъезд и квартира отдельными полями
    ("customers", "house", "VARCHAR(20)"),
    ("customers", "entrance", "VARCHAR(20)"),
    ("customers", "flat", "VARCHAR(20)"),
    # оплата картой
    ("orders", "paid_at", "TIMESTAMP"),
    ("orders", "payment_charge_id", "VARCHAR(128)"),
    ("orders", "provider_charge_id", "VARCHAR(128)"),
    ("orders", "paid_amount", "INTEGER"),
    ("orders", "checkout_at", "TIMESTAMP"),
]

INDEXES = [
    ("ix_variants_regos_group_id", "variants", "regos_group_id"),
    ("ix_orders_regos_document_id", "orders", "regos_document_id"),
]

# Уникальные индексы отдельно: на них держится защита от двойного проведения
# платежа, и создаются они другой командой. NULL уникальности не мешает —
# и в PostgreSQL, и в SQLite неоплаченных заказов может быть сколько угодно.
UNIQUE_INDEXES = [
    ("ux_orders_payment_charge_id", "orders", "payment_charge_id"),
]


def apply() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            if table not in existing_tables:
                continue                      # таблицу целиком создаст create_all
            columns = {c["name"] for c in inspector.get_columns(table)}
            if column in columns:
                continue
            log.info("Добавляю колонку %s.%s", table, column)
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

        for name, table, column in INDEXES:
            if table not in existing_tables:
                continue
            indexes = {i["name"] for i in inspect(engine).get_indexes(table)}
            if name in indexes:
                continue
            log.info("Создаю индекс %s", name)
            conn.execute(text(f"CREATE INDEX {name} ON {table} ({column})"))

        for name, table, column in UNIQUE_INDEXES:
            if table not in existing_tables:
                continue
            found = inspect(engine)
            # индекс мог появиться и сам: create_all создаёт его для новой таблицы
            indexes = {i["name"] for i in found.get_indexes(table)}
            uniques = {tuple(u["column_names"]) for u in found.get_unique_constraints(table)}
            if name in indexes or (column,) in uniques or any(
                    i.get("unique") and i["column_names"] == [column]
                    for i in found.get_indexes(table)):
                continue
            log.info("Создаю уникальный индекс %s", name)
            conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({column})"))
