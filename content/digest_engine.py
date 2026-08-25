"""Сбор и скоринг кандидатов для дайджест-движка (редакционная политика владельца,
2026-08-19, разделы 1-12). Слой ПОВЕРХ существующего content/news.py — не меняет и не
дублирует verify_and_rewrite/danger-photo/footer-stripping, а вызывает их же.

Пайплайн одного опроса (collect_candidates):
  resolve sources.yaml → для каждого канала fetch() → pre-filter (keyword DROP-01..03,
  HEALTH_CLAIM карантин, юмор — отдельный silo без скоринга) → один LLM-вызов
  (рерайт + классификация + под-рейтинги) → event_key → сверка с events (дедуп/novelty/
  статус-машина БПЛА) → скоринг + штрафы → маршрут → сохранение в digest_store.
  NOW-маршрут — сразу Draft → ModerationBot.send_draft() (тот же путь, что и раньше,
  просто без ручного триггера).
"""
import random
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from content.base import Draft
from content.digest_store import (
    get_cursor, set_cursor, insert_candidate, update_candidate_status, get_event, upsert_event,
    is_in_cooldown, mark_event_published,
)
from content.llm import call_yandexgpt, extract_json
from content.news import (
    RawItem, TelegramChannelSource, AfishaRuSource,
    DANGER_SOURCE_CHANNELS, SEASON_RUBRIC,
    _danger_photo_for, _strip_channel_footer, _ensure_image,
)
from content.source_policy import (
    role_for, HUMOR_ONLY_CHANNELS, HEALTH_CLAIM_SOURCE, HEALTH_CLAIM_TRIGGERS,
    AD_KEYWORDS, SELF_PROMO_KEYWORDS, ENGAGEMENT_KEYWORDS, matches_any, resolve_rubric,
)
from content.tmdb import fetch_poster

CINEMA_PRIMARY_TYPES = {
    "CINEMA_RELEASE", "CINEMA_TRAILER", "CINEMA_CASTING", "CINEMA_REVIEW", "CINEMA_ANALYSIS",
}

SOURCES_FILE = "sources.yaml"
FAST_POLL_LIMIT = 5   # алертных каналов немного, можно взять чуть больше свежих постов
NORMAL_POLL_LIMIT = 5

# Отметка "collect_candidates реально дошёл до конца, не завис" — читает supervisor.py.
# Обновляется независимо от того, нашлось ли что-то новое: сам факт возврата из функции
# важен, а не найденный контент (2026-08-20 — Telethon завис на MsgidDecreaseRetryError
# на много часов, ни разу не бросив исключение наружу, только на перезапуске процесса
# отпустило: content/digest_store.py's WAL-фикс от 2026-08-19 лечит конкурентную запись,
# это отдельная история про подвисший event loop).
HEARTBEAT_FILE = Path("scheduler_heartbeat.txt")

PRIMARY_TYPES = (
    "LOCAL_ALERT", "LOCAL_STATUS", "LOCAL_UTILITY", "LOCAL_EVENT",
    "GENERAL_NEWS", "NEWS_UPDATE",
    "CINEMA_RELEASE", "CINEMA_TRAILER", "CINEMA_CASTING", "CINEMA_REVIEW", "CINEMA_ANALYSIS",
    "RECIPE", "FOOD_SCIENCE", "FOOD_DISCOVERY",
    "HEALTH_CLAIM", "ANALYSIS", "DISCOVERY", "HUMOR",
)

# Классификация — на КАЖДЫЙ пост, прошедший pre-filter (33 канала, десятки постов в день,
# 76% в итоге уходят в ARCHIVE и никогда не показываются читателю, см. обсуждение 2026-08-25).
# Поэтому здесь сознательно НЕТ рерайта (title/body) — только компактная оценка, дешёвый
# вызов с маленьким maxTokens (см. CLASSIFY_MAX_TOKENS). Полный пересказ (дорогой, с двумя
# абзацами текста на выходе) делает отдельный DIGEST_REWRITE_SYSTEM_PROMPT ниже, и только
# для того, что реально прошло отбор (NOW/DIGEST/WEEKLY) — было наоборот (рерайт на всё
# подряд ДО решения о публикации), из-за чего 4/5 стоимости уходило на пересказ того, что
# никто не увидит.
DIGEST_CLASSIFY_SYSTEM_PROMPT = f"""Ты — редактор-аналитик городского Telegram-канала «City Radio»
(Новороссийск). Тебе присылают сырой пост из источника и рубрику. Нужно извлечь структурированные
данные для системы, которая решает — стоит ли это вообще публиковать (владелец канала прямо
просил не показывать читателю то, что не меняет его картину происходящего). Полный пересказ
текста делать НЕ нужно — это отдельный шаг, только для постов, которые реально пройдут отбор.

1. is_advertisement — призыв купить/заказать/перейти по реферальной ссылке (не путать с новостью
   О рекламе/бизнесе). needs_manual_review — если текст непонятен, вырван из контекста, или
   рубрика про происшествия/здоровье и в тексте только слухи/один неофициальный источник.
2. primary_type — ровно один из: {", ".join(PRIMARY_TYPES)}.
3. event_type — короткий ярлык события в SCREAMING_SNAKE_CASE, конкретнее чем primary_type
   (например BPLA_ALERT, WATER_OUTAGE, CINEMA_RELEASE_DATE, RESTAURANT_OPENING).
4. entity — главная сущность события (город/район, название фильма, тема), location — где
   происходит (пусто, если неприменимо), action — суть происходящего в 2-4 словах.
5. importance (0-25): 0-5 курьёз, 6-10 небольшой факт, 11-15 заметное событие, 16-20 важное
   изменение, 21-25 крупное событие.
6. relevance (0-25): Новороссийск 20-25, кино/еда по качеству 10-20, случайное событие другого
   региона 0-8.
7. actionability (0-10): насколько читателю нужно/можно среагировать сейчас (тревога, отключение
   воды, ограничение — высоко; отвлечённая новость — низко).
8. discovery (0-5): открывает ли читателю что-то новое/полезное (гастро-находка, лайфхак).
9. time_sensitive — true, если промедление с публикацией обесценивает новость (тревога,
    отключение, событие сегодня/на этой неделе).
10. Флаги (bool): clickbait — заголовок обещает больше, чем даёт текст; poster_no_new_facts —
    кино-пост это просто новый постер/кадр/пятый тизер без даты/рейтинга/трейлера/кастинга/
    статуса; opinion_no_new_thought — мнение повторяет уже сказанное, не добавляет мысли.

Ответ — ТОЛЬКО валидный JSON, без markdown-обёртки, строго по схеме:
{{"is_advertisement": false, "needs_manual_review": false,
"primary_type": "...", "event_type": "...", "entity": "...", "location": "...", "action": "...",
"importance": 0, "relevance": 0, "actionability": 0, "discovery": 0, "time_sensitive": false,
"clickbait": false, "poster_no_new_facts": false, "opinion_no_new_thought": false}}"""
CLASSIFY_MAX_TOKENS = 500  # только цифры/короткие поля — рерайта в ответе больше нет

DIGEST_REWRITE_SYSTEM_PROMPT = """Ты — редактор городского Telegram-канала «City Radio»
(Новороссийск). Пост уже прошёл отбор и точно будет показан читателю — перепиши его: не меняй
суть, не выдумывай деталей, перескажи ПОЛНОСТЬЮ своими словами, стиль мессенджера. HTML-разметка
допустима только <b>/<i>.

Ответ — ТОЛЬКО валидный JSON, без markdown-обёртки, строго по схеме: {"title": "...", "body": "..."}"""

NOVELTY_SYSTEM_PROMPT = """Ты сравниваешь новый пост с уже известными фактами по тому же
сюжету и оцениваешь, насколько он меняет картину для читателя, который уже видел известные факты.

Шкала NOVELTY:
0 — ничего не поменялось (тот же факт другими словами).
1 — косметическое дополнение (новое фото/постер/тизер без новых фактов) — почти всегда как 0.
2 — небольшое полезное обновление (уточнение состава, новый источник подтверждения).
3 — существенное изменение (точная дата, перенос, первый рейтинг, официальное решение).
4 — сильно меняет сюжет (отмена, резкий рост пострадавших, официальное опровержение).
5 — сменилось состояние непосредственно важного/срочного события.

Ответ — ТОЛЬКО валидный JSON: {"novelty": 0}"""

# Раздел 12 политики — статус-машина БПЛА/ЧС по ключевым словам источника, не LLM (дешевле,
# детерминированно). Порядок важен: проверяем терминальные состояния раньше промежуточных,
# чтобы длинный пост с "сирена ... угроза отменена" не застрял на промежуточном совпадении.
BPLA_STATUS_RULES = [
    ("SAFE", ("угроза отменена", "отмена сигнала", "угрозы нет", "опасност" + "и нет")),
    ("CANCELLED_BUT_THREAT", ("угроза сохраняется",)),
    ("ACTIVE", ("отражение атаки", "идёт отражение", "атака", "работает пво")),
    ("ALERT", ("сирена", "включилась сирена")),
    ("THREAT", ("объявлена угроза", "угроза бпла", "опасность атаки")),
]

PENALTIES = {
    "opinion_no_new_thought": -20,
    "clickbait": -15,
    "unclear_source": -15,
    "cooldown_repeat": -20,
    "poster_no_new_facts": -20,
}

# Раздел 9 — Reliability выводится из роли источника (SOURCE_MAP), а не спрашивается у LLM:
# таблица владельца сама привязана к типу источника, а не к содержанию конкретного поста.
def _reliability_score(channel: str) -> int:
    role = role_for(channel)
    if role.role == "OFFICIAL_LOCAL":
        return 15
    if role.can_confirm is True:
        return 13
    if role.can_confirm is None:  # "⚠" в таблице владельца — частичное подтверждение
        return 10
    if role.role == "GENERAL_RADAR":
        return 5
    if role.role == "ARCHIVE":  # канал не из присланной владельцем таблицы
        return 4
    return 8


def _clamp(value, lo, hi) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, value))


def _bpla_status_from_text(text: str) -> str | None:
    lowered = text.lower()
    for status, phrases in BPLA_STATUS_RULES:
        if any(p in lowered for p in phrases):
            return status
    return None


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _event_key(entity: str, event_type: str, location: str, action: str) -> str:
    return "|".join(_normalize(x) for x in (entity, event_type, location, action))


def _pre_filter_hard_drop(text: str) -> str | None:
    """DROP-01/02/03 (раздел 3) — проверяются ДО любого LLM-вызова. Возвращает причину
    дропа или None, если пост проходит дальше. DROP-04 (пустой визуальный апдейт кино)
    сознательно НЕ здесь — слишком расплывчатый для keyword-правила, реализован как мягкий
    штраф poster_no_new_facts по флагу от LLM (см. PENALTIES)."""
    if matches_any(text, AD_KEYWORDS):
        return "реклама (DROP-01)"
    if matches_any(text, SELF_PROMO_KEYWORDS):
        return "самопромо (DROP-02)"
    if matches_any(text, ENGAGEMENT_KEYWORDS):
        return "engagement bait (DROP-03)"
    return None


def _resolve_channels(spec: dict) -> list[tuple[str, object]]:
    """Возвращает [(channel_username, adapter), ...] для рубрики. Зеркалит разбор
    'telegram:xxx'/'web:xxx' из content/rotation.py._next_source, но отдаёт ВСЕ источники
    рубрики сразу (дайджест-движок опрашивает все каналы каждый цикл, а не по одному по
    кругу, как делает ручная ротация при отклонении черновика)."""
    source_type = spec.get("source_type")
    raw_source = spec.get("source")
    sources_list = raw_source if isinstance(raw_source, list) else [raw_source]

    resolved = []
    for picked in sources_list:
        if not isinstance(picked, str):
            continue
        if ":" in picked and picked.split(":", 1)[0] in ("telegram", "web", "vk"):
            kind, name = picked.split(":", 1)
        else:
            kind, name = source_type, picked

        if kind == "telegram":
            resolved.append((name.lstrip("@"), TelegramChannelSource(name)))
        elif kind == "web" and isinstance(name, str) and "afisha.ru" in name:
            resolved.append((name, AfishaRuSource(name)))
        # web:yandex.ru/pogoda (Погода и море), api (курс валют), manual, vk, недоименные
        # web/mixed-домены (kinopoisk.ru, imdb.com) — адаптера для дайджест-сбора нет, пропускаем
    return resolved


def _load_rubrics() -> dict:
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _channels_to_poll(fast_only: bool) -> list[tuple[str, str, object]]:
    """[(rubric_title, channel_username, adapter), ...] по всем рубрикам sources.yaml.
    fast_only=True — только каналы из DANGER_SOURCE_CHANNELS (частый опрос ради быстрых
    алертов), fast_only=False — все остальные (Погода и море исключена явно: у неё
    отдельный автопост в content/weather.py, не относится к дайджест-движку)."""
    out = []
    for spec in _load_rubrics().values():
        title = spec.get("title", "")
        if title == SEASON_RUBRIC:
            continue
        for channel, adapter in _resolve_channels(spec):
            is_danger = channel in DANGER_SOURCE_CHANNELS
            if fast_only and not is_danger:
                continue
            if not fast_only and is_danger:
                continue  # алертные каналы уже покрыты частым опросом
            out.append((title, channel, adapter))
    return out


def _extract_message_id(url: str) -> int | None:
    match = re.search(r"/(\d+)$", url or "")
    return int(match.group(1)) if match else None


def _fetch_new_items(channel: str, adapter, limit: int) -> list[RawItem]:
    """Фильтрует уже виденное по courser'у (актуально только для Telegram — у веб-источников
    типа афиши нет устойчивого id для курсора, там дедуп целиком на event_key)."""
    items = adapter.fetch(limit=limit)
    if not isinstance(adapter, TelegramChannelSource):
        return items

    cursor = get_cursor(channel)
    fresh, max_id = [], cursor or 0
    for item in items:
        msg_id = _extract_message_id(item.url)
        if msg_id is None:
            fresh.append(item)  # не смогли распознать id — лучше обработать, чем молча потерять
            continue
        if cursor is not None and msg_id <= cursor:
            continue
        fresh.append(item)
        max_id = max(max_id, msg_id)
    if max_id and max_id != cursor:
        set_cursor(channel, max_id)
    return fresh


def _classify(rubric: str, channel: str, item: RawItem) -> dict | None:
    user_content = (
        f"Рубрика: {rubric}\nКанал-источник: @{channel}\n"
        f"Заголовок источника: {item.title}\nТекст источника:\n{item.text}"
    )
    try:
        raw = call_yandexgpt(DIGEST_CLASSIFY_SYSTEM_PROMPT, user_content, max_tokens=CLASSIFY_MAX_TOKENS)
        data = extract_json(raw)
    except Exception as e:
        print(f"[digest_engine] классификация не удалась для @{channel}: {e}")
        return None
    if data.get("primary_type") not in PRIMARY_TYPES:
        data["primary_type"] = "GENERAL_NEWS"
    return data


def _rewrite(rubric: str, channel: str, item: RawItem) -> dict | None:
    """Полный пересказ (title/body) — только для того, что уже прошло отбор (route в
    NOW/DIGEST/WEEKLY), см. комментарий у DIGEST_CLASSIFY_SYSTEM_PROMPT."""
    user_content = (
        f"Рубрика: {rubric}\nКанал-источник: @{channel}\n"
        f"Заголовок источника: {item.title}\nТекст источника:\n{item.text}"
    )
    try:
        raw = call_yandexgpt(DIGEST_REWRITE_SYSTEM_PROMPT, user_content)
        return extract_json(raw)
    except Exception as e:
        print(f"[digest_engine] рерайт не удался для @{channel}: {e}")
        return None


def _assess_novelty(known_facts: list, new_text: str) -> int:
    facts_text = "\n".join(f"- {f}" for f in known_facts) or "(фактов пока нет — это первое сообщение)"
    try:
        raw = call_yandexgpt(
            NOVELTY_SYSTEM_PROMPT,
            f"Уже известные факты по сюжету:\n{facts_text}\n\nНовый пост:\n{new_text[:800]}",
        )
        return _clamp(extract_json(raw).get("novelty"), 0, 5)
    except Exception as e:
        print(f"[digest_engine] оценка novelty не удалась: {e}")
        return 2  # осторожный дефолт — не DROP молча из-за сбоя LLM, но и не приоритет


def _route_for(primary_type: str, total_score: int, time_sensitive: bool, status_changed: bool) -> str:
    if primary_type == "LOCAL_ALERT" and status_changed:
        return "NOW"
    if total_score >= 90 and time_sensitive:
        return "NOW"
    if total_score >= 70:
        return "DIGEST"
    if total_score >= 60:
        return "WEEKLY"
    if total_score >= 45:
        return "ARCHIVE"
    return "DROP"


def _build_draft_for_now(rubric: str, channel: str, item: RawItem, data: dict, candidate_id: int) -> Draft:
    # Подстановка danger-фото и YandexART-фоллбэк уже сделаны в _process_item (общая для
    # NOW/DIGEST/WEEKLY ветка) — item.image_paths здесь уже финальный.
    return Draft(
        rubric=rubric,
        source_name=item.source_label,
        source_url=item.url,
        original_title=item.title,
        original_text=item.text,
        title=data["title"],
        body=data["body"],
        concerns=[],
        needs_manual_review=data.get("needs_manual_review", False),
        image_paths=item.image_paths,
        candidate_ids=[candidate_id],
    )


def _cleanup_item_images(item: RawItem) -> None:
    """Кандидат не дошёл (или дойдёт, но без фото — ARCHIVE) до модерации, значит
    _cleanup_images() в moderation.py его никогда не подхватит — без этого скачанные
    фото/видео так и лежали бы в media_cache/ вечно (нашли 2.6 ГБ накопленных
    2026-08-22, включая несколько видео, ошибочно скачанных как "фото" — см. правку
    _download_photos в content/news.py)."""
    for path in item.image_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as e:
            print(f"[digest_engine] не смог удалить {path}: {e}")


def _process_item(rubric: str, channel: str, item: RawItem) -> None:
    item.text = _strip_channel_footer(item.text)
    if not item.text:
        _cleanup_item_images(item)
        return

    combined_text = f"{item.title} {item.text}"

    # Юмор — отдельный silo (раздел 17), вне общего скоринга. Pre-filter всё равно
    # применяется (юмористический канал тоже может репостить рекламу).
    if channel in HUMOR_ONLY_CHANNELS:
        reason = _pre_filter_hard_drop(combined_text)
        if reason:
            print(f"[digest_engine] дроп @{channel}: {reason}")
            _cleanup_item_images(item)
            return
        insert_candidate(
            source_channel=channel, rubric=rubric, raw_title=item.title, raw_text=item.text,
            url=item.url, image_paths=item.image_paths, primary_type="HUMOR",
            event_key=None, title=item.title, body=item.text,
            route="HUMOR_POOL", status="scored",
        )
        return

    # Раздел 16 — карантин медицинских утверждений: без внешней проверки не публикуется,
    # даже не доходит до обычного скоринга/классификации.
    if channel == HEALTH_CLAIM_SOURCE and matches_any(combined_text, HEALTH_CLAIM_TRIGGERS):
        print(f"[digest_engine] карантин HEALTH_CLAIM @{channel}: {item.title[:60]!r}")
        insert_candidate(
            source_channel=channel, rubric=rubric, raw_title=item.title, raw_text=item.text,
            url=item.url, image_paths=item.image_paths, primary_type="HEALTH_CLAIM",
            event_key=None, title=item.title, body=item.text,
            needs_manual_review=True, route="ARCHIVE", status="archived",
        )
        _cleanup_item_images(item)  # ARCHIVE никогда не уходит в модерацию — фото не нужны
        return

    drop_reason = _pre_filter_hard_drop(combined_text)
    if drop_reason:
        print(f"[digest_engine] дроп @{channel}: {drop_reason}")
        _cleanup_item_images(item)
        return

    data = _classify(rubric, channel, item)
    if data is None:
        _cleanup_item_images(item)
        return  # сбой LLM — уже залогирован в _classify, не роняем весь опрос
    if data.get("is_advertisement"):
        print(f"[digest_engine] дроп @{channel}: реклама (LLM-классификация)")
        _cleanup_item_images(item)
        return

    rubric = resolve_rubric(rubric, channel, data["primary_type"])

    entity, event_type = data.get("entity", ""), data.get("event_type", data["primary_type"])
    location, action = data.get("location", ""), data.get("action", "")
    event_key = _event_key(entity, event_type, location, action) if entity else None

    if data["primary_type"] in CINEMA_PRIMARY_TYPES and entity:
        # настоящий постер вместо случайного фото источника (скриншот, кадр, обложка
        # телеграм-поста) или сгенерированной YandexART-иллюстрации — владелец явно
        # хотел "просто постер" (2026-08-18), ключ TMDB подключили 2026-08-25.
        poster = fetch_poster(entity)
        if poster:
            _cleanup_item_images(item)
            item.image_paths = [poster]

    status_changed = False
    new_status = None
    if event_key:
        existing = get_event(event_key)
        if data["primary_type"] == "LOCAL_ALERT":
            new_status = _bpla_status_from_text(combined_text) or "THREAT"
            novelty = 5 if (existing is None or existing["status"] != new_status) else 0
            status_changed = existing is None or existing["status"] != new_status
        else:
            new_status = None
            novelty = 3 if existing is None else _assess_novelty(existing["known_facts"], combined_text)

        if novelty == 0:
            print(f"[digest_engine] дроп @{channel}: novelty=0 (event_key={event_key})")
            _cleanup_item_images(item)
            return  # ничего нового относительно уже увиденного — раздел 8, NOVELTY=0

        upsert_event(event_key, data["primary_type"], new_status or "OPEN", action or item.title, channel)
    else:
        novelty = 3  # не смогли извлечь сущность — не блокируем публикацию, просто не дедупим

    if event_key and is_in_cooldown(event_key, data["primary_type"]) and novelty < 3:
        print(f"[digest_engine] дроп @{channel}: cooldown, novelty={novelty}<3 (event_key={event_key})")
        _cleanup_item_images(item)
        return  # раздел 18: во время cooldown публикуется только при NOVELTY >= 3

    novelty_score = novelty * 4  # раздел 9 задаёт компонент 0-20, раздел 8 — тир 0-5; шкалируем x4
    reliability = _reliability_score(channel)

    penalty = 0
    if data.get("clickbait"):
        penalty += PENALTIES["clickbait"]
    if data.get("poster_no_new_facts"):
        penalty += PENALTIES["poster_no_new_facts"]
    if data.get("opinion_no_new_thought"):
        penalty += PENALTIES["opinion_no_new_thought"]
    if role_for(channel).role == "ARCHIVE":
        penalty += PENALTIES["unclear_source"]
    if event_key and is_in_cooldown(event_key, data["primary_type"]):
        penalty += PENALTIES["cooldown_repeat"]

    importance = _clamp(data.get("importance"), 0, 25)
    relevance = _clamp(data.get("relevance"), 0, 25)
    actionability = _clamp(data.get("actionability"), 0, 10)
    discovery = _clamp(data.get("discovery"), 0, 5)
    total_score = max(0, importance + relevance + novelty_score + reliability + actionability
                       + discovery + penalty)

    route = _route_for(data["primary_type"], total_score, data.get("time_sensitive", False), status_changed)
    if route == "DROP":
        print(f"[digest_engine] дроп @{channel}: score={total_score}<45")
        _cleanup_item_images(item)
        return

    if route in ("NOW", "DIGEST", "WEEKLY"):
        # Полный пересказ — только теперь, когда точно известно, что пост дойдёт до читателя
        # (см. DIGEST_CLASSIFY_SYSTEM_PROMPT/DIGEST_REWRITE_SYSTEM_PROMPT выше).
        rewritten = _rewrite(rubric, channel, item)
        if rewritten is None:
            print(f"[digest_engine] дроп @{channel}: рерайт не удался")
            _cleanup_item_images(item)
            return
        data["title"] = rewritten.get("title", item.title)
        data["body"] = rewritten.get("body", item.text)

        # Раньше подстановка danger-фото и YandexART-фоллбэк (см. content/news.py) были
        # реализованы только для NOW-черновика (_build_draft_for_now) — DIGEST/WEEKLY
        # (основной объём реального контента) их вообще не получали, посты без своего фото
        # уходили в модерацию совсем без картинки. Перенесено сюда, в общую ветку — работает
        # одинаково для всех трёх маршрутов, ARCHIVE/DROP по-прежнему не тратят на это ни
        # копейки (см. else-ветку ниже и ранний return на DROP).
        if channel in DANGER_SOURCE_CHANNELS:
            matched = _danger_photo_for(f"{item.title} {item.text}")
            if matched:
                item.image_paths = matched
        item.image_paths = _ensure_image(rubric, data["title"], data["body"], item.image_paths)
    else:
        # ARCHIVE никогда не показывается читателю — дорогой пересказ не нужен, оставляем
        # исходный текст (пригодится, если понадобится вручную посмотреть архив при калибровке
        # порогов).
        data["title"] = item.title
        data["body"] = item.text

    if event_key and route in ("NOW", "DIGEST", "WEEKLY"):
        # "Опубликовано" здесь = "отобрано в пул, который читатель вот-вот увидит" — точный
        # момент реальной доставки (одобрение владельцем в модерации) отследить сложнее и не
        # принципиально для cooldown: раздел 18 просто не хочет второго поста о том же сюжете
        # слишком быстро, а не бухгалтерии по факту клика "Опубликовать".
        updated_event = get_event(event_key)
        mark_event_published(event_key, updated_event["known_facts"])

    candidate_fields = dict(
        source_channel=channel, rubric=rubric, raw_title=item.title, raw_text=item.text,
        url=item.url, image_paths=item.image_paths, primary_type=data["primary_type"],
        event_key=event_key, title=data["title"], body=data["body"],
        needs_manual_review=data.get("needs_manual_review", False),
        is_advertisement=False, importance=importance, relevance=relevance, novelty=novelty_score,
        reliability=reliability, actionability=actionability, discovery=discovery,
        penalty=penalty, total_score=total_score, route=route, status="scored",
    )
    candidate_id = insert_candidate(**candidate_fields)
    if route == "ARCHIVE":
        _cleanup_item_images(item)  # ARCHIVE никогда не уходит в модерацию — фото не нужны

    if route == "NOW":
        from moderation import ModerationBot  # локальный импорт — как и в content/news.py
        bot = ModerationBot()
        if bot.token and bot.owner_chat_id:
            draft = _build_draft_for_now(rubric, channel, item, data, candidate_id)
            bot.send_draft(draft)
            update_candidate_status(candidate_id, "sent_moderation", digest_slot="now")


def collect_candidates(fast_only: bool = False) -> None:
    targets = _channels_to_poll(fast_only)
    limit = FAST_POLL_LIMIT if fast_only else NORMAL_POLL_LIMIT
    print(f"[digest_engine] опрос {'алертных' if fast_only else 'всех'} каналов: {len(targets)} источников")
    for i, (rubric, channel, adapter) in enumerate(targets):
        if i > 0:
            # небольшая случайная пауза между каналами — подряд идущие Telethon-запросы
            # без пауз выглядят как скрейпер сильнее, чем сам факт периодичности опроса
            # (см. обсуждение 2026-08-24 про риск повторной блокировки наблюдателя)
            time.sleep(random.uniform(1.5, 4.5))
        try:
            items = _fetch_new_items(channel, adapter, limit)
        except Exception as e:
            print(f"[digest_engine] не смог опросить @{channel}: {e}")
            continue
        for item in items:
            try:
                _process_item(rubric, channel, item)
            except Exception as e:
                print(f"[digest_engine] ошибка обработки поста @{channel}: {e}")

    HEARTBEAT_FILE.write_text(datetime.now().isoformat())
