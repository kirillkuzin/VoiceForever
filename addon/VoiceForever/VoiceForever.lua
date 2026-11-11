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

local PREFIX = "VF"
local DEFAULT_KEY = "CTRL-SHIFT-F12"
local FOCUS_TIMEOUT = 3 -- seconds; drop focus if the agent never pressed Enter
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

local Badge -- defined below, used by deliver()
local handleControl

local LABELS = {
    SAY = "Сказать", YELL = "Крик", EMOTE = "Эмоция", PARTY = "Группа", RAID = "Рейд",
    RAID_WARNING = "Объявление рейду", INSTANCE_CHAT = "Подземелье", GUILD = "Гильдия",
    OFFICER = "Офицеры", WHISPER = "Шёпот", CHANNEL = "Канал",
}

local function deliver(line)
    local channel, target, text = VoiceForever_Parse(line)
    if not channel then
        if line ~= "" then
            print("не понял строку от агента, пропускаю")
        end
        return
    end
    if channel == "ptt" or channel == "status" then
        handleControl(channel, target, text)
        return
    end
    if text == "" then
        return
    end
    local chatType, extra = VoiceForever_Resolve(channel, target)
    if not chatType then
        print("не отправлено (" .. extra .. "): " .. text)
        Badge.result(false, "Не отправлено: " .. extra)
        return
    end
    if chatType == "WHISPER" or chatType == "CHANNEL" then
        SendChat(text, chatType, nil, extra)
    else
        SendChat(text, chatType)
    end
    local label = LABELS[chatType] or chatType
    if chatType == "WHISPER" then
        label = label .. ": " .. tostring(extra)
    end
    Badge.result(true, label)
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
        badgeText:SetText(string.format("|cffff4040●|r ЗАПИСЬ %d:%02d", math.floor(secs / 60), secs % 60))
    elseif state.toast and state.toastUntil and now < state.toastUntil then
        badgeText:SetText(state.toast)
    elseif state.pending > 0 then
        badgeText:SetText("|cffffd100…|r Распознаю")
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
setupText:SetText("VoiceForever: нажми на геймпаде кнопку записи голоса\n(Esc — отмена)")

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
        print("кнопка записи: " .. key .. ". Раньше на ней было «" .. bindingName(previous)
            .. "», пока аддон включён, она записывает голос.")
    else
        print("кнопка записи: " .. key .. ", в игре на ней ничего не было.")
    end
    ReloadUI() -- writes SavedVariables, which the agent reads
end

local function startSetup(request)
    if InCombatLockdown() then
        setupPending = request
        print("выбор кнопки записи откроется после боя")
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
        print("выбор кнопки записи отменён")
    end
end)
setup:SetScript("OnUpdate", function()
    if setupRequest and GetTime() - setupRequest.openedAt > SETUP_TIMEOUT then
        setupRequest = nil
        finishSetup(nil)
        print("кнопку записи так и не нажали, попробуй ещё раз из окна агента")
    end
end)

handleControl = function(channel, target, text)
    if channel == "status" then
        Badge.result(false, text ~= "" and text or "не отправлено")
        return
    end
    local mode, max, nonce = text:match("^(%a+)%s+([%d%.]+)%s*(%S*)$")
    if mode ~= "toggle" and mode ~= "hold" then
        print("не понял настройку записи от агента")
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
events:RegisterEvent("PLAYER_REGEN_ENABLED")
events:SetScript("OnEvent", function(_, event)
    if event == "PLAYER_LOGIN" then
        VoiceForeverDB = VoiceForeverDB or {}
        VoiceForeverDB.key = VoiceForeverDB.key or DEFAULT_KEY
        applyBinding()
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
        print("клавиша агента: " .. VoiceForeverDB.key .. ". Укажи такую же в config.toml агента.")
    elseif cmd == "test" then
        deliver(PREFIX .. ":group::VoiceForever работает")
    elseif cmd == "ptt" and rest == "off" then
        VoiceForeverDB.ptt = nil
        applyBinding()
        print("кнопка записи освобождена, игра снова делает на ней своё")
    elseif cmd == "ptt" then
        local ptt = pttSettings()
        if ptt.key then
            print("кнопка записи: " .. ptt.key .. ", режим: " .. (ptt.mode == "hold" and "держать" or "нажать и нажать ещё раз")
                .. ". /vf ptt off — освободить её")
        else
            print("кнопка записи не выбрана: выбери её в окне агента, шаг «Проверка в игре»")
        end
    else
        local group = groupChannel() or "нет группы"
        print("клавиша агента: " .. VoiceForeverDB.key .. ", группа сейчас: " .. group)
        print("/vf key CTRL-SHIFT-F12 — сменить клавишу, /vf test — тест в группу, /vf ptt — кнопка записи")
    end
end
