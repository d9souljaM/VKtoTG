"""Настройки из переменных окружения (файл .env подхватывается автоматически)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    tg_token: str
    vk_token: str
    tg_proxy: str | None = None  # прокси только для Telegram, например http://127.0.0.1:12334
    vk_group_id: int | None = None
    vk_auto_setup: bool = True
    vk_api_version: str = "5.199"
    db_path: str = "bridge.db"
    log_level: str = "INFO"
    message_format: str = "inline"  # inline: «Имя: текст», newline: «Имя:\nтекст»
    tg_download_limit_mb: int = 20  # Bot API отдаёт файлы до 20 МБ
    tg_upload_limit_mb: int = 50  # Bot API принимает файлы до 50 МБ
    mapping_ttl_days: int = 30

    @property
    def newline(self) -> bool:
        return self.message_format == "newline"


def _int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должно быть числом, получено {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_config() -> Config:
    load_dotenv()

    tg_token = os.getenv("TG_TOKEN", "").strip()
    vk_token = os.getenv("VK_TOKEN", "").strip()
    missing = [name for name, value in (("TG_TOKEN", tg_token), ("VK_TOKEN", vk_token)) if not value]
    if missing:
        raise ConfigError(
            "Не заданы переменные окружения: " + ", ".join(missing) + ". Скопируйте .env.example в .env и заполните."
        )

    message_format = os.getenv("MESSAGE_FORMAT", "inline").strip().lower()
    if message_format not in ("inline", "newline"):
        raise ConfigError("MESSAGE_FORMAT может быть только inline или newline")

    tg_proxy = os.getenv("TG_PROXY", "").strip() or None
    if tg_proxy and not tg_proxy.lower().startswith(("http://", "https://", "socks4://", "socks5://")):
        raise ConfigError("TG_PROXY должен начинаться с http://, https://, socks4:// или socks5://")

    group_id = _int("VK_GROUP_ID", None)

    return Config(
        tg_token=tg_token,
        vk_token=vk_token,
        tg_proxy=tg_proxy,
        vk_group_id=abs(group_id) if group_id else None,
        vk_auto_setup=_bool("VK_AUTO_SETUP", True),
        db_path=os.getenv("DB_PATH", "bridge.db").strip() or "bridge.db",
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        message_format=message_format,
        tg_download_limit_mb=_int("TG_DOWNLOAD_LIMIT_MB", 20),
        tg_upload_limit_mb=_int("TG_UPLOAD_LIMIT_MB", 50),
        mapping_ttl_days=_int("MAPPING_TTL_DAYS", 30),
    )
