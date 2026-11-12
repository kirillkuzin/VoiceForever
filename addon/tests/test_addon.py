"""Runs VoiceForever.lua in a real Lua interpreter (lupa) with a stubbed WoW API."""

import json
import re
from pathlib import Path

import pytest

# WoW runs Lua 5.1, so test on 5.1.
lupa = pytest.importorskip("lupa.lua51")

ADDON = Path(__file__).resolve().parents[1] / "VoiceForever" / "VoiceForever.lua"
LOCALES = ADDON.with_name("Locales.lua")
TOC = ADDON.with_name("VoiceForever.toc")
# The addon's files in the order the game loads them: the file lines of the TOC.
FILES = [ADDON.with_name(line.strip()) for line in TOC.read_text(encoding="utf-8").splitlines()
         if line.strip() and not line.startswith("#")]

STUBS = r"""
sent, printed, bindings, game_bindings = {}, {}, {}, {}
reloaded = false
state = { raid = false, group = false, instance = false, guild = true, combat = false, last_tell = nil, now = 0,
          clock = 1731350000 }
-- What the game knows about the people and channels around the player.
world = {
  me = "Кирилл", realm = "Пламегор",
  channels = { 1, "Общий", false, 2, "Торговля", false, 5, "Рейдеры", false },
  party = {}, raid = {}, target = nil,
  guild = {}, friends = {}, bn = {},
}
rosters_asked, friends_asked = 0, 0
function GetRealmName() return world.realm end
function Ambiguate(name, ctx)
  local base, realm = name:match("^([^-]+)%-(.+)$")
  if base and realm == world.realm then return base end
  return name
end
function UnitName(unit)
  if unit == "player" then return world.me end
  if unit == "target" then return world.target end
  local i = unit:match("^party(%d)$")
  if i and world.party[tonumber(i)] then
    local name, realm = world.party[tonumber(i)]:match("^([^-]+)%-?(.*)$")
    return name, realm ~= "" and realm or nil
  end
  return nil
end
function UnitIsPlayer(unit) return unit == "target" and world.target ~= nil end
function GetNumGroupMembers() if state.raid then return #world.raid end return #world.party + 1 end
function GetRaidRosterInfo(i) return world.raid[i] end
function GetChannelList() return unpack(world.channels) end
function GetChannelName(name)
  for i = 1, #world.channels, 3 do
    if world.channels[i + 1] == name then return world.channels[i], name end
  end
  return 0, nil
end
C_GuildInfo = { GuildRoster = function() rosters_asked = rosters_asked + 1 end }
function GetNumGuildMembers() return #world.guild end
function GetGuildRosterInfo(i)
  local m = world.guild[i]
  return m.name, "Rank", 1, 60, "Warrior", "Zone", "", "", m.online
end
function GetGuildRosterLastOnline(i)
  local m = world.guild[i]
  if m.online then return nil end
  return 0, 0, m.days or 0, m.hours or 0
end
C_FriendList = {
  GetNumFriends = function() return #world.friends end,
  GetFriendInfoByIndex = function(i) return world.friends[i] end,
  ShowFriends = function() friends_asked = friends_asked + 1 end,
}
BNET_CLIENT_WOW, WOW_PROJECT_ID = "WoW", 14
function BNGetNumFriends() return #world.bn end
C_BattleNet = { GetFriendAccountInfo = function(i) return world.bn[i] end }
function GetBindingAction(key) return game_bindings[key] or "" end
function ReloadUI() reloaded = true end
function time() return state.clock end
BINDING_NAME_TOGGLEPINGLISTENER = "Метка"
LE_PARTY_CATEGORY_INSTANCE = 2
function IsInRaid() return state.raid end
function IsInGroup(cat) if cat == 2 then return state.instance end return state.group end
function IsInGuild() return state.guild end
function InCombatLockdown() return state.combat end
function GetTime() return state.now end
function ChatEdit_GetLastTellTarget() return state.last_tell end
function SendChatMessage(msg, chatType, lang, target) table.insert(sent, {msg, chatType, target}) end
function ClearOverrideBindings(owner)
  for key, entry in pairs(bindings) do if entry.owner == owner then bindings[key] = nil end end
end
function SetOverrideBindingClick(owner, prio, key, name) bindings[key] = { owner = owner, name = name } end
function bound(key) return bindings[key] and bindings[key].name end
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(printed, m) end }
UIParent, ChatFontNormal = {}, {}
SlashCmdList = {}
frames = {}
function CreateFrame(kind, name)
  local f = { scripts = {}, text = "", focus = false, events = {} }
  function f:SetScript(ev, fn) self.scripts[ev] = fn end
  function f:RegisterEvent(ev) self.events[ev] = true end
  function f:SetText(t) self.text = t end
  function f:GetText() return self.text end
  function f:SetFocus() self.focus = true; if self.scripts.OnEditFocusGained then self.scripts.OnEditFocusGained(self) end end
  function f:ClearFocus() self.focus = false end
  function f:HasFocus() return self.focus end
  f.shown = true
  function f:Show() self.shown = true end
  function f:Hide() self.shown = false end
  function f:IsShown() return self.shown end
  function f:CreateFontString()
    local fs = { text = "" }
    function fs:SetText(t) self.text = t end
    function fs:GetText() return self.text end
    function fs:SetPoint() end
    f.label = fs
    return fs
  end
  function f:EnableGamePadButton(on) self.gamepad = on end
  function f:EnableKeyboard(on) self.keyboard = on end
  function f:HighlightText() self.highlighted = true end
  for _, m in ipairs({"SetAutoFocus","SetFontObject","SetMaxLetters","SetSize","SetPoint","SetAlpha","RegisterForClicks"}) do f[m] = function() end end
  if name then frames[name] = f end
  table.insert(frames, f)
  return f
end
function fire(event, ...) for _, f in ipairs(frames) do if f.events[event] then f.scripts.OnEvent(f, event, ...) end end end
function agent_types(line)
  frames.VoiceForeverFocusButton.scripts.OnClick()
  local box = frames.VoiceForeverEditBox
  box.text = box.text .. line
  box.scripts.OnEnterPressed(box)
end
"""


def load(locale="enUS", db=None):
    """The addon as the game loads it: the files of the TOC in order, sharing one namespace."""
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUBS)
    rt.execute(f'function GetLocale() return "{locale}" end')
    ns = rt.table()
    for path in FILES:
        rt.execute(path.read_text(encoding="utf-8"), "VoiceForever", ns)
    rt.globals().ns = ns  # the addon's namespace, for tests of its modules
    if db is not None:  # SavedVariables from an earlier session, as Lua source
        rt.execute(f"VoiceForeverDB = {db}")
    rt.execute('fire("PLAYER_LOGIN")')
    return rt


@pytest.fixture
def lua():
    return load()


def last_sent(lua):
    sent = lua.globals().sent
    n = len(sent)
    return tuple(sent[n][i] for i in (1, 2, 3)) if n else None


def test_binding_installed(lua):
    assert lua.eval('bound("CTRL-SHIFT-F12")') == "VoiceForeverFocusButton"


@pytest.mark.parametrize(
    "setup, line, expected",
    [
        ("state.group = true", "VF:group::Иду", ("Иду", "PARTY", None)),
        ("state.group = true; state.raid = true", "VF:group::Иду", ("Иду", "RAID", None)),
        ("state.group = true; state.instance = true", "VF:group::Иду", ("Иду", "INSTANCE_CHAT", None)),
        ("", "VF:guild::Всем привет: как дела", ("Всем привет: как дела", "GUILD", None)),
        ("", "VF:whisper:Арторн:Сейчас", ("Сейчас", "WHISPER", "Арторн")),
        ("state.last_tell = 'Лиана'", "VF:reply::Ок", ("Ок", "WHISPER", "Лиана")),
        ("", "VF:channel:2:WTS руда", ("WTS руда", "CHANNEL", 2)),
        ("", "VF:say::Привет", ("Привет", "SAY", None)),
    ],
)
def test_delivery(lua, setup, line, expected):
    lua.execute(setup)
    lua.globals().agent_types(line)
    assert last_sent(lua) == expected
    assert not lua.eval("frames.VoiceForeverEditBox.focus")


@pytest.mark.parametrize(
    "setup, line",
    [
        ("", "VF:group::Иду"),  # not in a group
        ("", "VF:reply::Ок"),  # nobody whispered
        ("state.guild = false", "VF:guild::Привет"),
        ("", "random text"),
    ],
)
def test_refuses_and_prints(lua, setup, line):
    lua.execute(setup)
    lua.globals().agent_types(line)
    assert last_sent(lua) is None
    assert len(lua.globals().printed) == 1


def test_focus_timeout(lua):
    lua.execute("frames.VoiceForeverFocusButton.scripts.OnClick()")
    box = lua.eval("frames.VoiceForeverEditBox")
    assert box.focus
    lua.execute("state.now = 5; frames.VoiceForeverEditBox.scripts.OnUpdate(frames.VoiceForeverEditBox)")
    assert not box.focus


def test_key_change_waits_for_combat_end(lua):
    lua.execute('state.combat = true; SlashCmdList.VOICEFOREVER("key ctrl-alt-f11")')
    assert lua.eval('bound("CTRL-ALT-F11")') is None
    lua.execute('state.combat = false; fire("PLAYER_REGEN_ENABLED")')
    assert lua.eval('bound("CTRL-ALT-F11")') == "VoiceForeverFocusButton"


# ---------------------------------------------------------------- push-to-talk


def pad(lua, down=True, at=None):
    if at is not None:
        lua.execute(f"state.now = {at}")
    lua.execute(f"frames.VoiceForeverPTTButton.scripts.OnClick(frames.VoiceForeverPTTButton, 'LeftButton', {str(down).lower()})")
    tick(lua)


def tick(lua, at=None):
    if at is not None:
        lua.execute(f"state.now = {at}")
    lua.execute("frames.VoiceForeverBadge.scripts.OnUpdate(frames.VoiceForeverBadge)")


def badge(lua):
    shown = lua.eval("frames.VoiceForeverBadge.shown")
    return lua.eval("frames.VoiceForeverBadge.label.text") if shown else None


def pending(lua):
    return lua.eval("VoiceForeverPTT.pending")


@pytest.fixture
def ptt(lua):
    lua.execute('game_bindings.PADRSTICK = "TOGGLEPINGLISTENER"')
    lua.globals().agent_types("VF:ptt:setup:toggle 30 a1b2c3d4")
    assert lua.eval("frames.VoiceForeverSetup.shown") and lua.eval("frames.VoiceForeverSetup.gamepad")
    lua.execute('frames.VoiceForeverSetup.scripts.OnGamePadButtonDown(frames.VoiceForeverSetup, "PADRSTICK")')
    return lua


def test_setup_takes_the_button_and_reloads(ptt):
    db = ptt.eval("VoiceForeverDB.ptt")
    assert (db.key, db.mode, db.max, db.nonce) == ("PADRSTICK", "toggle", 30, "a1b2c3d4")
    assert (db.previous, db.previousName) == ("TOGGLEPINGLISTENER", "Метка")
    assert ptt.eval('bound("PADRSTICK")') == "VoiceForeverPTTButton"
    assert ptt.eval('bound("CTRL-SHIFT-F12")') == "VoiceForeverFocusButton"
    assert ptt.eval("reloaded") and not ptt.eval("frames.VoiceForeverSetup.shown")
    assert not ptt.eval("frames.VoiceForeverSetup.gamepad")


def test_setup_waits_for_combat_end(lua):
    lua.execute("state.combat = true")
    lua.globals().agent_types("VF:ptt:setup:hold 20 ffff0000")
    assert not lua.eval("frames.VoiceForeverSetup.gamepad")
    lua.execute('state.combat = false; fire("PLAYER_REGEN_ENABLED")')
    assert lua.eval("frames.VoiceForeverSetup.gamepad")


def test_setup_escape_cancels(lua):
    lua.globals().agent_types("VF:ptt:setup:toggle 30 x")
    lua.execute('frames.VoiceForeverSetup.scripts.OnKeyDown(frames.VoiceForeverSetup, "ESCAPE")')
    assert lua.eval("VoiceForeverDB.ptt") is None and not lua.eval("reloaded")
    assert not lua.eval("frames.VoiceForeverSetup.shown")


def test_toggle_badge_follows_a_recording(ptt):
    assert badge(ptt) is None
    pad(ptt, at=10)
    assert "RECORDING 0:00" in badge(ptt)
    tick(ptt, at=17.5)
    assert "RECORDING 0:07" in badge(ptt)
    pad(ptt, down=False)  # release does nothing in toggle mode
    assert "RECORDING" in badge(ptt)
    pad(ptt, at=18)
    assert "Recognising" in badge(ptt) and pending(ptt) == 1
    ptt.globals().agent_types("VF:guild::Всем привет")
    assert last_sent(ptt) == ("Всем привет", "GUILD", None)
    assert "Guild" in badge(ptt) and pending(ptt) == 0
    tick(ptt, at=30)
    assert badge(ptt) is None


def test_hold_mode(ptt):
    ptt.globals().agent_types("VF:ptt:config:hold 20")
    assert ptt.eval("VoiceForeverDB.ptt.mode") == "hold" and ptt.eval("VoiceForeverDB.ptt.max") == 20
    pad(ptt, at=1)
    pad(ptt, at=1.5)  # second press while holding: still recording
    assert "RECORDING" in badge(ptt)
    pad(ptt, down=False, at=3)
    assert pending(ptt) == 1


def test_short_press_and_time_limit(ptt):
    pad(ptt, at=1)
    pad(ptt, at=1.1)
    assert pending(ptt) == 0 and badge(ptt) is None
    pad(ptt, at=5)
    tick(ptt, at=35)  # 30 s limit: the agent sends now
    assert pending(ptt) == 1 and "Recognising" in badge(ptt)
    tick(ptt, at=100)  # no answer for too long: stop waiting
    assert pending(ptt) == 0 and badge(ptt) is None


def test_status_line_ends_the_wait(ptt):
    pad(ptt, at=1)
    pad(ptt, at=3)
    ptt.globals().agent_types("VF:status:empty:Не расслышал ни слова")
    assert pending(ptt) == 0 and "Не расслышал" in badge(ptt)
    assert last_sent(ptt) is None


def test_telegram_messages_do_not_show_the_badge(ptt):
    ptt.execute("state.group = true")
    ptt.globals().agent_types("VF:group::Иду")
    assert last_sent(ptt) == ("Иду", "PARTY", None)
    assert badge(ptt) is None


def test_ptt_off(ptt):
    ptt.execute('SlashCmdList.VOICEFOREVER("ptt off")')
    assert ptt.eval('bound("PADRSTICK")') is None and ptt.eval("VoiceForeverDB.ptt") is None


# ---------------------------------------------------------------- languages


def printed(lua):
    return list(lua.globals().printed.values())


def test_every_language_has_every_string():
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUBS)
    rt.execute('function GetLocale() return "enUS" end')
    ns = rt.table()
    rt.execute(LOCALES.read_text(encoding="utf-8"), "VoiceForever", ns)
    source = dict(ns.locales.enUS)
    for code in ("ruRU", "deDE", "esES", "frFR", "esMX"):
        other = dict(ns.locales[code])
        assert other.keys() == source.keys(), code
        for key, text in other.items():
            assert text.strip(), (code, key)
            assert re.findall(r"%[\d]*[sd]", text) == re.findall(r"%[\d]*[sd]", source[key]), (code, key)


@pytest.mark.parametrize(
    "locale, recording, test_line",
    [("enUS", "RECORDING", "VoiceForever works"), ("enGB", "RECORDING", "VoiceForever works"),
     ("ruRU", "ЗАПИСЬ", "VoiceForever работает"), ("deDE", "AUFNAHME", "VoiceForever funktioniert"),
     ("esES", "GRABANDO", "VoiceForever funciona"), ("esMX", "GRABANDO", "VoiceForever funciona"),
     ("frFR", "ENREGISTREMENT", "VoiceForever fonctionne"), ("ptBR", "RECORDING", "VoiceForever works")],
)
def test_texts_follow_the_game_language(locale, recording, test_line):
    lua = load(locale)
    lua.execute('state.group = true; SlashCmdList.VOICEFOREVER("test")')
    assert last_sent(lua) == (test_line, "PARTY", None)
    lua.execute('game_bindings.PADRSTICK = ""')
    lua.globals().agent_types("VF:ptt:setup:toggle 30 x")
    lua.execute('frames.VoiceForeverSetup.scripts.OnGamePadButtonDown(frames.VoiceForeverSetup, "PADRSTICK")')
    pad(lua, at=1)
    assert recording in badge(lua)


def test_refusals_and_help_in_the_game_language():
    lua = load("deDE")
    lua.globals().agent_types("VF:group::Hallo")
    assert printed(lua)[0].endswith("nicht gesendet (du bist in keiner Gruppe): Hallo")
    lua.execute('SlashCmdList.VOICEFOREVER("")')
    assert "Gruppe gerade: keine Gruppe" in printed(lua)[-2]
    assert printed(lua)[-1].startswith("|cff66ccff[VoiceForever]|r /vf key CTRL-SHIFT-F12 ändert die Taste")
    lua = load("ruRU")
    lua.execute('state.raid = true; state.group = true; SlashCmdList.VOICEFOREVER("")')
    assert "группа сейчас: Рейд" in printed(lua)[-2]
    assert lua.eval("frames.VoiceForeverSetup.label.text").startswith("VoiceForever: нажми")


# ---------------------------------------------------------------- names and channels


def lua_source(value):
    """A Lua table from one runtime written out as source for another, like SavedVariables."""
    if lupa.lua_type(value) == "table":
        items = ", ".join(f"[{lua_source(k)}] = {lua_source(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def export(lua, nonce="ab12"):
    """What the agent copies after "VF:export:<nonce>:": the snapshot as JSON."""
    lua.globals().agent_types(f"VF:export:{nonce}:")
    box = lua.eval("frames.VoiceForeverEditBox")
    assert box.focus and box.highlighted and box.exporting
    prefix, got_nonce, payload = box.text.split(" ", 2)
    assert (prefix, got_nonce) == ("VFX1", nonce)
    return json.loads(payload)


def test_channel_by_name(lua):
    lua.globals().agent_types("VF:channel:Рейдеры:Собираемся в 8")
    assert last_sent(lua) == ("Собираемся в 8", "CHANNEL", 5)
    lua.globals().agent_types("VF:channel:2:WTS руда")
    assert last_sent(lua) == ("WTS руда", "CHANNEL", 2)


def test_channel_left_is_refused_not_sent_elsewhere(lua):
    lua.globals().agent_types("VF:channel:Мир:Привет")
    assert last_sent(lua) is None
    assert printed(lua) == ["|cff66ccff[VoiceForever]|r not sent (you are not in the channel Мир): Привет"]


def test_export_puts_the_snapshot_in_the_box_and_enter_closes_it(lua):
    lua.execute("""
      world.party = {"Арторн", "Лиана-Гордунни"}; world.target = "Тарал"
      world.guild = {
        {name = "Вессa-Пламегор", online = false, days = 3},
        {name = "Тарал-Пламегор", online = true},
        {name = "Старик-Пламегор", online = false, days = 40},
        {name = "Кирилл-Пламегор", online = true},
      }
      world.friends = {{name = "Гром", connected = true}, {name = "Ива", connected = false}}
      world.bn = {
        {gameAccountInfo = {isOnline = true, characterName = "Зара", realmName = "Гордунни", clientProgram = "WoW", wowProjectID = 14}},
        {gameAccountInfo = {isOnline = true, characterName = "Ретейл", realmName = "Ревущий фьорд", clientProgram = "WoW", wowProjectID = 1}},
      }
      state.group = true
    """)
    snap = export(lua)
    assert snap["v"] == 1 and snap["at"] == 1731350000 and snap["me"] == "Кирилл-Пламегор"
    assert snap["channels"] == [{"n": 1, "name": "Общий"}, {"n": 2, "name": "Торговля"}, {"n": 5, "name": "Рейдеры"}]
    assert snap["group"] == ["Арторн", "Лиана-Гордунни"]
    # Online first, then by last seen; me and anyone gone over 14 days are left out.
    assert snap["guild"] == [{"name": "Тарал", "online": True}, {"name": "Вессa", "online": False}]
    assert snap["friends"] == [{"name": "Гром", "online": True}, {"name": "Ива", "online": False},
                               {"name": "Зара-Гордунни", "online": True}]
    assert snap["target"] == "Тарал"
    assert snap["whispers"] == [] and snap["speakers"] == []
    lua.execute("frames.VoiceForeverEditBox.scripts.OnEnterPressed(frames.VoiceForeverEditBox)")
    box = lua.eval("frames.VoiceForeverEditBox")
    assert box.text == "" and not box.focus and not box.exporting
    assert last_sent(lua) is None and printed(lua) == []


def test_export_ignores_a_bad_nonce_and_times_out(lua):
    lua.globals().agent_types("VF:export:no way!:")
    assert not lua.eval("frames.VoiceForeverEditBox.focus")
    export(lua)
    # A key the player pressed meanwhile spoils the copy; the agent then sends no Enter,
    # so the box gives the keyboard back by itself, sooner than after a normal line.
    lua.execute("state.now = 1.5; frames.VoiceForeverEditBox.scripts.OnUpdate(frames.VoiceForeverEditBox)")
    box = lua.eval("frames.VoiceForeverEditBox")
    assert box.text == "" and not box.focus and not box.exporting


def test_raid_members_without_me(lua):
    lua.execute('state.raid = true; world.raid = {"Арторн", "Кирилл", "Лиана-Гордунни"}')
    assert export(lua)["group"] == ["Арторн", "Лиана-Гордунни"]


def test_guild_list_is_capped(lua):
    lua.execute("for i = 1, 350 do world.guild[i] = {name = 'Член' .. i .. '-Пламегор', online = true} end")
    assert len(export(lua)["guild"]) == 300


def test_whispers_and_speakers_newest_first(lua):
    lua.execute("""
      fire("CHAT_MSG_WHISPER", "привет", "Арторн-Пламегор")
      state.clock = state.clock + 30
      fire("CHAT_MSG_WHISPER_INFORM", "ок", "Лиана-Гордунни")
      fire("CHAT_MSG_GUILD", "всем привет", "Тарал-Пламегор")
      fire("CHAT_MSG_CHANNEL", "WTS", "Торгаш-Пламегор", "", "2. Торговля", "", "", 2, 2, "Торговля")
      fire("CHAT_MSG_CHANNEL", "сбор", "Гром-Пламегор", "", "5. Рейдеры", "", "", 0, 5, "Рейдеры")
      fire("CHAT_MSG_SAY", "я", "Кирилл-Пламегор")
      state.clock = state.clock + 10
      fire("CHAT_MSG_WHISPER", "ещё", "Арторн-Пламегор")
      state.clock = state.clock + 5
    """)
    snap = export(lua)
    assert snap["whispers"] == [
        {"name": "Арторн", "dir": "in", "ago": 5},
        {"name": "Лиана-Гордунни", "dir": "out", "ago": 15},
    ]
    # Trade is a zone channel full of strangers; my own lines are not kept either.
    assert snap["speakers"] == [
        {"name": "Гром", "chan": "channel", "channel": "Рейдеры", "ago": 15},
        {"name": "Тарал", "chan": "guild", "ago": 15},
    ]
    assert "привет" not in json.dumps(snap, ensure_ascii=False)  # never the text itself


def test_saved_on_logout_and_restored_after_reload(lua):
    lua.execute('fire("CHAT_MSG_WHISPER", "привет", "Арторн-Пламегор"); state.clock = state.clock + 20')
    lua.execute('fire("PLAYER_LOGOUT")')
    db = lua.eval("VoiceForeverDB")
    saved = db.chars["Кирилл-Пламегор"]
    assert saved.at == 1731350020 and saved.whispers[1].name == "Арторн" and saved.whispers[1].ago == 20
    assert saved.channels[3].name == "Рейдеры"
    again = load(db=lua_source(db))
    again.execute("state.clock = 1731350100")
    assert export(again)["whispers"] == [{"name": "Арторн", "dir": "in", "ago": 100}]


def test_names_command(lua):
    lua.execute('world.party = {"Арторн"}; fire("CHAT_MSG_WHISPER", "x", "Гром"); SlashCmdList.VOICEFOREVER("names")')
    assert printed(lua)[-1].endswith(
        "channels: 1. Общий, 2. Торговля, 5. Рейдеры. Group: 1, guild: 0 (0 online), friends: 0, "
        "recent whispers: 1, recent speakers: 0."
    )
    assert lua.eval("rosters_asked") >= 1 and lua.eval("friends_asked") == 1


def test_json_never_carries_a_raw_pipe_or_line_break(lua):
    encode = lua.eval("ns.Context.encode")
    text = encode(lua.eval('{ a = "x\\"|y\\n", list = {1, 2.5, true} }'))
    assert text == '{"a":"x\\"\\u007cy\\n","list":[1,2.500,true]}'
    assert json.loads(text) == {"a": 'x"|y\n', "list": [1, 2.5, True]}
