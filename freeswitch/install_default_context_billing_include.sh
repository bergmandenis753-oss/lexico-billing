#!/usr/bin/env sh
set -eu

DEFAULT_XML="/etc/freeswitch/dialplan/default.xml"
INCLUDE_LINE='    <X-PRE-PROCESS cmd="include" data="default/00_lexico_clients.xml"/>'

if grep -Fq 'data="default/00_lexico_clients.xml"' "$DEFAULT_XML"; then
  echo "Lexico billing include already present in $DEFAULT_XML"
  exit 0
fi

cp -a "$DEFAULT_XML" "$DEFAULT_XML.bak-$(date +%Y%m%d%H%M%S)"

perl -0pi -e 's#(<context name="default">\n)#$1\n    <!-- Lexico: route carrier calls through billing before demo/default actions. -->\n    <X-PRE-PROCESS cmd="include" data="default/00_lexico_clients.xml"/>\n#' "$DEFAULT_XML"

fs_cli -x reloadxml
echo "Lexico billing include installed in $DEFAULT_XML"
