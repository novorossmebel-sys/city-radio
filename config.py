"""Конфигурация проекта: секреты из .env и список городов из cities.yaml."""
import os
import yaml
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")


settings = Settings()


def load_cities(path: str = "cities.yaml") -> dict:
    """Читает конфиг городов. Ключ словаря = внутренний id города."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
