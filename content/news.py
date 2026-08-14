"""Модуль новостей: источник → верификация/рерайт через YandexGPT → черновик на модерацию.

Пайплайн (см. sources.yaml и docs/plan_city_radio.md §4, §8):
  1. fetch()             — забрать сырой текст с источника
  2. verify_and_rewrite() — YandexGPT проверяет и переписывает, не меняя сути
  3. Draft                — уходит владельцу на согласование (публикация — отдельный шаг,
                             сюда пока не входит: очередь модерации в engine.py ещё не построена)

Почему YandexGPT, а не Claude: Anthropic не обслуживает Россию (юрлицо/ИП, зарегистрированное
в России, подпадает под ограничение по владению из Supported Regions Policy — см. обсуждение
в чате 2026-08-14). YandexGPT — российский сервис, без этой проблемы.

Реализованные адаптеры источников: только RSS (см. RssSource — используется для проверки
механики пайплайна на реальном тексте; ни одна из 20 согласованных рубрик RSS не использует).
Telegram/VK/веб-адаптеры под конкретные рубрики из sources.yaml — не реализованы, требуют:
  - Telegram: TELEGRAM_BOT_TOKEN с ботом-участником канала (Bot API getUpdates/webhook)
    или MTProto-сессия (Telethon/Pyrogram) для чтения истории канала.
  - VK: токен VK API.
Вызов при отсутствии адаптера — NotImplementedError с этим же объяснением.
"""
import json
import re
from dataclasses import dataclass

import feedparser
import requests

from config import settings
from content.base import Draft

YANDEX_COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
YANDEX_MODEL = "yandexgpt/latest"  # дешевле: "yandexgpt-lite/latest" — но хуже держит нюансы

SYSTEM_PROMPT = """Ты — редактор городского новостного канала «City Radio» (Новороссийск).
Тебе присылают сырой текст поста из источника и рубрику, к которой он относится.

Твоя задача — переписать текст своими словами для мессенджер-канала, СТРОГО соблюдая:
1. Не менять суть и факты. Никаких деталей, которых нет в оригинале — не выдумывай и не додумывай.
2. Не копируй дословно — перескажи. Заголовок короткий, тело — 2-5 предложений, стиль мессенджера.
3. Если рубрика про происшествия/ЧП/ЖКХ и в тексте нет ясного подтверждения (только слухи, один
   неофициальный источник, домыслы) — не сглаживай это, а укажи в concerns и поставь needs_manual_review=true.
4. Если рубрика про здоровье — только общие сведения, без советов лечения/диагнозов; если текст
   не является общим сведением, а похож на медицинский совет — отметь в concerns.
5. Если оригинал непонятен, обрублен или, похоже, вырван из контекста — needs_manual_review=true.
6. HTML-разметка Telegram допустима в body: <b>жирный</b>, <i>курсив</i> — больше ничего.

Ответ — ТОЛЬКО валидный JSON, без markdown-обёртки (без ```), без текста до/после, строго по схеме:
{"title": "...", "body": "...", "concerns": ["..."], "needs_manual_review": true}
где concerns — пустой массив [], если замечаний нет."""


@dataclass
class RawItem:
    title: str
    text: str
    url: str


class RssSource:
    """Читает RSS-ленту. Рабочий адаптер — проверено на novorab.ru/feed/."""

    def __init__(self, name: str, feed_url: str):
        self.name = name
        self.feed_url = feed_url

    def fetch(self, limit: int = 5) -> list:
        parsed = feedparser.parse(self.feed_url)
        items = []
        for entry in parsed.entries[:limit]:
            text = getattr(entry, "summary", "") or getattr(entry, "title", "")
            items.append(RawItem(title=entry.title, text=text, url=entry.link))
        return items


def _unimplemented_source(source_type: str) -> None:
    needs = {
        "telegram": "TELEGRAM_BOT_TOKEN + бот добавлен в канал (Bot API) или MTProto-сессия",
        "vk": "VK_API_TOKEN",
        "web": "адаптер под конкретный сайт (структура страницы у каждого источника своя)",
        "manual": "источник помечен как ручной в sources.yaml — автоматического чтения не будет",
    }
    raise NotImplementedError(
        f"Адаптер источника '{source_type}' не реализован. Нужно: {needs.get(source_type, '?')}"
    )


def _call_yandexgpt(system_prompt: str, user_content: str) -> str:
    payload = {
        "modelUri": f"gpt://{settings.YANDEX_FOLDER_ID}/{YANDEX_MODEL}",
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "2000"},
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_content},
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {settings.YANDEX_API_KEY}",
        "x-folder-id": settings.YANDEX_FOLDER_ID,
    }
    r = requests.post(YANDEX_COMPLETION_URL, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"YandexGPT вернул ошибку: {data['error']}")
    return data["result"]["alternatives"][0]["message"]["text"]


def _extract_json(raw: str) -> dict:
    """YandexGPT не гарантирует чистый JSON (в отличие от structured outputs у Claude) —
    иногда оборачивает в ```json ... ``` или добавляет пояснение. Вырезаем сам объект."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Не нашёл JSON в ответе модели: {raw[:200]!r}")
    return json.loads(match.group(0))


def verify_and_rewrite(rubric: str, item: RawItem) -> Draft:
    """Прогоняет сырой текст через YandexGPT: проверка + рерайт без искажения смысла.

    Модель иногда отказывается отвечать (фильтры безопасности YandexGPT) вместо JSON —
    в этом случае одна "плохая" новость не должна ронять весь прогон: она просто
    уходит на ручную проверку как есть, с текстом отказа в concerns.
    """
    user_content = (
        f"Рубрика: {rubric}\n"
        f"Заголовок источника: {item.title}\n"
        f"Текст источника:\n{item.text}"
    )

    raw = _call_yandexgpt(SYSTEM_PROMPT, user_content)

    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        return Draft(
            rubric=rubric,
            source_name=item.title,
            source_url=item.url,
            original_title=item.title,
            original_text=item.text,
            title=item.title,
            body=item.text,
            concerns=[f"модель не вернула рерайт (возможно, отказ фильтра): {raw[:300]}"],
            needs_manual_review=True,
        )

    return Draft(
        rubric=rubric,
        source_name=item.title,
        source_url=item.url,
        original_title=item.title,
        original_text=item.text,
        title=data["title"],
        body=data["body"],
        concerns=data.get("concerns", []),
        needs_manual_review=data.get("needs_manual_review", False),
    )


def _print_draft(draft: Draft) -> None:
    print("=" * 70)
    print(f"[{draft.rubric}] {draft.source_url}")
    print("-- ОРИГИНАЛ " + "-" * 40)
    print(draft.original_title)
    print(draft.original_text[:500])
    print("-- ЧЕРНОВИК " + "-" * 40)
    print(f"<b>{draft.title}</b>")
    print(draft.body)
    if draft.concerns:
        print(f"⚠ Замечания: {'; '.join(draft.concerns)}")
    print(f"Нужна ручная проверка: {'да' if draft.needs_manual_review else 'нет'}")


def run_rss_test(feed_url: str, rubric: str = "Тест механики (RSS)", limit: int = 2) -> None:
    """Локальный прогон пайплайна на реальном RSS-источнике.

    Ни одна из согласованных рубрик не использует RSS (см. sources.yaml) — это тест
    механики fetch → verify_and_rewrite на живом тексте, не привязанный к конкретной рубрике.
    """
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    source = RssSource(name="novorab.ru", feed_url=feed_url)
    items = source.fetch(limit=limit)

    if not items:
        print(f"[news] источник {feed_url} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        _print_draft(draft)


def run_rss_test_to_moderation(feed_url: str, rubric: str = "Тест механики (RSS)", limit: int = 1) -> None:
    """Как run_rss_test, но черновики уходят боту-модератору в Telegram, а не в консоль."""
    if not settings.YANDEX_API_KEY or not settings.YANDEX_FOLDER_ID:
        print("[news] YANDEX_API_KEY / YANDEX_FOLDER_ID не заданы в .env — рерайт недоступен.")
        return

    from moderation import ModerationBot  # локальный импорт — модерация не нужна остальному модулю

    bot = ModerationBot()
    if not bot.token or not bot.owner_chat_id:
        print("[news] TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_CHAT_ID не заданы в .env — отправлять некуда.")
        return

    source = RssSource(name="novorab.ru", feed_url=feed_url)
    items = source.fetch(limit=limit)
    if not items:
        print(f"[news] источник {feed_url} не отдал ни одной записи")
        return

    for item in items:
        draft = verify_and_rewrite(rubric, item)
        bot.send_draft(draft)
        print(f"[news] черновик отправлен на модерацию: {draft.title}")

    # Отправка и приём кнопок должны жить в одном процессе — pending хранится в памяти
    # конкретного объекта ModerationBot, поэтому слушаем кнопки этим же ботом, а не отдельным.
    bot.poll_forever()


if __name__ == "__main__":
    run_rss_test("https://novorab.ru/feed/")
