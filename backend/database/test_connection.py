"""
test_connection.py - check that your Neon database is reachable before uploading anything.

    python database/test_connection.py

It reads DATABASE_URL from the .env file in the project folder, prints the PostgreSQL
version, switches on PostGIS (needed by the outlines approach) and lists the tables
that are already in the database.
"""

import common


def main() -> None:
    print("Connecting to the database in DATABASE_URL...")
    with common.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version(), current_database(), current_user")
            version, database, user = cur.fetchone()
            print(f"  Connected to database '{database}' as '{user}'")
            print(f"  {version.split(',')[0]}")
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
                cur.execute("SELECT postgis_lib_version()")
                print(f"  PostGIS {cur.fetchone()[0]} is switched on (polygon_approach.py needs it)")
            except Exception as exc:  # noqa: BLE001 - show any problem in plain words
                conn.rollback()
                print(f"  PostGIS could not be switched on: {exc}")
                print("  stencil_approach.py will still work; polygon_approach.py needs PostGIS.")
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "AND tablename <> 'spatial_ref_sys' ORDER BY 1")
            tables = [row[0] for row in cur.fetchall()]
            print(f"  Tables already here: {', '.join(tables) if tables else 'none yet'}")
        conn.commit()
    print("All good - next, upload a map, e.g.:\n"
          "  python database/polygon_approach.py build my_map.tif\n"
          "  python database/stencil_approach.py build my_map.tif")


if __name__ == "__main__":
    main()
