"""
pipeline.py - the whole chain in one command: satellite layers -> compact layers -> Neon.

    python pipeline.py --name jambi --data-dir D:\\eudr\\inputs --bbox 103.0 -2.3 103.5 -1.8 --test-plots 1000 --verify

  1. Compaction (compaction/eudr_layers.py, Joe's code). Reads the input layers in --data-dir (JRC
     forest 2020, Hansen, JRC TMF, RADD, ...) for the box, and writes the compact layers into
     layers/<name>/: evidence.tif (every decision fact per pixel, 2000-2026, in 32 bits), outcome.tif,
     the test plots and, with --verify, how well both match the original layers.
  2. Upload (database/maps.py). Stores the folder in Neon as the map <name>, in one transaction:
     the outcome's 5 colours as stencils and outlines, and evidence.tif as evidence pages.
  3. Check (database/evidence_approach.py). Checks the plots (--plots, and the test plots) against
     Neon, saves the verdicts there, and compares every answer with eudr_layers.py's own answer from
     evidence.tif: the same verdict, reason, hectare figures and years, plot for plot.

Options for this script:
  --name NAME    the map's name in Neon; its folder is layers/<name> (e.g. 'Jambi v1' -> layers/jambi_v1)
  --out-dir DIR  another folder for the compact layers
  --skip-build   step 1 is done already: upload the folder that is there
  --no-upload    stop after step 1 (no database needed)
Every other option goes to eudr_layers.py: --data-dir, --bbox, --plots, --test-plots N, --verify,
--grid 30m, --protected FILE, ... (see: python compaction/eudr_layers.py --help).
Exits with code 1 if a plot gets a different answer from Neon than from evidence.tif.
"""

import argparse
import json
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
for _folder in ("compaction", "database"):
    if os.path.join(BACKEND_DIR, _folder) not in sys.path:
        sys.path.insert(0, os.path.join(BACKEND_DIR, _folder))

import common  # noqa: E402
import evidence_approach  # noqa: E402
import layers  # noqa: E402
import maps  # noqa: E402
from layers import eudr_layers  # noqa: E402


def parse(argv=None):
    """(this script's options, the options for eudr_layers.py)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="the map's name in Neon (and its folder in layers/)")
    ap.add_argument("--out-dir", help="folder for the compact layers (default: layers/<name>)")
    ap.add_argument("--skip-build", action="store_true", help="use the compact layers already in the folder")
    ap.add_argument("--no-upload", action="store_true", help="stop after building the compact layers")
    ap.add_argument("--no-outlines", action="store_true",
                    help="upload without the outlines (~24x less storage; the app's stencils-vs-outlines "
                         "comparison then can't run on this map)")
    args, rest = ap.parse_known_args(argv)
    seen = argparse.ArgumentParser(add_help=False)   # options eudr_layers.py also uses in step 3
    seen.add_argument("--plots")
    seen.add_argument("--min-plot-ha", type=float, default=0.0)
    args.plots, args.min_plot_ha = (lambda a: (a.plots, a.min_plot_ha))(seen.parse_known_args(rest)[0])
    return args, rest


def check_plots(out: str, name: str, plot_files: list[str], min_ha: float) -> bool:
    """Step 3: each plot file checked against the map in Neon (verdicts saved there), and every
    answer compared with eudr_layers.py's answer from evidence.tif. True if all are the same."""
    evidence = layers.evidence_file(out)
    rule = layers.rule_from(json.loads(layers.read_tags(evidence).get("RULE_PARAMS", "{}")))
    same_everywhere = True
    with common.connect() as conn:
        grid = common.load_grid(conn, name)
        jgrid = layers.joe_grid(grid)
        for path in plot_files:
            plots, features = evidence_approach.read_plots(common.find_geojson(path))
            rows, timings = evidence_approach.check(conn, grid, plots, min_ha)
            evidence_approach.save_results(conn, grid, features, rows)
            expected, seconds = eudr_layers.query_plots(plots, evidence, jgrid, eudr_layers.row_areas_m2(jgrid),
                                                        rule, min_ha)
            differ = [r["plot_id"] for r, e in zip(rows, expected) if eudr_layers.compare_outputs(r, e)]
            same_everywhere &= not differ
            n = len(rows)
            print(f"\n{os.path.basename(path)}: {n} plots checked against Neon in {timings['total_s']:.3f} s "
                  f"({timings['pages']} pages); evidence.tif on disk took {seconds:.3f} s")
            print(f"  Every output the same as eudr_layers.py from evidence.tif: {n - len(differ)} of {n} plots"
                  + (f" - DIFFERENT: {', '.join(differ[:10])}" if differ else ""))
            evidence_approach.print_summary(rows, timings)
    return same_everywhere


def main(argv=None) -> int:
    args, rest = parse(argv)
    name = args.name.strip()
    slug = common.map_schema(name)[len("map_"):]          # stops here if the name can't be used
    out = args.out_dir or os.path.join(common.LAYERS_DIR, slug)

    if not args.no_upload:                                 # before the long build: is the name free?
        with common.connect() as conn:
            if any(m["name"] == name for m in maps.list_maps(conn)):
                sys.exit(f"There's already a map called '{name}' in the database. Choose another --name, "
                         f"or delete that map first:\n  python database/maps.py delete \"{name}\" --yes")
    if args.skip_build:
        out = layers.results_folder(out)
    else:
        print(f"Step 1 of 3: building the compact layers into {common.nice_path(out)} (eudr_layers.py)")
        eudr_layers.main(rest + ["--out-dir", out])
    if args.no_upload:
        print(f"\nDone: the compact layers are in {common.nice_path(out)}. Upload them with:\n"
              f"  python database/maps.py upload \"{common.nice_path(out)}\" --name \"{name}\"")
        return 0

    print(f"\nStep 2 of 3: uploading {common.nice_path(out)} to Neon as '{name}'")
    with common.connect() as conn:
        maps.upload(conn, out, name, outlines=not args.no_outlines)

    test_plots = os.path.join(out, "test_plots.geojson")
    plot_files = ([args.plots] if args.plots else []) + ([test_plots] if os.path.isfile(test_plots) else [])
    if not plot_files:
        print("\nNo plots to check: add --plots FILE or --test-plots N.")
        return 0
    print("\nStep 3 of 3: checking the plots against Neon")
    same = check_plots(out, name, plot_files, args.min_plot_ha)
    print(f"\nNext: the evaluation (storage, speed, fidelity):\n"
          f"  python compare/compare.py \"{common.nice_path(out)}\" --map \"{name}\"")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
