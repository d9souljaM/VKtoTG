from types import SimpleNamespace

from vktg.formatting import (
    parse_command,
    sign,
    split_text,
    tg_header_html,
    tg_text_with_links,
    vk_text_to_html,
    vk_text_to_plain,
)


def test_vk_mentions_become_links_and_html_is_escaped():
    text = "[id1|Павел] <b>&"
    assert vk_text_to_html(text) == '<a href="https://vk.com/id1">Павел</a> &lt;b&gt;&amp;'
    assert vk_text_to_plain(text) == "Павел <b>&"


def test_parse_command_variants():
    assert parse_command("/bridge ABC") == ("bridge", "ABC")
    assert parse_command("[club77|@club77] /bridge abc") == ("bridge", "abc")
    assert parse_command("[club77|Клуб], /status") == ("status", "")
    assert parse_command("/bridge@bridge_bot prefix off") == ("bridge", "prefix off")
    assert parse_command("привет") is None
    assert parse_command("/") is None


def test_split_text_respects_limit():
    text = "строка номер раз\n" * 500
    chunks = split_text(text, 100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_text_without_spaces():
    chunks = split_text("я" * 250, 100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_hidden_links_use_utf16_offsets():
    text = "😀 сайт тут"  # эмодзи занимает две позиции UTF-16
    entity = SimpleNamespace(type="text_link", url="https://example.com", offset=3, length=4)
    assert tg_text_with_links(text, [entity]) == "😀 сайт (https://example.com) тут"


def test_sign_and_header():
    assert sign(tg_header_html("<Иван>", True), "", False) == "<b>[VK] &lt;Иван&gt;</b>"
    assert sign("Иван", "привет", True) == "Иван:\nпривет"
    assert sign("Иван", "привет", False) == "Иван: привет"
