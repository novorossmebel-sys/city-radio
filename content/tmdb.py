"""Постеры фильмов/сериалов через TMDB (themoviedb.org) — v3 API, ключ бесплатный
(Developer plan). Подключено 2026-08-25 взамен YandexART-заглушки для кино-рубрик:
владелец хотел настоящий постер, а не сгенерированную иллюстрацию (см. обсуждение
2026-08-18 в content/news.py::generate_illustration).
"""
import uuid
from pathlib import Path

import requests

from config import settings
from content.images import normalize_photo

MEDIA_CACHE_DIR = Path("media_cache")
SEARCH_URL = "https://api.themoviedb.org/3/search/multi"
IMAGE_BASE = "https://image.tmdb.org/t/p/w780"


def fetch_poster(title: str) -> str | None:
    """Ищет фильм/сериал по названию, скачивает постер первого результата с картинкой
    в media_cache/. Возвращает локальный путь или None (нет ключа, нет результата,
    сетевой сбой) — вызывающий код сам решает, на что откатиться."""
    if not settings.TMDB_API_KEY or not title:
        return None

    try:
        r = requests.get(
            SEARCH_URL,
            params={"api_key": settings.TMDB_API_KEY, "query": title, "language": "ru-RU"},
            timeout=15,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except requests.RequestException as e:
        print(f"[tmdb] сбой поиска «{title}»: {e}")
        return None

    poster_path = next((item["poster_path"] for item in results if item.get("poster_path")), None)
    if not poster_path:
        return None

    try:
        img = requests.get(f"{IMAGE_BASE}{poster_path}", timeout=20)
        img.raise_for_status()
    except requests.RequestException as e:
        print(f"[tmdb] не смог скачать постер «{title}»: {e}")
        return None

    MEDIA_CACHE_DIR.mkdir(exist_ok=True)
    dest = MEDIA_CACHE_DIR / f"{uuid.uuid4().hex}.jpg"
    dest.write_bytes(img.content)
    normalize_photo(str(dest))
    return str(dest)
