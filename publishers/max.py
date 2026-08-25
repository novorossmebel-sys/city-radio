"""Публикатор в MAX через Bot API.

Токен бота: @MasterBot → /create (с 2026-08-25 создание бота проходит модерацию MAX,
до суток, раньше выдавало токен сразу). Метод: POST /messages, тело {"text","format"}.

✅ ПРОВЕРЕНО НА ЖИВОМ ТОКЕНЕ 2026-08-25 — базовый URL и заголовок авторизации ниже
подтверждены реальной публикацией, менять не нужно. Единственная реальная ловушка была
не в этом, а в chat_id: то, что видно в ссылке канала ("id231536618490_biz"), — это
публичный слаг, не chat_id. Настоящий числовой chat_id (со знаком минус, как у Telegram)
узнаётся через GET {MAX_API_BASE}/chats под токеном бота (см. cities.yaml).

Картинки (добавлено 2026-08-25): MAX не принимает файл прямо в /messages — нужна
двухшаговая загрузка: POST /uploads?type=image возвращает одноразовый {"url": ...},
затем сам файл отправляется multipart-запросом на этот url. Ответ на живом токене —
{"photos": {"<photoId>": {"token": "..."}}} (НЕ плоский {"token": ...}, как можно
подумать по обзорным статьям — проверено запросом 2026-08-25), нужный token берётся
из единственного значения словаря photos и идёт в attachments[].payload.token
сообщения. См. https://dev.max.ru/docs-api/methods/POST/uploads.
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

    def _upload_image(self, path: str) -> str | None:
        try:
            r = requests.post(
                f"{MAX_API_BASE}/uploads",
                params={"type": "image"},
                headers=_auth_header(self.token),
                timeout=30,
            )
            r.raise_for_status()
            upload_url = r.json()["url"]

            with open(path, "rb") as f:
                r2 = requests.post(upload_url, files={"data": f}, timeout=60)
            r2.raise_for_status()
            photos = r2.json()["photos"]
            return next(iter(photos.values()))["token"]
        except (requests.RequestException, KeyError, ValueError, StopIteration) as e:
            print(f"[max] не смог загрузить картинку {path}: {e}")
            return None

    def publish(self, channel: str, post: Post) -> bool:
        if not self.token or not channel:
            return False  # нет токена или канал не задан — тихо пропускаем

        attachments = []
        for path in post.image_paths:
            token = self._upload_image(path)
            if token:
                attachments.append({"type": "image", "payload": {"token": token}})

        fmt = "html" if post.parse_mode.lower() == "html" else "markdown"
        body = {"text": post.text, "format": fmt}
        if attachments:
            body["attachments"] = attachments

        try:
            r = requests.post(
                f"{MAX_API_BASE}/messages",
                params={"chat_id": channel},
                headers=_auth_header(self.token),
                json=body,
                timeout=30,
            )
        except requests.RequestException as e:
            print(f"[max] сетевая ошибка: {e}")
            return False

        if not r.ok:
            print(f"[max] ошибка публикации ({r.status_code}): {r.text}")
            return False
        return True
