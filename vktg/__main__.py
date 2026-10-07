"""Точка входа: python -m vktg"""

from __future__ import annotations

import asyncio
import logging
import sys
from functools import partial

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats

from .app import App, VKNames, fetch_url
from .config import Config, ConfigError, load_config
from .db import Storage
from .linking import KEY_TTL
from .queues import Outbox
from .tg_handlers import build_router
from .to_tg import ToTG
from .to_vk import ToVK
from .vk_api import VKApi, VKError
from .vk_handlers import FatalVKError, VKHandlers

log = logging.getLogger("vktg")

GROUP_COMMANDS = [
    BotCommand(command="bridge", description="Получить ключ или связать чат"),
    BotCommand(command="unbridge", description="Отключить мост"),
    BotCommand(command="status", description="Состояние моста"),
    BotCommand(command="help", description="Инструкция"),
]


async def setup_vk_group(vk: VKApi, group_id: int) -> None:
    """Включает в сообществе то, без чего мост не работает. Нужны права «Управление сообществом»."""
    steps = (
        (
            "groups.setSettings",
            {"group_id": group_id, "messages": 1, "bots_capabilities": 1, "bots_add_to_chat": 1},
            "сообщения сообщества и возможности ботов",
        ),
        (
            "groups.setLongPollSettings",
            {"group_id": group_id, "enabled": 1, "api_version": vk.version, "message_new": 1, "message_edit": 1},
            "Bots Long Poll API",
        ),
    )
    for method, params, what in steps:
        try:
            await vk.call(method, **params)
        except VKError as exc:
            log.warning("Не удалось автоматически включить %s (%s). Включите вручную — см. README.", what, exc)


async def cleanup_loop(app: App) -> None:
    while True:
        try:
            await app.db.purge_keys(KEY_TTL)
            await app.db.purge_links(app.cfg.mapping_ttl_days * 86400)
        except Exception:
            log.exception("Ошибка очистки базы")
        await asyncio.sleep(3600)


async def run(cfg: Config) -> None:
    db = await Storage.open(cfg.db_path)
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    bot = Bot(cfg.tg_token, session=AiohttpSession(proxy=cfg.tg_proxy) if cfg.tg_proxy else None)
    outbox = Outbox(db)
    background: list[asyncio.Task[None]] = []
    try:
        vk = VKApi(cfg.vk_token, http, cfg.vk_api_version)
        group = await vk.group_info(cfg.vk_group_id)
        group_id = group["id"]
        if cfg.vk_auto_setup:
            await setup_vk_group(vk, group_id)
        me = await bot.get_me()

        app = App(
            cfg=cfg,
            db=db,
            bot=bot,
            vk=vk,
            group_id=group_id,
            group_name=group.get("name", ""),
            group_screen_name=group.get("screen_name") or f"club{group_id}",
            tg_username=me.username or "",
            outbox=outbox,
            names=VKNames(vk),
            fetch=partial(fetch_url, http),
        )
        dp = Dispatcher()
        dp.include_router(build_router())
        try:
            await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        except Exception as exc:
            log.warning("Не удалось задать меню команд Telegram: %s", exc)

        to_tg, to_vk = ToTG(app), ToVK(app)  # регистрируют обработчики очереди — до outbox.start()
        pending = await outbox.start()
        if pending:
            log.info("В очереди %d сообщений с прошлого запуска — доставляю", pending)

        vk_task = asyncio.create_task(VKHandlers(app, to_tg).run(), name="vk-longpoll")
        background += [vk_task, asyncio.create_task(cleanup_loop(app), name="cleanup")]

        def on_vk_stopped(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception():
                log.critical("VK остановлен: %s", task.exception())
                asyncio.create_task(dp.stop_polling())

        vk_task.add_done_callback(on_vk_stopped)

        log.info("Мост запущен: Telegram @%s ↔ VK «%s» (id %s)", app.tg_username, app.group_name, group_id)
        # handle_as_tasks=False: апдейты Telegram разбираются по порядку, а медленная
        # загрузка файлов идёт в очередях доставки и не задерживает остальные чаты.
        await dp.start_polling(
            bot,
            app=app,
            to_vk=to_vk,
            handle_as_tasks=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
        if vk_task.done() and not vk_task.cancelled() and isinstance(vk_task.exception(), FatalVKError):
            raise vk_task.exception()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await outbox.close()  # недоставленное остаётся в базе до следующего запуска
        await http.close()
        await bot.session.close()
        await db.close()


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except (FatalVKError, VKError) as exc:
        log.critical("%s", exc)
        sys.exit(1)
    except TelegramUnauthorizedError:
        log.critical("Telegram отклонил TG_TOKEN. Проверьте токен от @BotFather.")
        sys.exit(1)
    except TelegramNetworkError as exc:
        if cfg.tg_proxy:
            hint = f"Проверьте, что VPN включён и прокси {cfg.tg_proxy} работает."
        else:
            hint = (
                "Если Telegram открывается только через VPN, укажите в .env адрес прокси VPN-клиента, "
                "например TG_PROXY=http://127.0.0.1:12334"
            )
        log.critical("Не удаётся подключиться к Telegram (%s). %s", exc.message, hint)
        sys.exit(1)
    except aiohttp.ClientError as exc:
        log.critical("Не удаётся подключиться к VK: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
