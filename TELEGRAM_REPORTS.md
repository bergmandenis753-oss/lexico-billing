# Telegram reports

Bot-only patch. No changes to the billing web service, database schema,
SIP call processing, routing, balances or finalization.

## Commands

- `/cdrshop <client id> <minutes or MM:SS>` sends a UTF-8 TXT document.
  Selecting CDR shop and entering the duration uses the same export.
- `/check <A-or-B-number> [YYYY-MM-DD]` searches the independent SIP archive.
  The default date is today in UTC; `/check` alone prompts for a number.
- Check results are sent as TXT documents with incoming A, actual outgoing
  From/PAI/RPID, B including technical prefix, start time, conversation seconds,
  provider replies and hangup cause. Unknown fields stay explicitly unknown.

Existing administrator chat allowlisting and webhook authentication apply.
Normal balance commands and messages retain their existing transport.

## Data limits

The existing client-duration endpoint accepts at most 200 rows, strictly
longer than the requested duration, for the current UTC day. It has no
pagination. The bot requests 200 and includes every returned row. Reaching
the cap produces a warning in both caption and file, not a claim of a full
day export. This patch does not change that API.

The archive starts collecting on installation, not retroactively. It retains
seven days of SIP/UDP IPv4 traffic on ports 5060 and 5080, including fragmented
UDP INVITEs. It does not capture audio, SIP/TLS, TCP or IPv6. Collection gaps
and its start time are included in reports. Searching B also matches technical
prefixes; searching A matches the exact normalized incoming or outgoing From.

Only an outgoing INVITE's From and, when present, P-Asserted-Identity and
Remote-Party-ID are reported as transmitted identity. SIP records are linked
to call metadata by explicit FreeSWITCH channel UUIDs and SIP Call-IDs. Other
matching packets are presented separately without guessing the client.
An unavailable outgoing identity is explicitly marked unconfirmed.
The downstream provider may still change the display delivered to the callee.

## Verification and release

Run `python -m unittest discover -s tests -p 'test_*reports.py' -v` and
`python -m unittest discover -s tests -p test_sip_archive.py -v`.
Tests cover complete multipart upload, 200-row reports, both command flows,
empty reports, errors, exact SIP matching, no incoming-ID fallback, and auth.

Deploy only Railway `TGBOT / bot2`, from a separate bot-only branch.
Do not push this change to main: the billing web service also follows main.
Rollback by selecting the previous successful bot2 deployment. Do not
restart or change the web service to release this patch.

## Independent collector

Install `sip_archive_store.py` and `freeswitch/telegram_sip_archive.py` into
`/opt/lexico-telegram-archive/`, and install the new
`freeswitch/lexico-telegram-archive.service`. Only this new service is started.
The existing billing collector and FreeSWITCH are not modified or restarted.

The collector subscribes to CHANNEL_CREATE, CHANNEL_BRIDGE and
CHANNEL_HANGUP_COMPLETE on the existing localhost event socket. It only sends
`auth` and `event`, never `api`, `bgapi`, `sendmsg`, or call-control commands.
SQLite data is private to `/var/lib/lexico-telegram-archive/`. The service has
a 20% CPU quota, 192 MiB memory limit, seven-day retention and a 2 GB disk safety
stop. This database is unrelated to the billing database.

The server polls bot2 over verified HTTPS for read-only archive queries; it
opens no new inbound ports. Authentication uses a purpose-specific HMAC derived
from the already shared billing key. The derived token is not a billing API key.
Only allowlisted Telegram chats can enqueue requests. Results go only to the
requesting chat. Outstanding requests expire after 150 seconds; in-flight
requests may require retry after a bot deployment. The bot must run one replica.

The normal report has no 50/200-row cap. Exceptional queries exceeding 20000
SIP events are explicitly marked incomplete. Current IP directory names are
used for labels, never to guess A-leg/B-leg relationships or transmitted A.

Collector rollback: stop and disable only `lexico-telegram-archive.service`.
Preserve its SQLite archive. Bot rollback: previous successful bot2 deployment.
