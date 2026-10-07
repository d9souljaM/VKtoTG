"""Очереди доставки: по одной на чат-получатель, чтобы сообщения не обгоняли друг друга."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]


class KeyedQueues:
    def __init__(self, idle_timeout: float = 300.0) -> None:
        self._idle_timeout = idle_timeout
        self._queues: dict[Hashable, asyncio.Queue[Job]] = {}
        self._workers: dict[Hashable, asyncio.Task[None]] = {}

    def put(self, key: Hashable, job: Job) -> None:
        queue = self._queues.get(key)
        if queue is None:
            queue = self._queues[key] = asyncio.Queue()
            self._workers[key] = asyncio.create_task(self._work(key, queue), name=f"queue:{key}")
        queue.put_nowait(job)

    async def _work(self, key: Hashable, queue: asyncio.Queue[Job]) -> None:
        while True:
            try:
                job = await asyncio.wait_for(queue.get(), self._idle_timeout)
            except asyncio.TimeoutError:
                if queue.empty():
                    # Между проверкой и удалением нет await, поэтому put() не потеряет задачу.
                    del self._queues[key]
                    del self._workers[key]
                    return
                continue
            try:
                await job()
            except Exception:
                log.exception("Не удалось доставить сообщение в %s", key)
            finally:
                queue.task_done()

    async def join(self) -> None:
        """Ждёт, пока все поставленные задачи будут выполнены."""
        await asyncio.gather(*(queue.join() for queue in list(self._queues.values())))

    async def close(self) -> None:
        workers = list(self._workers.values())
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()
