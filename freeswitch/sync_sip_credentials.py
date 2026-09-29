#!/usr/bin/env python3
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape


API_URL = os.getenv(
    "LEXICO_SIP_CREDENTIALS_URL",
    "https://web-production-d5e1c.up.railway.app/api/freeswitch/sip-credentials",
)
API_KEY_FILE = Path(os.getenv("LEXICO_API_KEY_FILE", "/etc/freeswitch/billing_api_key"))
DIRECTORY = Path(os.getenv("LEXICO_SIP_DIRECTORY", "/etc/freeswitch/directory/default"))
FILE_PREFIX = "lexico-sip-"
SAFE_LOGIN = re.compile(r"^[A-Za-z0-9_.-]{4,64}$")
SAFE_PASSWORD = re.compile(r"^[A-Za-z0-9]{16,64}$")


def fetch_credentials():
    key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    request = urllib.request.Request(API_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return payload.get("credentials") or []


def render(item):
    credential_id = int(item["id"])
    client_id = int(item["client_id"])
    login = str(item["sip_login"])
    password = str(item["sip_password"])
    if not SAFE_LOGIN.fullmatch(login) or not SAFE_PASSWORD.fullmatch(password):
        raise ValueError(f"unsafe SIP credential {credential_id}")
    client_name = escape(str(item.get("client_name") or ""), {'"': "&quot;"})
    login_xml = escape(login, {'"': "&quot;"})
    password_xml = escape(password, {'"': "&quot;"})
    return f"""<include>
  <user id="{login_xml}">
    <params>
      <param name="password" value="{password_xml}"/>
    </params>
    <variables>
      <variable name="user_context" value="default"/>
      <variable name="effective_caller_id_name" value="{client_name}"/>
      <variable name="effective_caller_id_number" value="{login_xml}"/>
      <variable name="accountcode" value="client:{client_id}:sip:{credential_id}"/>
      <variable name="lexico_client_id" value="{client_id}"/>
      <variable name="lexico_sip_login" value="{login_xml}"/>
    </variables>
  </user>
</include>
"""


def sync():
    credentials = fetch_credentials()
    desired = {f"{FILE_PREFIX}{int(item['id'])}.xml": render(item) for item in credentials}
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    current = {path.name: path.read_text(encoding="utf-8") for path in DIRECTORY.glob(f"{FILE_PREFIX}*.xml")}
    if current == desired:
        print(f"SIP credentials unchanged: {len(desired)} active")
        return False

    with tempfile.TemporaryDirectory(dir=DIRECTORY) as temp_name:
        temp = Path(temp_name)
        for name, content in desired.items():
            (temp / name).write_text(content, encoding="utf-8")
        for path in DIRECTORY.glob(f"{FILE_PREFIX}*.xml"):
            path.unlink()
        for path in temp.iterdir():
            path.replace(DIRECTORY / path.name)

    subprocess.run(["fs_cli", "-x", "reloadxml"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["fs_cli", "-x", "sofia profile lexico-users flush_inbound_reg"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Applied {len(desired)} active SIP credentials")
    return True


if __name__ == "__main__":
    sync()
