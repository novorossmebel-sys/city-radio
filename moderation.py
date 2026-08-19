"""Бот-модератор: шлёт черновики (Draft) владельцу в личку, ждёт решения.

Кнопки под каждым черновиком: ✅ Опубликовать / ✏️ Править / ❌ Отклонить, плюс переключатели
показа источника (по умолчанию скрыт) и — если у поста есть фото — показа фото (по умолчанию
показано). Если у черновика есть фото/альбом (см. content/news.py → TelegramChannelSource),
они шлются владельцу отдельным превью-сообщением перед текстом с кнопками.
- «Опубликовать» → пост уходит в MAX → пауза 30-40 сек → пост уходит в Telegram-канал →
  владельцу приходит сообщение с кнопками-реакциями 👍/🔥/👎/♻️ (личный обучающий слой,
  раздел политики владельца 2026-08-19) — не самоудаляется, реагировать можно в любой момент.
- «Отклонить» → черновик просто отбрасывается.
- «Править» → владелец отвечает текстом (первая строка — заголовок, остальное — тело),
  бот подставляет правку и заново присылает черновик с теми же кнопками.

Используется тот же бот (TELEGRAM_BOT_TOKEN), что публикует в канал — он же пишет
владельцу в личные сообщения. Владелец должен хотя бы раз написать боту (например /start),
иначе Telegram не даст боту первым начать переписку.

Состояние («что ждёт решения», «кто сейчас правит какой черновик», offset getUpdates)
хранится не только в памяти, но и в moderation_state.json рядом с проектом — при перезапуске
бота (крах, обновление кода, перезагрузка ПК) очередь восстанавливается, а не теряется.
Файл — рабочее состояние процесса, не секрет, но в .gitignore: незачем коммитить чужой
незавершённый черновик.

Ограничение этой версии (осознанно, для локальных тестов):
- Обработка обновлений идёт последовательно (long polling в один поток): пока идёт публикация
  с 30-40-секундной паузой, бот не отвечает на другие кнопки. Для одного владельца это не
  проблема, но при росте — стоит сделать асинхронно.
"""
import json
import mimetypes
import random
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import requests

from config import settings, load_cities
from content.base import Draft, TELEGRAM_CAPTION_LIMIT
from publishers.telegram import TelegramPublisher
from publishers.max import MaxPublisher

PUBLISH_DELAY_RANGE = (30, 40)  # секунды между публикацией в MAX и в Telegram
STATUS_MESSAGE_TTL = 10  # секунд до удаления "Публикую…" после факта (сообщение с реакциями не удаляется)
STATE_FILE = Path("moderation_state.json")


def _format_draft_message(draft: Draft) -> str:
    lines = [
        f"📌 Рубрика: {draft.rubric}",
        f"🔗 Источник: {draft.source_name}" + ("" if draft.include_source else " (сейчас скрыт в посте)"),
        "",
        f"<b>{draft.title}</b>",
        "",
        draft.body,
    ]
    if draft.concerns:
        lines += ["", "⚠ " + "; ".join(draft.concerns)]
    return "\n".join(lines)


def _keyboard_for(draft: Draft, draft_id: str) -> dict:
    # "подсветка" кнопки — единственный доступный в Bot API способ показать активное
    # состояние: галочка спереди, когда источник/фото включены (источник по умолчанию
    # выключен, фото — по умолчанию включено, это часть контента, а не атрибуция).
    source_label = "✅ 🔗 Источник виден" if draft.include_source else "🔗 Показать источник"
    rows = [
        [
            {"text": "✅ Опубликовать", "callback_data": f"approve:{draft_id}"},
            {"text": "✏️ Править", "callback_data": f"edit:{draft_id}"},
            {"text": "❌ Отклонить", "callback_data": f"reject:{draft_id}"},
        ],
        [{"text": source_label, "callback_data": f"toggle_source:{draft_id}"}],
    ]
    if draft.image_paths:
        photo_label = "✅ 🖼 Фото будет в посте" if draft.include_photo else "🖼 Без фото"
        rows.append([{"text": photo_label, "callback_data": f"toggle_photo:{draft_id}"}])
    return {"inline_keyboard": rows}


# Личный обучающий слой (раздел политики владельца, 2026-08-19, последний абзац): реакции
# после публикации — только сбор сырых данных, без автоматической подстройки весов пока
# (владелец сам предложил сперва 2-3 недели копить реакции, прежде чем что-то менять).
REACTION_OPTIONS = (("👍", "полезно"), ("🔥", "оч. интересно"), ("👎", "не нужно"), ("♻️", "уже знал"))


def _reaction_keyboard(feedback_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": emoji, "callback_data": f"react:{feedback_id}:{emoji}"} for emoji, _ in REACTION_OPTIONS
    ]]}


class ModerationBot:
    def __init__(self, city_key: str = "novorossiysk"):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.owner_chat_id = settings.OWNER_TELEGRAM_CHAT_ID
        self.city_key = city_key
        self.telegram_pub = TelegramPublisher()
        self.max_pub = MaxPublisher()
        self.pending = {}          # draft_id -> Draft, ждут решения владельца
        self.awaiting_edit = {}    # chat_id (str) -> draft_id, ждём текст правки
        self.photo_messages = {}   # draft_id -> [message_id...], превью фото в личке владельца
        self._removed_draft_ids = set()  # см. _save_state — чтобы merge с диском не воскрешал удалённое
        self._offset = 0
        self._load_state()

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _load_state(self) -> None:
        """Восстанавливает очередь после перезапуска. Повреждённый/отсутствующий файл — не ошибка,
        просто стартуем с чистого состояния (как раньше, до появления персистентности)."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.pending = {k: Draft(**v) for k, v in data.get("pending", {}).items()}
            self.awaiting_edit = data.get("awaiting_edit", {})
            self.photo_messages = data.get("photo_messages", {})
            self._offset = data.get("offset", 0)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            print(f"[moderation] не смог прочитать {STATE_FILE}, стартую с пустой очереди: {e}")

    def _save_state(self) -> None:
        """Сливает с текущим содержимым файла, а не просто перезаписывает своей копией.

        Без этого долгая блокирующая операция (например, 30-40-секундная пауза при
        публикации — см. _publish_draft) даёт окно, в которое другой процесс (разовый
        тестовый скрипт, ещё один send_draft) может дописать в файл черновик, которого
        нет в НАШЕЙ памяти — а наш следующий _save_state() тогда просто стирал бы его,
        отвечая потом на честный клик "уже обработан или устарел" (было дважды за
        2026-08-17, разными путями). Explicit remove (del self.pending[...]) — через
        _removed_draft_ids, иначе слияние с диском воскрешало бы то, что мы только что
        намеренно удалили."""
        disk_pending, disk_awaiting, disk_photo = {}, {}, {}
        if STATE_FILE.exists():
            try:
                disk_data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                disk_pending = disk_data.get("pending", {})
                disk_awaiting = disk_data.get("awaiting_edit", {})
                disk_photo = disk_data.get("photo_messages", {})
            except (json.JSONDecodeError, OSError):
                pass

        merged_pending = dict(disk_pending)
        merged_pending.update({k: asdict(v) for k, v in self.pending.items()})
        merged_photo = dict(disk_photo)
        merged_photo.update(self.photo_messages)
        for removed_id in self._removed_draft_ids:
            merged_pending.pop(removed_id, None)
            merged_photo.pop(removed_id, None)

        merged_awaiting = dict(disk_awaiting)
        merged_awaiting.update(self.awaiting_edit)

        data = {
            "pending": merged_pending,
            "awaiting_edit": merged_awaiting,
            "photo_messages": merged_photo,
            "offset": self._offset,
        }
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _reload_pending(self) -> None:
        """Подхватывает черновики, добавленные в файл другим процессом (например,
        разовым тестовым скриптом, который делает send_draft() и сразу завершается) —
        не только тем, что запустил этот poll_forever(). Без этого бот, слушающий кнопки,
        не знает о черновике, которого не было в его собственной памяти при старте, и
        отвечает "уже обработан или устарел" на честный клик по свежей кнопке."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for draft_id, draft_data in data.get("pending", {}).items():
            if draft_id not in self.pending:
                self.pending[draft_id] = Draft(**draft_data)
        for chat_id, draft_id in data.get("awaiting_edit", {}).items():
            self.awaiting_edit.setdefault(chat_id, draft_id)
        for draft_id, message_ids in data.get("photo_messages", {}).items():
            self.photo_messages.setdefault(draft_id, message_ids)

    def _send_photos_preview(self, chat_id, image_paths: list) -> list:
        """Шлёт фото/альбом владельцу как превью (без подписи — текст идёт отдельным
        сообщением следом, см. _format_draft_message). Возвращает id отправленных сообщений."""
        message_ids = []
        files = {}
        try:
            if len(image_paths) == 1:
                with open(image_paths[0], "rb") as f:
                    r = requests.post(
                        self._api("sendPhoto"),
                        data={"chat_id": chat_id},
                        files={"photo": f},
                        timeout=30,
                    )
                if r.ok and r.json().get("ok"):
                    message_ids.append(r.json()["result"]["message_id"])
            else:
                media = []
                for i, path in enumerate(image_paths):
                    field_name = f"photo{i}"
                    files[field_name] = open(path, "rb")
                    media.append({"type": "photo", "media": f"attach://{field_name}"})
                r = requests.post(
                    self._api("sendMediaGroup"),
                    data={"chat_id": chat_id, "media": json.dumps(media)},
                    files=files,
                    timeout=60,
                )
                if r.ok and r.json().get("ok"):
                    message_ids.extend(m["message_id"] for m in r.json()["result"])
        finally:
            for f in files.values():
                f.close()

        if not message_ids:
            print("[moderation] не смог отправить превью фото")
        return message_ids

    def _send_photo_with_caption(self, chat_id, image_path: str, caption: str, keyboard: dict):
        """Одно фото + подпись + кнопки одним сообщением — для короткого текста (умещается
        в лимит подписи Telegram), чтобы превью на модерации выглядело как будущий пост."""
        with open(image_path, "rb") as f:
            r = requests.post(
                self._api("sendPhoto"),
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                      "reply_markup": json.dumps(keyboard)},
                files={"photo": f},
                timeout=30,
            )
        if r.ok and r.json().get("ok"):
            return r.json()["result"]["message_id"]
        print(f"[moderation] не смог отправить фото с подписью: {r.text}")
        return None

    def _cleanup_images(self, draft: Draft) -> None:
        # media_cache/ — временные скачанные фото источника, можно удалять после публикации.
        # Всё остальное (например assets/danger_photos/ — готовые постоянные иллюстрации
        # опасности, см. content/news.py → DANGER_PHOTO_RULES) переиспользуется — не трогаем.
        for path in draft.image_paths:
            if not path.startswith("media_cache"):
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def send_draft(self, draft: Draft) -> str:
        """Отправляет черновик владельцу на модерацию. Возвращает id черновика."""
        draft_id = uuid.uuid4().hex[:8]
        self.pending[draft_id] = draft
        text = _format_draft_message(draft)
        keyboard = _keyboard_for(draft, draft_id)

        # короткий текст + одно фото — превью одним сообщением (фото+подпись+кнопки),
        # так и будет выглядеть реальный пост. Альбом (несколько фото) не может нести
        # кнопки в Bot API (sendMediaGroup их не поддерживает) — там фото и текст всегда
        # раздельно, как и длинный текст (не влезает в лимит подписи).
        if len(draft.image_paths) == 1 and len(text) <= TELEGRAM_CAPTION_LIMIT:
            msg_id = self._send_photo_with_caption(self.owner_chat_id, draft.image_paths[0], text, keyboard)
            if msg_id is None:
                print("[moderation] не смог отправить черновик с фото")
            self._save_state()
            return draft_id

        if draft.image_paths:
            photo_ids = self._send_photos_preview(self.owner_chat_id, draft.image_paths)
            if photo_ids:
                self.photo_messages[draft_id] = photo_ids

        self._save_state()

        r = requests.post(
            self._api("sendMessage"),
            json={
                "chat_id": self.owner_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
            timeout=30,
        )
        if not r.ok or not r.json().get("ok"):
            print(f"[moderation] не смог отправить черновик: {r.text}")
        return draft_id

    def _publish_draft(self, draft: Draft) -> bool:
        """Публикует в MAX и Telegram. Оба паблишера сами не бросают исключений при
        сетевых сбоях (см. publishers/telegram.py, publishers/max.py) — просто отдают
        False, чтобы владелец получил честный статус, а не зависшее "Публикую…" навсегда."""
        cities = load_cities()
        city = cities.get(self.city_key, {})
        post = draft.to_post()

        max_ok = self.max_pub.publish(city.get("max_channel", ""), post)
        print(f"[moderation] MAX: {'OK' if max_ok else 'пропущено/ошибка'}")

        delay = random.randint(*PUBLISH_DELAY_RANGE)
        time.sleep(delay)

        tg_ok = self.telegram_pub.publish(city.get("telegram_channel", ""), post)
        print(f"[moderation] Telegram: {'OK' if tg_ok else 'пропущено/ошибка'}")
        return tg_ok  # Telegram — основная площадка; MAX пока без токена, его пропуск не считаем сбоем

    def _answer_callback(self, callback_query_id: str, text: str = "") -> None:
        requests.post(
            self._api("answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )

    def _send_text(self, chat_id, text: str):
        """Возвращает id отправленного сообщения (или None при сбое) — нужен, чтобы потом
        можно было удалить статусные сообщения вроде "Публикую…"/"Опубликовано" (см. approve)."""
        r = requests.post(self._api("sendMessage"), json={"chat_id": chat_id, "text": text}, timeout=15)
        if r.ok and r.json().get("ok"):
            return r.json()["result"]["message_id"]
        return None

    def send_text(self, chat_id, text: str):
        """Публичная обёртка над _send_text — для вызовов извне модуля (например,
        заголовочного сообщения дайджест-выпуска, см. content/digest_compose.py)."""
        return self._send_text(chat_id, text)

    def _send_reaction_prompt(self, chat_id, feedback_id: int):
        """Сообщение "Опубликовано" с кнопками-реакциями вместо самоудаляющегося статуса —
        специально НЕ удаляется по STATUS_MESSAGE_TTL (в отличие от "Публикую…"), чтобы
        владелец мог оценить материал в любой момент, а не только в первые 10 секунд."""
        r = requests.post(
            self._api("sendMessage"),
            json={
                "chat_id": chat_id, "text": "Опубликовано ✅\n\nКак вам материал?",
                "reply_markup": _reaction_keyboard(feedback_id),
            },
            timeout=15,
        )
        if r.ok and r.json().get("ok"):
            return r.json()["result"]["message_id"]
        return None

    def _handle_reaction(self, cq: dict, data: str) -> None:
        _, feedback_id, emoji = data.split(":", 2)
        from content.digest_store import record_reaction
        record_reaction(int(feedback_id), emoji)
        requests.post(
            self._api("editMessageText"),
            json={
                "chat_id": cq["message"]["chat"]["id"], "message_id": cq["message"]["message_id"],
                "text": f"Опубликовано ✅\n\nВаша реакция: {emoji}",
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=15,
        )

    def send_document(self, chat_id, file_path: str, caption: str = None, content_type: str = None) -> bool:
        """Отправляет файл (например, отчёт/статистику) владельцу как документ Telegram.

        Явно задаём имя файла и content-type — без этого requests подставляет полный
        Windows-путь как имя и application/octet-stream как тип, из-за чего некоторые
        клиенты (в т.ч. Telegram на iOS) неверно угадывают кодировку при предпросмотре
        текстового файла и показывают "тарабарщину" (владелец, 2026-08-19)."""
        name = Path(file_path).name
        if content_type is None:
            content_type, _ = mimetypes.guess_type(name)
            content_type = content_type or "text/plain; charset=utf-8"
        try:
            with open(file_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                files = {"document": (name, f, content_type)}
                r = requests.post(self._api("sendDocument"), data=data, files=files, timeout=60)
        except (OSError, requests.RequestException) as e:
            print(f"[moderation] не смог отправить документ {file_path}: {e}")
            return False
        ok = r.ok and r.json().get("ok", False)
        if not ok:
            print(f"[moderation] ошибка отправки документа: {r.text}")
        return ok

    def _delete_message(self, chat_id, message_id: int) -> None:
        # сетевой сбой здесь не должен обрывать остальную очистку (например, при approve
        # ниже идут ещё удаления после этого вызова) — не поднимаем исключение дальше
        try:
            requests.post(
                self._api("deleteMessage"),
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"[moderation] не смог удалить сообщение {message_id}: {e}")

    def _handle_callback(self, cq: dict) -> None:
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        self._answer_callback(cq["id"])

        if data.startswith("react:"):
            # Реакция ссылается на запись в digest_store.feedback, а не на Draft в
            # self.pending (материал уже опубликован и удалён из pending) — отдельная
            # ветка ДО обычного action:draft_id разбора ниже.
            self._handle_reaction(cq, data)
            return

        if ":" not in data:
            return
        action, draft_id = data.split(":", 1)
        # безусловно, не только когда draft_id не найден: черновик мог быть найден (уже
        # подхвачен ранним reload), но фото к нему прислал отдельный процесс уже ПОСЛЕ
        # этого — тогда photo_messages для него ещё не подтянут, и удаление фото при
        # approve/reject молча ничего не найдёт и не удалит.
        self._reload_pending()
        draft = self.pending.get(draft_id)
        if draft is None:
            self._send_text(chat_id, "Этот черновик уже обработан или устарел.")
            return

        if action == "approve":
            self._delete_message(chat_id, cq["message"]["message_id"])
            for pid in self.photo_messages.pop(draft_id, []):
                self._delete_message(chat_id, pid)
            publishing_id = self._send_text(chat_id, "Публикую…")
            tg_ok = self._publish_draft(draft)

            if not tg_ok:
                # черновик НЕ удаляем из pending здесь — иначе повторное "Опубликовать"
                # получит "уже обработан или устарел". Пересылаем заново с рабочими
                # кнопками под новым id, старую запись убираем только теперь.
                if publishing_id:
                    self._delete_message(chat_id, publishing_id)
                self._send_text(
                    chat_id,
                    "⚠ Публикация в Telegram не удалась (сетевая ошибка). Пришлю черновик заново.",
                )
                self.send_draft(draft)
                del self.pending[draft_id]
                self._removed_draft_ids.add(draft_id)
                self._save_state()
                return

            self._cleanup_images(draft)

            from content.digest_store import log_published
            feedback_id = log_published(draft.rubric, draft.source_name, draft.title)
            self._send_reaction_prompt(chat_id, feedback_id)  # НЕ самоудаляется — реагировать можно в любой момент

            del self.pending[draft_id]
            self._removed_draft_ids.add(draft_id)
            self._save_state()

            from content.rotation import record_resolved
            record_resolved(draft.rubric)

            # "Публикую…" своё дело сделал — недолго повисит, чтобы владелец успел увидеть,
            # и само удалится (в отличие от сообщения с реакциями выше)
            time.sleep(STATUS_MESSAGE_TTL)
            if publishing_id:
                self._delete_message(chat_id, publishing_id)
        elif action == "reject":
            self._delete_message(chat_id, cq["message"]["message_id"])
            for pid in self.photo_messages.pop(draft_id, []):
                self._delete_message(chat_id, pid)
            self._cleanup_images(draft)
            del self.pending[draft_id]
            self._removed_draft_ids.add(draft_id)
            self._save_state()
            self._handle_reject_followup(draft)
        elif action == "edit":
            self._delete_message(chat_id, cq["message"]["message_id"])
            for pid in self.photo_messages.pop(draft_id, []):
                self._delete_message(chat_id, pid)
            self.awaiting_edit[str(chat_id)] = draft_id
            self._save_state()
            self._send_text(
                chat_id,
                "Пришлите исправленный текст ответом на это сообщение.\n"
                "Первая строка — заголовок, остальное — текст поста.",
            )
        elif action == "toggle_source":
            draft.include_source = not draft.include_source
            self._save_state()
            self._update_keyboard(chat_id, cq["message"]["message_id"], draft, draft_id)
        elif action == "toggle_photo":
            draft.include_photo = not draft.include_photo
            self._save_state()
            self._update_keyboard(chat_id, cq["message"]["message_id"], draft, draft_id)

    def _update_keyboard(self, chat_id, message_id: int, draft: Draft, draft_id: str) -> None:
        requests.post(
            self._api("editMessageReplyMarkup"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": _keyboard_for(draft, draft_id),
            },
            timeout=15,
        )

    def _handle_reject_followup(self, draft: Draft) -> None:
        """После отклонения — тут же следующий пост из той же рубрики (следующий источник
        по кругу), либо пауза рубрики после 3 отклонений подряд. См. content/rotation.py."""
        from content.rotation import is_paused, record_reject, get_next_item
        from content.news import verify_and_rewrite, build_structured_draft

        # рубрики со структурированными данными (не текст новости) не проходят LLM-рерайт —
        # см. build_structured_draft
        NO_REWRITE_RUBRICS = {"Погода и море", "Афиша/события", "Кино (сеансы)"}

        if is_paused(draft.rubric):
            return  # уже на паузе с прошлого раза — молчим

        just_paused = record_reject(draft.rubric)
        if just_paused:
            self._send_text(
                self.owner_chat_id,
                f"⏸ Рубрика «{draft.rubric}»: 3 отклонения подряд — приостановлена до 10:00 завтра.",
            )
            return

        # реклама (см. verify_and_rewrite → is_advertisement) не должна доходить до владельца
        # вообще — молча пробуем следующий источник по кругу, до нескольких раз подряд
        MAX_AD_SKIPS = 3
        for _ in range(MAX_AD_SKIPS):
            try:
                item = get_next_item(draft.rubric)
                if item is None:
                    self._send_text(
                        self.owner_chat_id,
                        f"[{draft.rubric}] не нашёл следующий источник для авто-подбора.",
                    )
                    return
                if draft.rubric in NO_REWRITE_RUBRICS:
                    next_draft = build_structured_draft(draft.rubric, item)
                else:
                    next_draft = verify_and_rewrite(draft.rubric, item)
            except Exception as e:
                # сетевой/временный сбой (Telethon, YandexGPT) не должен ронять весь бот —
                # без этого один таймаут во время авто-продолжения убивал polling целиком.
                self._send_text(
                    self.owner_chat_id,
                    f"[{draft.rubric}] авто-подбор следующего поста не удался (временная ошибка): {e}",
                )
                return

            if next_draft is not None:
                self.send_draft(next_draft)
                return
            # next_draft is None → реклама, пробуем следующий источник молча

        self._send_text(
            self.owner_chat_id,
            f"[{draft.rubric}] несколько источников подряд оказались рекламой — попробуйте позже.",
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
        self.send_draft(draft)  # пересылаем правленый вариант с кнопками под новым id (сохраняет состояние)
        del self.pending[draft_id]  # старая версия больше не актуальна
        self._removed_draft_ids.add(draft_id)
        self._save_state()

    def poll_forever(self) -> None:
        if not self.token or not self.owner_chat_id:
            print("[moderation] TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_CHAT_ID не заданы — бот не запущен.")
            return

        print("[moderation] бот-модератор запущен, жду решений владельца. Ctrl+C для выхода.")
        while True:
            self._reload_pending()
            try:
                r = requests.get(
                    self._api("getUpdates"),
                    params={"offset": self._offset, "timeout": 30},
                    timeout=40,
                )
            except requests.RequestException as e:
                print(f"[moderation] сетевая ошибка getUpdates: {e}")
                time.sleep(5)
                continue

            if not r.ok:
                print(f"[moderation] getUpdates ошибка: {r.text}")
                time.sleep(5)
                continue

            results = r.json().get("result", [])
            if results:
                # перечитываем прямо перед сохранением — иначе _save_state() ниже (которое
                # пишет offset) перезапишет файл нашим устаревшим self.pending и сотрёт
                # черновик, добавленный другим процессом, пока мы ждали в getUpdates
                self._reload_pending()
            for update in results:
                self._offset = update["update_id"] + 1
                self._save_state()
                try:
                    if "callback_query" in update:
                        self._handle_callback(update["callback_query"])
                    elif "message" in update:
                        self._handle_message(update["message"])
                except Exception as e:
                    # один сбойный апдейт не должен ронять весь процесс модерации
                    print(f"[moderation] ошибка обработки апдейта: {e}")
