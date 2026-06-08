"""Шар БД на aiosqlite: шаблони аніме + заплановані випуски серій.

Дві таблиці:
  templates — аніме: назва, день тижня, час, к-сть серій, дата 1-ї серії.
  releases  — матеріали одного випуску (превʼю + MP4 + MKV) і час публікації;
              привʼязані до слота шаблону через (template_id, episode_no).
Планувальник щохвилини бере «дозрілі» releases і публікує їх.
"""

import logging
import os

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    weekday         INTEGER NOT NULL,           -- 0=Пн .. 6=Нд
    send_time       TEXT NOT NULL,              -- 'HH:MM'
    episodes_count  INTEGER NOT NULL,
    start_date      TEXT NOT NULL,              -- 'YYYY-MM-DD' (дата серії 1)
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY,
    preview_file_id TEXT NOT NULL,
    caption_html    TEXT,
    mp4_file_id     TEXT NOT NULL,
    mp4_kind        TEXT NOT NULL,
    mkv_file_id     TEXT NOT NULL,
    mkv_kind        TEXT NOT NULL,
    run_at          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    template_id     INTEGER,
    episode_no      INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_releases_due
    ON releases (status, run_at);
"""


async def init_db() -> None:
    global _db
    # SQLite не створює відсутніх тек — створюємо батьківську теку самі
    # (важливо для шляхів типу /app/data/releases.db на хостингу з томом).
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _db.commit()
    await _migrate()


async def _migrate() -> None:
    """Доганяє схему для вже створених БД: додає нові колонки releases, якщо їх нема."""
    cur = await _conn().execute("PRAGMA table_info(releases)")
    cols = {row["name"] for row in await cur.fetchall()}
    for col, decl in (("template_id", "INTEGER"), ("episode_no", "INTEGER")):
        if col not in cols:
            await _conn().execute(f"ALTER TABLE releases ADD COLUMN {col} {decl}")
    # Індекс по слоту створюємо тут — після того, як колонки гарантовано є
    # (на старій БД їх ще не було під час executescript).
    await _conn().execute(
        "CREATE INDEX IF NOT EXISTS idx_releases_slot ON releases (template_id, episode_no)"
    )
    await _conn().commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("База даних не ініціалізована (виклич init_db).")
    return _db


# ── Шаблони аніме ─────────────────────────────────────────────────────────────

async def create_template(
    *,
    name: str,
    weekday: int,
    send_time: str,
    episodes_count: int,
    start_date: str,
) -> int:
    cur = await _conn().execute(
        """
        INSERT INTO templates (name, weekday, send_time, episodes_count, start_date)
        VALUES (?, ?, ?, ?, ?)
        """,
        (name, weekday, send_time, episodes_count, start_date),
    )
    await _conn().commit()
    return cur.lastrowid


async def list_templates() -> list[dict]:
    cur = await _conn().execute(
        """
        SELECT id, name, weekday, send_time, episodes_count, start_date
        FROM templates
        ORDER BY created_at
        """,
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_template(template_id: int) -> dict | None:
    cur = await _conn().execute(
        """
        SELECT id, name, weekday, send_time, episodes_count, start_date
        FROM templates WHERE id = ?
        """,
        (template_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def delete_template(template_id: int) -> bool:
    """Видаляє шаблон і його ще не опубліковані (pending) релізи.
    Опубліковані релізи лишаються в історії. True, якщо шаблон існував."""
    await _conn().execute(
        "DELETE FROM releases WHERE template_id = ? AND status = 'pending'",
        (template_id,),
    )
    cur = await _conn().execute("DELETE FROM templates WHERE id = ?", (template_id,))
    await _conn().commit()
    return cur.rowcount > 0


# ── Випуски (releases) ────────────────────────────────────────────────────────

async def create_release(
    *,
    preview_file_id: str,
    caption_html: str | None,
    mp4_file_id: str,
    mp4_kind: str,
    mkv_file_id: str,
    mkv_kind: str,
    run_at: str,
    template_id: int | None = None,
    episode_no: int | None = None,
) -> int:
    cur = await _conn().execute(
        """
        INSERT INTO releases (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mkv_file_id, mkv_kind, run_at,
            template_id, episode_no
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mkv_file_id, mkv_kind, run_at,
            template_id, episode_no,
        ),
    )
    await _conn().commit()
    return cur.lastrowid


async def filled_episodes(template_id: int) -> set[int]:
    """Номери серій шаблону, що вже мають реліз (будь-якого статусу) — для ✅."""
    cur = await _conn().execute(
        "SELECT episode_no FROM releases WHERE template_id = ? AND episode_no IS NOT NULL",
        (template_id,),
    )
    return {row["episode_no"] for row in await cur.fetchall()}


async def get_release_by_slot(template_id: int, episode_no: int) -> dict | None:
    cur = await _conn().execute(
        """
        SELECT id, run_at, status, caption_html
        FROM releases
        WHERE template_id = ? AND episode_no = ?
        ORDER BY id DESC LIMIT 1
        """,
        (template_id, episode_no),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def due_releases(now_str: str) -> list[dict]:
    """Заплановані випуски, час яких уже настав (run_at <= зараз, Київ).

    Порівняння рядків 'YYYY-MM-DD HH:MM' з нулями зліва = хронологічне.
    `<=` (а не `==`) гарантує, що випуск не загубиться після простою бота.
    """
    cur = await _conn().execute(
        """
        SELECT id, preview_file_id, caption_html,
               mp4_file_id, mp4_kind, mkv_file_id, mkv_kind, run_at
        FROM releases
        WHERE status = 'pending' AND run_at <= ?
        ORDER BY run_at
        """,
        (now_str,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_sent(release_id: int) -> None:
    await _conn().execute(
        "UPDATE releases SET status = 'sent', sent_at = datetime('now') WHERE id = ?",
        (release_id,),
    )
    await _conn().commit()


async def mark_failed(release_id: int) -> None:
    await _conn().execute(
        "UPDATE releases SET status = 'failed' WHERE id = ?",
        (release_id,),
    )
    await _conn().commit()


async def cancel_release(release_id: int) -> bool:
    """Видаляє запланований (pending) випуск. True, якщо щось видалили."""
    cur = await _conn().execute(
        "DELETE FROM releases WHERE id = ? AND status = 'pending'",
        (release_id,),
    )
    await _conn().commit()
    return cur.rowcount > 0
