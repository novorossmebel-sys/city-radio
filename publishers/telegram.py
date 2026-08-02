"""Публикатор в Telegram через Bot API (без внешних библиотек, чистый HTTP)."""
import requests

from config import settings
from content.base import Post
from .base import Publisher


class TelegramPublisher(Publisher):
    name = "telegram"

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def publish(self, channel: str, post: Post) -> bool:
        if not self.token or not channel:
            return False  # нет токена или канал не задан — тихо пропускаем

        if post.image_path:
            with open(post.image_path, "rb") as f:
                r = requests.post(
                    self._api("sendPhoto"),
                    data={"chat_id": channel, "caption": post.text, "parse_mode": post.parse_mode},
                    files={"photo": f},
                    timeout=30,
                )
        else:
            r = requests.post(
                self._api("sendMessage"),
                data={
                    "chat_id": channel,
                    "text": post.text,
                    "parse_mode": post.parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )

        ok = r.ok and r.json().get("ok", False)
        if not ok:
            print(f"[telegram] ошибка публикации: {r.text}")
        return ok
