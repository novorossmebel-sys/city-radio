"""Публикатор в MAX — ЗАГЛУШКА.

Заполнить в Фазе 0, когда подтвердим API постинга в каналы MAX.
Ожидаемо это будет HTTP-запрос к Bot API MAX по образцу Telegram —
тогда код почти повторит telegram.py, поменяются только URL и формат запроса.
"""
from content.base import Post
from .base import Publisher


class MaxPublisher(Publisher):
    name = "max"

    def publish(self, channel: str, post: Post) -> bool:
        print(
            "[max] публикация в MAX пока не реализована (ждём подтверждения API, Фаза 0).\n"
            "----- готовый пост, который ушёл бы в MAX: -----\n"
            f"{post.text}\n"
            "------------------------------------------------"
        )
        return False
