"""Публикатор в MAX через Bot API.

Токен бота: @MasterBot → /create. Метод: POST /messages, тело {"text","format"}.

⚠️ ТРЕБУЕТ ПРОВЕРКИ НА ЖИВОМ ТОКЕНЕ. Вторичные источники расходятся по:
  1) базовому URL: platform-api.max.ru  ИЛИ  platform-api2.max.ru;
  2) авторизации: заголовок "Authorization: <token>"  ИЛИ  "Authorization: Bearer <token>"
     (передача токена через query-параметр access_token объявлена устаревшей).
Обе точки вынесены в константы ниже — если получишь 401/403/404, поменяй их
по факту (официальная дока: dev.max.ru/docs). Логика поста при этом не меняется.
"""
import requests

from config import settings
from content.base import Post
from .base import Publisher

# --- настройки, которые, возможно, придётся подправить после теста ---
MAX_API_BASE = "https://platform-api.max.ru"   # альтернатива: https://platform-api2.max.ru


def _auth_header(token: str) -> dict:
    # Если получишь 401 — попробуй вернуть {"Authorization": f"Bearer {token}"}
    return {"Authorization": token}
# ---------------------------------------------------------------------


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
