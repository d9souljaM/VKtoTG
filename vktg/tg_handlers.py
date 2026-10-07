"""Обработчики Telegram: команды и сообщения групп."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import ChatMemberUpdated, Message

from . import linking, texts
from .app import App, tg_retry
from .to_vk import ToVK, has_content

GROUP_CHATS = {"group", "supergroup"}
in_group = F.chat.type.in_(GROUP_CHATS)


async def is_admin(app: App, message: Message) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # анонимный администратор
    if message.from_user is None:
        return False
    member = await app.bot.get_chat_member(chat_id=message.chat.id, user_id=message.from_user.id)
    return member.status in ("creator", "administrator")


async def on_help(message: Message, app: App) -> None:
    await message.answer(texts.help_text(app))


async def on_bridge(message: Message, command: CommandObject, app: App) -> None:
    if not await is_admin(app, message):
        await message.reply(texts.ADMIN_ONLY)
        return
    reply = await linking.cmd_bridge(app, "tg", message.chat.id, command.args or "", message.chat.title)
    await message.reply(reply)


async def on_unbridge(message: Message, app: App) -> None:
    if not await is_admin(app, message):
        await message.reply(texts.ADMIN_ONLY)
        return
    await message.reply(await linking.cmd_unbridge(app, "tg", message.chat.id))


async def on_status(message: Message, app: App) -> None:
    await message.reply(await linking.cmd_status(app, "tg", message.chat.id))


async def on_migrate(message: Message, app: App) -> None:
    await app.db.migrate_tg_chat(message.chat.id, message.migrate_to_chat_id)


async def on_group_message(message: Message, app: App, to_vk: ToVK) -> None:
    if not has_content(message):
        return
    bridge = await app.db.bridge_by_tg(message.chat.id)
    if bridge and bridge.to_vk:
        to_vk.submit(bridge, message)


async def on_group_edit(message: Message, app: App, to_vk: ToVK) -> None:
    bridge = await app.db.bridge_by_tg(message.chat.id)
    if bridge and bridge.to_vk:
        to_vk.submit_edit(bridge, message)


async def on_added_to_group(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.type in GROUP_CHATS:
        await tg_retry(app.bot.send_message, chat_id=event.chat.id, text=texts.help_text(app))


async def on_private(message: Message, app: App) -> None:
    await message.answer(texts.help_text(app))


def build_router() -> Router:
    router = Router(name="telegram")
    router.message.register(on_help, Command("start", "help"))
    router.message.register(on_bridge, in_group, Command("bridge"))
    router.message.register(on_unbridge, in_group, Command("unbridge"))
    router.message.register(on_status, in_group, Command("status"))
    router.message.register(on_migrate, in_group, F.migrate_to_chat_id)
    router.message.register(on_group_message, in_group)
    router.message.register(on_private, F.chat.type == "private")
    router.edited_message.register(on_group_edit, in_group)
    router.my_chat_member.register(on_added_to_group, ChatMemberUpdatedFilter(JOIN_TRANSITION))
    return router
