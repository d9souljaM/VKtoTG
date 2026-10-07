"""Очередь доставки в SQLite: сообщения переживают перезапуск бота и временные сбои сети.

У каждого чата-получателя (target) своя очередь. Задачи в ней выполняются строго по порядку,
чтобы сообщения не обгоняли друг друга. Задача удаляется из базы только после доставки.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from .db import OutboxJob, Storage
from .vk_api import RETRYABLE_CODES, VKError

log = logging.getLogger(__name__)

Handler = Callable[[OutboxJob], Awaitable[None]]

MAX_AGE = 24 * 3600  # сколько пытаться доставить сообщение при временных сбоях
MAX_RETRY_DELAY = 600  # самая длинная пауза между попытками


def is_transient(exc: BaseException) -> bool:
    """Временная ошибка (сеть, перегрузка API) — стоит повторить. Остальные повтором не исправить."""
    if isinstance(exc, VKError):
        return exc.code in RETRYABLE_CODES
    return isinstance(
        exc,
        (
            TelegramNetworkError,
            TelegramServerError,
            TelegramRetryAfter,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ConnectionError,
        ),
    )


class Outbox:
    def __init__(self, db: Storage, retry_base: float = 5.0) -> None:
        self._db = db
        self._retry_base = retry_base
        self._handlers: dict[str, Handler] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    async def put(self, target: str, kind: str, payload: Any, delay: float = 0.0) -> int:
        """Ставит задачу в очередь. delay — не выполнять раньше (например, пока собирается альбом)."""
        job_id = await self._db.outbox_add(target, kind, payload, time.time() + delay)
        self._kick(target)
        return job_id

    async def update(self, job_id: int, payload: Any, delay: float = 0.0) -> None:
        await self._db.outbox_update(job_id, payload, time.time() + delay)

    async def start(self) -> int:
        """Запускает доставку задач, оставшихся с прошлого запуска; возвращает их число."""
        for target in await self._db.outbox_targets():
            self._kick(target)
        return await self._db.outbox_count()

    def _kick(self, target: str) -> None:
        if target in self._workers:
            self._wakeups[target].set()
            return
        self._wakeups[target] = asyncio.Event()
        self._workers[target] = asyncio.create_task(self._work(target), name=f"outbox:{target}")

    async def _work(self, target: str) -> None:
        wakeup = self._wakeups[target]
        while True:
            wakeup.clear()
            try:
                job = await self._db.outbox_head(target)
                if job is None:
                    if wakeup.is_set():  # задача добавилась, пока шёл запрос
                        continue
                    break
                wait = job.not_before - time.time()
                if wait > 0:
                    # Ждём сбора альбома или паузы перед повтором; новая задача будит раньше.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(wakeup.wait(), wait)
                    continue
                await self._run(job)
            except Exception:
                log.exception("Ошибка очереди доставки %s", target)
                await asyncio.sleep(5)
        # Между проверкой wakeup и удалением нет await, поэтому _kick() не потеряет задачу.
        del self._workers[target]
        del self._wakeups[target]

    async def _run(self, job: OutboxJob) -> None:
        handler = self._handlers.get(job.kind)
        try:
            if handler is None:
                raise LookupError(f"нет обработчика для задачи {job.kind}")
            await handler(job)
        except Exception as exc:
            transient = is_transient(exc)
            if transient and time.time() - job.created_at < MAX_AGE:
                delay = min(self._retry_base * 2**job.attempts, MAX_RETRY_DELAY)
                log.warning("Не удалось доставить в %s (%s), повтор через %.0f с", job.target, exc, delay)
                await self._db.outbox_retry(job.id, time.time() + delay)
                return
            # Ошибку повтором не исправить (или сбой длится слишком долго): пропускаем сообщение,
            # чтобы оно не держало очередь чата.
            log.error(
                "Сообщение для %s не доставлено: %r", job.target, exc, exc_info=None if transient else exc
            )
        await self._db.outbox_done(job.id)

    async def join(self) -> None:
        """Ждёт, пока очереди опустеют (для тестов)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()))

    async def close(self) -> None:
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._wakeups.clear()
