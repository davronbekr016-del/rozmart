"""Настройка бота доставщиков: вебхук, команды, описание.

    .venv\\Scripts\\python.exe -m scripts.setup_courier_bot          настроить
    .venv\\Scripts\\python.exe -m scripts.setup_courier_bot --show   что зарегистрировано

Нужны COURIER_BOT_TOKEN, COURIER_WEBHOOK_SECRET и PUBLIC_URL.
"""
import argparse
import os
import sys

from app import courier_bot

DESCRIPTION = ("Бот доставщиков ROZMART: свободные заказы, их приём и доставка. "
               "Доступ включает администратор магазина.")


def show() -> int:
    me = courier_bot.call("getMe", {})
    info = courier_bot.call("getWebhookInfo", {})
    print(f"бот:       @{me.get('username')}")
    print(f"вебхук:    {info.get('url') or '— не задан'}")
    print(f"в очереди: {info.get('pending_update_count', 0)}")
    if info.get("last_error_message"):
        print(f"ошибка:    {info['last_error_message']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Настройка бота доставщиков")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    if not courier_bot.TOKEN:
        print("Не задан COURIER_BOT_TOKEN.", file=sys.stderr)
        return 1
    if args.show:
        return show()
    public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not courier_bot.SECRET:
        print("Не задан COURIER_WEBHOOK_SECRET.", file=sys.stderr)
        return 1
    if not public.startswith("https://"):
        print("PUBLIC_URL должен быть адресом сайта вида https://…", file=sys.stderr)
        return 1
    courier_bot.call("setWebhook", {
        "url": public + courier_bot.WEBHOOK_PATH,
        "secret_token": courier_bot.SECRET,
        "allowed_updates": ["message", "callback_query"],
        "max_connections": 20,
    })
    courier_bot.call("setMyCommands", {"commands": [
        {"command": "start", "description": "Начать"},
        {"command": "orders", "description": "Свободные заказы"},
        {"command": "delivery", "description": "Мои заказы в доставке"},
    ]})
    courier_bot.call("setMyDescription", {"description": DESCRIPTION})
    print(f"Вебхук бота доставщиков: {public + courier_bot.WEBHOOK_PATH}")
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
