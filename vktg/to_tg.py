"""Пересылка VK → Telegram."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo, ReplyParameters

from .app import App, tg_retry
from .db import Bridge, OutboxJob
from .formatting import (
    TG_CAPTION_LIMIT,
    TG_TEXT_LIMIT,
    shorten,
    sign,
    split_text,
    tg_header_html,
    tg_header_plain,
    vk_text_to_html,
    vk_text_to_plain,
)

log = logging.getLogger(__name__)

FWD_LIMIT = 5  # сколько пересланных сообщений VK показывать
_VISUAL = ("photo", "video")  # то, что можно собрать в альбом Telegram
_PHOTO_SIZE_ORDER = "smxopqryzw"  # типы размеров фото VK от меньшего к большему
_VIDEO_QUALITIES = ("mp4_720", "mp4_480", "mp4_360", "mp4_240", "mp4_1080")


@dataclass
class Media:
    kind: str  # photo | video | animation | voice | document
    data: bytes
    filename: str


def _chunks(items: list[Media], size: int) -> list[tuple[Media, ...]]:
    """Как itertools.batched, которого нет в Python до 3.12."""
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def best_photo_url(photo: dict[str, Any]) -> str | None:
    orig = photo.get("orig_photo") or {}
    if orig.get("url"):
        return orig["url"]
    sizes = photo.get("sizes") or []
    if not sizes:
        return None
    best = max(
        sizes,
        key=lambda s: (s.get("width", 0) * s.get("height", 0), _PHOTO_SIZE_ORDER.find(s.get("type", ""))),
    )
    return best.get("url") or best.get("src")


def sticker_url(sticker: dict[str, Any]) -> str | None:
    images = [i for i in sticker.get("images") or [] if i.get("url")]
    if not images:
        return None
    small = [i for i in images if i.get("width", 0) <= 512]
    return max(small or images, key=lambda i: i.get("width", 0))["url"]


def video_file_url(video: dict[str, Any]) -> str | None:
    """Прямая ссылка на mp4. Токену сообщества VK обычно её не отдаёт."""
    files = video.get("files") or {}
    return next((files[q] for q in _VIDEO_QUALITIES if files.get(q)), None)


class ToTG:
    def __init__(self, app: App) -> None:
        self.app = app
        app.outbox.register("vk_message", self._run_message)
        app.outbox.register("vk_edit", self._run_edit)

    @property
    def _limit(self) -> int:
        return self.app.cfg.tg_upload_limit_mb * 1024 * 1024

    async def submit(self, bridge: Bridge, msg: dict[str, Any]) -> None:
        await self.app.outbox.put(f"tg:{bridge.tg_chat_id}", "vk_message", msg)

    async def submit_edit(self, bridge: Bridge, msg: dict[str, Any]) -> None:
        await self.app.outbox.put(f"tg:{bridge.tg_chat_id}", "vk_edit", msg)

    async def _bridge(self, job: OutboxJob, msg: dict[str, Any]) -> Bridge | None:
        """Связка на момент отправки: пока сообщение ждало в очереди, её могли отключить."""
        bridge = await self.app.db.bridge_by_vk(msg.get("peer_id", 0))
        if bridge is None or not bridge.to_tg or job.target != f"tg:{bridge.tg_chat_id}":
            return None
        return bridge

    async def _run_message(self, job: OutboxJob) -> None:
        bridge = await self._bridge(job, job.payload)
        if bridge:
            await self.deliver(bridge, job.payload)

    async def _run_edit(self, job: OutboxJob) -> None:
        bridge = await self._bridge(job, job.payload)
        if bridge:
            await self.edit(bridge, job.payload)

    async def deliver(self, bridge: Bridge, msg: dict[str, Any]) -> None:
        media, notes = await self._attachments(msg, download=True)
        html_body, plain_body, reply_to = await self._compose(bridge, msg, notes)
        sent = await self._send(bridge.tg_chat_id, html_body, plain_body, media, reply_to)
        cmid = msg.get("conversation_message_id")
        if cmid is None:
            return
        for tg_msg_id, role in sent:
            await self.app.db.save_link(bridge.tg_chat_id, tg_msg_id, msg["peer_id"], cmid, role)

    async def edit(self, bridge: Bridge, msg: dict[str, Any]) -> None:
        links = await self.app.db.links_by_vk(msg["peer_id"], msg.get("conversation_message_id", 0))
        target = next(
            (l for l in links if l.tg_chat_id == bridge.tg_chat_id and l.role in ("text", "caption")), None
        )
        if target is None:
            return
        _, notes = await self._attachments(msg, download=False)
        html_body, plain_body, _ = await self._compose(bridge, msg, notes)
        limit = TG_CAPTION_LIMIT if target.role == "caption" else TG_TEXT_LIMIT
        body, mode = (html_body, "HTML") if len(html_body) <= limit else (shorten(plain_body, limit), None)
        try:
            if target.role == "caption":
                await tg_retry(
                    self.app.bot.edit_message_caption,
                    chat_id=target.tg_chat_id,
                    message_id=target.tg_msg_id,
                    caption=body,
                    parse_mode=mode,
                )
            else:
                await tg_retry(
                    self.app.bot.edit_message_text,
                    chat_id=target.tg_chat_id,
                    message_id=target.tg_msg_id,
                    text=body,
                    parse_mode=mode,
                )
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                log.warning("Не удалось изменить сообщение в Telegram: %s", exc)

    # --- разбор сообщения VK ---

    async def _attachments(self, msg: dict[str, Any], download: bool) -> tuple[list[Media], list[str]]:
        """Скачивает медиа; остальное превращает в текстовые заметки. download=False — только заметки."""
        media: list[Media] = []
        notes: list[str] = []
        text = msg.get("text") or ""

        async def add(url: str | None, kind: str, filename: str, label: str, fallback: str | None = None) -> None:
            if not url:
                notes.append(label)
                return
            if not download:
                return
            data = await self.app.fetch(url, self._limit)
            if data is None:
                notes.append(fallback or f"{label}: {url}")
            else:
                media.append(Media(kind, data, filename))

        for attachment in msg.get("attachments") or []:
            kind = attachment.get("type", "")
            obj = attachment.get(kind) or {}
            if kind == "photo":
                await add(best_photo_url(obj), "photo", "photo.jpg", "🖼 Фото")
            elif kind == "graffiti":
                await add(obj.get("url"), "photo", "graffiti.png", "🖼 Граффити")
            elif kind == "sticker":
                await add(sticker_url(obj), "photo", "sticker.png", "🙂 Стикер")
            elif kind == "doc":
                title = obj.get("title") or "file"
                ext = (obj.get("ext") or "").lower()
                filename = title if not ext or title.lower().endswith("." + ext) else f"{title}.{ext}"
                if obj.get("size", 0) > self._limit:
                    notes.append(f"📎 {title}: {obj.get('url', '')}")
                    continue
                await add(obj.get("url"), "animation" if ext == "gif" else "document", filename, f"📎 {title}")
            elif kind == "audio_message":
                await add(obj.get("link_ogg"), "voice", "voice.ogg", "🎤 Голосовое сообщение")
            elif kind == "video":
                title = obj.get("title") or "Видео"
                link = f"🎬 {title}: https://vk.com/video{obj.get('owner_id')}_{obj.get('id')}"
                file_url = video_file_url(obj)
                if file_url:
                    await add(file_url, "video", "video.mp4", link, fallback=link)
                else:
                    notes.append(link)
            elif kind == "audio":
                notes.append(f"🎵 {obj.get('artist', '')} — {obj.get('title', '')}")
            elif kind == "link":
                url = obj.get("url", "")
                if url and url not in text:
                    notes.append(f"🔗 {obj.get('title') or url}: {url}")
            elif kind == "wall":
                owner = obj.get("owner_id") or obj.get("to_id") or obj.get("from_id")
                notes.append(f"📰 Запись: https://vk.com/wall{owner}_{obj.get('id')}")
            elif kind == "poll":
                notes.append(f"📊 Опрос: {obj.get('question', '')}")
            elif kind == "market":
                notes.append(f"🛍 Товар: {obj.get('title', '')}")
            elif kind == "gift":
                notes.append("🎁 Подарок")
            elif kind == "story":
                notes.append("📖 История")
            else:
                notes.append(f"[вложение: {kind}]")
        return media, notes

    async def _compose(
        self, bridge: Bridge, msg: dict[str, Any], notes: list[str]
    ) -> tuple[str, str, int | None]:
        """Возвращает (HTML-текст, тот же текст без разметки, id сообщения Telegram для ответа)."""
        names = self.app.names
        html_lines: list[str] = []
        plain_lines: list[str] = []

        def add_plain(line: str) -> None:
            html_lines.append(html.escape(line, quote=False))
            plain_lines.append(line)

        reply_to = None
        reply = msg.get("reply_message")
        if reply:
            links = await self.app.db.links_by_vk(msg["peer_id"], reply.get("conversation_message_id", 0))
            links = [l for l in links if l.tg_chat_id == bridge.tg_chat_id]
            if links:
                reply_to = links[0].tg_msg_id
            else:
                who = await names.get(reply.get("from_id", 0))
                quoted = shorten(vk_text_to_plain(reply.get("text") or ""), 80) or "вложение"
                add_plain(f"↩ В ответ на {who}: «{quoted}»")

        text = msg.get("text") or ""
        if text:
            html_lines.append(vk_text_to_html(text))
            plain_lines.append(vk_text_to_plain(text))

        for fwd in (msg.get("fwd_messages") or [])[:FWD_LIMIT]:
            who = await names.get(fwd.get("from_id", 0))
            fwd_text = shorten(vk_text_to_plain(fwd.get("text") or ""), 300) or "[вложение]"
            add_plain(f"↪ {who}: {fwd_text}")

        for note in notes:
            add_plain(note)

        name = await names.get(msg.get("from_id", 0))
        newline = self.app.cfg.newline
        html_body = sign(tg_header_html(name, bridge.prefix), "\n".join(html_lines), newline)
        plain_body = sign(tg_header_plain(name, bridge.prefix), "\n".join(plain_lines), newline)
        return html_body, plain_body, reply_to

    # --- отправка в Telegram ---

    async def _send(
        self, chat_id: int, html_body: str, plain_body: str, media: list[Media], reply_to: int | None
    ) -> list[tuple[int, str]]:
        """Отправляет сообщение; возвращает [(message_id, role)] всех отправленных сообщений."""
        sent: list[tuple[int, str]] = []
        reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
        caption = html_body if len(html_body) <= TG_CAPTION_LIMIT else None

        if not media or caption is None:
            for i, message_id in enumerate(await self._send_text(chat_id, html_body, plain_body, reply)):
                sent.append((message_id, "text" if i == 0 else "extra"))
            reply, caption = None, None

        visual = [m for m in media if m.kind in _VISUAL]
        others = [m for m in media if m.kind not in _VISUAL]
        for group in _chunks(visual, 10):
            if len(group) == 1:
                ids = [await self._send_file(chat_id, group[0], caption, reply)]
            else:
                ids = await self._send_album(chat_id, group, caption, reply)
            sent.append((ids[0], "caption" if caption else "media"))
            sent.extend((message_id, "media") for message_id in ids[1:])
            reply, caption = None, None
        for item in others:
            sent.append((await self._send_file(chat_id, item, caption, reply), "caption" if caption else "media"))
            reply, caption = None, None
        return sent

    async def _send_text(
        self, chat_id: int, html_body: str, plain_body: str, reply: ReplyParameters | None
    ) -> list[int]:
        bot = self.app.bot
        if len(html_body) <= TG_TEXT_LIMIT:
            message = await tg_retry(
                bot.send_message, chat_id=chat_id, text=html_body, parse_mode="HTML", reply_parameters=reply
            )
            return [message.message_id]
        # Слишком длинно для одного сообщения: режем текст без разметки, чтобы не разорвать теги.
        ids = []
        for chunk in split_text(plain_body, TG_TEXT_LIMIT):
            message = await tg_retry(
                bot.send_message, chat_id=chat_id, text=chunk, parse_mode=None, reply_parameters=reply
            )
            ids.append(message.message_id)
            reply = None
        return ids

    async def _send_file(
        self, chat_id: int, item: Media, caption: str | None, reply: ReplyParameters | None
    ) -> int:
        bot = self.app.bot
        file = BufferedInputFile(item.data, filename=item.filename)
        common = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML" if caption else None,
            "reply_parameters": reply,
        }
        method, field = {
            "photo": (bot.send_photo, "photo"),
            "video": (bot.send_video, "video"),
            "animation": (bot.send_animation, "animation"),
            "voice": (bot.send_voice, "voice"),
            "document": (bot.send_document, "document"),
        }[item.kind]
        try:
            message = await tg_retry(method, **{field: file}, **common)
        except TelegramBadRequest as exc:
            if item.kind == "document":
                raise
            # Например, слишком вытянутое фото или запрет голосовых — отправим как файл.
            log.info("Telegram не принял %s (%s), отправляю как файл", item.kind, exc)
            message = await tg_retry(bot.send_document, document=file, **common)
        return message.message_id

    async def _send_album(
        self, chat_id: int, group: tuple[Media, ...], caption: str | None, reply: ReplyParameters | None
    ) -> list[int]:
        builders = {"photo": InputMediaPhoto, "video": InputMediaVideo}
        items = [
            builders[m.kind](
                media=BufferedInputFile(m.data, filename=m.filename),
                caption=caption if i == 0 else None,
                parse_mode="HTML" if i == 0 and caption else None,
            )
            for i, m in enumerate(group)
        ]
        messages = await tg_retry(self.app.bot.send_media_group, chat_id=chat_id, media=items, reply_parameters=reply)
        return [m.message_id for m in messages]
