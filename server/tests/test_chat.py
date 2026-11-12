from vf_server.chat import Channel, sanitize, split_message, validate


def test_sanitize_blocks_commands_and_escapes():
    assert sanitize("/run DoSomething()") == "run DoSomething()"
    assert sanitize(".server info") == "server info"
    assert sanitize("a|cff00ff00b|r") == "a/cff00ff00b/r"
    assert sanitize("line1\nline2\t x") == "line1 line2 x"


def test_split_short_message_unchanged():
    assert split_message("Привет всем", 255) == ["Привет всем"]


def test_split_respects_utf8_bytes_and_words():
    text = " ".join(["слово"] * 60)  # 5 cyrillic letters = 10 bytes each
    parts = split_message(text, 255)
    assert len(parts) > 1
    assert all(len(p.encode()) <= 255 for p in parts)
    assert " ".join(parts) == text


def test_split_long_word():
    parts = split_message("я" * 300, 255)
    assert all(len(p.encode()) <= 255 for p in parts)
    assert "".join(parts) == "я" * 300


def test_validate_whisper_without_valid_name_becomes_reply():
    msg = validate("whisper", "два слова", "hi")
    assert msg.channel == "reply" and msg.target == ""


def test_validate_keeps_realm_name():
    msg = validate("whisper", "Арторн-Гордунни", "hi")
    assert msg.channel == "whisper" and msg.target == "Арторн-Гордунни"


def test_validate_unknown_channel_uses_default():
    msg = validate("bogus", "x", "hi", Channel.GUILD)
    assert msg.channel == "guild" and msg.target == ""


def test_validate_channel_takes_a_number_or_a_name():
    assert validate("channel", "2", "wts").target == "2"
    assert validate("channel", " Рейдеры ", "сбор").target == "Рейдеры"
    assert validate("channel", "Торговля - Город", "wts").target == "Торговля - Город"
    assert validate("channel", "Рейдеры", "сбор").label("ru") == "Канал Рейдеры"
    for bad in ("", "/w Вася", "a:b", "x|cff", "2nd", "-dash"):
        assert validate("channel", bad, "wts").channel == "say", bad


def test_channel_is_a_plain_string_on_the_wire():
    import json

    msg = validate("guild", "", "hi")
    assert msg.channel is Channel.GUILD
    assert json.dumps({"channel": msg.channel}) == '{"channel": "guild"}'
    assert msg.label() == "Guild" and msg.label("ru") == "Гильдия"
