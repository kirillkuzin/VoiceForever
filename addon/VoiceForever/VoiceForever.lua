-- VoiceForever: receives lines from the VoiceForever desktop agent and sends them to chat.
--
-- The agent presses a key (default CTRL-SHIFT-F12), which focuses an invisible edit box,
-- pastes "VF:<channel>:<target>:<text>" and presses Enter. The normal chat window never opens.
-- The addon has no network access: everything arrives as keyboard input.

local PREFIX = "VF"
local DEFAULT_KEY = "CTRL-SHIFT-F12"
local FOCUS_TIMEOUT = 3 -- seconds; drop focus if the agent never pressed Enter

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
            return nil, "ты не в группе"
        end
        return chatType
    elseif channel == "whisper" then
        if target == "" then
            return nil, "не указан получатель шёпота"
        end
        return "WHISPER", target
    elseif channel == "reply" then
        local name = lastTellTarget()
        if not name then
            return nil, "тебе ещё никто не писал, некому ответить"
        end
        return "WHISPER", name
    elseif channel == "channel" then
        local number = tonumber(target)
        if not number then
            return nil, "неизвестный номер канала"
        end
        return "CHANNEL", number
    elseif channel == "guild" or channel == "officer" then
        if IsInGuild and not IsInGuild() then
            return nil, "ты не в гильдии"
        end
        return SIMPLE[channel]
    end
    local chatType = SIMPLE[channel]
    if not chatType then
        return nil, "неизвестный канал " .. tostring(channel)
    end
    return chatType
end

local function deliver(line)
    local channel, target, text = VoiceForever_Parse(line)
    if not channel then
        if line ~= "" then
            print("не понял строку от агента, пропускаю")
        end
        return
    end
    if text == "" then
        return
    end
    local chatType, extra = VoiceForever_Resolve(channel, target)
    if not chatType then
        print("не отправлено (" .. extra .. "): " .. text)
        return
    end
    if chatType == "WHISPER" or chatType == "CHANNEL" then
        SendChat(text, chatType, nil, extra)
    else
        SendChat(text, chatType)
    end
end

-- Invisible edit box that the agent types into.
local box = CreateFrame("EditBox", "VoiceForeverEditBox", UIParent)
box:SetAutoFocus(false)
box:SetFontObject(ChatFontNormal)
box:SetMaxLetters(1000)
box:SetSize(200, 20)
box:SetPoint("BOTTOMLEFT", UIParent, "BOTTOMLEFT", 0, 0)
box:SetAlpha(0)
box:SetScript("OnEnterPressed", function(self)
    local line = self:GetText() or ""
    self:SetText("")
    self:ClearFocus()
    deliver(line)
end)
box:SetScript("OnEscapePressed", function(self)
    self:SetText("")
    self:ClearFocus()
end)
box:SetScript("OnEditFocusGained", function(self)
    self.focusedAt = GetTime()
end)
box:SetScript("OnUpdate", function(self)
    if self.focusedAt and self:HasFocus() and GetTime() - self.focusedAt > FOCUS_TIMEOUT then
        self.focusedAt = nil
        self:SetText("")
        self:ClearFocus()
    end
end)

-- Hidden button the key binding clicks.
local button = CreateFrame("Button", "VoiceForeverFocusButton", UIParent)
button:SetScript("OnClick", function()
    box:SetText("")
    box:SetFocus()
end)

local bindingPending = false

local function applyBinding()
    if InCombatLockdown() then
        bindingPending = true
        return
    end
    bindingPending = false
    ClearOverrideBindings(button)
    SetOverrideBindingClick(button, true, VoiceForeverDB.key, "VoiceForeverFocusButton")
end

local events = CreateFrame("Frame")
events:RegisterEvent("PLAYER_LOGIN")
events:RegisterEvent("PLAYER_REGEN_ENABLED")
events:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_LOGIN" then
        VoiceForeverDB = VoiceForeverDB or {}
        VoiceForeverDB.key = VoiceForeverDB.key or DEFAULT_KEY
        applyBinding()
    elseif event == "PLAYER_REGEN_ENABLED" and bindingPending then
        applyBinding()
    end
end)

SLASH_VOICEFOREVER1 = "/vf"
SlashCmdList.VOICEFOREVER = function(msg)
    local cmd, rest = trim(msg or ""):match("^(%S*)%s*(.-)$")
    cmd = (cmd or ""):lower()
    if cmd == "key" and rest ~= "" then
        VoiceForeverDB.key = rest:upper()
        applyBinding()
        print("клавиша агента: " .. VoiceForeverDB.key .. ". Укажи такую же в config.toml агента.")
    elseif cmd == "test" then
        deliver(PREFIX .. ":group::VoiceForever работает")
    else
        local group = groupChannel() or "нет группы"
        print("клавиша агента: " .. VoiceForeverDB.key .. ", группа сейчас: " .. group)
        print("/vf key CTRL-SHIFT-F12 — сменить клавишу, /vf test — тест в группу")
    end
end
