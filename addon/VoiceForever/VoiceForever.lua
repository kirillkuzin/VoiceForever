-- VoiceForever: receives lines from the VoiceForever desktop agent and sends them to chat.
--
-- The agent presses a key (default CTRL-SHIFT-F12), which focuses an invisible edit box,
-- pastes "VF:<channel>:<target>:<text>" and presses Enter. The normal chat window never opens.
-- The addon has no network access: everything arrives as keyboard input.
--
-- Push-to-talk: the addon takes the pad button the agent records with (an override binding,
-- so the game's own action on it is paused while the addon is on) and shows a badge:
-- recording, recognising, sent. The agent reads the same button through SDL; both follow
-- the same rules (toggle or hold, 0.3 s minimum, time limit), so they agree without talking.
-- Lines with channel "ptt" set the button up; lines with channel "status" end the badge
-- when the server could not send anything.
--
-- Names and channels: Context.lua keeps what the game knows about the people and channels
-- around the player. "VF:export:<nonce>:" hands it to the agent (see there), and
-- "VF:channel:<name>:<text>" finds the channel's number by its name at the moment of sending,
-- so a message never goes to another channel that took over an old number.
--
-- Texts come from Locales.lua, in the language of the WoW client.

local _, ns = ...
local L, T = ns.L, ns.T
local Context = ns.Context

local PREFIX = "VF"
local DEFAULT_KEY = "CTRL-SHIFT-F12"
local FOCUS_TIMEOUT = 3 -- seconds; drop focus if the agent never pressed Enter
local EXPORT_TIMEOUT = 1 -- seconds the snapshot stays in the box for the agent's Ctrl+C
local MIN_RECORD = 0.3 -- seconds; shorter presses are accidents, the agent drops them too
local PENDING_TIMEOUT = 40 -- seconds to wait for the message of a recording
local TOAST_TIME = 4
local SETUP_TIMEOUT = 60

local SendChat = (C_ChatInfo and C_ChatInfo.SendChatMessage) or SendChatMessage

local SIMPLE = {
    say = "SAY",
    yell = "YELL",
    emote = "EMOTE",
    party = "PARTY",
    raid = "RAID",
    raid_warning = "RAID_WARNING",
    instance = "INSTANCE_CHAT",
    guild = "GUILD",
    officer = "OFFICER",
}

local function print(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cff66ccff[VoiceForever]|r " .. msg)
end

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function groupChannel()
    if LE_PARTY_CATEGORY_INSTANCE and IsInGroup and IsInGroup(LE_PARTY_CATEGORY_INSTANCE) then
        return "INSTANCE_CHAT"
    end
    if IsInRaid and IsInRaid() then
        return "RAID"
    end
    if IsInGroup and IsInGroup() then
        return "PARTY"
    end
    return nil
end

local function lastTellTarget()
    if ChatEdit_GetLastTellTarget then
        local name = ChatEdit_GetLastTellTarget()
        if name and name ~= "" then
            return name
        end
    end
    return nil
end

-- Parse "VF:<channel>:<target>:<text>". Returns channel, target, text or nil.
function VoiceForever_Parse(line)
    local channel, target, text = line:match("^" .. PREFIX .. ":([%w_]+):([^:]*):(.*)$")
    if not channel then
        return nil
    end
    return channel, trim(target), trim(text)
end

-- Map a VoiceForever channel to a WoW chat type.
-- Returns chatType, extra (whisper name or channel number) or nil, error message.
function VoiceForever_Resolve(channel, target)
    if channel == "group" then
        local chatType = groupChannel()
        if not chatType then
            return nil, L.not_in_group
        end
        return chatType
    elseif channel == "whisper" then
        if target == "" then
            return nil, L.no_whisper_target
        end
        return "WHISPER", target
    elseif channel == "reply" then
        local name = lastTellTarget()
        if not name then
            return nil, L.no_reply_target
        end
        return "WHISPER", name
    elseif channel == "channel" then
        if target == "" then
            return nil, L.bad_channel_number
        end
        if target:match("^%d+$") then
            return "CHANNEL", tonumber(target)
        end
        local number = Context.findChannel(target)
        if not number then
            return nil, T("not_in_channel", target)
        end
        return "CHANNEL", number
    elseif channel == "guild" or channel == "officer" then
        if IsInGuild and not IsInGuild() then
            return nil, L.not_in_guild
        end
        return SIMPLE[channel]
    end
    local chatType = SIMPLE[channel]
    if not chatType then
        return nil, T("unknown_channel", tostring(channel))
    end
    return chatType
end

local Badge -- defined below, used by deliver()
local handleControl
local startExport

local function deliver(line)
    local channel, target, text = VoiceForever_Parse(line)
    if not channel then
        if line ~= "" then
            print(L.bad_line)
        end
        return
    end
    if channel == "ptt" or channel == "status" then
        handleControl(channel, target, text)
        return
    end
    if channel == "export" then
        startExport(target)
        return
    end
    if text == "" then
        return
    end
    local chatType, extra = VoiceForever_Resolve(channel, target)
    if not chatType then
        print(T("not_sent", extra, text))
        Badge.result(false, T("not_sent_badge", extra))
        return
    end
    if chatType == "WHISPER" or chatType == "CHANNEL" then
        SendChat(text, chatType, nil, extra)
    else
        SendChat(text, chatType)
    end
    local label = L[chatType]
    if chatType == "WHISPER" then
        label = label .. ": " .. tostring(extra)
    end
    Badge.result(true, label)
end

-- Invisible edit box that the agent types into.
local box = CreateFrame("EditBox", "VoiceForeverEditBox", UIParent)
box:SetAutoFocus(false)
box:SetFontObject(ChatFontNormal)
box:SetMaxLetters(0) -- no limit: the export line holds the whole snapshot
box:SetSize(200, 20)
box:SetPoint("BOTTOMLEFT", UIParent, "BOTTOMLEFT", 0, 0)
box:SetAlpha(0)
box:SetScript("OnEnterPressed", function(self)
    local line = self:GetText() or ""
    local exported = self.exporting
    self.exporting = nil
    self:SetText("")
    self:ClearFocus()
    if not exported then -- after an export, Enter only closes the box
        deliver(line)
    end
end)
box:SetScript("OnEscapePressed", function(self)
    self.exporting = nil
    self:SetText("")
    self:ClearFocus()
end)
box:SetScript("OnEditFocusGained", function(self)
    self.focusedAt = GetTime()
end)
box:SetScript("OnUpdate", function(self)
    local limit = self.exporting and EXPORT_TIMEOUT or FOCUS_TIMEOUT
    if self.focusedAt and self:HasFocus() and GetTime() - self.focusedAt > limit then
        self.focusedAt = nil
        self.exporting = nil
        self:SetText("")
        self:ClearFocus()
    end
end)

-- The agent asked for the snapshot: show it in the box, selected, for its Ctrl+C. Its Enter
-- (or the focus timeout) then empties the box.
startExport = function(nonce)
    if not nonce:match("^%w+$") then
        return
    end
    box.exporting = true
    box:SetText(Context.exportLine(nonce))
    box:SetFocus()
    box:HighlightText()
end

-- Hidden button the key binding clicks.
local button = CreateFrame("Button", "VoiceForeverFocusButton", UIParent)
button:SetScript("OnClick", function()
    box:SetText("")
    box:SetFocus()
end)

-- ------------------------------------------------------------------ push-to-talk badge
local badge = CreateFrame("Frame", "VoiceForeverBadge", UIParent)
badge:SetSize(260, 28)
badge:SetPoint("TOP", UIParent, "TOP", 0, -120)
badge:Hide()
local badgeText = badge:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
badgeText:SetPoint("CENTER")

-- State shared with the tests: recording start time, recordings waiting for their message.
VoiceForeverPTT = { startedAt = nil, pending = 0, pendingSince = nil, toast = nil, toastUntil = nil }
local state = VoiceForeverPTT

local function pttSettings()
    return VoiceForeverDB and VoiceForeverDB.ptt or {}
end

local function render()
    local now = GetTime()
    if state.startedAt then
        local secs = math.floor(now - state.startedAt)
        badgeText:SetText("|cffff4040●|r " .. T("recording", math.floor(secs / 60), secs % 60))
    elseif state.toast and state.toastUntil and now < state.toastUntil then
        badgeText:SetText(state.toast)
    elseif state.pending > 0 then
        badgeText:SetText("|cffffd100…|r " .. L.recognising)
    else
        state.toast = nil
        badge:Hide()
        return
    end
    badge:Show()
end

Badge = {}

function Badge.start()
    state.startedAt = GetTime()
    render()
end

function Badge.stop()
    if not state.startedAt then
        return
    end
    if GetTime() - state.startedAt >= MIN_RECORD then
        state.pending = state.pending + 1
        state.pendingSince = GetTime()
    end
    state.startedAt = nil
    render()
end

-- A message (or a refusal) arrived: one recording fewer is waiting.
function Badge.result(ok, text)
    if state.pending <= 0 and not state.startedAt then
        return -- typed from Telegram, not from a recording
    end
    if state.pending > 0 then
        state.pending = state.pending - 1
    end
    state.toast = (ok and "|cff40ff40✓|r " or "|cffff4040✗|r ") .. text
    state.toastUntil = GetTime() + TOAST_TIME
    render()
end

badge:SetScript("OnUpdate", function()
    local now = GetTime()
    local max = tonumber(pttSettings().max) or 30
    if state.startedAt and now - state.startedAt >= max then
        Badge.stop() -- the agent sends at the same moment
    end
    if state.pending > 0 and state.pendingSince and now - state.pendingSince > PENDING_TIMEOUT then
        state.pending = 0
    end
    render()
end)

-- The button the pad press clicks through the override binding.
local pttButton = CreateFrame("Button", "VoiceForeverPTTButton", UIParent)
pttButton:RegisterForClicks("AnyDown", "AnyUp")
pttButton:SetScript("OnClick", function(_, _, down)
    local hold = pttSettings().mode == "hold"
    if down then
        if not state.startedAt then
            Badge.start()
        elseif not hold then
            Badge.stop()
        end
    elseif hold then
        Badge.stop()
    end
end)

-- ------------------------------------------------------------------ bindings
local bindingPending = false

local function applyBinding()
    if InCombatLockdown() then
        bindingPending = true
        return
    end
    bindingPending = false
    ClearOverrideBindings(button)
    SetOverrideBindingClick(button, true, VoiceForeverDB.key, "VoiceForeverFocusButton")
    ClearOverrideBindings(pttButton)
    local key = pttSettings().key
    if key and key ~= "" then
        SetOverrideBindingClick(pttButton, true, key, "VoiceForeverPTTButton")
    end
end

-- ------------------------------------------------------------------ push-to-talk setup
-- The agent asks for it with "VF:ptt:setup:<mode> <max seconds> <nonce>". The player presses
-- the button in the game; the addon saves what the game had on it, takes it and reloads the
-- interface so the file the agent reads is written.
local setup = CreateFrame("Frame", "VoiceForeverSetup", UIParent)
setup:SetSize(420, 70)
setup:SetPoint("CENTER", UIParent, "CENTER", 0, 120)
setup:Hide()
local setupText = setup:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge")
setupText:SetPoint("CENTER")
setupText:SetText(L.setup_prompt)

local setupRequest = nil
local setupPending = nil -- a request that came in combat, run when combat ends

local function bindingName(action)
    if not action or action == "" then
        return ""
    end
    return _G["BINDING_NAME_" .. action] or action
end

local function finishSetup(key)
    local request = setupRequest
    setupRequest = nil
    setup:EnableGamePadButton(false)
    setup:EnableKeyboard(false)
    setup:Hide()
    if not request then
        return
    end
    local previous = GetBindingAction(key) or ""
    VoiceForeverDB.ptt = {
        key = key,
        mode = request.mode,
        max = request.max,
        previous = previous,
        previousName = bindingName(previous),
        nonce = request.nonce,
        at = time and time() or 0,
    }
    applyBinding()
    if previous ~= "" then
        print(T("ptt_took", key, bindingName(previous)))
    else
        print(T("ptt_took_free", key))
    end
    ReloadUI() -- writes SavedVariables, which the agent reads
end

local function startSetup(request)
    if InCombatLockdown() then
        setupPending = request
        print(L.ptt_after_combat)
        return
    end
    setupRequest = request
    setupRequest.openedAt = GetTime()
    setup:EnableGamePadButton(true)
    setup:EnableKeyboard(true)
    setup:Show()
end

setup:SetScript("OnGamePadButtonDown", function(_, key)
    finishSetup(key)
end)
setup:SetScript("OnKeyDown", function(_, key)
    if key == "ESCAPE" then
        setupRequest = nil
        finishSetup(nil)
        print(L.ptt_cancelled)
    end
end)
setup:SetScript("OnUpdate", function()
    if setupRequest and GetTime() - setupRequest.openedAt > SETUP_TIMEOUT then
        setupRequest = nil
        finishSetup(nil)
        print(L.ptt_timeout)
    end
end)

handleControl = function(channel, target, text)
    if channel == "status" then
        Badge.result(false, text ~= "" and text or L.not_sent_short)
        return
    end
    local mode, max, nonce = text:match("^(%a+)%s+([%d%.]+)%s*(%S*)$")
    if mode ~= "toggle" and mode ~= "hold" then
        print(L.bad_ptt)
        return
    end
    max = tonumber(max) or 30
    if target == "setup" then
        startSetup({ mode = mode, max = max, nonce = nonce })
    elseif target == "config" then
        VoiceForeverDB.ptt = VoiceForeverDB.ptt or {}
        VoiceForeverDB.ptt.mode = mode
        VoiceForeverDB.ptt.max = max
    end
end

local events = CreateFrame("Frame")
events:RegisterEvent("PLAYER_LOGIN")
events:RegisterEvent("PLAYER_LOGOUT")
events:RegisterEvent("PLAYER_REGEN_ENABLED")
for _, event in ipairs(Context.events()) do
    events:RegisterEvent(event)
end
events:SetScript("OnEvent", function(_, event, ...)
    if event == "PLAYER_LOGIN" then
        VoiceForeverDB = VoiceForeverDB or {}
        VoiceForeverDB.key = VoiceForeverDB.key or DEFAULT_KEY
        applyBinding()
        Context.restore(VoiceForeverDB)
        Context.start()
    elseif event == "PLAYER_LOGOUT" then
        -- Comes right before the game writes SavedVariables, on logout and on /reload.
        if VoiceForeverDB then
            Context.save(VoiceForeverDB)
        end
    elseif event:match("^CHAT_MSG_") then
        Context.onChat(event, ...)
    elseif event == "PLAYER_REGEN_ENABLED" then
        if bindingPending then
            applyBinding()
        end
        if setupPending then
            local request = setupPending
            setupPending = nil
            startSetup(request)
        end
    end
end)

SLASH_VOICEFOREVER1 = "/vf"
SlashCmdList.VOICEFOREVER = function(msg)
    local cmd, rest = trim(msg or ""):match("^(%S*)%s*(.-)$")
    cmd = (cmd or ""):lower()
    if cmd == "key" and rest ~= "" then
        VoiceForeverDB.key = rest:upper()
        applyBinding()
        print(T("key_set", VoiceForeverDB.key))
    elseif cmd == "test" then
        deliver(PREFIX .. ":group::" .. L.test_line)
    elseif cmd == "names" then
        local s, online = Context.counts()
        local channels = {}
        for _, ch in ipairs(s.channels) do
            table.insert(channels, ch.n .. ". " .. ch.name)
        end
        print(T("names_info", #channels > 0 and table.concat(channels, ", ") or L.no_channels,
            #s.group, #s.guild, online, #s.friends, #s.whispers, #s.speakers))
    elseif cmd == "ptt" and rest == "off" then
        VoiceForeverDB.ptt = nil
        applyBinding()
        print(L.ptt_freed)
    elseif cmd == "ptt" then
        local ptt = pttSettings()
        if ptt.key then
            print(T("ptt_info", ptt.key, ptt.mode == "hold" and L.mode_hold or L.mode_toggle))
        else
            print(L.ptt_none)
        end
    else
        local group = groupChannel()
        print(T("info", VoiceForeverDB.key, group and L[group] or L.no_group))
        print(L.help)
    end
end
