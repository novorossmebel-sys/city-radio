"""Одноразовый вход в Telegram-аккаунт-наблюдатель для Telethon (MTProto).

Нужен, чтобы читать чужие публичные каналы (novosti_efir, chpnvrsk_official) — Bot API
для этого не подходит, см. content/news.py. Логинимся не ботом, а обычным номером телефона
(TELETHON_PHONE в .env) — как если бы зашли в Telegram с нового устройства.

Два отдельных шага, потому что среде, где выполняется скрипт, недоступен обычный
интерактивный ввод (input() по ходу выполнения):
  1) python telethon_login.py request            — присылает код подтверждения на TELETHON_PHONE
  2) python telethon_login.py confirm <код>       — завершает вход
     python telethon_login.py confirm <код> <пароль>  — если на аккаунте включена 2FA

После успеха сессия сохраняется в telethon_observer.session (в .gitignore, не коммитить —
файл равнозначен паролю от аккаунта) и повторный логин не требуется.
"""
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import settings

SESSION_NAME = "telethon_observer"
STATE_FILE = Path("telethon_login_state.json")


def _client() -> TelegramClient:
    if not settings.TELETHON_API_ID or not settings.TELETHON_API_HASH or not settings.TELETHON_PHONE:
        print("[telethon] заполни TELETHON_API_ID / TELETHON_API_HASH / TELETHON_PHONE в .env")
        sys.exit(1)
    return TelegramClient(SESSION_NAME, int(settings.TELETHON_API_ID), settings.TELETHON_API_HASH)


def request_code() -> None:
    client = _client()
    client.connect()
    sent = client.send_code_request(settings.TELETHON_PHONE)
    STATE_FILE.write_text(json.dumps({"phone_code_hash": sent.phone_code_hash}), encoding="utf-8")
    print(f"[telethon] код отправлен на {settings.TELETHON_PHONE}.")
    print("[telethon] дальше: python telethon_login.py confirm <код>")
    client.disconnect()


def confirm_code(code: str, password: str | None = None) -> None:
    if not STATE_FILE.exists():
        print("[telethon] сначала выполни: python telethon_login.py request")
        return
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    client = _client()
    client.connect()
    try:
        client.sign_in(settings.TELETHON_PHONE, code, phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        if not password:
            print("[telethon] на аккаунте включена двухфакторная защита (облачный пароль).")
            print("[telethon] повтори: python telethon_login.py confirm <код> <пароль>")
            client.disconnect()
            return
        client.sign_in(password=password)
    print(f"[telethon] вход выполнен, сессия сохранена в {SESSION_NAME}.session")
    STATE_FILE.unlink(missing_ok=True)
    client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python telethon_login.py request | confirm <код> [<пароль>]")
    elif sys.argv[1] == "request":
        request_code()
    elif sys.argv[1] == "confirm" and len(sys.argv) >= 3:
        confirm_code(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        print("Использование: python telethon_login.py request | confirm <код> [<пароль>]")
