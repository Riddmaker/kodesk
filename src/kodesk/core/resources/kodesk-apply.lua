-- kodesk @VERSION@: applies Tuning-Garage changes that kodesk sent over Wi-Fi.
-- Source: https://github.com/Riddmaker/kodesk (GPL-3.0-or-later), written by kodesk itself.
--
-- KOReader runs this at every start (patch priority 1, before settings.reader.lua is opened).
-- Without kodesk/pending.json it returns at once. Otherwise it reads that file as JSON data,
-- never as code, and changes only the settings named in ALLOWED below, each only if it still
-- has the value kodesk saw. Before writing it copies the files to kodesk/backup/; after
-- writing it compares every other setting with the backup and puts the backup back on any
-- difference. The outcome goes to kodesk/result.json. No network, no commands.
-- Remove it like any patch (kodesk: Installed tab; or delete this file).

local ALLOWED = {
-- @ALLOWED@
}
local FILES = { reader = "settings.reader.lua", gestures = "settings/gestures.lua" }
local LIBRARY_DBS = {
    ["settings/bookinfo_cache.sqlite3"] = true,
    ["settings/PT_bookinfo_cache.sqlite3"] = true,
}
local LIBRARY_KEYS = {
    filemanager_display_mode = true,
    history_display_mode = true,
    collection_display_mode = true,
}
local MAX_BYTES, MAX_CHANGES, MAX_DEPTH, MAX_STRING = 65536, 200, 8, 4096
local KEEP_BACKUPS = 3

local lfs = require("libs/libkoreader-lfs")
local DataStorage = require("datastorage")
local logger = require("logger")

local data_dir = DataStorage:getDataDir()
local folder = data_dir .. "/kodesk"
local pending_path = folder .. "/pending.json"
if lfs.attributes(pending_path, "mode") ~= "file" then
    return
end

-- reading and checking the change file ------------------------------------------------------

local function read_file(path, limit)
    local handle = io.open(path, "rb")
    if not handle then return nil end
    local data = handle:read(limit + 1)
    handle:close()
    if data and #data > limit then error("file too large: " .. path) end
    return data or ""
end

local function write_file(path, data)
    local temp = path .. ".kodesk-tmp"
    local handle = assert(io.open(temp, "wb"))
    assert(handle:write(data))
    assert(handle:close())
    assert(os.rename(temp, path))
end

local function count(table_)
    local n = 0
    for _ in pairs(table_) do n = n + 1 end
    return n
end

local function is_array(value)
    -- KOReader's luajson marks arrays with a metatable, so only the shape counts here.
    return type(value) == "table" and count(value) == #value
end

local function is_key(key)
    if type(key) == "string" then
        return #key > 0 and #key <= 200
    end
    return type(key) == "number" and key == math.floor(key) and key >= -1e9 and key <= 1e9
end

-- A value: string, finite number, boolean, or {"table": [[key, value], ...]}.
local function decode_value(value, depth)
    local kind = type(value)
    if kind == "string" then
        if #value > MAX_STRING then error("string too long") end
        return value
    elseif kind == "number" then
        if value ~= value or value == math.huge or value == -math.huge then error("bad number") end
        return value
    elseif kind == "boolean" then
        return value
    elseif kind == "table" and depth < MAX_DEPTH and count(value) == 1 and is_array(value.table) then
        local out = {}
        for _, pair in ipairs(value.table) do
            if not is_array(pair) or #pair ~= 2 or not is_key(pair[1]) or out[pair[1]] ~= nil then
                error("bad table entry")
            end
            out[pair[1]] = decode_value(pair[2], depth + 1)
        end
        return out
    end
    error("value of an unknown kind")
end

local function optional(value, depth)
    if value == nil then return nil end
    return decode_value(value, depth)
end

local function check_change(change)
    if type(change) ~= "table" then error("bad change") end
    local checked = { target = change.target, raw = change }
    if change.old == nil and change.new == nil then error("change without old or new") end
    if change.target == "library" then
        if count(change) > 5 or not LIBRARY_DBS[change.db] or not LIBRARY_KEYS[change.key] then
            error("library change outside the allowed rows")
        end
        for _, side in ipairs({ "old", "new" }) do
            if change[side] ~= nil and (type(change[side]) ~= "string" or #change[side] > 200) then
                error("library value must be text")
            end
        end
        checked.db, checked.key, checked.old, checked.new = change.db, change.key, change.old, change.new
        return checked
    end
    local allowed = ALLOWED[change.target]
    local path = change.path
    if not allowed or count(change) > 4 or not is_array(path) or #path < 1 or #path > MAX_DEPTH then
        error("change outside the allowed settings")
    end
    for _, key in ipairs(path) do
        if not is_key(key) then error("bad key in path") end
    end
    if not allowed[path[1]] then error("setting not allowed: " .. tostring(path[1])) end
    checked.path = path
    checked.old = optional(change.old, #path)
    checked.new = optional(change.new, #path)
    return checked
end

local function read_pending(json)
    local document = json.decode(read_file(pending_path, MAX_BYTES))
    if type(document) ~= "table" or document.format ~= 1 or type(document.id) ~= "string"
            or not document.id:match("^[%w%-]+$") or #document.id > 64
            or not is_array(document.changes) or #document.changes > MAX_CHANGES then
        error("not a kodesk change file")
    end
    local changes = {}
    for i, change in ipairs(document.changes) do
        changes[i] = check_change(change)
    end
    return document.id, changes
end

-- applying ------------------------------------------------------------------------------------

local function deep_equal(a, b)
    if type(a) ~= type(b) then return false end
    if type(a) ~= "table" then return a == b end
    for key, value in pairs(a) do
        if not deep_equal(value, b[key]) then return false end
    end
    for key in pairs(b) do
        if a[key] == nil then return false end
    end
    return true
end

local function deep_copy(value)
    if type(value) ~= "table" then return value end
    local out = {}
    for key, item in pairs(value) do out[key] = deep_copy(item) end
    return out
end

local function get_path(data, path)
    local current = data
    for _, key in ipairs(path) do
        if type(current) ~= "table" then return nil end
        current = current[key]
    end
    return current
end

local function set_path(data, path, value)
    local current = data
    for i = 1, #path - 1 do
        local next_ = current[path[i]]
        if next_ == nil then
            if value == nil then return end
            next_ = {}
            current[path[i]] = next_
        end
        if type(next_) ~= "table" then error("not a table: " .. tostring(path[i])) end
        current = next_
    end
    current[path[#path]] = deep_copy(value)
end

local function prune_backups(backups)
    local names = {}
    for name in lfs.dir(backups) do
        if name:match("^[%w%-]+%.") then table.insert(names, name) end
    end
    table.sort(names)
    local ids = {}
    for _, name in ipairs(names) do
        local id = name:match("^([%w%-]+)%.")
        if ids[#ids] ~= id then table.insert(ids, id) end
    end
    local keep = {}
    for i = math.max(1, #ids - KEEP_BACKUPS + 1), #ids do keep[ids[i]] = true end
    for _, name in ipairs(names) do
        if not keep[name:match("^([%w%-]+)%.")] then os.remove(backups .. "/" .. name) end
    end
end

-- One settings file: backup, apply the keys whose old value still holds, flush, verify.
local function apply_settings(id, target, changes, report, restores)
    local LuaSettings = require("luasettings")
    local path = data_dir .. "/" .. FILES[target]
    if lfs.attributes(path, "mode") ~= "file" then
        for _, change in ipairs(changes) do
            table.insert(report.skipped, { change = change.raw, reason = "file missing" })
        end
        return
    end
    local backups = folder .. "/backup"
    if lfs.attributes(backups, "mode") ~= "directory" then assert(lfs.mkdir(backups)) end
    local backup = backups .. "/" .. id .. "." .. FILES[target]:gsub("/", "_")
    local original = read_file(path, 64 * 1024 * 1024)
    write_file(backup, original)
    table.insert(restores, function() write_file(path, original) end)
    local settings = LuaSettings:open(path)
    local expected = deep_copy(settings.data)
    local changed = false
    for _, change in ipairs(changes) do
        local current = get_path(settings.data, change.path)
        if deep_equal(current, change.old) then
            set_path(settings.data, change.path, change.new)
            set_path(expected, change.path, change.new)
            table.insert(report.applied, change.raw)
            changed = true
        elseif deep_equal(current, change.new) then
            table.insert(report.already, change.raw)
        else
            table.insert(report.skipped, { change = change.raw, reason = "changed on the reader" })
        end
    end
    if changed then
        settings:flush()
        if not deep_equal(LuaSettings:open(path).data, expected) then
            error(FILES[target] .. " reads back differently")
        end
    end
end

-- The library rows: one transaction, checked before it is committed.
local function apply_library(changes, report)
    local SQ3 = require("lua-ljsqlite3/init")
    local by_db = {}
    for _, change in ipairs(changes) do
        by_db[change.db] = by_db[change.db] or {}
        table.insert(by_db[change.db], change)
    end
    for db, list in pairs(by_db) do
        local path = data_dir .. "/" .. db
        if lfs.attributes(path, "mode") ~= "file" then
            for _, change in ipairs(list) do
                table.insert(report.skipped, { change = change.raw, reason = "file missing" })
            end
        else
            local conn = SQ3.open(path)
            local function config()
                local rows = {}
                local result = conn:exec("SELECT key, value FROM config;")
                if result then
                    for i, key in ipairs(result[1]) do rows[key] = result[2][i] end
                end
                return rows
            end
            local ok, err = pcall(function()
                conn:exec("BEGIN IMMEDIATE;")
                local before = config()
                local expected = deep_copy(before)
                local statement = conn:prepare("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?);")
                for _, change in ipairs(list) do
                    local current = before[change.key]
                    if current == change.old then
                        statement:bind(change.key, change.new)
                        statement:step()
                        statement:clearbind():reset()
                        expected[change.key] = change.new
                        table.insert(report.applied, change.raw)
                    elseif current == change.new then
                        table.insert(report.already, change.raw)
                    else
                        table.insert(report.skipped, { change = change.raw, reason = "changed on the reader" })
                    end
                end
                statement:close()
                if not deep_equal(config(), expected) then error(db .. " reads back differently") end
                conn:exec("COMMIT;")
            end)
            if not ok then pcall(function() conn:exec("ROLLBACK;") end) end
            conn:close()
            if not ok then error(err) end
        end
    end
end

local function run()
    local json = require("json")
    local report = { format = 1, applied = {}, already = {}, skipped = {}, restored = false }
    local restores = {}
    local ok, err = pcall(function()
        local id, changes = read_pending(json)
        report.id = id
        local groups = { reader = {}, gestures = {}, library = {} }
        for _, change in ipairs(changes) do table.insert(groups[change.target], change) end
        for _, target in ipairs({ "reader", "gestures" }) do
            if #groups[target] > 0 then apply_settings(id, target, groups[target], report, restores) end
        end
        if #groups.library > 0 then apply_library(groups.library, report) end
        if lfs.attributes(folder .. "/backup", "mode") == "directory" then
            prune_backups(folder .. "/backup")
        end
    end)
    if ok then
        os.remove(pending_path)
    else
        for i = #restores, 1, -1 do pcall(restores[i]) end
        report.restored = #restores > 0
        report.applied, report.already = {}, {}
        report.error = tostring(err)
        os.remove(folder .. "/pending.rejected.json")
        os.rename(pending_path, folder .. "/pending.rejected.json")
        logger.warn("kodesk-apply: nothing applied:", err)
    end
    write_file(folder .. "/result.json", json.encode(report))
end

local ok, err = pcall(run)
if not ok then
    logger.warn("kodesk-apply failed:", err)
    os.remove(folder .. "/pending.rejected.json")
    os.rename(pending_path, folder .. "/pending.rejected.json")
end
