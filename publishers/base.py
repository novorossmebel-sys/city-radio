"""Базовый интерфейс публикатора. Один пост можно отправить в любую платформу."""
from content.base import Post


class Publisher:
    name = "base"

    def publish(self, channel: str, post: Post) -> bool:
        """Публикует пост в канал. Возвращает True при успехе."""
        raise NotImplementedError
