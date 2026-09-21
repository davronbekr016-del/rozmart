"""Выгрузка подтверждённых заказов в REGOS.

Основную отправку делает само приложение сразу после оформления. Этот скрипт —
подстраховка: подбирает заказы, которые тогда уехать не смогли (REGOS был
недоступен, сеть моргнула, сервис перезапускался). Запускается по таймеру.

    .venv\\Scripts\\python.exe -m scripts.push_orders
    .venv\\Scripts\\python.exe -m scripts.push_orders --list   только показать
"""
import argparse
import logging
import sys

from app.db import SessionLocal, init_db
from app.regos import config
from app.regos.orders_push import pending, push_pending


def main() -> int:
    parser = argparse.ArgumentParser(description="Выгрузка заказов в REGOS")
    parser.add_argument("--list", action="store_true",
                        help="показать ожидающие заказы, ничего не отправляя")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not config.orders_enabled():
        print("Выгрузка выключена: не задан REGOS_ORDER_FROM_ID.\n"
              "Заведите источник заказов в справочнике DeliveryFrom и укажите его id.",
              file=sys.stderr)
        return 2

    init_db()
    db = SessionLocal()
    try:
        if args.list:
            rows = pending(db)
            print(f"ожидают выгрузки: {len(rows)}")
            for order in rows:
                note = f" — {order.regos_error}" if order.regos_error else ""
                print(f"  {order.number}  {order.status}  попыток {order.regos_attempts}{note}")
            return 0

        report = push_pending(db)
        print(f"отправлено {report['sent']}, с ошибкой {report['failed']}, "
              f"осталось {report.get('pending', 0)}")
        return 1 if report["failed"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
