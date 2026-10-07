"""Обработчики Telegram: команды и сообщения групп."""

from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from . import linking, texts
from .app import App, tg_retry
from .to_vk import ToVK, has_content
from .updater import Updater, UpdateError

GROUP_CHATS = {"group", "supergroup"}
in_group = F.chat.type.in_(GROUP_CHATS)

UPDATE_APPLY = "update:apply"
UPDATE_CANCEL = "update:cancel"
UPDATE_NOTICE_KEY = "update_notice"  # в kv: кому сообщить о версии после перезапуска
_PENDING_SHOWN = 15


def is_owner(app: App, user: User | None) -> bool:
    return user is not None and app.cfg.tg_owner_id is not None and user.id == app.cfg.tg_owner_id


def help_for(app: App, message: Message) -> str:
    text = texts.help_text(app)
    if message.chat.type == "private" and app.cfg.tg_owner_id is None and message.from_user:
        # Пока владелец не настроен, подсказываем ID — его нужно вписать в TG_OWNER_ID.
        text += f"\n\nВаш Telegram ID: {message.from_user.id}"
    return text


async def is_admin(app: App, message: Message) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True  # анонимный администратор
    if message.from_user is None:
        return False
    member = await app.bot.get_chat_member(chat_id=message.chat.id, user_id=message.from_user.id)
    return member.status in ("creator", "administrator")


async def on_help(message: Message, app: App) -> None:
    await message.answer(help_for(app, message))


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
        await to_vk.submit(bridge, message)


async def on_group_edit(message: Message, app: App, to_vk: ToVK) -> None:
    bridge = await app.db.bridge_by_tg(message.chat.id)
    if bridge and bridge.to_vk:
        await to_vk.submit_edit(bridge, message)


async def on_added_to_group(event: ChatMemberUpdated, app: App) -> None:
    if event.chat.type in GROUP_CHATS:
        await tg_retry(app.bot.send_message, chat_id=event.chat.id, text=texts.help_text(app))


async def on_private(message: Message, app: App) -> None:
    await message.answer(help_for(app, message))


# --- обновление из GitHub (только владелец бота) ---


async def on_update(message: Message, app: App, updater: Updater) -> None:
    chat_id = message.chat.id
    if not is_owner(app, message.from_user):
        await tg_retry(app.bot.send_message, chat_id=chat_id, text=help_for(app, message))
        return
    status = await tg_retry(app.bot.send_message, chat_id=chat_id, text="🔄 Проверяю обновления на GitHub…")
    markup = None
    try:
        check = await updater.check()
    except UpdateError as exc:
        text = f"❌ Не удалось проверить обновления:\n{exc}"
    else:
        if not check.pending:
            text = f"✅ Обновлений нет.\nТекущая версия: {check.current}"
        else:
            shown = check.pending[-_PENDING_SHOWN:]
            more = len(check.pending) - len(shown)
            lines = ([f"…и ещё {more}"] if more else []) + [f"• {commit}" for commit in shown]
            text = (
                f"Текущая версия: {check.current}\n\n"
                f"Новое на GitHub ({len(check.pending)}):\n" + "\n".join(lines)
            )
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⬆️ Обновить и перезапустить", callback_data=UPDATE_APPLY)],
                    [InlineKeyboardButton(text="Отмена", callback_data=UPDATE_CANCEL)],
                ]
            )
    await tg_retry(
        app.bot.edit_message_text, chat_id=chat_id, message_id=status.message_id, text=text, reply_markup=markup
    )


async def on_update_button(callback: CallbackQuery, app: App, updater: Updater) -> None:
    if not is_owner(app, callback.from_user):
        await app.bot.answer_callback_query(
            callback_query_id=callback.id, text="Обновлять бота может только владелец", show_alert=True
        )
        return
    await app.bot.answer_callback_query(callback_query_id=callback.id)
    if callback.message is None:
        return
    chat_id, message_id = callback.message.chat.id, callback.message.message_id

    async def show(text: str) -> None:
        await tg_retry(app.bot.edit_message_text, chat_id=chat_id, message_id=message_id, text=text)

    if callback.data == UPDATE_CANCEL:
        await show("Обновление отменено.")
        return
    await show("⏳ Обновляю…")
    try:
        result = await updater.apply()
    except UpdateError as exc:
        await show(f"❌ Не удалось обновить:\n{exc}")
        return
    if result.old == result.new:
        await show("✅ Уже установлена последняя версия.")
        return
    deps = "\nЗависимости обновлены." if result.requirements_changed else ""
    await show(f"✅ Код обновлён: {result.old} → {result.new}.{deps}\n♻️ Перезапускаюсь…")
    await app.db.kv_set(UPDATE_NOTICE_KEY, json.dumps({"chat_id": chat_id}))
    app.restart.set()


def build_router() -> Router:
    router = Router(name="telegram")
    router.message.register(on_help, Command("start", "help"))
    router.message.register(on_update, F.chat.type == "private", Command("update"))
    router.callback_query.register(on_update_button, F.data.in_({UPDATE_APPLY, UPDATE_CANCEL}))
    router.message.register(on_bridge, in_group, Command("bridge"))
    router.message.register(on_unbridge, in_group, Command("unbridge"))
    router.message.register(on_status, in_group, Command("status"))
    router.message.register(on_migrate, in_group, F.migrate_to_chat_id)
    router.message.register(on_group_message, in_group)
    router.message.register(on_private, F.chat.type == "private")
    router.edited_message.register(on_group_edit, in_group)
    router.my_chat_member.register(on_added_to_group, ChatMemberUpdatedFilter(JOIN_TRANSITION))
    return router
