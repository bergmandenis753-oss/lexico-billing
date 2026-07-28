import hashlib


def _route_pick(candidates, seed):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    digest = hashlib.sha256(str(seed or "").encode("utf-8")).hexdigest()
    return candidates[int(digest, 16) % len(candidates)]


def install(db):
    def match_client_rate_for_destination(conn, client_id, destination, seed=""):
        destination_norm = db.normalize_phone_number(destination)
        rows = conn.execute(
            "SELECT * FROM client_rates WHERE client_id = ? "
            "ORDER BY length(COALESCE(client_tech_prefix, '')) DESC, length(prefix) DESC, id",
            (client_id,),
        ).fetchall()

        best_key = None
        candidates = []
        for row in rows:
            client_tech_prefix = (row["client_tech_prefix"] or "") if "client_tech_prefix" in row.keys() else ""
            dial_destination = destination_norm
            if client_tech_prefix:
                if not destination_norm.startswith(client_tech_prefix):
                    continue
                dial_destination = destination_norm[len(client_tech_prefix):]
                if not dial_destination:
                    continue

            if not db.destination_matches_route(conn, dial_destination, row["prefix"], row["destination_name"]):
                continue

            key = (len(client_tech_prefix), len(row["prefix"] or ""))
            if best_key is None:
                best_key = key
            elif key != best_key:
                break
            candidates.append((row, dial_destination, client_tech_prefix))

        return _route_pick(candidates, f"{client_id}:{destination_norm}:{seed}")

    db.match_client_rate_for_destination = match_client_rate_for_destination
