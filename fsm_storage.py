"""Персистентний FSM-storage aiogram на тій самій SQLite-базі.

Стандартний MemoryStorage губить стан при рестарті бота: незавершений
масовий імпорт (буфер пересланих повідомлень) довелося б збирати заново.
Тут стан і дані майстрів зберігаються в таблиці fsm і переживають рестарт.

Вимагає попереднього виклику database.init_db().
"""

import json
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

import database


def _key(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}"


class SQLiteStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        await database._conn().execute(
            "INSERT INTO fsm (key, state) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state = excluded.state",
            (_key(key), value),
        )
        await database._conn().commit()

    async def get_state(self, key: StorageKey) -> str | None:
        cur = await database._conn().execute(
            "SELECT state FROM fsm WHERE key = ?", (_key(key),)
        )
        row = await cur.fetchone()
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        await database._conn().execute(
            "INSERT INTO fsm (key, data) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data = excluded.data",
            (_key(key), json.dumps(data, ensure_ascii=False)),
        )
        await database._conn().commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        cur = await database._conn().execute(
            "SELECT data FROM fsm WHERE key = ?", (_key(key),)
        )
        row = await cur.fetchone()
        return json.loads(row["data"]) if row else {}

    async def close(self) -> None:
        # Зʼєднання з БД закриває database.close_db() — тут нічого не тримаємо.
        pass
