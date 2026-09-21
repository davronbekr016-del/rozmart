"""Настройка информационного бота в Telegram.

Регистрирует адрес, по которому Telegram будет присылать «Старт» покупателя,
и заполняет описание бота. Запускается один раз после выкладки и ещё раз,
если сменились адрес сайта или токен.

    .venv\\Scripts\\python.exe -m scripts.setup_bot
    .venv\\Scripts\\python.exe -m scripts.setup_bot --show     только показать
    .venv\\Scripts\\python.exe -m scripts.setup_bot --remove   отключить вебхук

Нужны переменные окружения NOTIFY_BOT_TOKEN, NOTIFY_WEBHOOK_SECRET и
PUBLIC_URL (адрес сайта, на котором работает приложение).
"""
import argparse
import os
import sys

from app import notify
from app.bot import WEBHOOK_PATH

# Telegram шлёт всё подряд — от нажатия кнопки до изменений в каналах.
# Просим только то, что обрабатываем: остальное было бы лишней нагрузкой
# и лишними записями в журнале.
UPDATES = ["message", "my_chat_member"]

COMMANDS = [
    {"command": "start", "description": "Включить сообщения о заказах"},
    {"command": "stop", "description": "Больше не писать"},
]

DESCRIPTION = (
    "Информационный бот ROZMART. Сообщает, что происходит с вашим заказом: "
    "подтверждён, собирается, у курьера, доставлен. Заказы оформляются "
    "в приложении магазина."
)

SHORT_DESCRIPTION = "Сообщения о ваших заказах в ROZMART"


def show() -> int:
    info = notify.call("getWebhookInfo", {})
    me = notify.call("getMe", {})
    print(f"бот:     @{me.get('username')} ({me.get('first_name')})")
    print(f"адрес:   {info.get('url') or '— не задан, уведомления работать не будут'}")
    print(f"в очереди: {info.get('pending_update_count', 0)}")
    if info.get("last_error_message"):
        print(f"последняя ошибка: {info['last_error_message']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Настройка информационного бота")
    parser.add_argument("--show", action="store_true", help="показать текущие настройки")
    parser.add_argument("--remove", action="store_true", help="отключить вебхук")
    args = parser.parse_args()

    if not notify.enabled():
        print("Не задан NOTIFY_BOT_TOKEN — настраивать нечего.", file=sys.stderr)
        return 1

    if args.show:
        return show()

    if args.remove:
        notify.call("deleteWebhook", {"drop_pending_updates": False})
        print("Вебхук отключён. Сообщения покупателям больше приходить не будут.")
        return 0

    secret = os.getenv("NOTIFY_WEBHOOK_SECRET", "").strip()
    public = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if not secret:
        # без секрета приложение отвергает обновления: иначе через наш адрес
        # кто угодно подпишет чужой чат и станет слать людям сообщения от бота
        print("Не задан NOTIFY_WEBHOOK_SECRET — приложение не примет обновления.",
              file=sys.stderr)
        return 1
    if not public.startswith("https://"):
        # Telegram принимает вебхук только по https и только с настоящим
        # сертификатом: самоподписанный он молча отвергает
        print("PUBLIC_URL должен быть адресом сайта вида https://…", file=sys.stderr)
        return 1

    url = public + WEBHOOK_PATH
    notify.call("setWebhook", {
        "url": url,
        "secret_token": secret,
        "allowed_updates": UPDATES,
        # старые обновления, накопившиеся до настройки, не нужны: это могут
        # быть «Старт» месячной давности и вопросы, на которые уже не ответить
        "drop_pending_updates": True,
        "max_connections": 20,
    })
    notify.call("setMyCommands", {"commands": COMMANDS})
    notify.call("setMyDescription", {"description": DESCRIPTION})
    notify.call("setMyShortDescription", {"short_description": SHORT_DESCRIPTION})

    print(f"Вебхук установлен: {url}")
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
