import asyncio
from datetime import datetime

import aiohttp
from aiogram.types import Chat, Message, PhotoSize, Sticker, User

from vktg import tg_handlers
from vktg.queues import Outbox
from vktg.to_tg import ToTG
from vktg.to_vk import ToVK
from vktg.vk_api import VKError
from vktg.vk_handlers import VKHandlers

VK_PEER = 2_000_000_001
TG_CHAT = -1001
CHAT = Chat(id=TG_CHAT, type="supergroup", title="Группа")
USER = User(id=7, is_bot=False, first_name="Анна", last_name="Смирнова")


async def bridged(make_app, **kwargs):
    app = await make_app(**kwargs)
    await app.db.add_bridge(TG_CHAT, VK_PEER)
    return app


def vk_update(kind="message_new", **message):
    message.setdefault("peer_id", VK_PEER)
    message.setdefault("from_id", 5)
    return {"type": kind, "object": {"message": message} if kind == "message_new" else message}


def vk_photo(url):
    return {"type": "photo", "photo": {"sizes": [
        {"type": "s", "url": url + "?small", "width": 75, "height": 75},
        {"type": "x", "url": url, "width": 604, "height": 604},
    ]}}


def tg_msg(message_id, **fields):
    return Message(message_id=message_id, date=datetime(2026, 1, 1), chat=CHAT, from_user=USER, **fields)


def photo_sizes(file_id):
    return [
        PhotoSize(file_id=file_id + "-small", file_unique_id="s", width=90, height=90),
        PhotoSize(file_id=file_id, file_unique_id="b", width=800, height=800),
    ]


# --- VK → Telegram ---


def test_vk_text_goes_to_telegram(make_app):
    async def scenario():
        app = await bridged(make_app)
        handlers = VKHandlers(app, ToTG(app))
        await handlers.handle(vk_update(text="Привет, [id9|Оля] <3", conversation_message_id=10))
        await app.outbox.join()

        name, kwargs = app.bot.calls[-1]
        assert name == "send_message"
        assert kwargs["text"] == '<b>[VK] Иван #5</b>: Привет, <a href="https://vk.com/id9">Оля</a> &lt;3'
        assert kwargs["parse_mode"] == "HTML" and kwargs["chat_id"] == TG_CHAT
        links = await app.db.links_by_vk(VK_PEER, 10)
        assert [(link.tg_msg_id, link.role) for link in links] == [(1001, "text")]

    asyncio.run(scenario())


def test_vk_photos_become_album_with_caption(make_app):
    async def scenario():
        app = await bridged(make_app)
        handlers = VKHandlers(app, ToTG(app))
        attachments = [vk_photo("https://vk/1.jpg"), vk_photo("https://vk/2.jpg")]
        await handlers.handle(vk_update(text="отпуск", attachments=attachments, conversation_message_id=11))
        await app.outbox.join()

        name, kwargs = app.bot.calls[-1]
        assert name == "send_media_group" and len(kwargs["media"]) == 2
        assert kwargs["media"][0].caption == "<b>[VK] Иван #5</b>: отпуск"
        assert kwargs["media"][1].caption is None
        assert kwargs["media"][0].media.data == b"img:https://vk/1.jpg"  # взят самый большой размер
        roles = [link.role for link in await app.db.links_by_vk(VK_PEER, 11)]
        assert roles == ["caption", "media"]

    asyncio.run(scenario())


def test_vk_reply_and_edit_are_mirrored(make_app):
    async def scenario():
        app = await bridged(make_app)
        handlers = VKHandlers(app, ToTG(app))
        await handlers.handle(vk_update(text="раз", conversation_message_id=10))
        reply_message = {"conversation_message_id": 10, "from_id": 5, "text": "раз"}
        await handlers.handle(vk_update(text="два", conversation_message_id=11, reply_message=reply_message))
        await handlers.handle(vk_update("message_edit", text="раз (испр.)", conversation_message_id=10))
        await app.outbox.join()

        (_, first), (_, second), (edit_name, edit) = app.bot.calls
        assert first["reply_parameters"] is None
        assert second["reply_parameters"].message_id == 1001
        assert edit_name == "edit_message_text"
        assert edit["message_id"] == 1001 and edit["text"].endswith("раз (испр.)")

    asyncio.run(scenario())


def test_vk_unavailable_media_becomes_links(make_app):
    async def scenario():
        app = await bridged(make_app, fetched={"https://vk/broken.jpg": None})
        handlers = VKHandlers(app, ToTG(app))
        attachments = [
            {"type": "video", "video": {"owner_id": -1, "id": 2, "title": "Клип"}},
            vk_photo("https://vk/broken.jpg"),
        ]
        await handlers.handle(vk_update(text="", attachments=attachments, conversation_message_id=12))
        await app.outbox.join()

        name, kwargs = app.bot.calls[-1]
        assert name == "send_message"
        assert "🎬 Клип: https://vk.com/video-1_2" in kwargs["text"]
        assert "🖼 Фото: https://vk/broken.jpg" in kwargs["text"]

    asyncio.run(scenario())


def test_vk_ignores_own_messages_and_respects_direction(make_app):
    async def scenario():
        app = await bridged(make_app)
        handlers = VKHandlers(app, ToTG(app))
        await handlers.handle(vk_update(from_id=-77, text="эхо", conversation_message_id=13))
        bridge = await app.db.bridge_by_vk(VK_PEER)
        await app.db.set_direction(bridge.id, "tg2vk")
        await handlers.handle(vk_update(text="не пересылать", conversation_message_id=14))
        await app.outbox.join()
        assert app.bot.calls == []

    asyncio.run(scenario())


# --- Telegram → VK ---


def test_tg_text_goes_to_vk(make_app):
    async def scenario():
        app = await bridged(make_app)
        await tg_handlers.on_group_message(tg_msg(1, text="Привет"), app, ToVK(app))
        await app.outbox.join()

        sent = app.vk.sent[-1]
        assert (sent["peer_id"], sent["text"], sent["attachments"]) == (VK_PEER, "[TG] Анна Смирнова: Привет", [])
        link = await app.db.link_by_tg(TG_CHAT, 1)
        assert (link.vk_cmid, link.role) == (sent["cmid"], "source")

    asyncio.run(scenario())


def test_tg_reply_to_mirrored_message_becomes_vk_reply(make_app):
    async def scenario():
        app = await bridged(make_app)
        await app.db.save_link(TG_CHAT, 50, VK_PEER, 33, "text")  # копия сообщения из VK
        original = tg_msg(50, text="исходное")
        await tg_handlers.on_group_message(tg_msg(2, text="ответ", reply_to_message=original), app, ToVK(app))
        unknown = tg_msg(60, text="старое сообщение до моста")
        await tg_handlers.on_group_message(tg_msg(3, text="ещё", reply_to_message=unknown), app, ToVK(app))
        await app.outbox.join()

        assert app.vk.sent[0]["reply_cmid"] == 33
        assert app.vk.sent[1]["reply_cmid"] is None
        assert "↩ В ответ на Анна Смирнова: «старое сообщение до моста»" in app.vk.sent[1]["text"]

    asyncio.run(scenario())


def test_tg_photo_is_uploaded_to_vk(make_app):
    async def scenario():
        app = await bridged(make_app)
        message = tg_msg(4, photo=photo_sizes("big"), caption="фото")
        await tg_handlers.on_group_message(message, app, ToVK(app))
        await app.outbox.join()

        assert app.vk.uploads == [("photo", b"file:big", "photo.jpg")]
        assert app.vk.sent[-1]["attachments"] == ["photo-77_1"]
        assert app.vk.sent[-1]["text"] == "[TG] Анна Смирнова: фото"

    asyncio.run(scenario())


def test_tg_album_becomes_one_vk_message(make_app):
    async def scenario():
        app = await bridged(make_app)
        to_vk = ToVK(app)
        to_vk.album_delay = 0.05
        first = tg_msg(5, photo=photo_sizes("a"), media_group_id="g1")
        second = tg_msg(6, photo=photo_sizes("b"), media_group_id="g1", caption="подпись")
        after = tg_msg(7, text="после альбома")
        for message in (first, second, after):
            await tg_handlers.on_group_message(message, app, to_vk)
        await app.outbox.join()

        album, text = app.vk.sent
        assert album["attachments"] == ["photo-77_1", "photo-77_2"]
        assert album["text"] == "[TG] Анна Смирнова: подпись"
        assert text["text"].endswith("после альбома")  # порядок сохранён
        links = [await app.db.link_by_tg(TG_CHAT, i) for i in (5, 6)]
        assert {link.vk_cmid for link in links} == {album["cmid"]}

    asyncio.run(scenario())


def test_tg_edit_updates_vk_message(make_app):
    async def scenario():
        app = await bridged(make_app)
        to_vk = ToVK(app)
        await tg_handlers.on_group_message(tg_msg(8, photo=photo_sizes("p"), caption="было"), app, to_vk)
        await tg_handlers.on_group_edit(tg_msg(8, photo=photo_sizes("p"), caption="стало"), app, to_vk)
        await app.outbox.join()

        cmid = app.vk.sent[-1]["cmid"]
        assert app.vk.edits == [(VK_PEER, cmid, "[TG] Анна Смирнова: стало", "photo-77_1")]

    asyncio.run(scenario())


def test_tg_sticker_becomes_text(make_app):
    async def scenario():
        app = await bridged(make_app)
        sticker = Sticker(
            file_id="st", file_unique_id="st", type="regular", width=512, height=512,
            is_animated=False, is_video=False, emoji="😂",
        )
        await tg_handlers.on_group_message(tg_msg(9, sticker=sticker), app, ToVK(app))
        await app.outbox.join()
        assert app.vk.sent[-1]["text"] == "[TG] Анна Смирнова: [стикер 😂]"

    asyncio.run(scenario())


# --- очередь доставки ---


def test_transient_error_is_retried_without_duplicates(make_app):
    async def scenario():
        app = await bridged(make_app)
        app.vk.fail_sends = [aiohttp.ClientConnectionError("сеть пропала")]
        await tg_handlers.on_group_message(tg_msg(1, text="раз"), app, ToVK(app))
        await app.outbox.join()

        assert [s["text"] for s in app.vk.sent] == ["[TG] Анна Смирнова: раз"]
        first, second = app.vk.random_ids
        assert first == second  # повтор с тем же random_id: VK не создаст дубль
        assert await app.db.outbox_count() == 0

    asyncio.run(scenario())


def test_permanent_error_does_not_block_queue(make_app):
    async def scenario():
        app = await bridged(make_app)
        app.vk.fail_sends = [VKError(917, "нет доступа к беседе", "messages.send")]
        to_vk = ToVK(app)
        await tg_handlers.on_group_message(tg_msg(1, text="раз"), app, to_vk)
        await tg_handlers.on_group_message(tg_msg(2, text="два"), app, to_vk)
        await app.outbox.join()

        assert [s["text"] for s in app.vk.sent] == ["[TG] Анна Смирнова: два"]
        assert len(app.vk.random_ids) == 2  # первое сообщение не повторялось
        assert await app.db.outbox_count() == 0

    asyncio.run(scenario())


class CrashedOutbox(Outbox):
    """Очередь бота, который упал сразу после приёма сообщений: задачи пишутся в базу, но не доставляются."""

    def _kick(self, target):
        pass


def test_queue_survives_restart(make_app):
    async def scenario():
        app = await bridged(make_app)
        app.outbox = CrashedOutbox(app.db)
        to_vk = ToVK(app)
        to_vk.album_delay = 0.05
        question = tg_msg(50, text="вопрос")  # ответ проверяет, что вложенные объекты переживают JSON
        await tg_handlers.on_group_message(
            tg_msg(1, photo=photo_sizes("a"), media_group_id="g", reply_to_message=question), app, to_vk
        )
        await tg_handlers.on_group_message(
            tg_msg(2, photo=photo_sizes("b"), media_group_id="g", caption="альбом", reply_to_message=question),
            app, to_vk,
        )
        await tg_handlers.on_group_message(tg_msg(3, text="после"), app, to_vk)
        assert app.vk.sent == []

        # Новый запуск: та же база, новая очередь и новые обработчики без памяти об альбоме.
        app.outbox = Outbox(app.db, retry_base=0.01)
        ToVK(app)
        assert await app.outbox.start() == 2
        await app.outbox.join()

        album, text = app.vk.sent
        assert album["attachments"] == ["photo-77_1", "photo-77_2"]
        assert album["text"] == "[TG] Анна Смирнова: ↩ В ответ на Анна Смирнова: «вопрос»\nальбом"
        assert text["text"] == "[TG] Анна Смирнова: после"

    asyncio.run(scenario())


def test_vk_message_waits_in_queue_while_bridge_removed(make_app):
    async def scenario():
        app = await bridged(make_app)
        handlers = VKHandlers(app, ToTG(app))
        bridge = await app.db.bridge_by_vk(VK_PEER)
        await handlers.handle(vk_update(text="успею?", conversation_message_id=20))
        await app.db.delete_bridge(bridge.id)  # мост отключили, пока сообщение ждало в очереди
        await app.outbox.join()
        assert app.bot.calls == []

    asyncio.run(scenario())
