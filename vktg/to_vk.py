"""Пересылка Telegram → VK."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from aiogram.types import Message

from .app import App
from .db import Bridge
from .formatting import VK_TEXT_LIMIT, shorten, sign, split_text, tg_text_with_links, vk_header
from .vk_api import VKError

log = logging.getLogger(__name__)


def tg_sender_name(message: Message) -> str:
    if message.sender_chat:  # анонимный администратор или канал
        return message.sender_chat.title or "Канал"
    if message.from_user:
        return message.from_user.full_name
    return "Неизвестный"


def forward_label(message: Message) -> str | None:
    origin = message.forward_origin
    if origin is None or message.is_automatic_forward:
        return None
    if origin.type == "user":
        who = origin.sender_user.full_name
    elif origin.type == "hidden_user":
        who = origin.sender_user_name
    elif origin.type == "chat":
        who = origin.sender_chat.title
    elif origin.type == "channel":
        who = origin.chat.title
    else:
        who = "?"
    return f"↪ Переслано от {who}"


def real_reply(message: Message) -> Message | None:
    """Сообщение, на которое ответили. В темах форума reply на корень темы ответом не считается."""
    target = message.reply_to_message
    if target is None or target.forum_topic_created:
        return None
    if message.is_topic_message and target.message_id == message.message_thread_id:
        return None
    return target


def has_content(message: Message) -> bool:
    return any(
        (
            message.text,
            message.caption,
            message.photo,
            message.document,
            message.voice,
            message.video,
            message.video_note,
            message.animation,
            message.audio,
            message.sticker,
            message.location,
            message.contact,
            message.poll,
            message.dice,
        )
    )


def media_spec(message: Message) -> tuple[Any, str, str, str] | None:
    """(файл, имя для VK, тип загрузки, подпись для пользователя) или None."""
    if message.photo:
        return message.photo[-1], "photo.jpg", "photo", "фото"
    if message.voice:
        return message.voice, "voice.ogg", "audio_message", "голосовое"
    if message.video_note:
        return message.video_note, "video_note.mp4", "doc", "видеосообщение"
    if message.animation:  # проверяем до document: у GIF заполнены оба поля
        return message.animation, message.animation.file_name or "animation.mp4", "doc", "GIF"
    if message.video:
        return message.video, message.video.file_name or "video.mp4", "doc", "видео"
    if message.audio:
        return message.audio, message.audio.file_name or "audio.mp3", "doc", "аудио"
    if message.document:
        return message.document, message.document.file_name or "file", "doc", "файл"
    return None


@dataclass
class _Album:
    messages: list[Message] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    timer: asyncio.TimerHandle | None = None


class ToVK:
    album_delay = 1.5  # сколько ждать остальные части альбома Telegram

    def __init__(self, app: App) -> None:
        self.app = app
        self._albums: dict[str, _Album] = {}

    @property
    def _limit(self) -> int:
        return self.app.cfg.tg_download_limit_mb * 1024 * 1024

    def submit(self, bridge: Bridge, message: Message) -> None:
        key = ("vk", bridge.vk_peer_id)
        group_id = message.media_group_id
        if group_id is None:
            self.app.queues.put(key, partial(self._forward, bridge, message))
            return
        album = self._albums.get(group_id)
        if album is None:
            album = self._albums[group_id] = _Album()
            # Задача встаёт в очередь сразу, чтобы следующие сообщения не обогнали альбом.
            self.app.queues.put(key, partial(self._forward_album, bridge, album))
        album.messages.append(message)
        if album.timer:
            album.timer.cancel()
        album.timer = asyncio.get_running_loop().call_later(self.album_delay, self._close_album, group_id)

    def submit_edit(self, bridge: Bridge, message: Message) -> None:
        self.app.queues.put(("vk", bridge.vk_peer_id), partial(self._edit, bridge, message))

    def _close_album(self, group_id: str) -> None:
        album = self._albums.pop(group_id, None)
        if album:
            album.ready.set()

    async def _forward(self, bridge: Bridge, message: Message) -> None:
        attachment, note = await self._upload(bridge.vk_peer_id, message)
        body, reply_cmid = await self._compose(bridge, message, [note] if note else [])
        attachments = [attachment] if attachment else []
        cmid = await self._deliver(bridge.vk_peer_id, body, attachments, reply_cmid)
        await self.app.db.save_link(
            message.chat.id, message.message_id, bridge.vk_peer_id, cmid, "source", ",".join(attachments)
        )

    async def _forward_album(self, bridge: Bridge, album: _Album) -> None:
        await album.ready.wait()
        messages = sorted(album.messages, key=lambda m: m.message_id)[:10]
        attachments, notes = [], []
        for message in messages:
            attachment, note = await self._upload(bridge.vk_peer_id, message)
            if attachment:
                attachments.append(attachment)
            if note:
                notes.append(note)
        text_source = next((m for m in messages if m.caption), messages[0])
        body, reply_cmid = await self._compose(bridge, messages[0], notes, text_source)
        cmid = await self._deliver(bridge.vk_peer_id, body, attachments, reply_cmid)
        joined = ",".join(attachments)
        for message in messages:
            await self.app.db.save_link(message.chat.id, message.message_id, bridge.vk_peer_id, cmid, "source", joined)

    async def _edit(self, bridge: Bridge, message: Message) -> None:
        link = await self.app.db.link_by_tg(message.chat.id, message.message_id)
        if link is None or link.vk_peer_id != bridge.vk_peer_id:
            return
        body, _ = await self._compose(bridge, message, [])
        try:
            await self.app.vk.edit(link.vk_peer_id, link.vk_cmid, shorten(body, VK_TEXT_LIMIT), link.vk_attachments)
        except VKError as exc:
            log.warning("Не удалось изменить сообщение в VK: %s", exc)

    async def _compose(
        self, bridge: Bridge, message: Message, notes: list[str], text_source: Message | None = None
    ) -> tuple[str, int | None]:
        """Возвращает (текст для VK, conversation_message_id для ответа)."""
        lines = []
        reply_cmid = None
        target = real_reply(message)
        if target is not None:
            link = await self.app.db.link_by_tg(message.chat.id, target.message_id)
            if link and link.vk_peer_id == bridge.vk_peer_id:
                reply_cmid = link.vk_cmid
            else:
                quoted = shorten(target.text or target.caption or "", 80) or "вложение"
                lines.append(f"↩ В ответ на {tg_sender_name(target)}: «{quoted}»")

        label = forward_label(message)
        if label:
            lines.append(label)

        source = text_source or message
        if source.text:
            text = tg_text_with_links(source.text, source.entities)
        else:
            text = tg_text_with_links(source.caption or "", source.caption_entities)
        if text:
            lines.append(text)
        lines.extend(notes)

        header = vk_header(tg_sender_name(message), bridge.prefix)
        return sign(header, "\n".join(lines), self.app.cfg.newline), reply_cmid

    async def _upload(self, peer_id: int, message: Message) -> tuple[str | None, str | None]:
        """Загружает медиа сообщения в VK: (вложение, None) или (None, текстовая заметка)."""
        if message.sticker:
            return None, f"[стикер {message.sticker.emoji}]" if message.sticker.emoji else "[стикер]"
        if message.venue:
            venue = message.venue
            url = f"https://yandex.ru/maps/?pt={venue.location.longitude},{venue.location.latitude}&z=16&l=map"
            return None, f"📍 {venue.title}, {venue.address}: {url}"
        if message.location:
            loc = message.location
            return None, f"📍 Геопозиция: https://yandex.ru/maps/?pt={loc.longitude},{loc.latitude}&z=16&l=map"
        if message.contact:
            contact = message.contact
            name = f"{contact.first_name} {contact.last_name or ''}".strip()
            return None, f"👤 Контакт: {name}, {contact.phone_number}"
        if message.poll:
            options = "".join(f"\n• {option.text}" for option in message.poll.options)
            return None, f"📊 Опрос: {message.poll.question}{options}"
        if message.dice:
            return None, f"{message.dice.emoji} {message.dice.value}"

        spec = media_spec(message)
        if spec is None:
            return None, None
        file, filename, upload_type, label = spec
        if (file.file_size or 0) > self._limit:
            return None, f"[{label}: больше {self.app.cfg.tg_download_limit_mb} МБ, не передан]"
        try:
            buffer = await self.app.bot.download(file, timeout=120)
            data = buffer.read()
            if upload_type == "photo":
                return await self.app.vk.upload_photo(peer_id, data, filename), None
            return await self.app.vk.upload_doc(peer_id, data, filename, upload_type), None
        except Exception as exc:
            log.warning("Не удалось передать %s в VK: %s", label, exc)
            return None, f"[{label}: не удалось передать]"

    async def _deliver(self, peer_id: int, body: str, attachments: list[str], reply_cmid: int | None) -> int:
        chunks = split_text(body, VK_TEXT_LIMIT)
        cmid = await self.app.vk.send(peer_id, chunks[0], attachments, reply_cmid)
        for chunk in chunks[1:]:
            await self.app.vk.send(peer_id, chunk)
        return cmid
