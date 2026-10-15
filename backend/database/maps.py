"""
maps.py - several maps in one Neon database, each in its own schema (a folder of tables).

    public.maps     the list of maps: name, schema, source TIFF, fingerprint, size
    map_<name>      grid_info, stencil_pages, class_polys, square_labels, plots, plot_results

A map's schema comes from its name: 'Jambi v1' -> map_jambi_v1 (common.map_schema). Every query
names its tables with the schema (grid.table(...)), so maps never mix.

Commands (run from the project folder; TIFFs are looked up in data/tiff/, or give a full path)
    python database/maps.py list
    python database/maps.py upload my_map.tif [--name "Jambi v1"]   # both approaches at once
    python database/maps.py rename "Jambi v1" "Jambi 2024"           # its schema follows the name
    python database/maps.py delete "Jambi v1" --yes                  # can't be undone
    python database/maps.py migrate [--name jambi-v1]                # a map stored the old way

upload is ONE transaction: if anything fails, or you press Ctrl+C, nothing is left behind.
"""

import argparse
import sys
import time

import rasterio

import common
import polygon_approach
import stencil_approach

MAX_PIXELS = 100_000_000   # the app keeps the whole map in memory: 1 byte per pixel
MAP_TABLES = ["grid_info", "stencil_pages", "class_polys", "square_labels", "plots", "plot_results"]
UPLOAD_STEPS = 7           # stencils: read, pack, index; outlines: read, trace, index; tidy up


def size_problem(width: int, height: int) -> str:
    """Why a map is too big for the app ("" if it isn't)."""
    if width * height <= MAX_PIXELS:
        return ""
    return (f"This map is {width:,} x {height:,} pixels. The app keeps the whole map in memory, so it "
            f"supports up to {MAX_PIXELS // 1_000_000} million pixels (e.g. 10,000 x 10,000). "
            "Crop it into smaller areas first.")


def list_maps(conn) -> list[dict]:
    """Every stored map, by name: where it lives, its TIFF, its size and which approaches it has."""
    if not common.table_exists(conn, "public.maps"):
        if common.table_exists(conn, "public.grid_info"):
            sys.exit(common.OLD_LAYOUT_MESSAGE)
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT m.name, m.schema_name, m.source_file, m.width, m.height, m.added_at,
                   to_regclass(m.schema_name || '.stencil_pages') IS NOT NULL,
                   to_regclass(m.schema_name || '.class_polys') IS NOT NULL
                       AND to_regclass(m.schema_name || '.square_labels') IS NOT NULL
            FROM public.maps m
            WHERE to_regnamespace(m.schema_name) IS NOT NULL
            ORDER BY lower(m.name)""")
        rows = cur.fetchall()
    return [{"name": name, "schema": schema, "source_file": source, "width": width, "height": height,
             "added_at": added.isoformat() if added else None, "stencils": bool(stencils),
             "outlines": bool(outlines)}
            for name, schema, source, width, height, added, stencils, outlines in rows]


def upload(conn, tif: str, name: str, report=None) -> common.Grid:
    """A new map with BOTH approaches, in one transaction: if anything fails or is cancelled
    (report may raise), everything is rolled back and the database is as it was. Then the new
    tables are packed (VACUUM ANALYZE), which can't run inside a transaction."""
    name = (name or "").strip()
    common.map_schema(name)  # stops here if the name can't be used
    has_catalogue = common.table_exists(conn, "public.maps")
    if not has_catalogue and common.table_exists(conn, "public.grid_info"):
        sys.exit(common.OLD_LAYOUT_MESSAGE)
    if has_catalogue:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM public.maps WHERE name = %s", (name,))
            if cur.fetchone():
                sys.exit(f"There's already a map called '{name}'. Choose another name, "
                         "or delete that map first.")
    with rasterio.open(tif) as src:
        problem = size_problem(src.width, src.height)
    if problem:
        sys.exit(problem)
    start = time.perf_counter()
    try:
        stencil_approach.build(conn, tif, name, report, tidy=False)
        grid = polygon_approach.build(conn, tif, name, report, tidy=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    def tell(label, done, total):
        """Progress after the commit: the map is saved now, so a late Cancel can't undo it."""
        if report is not None:
            try:
                report(label, done, total)
            except Exception:  # noqa: BLE001 - e.g. the app's Cancelled: too late, finish tidying
                pass

    tables = [grid.table(t) for t in ("stencil_pages", "class_polys", "square_labels")]
    for i, table in enumerate(tables):
        tell("tidying up", i, len(tables))
        common.tidy_up(conn, table)
    tell("tidying up", len(tables), len(tables))
    print(f"Uploaded '{name}' (both approaches) in {time.perf_counter() - start:.1f} s")
    common.print_storage(conn, [grid.table(t) for t in MAP_TABLES])
    return grid


def describe(conn, name: str) -> dict:
    """What a map uses in the database, and how many saved verdicts would go with it."""
    map_name, schema = common.find_map(conn, name)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'""", (schema,))
        size = int(cur.fetchone()[0])
        saved = 0
        if common.table_exists(conn, f"{schema}.plot_results"):
            cur.execute(f"SELECT count(*) FROM {schema}.plot_results")
            saved = int(cur.fetchone()[0])
        cur.execute("SELECT source_file, width, height FROM public.maps WHERE name = %s", (map_name,))
        source_file, width, height = cur.fetchone()
    return {"name": map_name, "schema": schema, "bytes": size, "saved_results": saved,
            "source_file": source_file, "width": width, "height": height}


def delete(conn, name: str) -> dict:
    """Remove a map: its schema (every table in it) and its row in public.maps, in one transaction.
    Needs the map's exact name."""
    map_name, schema = common.find_map(conn, name)
    if map_name != name.strip():
        sys.exit(f"To delete a map, give its exact name: '{map_name}'.")
    if not common.SCHEMA_RE.match(schema):  # never drop anything that isn't a map's own schema
        sys.exit(f"Refusing to delete schema '{schema}': it isn't a map's schema (map_...).")
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute("DELETE FROM public.maps WHERE name = %s", (map_name,))
    conn.commit()
    return {"name": map_name, "schema": schema}


def rename(conn, name: str, new_name: str) -> dict:
    """Give a map a new name, in one transaction. Its schema follows the name (ALTER SCHEMA only
    renames: nothing is copied), and the name inside grid_info and saved results changes too."""
    old, schema = common.find_map(conn, name)
    new = (new_name or "").strip()
    new_schema = common.map_schema(new)  # stops here if the name can't be used
    if new == old:
        sys.exit(f"The map is already called '{old}'.")
    with conn.cursor() as cur:
        cur.execute("SELECT name, schema_name FROM public.maps WHERE name <> %s", (old,))
        for other, other_schema in cur.fetchall():
            if other == new:
                sys.exit(f"There's already a map called '{other}'. Choose another name.")
            if other_schema == new_schema:
                sys.exit(f"'{new}' is too close to the map '{other}': both would be stored as "
                         f"{new_schema}. Choose another name.")
        if new_schema != schema:
            cur.execute("SELECT to_regnamespace(%s) IS NOT NULL", (new_schema,))
            if cur.fetchone()[0]:
                sys.exit(f"The schema {new_schema} already exists in the database, but no map uses it. "
                         "Choose another name.")
            cur.execute(f"ALTER SCHEMA {schema} RENAME TO {new_schema}")
        cur.execute(f"UPDATE {new_schema}.grid_info SET map_version = %s", (new,))
        if common.table_exists(conn, f"{new_schema}.plot_results"):
            cur.execute(f"UPDATE {new_schema}.plot_results SET map_version = %s WHERE map_version = %s",
                        (new, old))
        cur.execute("UPDATE public.maps SET name = %s, schema_name = %s WHERE name = %s",
                    (new, new_schema, old))
    conn.commit()
    return {"old": old, "name": new, "schema": new_schema}


def migrate(conn, name: str | None = None) -> str:
    """Move a map stored the old way (its tables in the public schema) into a schema of its own,
    and list it in public.maps. One transaction; nothing is copied, so it takes about a second.
    To undo: ALTER TABLE <schema>.<table> SET SCHEMA public for each table, then delete its row."""
    if not common.table_exists(conn, "public.grid_info"):
        sys.exit("Nothing to move: no map is stored the old way (there is no public.grid_info).")
    with conn.cursor() as cur:
        cur.execute("SELECT map_version, fingerprint, source_file, width, height FROM public.grid_info")
        rows = cur.fetchall()
    if len(rows) != 1:
        sys.exit(f"public.grid_info should describe one map but has {len(rows)} rows, "
                 "so it can't be moved automatically.")
    old_name, fingerprint, source_file, width, height = rows[0]
    name = (name or old_name or "").strip()
    schema = common.map_schema(name)
    with conn.cursor() as cur:
        cur.execute(common.CATALOGUE_SQL)
        cur.execute("SELECT 1 FROM public.maps WHERE name = %s OR schema_name = %s", (name, schema))
        if cur.fetchone():
            sys.exit(f"A map called '{name}' (schema {schema}) already exists. Choose another --name.")
        cur.execute("SELECT to_regnamespace(%s) IS NOT NULL", (schema,))
        if cur.fetchone()[0]:
            sys.exit(f"The schema {schema} already exists. Choose another --name.")
        cur.execute(f"CREATE SCHEMA {schema}")
        moved = []
        for table in MAP_TABLES:
            if common.table_exists(conn, f"public.{table}"):
                cur.execute(f"ALTER TABLE public.{table} SET SCHEMA {schema}")
                moved.append(table)
        if name != old_name:
            cur.execute(f"UPDATE {schema}.grid_info SET map_version = %s", (name,))
            if "plot_results" in moved:
                cur.execute(f"UPDATE {schema}.plot_results SET map_version = %s WHERE map_version = %s",
                            (name, old_name))
        cur.execute("INSERT INTO public.maps (name, schema_name, source_file, fingerprint, width, height) "
                    "VALUES (%s,%s,%s,%s,%s,%s)", (name, schema, source_file, fingerprint, width, height))
    conn.commit()
    print(f"Moved the map '{name}' into schema {schema}: {', '.join(moved)}")
    return name


def print_list(stored: list[dict]) -> None:
    if not stored:
        print("No maps stored yet. Upload one with:\n"
              "  python database/maps.py upload my_map.tif --name NAME")
        return
    for m in stored:
        built = " + ".join(a for a, ok in (("stencils", m["stencils"]), ("outlines", m["outlines"])) if ok)
        size = f"{m['width'] or 0:,} x {m['height'] or 0:,} px"
        print(f"  {m['name']:<24} {m['schema']:<26} {size:>18}  {m['source_file'] or '':<20} "
              f"{built or 'nothing built'}")


# --------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the maps stored in the database")
    u = sub.add_parser("upload", help="upload a TIFF as a new map (builds both approaches)")
    u.add_argument("tif", help="TIFF file name in data/tiff/ (or a full path)")
    u.add_argument("--name", help="the map's name (default: the TIFF's file name, e.g. my_map)")
    r = sub.add_parser("rename", help="give a map a new name (nothing is copied)")
    r.add_argument("name", help="the map's name now (see: list)")
    r.add_argument("new_name", help="its new name")
    d = sub.add_parser("delete", help="delete a map and everything stored with it")
    d.add_argument("name", help="the map's exact name (see: list)")
    d.add_argument("--yes", action="store_true", help="really delete it - this can't be undone")
    m = sub.add_parser("migrate", help="move a map stored the old way into its own schema")
    m.add_argument("--name", help="the map's name (default: the name it was built with)")
    args = ap.parse_args()
    if args.cmd == "delete" and not args.yes:
        sys.exit(f"This deletes the map '{args.name}' and everything stored with it, for good. "
                 "Add --yes to go ahead.")
    tif = common.find_tiff(args.tif) if args.cmd == "upload" else None

    with common.connect() as conn:
        if args.cmd == "list":
            print_list(list_maps(conn))
        elif args.cmd == "upload":
            upload(conn, tif, args.name or common.default_map_name(tif))
        elif args.cmd == "rename":
            info = rename(conn, args.name, args.new_name)
            print(f"Renamed '{info['old']}' to '{info['name']}' (schema {info['schema']}).")
        elif args.cmd == "delete":
            info = delete(conn, args.name)
            print(f"Deleted the map '{info['name']}' (schema {info['schema']}).")
        else:
            migrate(conn, args.name)


if __name__ == "__main__":
    main()
