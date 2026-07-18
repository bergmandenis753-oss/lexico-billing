# Lexico VoIP billing

FastAPI prepaid billing dashboard for VoIP routing.

## Required environment variables

Set these variables in Railway before exposing the service:

```env
ADMIN_USER=your-admin-login
ADMIN_PASSWORD=use-a-long-random-password
API_SECRET_KEY=use-a-different-long-random-secret
OPENAI_API_KEY=sk-proj-... # optional, enables the dashboard AI call analyst
OPENAI_MODEL=gpt-4.1-mini # optional override
```

The app fails closed when these variables are missing:

- `/` and dashboard CRUD endpoints require HTTP Basic auth with `ADMIN_USER` / `ADMIN_PASSWORD`.
- `/api/reserve` and `/api/finalize` require the API secret.
- `/docs`, `/redoc`, and `/openapi.json` are disabled.

## Service API authentication

For FreeSWITCH or another trusted integration, send either header:

```http
Authorization: Bearer <API_SECRET_KEY>
```

or:

```http
X-API-Key: <API_SECRET_KEY>
```

`/healthz` is intentionally public so Railway can check that the app is alive.

## Read-only call analyst

The dashboard has a read-only AI assistant for recent SIP/CDR diagnostics.
It can explain the latest call attempts, but it cannot edit clients, routes,
balances, whitelist, or FreeSWITCH.

For full SIP ladder analysis, run `freeswitch/pcap_collector.py` on the
FreeSWITCH server. It watches SIP packets on UDP 5060/5080, parses compact
headers, and posts them to `/api/pcap-events` with `API_SECRET_KEY`.

## Persistent database on Railway

SQLite data is lost on Railway when it is stored inside the app container.
Create a Railway Volume mounted at `/data`.

Railway automatically exposes `RAILWAY_VOLUME_MOUNT_PATH`, and the app stores
the SQLite database at:

```env
${RAILWAY_VOLUME_MOUNT_PATH}/billing.db
```

You may also set `BILLING_DB_PATH=/data/billing.db` explicitly if preferred.

After this, deploys can replace the code without deleting billing data.

## Terminator routing modes

Terminators can route calls in two ways:

- Set `gateway_name` to an existing FreeSWITCH gateway name, for example `lexico`.
- Leave `gateway_name` empty and set one or more terminator IPs. FreeSWITCH will bridge directly to the selected IP.

When multiple IPs are entered, separate them with commas or semicolons.

Originator IP fields also accept CIDR networks, for example:

```text
204.44.67.152, 91.202.0.53, 204.44.67.0/24
```

## FreeSWITCH default context

Authenticated SIP users can enter the FreeSWITCH `default` context before the
public carrier dialplan. Keep `freeswitch/dialplan/default/00_lexico_clients.xml`
included near the top of `/etc/freeswitch/dialplan/default.xml`, immediately
after `<context name="default">`, before the stock demo/global extensions.

That keeps carrier calls inside the billing flow first, so calls are reserved,
bridged, finalized, and written to CDR before any demo dialplan actions can run.
