"""Runs VoiceForever.lua in a real Lua interpreter (lupa) with a stubbed WoW API."""

import re
from pathlib import Path

import pytest

# WoW runs Lua 5.1, so test on 5.1.
lupa = pytest.importorskip("lupa.lua51")

ADDON = Path(__file__).resolve().parents[1] / "VoiceForever" / "VoiceForever.lua"
LOCALES = ADDON.with_name("Locales.lua")

STUBS = r"""
sent, printed, bindings, game_bindings = {}, {}, {}, {}
reloaded = false
state = { raid = false, group = false, instance = false, guild = true, combat = false, last_tell = nil, now = 0 }
function GetBindingAction(key) return game_bindings[key] or "" end
function ReloadUI() reloaded = true end
function time() return 1731350000 end
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
  for _, m in ipairs({"SetAutoFocus","SetFontObject","SetMaxLetters","SetSize","SetPoint","SetAlpha","RegisterForClicks"}) do f[m] = function() end end
  if name then frames[name] = f end
  table.insert(frames, f)
  return f
end
function fire(event) for _, f in ipairs(frames) do if f.events[event] then f.scripts.OnEvent(f, event) end end end
function agent_types(line)
  frames.VoiceForeverFocusButton.scripts.OnClick()
  local box = frames.VoiceForeverEditBox
  box.text = box.text .. line
  box.scripts.OnEnterPressed(box)
end
"""


def load(locale="enUS"):
    """The addon as the game loads it: the files of the TOC in order, sharing one namespace."""
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUBS)
    rt.execute(f'function GetLocale() return "{locale}" end')
    ns = rt.table()
    for path in (LOCALES, ADDON):
        rt.execute(path.read_text(encoding="utf-8"), "VoiceForever", ns)
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
