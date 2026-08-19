"""Сборка плановых выпусков (разделы 12-21 редакционной политики владельца, 2026-08-19):
Morning Radar (06:30), Evening Digest (19:00, юмор для него отбирается отдельно в 18:00),
Weekly (пятница). Кандидатов поставляет content/digest_engine.py в digest_store.

Отправка выпуска = одно заголовочное сообщение + bot.send_draft() на каждый отобранный
пункт — approve/reject/edit остаются как есть, ничего в moderation.py не меняется по
существу (кроме тонкой обёртки send_text, см. ModerationBot.send_text).

"Правило пустого выпуска" (раздел 20): если кандидатов меньше квоты слота — слот просто
не заполняется слабым контентом, вплоть до нуля материалов во всём выпуске.
"""
from content.base import Draft
from content.digest_store import fetch_pool, fetch_candidates, update_candidate_status
from content.llm import call_yandexgpt, extract_json

LOCAL_TYPES = {"LOCAL_ALERT", "LOCAL_STATUS", "LOCAL_UTILITY", "LOCAL_EVENT"}
GENERAL_TYPES = {"GENERAL_NEWS", "NEWS_UPDATE"}
CINEMA_FACT_TYPES = {"CINEMA_RELEASE", "CINEMA_TRAILER", "CINEMA_CASTING"}
CINEMA_OPINION_TYPES = {"CINEMA_REVIEW", "CINEMA_ANALYSIS"}
FOOD_DISCOVERY_TYPES = {"RECIPE", "FOOD_SCIENCE", "FOOD_DISCOVERY", "DISCOVERY"}
ANALYSIS_TYPES = {"ANALYSIS"}

WEEKLY_MAX_ITEMS = 12
WEEKLY_TYPE_CAP = 3  # не более 3 материалов одного primary_type — простая замена "8 рубрик"

CINEMA_SYNTHESIS_PROMPT = """Ты сводишь несколько мнений о фильме/сериале в одну карточку
для читателя, который решает, смотреть или нет. Не пересказывай каждый обзор — синтезируй.

Ответ — ТОЛЬКО валидный JSON: {"for": "...", "against": "...", "consensus": "...",
"verdict": "попробовать | на любителя | пропустить"}"""

HUMOR_SELECT_PROMPT = """Ты выбираешь ОДИН лучший пост для юмористического блока вечернего
выпуска среди присланного списка (или ни одного, если все слабые). Критерии (раздел 17
политики владельца):
- НЕ трагедия, НЕ тяжёлое ЧП, НЕ унижение конкретной жертвы.
- НЕ просто очередная вирусная новость без юмористической подачи.
- Желательно визуально или концептуально смешно.
- Если ни один пункт не подходит — верни null, не публикуй слабое ради галочки.

Список пронумерован. Ответ — ТОЛЬКО валидный JSON: {"pick": null или номер пункта (int)}"""


def _draft_from_dict(d: dict) -> Draft:
    return Draft(
        rubric=d["rubric"], source_name=d["source_name"], source_url=d.get("url", ""),
        original_title=d.get("raw_title", ""), original_text=d.get("raw_text", ""),
        title=d["title"], body=d["body"], concerns=d.get("concerns", []),
        needs_manual_review=bool(d.get("needs_manual_review", False)),
        image_paths=d.get("image_paths", []),
    )


def _pick_top(pool: list[dict], used_ids: set, n: int, predicate=None) -> list[dict]:
    picked = []
    for c in pool:
        if c["id"] in used_ids:
            continue
        if predicate and not predicate(c):
            continue
        picked.append(c)
        if len(picked) >= n:
            break
    return picked


def _group_by_entity(candidates: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        if not c.get("event_key"):
            continue
        entity = c["event_key"].split("|", 1)[0]
        groups.setdefault(entity, []).append(c)
    return groups


def _synthesize_cinema_verdict(entity: str, opinions: list[dict]) -> dict:
    joined = "\n\n".join(f"[{o['source_channel']}] {o['body']}" for o in opinions[:6])
    try:
        raw = call_yandexgpt(CINEMA_SYNTHESIS_PROMPT, f"Фильм/сериал: {entity}\n\nМнения:\n{joined}")
        data = extract_json(raw)
    except Exception as e:
        print(f"[digest_compose] синтез мнений о «{entity}» не удался: {e}")
        data = {}
    body = (
        f"<b>За:</b> {data.get('for', '—')}\n\n"
        f"<b>Против:</b> {data.get('against', '—')}\n\n"
        f"<b>Консенсус:</b> {data.get('consensus', '—')}\n\n"
        f"<b>Вердикт:</b> {data.get('verdict', '—')}"
    )
    return {
        "rubric": opinions[0]["rubric"],
        "source_name": "; ".join(sorted({f"@{o['source_channel']}" for o in opinions})),
        "url": opinions[0].get("url", ""),
        "raw_title": entity, "raw_text": joined,
        "title": f"⭐ Стоит смотреть «{entity}»?", "body": body,
        "concerns": [], "needs_manual_review": False, "image_paths": [],
    }


def _pick_cinema_opinion(pool: list[dict], used_ids: set) -> tuple[dict | None, list[int]]:
    """Раздел 14: если по одному фильму высказались ≥2 opinion-источника — синтез
    "За/Против/Консенсус/Вердикт" вместо публикации каждого мнения отдельно."""
    candidates = [c for c in pool if c["id"] not in used_ids and c["primary_type"] in CINEMA_OPINION_TYPES]
    if not candidates:
        return None, []
    groups = _group_by_entity(candidates)
    if groups:
        entity, group = max(groups.items(), key=lambda kv: (len(kv[1]), kv[1][0]["total_score"]))
        if len(group) >= 2:
            return _synthesize_cinema_verdict(entity, group), [c["id"] for c in group]
    top = candidates[0]
    return dict(top, source_name=f"@{top['source_channel']}"), [top["id"]]


def _dispatch_digest(header: str, items: list[tuple[dict, list[int]]], slot: str) -> None:
    if not items:
        print(f"[digest_compose] «{header}»: кандидатов не нашлось, выпуск пропущен (правило пустого выпуска)")
        return

    from moderation import ModerationBot
    bot = ModerationBot()
    if not (bot.token and bot.owner_chat_id):
        print("[digest_compose] TELEGRAM_BOT_TOKEN / OWNER_TELEGRAM_CHAT_ID не заданы — отправлять некуда")
        return

    bot.send_text(bot.owner_chat_id, f"{header} — {len(items)} материал(ов)")
    for draft_source, ids in items:
        draft = _draft_from_dict(draft_source)
        bot.send_draft(draft)
        for cid in ids:
            update_candidate_status(cid, "sent_moderation", digest_slot=slot)
    print(f"[digest_compose] «{header}»: отправлено {len(items)} материал(ов) на модерацию")


def compose_morning_radar() -> None:
    """06:30 — раздел 19: 2 local practical / 1 major general / 1 watchlist-кино /
    1 "сегодня следим". Целевое чтение 2-3 минуты — поэтому квоты маленькие."""
    pool = fetch_pool("DIGEST")
    used_ids: set = set()
    items: list[tuple[dict, list[int]]] = []

    local = _pick_top(pool, used_ids, 2, lambda c: c["primary_type"] in LOCAL_TYPES)
    used_ids |= {c["id"] for c in local}
    general = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in GENERAL_TYPES)
    used_ids |= {c["id"] for c in general}
    cinema = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in CINEMA_FACT_TYPES | CINEMA_OPINION_TYPES)
    used_ids |= {c["id"] for c in cinema}
    followup = _pick_top(pool, used_ids, 1)  # "сегодня следим" — любой оставшийся топ-кандидат
    used_ids |= {c["id"] for c in followup}

    for c in local + general + cinema + followup:
        items.append((dict(c, source_name=f"@{c['source_channel']}"), [c["id"]]))

    _dispatch_digest("🌅 Утренний дайджест", items, "morning")


def select_humor() -> None:
    """18:00 — раздел 17: отдельный silo (HUMOR_POOL), максимум 1 материал, свои критерии
    (не трагедия/не унижение жертвы/действительно смешно). Отобранный пункт помечается
    queued_evening и подхватывается compose_evening_digest часом позже."""
    pool = fetch_candidates(route="HUMOR_POOL", status="scored")
    if not pool:
        print("[digest_compose] юмор: пул пуст, пропуск")
        return

    listing = "\n".join(
        f"{i + 1}. [{c['source_channel']}] {c['raw_title']}: {c['raw_text'][:300]}"
        for i, c in enumerate(pool[:20])
    )
    try:
        raw = call_yandexgpt(HUMOR_SELECT_PROMPT, listing)
        data = extract_json(raw)
    except Exception as e:
        print(f"[digest_compose] выбор юмора не удался: {e}")
        return

    pick = data.get("pick")
    if not isinstance(pick, int) or not (1 <= pick <= len(pool[:20])):
        print("[digest_compose] юмор: ничего не выбрано (слабый материал или explicit null)")
        return

    chosen = pool[pick - 1]
    update_candidate_status(chosen["id"], "queued_evening")
    print(f"[digest_compose] юмор: выбран пост из @{chosen['source_channel']}")


def compose_evening_digest() -> None:
    """19:00 — раздел 19: 2 главное / 1 Новороссийск / 1 кино-факт / 1 кино-рекомендация
    (синтез мнений, раздел 14) / 1 food-health-discovery / 1 аналитика / 1 юмор (опционально,
    из select_humor в 18:00). 4 хороших материала лучше 8 слабых — квоты не форсируются."""
    pool = fetch_pool("DIGEST")
    used_ids: set = set()
    items: list[tuple[dict, list[int]]] = []

    main = _pick_top(pool, used_ids, 2)  # "2 главное" — топ-2 вообще без ограничения по типу
    used_ids |= {c["id"] for c in main}
    local = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in LOCAL_TYPES)
    used_ids |= {c["id"] for c in local}
    cinema_fact = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in CINEMA_FACT_TYPES)
    used_ids |= {c["id"] for c in cinema_fact}
    cinema_opinion, opinion_ids = _pick_cinema_opinion(pool, used_ids)
    used_ids |= set(opinion_ids)
    food = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in FOOD_DISCOVERY_TYPES)
    used_ids |= {c["id"] for c in food}
    analysis = _pick_top(pool, used_ids, 1, lambda c: c["primary_type"] in ANALYSIS_TYPES)
    used_ids |= {c["id"] for c in analysis}

    for c in main + local + cinema_fact + food + analysis:
        items.append((dict(c, source_name=f"@{c['source_channel']}"), [c["id"]]))
    if cinema_opinion:
        items.append((cinema_opinion, opinion_ids))

    humor = fetch_candidates(route="HUMOR_POOL", status="queued_evening")
    if humor:
        c = humor[0]
        items.append((dict(c, source_name=f"@{c['source_channel']}"), [c["id"]]))

    _dispatch_digest("🌆 Вечерний дайджест", items, "evening")


def compose_weekly() -> None:
    """Пятница — раздел 19: до 12 материалов. Вместо строгого распределения по 8 именным
    рубрикам документа (Что реально изменилось/Лучшее кино/... — это скорее стиль подачи,
    чем механическая квота) — топ по score с мягким ограничением на разнообразие типов,
    не более WEEKLY_TYPE_CAP одного primary_type подряд."""
    pool = fetch_pool("WEEKLY")
    items: list[tuple[dict, list[int]]] = []
    type_counts: dict[str, int] = {}

    for c in pool:
        if len(items) >= WEEKLY_MAX_ITEMS:
            break
        if type_counts.get(c["primary_type"], 0) >= WEEKLY_TYPE_CAP:
            continue
        items.append((dict(c, source_name=f"@{c['source_channel']}"), [c["id"]]))
        type_counts[c["primary_type"]] = type_counts.get(c["primary_type"], 0) + 1

    _dispatch_digest("🔬 Еженедельный обзор", items, "weekly")
