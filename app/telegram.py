"""Кто открыл приложение.

Telegram передаёт в приложение данные о пользователе, подписанные токеном
бота. Подпись считается на сервере: всё, что прислал браузер без верной
подписи, — не пользователь, а просто текст в запросе.
"""
import hashlib
import hmac
import json
import os
import time
from typing import NamedTuple
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException

# Токен задаётся переменной окружения на сервере, в репозитории его нет.
# strip() — при копировании из BotFather легко прихватить перевод строки,
# и тогда не сойдётся ни одна подпись, а причина будет невидима.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Разработку вести надо и на машине, где Telegram нет. Но выключаться проверка
# должна по явному требованию, а не просто оттого, что токен забыли задать.
AUTH_DISABLED = os.getenv("TELEGRAM_AUTH_DISABLED") == "1"

# Данные подписываются в момент открытия приложения. Сутки — запас на то, что
# приложение висит открытым, но не настолько, чтобы перехваченной подписью
# можно было пользоваться неделями.
MAX_AGE_SECONDS = 24 * 60 * 60

# небольшой допуск назад: часы сервера и телефона расходятся на секунды
CLOCK_SKEW_SECONDS = 60


class TelegramUser(NamedTuple):
    id: int
    name: str
    # @имя в Telegram. Есть не у всех: аккаунт без username — обычное дело,
    # поэтому ни искать по нему людей, ни требовать его нельзя
    username: str | None = None


def check_configuration() -> None:
    """Проверяется при старте: тихий запуск без токена — это открытый магазин."""
    if not BOT_TOKEN and not AUTH_DISABLED:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Без него вход через Telegram не проверяется и заказ "
            "может оформить кто угодно. Задайте токен бота, а если это разработка "
            "без Telegram — переменную TELEGRAM_AUTH_DISABLED=1"
        )


def check_init_data(init_data: str) -> TelegramUser | None:
    """Разбирает строку от Telegram и возвращает пользователя, если подпись верна."""
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    # compare_digest на строках падает на не-ASCII, а сюда приходит что угодно
    if len(received) != 64 or any(c not in "0123456789abcdef" for c in received):
        return None

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    # сравнение с постоянным временем: обычное «==» выдаёт, сколько символов совпало
    if not hmac.compare_digest(expected, received):
        return None

    try:
        age = time.time() - int(fields["auth_date"])
        if not -CLOCK_SKEW_SECONDS <= age <= MAX_AGE_SECONDS:
            return None
        user = json.loads(fields["user"])
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
        return TelegramUser(
            id=int(user["id"]),
            name=name.strip(),
            username=(user.get("username") or "").strip() or None,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def current_user(
    init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> TelegramUser | None:
    """Пользователь, если приложение открыто из Telegram и подпись сошлась."""
    if not BOT_TOKEN or not init_data:
        return None
    return check_init_data(init_data)


def require_user(user: TelegramUser | None = Depends(current_user)) -> TelegramUser:
    """Для того, что имеет смысл только для конкретного человека."""
    if user is None:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")
    return user


def buyer(user: TelegramUser | None = Depends(current_user)) -> TelegramUser | None:
    """Кто оформляет заказ. Каталог смотрят все, заказывают — из Telegram."""
    if BOT_TOKEN and user is None:
        raise HTTPException(
            status_code=401, detail="Оформить заказ можно только из приложения в Telegram"
        )
    return user
