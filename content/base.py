"""Базовый интерфейс контент-модуля.

Каждый модуль (погода, валюта, гороскоп...) получает конфиг города и
возвращает готовый пост (Post) либо None, если данных нет (например, не задан ключ API).
"""
from dataclasses import dataclass, field
from typing import Optional

TELEGRAM_CAPTION_LIMIT = 1024  # лимит Bot API на подпись к фото/альбому


@dataclass
class Post:
    text: str
    image_paths: list = field(default_factory=list)  # локальные пути к картинкам, если есть (альбом — несколько)
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
    include_source: bool = False  # по умолчанию скрыт; кнопка в модерации явно включает показ ссылки
    image_paths: list = field(default_factory=list)  # фото из исходного поста, если есть
    include_photo: bool = True  # по умолчанию показан (в отличие от источника) — это часть контента
    candidate_ids: list = field(default_factory=list)  # id-шники в digest_store.candidates —
    # обычно один, но синтез из нескольких мнений (кино, digest_compose.py) даёт несколько;
    # moderation.py по ним обновляет статус после решения владельца (было: навсегда
    # "sent_moderation", см. обсуждение 2026-08-19/25)

    def to_post(self) -> Post:
        parts = [f"📻 {self.rubric}", "", f"<b>{self.title}</b>", "", self.body]
        if self.include_source and self.source_url:
            parts += ["", f"<a href=\"{self.source_url}\">Источник: {self.source_name}</a>"]
        images = self.image_paths if (self.include_photo and self.image_paths) else []
        return Post(text="\n".join(parts), image_paths=images)
