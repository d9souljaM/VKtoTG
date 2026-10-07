import asyncio

from vktg import linking, texts
from vktg.to_tg import ToTG
from vktg.vk_handlers import VKHandlers

VK_PEER = 2_000_000_001
TG_CHAT = -100


def test_bridge_by_key_flow(make_app):
    async def scenario():
        app = await make_app()
        reply = await linking.cmd_bridge(app, "tg", TG_CHAT, "", "Группа")
        key = reply.split("Ключ: ")[1].split()[0]
        assert "беседе VK" in reply

        # ключ из Telegram нельзя использовать в другой группе Telegram
        assert "создан в Telegram" in await linking.cmd_bridge(app, "tg", -200, key)

        assert (await linking.cmd_bridge(app, "vk", VK_PEER, key.lower(), "Беседа")).startswith("✅")
        bridge = await app.db.bridge_by_vk(VK_PEER)
        assert (bridge.tg_chat_id, bridge.direction, bridge.prefix) == (TG_CHAT, "both", True)
        name, kwargs = app.bot.calls[-1]
        assert name == "send_message" and kwargs["chat_id"] == TG_CHAT and "«Беседа»" in kwargs["text"]

        # ключ одноразовый
        assert "не найден" in await linking.cmd_bridge(app, "vk", VK_PEER + 1, key)
        # повторная привязка того же чата запрещена
        assert "уже связан" in await linking.cmd_bridge(app, "tg", TG_CHAT, "")

        assert "выключены" in await linking.cmd_bridge(app, "tg", TG_CHAT, "prefix off")
        assert "VK → Telegram" in await linking.cmd_bridge(app, "tg", TG_CHAT, "direction vk>tg")
        bridge = await app.db.bridge_by_tg(TG_CHAT)
        assert (bridge.prefix, bridge.to_vk, bridge.to_tg) == (False, False, True)
        assert "Использование" in await linking.cmd_bridge(app, "tg", TG_CHAT, "direction sideways")

        assert "❌" in await linking.cmd_unbridge(app, "vk", VK_PEER)
        assert await app.db.bridge_by_tg(TG_CHAT) is None
        assert app.bot.calls[-1][1]["text"].startswith("❌")
        assert await linking.cmd_status(app, "tg", TG_CHAT) == texts.NOT_BRIDGED

    asyncio.run(scenario())


def test_expired_key_is_rejected(make_app):
    async def scenario():
        app = await make_app()
        await app.db.create_key("tg", TG_CHAT, "ABCDEFGH")
        assert await app.db.take_key("ABCDEFGH", "tg", ttl=-1) is None
        assert await app.db.take_key("ABCDEFGH", "tg", ttl=60) == TG_CHAT

    asyncio.run(scenario())


def test_vk_commands_require_chat_admin(make_app):
    async def scenario():
        app = await make_app()
        handlers = VKHandlers(app, ToTG(app))
        message = {"peer_id": VK_PEER, "from_id": 5, "text": "[club77|@club77] /bridge", "conversation_message_id": 1}

        await handlers.handle({"type": "message_new", "object": {"message": message}})
        assert app.vk.sent[-1]["text"] == texts.ADMIN_ONLY

        message["from_id"] = 2  # есть в admin_ids
        await handlers.handle({"type": "message_new", "object": {"message": message}})
        assert "Ключ:" in app.vk.sent[-1]["text"]

        app.vk.chat_settings = None  # сообщество не админ беседы
        await handlers.handle({"type": "message_new", "object": {"message": message}})
        assert app.vk.sent[-1]["text"] == texts.VK_NEED_ADMIN

    asyncio.run(scenario())


def test_private_messages_to_community_are_ignored(make_app):
    async def scenario():
        app = await make_app()
        handlers = VKHandlers(app, ToTG(app))
        await handlers.handle({"type": "message_new", "object": {"message": {"peer_id": 5, "from_id": 5, "text": "привет"}}})
        assert app.vk.sent == []
        await handlers.handle({"type": "message_new", "object": {"message": {"peer_id": 5, "from_id": 5, "text": "/help"}}})
        assert "Мост VK ↔ Telegram" in app.vk.sent[-1]["text"]

    asyncio.run(scenario())
