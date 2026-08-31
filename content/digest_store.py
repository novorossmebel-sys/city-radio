"""Хранилище дайджест-движка — SQLite (digest.db, gitignored как media_cache/).

До этого модуля в проекте не было персистентного состояния кроме простых JSON-файлов
(moderation_state.json, rotation_state.json) — для них хватало "весь файл целиком".
Дайджест-движку нужны запросы вида "топ-N нескормленных кандидатов рубрики X, не в
cooldown, отсортированные по score" — на JSON-файле с потенциально тысячами строк в день
это означало бы каждый раз читать и фильтровать всё в Python; SQLite — тот же
"один файл рядом с проектом", но с нормальными WHERE/ORDER BY. Не ORM — намеренно, схема
небольшая и меняется редко.

Таблицы:
  candidates      — каждый пост, прошедший pre-filter и классификацию (см. digest_engine.py).
  events          — память сюжета: known_facts/last_published_facts/статус (раздел 7,
                     статус-машина БПЛА — раздел 12 редакционной политики владельца, 2026-08-19).
  channel_cursors — что уже видели в каждом канале, чтобы collect_candidates() не
                     перечитывал одно и то же при каждом опросе.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("digest.db")

# Cooldown по типу события (раздел 18 политики) — часы; RECIPE меряется в днях (7*24).
COOLDOWN_HOURS = {
    "default": 6,
    "CINEMA_RELEASE": 24, "CINEMA_TRAILER": 24, "CINEMA_CASTING": 24,
    "CINEMA_REVIEW": 24, "CINEMA_ANALYSIS": 24,
    "RECIPE": 7 * 24,
    "ANALYSIS": 48,
}


def _connect() -> sqlite3.Connection:
    # timeout=30 (вместо дефолтных 5с) + WAL — планировщик дёргает эту БД из двух параллельных
    # задач (быстрый и обычный опрос каналов, см. scheduler.py), а проект лежит в папке
    # OneDrive, который может кратковременно держать файл во время синхронизации — без этого
    # ловили "database is locked" и молча пропускали опрос алертных каналов (2026-08-19).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_channel TEXT NOT NULL,
                rubric TEXT NOT NULL,
                raw_title TEXT,
                raw_text TEXT,
                url TEXT,
                image_paths TEXT DEFAULT '[]',
                primary_type TEXT,
                event_key TEXT,
                title TEXT,
                body TEXT,
                concerns TEXT DEFAULT '[]',
                needs_manual_review INTEGER DEFAULT 0,
                is_advertisement INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 0,
                relevance INTEGER DEFAULT 0,
                novelty INTEGER DEFAULT 0,
                reliability INTEGER DEFAULT 0,
                actionability INTEGER DEFAULT 0,
                discovery INTEGER DEFAULT 0,
                penalty INTEGER DEFAULT 0,
                total_score INTEGER DEFAULT 0,
                route TEXT DEFAULT 'ARCHIVE',
                status TEXT DEFAULT 'new',
                fetched_at TEXT NOT NULL,
                digest_slot TEXT,
                published_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_route_status ON candidates(route, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_event_key ON candidates(event_key)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                category TEXT,
                status TEXT,
                known_facts TEXT DEFAULT '[]',
                last_published_facts TEXT DEFAULT '[]',
                sources TEXT DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_update_at TEXT NOT NULL,
                last_published_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_cursors (
                channel TEXT PRIMARY KEY,
                last_message_id INTEGER,
                updated_at TEXT NOT NULL
            )
        """)

        # Последовательная модерация вечернего/недельного дайджеста (2026-08-25, по просьбе
        # владельца): вместо того чтобы слать все карточки выпуска разом, digest_compose.py
        # кладёт их сюда одним списком и шлёт только первую; после approve/reject
        # moderation.py присылает следующую с задержкой (см. QUEUE_ADVANCE_DELAY там же).
        # Каждый процесс (планировщик и бот-модератор) — отдельный процесс, поэтому очередь
        # не может жить в памяти одного из них — нужен тот же общий SQLite, что и для candidates.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digest_queues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot TEXT NOT NULL,
                items_json TEXT NOT NULL,
                cursor INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        # Личный обучающий слой (последний раздел политики владельца, 2026-08-19) — реакции
        # 👍/🔥/👎/♻️ на карточке модерации ПОСЛЕ публикации. Пока только сбор сырых данных:
        # автоматическая перестройка весов рубрик/источников по ним — отдельная будущая работа,
        # владелец сам предложил сначала 2-3 недели просто накапливать реакции.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rubric TEXT,
                source_name TEXT,
                title TEXT,
                published_at TEXT NOT NULL,
                reaction TEXT
            )
        """)


def _now() -> str:
    return datetime.now().isoformat()


# --- channel_cursors ---

def get_cursor(channel: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_message_id FROM channel_cursors WHERE channel = ?", (channel,)
        ).fetchone()
        return row["last_message_id"] if row else None


def set_cursor(channel: str, last_message_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO channel_cursors (channel, last_message_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET last_message_id = excluded.last_message_id, "
            "updated_at = excluded.updated_at",
            (channel, last_message_id, _now()),
        )


# --- candidates ---

def insert_candidate(**fields) -> int:
    """fields может содержать любое подмножество колонок candidates (кроме id/fetched_at,
    которые проставляются здесь). Списки (image_paths/concerns) передаются как Python-списки,
    сериализуются в JSON автоматически."""
    fields.setdefault("fetched_at", _now())
    for list_field in ("image_paths", "concerns"):
        if list_field in fields and not isinstance(fields[list_field], str):
            fields[list_field] = json.dumps(fields[list_field], ensure_ascii=False)

    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    with _connect() as conn:
        cur = conn.execute(
            f"INSERT INTO candidates ({', '.join(columns)}) VALUES ({placeholders})",
            [fields[c] for c in columns],
        )
        return cur.lastrowid


def update_candidate_status(candidate_id: int, status: str, digest_slot: str | None = None) -> None:
    with _connect() as conn:
        if digest_slot is not None:
            conn.execute(
                "UPDATE candidates SET status = ?, digest_slot = ?, published_at = ? WHERE id = ?",
                (status, digest_slot, _now() if status == "published" else None, candidate_id),
            )
        else:
            conn.execute("UPDATE candidates SET status = ? WHERE id = ?", (status, candidate_id))


def update_candidate_content(candidate_id: int, title: str, body: str, image_paths: list,
                              needs_manual_review: bool = False, concerns: list | None = None) -> None:
    """Персистит донабранный пересказ+фото WEEKLY-кандидата (см.
    digest_engine.finalize_weekly_candidate) — WEEKLY откладывает дорогой пересказ до
    момента реального отбора в выпуск, эта функция записывает результат обратно, чтобы
    он не потерялся вместе с процессом планировщика до следующего опроса."""
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET title = ?, body = ?, image_paths = ?, needs_manual_review = ?, "
            "concerns = ? WHERE id = ?",
            (title, body, json.dumps(image_paths, ensure_ascii=False), int(needs_manual_review),
             json.dumps(concerns or [], ensure_ascii=False), candidate_id),
        )


def _candidate_from_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["image_paths"] = json.loads(d.get("image_paths") or "[]")
    d["concerns"] = json.loads(d.get("concerns") or "[]")
    return d


def fetch_candidates(route: str | None = None, status: str | None = None) -> list[dict]:
    """Более общий запрос, чем fetch_pool — без исключений по статусу (нужен, например,
    чтобы найти конкретно кандидата в статусе queued_evening для юмористического слота)."""
    conditions, params = [], []
    if route is not None:
        conditions.append("route = ?")
        params.append(route)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM candidates {where} ORDER BY total_score DESC", params).fetchall()
    return [_candidate_from_row(r) for r in rows]


def fetch_pool(route: str, exclude_statuses=("sent_moderation", "published", "dropped"),
               limit: int | None = None) -> list[dict]:
    """Кандидаты заданного route (DIGEST/WEEKLY/NOW), ещё не использованные и не в
    cooldown (cooldown проверяется отдельно через is_in_cooldown при отборе слотов —
    здесь просто сортировка по score, фильтрация по cooldown требует знания event_key
    и категории, поэтому остаётся на стороне digest_compose.py)."""
    placeholders = ", ".join("?" for _ in exclude_statuses)
    query = f"""
        SELECT * FROM candidates
        WHERE route = ? AND status NOT IN ({placeholders})
        ORDER BY total_score DESC, fetched_at ASC
    """
    params = [route, *exclude_statuses]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_candidate_from_row(r) for r in rows]


# --- events ---

def get_event(event_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM events WHERE event_key = ?", (event_key,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["known_facts"] = json.loads(d.get("known_facts") or "[]")
    d["last_published_facts"] = json.loads(d.get("last_published_facts") or "[]")
    d["sources"] = json.loads(d.get("sources") or "[]")
    return d


def upsert_event(event_key: str, category: str, status: str, new_fact: str, source: str) -> None:
    """Создаёт событие или дописывает новый факт к known_facts существующего.
    last_published_facts НЕ трогает — это апдейтится отдельно, в момент реальной
    публикации (mark_event_published), а не при каждом новом кандидате."""
    existing = get_event(event_key)
    now = _now()
    with _connect() as conn:
        if existing is None:
            conn.execute(
                "INSERT INTO events (event_key, category, status, known_facts, sources, "
                "first_seen_at, last_update_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_key, category, status, json.dumps([new_fact], ensure_ascii=False),
                 json.dumps([source], ensure_ascii=False), now, now),
            )
        else:
            known_facts = existing["known_facts"] + [new_fact]
            sources = list(dict.fromkeys(existing["sources"] + [source]))  # без дублей, порядок сохранён
            conn.execute(
                "UPDATE events SET status = ?, known_facts = ?, sources = ?, last_update_at = ? "
                "WHERE event_key = ?",
                (status, json.dumps(known_facts, ensure_ascii=False),
                 json.dumps(sources, ensure_ascii=False), now, event_key),
            )


def mark_event_published(event_key: str, published_facts: list) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE events SET last_published_facts = ?, last_published_at = ? WHERE event_key = ?",
            (json.dumps(published_facts, ensure_ascii=False), _now(), event_key),
        )


def is_in_cooldown(event_key: str, primary_type: str) -> bool:
    event = get_event(event_key)
    if event is None or not event["last_published_at"]:
        return False
    hours = COOLDOWN_HOURS.get(primary_type, COOLDOWN_HOURS["default"])
    last_published = datetime.fromisoformat(event["last_published_at"])
    return datetime.now() < last_published + timedelta(hours=hours)


# --- digest_queues (последовательная модерация вечернего/недельного дайджеста) ---

def create_digest_queue(slot: str, items: list[dict]) -> int:
    """items — [{"draft": <словарь кандидата для draft_from_candidate_dict>,
    "candidate_ids": [...]}] в том порядке, в котором их нужно присылать."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO digest_queues (slot, items_json, cursor, created_at) VALUES (?, ?, 0, ?)",
            (slot, json.dumps(items, ensure_ascii=False), _now()),
        )
        return cur.lastrowid


def get_digest_queue(queue_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM digest_queues WHERE id = ?", (queue_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["items"] = json.loads(d["items_json"])
    return d


def advance_digest_queue(queue_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE digest_queues SET cursor = cursor + 1 WHERE id = ?", (queue_id,))


# --- feedback (личный обучающий слой) ---

def log_published(rubric: str, source_name: str, title: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO feedback (rubric, source_name, title, published_at) VALUES (?, ?, ?, ?)",
            (rubric, source_name, title, _now()),
        )
        return cur.lastrowid


def record_reaction(feedback_id: int, reaction: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE feedback SET reaction = ? WHERE id = ?", (reaction, feedback_id))


init_db()  # таблицы создаются при первом импорте модуля — не нужно помнить вызвать вручную
