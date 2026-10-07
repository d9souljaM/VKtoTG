"""Пересылка Telegram → VK."""

from __future__ import annotations

import logging
from typing import Any

from aiogram.types import Message

from .app import App
from .db import Bridge, OutboxJob
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


def dump_message(message: Message) -> dict[str, Any]:
    """Сообщение Telegram в JSON для очереди доставки."""
    return message.model_dump(mode="json", exclude_none=True)


class ToVK:
    album_delay = 1.5  # сколько ждать остальные части альбома Telegram

    def __init__(self, app: App) -> None:
        self.app = app
        # media_group_id → (id задачи в очереди, части альбома): Telegram присылает их по одной.
        self._albums: dict[str, tuple[int, list[Message]]] = {}
        app.outbox.register("tg_message", self._run_message)
        app.outbox.register("tg_album", self._run_album)
        app.outbox.register("tg_edit", self._run_edit)

    @property
    def _limit(self) -> int:
        return self.app.cfg.tg_download_limit_mb * 1024 * 1024

    async def submit(self, bridge: Bridge, message: Message) -> None:
        target = f"vk:{bridge.vk_peer_id}"
        group_id = message.media_group_id
        if group_id is None:
            await self.app.outbox.put(target, "tg_message", dump_message(message))
            return
        album = self._albums.get(group_id)
        if album is None:
            # Задача встаёт в очередь с первой частью альбома, чтобы следующие сообщения
            # не обогнали его; остальные части дописываются в неё по мере прихода.
            job_id = await self.app.outbox.put(target, "tg_album", [dump_message(message)], delay=self.album_delay)
            self._albums[group_id] = (job_id, [message])
            return
        job_id, messages = album
        messages.append(message)
        await self.app.outbox.update(job_id, [dump_message(m) for m in messages], delay=self.album_delay)

    async def submit_edit(self, bridge: Bridge, message: Message) -> None:
        await self.app.outbox.put(f"vk:{bridge.vk_peer_id}", "tg_edit", dump_message(message))

    async def _bridge(self, job: OutboxJob, message: Message) -> Bridge | None:
        """Связка на момент отправки: пока сообщение ждало в очереди, её могли отключить."""
        bridge = await self.app.db.bridge_by_tg(message.chat.id)
        if bridge is None or not bridge.to_vk or job.target != f"vk:{bridge.vk_peer_id}":
            return None
        return bridge

    async def _run_message(self, job: OutboxJob) -> None:
        message = Message.model_validate(job.payload)
        bridge = await self._bridge(job, message)
        if bridge:
            await self._forward(bridge, message, job.nonce)

    async def _run_album(self, job: OutboxJob) -> None:
        messages = [Message.model_validate(m) for m in job.payload]
        album = self._albums.pop(messages[0].media_group_id or "", None)
        if album and album[0] == job.id:
            messages = album[1]  # в памяти могут быть части, которые ещё не записаны в базу
        bridge = await self._bridge(job, messages[0])
        if bridge:
            await self._forward_album(bridge, messages, job.nonce)

    async def _run_edit(self, job: OutboxJob) -> None:
        message = Message.model_validate(job.payload)
        bridge = await self._bridge(job, message)
        if bridge:
            await self._edit(bridge, message)

    async def _forward(self, bridge: Bridge, message: Message, nonce: int) -> None:
        attachment, note = await self._upload(bridge.vk_peer_id, message)
        body, reply_cmid = await self._compose(bridge, message, [note] if note else [])
        attachments = [attachment] if attachment else []
        cmid = await self._deliver(bridge.vk_peer_id, body, attachments, reply_cmid, nonce)
        await self.app.db.save_link(
            message.chat.id, message.message_id, bridge.vk_peer_id, cmid, "source", ",".join(attachments)
        )

    async def _forward_album(self, bridge: Bridge, messages: list[Message], nonce: int) -> None:
        messages = sorted(messages, key=lambda m: m.message_id)[:10]
        attachments, notes = [], []
        for message in messages:
            attachment, note = await self._upload(bridge.vk_peer_id, message)
            if attachment:
                attachments.append(attachment)
            if note:
                notes.append(note)
        text_source = next((m for m in messages if m.caption), messages[0])
        body, reply_cmid = await self._compose(bridge, messages[0], notes, text_source)
        cmid = await self._deliver(bridge.vk_peer_id, body, attachments, reply_cmid, nonce)
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

    async def _deliver(
        self, peer_id: int, body: str, attachments: list[str], reply_cmid: int | None, nonce: int
    ) -> int:
        # random_id из задачи очереди: при повторе после сбоя VK не создаст дубль уже отправленной части.
        chunks = split_text(body, VK_TEXT_LIMIT)
        cmid = await self.app.vk.send(peer_id, chunks[0], attachments, reply_cmid, random_id=nonce)
        for i, chunk in enumerate(chunks[1:], start=1):
            await self.app.vk.send(peer_id, chunk, random_id=nonce + i)
        return cmid
