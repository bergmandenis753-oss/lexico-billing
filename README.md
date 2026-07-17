# Lexico VoIP billing

FastAPI prepaid billing dashboard for VoIP routing.

## Required environment variables

Set these variables in Railway before exposing the service:

```env
ADMIN_USER=your-admin-login
ADMIN_PASSWORD=use-a-long-random-password
API_SECRET_KEY=use-a-different-long-random-secret
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
