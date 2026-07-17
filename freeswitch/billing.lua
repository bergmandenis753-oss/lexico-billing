local API = "https://web-production-d5e1c.up.railway.app"
local KEY_FILE = "/etc/freeswitch/billing_api_key"

local client_ip = session:getVariable("network_addr") or session:getVariable("sip_received_ip") or ""
local client_port = session:getVariable("network_port") or session:getVariable("sip_received_port") or session:getVariable("sip_network_port") or ""
local dest = session:getVariable("destination_number") or ""
local uuid = session:getVariable("uuid") or tostring(os.time())
local clid = session:getVariable("caller_id_number") or session:getVariable("sip_from_user") or ""
local user_agent = session:getVariable("sip_user_agent") or ""
local sip_call_id = session:getVariable("sip_call_id") or ""
local profile = session:getVariable("sofia_profile_name") or session:getVariable("sip_profile_name") or ""
local context = session:getVariable("context") or ""

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
  '{"sip_ip":"%s","sip_port":"%s","destination":"%s","call_uuid":"%s","clid":"%s","user_agent":"%s","sip_call_id":"%s","profile":"%s","context":"%s"}',
  json_escape(client_ip),
  json_escape(client_port),
  json_escape(dest),
  json_escape(uuid),
  json_escape(clid),
  json_escape(user_agent),
  json_escape(sip_call_id),
  json_escape(profile),
  json_escape(context)
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
local client_tech_prefix = jstr(body, "client_tech_prefix") or ""
local dial_destination = jstr(body, "dial_destination") or dest
local provider_number = jstr(body, "provider_number") or (tech_prefix .. dial_destination)
local terminator_id = jnum(body, "terminator_id") or 0
local terminator_name = jstr(body, "terminator_name") or ""
local terminator_destination_name = jstr(body, "terminator_destination_name") or ""
local terminator_prefix = jstr(body, "terminator_prefix") or ""
local terminator_tech_prefix = jstr(body, "terminator_tech_prefix") or tech_prefix

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
local dial_number = provider_number
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
local hangup_cause = session:getVariable("hangup_cause") or ""
local bridge_hangup_cause = session:getVariable("bridge_hangup_cause")
  or session:getVariable("originate_disposition")
  or session:getVariable("endpoint_disposition")
  or ""
local result = "Normal"
if billsec <= 0 then
  result = bridge_hangup_cause ~= "" and bridge_hangup_cause or hangup_cause
  if result == "" then result = "Rejected" end
end

local fjson = string.format(
  '{"client_id":%d,"call_uuid":"%s","sip_ip":"%s","clid":"%s","destination":"%s","client_tech_prefix":"%s","dial_destination":"%s","provider_number":"%s","billsec":%d,"sell_rate_cents":%d,"cost_rate_cents":%d,"gateway_name":"%s","route_ip":"%s","terminator_id":%d,"terminator_name":"%s","terminator_destination_name":"%s","terminator_prefix":"%s","terminator_tech_prefix":"%s","hangup_cause":"%s","bridge_hangup_cause":"%s","result":"%s"}',
  client_id,
  json_escape(uuid),
  json_escape(client_ip),
  json_escape(clid),
  json_escape(dest),
  json_escape(client_tech_prefix),
  json_escape(dial_destination),
  json_escape(provider_number),
  billsec,
  sell,
  cost,
  json_escape(used_route),
  json_escape(route_ip),
  terminator_id,
  json_escape(terminator_name),
  json_escape(terminator_destination_name),
  json_escape(terminator_prefix),
  json_escape(terminator_tech_prefix),
  json_escape(hangup_cause),
  json_escape(bridge_hangup_cause),
  json_escape(result)
)
local fcode, fbody = http_post("/api/finalize", fjson)
if fcode ~= 200 then
  freeswitch.consoleLog("error", "[billing] finalize error (" .. fcode .. "): " .. fbody .. "\n")
end
