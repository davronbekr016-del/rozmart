"""Тексты для покупателя на узбекском.

Приложение магазина говорит на двух языках и передаёт выбранный в заголовке
X-Lang. Сообщения об ошибках сервер пишет по-русски — здесь их узбекские
варианты (латиницей); подставляет их обработчик ошибок в app/main.py.
Ошибка без перевода уходит как есть: лучше русский текст, чем никакого.
"""
import re

UZ = {
    "В корзине есть товары, которых больше нет":
        "Savatda endi mavjud bo'lmagan mahsulotlar bor",
    "Заказ не найден": "Buyurtma topilmadi",
    "Заказ отменён — оплатить его нельзя": "Buyurtma bekor qilingan — uni to'lab bo'lmaydi",
    "Заказ оформлен с оплатой наличными": "Buyurtma naqd pulda to'lash bilan berilgan",
    "Заказ уже оплачен": "Buyurtma allaqachon to'langan",
    "Категория не найдена": "Kategoriya topilmadi",
    "Оплата картой пока недоступна. Выберите оплату наличными.":
        "Karta orqali to'lov hozircha mavjud emas. Naqd pulda to'lashni tanlang.",
    "Оплата картой сейчас недоступна": "Karta orqali to'lov hozir mavjud emas",
    "Откройте приложение из Telegram": "Ilovani Telegram orqali oching",
    "Отметьте согласие на обработку данных — без него доставить заказ нельзя":
        "Ma'lumotlarni qayta ishlashga roziligingizni belgilang — busiz buyurtmani "
        "yetkazib bo'lmaydi",
    "Оформить заказ можно только из приложения в Telegram":
        "Buyurtmani faqat Telegram'dagi ilova orqali berish mumkin",
    "Товар не найден": "Mahsulot topilmadi",
    "Цены изменились. Проверьте корзину и подтвердите заказ заново.":
        "Narxlar o'zgardi. Savatni tekshirib, buyurtmani qayta tasdiqlang.",
    "Не удалось выставить счёт. Попробуйте ещё раз.":
        "Hisob chiqarib bo'lmadi. Qayta urinib ko'ring.",
    "Сумма меньше минимальной для оплаты картой. Выберите оплату наличными.":
        "Summa karta orqali to'lash uchun eng kam miqdordan oz. Naqd pulda to'lashni tanlang.",
}

# тексты с подстановкой: «Докторская» сейчас недоступен…
PATTERNS = [
    (re.compile(r"^«(.+)» сейчас недоступен для заказа$"),
     "«{0}» hozir buyurtma uchun mavjud emas"),
    (re.compile(r"^Не больше (\d+) штук одной позиции в заказе$"),
     "Buyurtmada bitta mahsulotdan {0} donadan ortiq bo'lmasin"),
]


def lang_of(header: str | None) -> str:
    return "uz" if (header or "").strip().lower().startswith("uz") else "ru"


def translate(text: str, lang: str) -> str:
    if lang != "uz":
        return text
    if text in UZ:
        return UZ[text]
    for pattern, template in PATTERNS:
        match = pattern.match(text)
        if match:
            return template.format(*match.groups())
    return text
