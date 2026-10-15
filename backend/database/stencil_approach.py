"""
stencil_approach.py - the STENCILS approach (Matthew's 8 x 8 bitmap idea).

For every colour except the map's most common one, the map becomes a yes/no stencil. The
stencil is cut into 8 x 8 squares and each square's 64 yes/no values are squeezed into ONE
64-bit number (pixel r, c inside a square is bit r * 8 + c). Then, as Matthew suggested,
a standard compression algorithm crunches them down: each 256 x 256-pixel page's numbers -
1,024 per colour - are zipped together into one row. Most numbers are 0 (none of that
colour) or all ones (all of it), so a page zips down to a few hundred bytes.

Nothing is lost: anything not in a stencil is the most common colour (usually land), and
the whole map can be rebuilt pixel for pixel.

Needs only plain PostgreSQL - no PostGIS. Checking a farm:
    point   -> unzip the page it is on and test one bit per colour
    outline -> unzip the pages it covers, AND the farm's own stencil with each colour's
               stencil, and count the 1s left over (= pixels where they overlap)

Commands (run from the project folder; give just the file name - TIFFs are looked up
in data/tiff/ and GeoJSONs in data/geojson/ - or a full path)
    python database/stencil_approach.py build  test_map.tif [--map NAME]
    python database/stencil_approach.py check  test_plots.geojson [--map NAME] [--rule touched|centre]
                                               [--point-mode pixel|circle] [--csv FILE]
    python database/stencil_approach.py verify test_map.tif [--map NAME]
(To upload a new map with both approaches at once, use database/maps.py upload.)

Table (in the map's own schema, next to the shared ones in common.py)
    stencil_pages   one row per page: (page_col, page_row, stencils)
                    stencils = one layer of 64-bit numbers per colour code 0-5, zipped
"""

import argparse
import math
import os
import time
import zlib

import numpy as np
import rasterio

import common

METHOD = "stencils"
TABLES = ["stencil_pages"]
OTHER_TABLES = {"class_polys": "python database/polygon_approach.py build <your map>"}
B = 8                          # squares are 8 x 8 pixels = 64 bits
PAGE = common.PAGE             # pages are 256 x 256 pixels = 32 x 32 squares
LAYERS = len(common.COLOURS)   # one stencil layer per colour code 0-5


def to_numbers(yes: np.ndarray) -> np.ndarray:
    """Yes/no pixels -> one 64-bit number per 8 x 8 square (shape: squares down x across)."""
    h, w = yes.shape
    H, W = -(-h // B) * B, -(-w // B) * B
    padded = np.zeros((H, W), bool)
    padded[:h, :w] = yes
    squares = padded.reshape(H // B, B, W // B, B).swapaxes(1, 2).reshape(H // B, W // B, B * B)
    return np.packbits(squares, axis=-1, bitorder="little").view("<u8")[..., 0]


def to_pixels(numbers: np.ndarray) -> np.ndarray:
    """64-bit numbers (squares down x across) -> yes/no pixels."""
    rows, cols = numbers.shape
    raw = np.ascontiguousarray(numbers, dtype="<u8").view(np.uint8).reshape(rows, cols, 8)
    bits = np.unpackbits(raw, axis=-1, bitorder="little").reshape(rows, cols, B, B)
    return bits.swapaxes(1, 2).reshape(rows * B, cols * B).astype(bool)


def page_size(grid: common.Grid, page_col: int, page_row: int) -> tuple[int, int]:
    """(height, width) in pixels - pages on the right and bottom edges can be smaller."""
    return (min(PAGE, grid.height - page_row * PAGE), min(PAGE, grid.width - page_col * PAGE))


def pack_page(data: np.ndarray, colours) -> np.ndarray:
    """A page of colours -> stencil layers (colour code x squares down x squares across)."""
    h, w = data.shape
    layers = np.zeros((LAYERS, -(-h // B), -(-w // B)), "<u8")
    for colour in colours:
        yes = data == colour
        if yes.any():
            layers[colour] = to_numbers(yes)
    return layers


def unzip_page(blob: bytes, height: int, width: int) -> np.ndarray:
    """A stored page -> its stencil layers, for a page of height x width pixels."""
    return np.frombuffer(zlib.decompress(blob), dtype="<u8").reshape(LAYERS, -(-height // B), -(-width // B))


def ensure_built(conn, grid: common.Grid) -> None:
    if not common.table_exists(conn, grid.table("stencil_pages")):
        raise SystemExit(f"The map '{grid.map_version}' has no stencils yet - build them first:\n"
                         f"  python database/stencil_approach.py build my_map.tif --map \"{grid.map_version}\"")


# --------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------
def build(conn, tif: str, map_name: str, report=None, tidy: bool = True) -> common.Grid:
    """Pack the TIFF into the map's stencil_pages. report(label, done, total) follows the progress.
    tidy=False leaves the transaction open and skips VACUUM (maps.upload builds both approaches
    in one transaction, then tidies up)."""
    start = time.perf_counter()
    with rasterio.open(tif) as src:
        grid, counts = common.start_build(conn, src, tif, map_name, OTHER_TABLES, report)
        stored = common.stored_colours(grid, counts)
        table = grid.table("stencil_pages")
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(f"""
                CREATE TABLE {table} (
                    page_col  INT NOT NULL,     -- which 256 x 256 page (column)
                    page_row  INT NOT NULL,     -- which 256 x 256 page (row)
                    stencils  BYTEA NOT NULL    -- per colour code 0-5: the page's 8 x 8 squares
                                                -- as 64-bit numbers - all zipped together
                )""")
            # keep big zipped pages inside the table rows instead of a side table (measured: ~6% smaller)
            cur.execute(f"ALTER TABLE {table} ALTER COLUMN stencils SET STORAGE MAIN")

        rows = []
        progress = common.Progress(common.page_count(src), "packing pages", report)
        for page in common.iter_pages(src):
            layers = pack_page(page.data, stored)
            if layers.any():  # a page that is all the most common colour needs no row at all
                rows.append((page.page_col, page.page_row, zlib.compress(layers.tobytes(), 9)))
            if len(rows) > 2_000:
                common.copy_rows(conn, table, ["page_col", "page_row", "stencils"], rows)
                rows.clear()
            progress.step()
        common.copy_rows(conn, table, ["page_col", "page_row", "stencils"], rows)

        print("  building the index...")
        if report is not None:
            report("building the index", 0, 1)
        with conn.cursor() as cur:
            # The primary key IS the index: a farm's location says exactly which pages to fetch.
            cur.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (page_row, page_col)")
        if report is not None:
            report("building the index", 1, 1)  # last report before the commit: a Cancel still undoes it all
    if tidy:
        common.tidy_up(conn, table)
        print(f"Built in {time.perf_counter() - start:.1f} s")
        common.print_storage(conn, [grid.table(t) for t in TABLES + ["grid_info"]])
    return grid


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------
def colour_at(pages: dict, grid: common.Grid, row: int, col: int) -> int:
    """The colour of one pixel: test one bit in each colour's stencil."""
    layers = pages.get((col // PAGE, row // PAGE))
    if layers is not None:
        r, c = row % PAGE, col % PAGE
        bit = (r % B) * B + (c % B)
        for colour in range(LAYERS):
            if (int(layers[colour, r // B, c // B]) >> bit) & 1:
                return colour
    return grid.default_class


def window_colours(pages: dict, grid: common.Grid, r0: int, c0: int, h: int, w: int) -> np.ndarray:
    """The colours of a block of pixels, unpacking only the 8 x 8 squares it needs."""
    out = np.full((h, w), grid.default_class, np.uint8)
    for pr in range(r0 // PAGE, (r0 + h - 1) // PAGE + 1):
        for pc in range(c0 // PAGE, (c0 + w - 1) // PAGE + 1):
            layers = pages.get((pc, pr))
            if layers is None:
                continue
            top, left = pr * PAGE, pc * PAGE  # the page's first pixel
            r_from, r_to = max(r0, top), min(r0 + h, top + PAGE, grid.height)
            c_from, c_to = max(c0, left), min(c0 + w, left + PAGE, grid.width)
            s_top, s_left = (r_from - top) // B, (c_from - left) // B
            s_bottom, s_right = (r_to - 1 - top) // B + 1, (c_to - 1 - left) // B + 1
            y0, x0 = r_from - top - s_top * B, c_from - left - s_left * B
            target = out[r_from - r0:r_to - r0, c_from - c0:c_to - c0]
            for colour in range(LAYERS):
                numbers = layers[colour, s_top:s_bottom, s_left:s_right]
                if numbers.any():
                    yes = to_pixels(numbers)
                    target[yes[y0:y0 + r_to - r_from, x0:x0 + c_to - c_from]] = colour
    return out


def check(conn, grid: common.Grid, farms: list[common.Farm], all_touched: bool = True):
    """Returns (results, timings). One query fetches every page that any farm needs."""
    ensure_built(conn, grid)
    t0 = time.perf_counter()
    results: list = [None] * len(farms)
    points, outlines, needed = {}, {}, set()
    for i, farm in enumerate(farms):
        if common.farm_outside(farm, grid):
            results[i] = common.outside_result(farm)
        elif farm.kind == "point":
            row, col = grid.point_to_pixel(farm.geom.x, farm.geom.y)
            points[i] = (row, col)
            needed.add((col // PAGE, row // PAGE))
        else:
            r0, c0, mask = common.farm_pixels(farm, grid, all_touched)
            outlines[i] = (r0, c0, mask)
            for pr in range(r0 // PAGE, (r0 + mask.shape[0] - 1) // PAGE + 1):
                for pc in range(c0 // PAGE, (c0 + mask.shape[1] - 1) // PAGE + 1):
                    needed.add((pc, pr))

    pages = {}  # (page_col, page_row) -> stencil layers
    if needed:
        page_cols, page_rows = zip(*needed)
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT s.page_col, s.page_row, s.stencils
                FROM unnest(%s::int[], %s::int[]) AS q(pc, pr)
                JOIN {grid.table("stencil_pages")} s ON s.page_col = q.pc AND s.page_row = q.pr
                """, (list(page_cols), list(page_rows)))
            for pc, pr, blob in cur.fetchall():
                pages[(pc, pr)] = unzip_page(blob, *page_size(grid, pc, pr))

    for i, (row, col) in points.items():
        results[i] = common.point_result(farms[i], colour_at(pages, grid, row, col))
    for i, (r0, c0, mask) in outlines.items():
        colours = window_colours(pages, grid, r0, c0, *mask.shape)
        counts = np.bincount(colours[mask], minlength=LAYERS)
        results[i] = common.pixel_result(farms[i], grid, counts, r0, mask.shape[0])
    return results, {"total_s": time.perf_counter() - t0}


# --------------------------------------------------------------------------------------
# verify: unpack every page back into pixels and compare with the TIFF
# --------------------------------------------------------------------------------------
def verify(conn, grid: common.Grid, tif: str) -> dict:
    ensure_built(conn, grid)
    mismatched = total = 0
    with rasterio.open(tif) as src, conn.cursor() as cur:
        fingerprint = common.Fingerprint(common.same_grid_or_exit(src, grid))
        progress = common.Progress(math.ceil(src.height / PAGE), "rebuilding the map")
        for row_off, expected in common.iter_strips(src):
            fingerprint.add(expected)
            h, w = expected.shape
            rebuilt = np.full((h, w), grid.default_class, np.uint8)
            cur.execute(f"SELECT page_col, stencils FROM {grid.table('stencil_pages')} WHERE page_row = %s",
                        (row_off // PAGE,))
            for pc, blob in cur.fetchall():
                ph, pw = page_size(grid, pc, row_off // PAGE)
                layers = unzip_page(blob, ph, pw)
                target = rebuilt[:, pc * PAGE:pc * PAGE + pw]
                for colour in range(LAYERS):
                    if layers[colour].any():
                        target[to_pixels(layers[colour])[:ph, :pw]] = colour
            mismatched += int((rebuilt != expected).sum())
            total += h * w
            progress.step()
    return {"pixels": total, "mismatched": mismatched,
            "fidelity_pct": 100.0 * (1 - mismatched / total),
            "same_map": fingerprint.hexdigest() == grid.fingerprint}


# --------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="pack the TIFF into zipped 64-bit squares and upload them")
    b.add_argument("tif", help="TIFF file name in data/tiff/ (or a full path)")
    b.add_argument("--map", help="the map's name (default: the TIFF's file name, e.g. my_map)")
    c = sub.add_parser("check", help="give a verdict for every farm in a GeoJSON file")
    c.add_argument("plots", help="GeoJSON file name in data/geojson/ (or a full path)")
    c.add_argument("--map", help="which stored map to check against (needed when there are several)")
    c.add_argument("--rule", choices=["touched", "centre"], default="touched",
                   help="touched: every pixel that overlaps an outline counts (cautious); "
                        "centre: only pixels whose centre is inside")
    c.add_argument("--point-mode", choices=["pixel", "circle"], default="pixel",
                   help="pixel: check the pixel under each point; circle: use the declared area")
    c.add_argument("--csv", default=os.path.join(common.DATABASE_DIR, "results", "results_stencils.csv"),
                   help="where to write the verdicts (default: database/results/results_stencils.csv)")
    v = sub.add_parser("verify", help="rebuild the map from the database and compare it with the TIFF")
    v.add_argument("tif", help="TIFF file name in data/tiff/ (or a full path)")
    v.add_argument("--map", help="which stored map to rebuild (needed when there are several)")
    args = ap.parse_args()
    if args.cmd == "check":
        args.plots = common.find_geojson(args.plots)
    else:
        args.tif = common.find_tiff(args.tif)

    with common.connect() as conn:
        if args.cmd == "build":
            build(conn, args.tif, args.map or common.default_map_name(args.tif))
        elif args.cmd == "check":
            grid = common.load_grid(conn, args.map)
            farms = common.load_farms(args.plots, grid, args.point_mode)
            results, timings = check(conn, grid, farms, all_touched=(args.rule == "touched"))
            common.save_results(conn, grid, farms, results, METHOD)
            common.print_summary(METHOD, results, timings)
            common.write_csv(args.csv, results)
        else:
            common.verify_report("Stencils", verify(conn, common.load_grid(conn, args.map), args.tif))


if __name__ == "__main__":
    main()
