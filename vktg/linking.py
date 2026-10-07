"""Команды привязки чатов: /bridge, /unbridge, /status. Общие для Telegram и VK."""

from __future__ import annotations

import secrets

from . import texts
from .app import App
from .db import AlreadyBridged, Bridge

KEY_TTL = 15 * 60
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих O/0 и I/1

OTHER = {"tg": "vk", "vk": "tg"}
LABEL = {"tg": "Telegram", "vk": "VK"}

DIRECTION_ALIASES = {
    "both": "both",
    "tg>vk": "tg2vk",
    "tg2vk": "tg2vk",
    "vk>tg": "vk2tg",
    "vk2tg": "vk2tg",
}
DIRECTION_LABELS = {
    "both": "в обе стороны",
    "tg2vk": "только Telegram → VK",
    "vk2tg": "только VK → Telegram",
}


def new_key() -> str:
    return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(8))


async def _bridge_of(app: App, platform: str, chat_id: int) -> Bridge | None:
    if platform == "tg":
        return await app.db.bridge_by_tg(chat_id)
    return await app.db.bridge_by_vk(chat_id)


def _where(app: App, platform: str) -> str:
    """Где отправить ключ: описание чата на другой платформе."""
    if platform == "vk":
        return f"беседе VK, куда добавлено сообщество «{app.group_name}»"
    return f"группе Telegram, куда добавлен бот @{app.tg_username}"


async def cmd_bridge(app: App, platform: str, chat_id: int, args: str, title: str | None = None) -> str:
    parts = args.split()
    bridge = await _bridge_of(app, platform, chat_id)

    if not parts:
        if bridge:
            return "Этот чат уже связан. /status — подробности, /unbridge — отключить."
        key = new_key()
        await app.db.create_key(platform, chat_id, key)
        return (
            f"🔑 Ключ: {key}\n\n"
            f"Отправьте в {_where(app, OTHER[platform])} команду:\n"
            f"/bridge {key}\n\n"
            "Ключ действует 15 минут."
        )

    sub = parts[0].lower()
    if sub == "prefix":
        if not bridge:
            return texts.NOT_BRIDGED
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            return "Использование: /bridge prefix on|off"
        enabled = parts[1].lower() == "on"
        await app.db.set_prefix(bridge.id, enabled)
        return "Метки [VK]/[TG] включены." if enabled else "Метки [VK]/[TG] выключены."

    if sub in ("direction", "dir"):
        if not bridge:
            return texts.NOT_BRIDGED
        direction = DIRECTION_ALIASES.get(parts[1].lower()) if len(parts) > 1 else None
        if direction is None:
            return "Использование: /bridge direction both | tg>vk | vk>tg"
        await app.db.set_direction(bridge.id, direction)
        return f"Направление пересылки: {DIRECTION_LABELS[direction]}."

    if bridge:
        return "Этот чат уже связан. Сначала отключите мост командой /unbridge."

    key = parts[0].upper()
    other = OTHER[platform]
    other_chat = await app.db.take_key(key, other, KEY_TTL)
    if other_chat is None:
        if await app.db.key_platform(key) == platform:
            return f"Этот ключ создан в {LABEL[platform]}. Отправьте его в {_where(app, other)}."
        return "Ключ не найден или истёк. Получите новый командой /bridge."

    tg_chat, vk_peer = (chat_id, other_chat) if platform == "tg" else (other_chat, chat_id)
    try:
        await app.db.add_bridge(tg_chat, vk_peer)
    except AlreadyBridged:
        return "Один из чатов уже связан с другим. Сначала отключите старый мост командой /unbridge."

    source = f"{LABEL[platform]}: «{title}»" if title else LABEL[platform]
    await app.notify(other, other_chat, f"✅ Мост установлен с {source}. Сообщения пересылаются в обе стороны.")
    return "✅ Мост установлен. Сообщения пересылаются в обе стороны."


async def cmd_unbridge(app: App, platform: str, chat_id: int) -> str:
    bridge = await _bridge_of(app, platform, chat_id)
    if bridge is None:
        return "Этот чат не связан."
    await app.db.delete_bridge(bridge.id)
    other_chat = bridge.vk_peer_id if platform == "tg" else bridge.tg_chat_id
    await app.notify(OTHER[platform], other_chat, f"❌ Мост отключён из {LABEL[platform]}.")
    return "❌ Мост отключён."


async def cmd_status(app: App, platform: str, chat_id: int) -> str:
    bridge = await _bridge_of(app, platform, chat_id)
    if bridge is None:
        return texts.NOT_BRIDGED
    other_chat = bridge.vk_peer_id if platform == "tg" else bridge.tg_chat_id
    return (
        f"🔗 Связан с {LABEL[OTHER[platform]]} (ID {other_chat})\n"
        f"Направление: {DIRECTION_LABELS[bridge.direction]}\n"
        f"Метки [VK]/[TG]: {'включены' if bridge.prefix else 'выключены'}"
    )
