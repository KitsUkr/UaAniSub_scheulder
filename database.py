"""Шар БД на aiosqlite: запланований випуск серії.

Одна таблиця releases — матеріали одного випуску (превʼю + MP4 + MKV) та час
публікації. Планувальник щохвилини бере «дозрілі» рядки й публікує їх.
"""

import logging
import os

import aiosqlite

from config import DB_PATH

logger = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None


_SCHEMA = """
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


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("База даних не ініціалізована (виклич init_db).")
    return _db


async def create_release(
    *,
    preview_file_id: str,
    caption_html: str | None,
    mp4_file_id: str,
    mp4_kind: str,
    mkv_file_id: str,
    mkv_kind: str,
    run_at: str,
) -> int:
    cur = await _conn().execute(
        """
        INSERT INTO releases (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mkv_file_id, mkv_kind, run_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mkv_file_id, mkv_kind, run_at,
        ),
    )
    await _conn().commit()
    return cur.lastrowid


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


async def list_pending() -> list[dict]:
    cur = await _conn().execute(
        """
        SELECT id, caption_html, run_at
        FROM releases
        WHERE status = 'pending'
        ORDER BY run_at
        """,
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def cancel_release(release_id: int) -> bool:
    """Видаляє запланований (pending) випуск. True, якщо щось видалили."""
    cur = await _conn().execute(
        "DELETE FROM releases WHERE id = ? AND status = 'pending'",
        (release_id,),
    )
    await _conn().commit()
    return cur.rowcount > 0
