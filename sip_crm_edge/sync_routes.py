#!/usr/bin/env python3
"""Keep a dedicated FreeSWITCH SIP CRM edge configured without call-time HTTP."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape


DEFAULT_API = "https://web-production-d5e1c.up.railway.app"
DEFAULT_OUTPUT = "/etc/freeswitch/dialplan/public/00_sip_crm_routes.xml"


def _digits(value):
    return re.sub(r"\D", "", str(value or ""))


def fetch_config(api_url, node_key, api_key, timeout=5):
    url = f"{api_url.rstrip('/')}/api/freeswitch/sip-crm-config/{node_key}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def render_dialplan(payload):
    routes = payload.get("routes")
    if not isinstance(routes, list):
        raise ValueError("config has no routes list")
    extensions = []
    seen = set()
    for route in routes:
        inbound_number = _digits(route.get("inbound_number"))
        target = str(route.get("bridge_target") or "").strip()
        raw_ips = route.get("originator_ips")
        if isinstance(raw_ips, str):
            raw_ips = re.split(r"[\s,;]+", raw_ips)
        if not isinstance(raw_ips, list) or not raw_ips:
            raise ValueError(f"route {inbound_number!r} has no originator IPs")
        originator_ips = []
        for raw_ip in raw_ips:
            try:
                address = str(ipaddress.ip_address(str(raw_ip).strip()))
            except ValueError as exc:
                raise ValueError(f"invalid originator IP: {raw_ip!r}") from exc
            if address not in originator_ips:
                originator_ips.append(address)
        if len(inbound_number) < 5 or inbound_number in seen:
            raise ValueError(f"invalid or duplicate inbound number: {inbound_number!r}")
        if not re.fullmatch(r"sofia/external/sips?:[^\s]+", target, flags=re.IGNORECASE):
            raise ValueError(f"invalid bridge target for number {inbound_number}")
        seen.add(inbound_number)
        source_expression = "^(?:" + "|".join(re.escape(ip) for ip in originator_ips) + ")$"
        extensions.append(
            f'''  <extension name="sip-buffer-{inbound_number}" continue="false">
    <condition field="network_addr" expression="{source_expression}"/>
    <condition field="destination_number" expression="^\\+?{inbound_number}$">
      <action application="set" data="hangup_after_bridge=true"/>
      <action application="set" data="continue_on_fail=false"/>
      <action application="bridge" data="{escape(target, {'"': '&quot;'})}"/>
    </condition>
  </extension>'''
        )
    body = "\n".join(extensions)
    return f'''<!-- Generated standalone SIP buffer config. Do not edit. -->
<include>
{body}
</include>
'''


def atomic_update(output_path, content, reload_command=("fs_cli", "-x", "reloadxml")):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    if output.exists() and output.read_bytes() == encoded:
        return False
    with tempfile.NamedTemporaryFile("wb", dir=output.parent, delete=False) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.chmod(0o640)
    os.replace(temporary, output)
    subprocess.run(reload_command, check=True, timeout=10)
    return True


def sync_once(api_url, node_key, api_key, output_path):
    payload = fetch_config(api_url, node_key, api_key)
    content = render_dialplan(payload)
    changed = atomic_update(output_path, content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    print(f"sip-crm sync ok: node={node_key} routes={len(payload['routes'])} changed={changed} sha={digest}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=float(os.getenv("SIP_CRM_SYNC_INTERVAL", "10")))
    args = parser.parse_args()
    api_url = os.getenv("BILLING_API_URL", DEFAULT_API)
    node_key = os.environ["SIP_CRM_NODE_KEY"]
    output_path = os.getenv("SIP_CRM_DIALPLAN_PATH", DEFAULT_OUTPUT)
    key_file = Path(os.getenv("BILLING_API_KEY_FILE", "/etc/freeswitch/billing_api_key"))
    api_key = key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise SystemExit("empty billing API key")
    while True:
        try:
            sync_once(api_url, node_key, api_key, output_path)
        except Exception as exc:
            print(f"sip-crm sync failed; keeping last local config: {exc}", flush=True)
            if not args.watch:
                raise
        if not args.watch:
            return
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    main()
