"""Публикатор в MAX через Bot API.

Токен бота: @MasterBot → /create (с 2026-08-25 создание бота проходит модерацию MAX,
до суток, раньше выдавало токен сразу). Метод: POST /messages, тело {"text","format"}.

✅ ПРОВЕРЕНО НА ЖИВОМ ТОКЕНЕ 2026-08-25 — базовый URL и заголовок авторизации ниже
подтверждены реальной публикацией, менять не нужно. Единственная реальная ловушка была
не в этом, а в chat_id: то, что видно в ссылке канала ("id231536618490_biz"), — это
публичный слаг, не chat_id. Настоящий числовой chat_id (со знаком минус, как у Telegram)
узнаётся через GET {MAX_API_BASE}/chats под токеном бота (см. cities.yaml).
"""
import requests

from config import settings
from content.base import Post
from .base import Publisher

MAX_API_BASE = "https://platform-api.max.ru"


def _auth_header(token: str) -> dict:
    return {"Authorization": token}


class MaxPublisher(Publisher):
    name = "max"

    def __init__(self):
        self.token = settings.MAX_BOT_TOKEN

    def publish(self, channel: str, post: Post) -> bool:
        if not self.token or not channel:
            return False  # нет токена или канал не задан — тихо пропускаем

        if post.image_paths:
            # Картинки в MAX требуют предварительной загрузки (upload → token → attachment).
            # Пока не реализовано — отправляем только текст.
            print("[max] картинки пока не поддержаны — отправляю только текст")

        fmt = "html" if post.parse_mode.lower() == "html" else "markdown"

        try:
            r = requests.post(
                f"{MAX_API_BASE}/messages",
                params={"chat_id": channel},
                headers=_auth_header(self.token),
                json={"text": post.text, "format": fmt},
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[max] сетевая ошибка: {e}")
            return False

        if not r.ok:
            print(f"[max] ошибка публикации ({r.status_code}): {r.text}")
            return False
        return True
