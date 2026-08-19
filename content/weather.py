"""Модуль погоды — публикуется автоматически, без модерации (владелец, 2026-08-17): рубрика
"Погода и море" уже проверена на практике и не нуждается в ручной проверке, как курс валют.

Переиспользует YandexPogodaSource и build_structured_draft из content/news.py — та же логика
(сезонная картинка, разделитель, температура воздуха/воды и т.д.), что и в модерации, просто
без похода к владельцу за подтверждением.
"""
from content.base import ContentModule, Post
from content.news import YandexPogodaSource, build_structured_draft


class WeatherModule(ContentModule):
    name = "weather"

    def build(self, city: dict) -> Post | None:
        try:
            items = YandexPogodaSource().fetch()
        except Exception as e:
            print(f"[weather] не смог получить данные с Яндекс.Погоды: {e}")
            return None
        if not items:
            return None

        draft = build_structured_draft("Погода и море", items[0])
        return draft.to_post()
