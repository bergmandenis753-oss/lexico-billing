local function safe_filename(s)
  local cleaned = tostring(s or ""):gsub("[^%w%._%-]", "_")
  return cleaned
end

local function first_script_arg()
  local sources = {argv, arg}
  for _, source in ipairs(sources) do
    if source then
      for i = 1, #source do
        local value = tostring(source[i] or "")
        if value ~= "" and not value:match("%.lua$") then
          return value
        end
      end
    end
  end
  return ""
end

local uuid = first_script_arg()

if uuid == "" then
  freeswitch.consoleLog("error", "[billing] answer marker missing uuid\n")
  return
end

local path = "/tmp/billing_answer_" .. safe_filename(uuid) .. ".ts"
local f = io.open(path, "w")
if not f then
  freeswitch.consoleLog("error", "[billing] cannot write answer marker " .. path .. "\n")
  return
end

f:write(tostring(os.time()))
f:close()
freeswitch.consoleLog("info", "[billing] answer marked uuid=" .. uuid .. "\n")
