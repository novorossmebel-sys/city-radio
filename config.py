"""Конфигурация проекта: секреты из .env и список городов из cities.yaml."""
import os
import yaml
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
    MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
    YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
    YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
    OWNER_TELEGRAM_CHAT_ID = os.getenv("OWNER_TELEGRAM_CHAT_ID", "")


settings = Settings()


def load_cities(path: str = "cities.yaml") -> dict:
    """Читает конфиг городов. Ключ словаря = внутренний id города."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
