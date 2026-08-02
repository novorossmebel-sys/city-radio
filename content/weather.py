"""Модуль погоды. Источник — OpenWeather (нужен бесплатный ключ).

Без ключа модуль возвращает None и просто пропускается.
"""
import requests

from config import settings
from .base import ContentModule, Post

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


class WeatherModule(ContentModule):
    name = "weather"

    def build(self, city: dict):
        key = settings.OPENWEATHER_API_KEY
        if not key:
            return None  # ключ не задан — модуль пропускается

        w = city["weather"]
        r = requests.get(
            OWM_URL,
            params={
                "lat": w["lat"],
                "lon": w["lon"],
                "appid": key,
                "units": "metric",
                "lang": "ru",
            },
            timeout=15,
        ).json()

        temp = round(r["main"]["temp"])
        feels = round(r["main"]["feels_like"])
        desc = r["weather"][0]["description"].capitalize()
        wind = round(r["wind"]["speed"])

        text = (
            f"🌤 <b>Погода — {city['title']}</b>\n\n"
            f"{desc}\n"
            f"🌡 {temp}°C (ощущается {feels}°C)\n"
            f"💨 Ветер {wind} м/с"
        )
        return Post(text=text)
