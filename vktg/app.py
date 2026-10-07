"""Общий контекст приложения и вспомогательные функции для обеих сторон моста."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from .config import Config
from .db import Storage
from .queues import Outbox
from .vk_api import VKApi, VKError

log = logging.getLogger(__name__)

Fetch = Callable[[str, int], Awaitable[bytes | None]]


@dataclass
class App:
    cfg: Config
    db: Storage
    bot: Bot
    vk: VKApi
    group_id: int
    group_name: str
    group_screen_name: str
    tg_username: str
    outbox: Outbox
    names: VKNames
    fetch: Fetch

    async def notify(self, platform: str, chat_id: int, text: str) -> None:
        """Служебное сообщение в чат: platform = "tg" или "vk"."""
        try:
            if platform == "tg":
                await tg_retry(self.bot.send_message, chat_id=chat_id, text=text)
            else:
                await self.vk.send(chat_id, text)
        except Exception as exc:
            log.warning("Не удалось отправить уведомление в %s %s: %s", platform, chat_id, exc)


class VKNames:
    """Имена пользователей и сообществ VK с кэшем."""

    def __init__(self, vk: VKApi, limit: int = 10_000) -> None:
        self._vk = vk
        self._limit = limit
        self._cache: dict[int, str] = {}

    async def get(self, owner_id: int) -> str:
        if owner_id in self._cache:
            return self._cache[owner_id]
        name = ""
        try:
            if owner_id > 0:
                user = (await self._vk.call("users.get", user_ids=owner_id))[0]
                name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            elif owner_id < 0:
                name = (await self._vk.group_info(-owner_id)).get("name", "")
        except (VKError, LookupError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("Не удалось получить имя %s: %s", owner_id, exc)
        if not name:
            # Не кэшируем: возможно, это временная ошибка сети.
            return f"id{owner_id}" if owner_id >= 0 else f"club{-owner_id}"
        if len(self._cache) >= self._limit:
            self._cache.clear()
        self._cache[owner_id] = name
        return name


async def tg_retry(method: Callable[..., Awaitable[Any]], /, **kwargs: Any) -> Any:
    """Вызов Bot API с ожиданием при флуд-лимите и повтором при сетевых ошибках."""
    for attempt in range(1, 6):
        try:
            return await method(**kwargs)
        except TelegramRetryAfter as exc:
            if attempt == 5:
                raise
            log.info("Telegram просит подождать %s с", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 0.5)
        except TelegramNetworkError:
            if attempt == 5:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("unreachable")


async def fetch_url(http: aiohttp.ClientSession, url: str, limit: int) -> bytes | None:
    """Скачивает файл целиком в память; None, если он больше limit байт или недоступен."""
    host = urlsplit(url).netloc  # в логах только хост: в ссылках VK бывают ключи доступа
    try:
        async with http.get(url) as resp:
            if resp.status != 200:
                log.warning("Не удалось скачать файл с %s: HTTP %s", host, resp.status)
                return None
            if resp.content_length and resp.content_length > limit:
                return None
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(1 << 16):
                buf.extend(chunk)
                if len(buf) > limit:
                    return None
            return bytes(buf)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Не удалось скачать файл с %s: %s", host, exc)
        return None
