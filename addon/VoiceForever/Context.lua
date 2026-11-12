-- VoiceForever: what the game knows about the people and channels around the player.
--
-- The server uses it to spell nicknames right (speech recognition hint, Claude, whisper
-- targets) and to find a chat channel by its name. The addon keeps a snapshot:
--   channels  joined chat channels: number and name
--   group     party or raid members
--   guild     guild members online or seen in the last 14 days
--   friends   the friends list (and Battle.net friends playing this game)
--   whispers  who whispered the player or was whispered, newest first
--   speakers  who wrote in say, yell, group, guild or a custom channel, newest first
--   target    the targeted player
-- Only who, where and when: the text of other people's messages is never kept.
--
-- The snapshot reaches the agent two ways: in SavedVariables (written by the game on
-- /reload and logout) and live, when the agent asks with "VF:export:<nonce>:". The addon
-- then puts "VFX1 <nonce> <json>" into its hidden edit box, selected, and the agent copies
-- it with Ctrl+C. Addons cannot touch the clipboard themselves.

local _, ns = ...

local Context = {}
ns.Context = Context

local MAX_WHISPERS = 20
local MAX_SPEAKERS = 30
local MAX_GUILD = 300
local MAX_FRIENDS = 100
local GUILD_DAYS = 14
local ROSTER_EVERY = 60 -- seconds between guild roster requests
local SNAPSHOT_VERSION = 1

-- Chat events whose authors go to `speakers`, and the channel they are written to.
local SPEAKER_EVENTS = {
    CHAT_MSG_SAY = "say",
    CHAT_MSG_YELL = "yell",
    CHAT_MSG_PARTY = "party",
    CHAT_MSG_PARTY_LEADER = "party",
    CHAT_MSG_RAID = "raid",
    CHAT_MSG_RAID_LEADER = "raid",
    CHAT_MSG_RAID_WARNING = "raid",
    CHAT_MSG_INSTANCE_CHAT = "instance",
    CHAT_MSG_INSTANCE_CHAT_LEADER = "instance",
    CHAT_MSG_GUILD = "guild",
    CHAT_MSG_OFFICER = "officer",
    CHAT_MSG_CHANNEL = "channel",
}

local whispers = {} -- { name, dir = "in" | "out", at = time() }
local speakers = {} -- { name, chan, channel (name, for chan "channel"), at = time() }
local rosterAskedAt = nil

local function now()
    return time and time() or 0
end

local function myName()
    local name = UnitName and UnitName("player")
    return name
end

-- "Name-Realm" for someone from another realm, "Name" for someone from ours.
local function short(name)
    if type(name) ~= "string" or name == "" then
        return nil
    end
    if Ambiguate then
        return Ambiguate(name, "none")
    end
    local realm = GetRealmName and GetRealmName():gsub("%s", "") or ""
    local base, other = name:match("^([^-]+)%-(.+)$")
    if base and other == realm then
        return base
    end
    return name
end

local function fullName(name, realm)
    if realm and realm ~= "" then
        return short(name .. "-" .. realm:gsub("%s", ""))
    end
    return name
end

local function isMe(name)
    local me = myName()
    return me ~= nil and (name == me or short(name) == me)
end

-- Put an entry first, dropping an older one for the same name; keep at most `max`.
local function remember(list, entry, max)
    for i = #list, 1, -1 do
        if list[i].name == entry.name then
            table.remove(list, i)
        end
    end
    table.insert(list, 1, entry)
    while #list > max do
        table.remove(list)
    end
end

function Context.onChat(event, ...)
    local _, author = ...
    local name = short(author)
    if not name or isMe(name) then
        return
    end
    if event == "CHAT_MSG_WHISPER" or event == "CHAT_MSG_WHISPER_INFORM" then
        remember(whispers, { name = name, dir = event == "CHAT_MSG_WHISPER" and "in" or "out", at = now() }, MAX_WHISPERS)
        return
    end
    local chan = SPEAKER_EVENTS[event]
    if not chan then
        return
    end
    local entry = { name = name, chan = chan, at = now() }
    if chan == "channel" then
        local zoneChannelID, _, baseName = select(7, ...)
        -- General, Trade and the other zone channels are full of strangers: only custom ones.
        if (zoneChannelID and zoneChannelID ~= 0) or not baseName or baseName == "" then
            return
        end
        entry.channel = baseName
    end
    remember(speakers, entry, MAX_SPEAKERS)
end

-- ------------------------------------------------------------------ lists from the game API
local function pack(...)
    return { n = select("#", ...), ... }
end

local function channelList()
    local out = {}
    if not GetChannelList then
        return out
    end
    local list = pack(GetChannelList())
    for i = 1, list.n, 3 do
        local id, name = list[i], list[i + 1]
        if type(id) == "number" and type(name) == "string" and name ~= "" then
            table.insert(out, { n = id, name = name })
        end
    end
    return out
end

-- Number of the joined channel called `name` (as the server got it from the snapshot), or nil.
function Context.findChannel(name)
    if not name or name == "" then
        return nil
    end
    local lower = name:lower()
    for _, ch in ipairs(channelList()) do
        if ch.name == name or ch.name:lower() == lower then
            return ch.n
        end
    end
    if GetChannelName then
        local id, joined = GetChannelName(name)
        if type(id) == "number" and id > 0 and joined then
            return id
        end
    end
    return nil
end

local function groupList()
    local out = {}
    if IsInRaid and IsInRaid() and GetRaidRosterInfo then
        for i = 1, (GetNumGroupMembers and GetNumGroupMembers() or 0) do
            local name = short(GetRaidRosterInfo(i))
            if name and not isMe(name) then
                table.insert(out, name)
            end
        end
    elseif UnitName then
        for i = 1, 4 do
            local name, realm = UnitName("party" .. i)
            if name and name ~= "" then
                table.insert(out, fullName(name, realm))
            end
        end
    end
    return out
end

local function askRoster()
    local ask = (C_GuildInfo and C_GuildInfo.GuildRoster) or GuildRoster
    if ask and IsInGuild and IsInGuild() then
        rosterAskedAt = GetTime()
        ask()
    end
end

local function guildList()
    local out = {}
    if not (IsInGuild and IsInGuild() and GetNumGuildMembers and GetGuildRosterInfo) then
        return out
    end
    if not rosterAskedAt or GetTime() - rosterAskedAt > ROSTER_EVERY then
        askRoster() -- the answer is for the next snapshot; the client has the last one
    end
    local members = {}
    for i = 1, (GetNumGuildMembers() or 0) do
        local name, _, _, _, _, _, _, _, online = GetGuildRosterInfo(i)
        name = short(name)
        if name and not isMe(name) then
            local hours = 0
            if not online and GetGuildRosterLastOnline then
                local years, months, days, h = GetGuildRosterLastOnline(i)
                hours = (((years or 0) * 12 + (months or 0)) * 30 + (days or 0)) * 24 + (h or 0)
            end
            if online or hours <= GUILD_DAYS * 24 then
                table.insert(members, { name = name, online = online and true or false, hours = hours })
            end
        end
    end
    table.sort(members, function(a, b)
        if a.online ~= b.online then
            return a.online
        end
        if a.hours ~= b.hours then
            return a.hours < b.hours
        end
        return a.name < b.name
    end)
    for i = 1, math.min(#members, MAX_GUILD) do
        table.insert(out, { name = members[i].name, online = members[i].online })
    end
    return out
end

local function friendList()
    local out, seen = {}, {}
    local function add(name, online)
        name = short(name)
        if name and not seen[name] and #out < MAX_FRIENDS then
            seen[name] = true
            table.insert(out, { name = name, online = online and true or false })
        end
    end
    if C_FriendList and C_FriendList.GetNumFriends and C_FriendList.GetFriendInfoByIndex then
        for i = 1, C_FriendList.GetNumFriends() or 0 do
            local info = C_FriendList.GetFriendInfoByIndex(i)
            if info and info.name then
                add(info.name, info.connected)
            end
        end
    end
    -- Battle.net friends who play this same game right now: their character names.
    if BNGetNumFriends and C_BattleNet and C_BattleNet.GetFriendAccountInfo then
        for i = 1, (BNGetNumFriends()) or 0 do
            local account = C_BattleNet.GetFriendAccountInfo(i)
            local game = account and account.gameAccountInfo
            if game and game.isOnline and game.characterName and game.characterName ~= ""
                and game.clientProgram == (BNET_CLIENT_WOW or "WoW")
                and (not WOW_PROJECT_ID or game.wowProjectID == WOW_PROJECT_ID) then
                add(fullName(game.characterName, game.realmName), true)
            end
        end
    end
    return out
end

local function targetName()
    if UnitIsPlayer and UnitIsPlayer("target") and UnitName then
        local name, realm = UnitName("target")
        if name and not isMe(name) then
            return fullName(name, realm)
        end
    end
    return nil
end

-- "Name-Realm" of the player: the key of this character's saved snapshot.
local function key()
    local me = myName()
    if not me then
        return nil
    end
    return me .. "-" .. ((GetRealmName and GetRealmName() or ""):gsub("%s", ""))
end

local function aged(list, at)
    local out = {}
    for _, e in ipairs(list) do
        local copy = { name = e.name, ago = math.max(0, at - (e.at or at)) }
        for k, v in pairs(e) do
            if k ~= "at" and k ~= "name" then
                copy[k] = v
            end
        end
        table.insert(out, copy)
    end
    return out
end

-- Everything above as one table; "ago" values are seconds before `at`.
function Context.snapshot()
    local at = now()
    return {
        v = SNAPSHOT_VERSION,
        at = at,
        me = key(),
        realm = GetRealmName and GetRealmName() or nil,
        locale = GetLocale and GetLocale() or nil,
        channels = channelList(),
        group = groupList(),
        guild = guildList(),
        friends = friendList(),
        whispers = aged(whispers, at),
        speakers = aged(speakers, at),
        target = targetName(),
    }
end

function Context.counts()
    local s = Context.snapshot()
    local online = 0
    for _, m in ipairs(s.guild) do
        if m.online then
            online = online + 1
        end
    end
    return s, online
end

-- ------------------------------------------------------------------ saved between sessions
-- Called on PLAYER_LOGOUT, which comes right before the game writes SavedVariables
-- (also on /reload).
function Context.save(db)
    local k = key()
    if not k then
        return
    end
    db.chars = db.chars or {}
    db.chars[k] = Context.snapshot()
end

-- After a /reload: the recent whispers and speakers of this character come back.
function Context.restore(db)
    local k = key()
    local saved = k and db.chars and db.chars[k]
    if type(saved) ~= "table" or type(saved.at) ~= "number" then
        return
    end
    local function back(list, into, max)
        for _, e in ipairs(type(list) == "table" and list or {}) do
            if type(e) == "table" and type(e.name) == "string" and #into < max then
                local copy = { at = saved.at - (tonumber(e.ago) or 0) }
                for k2, v in pairs(e) do
                    if k2 ~= "ago" then
                        copy[k2] = v
                    end
                end
                table.insert(into, copy)
            end
        end
    end
    if #whispers == 0 then
        back(saved.whispers, whispers, MAX_WHISPERS)
    end
    if #speakers == 0 then
        back(saved.speakers, speakers, MAX_SPEAKERS)
    end
end

function Context.start()
    askRoster()
    if C_FriendList and C_FriendList.ShowFriends then
        C_FriendList.ShowFriends()
    end
end

function Context.events()
    local list = { "CHAT_MSG_WHISPER", "CHAT_MSG_WHISPER_INFORM" }
    for event in pairs(SPEAKER_EVENTS) do
        table.insert(list, event)
    end
    table.sort(list)
    return list
end

-- ------------------------------------------------------------------ JSON
local ESCAPES = { ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\r"] = "\\r", ["\t"] = "\\t" }

local function escape(c)
    -- "|" starts an escape sequence in WoW edit boxes, so it never goes in raw.
    return ESCAPES[c] or string.format("\\u%04x", c:byte())
end

local function encode(value)
    local kind = type(value)
    if kind == "string" then
        return '"' .. value:gsub('[%c"\\|]', escape) .. '"'
    elseif kind == "number" then
        if value == math.floor(value) then
            return string.format("%d", value)
        end
        return string.format("%.3f", value)
    elseif kind == "boolean" then
        return value and "true" or "false"
    elseif kind == "table" then
        local parts = {}
        if #value > 0 or next(value) == nil then
            for _, item in ipairs(value) do
                table.insert(parts, encode(item))
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for k in pairs(value) do
            table.insert(keys, tostring(k))
        end
        table.sort(keys)
        for _, k in ipairs(keys) do
            table.insert(parts, encode(k) .. ":" .. encode(value[k]))
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end
Context.encode = encode

-- The line the agent copies: "VFX1 <nonce> <json>".
function Context.exportLine(nonce)
    return "VFX1 " .. nonce .. " " .. encode(Context.snapshot())
end
