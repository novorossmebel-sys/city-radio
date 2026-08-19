"""Авто-продолжение и эскалация при отклонении черновика на модерации.

Правило (согласовано с владельцем 2026-08-15): если черновик рубрики отклонён — тут же
отправляем следующий черновик из той же рубрики (следующий источник по кругу, см. `rotate`
в sources.yaml). После 3 отклонений подряд в одной рубрике — рубрика приостанавливается до
10:00 следующего дня, чтобы не заваливать владельца плохими подборками из одного захода.
Правило действует по рубрикам независимо: стрик и пауза одной рубрики не влияют на другие.
Одобрение (или правка) сбрасывает счётчик отклонений этой рубрики.

Состояние (индекс ротации, стрик, пауза) — в rotation_state.json (гитигнор, как и
moderation_state.json), по ключу — название рубрики (draft.rubric), как оно и хранится в Draft.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from content.news import RawItem, TelegramChannelSource, VkGroupSource, YandexPogodaSource, AfishaRuSource

STATE_FILE = Path("rotation_state.json")
SOURCES_FILE = Path("sources.yaml")
REJECT_LIMIT = 3
PAUSE_UNTIL_HOUR = 10


def _load_sources_yaml() -> dict:
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_rubric_spec(rubric_title: str) -> dict | None:
    for spec in _load_sources_yaml().values():
        if spec.get("title") == rubric_title:
            return spec
    return None


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_paused(rubric_title: str) -> bool:
    entry = _load_state().get(rubric_title, {})
    paused_until = entry.get("paused_until")
    if not paused_until:
        return False
    return datetime.fromisoformat(paused_until) > datetime.now()


def record_reject(rubric_title: str) -> bool:
    """Увеличивает стрик отклонений подряд. Возвращает True, если рубрика только что
    ушла в паузу (этим отклонением стрик достиг лимита)."""
    state = _load_state()
    entry = state.setdefault(rubric_title, {"streak": 0, "paused_until": None, "index": 0})
    entry["streak"] = entry.get("streak", 0) + 1

    just_paused = False
    if entry["streak"] >= REJECT_LIMIT:
        tomorrow_10 = (datetime.now() + timedelta(days=1)).replace(
            hour=PAUSE_UNTIL_HOUR, minute=0, second=0, microsecond=0
        )
        entry["paused_until"] = tomorrow_10.isoformat()
        entry["streak"] = 0
        just_paused = True

    state[rubric_title] = entry
    _save_state(state)
    return just_paused


def record_resolved(rubric_title: str) -> None:
    """Сбрасывает стрик отклонений при не-отклоняющем исходе (одобрение и т.п.)."""
    state = _load_state()
    if rubric_title in state:
        state[rubric_title]["streak"] = 0
        _save_state(state)


def _next_source(spec: dict, index: int):
    """Источник рубрики по кругу: (адаптер или None, следующий индекс)."""
    source_type = spec.get("source_type")
    raw_source = spec.get("source")
    sources_list = raw_source if isinstance(raw_source, list) else [raw_source]
    if not sources_list or sources_list[0] is None:
        return None, index

    picked = sources_list[index % len(sources_list)]
    next_index = index + 1

    # mixed-рубрики кодируют природу элемента прямо в строке: "telegram:xxx", "web:xxx"
    if isinstance(picked, str) and ":" in picked and picked.split(":", 1)[0] in ("telegram", "web", "vk"):
        kind, name = picked.split(":", 1)
    else:
        kind, name = source_type, picked

    if kind == "telegram":
        return TelegramChannelSource(name), next_index
    if kind == "vk":
        return VkGroupSource(name), next_index
    if kind == "web" and "yandex.ru/pogoda" in name:
        return YandexPogodaSource(name), next_index
    if kind == "web" and "afisha.ru" in name:
        return AfishaRuSource(name), next_index
    # rss/остальные web/api/manual — для авто-подбора следующего поста пока не реализовано
    return None, next_index


def get_next_item(rubric_title: str) -> RawItem | None:
    """Следующий «сырой» пост для рубрики (следующий источник по кругу, см. rotate)."""
    spec = _find_rubric_spec(rubric_title)
    if spec is None:
        return None

    state = _load_state()
    entry = state.setdefault(rubric_title, {"streak": 0, "paused_until": None, "index": 0})
    index = entry.get("index", 0)

    adapter, next_index = _next_source(spec, index)
    entry["index"] = next_index
    state[rubric_title] = entry
    _save_state(state)

    if adapter is None:
        return None

    items = adapter.fetch(limit=5)
    return items[0] if items else None
