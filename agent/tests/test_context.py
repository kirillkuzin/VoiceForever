"""Names and channels from the game: what the addon exports and what its saved file holds."""

import json

from vf_agent import savedvars
from vf_agent.context import LIMITS, GameContext, Source, from_saved, normalize, parse_export

# What Context.exportLine writes: sorted keys, "|" as | (the edit box would eat it).
EXPORT = (
    'VFX1 ab12cd34 {"at":1762950000,"channels":[{"n":1,"name":"Общий"},{"n":2,"name":"Торговля"},'
    '{"n":5,"name":"Рейдеры"}],"friends":[{"name":"Лиана","online":true}],'
    '"group":["Арторн","Грома-Гордунни"],"guild":[{"name":"Арторн","online":true},'
    '{"name":"Тень\\u007ccff","online":false}],"locale":"ruRU","me":"Кирилл-Пламегор","realm":"Пламегор",'
    '"speakers":[{"ago":40,"chan":"channel","channel":"Рейдеры","name":"Варг"},{"ago":5,"chan":"guild","name":"Арторн"}],'
    '"target":"Грома-Гордунни","v":1,"whispers":[{"ago":12,"dir":"in","name":"Лиана"}]}'
)


def test_export_from_the_addon():
    ctx = parse_export(EXPORT, "ab12cd34", received=1000.0)
    assert ctx is not None and ctx.source is Source.LIVE and ctx.at == 1000.0
    d = ctx.data
    assert d["me"] == "Кирилл-Пламегор" and d["target"] == "Грома-Гордунни"
    assert d["channels"] == [{"n": 1, "name": "Общий"}, {"n": 2, "name": "Торговля"}, {"n": 5, "name": "Рейдеры"}]
    assert d["guild"] == [{"name": "Арторн", "online": True}, {"name": "Теньcff", "online": False}]
    assert d["whispers"] == [{"name": "Лиана", "ago": 12, "dir": "in"}]
    assert d["speakers"][0] == {"name": "Варг", "ago": 40, "chan": "channel", "channel": "Рейдеры"}
    assert ctx.counts() == {"channels": 3, "group": 2, "guild": 2, "guild_online": 1, "friends": 1, "recent": 3}
    message = ctx.message(now=1003.4)
    assert message["type"] == "context" and message["source"] == "live" and message["age_s"] == 3
    assert json.loads(json.dumps(message, ensure_ascii=False)) == message


def test_export_is_refused_when_it_is_not_ours():
    assert parse_export(EXPORT, "00000000") is None  # another request's answer
    assert parse_export(None, "ab12cd34") is None  # an old addon: nothing was copied
    assert parse_export("VF:export:ab12cd34:", "ab12cd34") is None  # the line the agent pasted
    assert parse_export("VFX1 ab12cd34 {broken", "ab12cd34") is None
    assert parse_export(EXPORT.replace('"v":1', '"v":2'), "ab12cd34") is None


def test_lists_are_checked_and_capped():
    raw = {
        "v": 1,
        "at": 5,
        "group": ["А" * 65, 7, "  Арторн  "],
        "guild": [{"name": f"Член{i}", "online": i < 3} for i in range(500)],
        "channels": [{"n": 0, "name": "x"}, {"n": 3, "name": ""}, {"n": 4.0, "name": "Местный"}, "x"],
        "speakers": [{"name": "Варг", "chan": "whisper", "ago": -5}],
        "whispers": {},  # an empty Lua table read from the saved file
    }
    d = normalize(raw)
    assert d["group"] == ["Арторн"]
    assert len(d["guild"]) == LIMITS["guild"]
    assert d["channels"] == [{"n": 4, "name": "Местный"}]
    assert d["speakers"] == [{"name": "Варг", "ago": 0}]
    assert d["whispers"] == [] and d["friends"] == [] and d["me"] == ""


def test_newest_character_from_the_saved_file():
    text = (
        'VoiceForeverDB = {\n["chars"] = {\n'
        '["Старый-Пламегор"] = {\n["v"] = 1,\n["at"] = 100,\n["me"] = "Старый-Пламегор",\n["channels"] = {},\n},\n'
        '["Кирилл-Пламегор"] = {\n["v"] = 1,\n["at"] = 200,\n["me"] = "Кирилл-Пламегор",\n'
        '["channels"] = {\n{\n["n"] = 5,\n["name"] = "Рейдеры",\n},\n},\n'
        '["whispers"] = {\n{\n["name"] = "Лиана",\n["dir"] = "out",\n["ago"] = 30,\n},\n},\n},\n'
        '},\n["ptt"] = {\n["key"] = "PADRSTICK",\n},\n}\n'
    )
    db = savedvars.parse(text)["VoiceForeverDB"]
    ctx = from_saved(db)
    assert isinstance(ctx, GameContext) and ctx.source is Source.FILE and ctx.at == 200
    assert ctx.data["me"] == "Кирилл-Пламегор"
    assert ctx.data["channels"] == [{"n": 5, "name": "Рейдеры"}]
    assert ctx.data["whispers"] == [{"name": "Лиана", "ago": 30, "dir": "out"}]
    assert from_saved({"ptt": {}}) is None
