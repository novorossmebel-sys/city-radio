"""Бот-модератор: шлёт черновики (Draft) владельцу в личку, ждёт решения.

Кнопки под каждым черновиком: ✅ Опубликовать / ✏️ Править / ❌ Отклонить.
- «Опубликовать» → пост уходит в MAX → пауза 30-40 сек → пост уходит в Telegram-канал.
- «Отклонить» → черновик просто отбрасывается.
- «Править» → владелец отвечает текстом (первая строка — заголовок, остальное — тело),
  бот подставляет правку и заново присылает черновик с теми же тремя кнопками.

Используется тот же бот (TELEGRAM_BOT_TOKEN), что публикует в канал — он же пишет
владельцу в личные сообщения. Владелец должен хотя бы раз написать боту (например /start),
иначе Telegram не даст боту первым начать переписку.

Ограничения этой версии (осознанно, для локальных тестов):
- Состояние («что ждёт решения», «кто сейчас правит какой черновик») хранится в памяти
  процесса — при перезапуске бота все неподтверждённые черновики теряются. Для продакшена
  нужно вынести в файл/БД (см. docs/handoff_session_2026-08-13.md).
- Обработка обновлений идёт последовательно (long polling в один поток): пока идёт публикация
  с 30-40-секундной паузой, бот не отвечает на другие кнопки. Для одного владельца это не
  проблема, но при росте — стоит сделать асинхронно.
"""
import random
import time
import uuid

import requests

from config import settings, load_cities
from content.base import Draft
from publishers.telegram import TelegramPublisher
from publishers.max import MaxPublisher

PUBLISH_DELAY_RANGE = (30, 40)  # секунды между публикацией в MAX и в Telegram


def _format_draft_message(draft: Draft) -> str:
    lines = [
        f"📌 Рубрика: {draft.rubric}",
        f"🔗 Источник: {draft.source_name}",
        "",
        f"<b>{draft.title}</b>",
        "",
        draft.body,
    ]
    if draft.concerns:
        lines += ["", "⚠ " + "; ".join(draft.concerns)]
    return "\n".join(lines)


class ModerationBot:
    def __init__(self, city_key: str = "novorossiysk"):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.owner_chat_id = settings.OWNER_TELEGRAM_CHAT_ID
        self.city_key = city_key
        self.telegram_pub = TelegramPublisher()
        self.max_pub = MaxPublisher()
        self.pending = {}        # draft_id -> Draft, ждут решения владельца
        self.awaiting_edit = {}  # chat_id (str) -> draft_id, ждём текст правки
        self._offset = 0

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send_draft(self, draft: Draft) -> str:
        """Отправляет черновик владельцу на модерацию. Возвращает id черновика."""
        draft_id = uuid.uuid4().hex[:8]
        self.pending[draft_id] = draft

        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Опубликовать", "callback_data": f"approve:{draft_id}"},
                {"text": "✏️ Править", "callback_data": f"edit:{draft_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{draft_id}"},
            ]]
        }
        r = requests.post(
            self._api("sendMessage"),
            json={
                "chat_id": self.owner_chat_id,
                "text": _format_draft_message(draft),
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
            timeout=30,
        )
        if not r.ok or not r.json().get("ok"):
            print(f"[moderation] не смог отправить черновик: {r.text}")
        return draft_id

    def _publish_draft(self, draft: Draft) -> None:
        cities = load_cities()
        city = cities.get(self.city_key, {})
        post = draft.to_post()

        max_ok = self.max_pub.publish(city.get("max_channel", ""), post)
        print(f"[moderation] MAX: {'OK' if max_ok else 'пропущено/ошибка'}")

        delay = random.randint(*PUBLISH_DELAY_RANGE)
        time.sleep(delay)

        tg_ok = self.telegram_pub.publish(city.get("telegram_channel", ""), post)
        print(f"[moderation] Telegram: {'OK' if tg_ok else 'пропущено/ошибка'}")

    def _answer_callback(self, callback_query_id: str, text: str = "") -> None:
        requests.post(
            self._api("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )

    def _send_text(self, chat_id, text: str) -> None:
        requests.post(self._api("sendMessage"), json={"chat_id": chat_id, "text": text}, timeout=15)

    def _handle_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        self._answer_callback(cq["id"])

        if ":" not in data:
            return
        action, draft_id = data.split(":", 1)
        draft = self.pending.get(draft_id)
        if draft is None:
            self._send_text(chat_id, "Этот черновик уже обработан или устарел.")
            return

        if action == "approve":
            self._send_text(chat_id, "Публикую…")
            self._publish_draft(draft)
            self._send_text(chat_id, "Опубликовано ✅")
            del self.pending[draft_id]
        elif action == "reject":
            self._send_text(chat_id, "Отклонено ❌")
            del self.pending[draft_id]
        elif action == "edit":
            self.awaiting_edit[str(chat_id)] = draft_id
            self._send_text(
                chat_id,
                "Пришлите исправленный текст ответом на это сообщение.\n"
                "Первая строка — заголовок, остальное — текст поста.",
            )

    def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "")
        if not text:
            return

        draft_id = self.awaiting_edit.get(chat_id)
        if draft_id is None or draft_id not in self.pending:
            return  # не в режиме правки — игнорируем произвольные сообщения

        draft = self.pending[draft_id]
        if "\n" in text:
            title, body = text.split("\n", 1)
            draft.title, draft.body = title.strip(), body.strip()
        else:
            draft.body = text.strip()

        del self.awaiting_edit[chat_id]
        self._send_text(chat_id, "Обновил черновик:")
        self.send_draft(draft)  # пересылаем правленый вариант с кнопками под новым id
        del self.pending[draft_id]  # старая версия больше не актуальна

    def poll_forever(self) -> None:
        if not self.token or not self.owner_chat_id:
            print("[moderation] TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_CHAT_ID не заданы — бот не запущен.")
            return

        print("[moderation] бот-модератор запущен, жду решений владельца. Ctrl+C для выхода.")
        while True:
            r = requests.get(
                self._api("getUpdates"),
                params={"offset": self._offset, "timeout": 30},
                timeout=40,
            )
            if not r.ok:
                print(f"[moderation] getUpdates ошибка: {r.text}")
                time.sleep(5)
                continue

            for update in r.json().get("result", []):
                self._offset = update["update_id"] + 1
                if "callback_query" in update:
                    self._handle_callback(update["callback_query"])
                elif "message" in update:
                    self._handle_message(update["message"])
