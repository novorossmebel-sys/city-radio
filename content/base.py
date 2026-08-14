"""Базовый интерфейс контент-модуля.

Каждый модуль (погода, валюта, гороскоп...) получает конфиг города и
возвращает готовый пост (Post) либо None, если данных нет (например, не задан ключ API).
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Post:
    text: str
    image_path: Optional[str] = None  # локальный путь к картинке, если есть
    parse_mode: str = "HTML"


class ContentModule:
    name: str = "base"

    def build(self, city: dict) -> Optional[Post]:
        raise NotImplementedError


@dataclass
class Draft:
    """Черновик новости, ожидающий модерации владельцем (см. sources.yaml).

    В отличие от Post — несёт происхождение и результат верификации,
    чтобы модератор видел не только текст, но и откуда он и что вызвало вопросы.
    """
    rubric: str
    source_name: str
    source_url: str
    original_title: str
    original_text: str
    title: str
    body: str
    concerns: list = field(default_factory=list)  # причины насторожиться при проверке, пусто = ок
    needs_manual_review: bool = False  # True = модель сама просит внимательнее посмотреть перед публикацией

    def to_post(self) -> Post:
        text = f"<b>{self.title}</b>\n\n{self.body}\n\n<a href=\"{self.source_url}\">Источник: {self.source_name}</a>"
        return Post(text=text)
