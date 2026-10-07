"""Минимальный асинхронный клиент VK API для бота сообщества: методы, загрузка файлов, Bots Long Poll."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"
CHAT_PEER_OFFSET = 2_000_000_000  # peer_id бесед начинаются с этого числа

# 1 — неизвестная ошибка, 6 — слишком много запросов, 9 — флуд-контроль, 10 — внутренняя ошибка VK.
RETRYABLE_CODES = {1, 6, 9, 10}
_ATTEMPTS = 5
# Обычный запрос к API не должен висеть минутами: зависшее соединение держит очередь чата.
# Загрузка файлов и long poll используют свои таймауты.
_API_TIMEOUT = aiohttp.ClientTimeout(total=30, sock_connect=10)
SLOW_CALL = 2.0  # ответы API дольше этого (в секундах) попадают в лог


class VKError(Exception):
    def __init__(self, code: int, message: str, method: str = "") -> None:
        super().__init__(f"{method}: [{code}] {message}" if method else f"[{code}] {message}")
        self.code = code


def _prepare(params: dict[str, Any]) -> dict[str, str]:
    prepared = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, (list, tuple)):
            value = ",".join(map(str, value))
        prepared[key] = str(value)
    return prepared


def _attachment(kind: str, obj: dict[str, Any]) -> str:
    result = f"{kind}{obj['owner_id']}_{obj['id']}"
    if obj.get("access_key"):
        result += f"_{obj['access_key']}"
    return result


class VKApi:
    def __init__(self, token: str, http: aiohttp.ClientSession, version: str = "5.199") -> None:
        self._token = token
        self._http = http
        self.version = version

    async def call(self, method: str, **params: Any) -> Any:
        data = _prepare(params)
        data["access_token"] = self._token
        data["v"] = self.version
        delay = 1.0
        for attempt in range(1, _ATTEMPTS + 1):
            started = time.monotonic()
            try:
                async with self._http.post(API_URL + method, data=data, timeout=_API_TIMEOUT) as resp:
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                if attempt == _ATTEMPTS:
                    raise
                log.warning(
                    "VK %s: сетевая ошибка через %.1f с (%r), повтор через %.0f с",
                    method, time.monotonic() - started, exc, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            elapsed = time.monotonic() - started
            if elapsed > SLOW_CALL:
                log.info("VK %s ответил за %.1f с", method, elapsed)

            error = payload.get("error")
            if error is None:
                return payload["response"]
            code = error.get("error_code", 0)
            if code in RETRYABLE_CODES and attempt < _ATTEMPTS:
                log.warning(
                    "VK %s: ошибка %s (%s), повтор через %.0f с", method, code, error.get("error_msg", ""), delay
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise VKError(code, error.get("error_msg", ""), method)
        raise VKError(0, "исчерпаны попытки", method)

    async def group_info(self, group_id: int | None = None) -> dict[str, Any]:
        """Без group_id возвращает сообщество, которому принадлежит токен."""
        response = await self.call("groups.getById", group_id=group_id)
        groups = response["groups"] if isinstance(response, dict) else response
        return groups[0]

    # --- сообщения ---

    async def send(
        self,
        peer_id: int,
        text: str = "",
        attachments: Iterable[str] = (),
        reply_cmid: int | None = None,
        random_id: int | None = None,
    ) -> int:
        """Отправляет сообщение и возвращает его conversation_message_id."""
        params: dict[str, Any] = {
            # peer_ids вместо peer_id: только так VK возвращает conversation_message_id.
            "peer_ids": peer_id,
            # VK не отправляет повторно сообщение с тем же random_id — это защищает от дублей при повторах.
            "random_id": random_id or secrets.randbelow(2**31 - 1) + 1,
            "message": text or None,
            "attachment": ",".join(attachments) or None,
            "disable_mentions": 1,
        }
        if reply_cmid:
            params["forward"] = json.dumps(
                {"peer_id": peer_id, "conversation_message_ids": [reply_cmid], "is_reply": True}
            )
        response = await self.call("messages.send", **params)
        item = response[0]
        if "error" in item:
            error = item["error"]
            raise VKError(error.get("code", 0), error.get("description", ""), "messages.send")
        return item["conversation_message_id"]

    async def edit(self, peer_id: int, cmid: int, text: str, attachments: str = "") -> None:
        await self.call(
            "messages.edit",
            peer_id=peer_id,
            conversation_message_id=cmid,
            message=text or None,
            attachment=attachments or None,
            keep_forward_messages=1,
            keep_snippets=1,
        )

    # --- загрузка файлов ---

    async def _upload(self, url: str, field: str, data: bytes, filename: str) -> dict[str, Any]:
        form = aiohttp.FormData()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        form.add_field(field, data, filename=filename, content_type=content_type)
        async with self._http.post(url, data=form) as resp:
            return await resp.json(content_type=None)

    async def upload_photo(self, peer_id: int, data: bytes, filename: str = "photo.jpg") -> str:
        server = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        uploaded = await self._upload(server["upload_url"], "photo", data, filename)
        if not uploaded.get("photo") or uploaded["photo"] == "[]":
            raise VKError(0, f"фото не загрузилось: {uploaded}", "upload")
        saved = await self.call(
            "photos.saveMessagesPhoto", photo=uploaded["photo"], server=uploaded["server"], hash=uploaded["hash"]
        )
        return _attachment("photo", saved[0])

    async def upload_doc(self, peer_id: int, data: bytes, filename: str, doc_type: str = "doc") -> str:
        """doc_type: doc — обычный файл, audio_message — голосовое (OGG/Opus)."""
        server = await self.call("docs.getMessagesUploadServer", peer_id=peer_id, type=doc_type)
        uploaded = await self._upload(server["upload_url"], "file", data, filename)
        if "file" not in uploaded:
            reason = uploaded.get("error_descr") or uploaded.get("error") or uploaded
            raise VKError(0, f"файл не загрузился: {reason}", "upload")
        saved = await self.call("docs.save", file=uploaded["file"], title=filename)
        if isinstance(saved, list):
            obj = saved[0]
        else:
            obj = saved.get(saved.get("type", "doc")) or saved.get("doc")
        # Голосовые тоже документы: вложение вида doc<owner>_<id>.
        return _attachment("doc", obj)

    # --- Bots Long Poll ---

    async def listen(self, group_id: int) -> AsyncIterator[dict[str, Any]]:
        server = await self.call("groups.getLongPollServer", group_id=group_id)
        ts = server["ts"]
        while True:
            try:
                async with self._http.get(
                    server["server"],
                    params={"act": "a_check", "key": server["key"], "ts": ts, "wait": 25},
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                log.warning("VK long poll: %s, переподключение", exc)
                await asyncio.sleep(3)
                continue

            failed = data.get("failed")
            if failed == 1:  # история событий устарела — просто берём новый ts
                ts = data["ts"]
                continue
            if failed:  # 2 — истёк ключ, 3 — потеряны ключ и ts
                server = await self.call("groups.getLongPollServer", group_id=group_id)
                if failed != 2:
                    ts = server["ts"]
                continue

            ts = data.get("ts", ts)
            for update in data.get("updates", []):
                yield update
