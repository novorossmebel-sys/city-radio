"""Базовый интерфейс контент-модуля.

Каждый модуль (погода, валюта, гороскоп...) получает конфиг города и
возвращает готовый пост (Post) либо None, если данных нет (например, не задан ключ API).
"""
from dataclasses import dataclass
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
