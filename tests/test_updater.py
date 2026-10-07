import asyncio
import dataclasses
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from vktg import tg_handlers
from vktg.updater import UpdateCheck, Updater, UpdateError, UpdateResult


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q", "origin", "HEAD:main")


@pytest.fixture
def repos(tmp_path):
    """GitHub (bare-репозиторий), рабочая копия разработчика и копия на сервере."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    commit(dev, {"requirements.txt": "aiogram\n", "bot.py": "v1\n"}, "Первая версия")
    server = tmp_path / "server"
    git(tmp_path, "clone", "-q", str(origin), str(server))
    (server / ".env").write_text("TG_TOKEN=secret\n", encoding="utf-8")  # как на сервере: не в git
    return dev, server


def test_check_lists_new_commits_and_apply_fast_forwards(repos):
    dev, server = repos
    commit(dev, {"bot.py": "v2\n"}, "Кнопка обновления")
    commit(dev, {"bot.py": "v3\n"}, "Исправление")

    async def scenario():
        updater = Updater(server, install_cmd=[sys.executable, "-c", "raise SystemExit('pip не нужен')"])
        check = await updater.check()
        assert [c.split(" ", 1)[1] for c in check.pending] == ["Кнопка обновления", "Исправление"]
        assert check.current.endswith("Первая версия")

        result = await updater.apply()
        assert result.old != result.new and not result.requirements_changed
        assert (server / "bot.py").read_text(encoding="utf-8") == "v3\n"
        assert (server / ".env").exists()  # неотслеживаемые файлы на месте
        assert (await updater.check()).pending == []

    asyncio.run(scenario())


def test_requirements_change_runs_install(repos, tmp_path):
    dev, server = repos
    commit(dev, {"requirements.txt": "aiogram\naiohttp-socks\n"}, "Новая зависимость")
    marker = tmp_path / "installed"

    async def scenario():
        updater = Updater(server, install_cmd=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])
        result = await updater.apply()
        assert result.requirements_changed
        assert marker.exists()

    asyncio.run(scenario())


def test_failed_install_rolls_back(repos):
    dev, server = repos
    before = git(server, "rev-parse", "HEAD")
    commit(dev, {"requirements.txt": "broken-package\n"}, "Сломанная зависимость")

    async def scenario():
        updater = Updater(server, install_cmd=[sys.executable, "-c", "raise SystemExit(1)"])
        with pytest.raises(UpdateError, match="версия возвращена"):
            await updater.apply()

    asyncio.run(scenario())
    assert git(server, "rev-parse", "HEAD") == before
    assert (server / "requirements.txt").read_text(encoding="utf-8") == "aiogram\n"


def test_without_git_repository(tmp_path):
    async def scenario():
        with pytest.raises(UpdateError, match="не из git-репозитория"):
            await Updater(tmp_path).check()

    asyncio.run(scenario())


# --- кнопка обновления в Telegram ---

OWNER = User(id=42, is_bot=False, first_name="Владелец")
STRANGER = User(id=7, is_bot=False, first_name="Гость")
DM = Chat(id=42, type="private")


class FakeUpdater:
    def __init__(self, pending=("b2b2b2b Кнопка обновления",)):
        self.pending = list(pending)
        self.applied = False

    async def check(self):
        return UpdateCheck("a1a1a1a Первая версия", self.pending)

    async def apply(self):
        self.applied = True
        return UpdateResult("a1a1a1a", "b2b2b2b", requirements_changed=False)


async def owner_app(make_app):
    app = await make_app()
    app.cfg = dataclasses.replace(app.cfg, tg_owner_id=OWNER.id)
    return app


def update_command(user):
    return Message(message_id=1, date=datetime(2026, 1, 1), chat=Chat(id=user.id, type="private"),
                   from_user=user, text="/update")


def button(user, data):
    message = Message(message_id=500, date=datetime(2026, 1, 1), chat=DM, text="…")
    return CallbackQuery(id="cb", from_user=user, chat_instance="ci", data=data, message=message)


def test_update_shows_new_commits_with_button(make_app):
    async def scenario():
        app = await owner_app(make_app)
        await tg_handlers.on_update(update_command(OWNER), app, FakeUpdater())
        name, kwargs = app.bot.calls[-1]
        assert name == "edit_message_text"
        assert "• b2b2b2b Кнопка обновления" in kwargs["text"]
        buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        assert buttons == [tg_handlers.UPDATE_APPLY, tg_handlers.UPDATE_CANCEL]

        await tg_handlers.on_update(update_command(OWNER), app, FakeUpdater(pending=()))
        assert app.bot.calls[-1][1]["text"].startswith("✅ Обновлений нет")
        assert app.bot.calls[-1][1]["reply_markup"] is None

    asyncio.run(scenario())


def test_update_button_applies_and_requests_restart(make_app):
    async def scenario():
        app = await owner_app(make_app)
        updater = FakeUpdater()
        await tg_handlers.on_update_button(button(OWNER, tg_handlers.UPDATE_APPLY), app, updater)

        assert updater.applied and app.restart.is_set()
        assert "a1a1a1a → b2b2b2b" in app.bot.calls[-1][1]["text"]
        notice = await app.db.kv_pop(tg_handlers.UPDATE_NOTICE_KEY)
        assert json.loads(notice) == {"chat_id": DM.id}

    asyncio.run(scenario())


def test_only_owner_can_update(make_app):
    async def scenario():
        app = await owner_app(make_app)
        updater = FakeUpdater()
        await tg_handlers.on_update_button(button(STRANGER, tg_handlers.UPDATE_APPLY), app, updater)
        assert not updater.applied and not app.restart.is_set()
        name, kwargs = app.bot.calls[-1]
        assert name == "answer_callback_query" and kwargs["show_alert"]

        await tg_handlers.on_update(update_command(STRANGER), app, updater)
        name, kwargs = app.bot.calls[-1]
        assert name == "send_message" and kwargs["text"].startswith("🌉 Мост")  # чужому — только справка

    asyncio.run(scenario())
