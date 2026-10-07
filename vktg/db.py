"""Хранилище SQLite: связки чатов, ключи привязки и соответствие сообщений."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS bridges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id  INTEGER NOT NULL UNIQUE,
    vk_peer_id  INTEGER NOT NULL UNIQUE,
    direction   TEXT    NOT NULL DEFAULT 'both',
    prefix      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_keys (
    key         TEXT    PRIMARY KEY,
    platform    TEXT    NOT NULL,
    chat_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

-- Одна строка: сообщение Telegram и парное ему сообщение VK.
-- role = source: оригинал написан в Telegram, а в VK лежит копия.
-- role = text | caption | media | extra: оригинал в VK, а это копия, отправленная ботом в Telegram.
CREATE TABLE IF NOT EXISTS messages (
    tg_chat_id      INTEGER NOT NULL,
    tg_msg_id       INTEGER NOT NULL,
    vk_peer_id      INTEGER NOT NULL,
    vk_cmid         INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    vk_attachments  TEXT    NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (tg_chat_id, tg_msg_id)
);

CREATE INDEX IF NOT EXISTS messages_by_vk ON messages (vk_peer_id, vk_cmid);
CREATE INDEX IF NOT EXISTS messages_by_age ON messages (created_at);

-- Очередь доставки. target — чат-получатель ("tg:<chat_id>" или "vk:<peer_id>"),
-- задачи одного target выполняются строго по порядку id. Строка удаляется после доставки.
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    nonce       INTEGER NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  REAL    NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS outbox_by_target ON outbox (target, id);

-- Служебные значения, например «после перезапуска сообщить владельцу об обновлении».
CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

DIRECTIONS = ("both", "tg2vk", "vk2tg")


def _now() -> int:
    return int(time.time())


@dataclass(frozen=True)
class Bridge:
    id: int
    tg_chat_id: int
    vk_peer_id: int
    direction: str
    prefix: bool

    @property
    def to_vk(self) -> bool:
        return self.direction in ("both", "tg2vk")

    @property
    def to_tg(self) -> bool:
        return self.direction in ("both", "vk2tg")


@dataclass(frozen=True)
class MessageLink:
    tg_chat_id: int
    tg_msg_id: int
    vk_peer_id: int
    vk_cmid: int
    role: str
    vk_attachments: str


@dataclass(frozen=True)
class OutboxJob:
    id: int
    target: str
    kind: str
    payload: Any
    nonce: int  # random_id для VK: повторная отправка той же задачи не создаёт дубль
    attempts: int
    not_before: float
    created_at: int


class AlreadyBridged(Exception):
    pass


def _bridge(row: sqlite3.Row | None) -> Bridge | None:
    if row is None:
        return None
    return Bridge(row["id"], row["tg_chat_id"], row["vk_peer_id"], row["direction"], bool(row["prefix"]))


def _link(row: sqlite3.Row) -> MessageLink:
    return MessageLink(
        row["tg_chat_id"], row["tg_msg_id"], row["vk_peer_id"], row["vk_cmid"], row["role"], row["vk_attachments"]
    )


class Storage:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._db = conn

    @classmethod
    async def open(cls, path: str) -> Storage:
        conn = await aiosqlite.connect(path)
        conn.row_factory = sqlite3.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._db.close()

    # --- ключи привязки ---

    async def create_key(self, platform: str, chat_id: int, key: str) -> None:
        await self._db.execute("DELETE FROM pending_keys WHERE platform = ? AND chat_id = ?", (platform, chat_id))
        await self._db.execute(
            "INSERT INTO pending_keys (key, platform, chat_id, created_at) VALUES (?, ?, ?, ?)",
            (key, platform, chat_id, _now()),
        )
        await self._db.commit()

    async def take_key(self, key: str, platform: str, ttl: int) -> int | None:
        """Забирает ключ, созданный на платформе platform; возвращает chat_id или None."""
        cursor = await self._db.execute(
            "DELETE FROM pending_keys WHERE key = ? AND platform = ? AND created_at >= ? RETURNING chat_id",
            (key, platform, _now() - ttl),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self._db.commit()
        return row["chat_id"] if row else None

    async def key_platform(self, key: str) -> str | None:
        async with self._db.execute("SELECT platform FROM pending_keys WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row["platform"] if row else None

    async def purge_keys(self, ttl: int) -> None:
        await self._db.execute("DELETE FROM pending_keys WHERE created_at < ?", (_now() - ttl,))
        await self._db.commit()

    # --- связки ---

    async def add_bridge(self, tg_chat_id: int, vk_peer_id: int) -> Bridge:
        try:
            cursor = await self._db.execute(
                "INSERT INTO bridges (tg_chat_id, vk_peer_id, created_at) VALUES (?, ?, ?)",
                (tg_chat_id, vk_peer_id, _now()),
            )
        except sqlite3.IntegrityError:
            raise AlreadyBridged from None
        await self._db.commit()
        return Bridge(cursor.lastrowid, tg_chat_id, vk_peer_id, "both", True)

    async def bridge_by_tg(self, tg_chat_id: int) -> Bridge | None:
        async with self._db.execute("SELECT * FROM bridges WHERE tg_chat_id = ?", (tg_chat_id,)) as cursor:
            return _bridge(await cursor.fetchone())

    async def bridge_by_vk(self, vk_peer_id: int) -> Bridge | None:
        async with self._db.execute("SELECT * FROM bridges WHERE vk_peer_id = ?", (vk_peer_id,)) as cursor:
            return _bridge(await cursor.fetchone())

    async def delete_bridge(self, bridge_id: int) -> None:
        await self._db.execute("DELETE FROM bridges WHERE id = ?", (bridge_id,))
        await self._db.commit()

    async def set_prefix(self, bridge_id: int, enabled: bool) -> None:
        await self._db.execute("UPDATE bridges SET prefix = ? WHERE id = ?", (int(enabled), bridge_id))
        await self._db.commit()

    async def set_direction(self, bridge_id: int, direction: str) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(direction)
        await self._db.execute("UPDATE bridges SET direction = ? WHERE id = ?", (direction, bridge_id))
        await self._db.commit()

    async def migrate_tg_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        """Группа Telegram стала супергруппой и получила новый chat_id."""
        await self._db.execute("UPDATE bridges SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_chat_id, old_chat_id))
        await self._db.execute(
            "UPDATE OR IGNORE messages SET tg_chat_id = ? WHERE tg_chat_id = ?", (new_chat_id, old_chat_id)
        )
        await self._db.commit()

    # --- соответствие сообщений ---

    async def save_link(
        self, tg_chat_id: int, tg_msg_id: int, vk_peer_id: int, vk_cmid: int, role: str, vk_attachments: str = ""
    ) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO messages "
            "(tg_chat_id, tg_msg_id, vk_peer_id, vk_cmid, role, vk_attachments, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tg_chat_id, tg_msg_id, vk_peer_id, vk_cmid, role, vk_attachments, _now()),
        )
        await self._db.commit()

    async def link_by_tg(self, tg_chat_id: int, tg_msg_id: int) -> MessageLink | None:
        async with self._db.execute(
            "SELECT * FROM messages WHERE tg_chat_id = ? AND tg_msg_id = ?", (tg_chat_id, tg_msg_id)
        ) as cursor:
            row = await cursor.fetchone()
        return _link(row) if row else None

    async def links_by_vk(self, vk_peer_id: int, vk_cmid: int) -> list[MessageLink]:
        async with self._db.execute(
            "SELECT * FROM messages WHERE vk_peer_id = ? AND vk_cmid = ? ORDER BY tg_msg_id", (vk_peer_id, vk_cmid)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_link(row) for row in rows]

    async def purge_links(self, max_age: int) -> None:
        await self._db.execute("DELETE FROM messages WHERE created_at < ?", (_now() - max_age,))
        await self._db.commit()

    # --- очередь доставки ---

    async def outbox_add(self, target: str, kind: str, payload: Any, not_before: float) -> int:
        cursor = await self._db.execute(
            "INSERT INTO outbox (target, kind, payload, nonce, not_before, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                target,
                kind,
                json.dumps(payload, ensure_ascii=False),
                secrets.randbelow(2**31 - 1000) + 1,  # запас: длинный текст уходит частями nonce, nonce+1, …
                not_before,
                _now(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def outbox_update(self, job_id: int, payload: Any, not_before: float) -> None:
        await self._db.execute(
            "UPDATE outbox SET payload = ?, not_before = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), not_before, job_id),
        )
        await self._db.commit()

    async def outbox_head(self, target: str) -> OutboxJob | None:
        """Первая (самая старая) задача для чата-получателя."""
        async with self._db.execute(
            "SELECT * FROM outbox WHERE target = ? ORDER BY id LIMIT 1", (target,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return OutboxJob(
            row["id"],
            row["target"],
            row["kind"],
            json.loads(row["payload"]),
            row["nonce"],
            row["attempts"],
            row["not_before"],
            row["created_at"],
        )

    async def outbox_targets(self) -> list[str]:
        async with self._db.execute("SELECT DISTINCT target FROM outbox") as cursor:
            return [row["target"] for row in await cursor.fetchall()]

    async def outbox_count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) AS n FROM outbox") as cursor:
            return (await cursor.fetchone())["n"]

    async def outbox_retry(self, job_id: int, not_before: float) -> None:
        await self._db.execute(
            "UPDATE outbox SET attempts = attempts + 1, not_before = ? WHERE id = ?", (not_before, job_id)
        )
        await self._db.commit()

    async def outbox_done(self, job_id: int) -> None:
        await self._db.execute("DELETE FROM outbox WHERE id = ?", (job_id,))
        await self._db.commit()

    # --- служебные значения ---

    async def kv_set(self, key: str, value: str) -> None:
        await self._db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        await self._db.commit()

    async def kv_pop(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        await self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
        await self._db.commit()
        return row["value"]
