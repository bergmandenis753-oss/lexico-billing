def apply(db):
    def destination_matches_route(conn, destination, prefix, destination_name):
        destination = db.normalize_phone_number(destination)
        prefix = db.normalize_phone_number(prefix)
        resolved = db.resolve_e164(conn, destination)
        if resolved is not None:
            if db.is_countrywide_prefix(conn, destination_name, prefix):
                return db.direction_matches(destination_name, resolved["country"])
            if not db.direction_matches(destination_name, resolved["country"]):
                return False
        return bool(prefix and destination.startswith(prefix))

    db.destination_matches_route = destination_matches_route
