"""Обработчики VK: события Bots Long Poll, команды в беседах."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import linking, texts
from .app import App
from .formatting import parse_command
from .to_tg import ToTG
from .vk_api import CHAT_PEER_OFFSET, VKError

log = logging.getLogger(__name__)

COMMANDS = {"bridge", "unbridge", "status", "help", "start"}


class FatalVKError(RuntimeError):
    """Ошибка, после которой нет смысла переподключаться (например, неверный токен)."""


class VKHandlers:
    def __init__(self, app: App, to_tg: ToTG) -> None:
        self.app = app
        self.to_tg = to_tg

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                async for update in self.app.vk.listen(self.app.group_id):
                    backoff = 1
                    try:
                        await self.handle(update)
                    except Exception:
                        log.exception("Ошибка обработки события VK")
            except VKError as exc:
                if exc.code in (5, 15, 27):  # неверный токен, нет доступа, ключ сообщества отозван
                    raise FatalVKError(f"VK отклонил токен: {exc}") from exc
                log.error("VK long poll: %s", exc)
            except Exception:
                log.exception("VK long poll упал, переподключение")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def handle(self, update: dict[str, Any]) -> None:
        kind = update.get("type")
        obj = update.get("object") or {}
        if kind == "message_new":
            await self.on_message(obj.get("message", obj))
        elif kind == "message_edit":
            await self.on_edit(obj)

    async def on_message(self, msg: dict[str, Any]) -> None:
        peer_id = msg.get("peer_id", 0)
        from_id = msg.get("from_id", 0)
        if from_id == -self.app.group_id:
            return
        command = parse_command(msg.get("text") or "")

        if peer_id < CHAT_PEER_OFFSET:
            # Личные сообщения сообществу: отвечаем только на явный запрос справки,
            # чтобы не мешать обычной переписке сообщества с подписчиками.
            if command and command[0] in ("help", "start"):
                await self._reply(peer_id, texts.help_text(self.app))
            return

        action = msg.get("action") or {}
        if action:
            if action.get("type") == "chat_invite_user" and action.get("member_id") == -self.app.group_id:
                await self._reply(peer_id, texts.help_text(self.app))
            return  # служебные сообщения беседы не пересылаем

        if command and command[0] in COMMANDS:
            await self.on_command(peer_id, from_id, *command)
            return

        bridge = await self.app.db.bridge_by_vk(peer_id)
        if bridge and bridge.to_tg:
            self.to_tg.submit(bridge, msg)

    async def on_edit(self, msg: dict[str, Any]) -> None:
        peer_id = msg.get("peer_id", 0)
        if msg.get("from_id") == -self.app.group_id or peer_id < CHAT_PEER_OFFSET:
            return
        bridge = await self.app.db.bridge_by_vk(peer_id)
        if bridge and bridge.to_tg:
            self.to_tg.submit_edit(bridge, msg)

    async def on_command(self, peer_id: int, from_id: int, name: str, args: str) -> None:
        if name in ("help", "start"):
            await self._reply(peer_id, texts.help_text(self.app))
            return
        if name == "status":
            await self._reply(peer_id, await linking.cmd_status(self.app, "vk", peer_id))
            return

        settings = await self._chat_settings(peer_id)
        if settings is None:
            await self._reply(peer_id, texts.VK_NEED_ADMIN)
            return
        admins = set(settings.get("admin_ids") or [])
        admins.add(settings.get("owner_id"))
        if from_id not in admins:
            await self._reply(peer_id, texts.ADMIN_ONLY)
            return

        if name == "bridge":
            reply = await linking.cmd_bridge(self.app, "vk", peer_id, args, settings.get("title"))
        else:
            reply = await linking.cmd_unbridge(self.app, "vk", peer_id)
        await self._reply(peer_id, reply)

    async def _chat_settings(self, peer_id: int) -> dict[str, Any] | None:
        """Настройки беседы (владелец, админы, название). None — у сообщества нет доступа."""
        try:
            response = await self.app.vk.call("messages.getConversationsById", peer_ids=peer_id)
        except VKError as exc:
            log.info("Нет доступа к настройкам беседы %s: %s", peer_id, exc)
            return None
        items = response.get("items") or []
        return items[0].get("chat_settings") if items else None

    async def _reply(self, peer_id: int, text: str) -> None:
        try:
            await self.app.vk.send(peer_id, text)
        except VKError as exc:
            log.warning("Не удалось ответить в VK %s: %s", peer_id, exc)
