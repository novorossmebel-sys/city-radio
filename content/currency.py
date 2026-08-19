"""Модуль курса валют. Источник — ЦБ РФ (бесплатно, без ключа).

Используем удобный JSON-зеркало cbr-xml-daily.ru.
Официальная альтернатива: https://www.cbr.ru/scripts/XML_daily.asp (XML).
"""
from pathlib import Path

import requests

from .base import ContentModule, Post

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


CURRENCY_PHOTO = Path("assets/currency_photo/kurs_valut.jpg")


class CurrencyModule(ContentModule):
    name = "currency"

    def build(self, city: dict):
        data = requests.get(CBR_URL, timeout=15).json()
        v = data["Valute"]

        def line(code: str, label: str) -> str:
            c = v[code]
            cur, prev = c["Value"], c["Previous"]
            arrow = "▲" if cur > prev else ("▼" if cur < prev else "▬")
            diff = abs(cur - prev)
            return f"{label}: <b>{cur:.2f} ₽</b> {arrow} {diff:.2f}"

        text = (
            "💱 <b>Курс валют ЦБ РФ</b>\n\n"
            + line("USD", "🇺🇸 Доллар") + "\n"
            + line("EUR", "🇪🇺 Евро") + "\n"
            + line("CNY", "🇨🇳 Юань")
        )
        image_paths = [str(CURRENCY_PHOTO)] if CURRENCY_PHOTO.exists() else []
        return Post(text=text, image_paths=image_paths)
