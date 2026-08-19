"""Планировщик «программа дня» — публикует по расписанию, а не спамом.

Два независимых контура:
  1. weather/currency (PROGRAM) — прямая автопубликация без модерации, без изменений.
  2. Дайджест-движок (content/digest_engine.py, content/digest_compose.py) — сбор
     кандидатов по 19 рубрикам/35 каналам + плановая сборка Morning Radar/Evening Digest/
     Weekly (редакционная политика владельца, 2026-08-19). Критические алерты (БПЛА/вода/
     погода) уходят в модерацию сразу при обнаружении — не ждут плановой сборки, поэтому
     алертные каналы опрашиваются чаще (FAST_POLL), чем остальные (NORMAL_POLL).
"""
from apscheduler.schedulers.blocking import BlockingScheduler

from config import load_cities
from engine import post_module
from content.digest_engine import collect_candidates
from content.digest_compose import compose_morning_radar, compose_evening_digest, compose_weekly, select_humor

# Программа дня (МСК): (час, минута, модуль)
PROGRAM = [
    (8, 0, "weather"),
    (12, 30, "currency"),
]

FAST_POLL_MINUTES = 15    # алертные каналы (nvrskadm, chpnvrsk_official) — быстрые NOW-уведомления
NORMAL_POLL_MINUTES = 60  # остальные 17 рубрик — копятся в пулы DIGEST/WEEKLY/HUMOR_POOL


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

    sched.add_job(collect_candidates, "interval", minutes=FAST_POLL_MINUTES,
                  args=[True], id="digest-collect-fast")
    sched.add_job(collect_candidates, "interval", minutes=NORMAL_POLL_MINUTES,
                  args=[False], id="digest-collect-normal")
    sched.add_job(compose_morning_radar, "cron", hour=6, minute=30, id="digest-morning")
    sched.add_job(select_humor, "cron", hour=18, minute=0, id="digest-humor-select")
    sched.add_job(compose_evening_digest, "cron", hour=19, minute=0, id="digest-evening")
    sched.add_job(compose_weekly, "cron", day_of_week="fri", hour=12, minute=0, id="digest-weekly")

    print("[scheduler] запущен. Программа дня + дайджест-движок активны. Ctrl+C для выхода.")
    sched.start()
