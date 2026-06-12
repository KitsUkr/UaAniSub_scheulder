"""Шар БД на aiosqlite: шаблони аніме + заплановані випуски серій.

Дві таблиці:
  templates — аніме: назва, день тижня, час, к-сть серій, дата 1-ї серії.
  releases  — матеріали одного випуску (превʼю + MP4 + MKV) і час публікації;
              привʼязані до слота шаблону через (template_id, episode_no).
Планувальник щохвилини бере «дозрілі» releases і публікує їх.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from config import DB_PATH, TZ

logger = logging.getLogger(__name__)

_KYIV = ZoneInfo(TZ)


def _now() -> str:
    """Поточний київський час — у БД всі мітки (run_at, created_at, sent_at)
    в одному поясі, інакше datetime('now') писав би UTC."""
    return datetime.now(_KYIV).strftime("%Y-%m-%d %H:%M:%S")

_db: aiosqlite.Connection | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    weekday         INTEGER NOT NULL,           -- 0=Пн .. 6=Нд
    send_time       TEXT NOT NULL,              -- 'HH:MM'
    episodes_count  INTEGER NOT NULL,
    start_date      TEXT NOT NULL,              -- 'YYYY-MM-DD' (дата серії 1)
    manual_done     INTEGER NOT NULL DEFAULT 1, -- скільки перших серій уже викладено вручну
    has_zero        INTEGER NOT NULL DEFAULT 0, -- чи є нульова серія (відлік з 0)
    first_day       INTEGER NOT NULL DEFAULT 1, -- скільки серій виходить у перший день
    repost_channel  TEXT,                       -- партнерський канал для репосту MP4 (NULL = пряма публікація)
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY,
    preview_file_id TEXT NOT NULL,
    caption_html    TEXT,
    mp4_file_id     TEXT NOT NULL,
    mp4_kind        TEXT NOT NULL,
    mp4_caption_html TEXT,
    mkv_file_id     TEXT NOT NULL,
    mkv_kind        TEXT NOT NULL,
    run_at          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    mp4_msg_id      INTEGER,                    -- id поста MP4 у release-каналі (вже відправлено)
    mkv_msg_id      INTEGER,                    -- id поста MKV у release-каналі (вже відправлено)
    -- При видаленні шаблону опубліковані релізи лишаються в історії без привʼязки.
    template_id     INTEGER REFERENCES templates(id) ON DELETE SET NULL,
    episode_no      INTEGER,
    created_at      TEXT DEFAULT (datetime('now')),
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_releases_due
    ON releases (status, run_at);

-- Стан FSM aiogram (майстри створення/заповнення/імпорту) — переживає рестарт.
CREATE TABLE IF NOT EXISTS fsm (
    key   TEXT PRIMARY KEY,             -- bot_id:chat_id:user_id
    state TEXT,
    data  TEXT NOT NULL DEFAULT '{}'
);
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
    # WAL: читання не блокується записом + краща стійкість до збоїв.
    await _db.execute("PRAGMA journal_mode=WAL")
    # FK діють для БД, створених із REFERENCES у схемі; старі БД без FK
    # прибирає вручну delete_template — поведінка та сама.
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.executescript(_SCHEMA)
    await _db.commit()
    await _migrate()


async def _migrate() -> None:
    """Доганяє схему для вже створених БД: додає нові колонки releases, якщо їх нема."""
    cur = await _conn().execute("PRAGMA table_info(releases)")
    cols = {row["name"] for row in await cur.fetchall()}
    for col, decl in (
        ("template_id", "INTEGER"),
        ("episode_no", "INTEGER"),
        ("mp4_caption_html", "TEXT"),
        ("mp4_msg_id", "INTEGER"),
        ("mkv_msg_id", "INTEGER"),
    ):
        if col not in cols:
            await _conn().execute(f"ALTER TABLE releases ADD COLUMN {col} {decl}")
    # Колонка templates.manual_done для вже створених БД.
    cur = await _conn().execute("PRAGMA table_info(templates)")
    tcols = {row["name"] for row in await cur.fetchall()}
    if "manual_done" not in tcols:
        await _conn().execute(
            "ALTER TABLE templates ADD COLUMN manual_done INTEGER NOT NULL DEFAULT 1"
        )
    if "has_zero" not in tcols:
        await _conn().execute(
            "ALTER TABLE templates ADD COLUMN has_zero INTEGER NOT NULL DEFAULT 0"
        )
    if "first_day" not in tcols:
        await _conn().execute(
            "ALTER TABLE templates ADD COLUMN first_day INTEGER NOT NULL DEFAULT 1"
        )
    if "repost_channel" not in tcols:
        await _conn().execute("ALTER TABLE templates ADD COLUMN repost_channel TEXT")
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
    manual_done: int = 1,
    has_zero: int = 0,
    first_day: int = 1,
) -> int:
    cur = await _conn().execute(
        """
        INSERT INTO templates
            (name, weekday, send_time, episodes_count, start_date,
             manual_done, has_zero, first_day, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, weekday, send_time, episodes_count, start_date,
         manual_done, has_zero, first_day, _now()),
    )
    await _conn().commit()
    return cur.lastrowid


async def list_templates() -> list[dict]:
    cur = await _conn().execute(
        """
        SELECT id, name, weekday, send_time, episodes_count, start_date, manual_done, has_zero, first_day, repost_channel
        FROM templates
        ORDER BY created_at
        """,
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_template(template_id: int) -> dict | None:
    cur = await _conn().execute(
        """
        SELECT id, name, weekday, send_time, episodes_count, start_date, manual_done, has_zero, first_day, repost_channel
        FROM templates WHERE id = ?
        """,
        (template_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_send_time(template_id: int, send_time: str) -> int:
    """Змінює час публікації аніме й переносить на новий час усі ще не
    опубліковані (pending) випуски. Повертає к-сть перенесених випусків.
    run_at має формат 'YYYY-MM-DD HH:MM' — дата лишається, час замінюється."""
    await _conn().execute(
        "UPDATE templates SET send_time = ? WHERE id = ?",
        (send_time, template_id),
    )
    cur = await _conn().execute(
        """
        UPDATE releases SET run_at = substr(run_at, 1, 10) || ' ' || ?
        WHERE template_id = ? AND status = 'pending'
        """,
        (send_time, template_id),
    )
    await _conn().commit()
    return cur.rowcount


async def set_repost_channel(template_id: int, channel: str | None) -> None:
    """Партнерський канал для репосту MP4 (NULL — вимкнути, пряма публікація)."""
    await _conn().execute(
        "UPDATE templates SET repost_channel = ? WHERE id = ?",
        (channel, template_id),
    )
    await _conn().commit()


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
    mp4_caption_html: str | None = None,
    template_id: int | None = None,
    episode_no: int | None = None,
) -> int:
    cur = await _conn().execute(
        """
        INSERT INTO releases (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mp4_caption_html, mkv_file_id, mkv_kind, run_at,
            template_id, episode_no, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mp4_caption_html, mkv_file_id, mkv_kind, run_at,
            template_id, episode_no, _now(),
        ),
    )
    await _conn().commit()
    return cur.lastrowid


async def create_releases_bulk(releases: list[dict]) -> None:
    """Створює багато релізів однією транзакцією (один commit замість N) —
    масовий імпорт атомарний: або всі серії, або жодної.
    Ключі словників — як іменовані аргументи create_release."""
    now = _now()
    await _conn().executemany(
        """
        INSERT INTO releases (
            preview_file_id, caption_html,
            mp4_file_id, mp4_kind, mp4_caption_html, mkv_file_id, mkv_kind, run_at,
            template_id, episode_no, created_at
        )
        VALUES (:preview_file_id, :caption_html, :mp4_file_id, :mp4_kind,
                :mp4_caption_html, :mkv_file_id, :mkv_kind, :run_at,
                :template_id, :episode_no, :created_at)
        """,
        [{**r, "created_at": now} for r in releases],
    )
    await _conn().commit()


async def episode_statuses(template_id: int) -> dict[int, str]:
    """Останній статус релізу для кожної серії шаблону: episode_no → status
    ('pending' — заплановано ⏳, 'sent' — опубліковано ✅, 'failed').
    Беремо MAX(id), бо «Замінити» лишає старий реліз і додає новий."""
    cur = await _conn().execute(
        """
        SELECT episode_no, status
        FROM releases r
        WHERE template_id = ? AND episode_no IS NOT NULL
          AND id = (
            SELECT MAX(id) FROM releases
            WHERE template_id = r.template_id AND episode_no = r.episode_no
          )
        """,
        (template_id,),
    )
    return {row["episode_no"]: row["status"] for row in await cur.fetchall()}


async def episode_statuses_all() -> dict[int, dict[int, str]]:
    """Як episode_statuses, але для всіх шаблонів одним запитом:
    template_id → {episode_no → status}. Для списку аніме (без N+1)."""
    cur = await _conn().execute(
        """
        SELECT template_id, episode_no, status
        FROM releases r
        WHERE template_id IS NOT NULL AND episode_no IS NOT NULL
          AND id = (
            SELECT MAX(id) FROM releases
            WHERE template_id = r.template_id AND episode_no = r.episode_no
          )
        """,
    )
    result: dict[int, dict[int, str]] = {}
    for row in await cur.fetchall():
        result.setdefault(row["template_id"], {})[row["episode_no"]] = row["status"]
    return result


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
    'sending' теж беремо: це випуски, на яких бот упав посеред публікації —
    msg_id уже відправлених кроків збережені, тож продовжимо без дублів.
    """
    cur = await _conn().execute(
        """
        SELECT r.id, r.preview_file_id, r.caption_html,
               r.mp4_file_id, r.mp4_kind, r.mp4_caption_html, r.mkv_file_id, r.mkv_kind, r.run_at,
               r.mp4_msg_id, r.mkv_msg_id,
               t.repost_channel
        FROM releases r LEFT JOIN templates t ON t.id = r.template_id
        WHERE r.status IN ('pending', 'sending') AND r.run_at <= ?
        ORDER BY r.run_at
        """,
        (now_str,),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_sending(release_id: int) -> None:
    """Позначає випуск як «публікується» — щоб після падіння посеред публікації
    було видно, що частина кроків могла вже піти в канали."""
    await _conn().execute(
        "UPDATE releases SET status = 'sending' WHERE id = ?",
        (release_id,),
    )
    await _conn().commit()


async def save_mp4_msg_id(release_id: int, message_id: int) -> None:
    """Фіксує id уже відправленого поста MP4 — при повторі цей крок пропускається."""
    await _conn().execute(
        "UPDATE releases SET mp4_msg_id = ? WHERE id = ?",
        (message_id, release_id),
    )
    await _conn().commit()


async def save_mkv_msg_id(release_id: int, message_id: int) -> None:
    """Фіксує id уже відправленого поста MKV — при повторі цей крок пропускається."""
    await _conn().execute(
        "UPDATE releases SET mkv_msg_id = ? WHERE id = ?",
        (message_id, release_id),
    )
    await _conn().commit()


async def mark_sent(release_id: int) -> None:
    await _conn().execute(
        "UPDATE releases SET status = 'sent', sent_at = ? WHERE id = ?",
        (_now(), release_id),
    )
    await _conn().commit()


async def mark_failed(release_id: int) -> None:
    await _conn().execute(
        "UPDATE releases SET status = 'failed' WHERE id = ?",
        (release_id,),
    )
    await _conn().commit()


async def retry_release(release_id: int) -> bool:
    """Повертає провалений (failed) випуск у чергу: status → pending.
    run_at уже в минулому, тож планувальник підхопить його на найближчій хвилині."""
    cur = await _conn().execute(
        "UPDATE releases SET status = 'pending' WHERE id = ? AND status = 'failed'",
        (release_id,),
    )
    await _conn().commit()
    return cur.rowcount > 0


async def cancel_release(release_id: int) -> bool:
    """Видаляє запланований (pending) випуск. True, якщо щось видалили."""
    cur = await _conn().execute(
        "DELETE FROM releases WHERE id = ? AND status = 'pending'",
        (release_id,),
    )
    await _conn().commit()
    return cur.rowcount > 0
