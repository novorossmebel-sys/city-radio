"""Конфигурация проекта: секреты из .env и список городов из cities.yaml."""
import os
import yaml
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
    YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
    YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
    OWNER_TELEGRAM_CHAT_ID = os.getenv("OWNER_TELEGRAM_CHAT_ID", "")

    # Telethon (MTProto) — отдельный номер-наблюдатель для чтения чужих Telegram-каналов,
    # т.к. Bot API не отдаёт посты каналов, где бот не администратор (см. content/news.py).
    #
    # По умолчанию используются официальные api_id/api_hash самого Telegram Desktop
    # (2040 / b18441a1ff607e10a989891a5462e627) — они открыты, потому что Telegram Desktop
    # сам open-source, и это позволяет обойтись без my.telegram.org: у нас (2026-08-15)
    # my.telegram.org стабильно отдаёт "ERROR" при создании приложения на нескольких
    # номерах/браузерах/сетях — известный нерешённый баг сервиса, не наша проблема.
    # Если позже my.telegram.org заработает и появится желание — можно завести свою пару
    # и переопределить через .env, тогда эти значения по умолчанию не используются.
    TELETHON_API_ID = os.getenv("TELETHON_API_ID", "2040")
    TELETHON_API_HASH = os.getenv("TELETHON_API_HASH", "b18441a1ff607e10a989891a5462e627")
    TELETHON_PHONE = os.getenv("TELETHON_PHONE", "")

    VK_API_TOKEN = os.getenv("VK_API_TOKEN", "")

    # TMDB (themoviedb.org) — реальные постеры для кино-рубрик вместо YandexART-заглушки.
    # v3 API-ключ (Developer plan, бесплатно). v4 Read Access Token не используется — не
    # нужен для простого поиска/постеров, оставлен на будущее в TMDB_READ_ACCESS_TOKEN.
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
    TMDB_READ_ACCESS_TOKEN = os.getenv("TMDB_READ_ACCESS_TOKEN", "")


settings = Settings()


def load_cities(path: str = "cities.yaml") -> dict:
    """Читает конфиг городов. Ключ словаря = внутренний id города."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
