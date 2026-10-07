"""Тексты, которые бот показывает пользователям."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App

ADMIN_ONLY = "Эта команда доступна только администраторам чата."

VK_NEED_ADMIN = (
    "Я не вижу настроек беседы. Назначьте сообщество администратором беседы и повторите команду."
)

NOT_BRIDGED = "Этот чат пока не связан. Отправьте /bridge, чтобы получить ключ."


def help_text(app: App) -> str:
    return (
        "🌉 Мост VK ↔ Telegram\n\n"
        "Пересылаю сообщения между беседой ВКонтакте и группой Telegram: "
        "текст, фото, файлы, голосовые и ответы.\n\n"
        "Как подключить:\n"
        f"1. Добавьте Telegram-бота @{app.tg_username} в группу Telegram.\n"
        f"2. Добавьте сообщество «{app.group_name}» (vk.com/{app.group_screen_name}) в беседу VK — "
        "кнопка «Добавить в чат» на странице сообщества — и назначьте его администратором беседы.\n"
        "3. В одном из чатов отправьте /bridge — придёт ключ.\n"
        "4. В другом чате отправьте /bridge КЛЮЧ.\n\n"
        "Команды (для администраторов чата):\n"
        "/bridge — получить ключ\n"
        "/bridge КЛЮЧ — связать чаты\n"
        "/bridge prefix on|off — метки [VK]/[TG] перед именем\n"
        "/bridge direction both|tg>vk|vk>tg — направление пересылки\n"
        "/unbridge — отключить мост\n"
        "/status — состояние моста"
    )
