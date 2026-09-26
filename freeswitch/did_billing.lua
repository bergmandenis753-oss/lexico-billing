local API = "https://web-production-d5e1c.up.railway.app"
local KEY_FILE = "/etc/freeswitch/billing_api_key"

local did = session:getVariable("destination_number") or ""
local caller_id = session:getVariable("caller_id_number") or session:getVariable("sip_from_user") or ""
local source_ip = session:getVariable("network_addr") or session:getVariable("sip_received_ip") or ""
local uuid = session:getVariable("uuid") or tostring(os.time())

local function trim(value)
  local cleaned = (value or ""):gsub("^%s+", "")
  return cleaned:gsub("%s+$", "")
end

local function read_api_key()
  local file = io.open(KEY_FILE, "r")
  if not file then return "" end
  local key = trim(file:read("*a"))
  file:close()
  return key
end

local function shell_quote(value)
  return "'" .. tostring(value or ""):gsub("'", "'\\''") .. "'"
end

local function json_escape(value)
  return tostring(value or ""):gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function json_unescape(value)
  if not value then return "" end
  return value:gsub('\\"', '"'):gsub("\\n", "\n"):gsub("\\r", "\r"):gsub("\\\\", "\\")
end

local function jstr(body, key)
  return json_unescape(body:match('"' .. key .. '"%s*:%s*"(.-)"'))
end

local function jnum(body, key)
  return tonumber(body:match('"' .. key .. '"%s*:%s*([%-0-9%.]+)'))
end

local function http_post(path, payload)
  local key = read_api_key()
  if key == "" then return 0, "missing API key" end
  local output = "/tmp/did_bill_" .. uuid:gsub("[^%w%._%-]", "_") .. ".out"
  local command = table.concat({
    "curl -s -m 8", "-o " .. shell_quote(output), "-w '%{http_code}'",
    "-H " .. shell_quote("Content-Type: application/json"),
    "-H " .. shell_quote("Authorization: Bearer " .. key),
    "-X POST", "--data-binary " .. shell_quote(payload), shell_quote(API .. path)
  }, " ")
  local process = io.popen(command)
  local code = process:read("*a")
  process:close()
  local file = io.open(output, "r")
  local body = file and file:read("*a") or ""
  if file then file:close() end
  os.remove(output)
  return tonumber(code) or 0, body
end

local reserve_payload = string.format(
  '{"did_number":"%s","caller_id":"%s","source_ip":"%s","call_uuid":"%s"}',
  json_escape(did), json_escape(caller_id), json_escape(source_ip), json_escape(uuid)
)
local code, body = http_post("/api/dids/reserve", reserve_payload)
if code ~= 200 then
  freeswitch.consoleLog("warning", "[did] reserve rejected (" .. code .. "): " .. body .. "\n")
  session:execute("respond", code == 404 and "404" or "403")
  return
end

local bridge_target = jstr(body, "bridge_target")
local backup_target = jstr(body, "backup_bridge_target")
local max_seconds = jnum(body, "max_seconds") or 0
if bridge_target == "" or max_seconds <= 0 then
  freeswitch.consoleLog("error", "[did] invalid reserve response: " .. body .. "\n")
  session:execute("respond", "500")
  return
end

session:execute("set", "execute_on_answer=sched_hangup +" .. math.floor(max_seconds) .. " normal_clearing")
session:execute("set", "continue_on_fail=true")
session:execute("set", "hangup_after_bridge=true")
session:execute("bridge", bridge_target)
if (tonumber(session:getVariable("billsec")) or 0) <= 0 and backup_target ~= "" and session:ready() then
  freeswitch.consoleLog("notice", "[did] primary route failed, trying backup\n")
  session:execute("bridge", backup_target)
end

local billsec = math.floor(tonumber(session:getVariable("billsec")) or tonumber(session:getVariable("bridge_billsec")) or 0)
local hangup_cause = session:getVariable("bridge_hangup_cause") or session:getVariable("hangup_cause") or ""
local result = billsec > 0 and "Normal" or hangup_cause
local finalize_payload = string.format(
  '{"call_uuid":"%s","billsec":%d,"hangup_cause":"%s","result":"%s"}',
  json_escape(uuid), billsec, json_escape(hangup_cause), json_escape(result)
)
local final_code, final_body = http_post("/api/dids/finalize", finalize_payload)
if final_code ~= 200 then
  freeswitch.consoleLog("error", "[did] finalize failed (" .. final_code .. "): " .. final_body .. "\n")
else
  freeswitch.consoleLog("info", "[did] finalized: " .. final_body .. "\n")
end
