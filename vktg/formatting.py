"""Преобразование текста между форматами VK и Telegram."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
VK_TEXT_LIMIT = 4096

# Упоминание VK: [id123|Иван], [club456|Сообщество]
VK_MENTION_RE = re.compile(r"\[(id|club|public|event)(\d+)\|([^\]\[\n]+)\]")
# Обращение к боту в начале сообщения: «[club123|@bot] /bridge» или «[club123|Бот], /bridge»
VK_BOT_MENTION_RE = re.compile(r"^\s*\[(?:club|public)\d+\|[^\]]*\]\s*[,:]?\s*")


def vk_text_to_html(text: str) -> str:
    parts = []
    pos = 0
    for match in VK_MENTION_RE.finditer(text):
        parts.append(html.escape(text[pos : match.start()], quote=False))
        kind, number, label = match.groups()
        parts.append(f'<a href="https://vk.com/{kind}{number}">{html.escape(label, quote=False)}</a>')
        pos = match.end()
    parts.append(html.escape(text[pos:], quote=False))
    return "".join(parts)


def vk_text_to_plain(text: str) -> str:
    return VK_MENTION_RE.sub(r"\3", text)


def sign(header: str, body: str, newline: bool) -> str:
    """«Имя: текст» или «Имя:\\nтекст»; без текста — только имя."""
    if not body:
        return header
    return header + (":\n" if newline else ": ") + body


def tg_header_plain(name: str, prefix: bool) -> str:
    return f"[VK] {name}" if prefix else name


def tg_header_html(name: str, prefix: bool) -> str:
    return f"<b>{html.escape(tg_header_plain(name, prefix), quote=False)}</b>"


def vk_header(name: str, prefix: bool) -> str:
    return f"[TG] {name}" if prefix else name


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_text(text: str, limit: int) -> list[str]:
    """Режет длинный текст на части не длиннее limit, по возможности по переносам строк и пробелам."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    chunks.append(text)
    return chunks


def parse_command(text: str) -> tuple[str, str] | None:
    """«/bridge KEY» → ("bridge", "KEY"). Понимает /cmd@bot и обращение к сообществу VK."""
    text = VK_BOT_MENTION_RE.sub("", text).strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    parts = text[1:].split(maxsplit=1)
    name = parts[0].split("@", 1)[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args


def tg_text_with_links(text: str, entities: Iterable[Any] | None) -> str:
    """Скрытые ссылки Telegram («текст» со ссылкой) превращает в «текст (url)» — VK их не поддерживает."""
    links = sorted((e for e in entities or () if e.type == "text_link" and e.url), key=lambda e: e.offset)
    if not links:
        return text
    # Смещения в Telegram считаются в UTF-16.
    encoded = text.encode("utf-16-le")
    for entity in reversed(links):
        end = (entity.offset + entity.length) * 2
        encoded = encoded[:end] + f" ({entity.url})".encode("utf-16-le") + encoded[end:]
    return encoded.decode("utf-16-le")
