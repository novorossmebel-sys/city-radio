"""Движок: берёт модуль → строит пост → рассылает во все публикаторы.

Добавить новый контент-модуль = дописать класс в content/ и зарегистрировать в MODULES.
Добавить платформу = дописать публикатор в publishers/ и добавить в PUBLISHERS.
"""
from config import load_cities
from content.currency import CurrencyModule
from content.weather import WeatherModule
from publishers.telegram import TelegramPublisher
from publishers.max import MaxPublisher

MODULES = {
    "currency": CurrencyModule(),
    "weather": WeatherModule(),
}

PUBLISHERS = [TelegramPublisher(), MaxPublisher()]


def post_module(module_name: str, city_key: str) -> None:
    cities = load_cities()
    if city_key not in cities:
        print(f"[engine] неизвестный город: {city_key}")
        return
    if module_name not in MODULES:
        print(f"[engine] неизвестный модуль: {module_name}")
        return

    city = cities[city_key]
    module = MODULES[module_name]

    try:
        post = module.build(city)
    except Exception as e:  # модуль не должен ронять весь процесс
        print(f"[engine] модуль '{module_name}' упал с ошибкой: {e}")
        return

    if post is None:
        print(f"[engine] модуль '{module_name}' ничего не вернул (нет ключа/данных) — пропуск")
        return

    for pub in PUBLISHERS:
        channel = city.get(f"{pub.name}_channel", "")
        ok = pub.publish(channel, post)
        print(f"[engine] {module_name} → {pub.name}: {'OK' if ok else 'пропущено/ошибка'}")
