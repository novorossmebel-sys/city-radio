"""Планировщик «программа дня» — публикует по расписанию, а не спамом.

Расписание пока простое (заглушка под MVP). Позже расширим до полной сетки:
утро / день / вечер / ночь с разными модулями.
"""
from apscheduler.schedulers.blocking import BlockingScheduler

from config import load_cities
from engine import post_module

# Программа дня (МСК): (час, минута, модуль)
PROGRAM = [
    (8, 0, "weather"),
    (12, 30, "currency"),
    (18, 0, "weather"),
]


def start() -> None:
    cities = load_cities()
    sched = BlockingScheduler(timezone="Europe/Moscow")

    for city_key in cities:
        for hour, minute, module in PROGRAM:
            sched.add_job(
                post_module,
                "cron",
                hour=hour,
                minute=minute,
                args=[module, city_key],
                id=f"{city_key}-{module}-{hour:02d}{minute:02d}",
            )

    print("[scheduler] запущен. Программа дня активна. Ctrl+C для выхода.")
    sched.start()
