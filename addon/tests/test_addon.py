"""Runs VoiceForever.lua in a real Lua interpreter (lupa) with a stubbed WoW API."""

from pathlib import Path

import pytest

# WoW runs Lua 5.1, so test on 5.1.
lupa = pytest.importorskip("lupa.lua51")

ADDON = Path(__file__).resolve().parents[1] / "VoiceForever" / "VoiceForever.lua"

STUBS = r"""
sent, printed, bindings = {}, {}, {}
state = { raid = false, group = false, instance = false, guild = true, combat = false, last_tell = nil, now = 0 }
LE_PARTY_CATEGORY_INSTANCE = 2
function IsInRaid() return state.raid end
function IsInGroup(cat) if cat == 2 then return state.instance end return state.group end
function IsInGuild() return state.guild end
function InCombatLockdown() return state.combat end
function GetTime() return state.now end
function ChatEdit_GetLastTellTarget() return state.last_tell end
function SendChatMessage(msg, chatType, lang, target) table.insert(sent, {msg, chatType, target}) end
function ClearOverrideBindings() bindings = {} end
function SetOverrideBindingClick(owner, prio, key, name) bindings[key] = name end
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
  for _, m in ipairs({"SetAutoFocus","SetFontObject","SetMaxLetters","SetSize","SetPoint","SetAlpha"}) do f[m] = function() end end
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


@pytest.fixture
def lua():
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(STUBS)
    rt.execute(ADDON.read_text(encoding="utf-8"))
    rt.execute('fire("PLAYER_LOGIN")')
    return rt


def last_sent(lua):
    sent = lua.globals().sent
    n = len(sent)
    return tuple(sent[n][i] for i in (1, 2, 3)) if n else None


def test_binding_installed(lua):
    assert lua.eval('bindings["CTRL-SHIFT-F12"]') == "VoiceForeverFocusButton"


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
    assert lua.eval('bindings["CTRL-ALT-F11"]') is None
    lua.execute('state.combat = false; fire("PLAYER_REGEN_ENABLED")')
    assert lua.eval('bindings["CTRL-ALT-F11"]') == "VoiceForeverFocusButton"
