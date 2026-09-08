# Telegram reports

Bot-only patch. No changes to the billing web service, database schema,
SIP call processing, routing, balances or finalization.

## Commands

- `/cdrshop <client id> <minutes or MM:SS>` sends a UTF-8 TXT document.
  Selecting CDR shop and entering the duration uses the same export.
- `/check <B-number>` searches the diagnostics snapshot for matching CDRs
  and outgoing SIP INVITEs. `/check` alone prompts for the next message.
- Long check results are also sent as a TXT document.

Existing administrator chat allowlisting and webhook authentication apply.
Normal balance commands and messages retain their existing transport.

## Data limits

The existing client-duration endpoint accepts at most 200 rows, strictly
longer than the requested duration, for the current UTC day. It has no
pagination. The bot requests 200 and includes every returned row. Reaching
the cap produces a warning in both caption and file, not a claim of a full
day export. This patch does not change that API.

The existing diagnostics endpoint returns a recent snapshot, not the entire
CDR/SIP history. A missing match does not establish that a call never existed.
CDR `clid` is the incoming caller ID and is never presented as outbound ID.
The route's current number pool is never used to reconstruct past calls.

Only an outgoing INVITE's From and, when present, P-Asserted-Identity and
Remote-Party-ID are reported as transmitted identity. SIP records are linked
to a CDR only by an exact call identifier and matching provider IP. Other
matching packets are presented separately without guessing the client.
An unavailable outgoing identity is explicitly marked unconfirmed.
The downstream provider may still change the display delivered to the callee.

## Verification and release

Run `python -m unittest discover -s tests -p test_telegram_reports.py -v`.
Tests cover complete multipart upload, 200-row reports, both command flows,
empty reports, errors, exact SIP matching, no incoming-ID fallback, and auth.

Deploy only Railway `TGBOT / bot2`, from a separate bot-only branch.
Do not push this change to main: the billing web service also follows main.
Rollback by selecting the previous successful bot2 deployment. Do not
restart or change the web service to release this patch.
