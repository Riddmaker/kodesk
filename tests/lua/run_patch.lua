-- Runs kodesk's reader patch (B-26) under LuaJIT with stand-ins for the KOReader modules it uses.
-- Usage: luajit run_patch.lua <patch file> <data dir>
-- The stand-ins behave like KOReader v2026.07.2 where the patch relies on them: luajson marks
-- arrays with a metatable and decodes null to a function; LuaSettings reads with dofile and
-- writes KOReader's dump layout; lua-ljsqlite3's exec returns columns of values.

local patch, data_dir = arg[1], arg[2]
local cjson = require("cjson")
local lfs = require("lfs")
local sqlite3 = require("lsqlite3")

local ArrayMT = { __is_luajson_array = true }
local function null() return null end

local function mark(value)
    if value == cjson.null then return null end
    if type(value) ~= "table" then return value end
    for key, item in pairs(value) do value[key] = mark(item) end
    if #value > 0 then setmetatable(value, ArrayMT) end
    return value
end

package.preload["json"] = function()
    return {
        decode = function(text) return mark(cjson.decode(text)) end,
        encode = function(value) return cjson.encode(value) end,
    }
end
package.preload["libs/libkoreader-lfs"] = function() return lfs end
package.preload["datastorage"] = function()
    return { getDataDir = function() return data_dir end }
end
package.preload["logger"] = function()
    return { warn = function(...) io.stderr:write(table.concat({ ... }, " "), "\n") end }
end

local function sorted_keys(t)
    local keys = {}
    for key in pairs(t) do table.insert(keys, key) end
    table.sort(keys, function(a, b)
        if type(a) == type(b) then return a < b end
        return tostring(a) < tostring(b)
    end)
    return keys
end

local function dump(value, indent)
    if type(value) == "string" then return string.format("%q", value) end
    if type(value) ~= "table" then return tostring(value) end
    if next(value) == nil then return "{}" end
    local pad = string.rep("    ", indent + 1)
    local out = { "{\n" }
    for _, key in ipairs(sorted_keys(value)) do
        table.insert(out, pad .. "[" .. dump(key, 0) .. "] = " .. dump(value[key], indent + 1) .. ",\n")
    end
    table.insert(out, string.rep("    ", indent) .. "}")
    return table.concat(out)
end

package.preload["luasettings"] = function()
    local LuaSettings = {}
    LuaSettings.__index = LuaSettings
    function LuaSettings:open(file)
        local ok, data = pcall(dofile, file)
        return setmetatable({ file = file, data = ok and type(data) == "table" and data or {} }, self)
    end
    function LuaSettings:flush()
        if os.getenv("KODESK_TEST_BAD_FLUSH") then self.data.bad_flush = true end
        local handle = assert(io.open(self.file, "wb"))
        handle:write("-- " .. self.file .. "\nreturn " .. dump(self.data, 0) .. "\n")
        handle:close()
    end
    return LuaSettings
end

package.preload["lua-ljsqlite3/init"] = function()
    local function check(db, code)
        if code ~= sqlite3.OK and code ~= sqlite3.DONE and code ~= sqlite3.ROW then
            error(db:errmsg())
        end
    end
    local SQ3 = {}
    function SQ3.open(path)
        local db = assert(sqlite3.open(path))
        local conn = {}
        function conn:exec(sql)
            if not sql:match("^SELECT") then
                check(db, db:exec(sql))
                return nil
            end
            local statement = db:prepare(sql)
            if not statement then error(db:errmsg()) end
            local columns, rows = {}, 0
            while statement:step() == sqlite3.ROW do
                rows = rows + 1
                for i = 1, statement:columns() do
                    columns[i] = columns[i] or {}
                    columns[i][rows] = statement:get_value(i - 1)  -- NULL leaves a hole, as there
                end
            end
            statement:finalize()
            if not columns[1] then return nil end
            return columns
        end
        function conn:prepare(sql)
            local statement = db:prepare(sql)
            if not statement then error(db:errmsg()) end
            local wrapper = {}
            function wrapper:bind(...)
                local values = { ... }
                for i = 1, select("#", ...) do
                    if values[i] == nil then statement:bind(i, nil) else statement:bind(i, values[i]) end
                end
                return self
            end
            function wrapper:step() check(db, statement:step()) end
            function wrapper:clearbind() return self end  -- the next bind sets every value
            function wrapper:reset() statement:reset() return self end
            function wrapper:close() statement:finalize() end
            return wrapper
        end
        function conn:close() db:close() end
        return conn
    end
    return SQ3
end

dofile(patch)
