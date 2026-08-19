"""Публикатор в Telegram через Bot API (без внешних библиотек, чистый HTTP)."""
import json

import requests

from config import settings
from content.base import Post, TELEGRAM_CAPTION_LIMIT
from .base import Publisher


class TelegramPublisher(Publisher):
    name = "telegram"

    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _send_photos(self, channel: str, image_paths: list, caption: str = None, parse_mode: str = "HTML") -> bool:
        # caption=None — фото без подписи, текст идёт отдельным sendMessage следом (см.
        # publish(): нужно, когда текст длиннее лимита подписи в 1024 символа). Если
        # caption задан, это единственное сообщение с фото и полным текстом.
        files = {}
        try:
            if len(image_paths) == 1:
                data = {"chat_id": channel}
                if caption is not None:
                    data["caption"] = caption
                    data["parse_mode"] = parse_mode
                with open(image_paths[0], "rb") as f:
                    r = requests.post(
                        self._api("sendPhoto"), data=data, files={"photo": f}, timeout=30,
                    )
            else:
                media = []
                for i, path in enumerate(image_paths):
                    field_name = f"photo{i}"
                    files[field_name] = open(path, "rb")
                    item = {"type": "photo", "media": f"attach://{field_name}"}
                    if caption is not None and i == 0:
                        item["caption"] = caption
                        item["parse_mode"] = parse_mode
                    media.append(item)
                r = requests.post(
                    self._api("sendMediaGroup"),
                    data={"chat_id": channel, "media": json.dumps(media)},
                    files=files,
                    timeout=60,
                )
        except requests.RequestException as e:
            print(f"[telegram] сетевая ошибка при публикации фото: {e}")
            return False
        finally:
            for f in files.values():
                f.close()

        ok = r.ok and r.json().get("ok", False)
        if not ok:
            print(f"[telegram] ошибка публикации фото: {r.text}")
        return ok

    def publish(self, channel: str, post: Post) -> bool:
        if not self.token or not channel:
            return False  # нет токена или канал не задан — тихо пропускаем

        if post.image_paths:
            # короткий текст — одним сообщением (фото + подпись), как обычный пост в канале;
            # длинный (полный пересказ без урезания) в подпись не влезает — фото и текст
            # отдельными сообщениями (см. _send_photos без caption + sendMessage ниже)
            if len(post.text) <= TELEGRAM_CAPTION_LIMIT:
                return self._send_photos(channel, post.image_paths, caption=post.text, parse_mode=post.parse_mode)
            if not self._send_photos(channel, post.image_paths):
                return False  # текст без фото не публикуем — лучше явный сбой, чем неполный пост

        try:
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
        except requests.RequestException as e:
            print(f"[telegram] сетевая ошибка публикации: {e}")
            return False

        ok = r.ok and r.json().get("ok", False)
        if not ok:
            print(f"[telegram] ошибка публикации: {r.text}")
        return ok
