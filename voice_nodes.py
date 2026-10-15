PRIMARY_NODE = "primary"

VOICE_NODES = (
    {
        "id": PRIMARY_NODE,
        "name": "Основной",
        "public_ip": "207.154.192.34",
        "private_ip": "10.114.0.2",
    },
    {
        "id": "secondary",
        "name": "Новый",
        "public_ip": "207.154.219.145",
        "private_ip": "10.114.0.3",
    },
)

_NODE_IDS = {node["id"] for node in VOICE_NODES}


def normalize(value):
    node_id = str(value or PRIMARY_NODE).strip().lower()
    return node_id if node_id in _NODE_IDS else PRIMARY_NODE


def validate(value):
    node_id = str(value or "").strip().lower()
    if node_id not in _NODE_IDS:
        raise ValueError("Неизвестный голосовой сервер")
    return node_id


def list_nodes():
    return [dict(node) for node in VOICE_NODES]


def init_schema(conn):
    migrations = (
        ("client_rates", "voice_node", "TEXT NOT NULL DEFAULT 'primary'"),
        ("sip_hits", "voice_node", "TEXT NOT NULL DEFAULT 'primary'"),
        ("cdr", "voice_node", "TEXT NOT NULL DEFAULT 'primary'"),
    )
    for table, column, definition in migrations:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if table_exists is None:
            continue
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} IS NULL OR trim({column}) = ''",
            (PRIMARY_NODE,),
        )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_crates_voice_node "
        "ON client_rates(client_id, voice_node, client_tech_prefix, prefix)"
    )
