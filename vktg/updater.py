"""Обновление бота из GitHub: git fetch → fast-forward → зависимости. Перезапуск делает __main__."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parents[1]
PIP_INSTALL = (sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", "requirements.txt")
_TIMEOUT = 300


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateCheck:
    current: str  # «a1b2c3d Описание коммита»
    pending: list[str]  # новые коммиты на GitHub, от старых к новым


@dataclass(frozen=True)
class UpdateResult:
    old: str
    new: str
    requirements_changed: bool


class Updater:
    def __init__(self, repo_dir: Path = REPO_DIR, install_cmd: Sequence[str] = PIP_INSTALL) -> None:
        self.repo_dir = repo_dir
        self._install_cmd = tuple(install_cmd)
        self._lock = asyncio.Lock()  # два нажатия кнопки подряд не запустят обновление дважды

    @property
    def available(self) -> bool:
        return (self.repo_dir / ".git").exists()

    async def _run(self, *cmd: str) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # не ждать ввода пароля, если доступа нет
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.repo_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            raise UpdateError(f"не найдена программа {cmd[0]}") from None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), _TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise UpdateError(f"{' '.join(cmd[:2])}: нет ответа за {_TIMEOUT} с") from None
        text = out.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise UpdateError(text or f"{' '.join(cmd[:2])} завершился с кодом {proc.returncode}")
        return text

    async def _git(self, *args: str) -> str:
        return await self._run("git", *args)

    def _require_git(self) -> None:
        if not self.available:
            raise UpdateError(
                "бот запущен не из git-репозитория. На сервере выполните deploy/setup-github.sh (см. README)."
            )

    async def version(self) -> str:
        self._require_git()
        return await self._git("log", "-1", "--format=%h %s")

    async def check(self) -> UpdateCheck:
        self._require_git()
        async with self._lock:
            await self._git("fetch", "--quiet")
            pending = await self._git("log", "--reverse", "--format=%h %s", "HEAD..@{upstream}")
            return UpdateCheck(await self.version(), pending.splitlines() if pending else [])

    async def apply(self) -> UpdateResult:
        self._require_git()
        async with self._lock:
            await self._git("fetch", "--quiet")
            old = await self._git("rev-parse", "HEAD")
            await self._git("merge", "--ff-only", "--quiet", "@{upstream}")
            new = await self._git("rev-parse", "HEAD")
            changed = (await self._git("diff", "--name-only", old, new)).splitlines() if new != old else []
            requirements_changed = "requirements.txt" in changed
            if requirements_changed:
                try:
                    await self._run(*self._install_cmd)
                except UpdateError as exc:
                    # Без новых зависимостей новый код может не запуститься — возвращаем прежнюю версию.
                    await self._git("reset", "--keep", old)
                    raise UpdateError(f"не удалось установить зависимости, версия возвращена:\n{exc}") from None
            log.info("Обновление из GitHub: %s → %s", old[:7], new[:7])
            return UpdateResult(old[:7], new[:7], requirements_changed)
