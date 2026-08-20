"""Наблюдатель за `run.py serve` — перезапускает его, если тот завис или упал.

Зачем: 2026-08-20 Telethon-клиент (content/news.py::TelegramChannelSource) поймал
известный баг библиотеки — MsgidDecreaseRetryError вместе с гонкой в приёме данных —
и завис в бесконечном цикле переподключений на много часов, ни разу не бросив
исключение наружу. Процесс при этом оставался "живым" (PID существовал), поэтому
ни один внешний наблюдатель за самим процессом эту ситуацию бы не поймал — нужен
именно heartbeat изнутри пайплайна (content/digest_engine.py::collect_candidates
пишет отметку времени в HEARTBEAT_FILE при каждом успешном завершении опроса,
вне зависимости от того, нашлось что-то новое или нет).

Сам `run.py serve` не может перезапустить себя изнутри (тот же зависший процесс не
может выполнить свой же рестарт) — поэтому это отдельный родительский процесс,
который запускает `run.py serve` подпроцессом и следит за ним снаружи.
"""
import subprocess
import sys
import time
from pathlib import Path

from config import settings

HEARTBEAT_FILE = Path("scheduler_heartbeat.txt")
CHECK_INTERVAL_SECONDS = 5 * 60
STALE_AFTER_SECONDS = 30 * 60  # с запасом на один пропущенный FAST_POLL-цикл (раз в 15 мин)
STARTUP_GRACE_SECONDS = STALE_AFTER_SECONDS  # не проверять heartbeat, пока процесс не успел стартовать


def _notify(text: str) -> None:
    if not (settings.TELEGRAM_BOT_TOKEN and settings.OWNER_TELEGRAM_CHAT_ID):
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": settings.OWNER_TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"[supervisor] не смог отправить уведомление: {e}")


def _heartbeat_age_seconds() -> float | None:
    if not HEARTBEAT_FILE.exists():
        return None
    return time.time() - HEARTBEAT_FILE.stat().st_mtime


def run_supervised() -> None:
    print("[supervisor] запущен, слежу за run.py serve")
    while True:
        proc = subprocess.Popen([sys.executable, "-u", "run.py", "serve"])
        started_at = time.monotonic()
        print(f"[supervisor] run.py serve запущен (pid={proc.pid})")

        restart_reason = None
        while True:
            try:
                proc.wait(timeout=CHECK_INTERVAL_SECONDS)
                restart_reason = f"процесс завершился сам (код {proc.returncode})"
                break
            except subprocess.TimeoutExpired:
                pass  # обычный тик проверки, процесс всё ещё жив — идём смотреть heartbeat

            if time.monotonic() - started_at < STARTUP_GRACE_SECONDS:
                continue  # даём время на старт + первый цикл опроса, прежде чем требовать heartbeat

            age = _heartbeat_age_seconds()
            if age is not None and age > STALE_AFTER_SECONDS:
                restart_reason = f"heartbeat не обновлялся {age / 60:.0f} мин — похоже, завис"
                break

        print(f"[supervisor] перезапускаю: {restart_reason}")
        _notify(f"⚠️ Планировщик перезапущен автоматически: {restart_reason}")
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        time.sleep(5)


if __name__ == "__main__":
    run_supervised()
