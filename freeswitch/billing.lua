local API = "https://web-production-d5e1c.up.railway.app"
local KEY_FILE = "/etc/freeswitch/billing_api_key"

local client_ip = session:getVariable("network_addr") or session:getVariable("sip_received_ip") or ""
local dest = session:getVariable("destination_number") or ""
local uuid = session:getVariable("uuid") or tostring(os.time())

local function trim(s)
  return (s or ""):gsub("^%s+", ""):gsub("%s+$", "")
end

local function read_api_key()
  local f = io.open(KEY_FILE, "r")
  if not f then return "" end
  local key = trim(f:read("*a"))
  f:close()
  return key
end

local API_KEY = read_api_key()

local function shell_quote(s)
  s = tostring(s or "")
  return "'" .. s:gsub("'", "'\\''") .. "'"
end

local function json_escape(s)
  s = tostring(s or "")
  s = s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
  return s
end

local function json_unescape(s)
  if not s then return nil end
  s = s:gsub('\\"', '"'):gsub("\\n", "\n"):gsub("\\r", "\r"):gsub("\\\\", "\\")
  return s
end

local function jstr(body, key)
  return json_unescape(body:match('"' .. key .. '"%s*:%s*"(.-)"'))
end

local function jnum(body, key)
  return tonumber(body:match('"' .. key .. '"%s*:%s*([%-0-9%.]+)'))
end

local function http_post(path, json)
  if API_KEY == "" then
    return 0, "missing API key file"
  end
  local out = "/tmp/bill_" .. uuid .. ".out"
  local cmd = table.concat({
    "curl -s -m 8",
    "-o " .. shell_quote(out),
    "-w '%{http_code}'",
    "-H " .. shell_quote("Content-Type: application/json"),
    "-H " .. shell_quote("Authorization: Bearer " .. API_KEY),
    "-X POST",
    "--data-binary " .. shell_quote(json),
    shell_quote(API .. path)
  }, " ")
  local h = io.popen(cmd)
  local code = h:read("*a"); h:close()
  local f = io.open(out, "r")
  local body = f and f:read("*a") or ""
  if f then f:close() end
  os.remove(out)
  return tonumber(code) or 0, body
end

local rjson = string.format(
  '{"sip_ip":"%s","destination":"%s","call_uuid":"%s"}',
  json_escape(client_ip), json_escape(dest), json_escape(uuid)
)

local code, body = http_post("/api/reserve", rjson)
if code ~= 200 then
  freeswitch.consoleLog("warning", "[billing] reserve rejected (" .. code .. "): " .. body .. "\n")
  session:hangup("CALL_REJECTED")
  return
end

local max_seconds = jnum(body, "max_seconds")
local sell = jnum(body, "sell_rate_cents")
local cost = jnum(body, "cost_rate_cents")
local client_id = jnum(body, "client_id")
local gateway = trim(jstr(body, "gateway_name") or "")
local route_ip = trim(jstr(body, "route_ip") or "")
local tech_prefix = jstr(body, "tech_prefix") or ""
local dial_destination = jstr(body, "dial_destination") or dest

if not max_seconds or max_seconds <= 0 or not client_id or not sell or not cost then
  freeswitch.consoleLog("warning", "[billing] invalid reserve response: " .. body .. "\n")
  session:hangup("CALL_REJECTED")
  return
end

if gateway == "" and route_ip == "" then
  freeswitch.consoleLog("warning", "[billing] reserve has neither gateway nor route_ip: " .. body .. "\n")
  session:hangup("CALL_REJECTED")
  return
end

session:execute("set", "execute_on_answer=sched_hangup +" .. max_seconds .. " normal_clearing")
local dial_number = tech_prefix .. dial_destination
local bridge_target = ""
local used_route = gateway
if gateway ~= "" then
  bridge_target = "sofia/gateway/" .. gateway .. "/" .. dial_number
else
  bridge_target = "sofia/external/" .. dial_number .. "@" .. route_ip
  used_route = "direct:" .. route_ip
end

freeswitch.consoleLog("info", "[billing] bridge " .. bridge_target .. "\n")
session:execute("bridge", bridge_target)

local billsec = tonumber(session:getVariable("billsec")) or 0
local fjson = string.format(
  '{"client_id":%d,"call_uuid":"%s","destination":"%s","billsec":%d,"sell_rate_cents":%d,"cost_rate_cents":%d,"gateway_name":"%s"}',
  client_id, json_escape(uuid), json_escape(dest), billsec, sell, cost, json_escape(used_route)
)
local fcode, fbody = http_post("/api/finalize", fjson)
if fcode ~= 200 then
  freeswitch.consoleLog("error", "[billing] finalize error (" .. fcode .. "): " .. fbody .. "\n")
end
