"""Reading the addon's SavedVariables file the way the game writes it."""

import os

import pytest

from vf_agent import savedvars

GAME_FILE = '''
VoiceForeverDB = {
	["key"] = "CTRL-SHIFT-F12",
	["ptt"] = {
		["key"] = "PADRSTICK",
		["previous"] = "TOGGLEPINGLISTENER",
		["mode"] = "toggle",
		["max"] = 30,
		["nonce"] = "a1b2c3d4",
		["at"] = 1731350000.5,
		["note"] = "Кнопка \\"записи\\"\\nвторая строка",
	},
	["list"] = {
		"Арторн", -- [1]
		"Лиана", -- [2]
	},
	["flag"] = true,
	["none"] = nil,
	[1] = false,
}
OtherAddonDB = { x = 1, y = -2.5e1; }
'''


def test_parse_game_format():
    data = savedvars.parse(GAME_FILE)
    db = data["VoiceForeverDB"]
    assert db["key"] == "CTRL-SHIFT-F12"
    assert db["ptt"] == {
        "key": "PADRSTICK",
        "previous": "TOGGLEPINGLISTENER",
        "mode": "toggle",
        "max": 30,
        "nonce": "a1b2c3d4",
        "at": 1731350000.5,
        "note": 'Кнопка "записи"\nвторая строка',
    }
    assert db["list"] == ["Арторн", "Лиана"]
    assert db["flag"] is True and db["none"] is None and db[1] is False
    assert data["OtherAddonDB"] == {"x": 1, "y": -25.0}


def test_decimal_escapes_are_utf8_bytes():
    # "Ок" written byte by byte, as some game builds do.
    assert savedvars.parse('A = "\\208\\158\\208\\186"')["A"] == "Ок"


@pytest.mark.parametrize("bad", ["VoiceForeverDB = {", "= 1", "A = @", "A = {[1] 2}"])
def test_bad_files_raise(bad):
    with pytest.raises(savedvars.LuaError):
        savedvars.parse(bad)


def test_addon_files_newest_first(tmp_path):
    exe = tmp_path / "_forever_" / "Wow.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    paths = []
    for i, account in enumerate(["ACC1", "ACC2"]):
        path = exe.parent / "WTF" / "Account" / account / "SavedVariables" / "VoiceForever.lua"
        path.parent.mkdir(parents=True)
        path.write_text(f'VoiceForeverDB = {{ ["n"] = {i}, }}', encoding="utf-8")
        os.utime(path, (1000 + i, 1000 + i))
        paths.append(path)
    assert savedvars.addon_files(exe) == [paths[1], paths[0]]
    assert savedvars.read_addon_db(paths[1]) == {"n": 1}
    assert savedvars.addon_files(tmp_path / "nowhere" / "Wow.exe") == []
