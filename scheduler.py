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
from content.news import post_horoscope

# Программа дня (МСК): (час, минута, модуль)
PROGRAM = [
    (8, 0, "weather"),
    (12, 30, "currency"),
]

FAST_POLL_MINUTES = 15    # алертные каналы (nvrskadm, chpnvrsk_official) — быстрые NOW-уведомления
NORMAL_POLL_MINUTES = 60  # остальные 17 рубрик — копятся в пулы DIGEST/WEEKLY/HUMOR_POOL


def _safe(fn):
    """Оборачивает задачу планировщика: APScheduler и так не роняет весь процесс на
    исключении из одной задачи, но по умолчанию просто пишет traceback через logging,
    который никто не читает — ровно так compose_weekly (2026-08-21, единственный на тот
    момент реальный прогон по пятничному cron) и compose_evening_digest один раз молча
    не долетели до владельца из-за обрыва соединения с api.telegram.org, и compose_weekly
    из-за этого не повторится ещё неделю (cron раз в неделю, retry нет). Теперь любая
    такая ошибка хотя бы уходит владельцу в бот, а не тонет в логе фонового процесса."""
    def wrapped(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception as e:
            print(f"[scheduler] {fn.__name__} упал с ошибкой: {e}")
            try:
                from moderation import ModerationBot
                bot = ModerationBot()
                if bot.token and bot.owner_chat_id:
                    bot.send_text(bot.owner_chat_id, f"⚠️ {fn.__name__} не выполнился: {e}")
            except Exception:
                pass
    wrapped.__name__ = getattr(fn, "__name__", "job")
    return wrapped


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

    # jitter — точные интервалы без разброса сами по себе выглядят как автоматизация
    # (см. 2026-08-24, риск повторной блокировки Telethon-наблюдателя); на публикации
    # по расписанию ниже jitter не ставим — их время по смыслу должно быть предсказуемым
    # для читателя.
    sched.add_job(_safe(collect_candidates), "interval", minutes=FAST_POLL_MINUTES,
                  jitter=180, args=[True], id="digest-collect-fast")
    sched.add_job(_safe(collect_candidates), "interval", minutes=NORMAL_POLL_MINUTES,
                  jitter=600, args=[False], id="digest-collect-normal")
    sched.add_job(_safe(compose_morning_radar), "cron", hour=6, minute=30, id="digest-morning")
    sched.add_job(_safe(post_horoscope), "cron", hour=7, minute=0, id="horoscope-daily")
    sched.add_job(_safe(select_humor), "cron", hour=18, minute=0, id="digest-humor-select")
    sched.add_job(_safe(compose_evening_digest), "cron", hour=19, minute=0, id="digest-evening")
    sched.add_job(_safe(compose_weekly), "cron", day_of_week="fri", hour=12, minute=0, id="digest-weekly")

    print("[scheduler] запущен. Программа дня + дайджест-движок активны. Ctrl+C для выхода.")
    sched.start()
