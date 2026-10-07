"""Подделки Telegram и VK для тестов: записывают вызовы вместо сетевых запросов."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vktg.app import App, VKNames  # noqa: E402
from vktg.config import Config  # noqa: E402
from vktg.db import Storage  # noqa: E402
from vktg.queues import Outbox  # noqa: E402


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 1000

    def _message(self) -> SimpleNamespace:
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    def __getattr__(self, name: str):
        if not (name.startswith("send_") or name.startswith("edit_")):
            raise AttributeError(name)

        async def method(**kwargs):
            self.calls.append((name, kwargs))
            if name == "send_media_group":
                return [self._message() for _ in kwargs["media"]]
            return self._message()

        return method

    async def download(self, file, timeout: int = 30):
        return io.BytesIO(b"file:" + file.file_id.encode())


class FakeVK:
    version = "5.199"

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[tuple] = []
        self.uploads: list[tuple] = []
        self.next_cmid = 500
        self.random_ids: list[int | None] = []  # random_id каждой попытки отправки, включая неудачные
        self.fail_sends: list[Exception] = []  # ошибки, которые вернут следующие вызовы send()
        self.chat_settings: dict | None = {"owner_id": 1, "admin_ids": [2], "title": "Беседа"}

    async def call(self, method: str, **params):
        if method == "users.get":
            user_id = int(params["user_ids"])
            return [{"id": user_id, "first_name": "Иван", "last_name": f"#{user_id}"}]
        if method == "messages.getConversationsById":
            return {"items": [{"chat_settings": self.chat_settings}] if self.chat_settings else []}
        raise AssertionError(f"неожиданный вызов VK API: {method}")

    async def group_info(self, group_id=None):
        return {"id": group_id or 77, "name": "Клуб", "screen_name": "club77"}

    async def send(self, peer_id, text="", attachments=(), reply_cmid=None, random_id=None):
        self.random_ids.append(random_id)
        if self.fail_sends:
            raise self.fail_sends.pop(0)
        self.next_cmid += 1
        self.sent.append(
            {"peer_id": peer_id, "text": text, "attachments": list(attachments), "reply_cmid": reply_cmid,
             "cmid": self.next_cmid}
        )
        return self.next_cmid

    async def edit(self, peer_id, cmid, text, attachments=""):
        self.edits.append((peer_id, cmid, text, attachments))

    async def upload_photo(self, peer_id, data, filename="photo.jpg"):
        self.uploads.append(("photo", data, filename))
        return f"photo-77_{len(self.uploads)}"

    async def upload_doc(self, peer_id, data, filename, doc_type="doc"):
        self.uploads.append((doc_type, data, filename))
        return f"doc-77_{len(self.uploads)}"


@pytest.fixture
def make_app():
    """Фабрика App с подделками; вызывать внутри asyncio.run()."""
    databases: list[Storage] = []

    async def factory(fetched: dict[str, bytes | None] | None = None) -> App:
        vk = FakeVK()
        db = await Storage.open(":memory:")
        databases.append(db)

        async def fetch(url: str, limit: int) -> bytes | None:
            if fetched is not None and url in fetched:
                return fetched[url]
            return b"img:" + url.encode()

        return App(
            cfg=Config(tg_token="tg", vk_token="vk"),
            db=db,
            bot=FakeBot(),
            vk=vk,
            group_id=77,
            group_name="Клуб",
            group_screen_name="club77",
            tg_username="bridge_bot",
            outbox=Outbox(db, retry_base=0.01),  # в тестах повтор почти сразу
            names=VKNames(vk),
            fetch=fetch,
        )

    yield factory

    # Незакрытое соединение aiosqlite при сборке мусора обращается к уже закрытому циклу событий
    # и роняет предупреждение в чужом тесте. Закрываем базы явно.
    async def close_all() -> None:
        for db in databases:
            await db.close()

    asyncio.run(close_all())
