"""Настройка бота доставщиков: вебхук, команды, описание.

    .venv\\Scripts\\python.exe -m scripts.setup_courier_bot          настроить
    .venv\\Scripts\\python.exe -m scripts.setup_courier_bot --show   что зарегистрировано

Нужны COURIER_BOT_TOKEN, COURIER_WEBHOOK_SECRET и PUBLIC_URL.
"""
import argparse
import os
import sys

from app import courier_bot

# Описание — то, что человек видит в пустом чате до «Старт» (до 512 знаков);
# короткое — в профиле бота и в пересланной ссылке (до 120). Telegram
# показывает вариант на языке приложения человека, "" — для всех остальных
DESCRIPTIONS = {
    "": (
        "🚚 Бот доставщиков ROZMART\n\n"
        "📦 Заказы — свободные заказы магазина. Нажмите «Принять заказ» — он ваш, "
        "другие доставщики его уже не возьмут.\n\n"
        "🚚 Доставка — ваши заказы в пути: телефон покупателя, адрес на карте, "
        "кнопка «Завершить», когда заказ вручён, или «Отменить», чтобы вернуть его другим.\n\n"
        "В каждом заказе — сколько получить с покупателя или пометка «оплачено картой», "
        "адрес и состав.\n\n"
        "Бот только для сотрудников. Нажмите «Старт» — доступ включит администратор магазина.",
        "Заказы ROZMART для доставщиков: принять, отвезти, отметить доставку. "
        "Доступ даёт администратор.",
    ),
    "uz": (
        "🚚 ROZMART yetkazib beruvchilar boti\n\n"
        "📦 Buyurtmalar — do'konning bo'sh buyurtmalari. «Buyurtmani qabul qilish»ni "
        "bosing — buyurtma sizniki, boshqalar uni endi ololmaydi.\n\n"
        "🚚 Yetkazish — yo'ldagi buyurtmalaringiz: xaridor telefoni, xaritadagi manzil, "
        "topshirilganda «Yakunlash», boshqalarga qaytarish uchun «Bekor qilish».\n\n"
        "Har bir buyurtmada — xaridordan olinadigan summa yoki «karta orqali to'langan» "
        "belgisi, manzil va tarkib.\n\n"
        "Bot faqat xodimlar uchun. «Start»ni bosing — ruxsatni do'kon administratori beradi.",
        "ROZMART buyurtmalari yetkazib beruvchilar uchun: qabul qilish, yetkazish, "
        "belgilash. Ruxsat — administratorda.",
    ),
}


# меню команд: "" — для всех, "uz" — у кого Telegram на узбекском
COMMANDS = {
    "": ("Начать", "Свободные заказы", "Мои заказы в доставке"),
    "uz": ("Boshlash", "Bo'sh buyurtmalar", "Yetkazishdagi buyurtmalarim"),
}


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
    for language, (start, orders, delivery) in COMMANDS.items():
        courier_bot.call("setMyCommands", {"language_code": language, "commands": [
            {"command": "start", "description": start},
            {"command": "orders", "description": orders},
            {"command": "delivery", "description": delivery},
        ]})
    for language, (description, short) in DESCRIPTIONS.items():
        assert len(description) <= 512 and len(short) <= 120, language
        courier_bot.call("setMyDescription", {"description": description,
                                              "language_code": language})
        courier_bot.call("setMyShortDescription", {"short_description": short,
                                                   "language_code": language})
    print(f"Вебхук бота доставщиков: {public + courier_bot.WEBHOOK_PATH}")
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
