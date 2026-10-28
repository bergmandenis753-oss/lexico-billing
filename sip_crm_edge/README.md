# Dedicated SIP CRM edge

This package is installed only on the third, isolated FreeSWITCH server. It
does not modify either existing Lexico voice server.

The sync process downloads the active routes for one `SIP_CRM_NODE_KEY`,
validates the complete response, writes a local XML dialplan atomically and
then runs `reloadxml`. FreeSWITCH uses that local file during calls. There is
no Billing API or SQLite request in the call path. If Billing is unavailable,
the last valid local dialplan remains in service.

## Server setup

1. Install FreeSWITCH with `mod_sofia` and `mod_dptools` on a separate VPS.
   The hardened minimal module list, local-only event socket and safe empty
   dialplan shipped in `freeswitch/` can be copied over the vanilla defaults
   before the service is started.
2. Copy `sync_routes.py` to `/usr/local/bin/sip-crm-sync` and make it executable.
3. Copy `sip-crm-sync.service` to `/etc/systemd/system/`.
4. Create `/etc/lexico/sip-crm-edge.env`:

```env
BILLING_API_URL=https://web-production-d5e1c.up.railway.app
SIP_CRM_NODE_KEY=salesrender-edge-1
BILLING_API_KEY_FILE=/etc/freeswitch/billing_api_key
SIP_CRM_SYNC_INTERVAL=10
```

5. Put the existing Billing API key in `/etc/freeswitch/billing_api_key` with
   mode `0600`.
6. Allow SIP only from the DID provider IPs at the VPS firewall. Open the RTP
   range only for the provider and SalesRender networks when those addresses
   are available.
7. Enable the service:

```sh
systemctl daemon-reload
systemctl enable --now sip-crm-sync.service
```

Create the same node key in the Billing **SIP CRM** tab, assign a DID to it and
enable the route only after a test INVITE reaches the new VPS. Point the DID
provider to this third server only after the local XML has been verified with
`fs_cli -x "show dialplan"` and a controlled test call.
