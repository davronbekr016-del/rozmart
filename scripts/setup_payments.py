"""Настройка оплаты картой: вебхук магазинного бота и проверка токена провайдера.

    .venv\\Scripts\\python.exe -m scripts.setup_payments          настроить вебхук
    .venv\\Scripts\\python.exe -m scripts.setup_payments --show   что зарегистрировано
    .venv\\Scripts\\python.exe -m scripts.setup_payments --check  выставить пробный счёт

Нужны BOT_TOKEN (магазинный бот), PAYMENT_PROVIDER_TOKEN, SHOP_WEBHOOK_SECRET
и PUBLIC_URL.

До оплаты у магазинного бота вебхука не было вовсе. Теперь Telegram шлёт на
него запросы перед списанием и уведомления об оплате — адрес отдельный от
служебного бота, секрет свой.

--check выставляет счёт на 20 000 сумов и печатает ссылку. Денег это не списывает:
счёт — это только ссылка, оплата начинается, когда по ней нажмут «Оплатить».
Проверяет, что Telegram принимает токен провайдера, валюту и суммы в тийинах.
"""
import argparse
import os
import sys

from app import notify, payments
from app.shop_bot import WEBHOOK_PATH
from app.telegram import BOT_TOKEN

# Только то, что обрабатываем. Обычные сообщения покупателей магазинному боту
# тоже приходят как message — обработчик их молча пропускает.
UPDATES = ["pre_checkout_query", "message"]


def call(method: str, payload: dict) -> dict:
    return notify.call(method, payload, token=BOT_TOKEN)


def show() -> int:
    me = call("getMe", {})
    info = call("getWebhookInfo", {})
    print(f"бот:        @{me.get('username')}")
    print(f"вебхук:     {info.get('url') or '— не задан, оплата работать не будет'}")
    print(f"в очереди:  {info.get('pending_update_count', 0)}")
    if info.get("last_error_message"):
        print(f"ошибка:     {info['last_error_message']}")
    print(f"провайдер:  {'задан' if payments.enabled() else 'НЕ задан'}"
          f"{' (тестовый)' if payments.is_test() else ''}")
    if payments.is_test():
        print(f"тестировщики: {sorted(payments.TEST_USERS) or 'никого — оплату картой не увидит никто'}")
    return 0


def check() -> int:
    """Пробный счёт: проверяет токен провайдера, валюту и суммы."""
    link = call("createInvoiceLink", {
        "title": "Проверка оплаты",
        "description": "Пробный счёт: проверяем подключение провайдера",
        "payload": "setup-check",
        "provider_token": payments.TOKEN,
        "currency": payments.CURRENCY,
        "prices": [
            {"label": "Товары", "amount": 8000 * payments.MINOR},
            {"label": "Доставка", "amount": 12000 * payments.MINOR},
        ],
    })
    print("счёт выставлен, Telegram принял токен и суммы:")
    print(" ", link)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Настройка оплаты картой")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--drop-pending", action="store_true",
                        help="выбросить недоставленные обновления — только при первой установке")
    args = parser.parse_args()

    if not BOT_TOKEN:
        print("Не задан BOT_TOKEN магазинного бота.", file=sys.stderr)
        return 1
    if args.show:
        return show()
    if not payments.enabled():
        print("Не задан PAYMENT_PROVIDER_TOKEN.", file=sys.stderr)
        return 1
    if args.check:
        return check()

    secret = os.getenv("SHOP_WEBHOOK_SECRET", "").strip()
    public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not secret:
        print("Не задан SHOP_WEBHOOK_SECRET — приложение не примет обновления.",
              file=sys.stderr)
        return 1
    if not public.startswith("https://"):
        print("PUBLIC_URL должен быть адресом сайта вида https://…", file=sys.stderr)
        return 1

    call("setWebhook", {
        "url": public + WEBHOOK_PATH,
        "secret_token": secret,
        "allowed_updates": UPDATES,
        # Очередь по умолчанию НЕ сбрасываем: при повторной настройке (смена
        # адреса или секрета) в ней могут лежать недоставленные уведомления
        # об оплате — выбросить их значит потерять оплаченные заказы.
        # Сбросить можно только явно и только при первой установке
        "drop_pending_updates": args.drop_pending,
        "max_connections": 20,
    })
    print(f"Вебхук магазинного бота: {public + WEBHOOK_PATH}")
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
