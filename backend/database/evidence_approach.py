"""
evidence_approach.py - the EVIDENCE PAGES: every EUDR decision fact, for every pixel, in the database.

The compaction step (compaction/eudr_layers.py, Joe's code) packs everything the EUDR decision needs
to know about a pixel into one 32-bit number: forest on 31 Dec 2020, forest type, the year each
source (JRC TMF, Hansen, RADD radar, optical alerts) records a clearing, rubber, water, ... (its
evidence.tif). The stencils and outlines store the rule's answer in 5 colours; these pages store the
evidence itself, so a check gives each plot everything eudr_layers.py gives it: the verdict and its
reason, the hectares of 2020 forest and of each kind of clearing, the years, and the figures the
due diligence statement asks for.

Storage: the map is cut into 256 x 256-pixel pages, as for the stencils. A page holds few distinct
evidence values (a median of 24 on a map made from Jambi's real outcome), so each page is stored as
its palette (the distinct values, 4 bytes each) and one index per pixel (1 byte, or 2 if a page has
more than 256 values), zipped together. Pages that are all the map's most common value get no row.
Nothing is lost: verify rebuilds evidence.tif bit for bit.

Checking plots: one query fetches every page any plot needs. Then each plot gets exactly what
eudr_layers.py does with evidence.tif (its plot_window, plot_mask and judge): the rule runs on the
facts under the plot. So the answers are eudr_layers.py's, plot for plot (tests/test_evidence.py).

Commands (from the backend folder; a results folder is given by its path or its name in layers/)
    python database/evidence_approach.py build  layers/jambi [--map NAME]
    python database/evidence_approach.py check  layers/jambi/test_plots.geojson [--map NAME]
                                                [--min-plot-ha HA] [--csv FILE]
    python database/evidence_approach.py verify layers/jambi [--map NAME]
(maps.py upload stores a results folder with all three approaches at once.)

Tables (in the map's own schema)
    evidence_info     one row: the bit layout, the rule's settings, the most common value, provenance
    evidence_pages    one row per page: (page_col, page_row, evidence)
    evidence_results  one row per plot checked: verdict, reason and every figure (facts, as JSON)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import time
import zlib
from collections import Counter

import numpy as np
import rasterio
from psycopg.types.json import Jsonb
from rasterio.windows import Window

import common
import layers
from layers import eudr_layers

METHOD = "evidence"
TABLES = ["evidence_pages", "evidence_info"]
PAGE = common.PAGE
FIRST_COLUMNS = ["plot_id", "verdict", "reason", "area_ha", "forest_2020_ha", "confirmed_ha", "probable_ha",
                 "review_ha", "first_deforestation_year"]


# --------------------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------------------
def pack_page(values: np.ndarray) -> bytes:
    """A page of evidence values -> zip(count of distinct values | the values | one index per pixel)."""
    palette, index = np.unique(values, return_inverse=True)
    index = index.reshape(values.shape).astype(np.uint8 if len(palette) <= 256 else "<u2")
    return zlib.compress(np.array([len(palette)], "<u4").tobytes() + palette.astype("<u4").tobytes()
                         + index.tobytes(), 9)


def unpack_page(blob: bytes, height: int, width: int) -> np.ndarray:
    """A stored page -> its evidence values (height x width, uint32)."""
    raw = zlib.decompress(blob)
    count = int.from_bytes(raw[:4], "little")
    palette = np.frombuffer(raw, "<u4", count=count, offset=4)
    index = np.frombuffer(raw, np.uint8 if count <= 256 else "<u2", offset=4 + 4 * count)
    return palette[index.reshape(height, width)].astype(np.uint32)


def page_size(grid: common.Grid, page_col: int, page_row: int) -> tuple[int, int]:
    """(height, width) in pixels - pages on the right and bottom edges can be smaller."""
    return min(PAGE, grid.height - page_row * PAGE), min(PAGE, grid.width - page_col * PAGE)


def window(pages: dict, default: int, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
    """Rows r0:r1, columns c0:c1 of the map from unpacked pages {(page_col, page_row): values};
    a page with no row is all the default value."""
    out = np.full((r1 - r0, c1 - c0), default, np.uint32)
    for pr in range(r0 // PAGE, (r1 - 1) // PAGE + 1):
        for pc in range(c0 // PAGE, (c1 - 1) // PAGE + 1):
            page = pages.get((pc, pr))
            if page is None:
                continue
            top, left = pr * PAGE, pc * PAGE
            rows = slice(max(r0, top), min(r1, top + page.shape[0]))
            cols = slice(max(c0, left), min(c1, left + page.shape[1]))
            out[rows.start - r0:rows.stop - r0, cols.start - c0:cols.stop - c0] = \
                page[rows.start - top:rows.stop - top, cols.start - left:cols.stop - left]
    return out


def ensure_built(conn, grid: common.Grid) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                    (grid.table("evidence_info"), grid.table("evidence_pages")))
        built = cur.fetchone()[0]
    if not built:
        raise SystemExit(
            f"The map '{grid.map_version}' has no evidence pages: it was uploaded from a TIFF, not from "
            "a results folder of the compaction step. Upload the results folder as a map:\n"
            "  python database/maps.py upload layers/NAME --name NAME\n"
            "or build the map from your satellite layers:\n"
            "  python pipeline.py --name NAME --data-dir YOUR_LAYERS --bbox XMIN YMIN XMAX YMAX")


def load_info(conn, grid: common.Grid) -> dict:
    """The evidence's settings: most common value, the rule (from its parameters) and fingerprint."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT default_value, rule_params, bit_layout, fingerprint FROM {grid.table('evidence_info')}")
        row = cur.fetchone()
    if row is None:
        raise SystemExit(f"The map '{grid.map_version}' has evidence pages but no evidence_info. Build them "
                         f"again: python database/evidence_approach.py build layers/NAME --map \"{grid.map_version}\"")
    default, params, layout, fingerprint = row
    layers.check_layout(layout)
    return {"default_value": int(default), "rule_params": params, "rule": layers.rule_from(params),
            "fingerprint": fingerprint}


# --------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------
def build(conn, results: str, map_name: str, report=None, tidy: bool = True) -> common.Grid:
    """Store a results folder's evidence.tif as the evidence pages of the map map_name, which must
    hold the same results folder's 5 colours (maps.upload does both in one transaction).
    report(label, done, total) follows the progress. tidy=False leaves the transaction open and
    skips VACUUM."""
    start = time.perf_counter()
    grid = common.load_grid(conn, map_name)
    folder = layers.results_folder(results)
    path = layers.evidence_file(folder)
    with rasterio.open(path) as src:
        common.same_grid_or_exit(src, grid)
    with rasterio.open(layers.class_map(folder)) as src:
        colours = common.scan_map(src, grid)[1]
    if colours != grid.fingerprint:
        raise SystemExit(f"This results folder is not the one the map '{grid.map_version}' was uploaded "
                         "from (its 5 colours differ). Upload it as a map of its own:\n"
                         f"  python database/maps.py upload \"{common.nice_path(folder)}\" --name NAME")

    pages, counts, fingerprint = [], Counter(), common.Fingerprint(grid)
    with rasterio.open(path) as src:
        tags = src.tags()
        layout = json.loads(tags.get("BIT_LAYOUT", "[]"))
        layers.check_layout(layout)
        params = json.loads(tags.get("RULE_PARAMS", "{}"))
        progress = common.Progress(math.ceil(src.height / PAGE), "packing the evidence", report)
        for row_off in range(0, src.height, PAGE):
            strip = src.read(1, window=Window(0, row_off, src.width, min(PAGE, src.height - row_off)))
            fingerprint.add(strip)
            values, n = np.unique(strip, return_counts=True)
            counts.update(dict(zip(values.tolist(), n.tolist())))
            for col_off in range(0, src.width, PAGE):
                page = strip[:, col_off:col_off + PAGE]
                first = int(page[0, 0])
                if (page == first).all():       # one value: packed later, unless it is the default
                    pages.append((col_off // PAGE, row_off // PAGE, first, page.shape))
                else:
                    pages.append((col_off // PAGE, row_off // PAGE, None, pack_page(page)))
            progress.step()
    top = max(counts.values())
    default = min(v for v, n in counts.items() if n == top)
    rows = [(pc, pr, blob) if value is None else (pc, pr, pack_page(np.full(blob, value, np.uint32)))
            for pc, pr, value, blob in pages if value != default]

    info, table = grid.table("evidence_info"), grid.table("evidence_pages")
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}, {info}")
        cur.execute(f"""
            CREATE TABLE {info} (
                producer       TEXT,            -- the eudr_layers.py version that built evidence.tif
                bit_layout     JSONB NOT NULL,  -- where each fact sits in the 32 bits: [{{field, bits, meaning}}]
                rule_params    JSONB NOT NULL,  -- the rule's settings (hansen_only, settled_until, ...)
                default_value  BIGINT NOT NULL, -- the map's most common value: pages all of it have no row
                fingerprint    TEXT NOT NULL,   -- identifies the exact evidence.tif
                source_file    TEXT,            -- the evidence.tif (its path inside layers/)
                tags           JSONB            -- everything evidence.tif says about itself (rule, sources, ...)
            )""")
        cur.execute(f"""
            CREATE TABLE {table} (
                page_col  INT NOT NULL,      -- which 256 x 256 page (column)
                page_row  INT NOT NULL,      -- which 256 x 256 page (row)
                evidence  BYTEA NOT NULL     -- the page's distinct values + one index per pixel, zipped
            )""")
        cur.execute(f"ALTER TABLE {table} ALTER COLUMN evidence SET STORAGE MAIN")
        cur.execute(f"INSERT INTO {info} (producer, bit_layout, rule_params, default_value, fingerprint, "
                    "source_file, tags) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (tags.get("PRODUCER"), Jsonb(layout), Jsonb(params), default, fingerprint.hexdigest(),
                     common.stored_path(path), Jsonb(tags)))
    for i in range(0, len(rows), 500):
        common.copy_rows(conn, table, ["page_col", "page_row", "evidence"], rows[i:i + 500])

    print("  building the evidence index...")
    if report is not None:
        report("building the evidence index", 0, 1)
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (page_row, page_col)")
    if report is not None:
        report("building the evidence index", 1, 1)  # last report before the commit: Cancel undoes it all
    if tidy:
        common.tidy_up(conn, table)
        print(f"Built in {time.perf_counter() - start:.1f} s")
        common.print_storage(conn, [grid.table(t) for t in TABLES])
    return grid


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------
def read_plots(path: str):
    """(plots, features): the plots as eudr_layers.py reads them (WGS84; a point becomes a circle of
    its declared area, 4 ha if none is given, as the EU system assumes), with the same ids the
    stencils and outlines give them (common.plot_ids); and the GeoJSON features themselves."""
    features = common.read_features(path)
    ids = common.plot_ids(features)
    slim = {"type": "FeatureCollection", "features": [  # only what eudr_layers.read_plots reads
        {"type": "Feature", "properties": {"Area": common.declared_area(f.get("properties") or {})},
         "geometry": f.get("geometry")} for f in features]}
    plots = eudr_layers.read_plots(io.BytesIO(json.dumps(slim).encode("utf-8")))
    plots["plot_id"] = ids
    return plots, features


def check(conn, grid: common.Grid, plots, min_ha: float = 0.0):
    """Every plot's facts, verdict and reason, as eudr_layers.py gives them from evidence.tif.
    plots: a GeoDataFrame with plot_id (see read_plots). Returns (rows in plot order, timings).
    Round trips: one to see the tables are there, one for the rule, one for every page needed."""
    ensure_built(conn, grid)
    t0 = time.perf_counter()
    info = load_info(conn, grid)
    jgrid = layers.joe_grid(grid)
    row_area = eudr_layers.row_areas_m2(jgrid)
    items = list(zip(plots["plot_id"], plots.geometry))
    windows, needed = [], set()
    for _, geom in items:
        if geom is None or geom.is_empty:
            windows.append(None)
            continue
        r0, r1, c0, c1 = eudr_layers.plot_window(geom, jgrid)
        windows.append((r0, r1, c0, c1))
        if r1 > r0 and c1 > c0:
            needed |= {(pc, pr) for pr in range(r0 // PAGE, (r1 - 1) // PAGE + 1)
                       for pc in range(c0 // PAGE, (c1 - 1) // PAGE + 1)}
    pages = {}
    if needed:
        page_cols, page_rows = zip(*sorted(needed))
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT s.page_col, s.page_row, s.evidence
                FROM unnest(%s::int[], %s::int[]) AS q(pc, pr)
                JOIN {grid.table("evidence_pages")} s ON s.page_col = q.pc AND s.page_row = q.pr
                """, (list(page_cols), list(page_rows)))
            for pc, pr, blob in cur.fetchall():
                pages[(pc, pr)] = unpack_page(blob, *page_size(grid, pc, pr))

    rows = []
    for (pid, geom), span in zip(items, windows):
        note = ""
        if span is None or span[1] <= span[0] or span[3] <= span[2]:  # outside the map (as eudr_layers)
            facts, verdict, reason = eudr_layers.judge(
                eudr_layers.unpack(np.zeros((1, 1), eudr_layers.PACKED_DTYPE)), np.zeros((1, 1), bool),
                row_area[:1], info["rule"], min_ha)
            note = "no geometry" if span is None else ""
        else:
            r0, r1, c0, c1 = span
            mask, note = eudr_layers.plot_mask(geom, eudr_layers.window_grid(jgrid, r0, r1, c0, c1))
            values = window(pages, info["default_value"], r0, r1, c0, c1)
            facts, verdict, reason = eudr_layers.judge(eudr_layers.unpack(values), mask, row_area[r0:r1],
                                                       info["rule"], min_ha)
        rows.append({"plot_id": pid, **facts, "verdict": verdict, "reason": reason, "note": note})
    return rows, {"total_s": time.perf_counter() - t0, "pages": len(pages)}


def save_results(conn, grid: common.Grid, features: list[dict], rows: list[dict]) -> None:
    """The plots (as sent) and their verdicts, reasons and figures, in the map's own schema."""
    results = grid.table("evidence_results")
    with conn.cursor() as cur:
        cur.execute(common.map_tables_sql(grid))
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {results} (
                plot_id     TEXT PRIMARY KEY REFERENCES {grid.table('plots')}(plot_id) ON DELETE CASCADE,
                verdict     TEXT NOT NULL,   -- non-compliant, probable deforestation, needs review,
                                             -- deforestation-free or not assessed (eudr_layers.py)
                reason      TEXT,
                facts       JSONB NOT NULL,  -- every figure: hectares by kind of clearing, the years, ...
                checked_at  TIMESTAMPTZ DEFAULT now()
            )""")
        cur.executemany(
            f"INSERT INTO {grid.table('plots')} (plot_id, supplier_id, declared_ha, geojson) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (plot_id) DO UPDATE SET supplier_id = EXCLUDED.supplier_id, "
            "declared_ha = EXCLUDED.declared_ha, geojson = EXCLUDED.geojson",
            [(r["plot_id"], (f.get("properties") or {}).get("supplier_id"),
              common.declared_area(f.get("properties") or {}), Jsonb(f)) for f, r in zip(features, rows)])
        cur.executemany(
            f"INSERT INTO {results} (plot_id, verdict, reason, facts) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (plot_id) DO UPDATE SET verdict = EXCLUDED.verdict, reason = EXCLUDED.reason, "
            "facts = EXCLUDED.facts, checked_at = now()",
            [(r["plot_id"], r["verdict"], r["reason"],
              Jsonb({k: v for k, v in r.items() if k not in ("plot_id", "verdict", "reason")})) for r in rows])
    conn.commit()


def write_csv(path: str, rows: list[dict]) -> None:
    common.make_parent(path)
    columns = FIRST_COLUMNS + [c for c in rows[0] if c not in FIRST_COLUMNS] if rows else FIRST_COLUMNS
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, columns)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {common.nice_path(path)}")


def print_summary(rows: list[dict], timings: dict) -> None:
    n = len(rows)
    total = timings["total_s"]
    print(f"\n{METHOD}: checked {n} plots in {total:.3f} s ({1000 * total / max(n, 1):.2f} ms per plot), "
          f"{timings['pages']} pages fetched")
    for verdict, count in Counter(r["verdict"] for r in rows).most_common():
        print(f"  {verdict:24s} {count}")


# --------------------------------------------------------------------------------------
# verify: rebuild the whole evidence from the pages and compare it with evidence.tif
# --------------------------------------------------------------------------------------
def verify(conn, grid: common.Grid, results: str) -> dict:
    ensure_built(conn, grid)
    info = load_info(conn, grid)
    path = layers.evidence_file(layers.results_folder(results))
    mismatched = total = 0
    with rasterio.open(path) as src, conn.cursor() as cur:
        fingerprint = common.Fingerprint(common.same_grid_or_exit(src, grid))
        progress = common.Progress(math.ceil(src.height / PAGE), "rebuilding the evidence")
        for row_off in range(0, src.height, PAGE):
            h = min(PAGE, src.height - row_off)
            expected = src.read(1, window=Window(0, row_off, src.width, h))
            fingerprint.add(expected)
            rebuilt = np.full((h, src.width), info["default_value"], np.uint32)
            cur.execute(f"SELECT page_col, evidence FROM {grid.table('evidence_pages')} WHERE page_row = %s",
                        (row_off // PAGE,))
            for pc, blob in cur.fetchall():
                ph, pw = page_size(grid, pc, row_off // PAGE)
                rebuilt[:, pc * PAGE:pc * PAGE + pw] = unpack_page(blob, ph, pw)
            mismatched += int(np.count_nonzero(rebuilt != expected))
            total += h * src.width
            progress.step()
    return {"pixels": total, "mismatched": mismatched, "fidelity_pct": 100.0 * (1 - mismatched / total),
            "same_map": fingerprint.hexdigest() == info["fingerprint"]}


# --------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="store a results folder's evidence.tif as evidence pages")
    b.add_argument("results", help="the results folder (its path, or its name in layers/)")
    b.add_argument("--map", help="the map it belongs to (needed when there are several)")
    c = sub.add_parser("check", help="every plot's facts, verdict and reason, from the database")
    c.add_argument("plots", help="GeoJSON of plots (a path, or a path inside layers/)")
    c.add_argument("--map", help="which stored map to check against (needed when there are several)")
    c.add_argument("--min-plot-ha", type=float, default=0.0,
                   help="ignore clearing of at most this many ha per plot (the EUDR has no tolerance; default 0)")
    c.add_argument("--csv", default=os.path.join(common.DATABASE_DIR, "results", "results_evidence.csv"),
                   help="where to write the results (default: database/results/results_evidence.csv)")
    v = sub.add_parser("verify", help="rebuild the evidence from the database and compare it with evidence.tif")
    v.add_argument("results", help="the results folder (its path, or its name in layers/)")
    v.add_argument("--map", help="which stored map to rebuild (needed when there are several)")
    args = ap.parse_args()
    if args.cmd == "check":
        args.plots = common.find_geojson(args.plots)

    with common.connect() as conn:
        if args.cmd == "build":
            build(conn, args.results, common.find_map(conn, args.map)[0])
        elif args.cmd == "check":
            grid = common.load_grid(conn, args.map)
            plots, features = read_plots(args.plots)
            rows, timings = check(conn, grid, plots, args.min_plot_ha)
            save_results(conn, grid, features, rows)
            print_summary(rows, timings)
            write_csv(args.csv, rows)
        else:
            stats = verify(conn, common.load_grid(conn, args.map), args.results)
            print(f"Evidence: rebuilt {stats['pixels']:,} pixels from the database - {stats['mismatched']:,} "
                  f"differ from evidence.tif (all 32 bits) -> pixel fidelity {common.fidelity_text(stats)}")
            if not stats["same_map"]:
                print("  Warning: this evidence.tif is not the one that was uploaded.")


if __name__ == "__main__":
    main()
