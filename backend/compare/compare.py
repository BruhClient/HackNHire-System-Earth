"""
compare.py - the evaluation: both approaches vs the original TIFF, on the same farms.

The two questions are tested separately:
  Question 1  "Is the point in forest?"          - every point farm
  Question 2  "Does the outline overlap forest?" - every outline farm
For each one, every method reports
  * decision fidelity - % of farms whose answers match the TIFF exactly (in forest? / overlaps
                        forest?, every colour touched, the EUDR verdict)
  * query speed       - all farms in one go, per farm, and one farm checked on its own
Then, for the whole map:
  * pixel fidelity    - rebuild the whole map from the database and compare pixel by pixel
  * storage           - database size vs the TIFF

Each approach is compared with the TIFF using the SAME rule, so only the storage differs:
  stencils  <->  TIFF counted in pixels   (--rule touched|centre)
  polygons  <->  TIFF measured by exact overlap area
(For a point both rules are the same: the colour of the pixel it lands on.)

Run both 'build' commands on the same TIFF first, then (from the project folder):
    python compare/compare.py test_map.tif test_plots.geojson         # farms from a GeoJSON
    python compare/compare.py test_map.tif --point 103.6236 -1.7794  # one point: longitude latitude
    python compare/compare.py test_map.tif my_farms.geojson --point 103.5 -1.6 --point 103.6 -1.7
With several maps stored, add --map NAME (the map the TIFF was uploaded as).
Give just the file names (looked up in data/tiff/ and data/geojson/) or full paths.
For a GeoJSON, the per-farm table is written to compare/results/comparison.csv.
"""

import argparse
import csv
import os
import statistics
import sys

import numpy as np
import rasterio
from pyproj import Transformer

# The two approaches live in the database/ folder next to this one - make them importable.
COMPARE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(COMPARE_DIR), "database"))

import common  # noqa: E402
import polygon_approach  # noqa: E402
import stencil_approach  # noqa: E402

YES_NO = {True: "yes", False: "no"}


def run_batch(check, farms: list, repeat: int):
    """Check all the farms in one go, `repeat` times.
    Returns (results of the last run, median seconds, timings of the last run)."""
    runs = [check(farms) for _ in range(repeat)]
    return runs[-1][0], statistics.median(r[1]["total_s"] for r in runs), runs[-1][1]


def run_one_by_one(check, farms: list, sample: int) -> float:
    """Median seconds to check ONE farm on its own - what a single upload would wait for."""
    picks = farms[::max(1, len(farms) // sample)][:sample]
    check(picks[:1])  # warm-up
    return statistics.median(check([farm])[1]["total_s"] for farm in picks)


def share(approach: list, reference: list, test) -> str:
    same = sum(test(a) == test(r) for a, r in zip(approach, reference))
    return f"{100 * same / len(reference):9.2f}%"


def print_mismatches(name: str, approach: list, reference: list) -> None:
    bad = [(a, r) for a, r in zip(approach, reference) if a.key() != r.key()]
    for a, r in bad[:10]:
        print(f"    mismatch ({name}) {a.plot_id}: {a.verdict} [{a.colours_text()}] "
              f"vs TIFF {r.verdict} [{r.colours_text()}]")
    if len(bad) > 10:
        print(f"    ... and {len(bad) - 10} more (see the CSV)")


def map_bounds_text(grid: common.Grid) -> str:
    """The map's extent in longitude/latitude, to help when a test point falls outside it."""
    xs, ys = grid.bounds_box().exterior.xy
    if grid.srid != 4326:
        xs, ys = Transformer.from_crs(grid.srid, 4326, always_xy=True).transform(np.array(xs), np.array(ys))
    return f"longitude {min(xs):.4f} to {max(xs):.4f}, latitude {min(ys):.4f} to {max(ys):.4f}"


# --------------------------------------------------------------------------------------
# --point: test single points typed on the command line
# --------------------------------------------------------------------------------------
def test_points(points, grid, methods, repeat: int) -> None:
    print("\n=== Your points: \"is the point in forest?\" ===")
    farms = common.farms_from_points(points, grid)
    for farm in farms:
        print(f"  {farm.plot_id[len('point '):]}  (longitude, latitude)")
        reference = None
        for name, check in methods:
            check([farm])  # warm-up
            runs = [check([farm]) for _ in range(repeat)]
            result = runs[-1][0][0]
            ms = 1000 * statistics.median(r[1]["total_s"] for r in runs)
            if result.outside:
                answer = f"{'-':14s} {'outside the map':18s}"
            else:
                answer = f"{common.CLASS_NAMES[result.point_colour]:14s} in forest: {YES_NO[result.touches_forest]:4s}   "
            note = ""
            if reference is None:
                reference = result
            else:
                note = "same as the TIFF" if result.key() == reference.key() else "DIFFERENT from the TIFF"
            print(f"    {name:14s} {answer} {result.verdict:15s} {ms:8.2f} ms   {note}".rstrip())
        if reference.outside:
            print(f"    (the map covers {map_bounds_text(grid)})")


# --------------------------------------------------------------------------------------
# a GeoJSON: question 1 on its points, question 2 on its outlines
# --------------------------------------------------------------------------------------
def speed_table(rows: list, n: int, what: str) -> None:
    print(f"\n  {'Speed':22s}{f'all {n} at once':>16s}{'per ' + what:>14s}{what + ' alone':>18s}")
    for name, seconds, single in rows:
        print(f"    {name:20s}{seconds:14.3f} s{1000 * seconds / n:11.2f} ms{1000 * single:15.2f} ms")


def label_line(timings: dict, n: int, what: str) -> None:
    if timings.get("used_labels"):
        by_label = timings["by_label"]
        print(f"  (polygons answered {by_label} of {n} {what} ({100 * by_label / n:.0f}%) from the square "
              f"labels alone - no geometry needed)")
    else:
        print(f"  (polygons skip the square labels for fewer than {polygon_approach.LABELS_FROM} farms "
              f"at once - it saves a round trip)")


def question_1(farms, idx, methods, args) -> dict:
    """Point farms: the colour under the point, so: is the point in forest?"""
    group = [farms[i] for i in idx]
    n = len(group)
    print(f"\n=== Question 1: \"Is the point in forest?\" - {n} point farm{'' if n == 1 else 's'} ===")
    out, rows = {}, []
    for key, name, check in methods:
        if key == "tiff_ex":  # for a point, exact area and the pixel rule are the same thing
            continue
        results, seconds, timings = run_batch(check, group, args.repeat)
        rows.append((name, seconds, run_one_by_one(check, group, args.single)))
        out[key] = (results, timings)
    out["tiff_ex"] = out["tiff_px"]
    tiff, sten, poly = out["tiff_px"][0], out["stencils"][0], out["polygons"][0]
    in_forest = sum(r.touches_forest for r in tiff)
    outside = sum(r.outside for r in tiff)
    answers = f"  Answers from the original TIFF: {in_forest} in forest, {n - in_forest - outside} not in forest"
    print(answers + (f", {outside} outside the map" if outside else ""))
    print(f"\n  {'Same answer as the TIFF':38s}{'stencils':>10s}{'polygons':>10s}")
    for label, test in (("In forest? (yes/no)", lambda r: r.touches_forest),
                        ("Colour under the point", lambda r: r.point_colour),
                        ("EUDR verdict", lambda r: r.verdict),
                        ("Everything (decision fidelity)", lambda r: r.key())):
        print(f"    {label:36s}{share(sten, tiff, test)}{share(poly, tiff, test)}")
    print_mismatches("stencils", sten, tiff)
    print_mismatches("polygons", poly, tiff)
    speed_table(rows, n, "point")
    label_line(out["polygons"][1], n, "points")
    return out


def question_2(farms, idx, methods, args) -> dict:
    """Outline farms: every colour inside the outline, so: does it overlap forest?"""
    group = [farms[i] for i in idx]
    n = len(group)
    print(f"\n=== Question 2: \"Does the outline overlap forest?\" - {n} outline farm{'' if n == 1 else 's'} ===")
    out, rows = {}, []
    for key, name, check in methods:
        results, seconds, timings = run_batch(check, group, args.repeat)
        rows.append((name, seconds, run_one_by_one(check, group, args.single)))
        out[key] = (results, timings)
    tiff_px, tiff_ex = out["tiff_px"][0], out["tiff_ex"][0]
    sten, poly = out["stencils"][0], out["polygons"][0]
    exact = sum(r.touches_forest for r in tiff_ex)
    pixel = sum(r.touches_forest for r in tiff_px)
    print(f"  Answers from the original TIFF: {exact} of {n} overlap forest (exact area)"
          + ("" if pixel == exact else f", {pixel} with the pixel rule"))
    print(f"\n  {'Same answer as the TIFF':38s}{'stencils':>10s}{'polygons':>10s}")
    for label, test in (("Overlaps forest? (yes/no)", lambda r: r.touches_forest),
                        ("Every colour touched", lambda r: r.present),
                        ("EUDR verdict", lambda r: r.verdict),
                        ("Everything (decision fidelity)", lambda r: r.key())):
        print(f"    {label:36s}{share(sten, tiff_px, test)}{share(poly, tiff_ex, test)}")
    print_mismatches("stencils", sten, tiff_px)
    print_mismatches("polygons", poly, tiff_ex)
    rule_diff = sum(a.key() != b.key() for a, b in zip(tiff_px, tiff_ex))
    print(f"  (stencils are compared with the TIFF using the pixel rule, polygons using exact areas;\n"
          f"   those two rules themselves give different answers for {rule_diff} outline(s) - a policy\n"
          f"   choice about the rule, not a storage error)")
    speed_table(rows, n, "outline")
    label_line(out["polygons"][1], n, "outlines")
    return out


def test_geojson(conn, grid, args, methods, tif_info) -> None:
    farms = common.load_farms(args.plots, grid, args.point_mode)
    n = len(farms)
    if not n:
        raise SystemExit("No farms found in that GeoJSON.")
    point_idx = [i for i, f in enumerate(farms) if f.kind == "point"]
    outline_idx = [i for i, f in enumerate(farms) if f.kind == "outline"]
    print(f"Each method checks the farms {args.repeat} times and the median is reported; "
          f"'alone' = up to {args.single} farms per question, each checked on its own.")

    # Put each method's answers for both groups back into one list, in the file's order.
    merged = {key: [None] * n for key, _, _ in methods}
    for idx, question, skipped in (
            (point_idx, question_1, "No point farms to test, so question 1 is skipped"
             + (" (--point-mode circle turned them into outlines)." if args.point_mode == "circle" else ".")
             + " Try --point LON LAT."),
            (outline_idx, question_2, "No outline farms in this file, so question 2 is skipped.")):
        if not idx:
            print(f"\n({skipped})")
            continue
        for key, (results, _) in question(farms, idx, methods, args).items():
            for i, res in zip(idx, results):
                merged[key][i] = res
    tiff_px, tiff_ex = merged["tiff_px"], merged["tiff_ex"]
    sten, poly = merged["stencils"], merged["polygons"]

    print(f"\n=== All {n} farms: decision fidelity (every answer matches the TIFF) ===")
    print(f"    stencils {share(sten, tiff_px, lambda r: r.key()).strip()}    "
          f"polygons {share(poly, tiff_ex, lambda r: r.key()).strip()}")
    for results, method in ((tiff_px, "tiff-pixel"), (tiff_ex, "tiff-exact"),
                            (sten, stencil_approach.METHOD), (poly, polygon_approach.METHOD)):
        common.save_results(conn, grid, farms, results, method)

    if not args.skip_verify:
        print("\n=== Pixel fidelity (rebuild the whole map from the database) ===")
        for name, module in (("stencils", stencil_approach), ("polygons", polygon_approach)):
            stats = module.verify(conn, grid, args.tif)
            print(f"  {name:9s} {stats['mismatched']:,} of {stats['pixels']:,} pixels differ  "
                  f"-> {common.fidelity_text(stats)} (all colours, including land and forest)")

    print("\n=== Storage ===")
    file_bytes, raw_bytes = tif_info
    print(f"  original TIFF file (compressed)     {file_bytes / 1e6:8.3f} MB")
    print(f"  original TIFF, uncompressed pixels  {raw_bytes / 1e6:8.3f} MB")
    shared_bytes = sum(s for s, _ in common.table_sizes(conn, [grid.table("grid_info")]).values())
    for name, module in (("stencils", stencil_approach), ("polygons", polygon_approach)):
        sizes = common.table_sizes(conn, [grid.table(t) for t in module.TABLES])
        total = sum(s for s, _ in sizes.values()) + shared_bytes
        detail = ", ".join(f"{t.rsplit('.', 1)[-1]}: {r:,} rows" for t, (s, r) in sizes.items())
        print(f"  {name:9s} database                  {total / 1e6:8.3f} MB   ({detail})")
    print("  (Database sizes include indexes and PostgreSQL's per-row bookkeeping.)")

    common.make_parent(args.csv)
    with open(args.csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["plot_id", "kind",
                    "tiff_pixel_verdict", "stencils_verdict", "stencils_match",
                    "tiff_exact_verdict", "polygons_verdict", "polygons_match",
                    "point_colour_tiff", "point_colour_stencils", "point_colour_polygons",
                    "forest_tiff_pixel", "forest_stencils", "forest_tiff_exact", "forest_polygons",
                    "colours_tiff_pixel", "colours_stencils", "colours_tiff_exact", "colours_polygons",
                    "forest_ha_tiff_exact", "forest_ha_polygons", "note"])
        for tp, st, te, po in zip(tiff_px, sten, tiff_ex, poly):
            w.writerow([tp.plot_id, tp.kind,
                        tp.verdict, st.verdict, st.key() == tp.key(),
                        te.verdict, po.verdict, po.key() == te.key(),
                        *(common.CLASS_NAMES.get(r.point_colour, "") for r in (tp, st, po)),
                        *(YES_NO[r.touches_forest] for r in (tp, st, te, po)),
                        tp.colours_text(), st.colours_text(), te.colours_text(), po.colours_text(),
                        f"{te.ha(common.FOREST):.4f}", f"{po.ha(common.FOREST):.4f}", tp.note])
    print(f"\nWrote {common.nice_path(args.csv)} (one row per farm)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tif", help="TIFF file name in data/tiff/ (or a full path)")
    ap.add_argument("plots", nargs="?", help="GeoJSON file name in data/geojson/ (or a full path)")
    ap.add_argument("--map", help="which stored map to compare with (needed when there are several)")
    ap.add_argument("--point", nargs=2, type=float, action="append", metavar=("LON", "LAT"),
                    help="test one point: longitude first, then latitude (repeat for more points)")
    ap.add_argument("--rule", choices=["touched", "centre"], default="touched",
                    help="pixel rule for the stencils and the pixel baseline (default touched)")
    ap.add_argument("--point-mode", choices=["pixel", "circle"], default="pixel",
                    help="GeoJSON points: the pixel under the point, or a circle of the declared area")
    ap.add_argument("--repeat", type=int, default=3, help="timing runs per method (median is reported)")
    ap.add_argument("--single", type=int, default=20,
                    help="how many farms to also time one at a time (default 20)")
    ap.add_argument("--skip-verify", action="store_true", help="skip the pixel-by-pixel rebuild")
    ap.add_argument("--csv", default=os.path.join(COMPARE_DIR, "results", "comparison.csv"),
                    help="where to write the per-farm table (default: compare/results/comparison.csv)")
    args = ap.parse_args()
    if not args.plots and not args.point:
        ap.error("give a GeoJSON file of farms, or --point LON LAT (or both)")
    for lon, lat in args.point or []:
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            ap.error(f"--point {lon:g} {lat:g} is not a longitude/latitude pair - "
                     "give the longitude first (e.g. 103.5), then the latitude (e.g. -1.6)")
    args.repeat, args.single = max(1, args.repeat), max(1, args.single)
    args.tif = common.find_tiff(args.tif)
    if args.plots:
        args.plots = common.find_geojson(args.plots)
    all_touched = args.rule == "touched"

    with common.connect() as conn:
        conn.execute("SELECT 1")  # wake Neon up before timing anything
        grid = common.load_grid(conn, args.map)
        polygon_approach.ensure_built(conn, grid)
        stencil_approach.ensure_built(conn, grid)
        with rasterio.open(args.tif) as src:
            tif_grid = common.same_grid_or_exit(src, grid)
            tif_info = (os.path.getsize(args.tif), src.width * src.height * np.dtype(src.dtypes[0]).itemsize)
            _, fingerprint = common.scan_map(src, tif_grid)
        if fingerprint != grid.fingerprint:
            raise SystemExit(f"This TIFF is not the map stored as '{grid.map_version}' (same place, different "
                             "pixels).\nUpload it as a map of its own (python database/maps.py upload ...), "
                             "then compare with --map.")

        pixel_rule = "pixel rule" if all_touched else "centre rule"
        methods = [
            ("tiff_px", f"TIFF, {pixel_rule}", lambda fs: common.check_with_tiff(args.tif, grid, fs, "pixel", all_touched)),
            ("tiff_ex", "TIFF, exact area", lambda fs: common.check_with_tiff(args.tif, grid, fs, "exact")),
            ("stencils", "stencils", lambda fs: stencil_approach.check(conn, grid, fs, all_touched)),
            ("polygons", "polygons", lambda fs: polygon_approach.check(conn, grid, fs)),
        ]
        if args.point:
            point_methods = [("original TIFF", methods[0][2]), ("stencils", methods[2][2]), ("polygons", methods[3][2])]
            test_points(args.point, grid, point_methods, args.repeat)
            conn.rollback()  # test points are not saved
        if args.plots:
            print()
            test_geojson(conn, grid, args, methods, tif_info)


if __name__ == "__main__":
    main()
