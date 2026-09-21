"""Подтянуть статусы заказов из REGOS.

Приложение делает это само раз в минуту. Скрипт нужен, чтобы посмотреть
расхождение руками и чтобы выровнять базу без рассылки сообщений — например,
сразу после включения синхронизации, когда расхождение копилось днями.

    .venv\\Scripts\\python.exe -m scripts.sync_orders
    .venv\\Scripts\\python.exe -m scripts.sync_orders --quiet   без сообщений
    .venv\\Scripts\\python.exe -m scripts.sync_orders --list    только показать
"""
import argparse
import logging
import sys

from app.db import SessionLocal, init_db
from app.regos import config
from app.regos.client import RegosClient
from app.regos.orders_sync import STATUS, fetch_statuses, sync, target_status, watched


def show(db) -> int:
    orders = watched(db)
    if not orders:
        print("Выгруженных незакрытых заказов нет.")
        return 0
    statuses = fetch_statuses(RegosClient(), [o.regos_document_id for o in orders])
    print(f"{'Заказ':10} {'Документ':9} {'Витрина':12} {'REGOS':20} Что будет")
    for order in orders:
        status = statuses.get(order.regos_document_id) or {}
        new = target_status(order, status) if status else None
        print(f"{order.number:10} {order.regos_document_id:<9} {order.status:12} "
              f"{(status.get('name') or 'не найден'):20} {new or '—'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Статусы заказов из REGOS")
    parser.add_argument("--list", action="store_true", help="показать, ничего не меняя")
    parser.add_argument("--quiet", action="store_true",
                        help="изменить состояния, но не писать сотрудникам")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not config.is_configured():
        print("Не задан REGOS_KEY.", file=sys.stderr)
        return 1

    init_db()
    db = SessionLocal()
    try:
        if args.list:
            return show(db)
        result = sync(db, notify_staff=not args.quiet)
        print(f"Проверено заказов: {result['checked']}")
        for change in result["changed"]:
            print(f"  {change['number']}: {change['from']} -> {change['to']} "
                  f"(REGOS: {change['regos']})")
        if not result["changed"]:
            print("Расхождений нет.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
