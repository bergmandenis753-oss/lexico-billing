# Route Caller ID pools

The originator routes table has a Caller ID column. Its + button opens a
multiline number list. Saving an empty list disables the override. Numbers are
normalized to international digits and duplicates are removed. Limits: 200
unique numbers, 7-15 digits each.

Selection is round-robin per client_rates.id inside the existing SQLite reserve
transaction. A reservation keeps its selected number for retries. The original
inbound CLID stays in CDR. No charging or finalize code is changed.

Deploy the web changes together, then add only the outbound Caller ID block to
the running FreeSWITCH billing.lua. Back up that live file first: its billing
logic can differ from the repository copy. No FreeSWITCH restart is needed.
Existing routes default to an empty list; existing calls keep their script.

FreeSWITCH receives origination_caller_id_number/name and sip_invite_from_uri;
the latter overrides a gateway's fixed From user. sip_cid_type=pid generates
identity from the selected caller profile. A downstream carrier can still
screen or replace the number. Configure numbers authorized for the trunk.

Reference: https://developer.signalwire.com/freeswitch/reference/channel-variables/

Tests: install requirements-test.txt, then run
`python -m unittest discover -s tests -v`. Tests use temporary databases and
mock the FreeSWITCH session; they do not place real calls.
