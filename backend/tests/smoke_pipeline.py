"""smoke_pipeline.py - the whole chain against Neon, on made-up layers.

WRITES to Neon, but only to a throwaway map ('zz pipeline smoke'), which it deletes again even if a
step fails. Your own maps are only listed (before and after, to show they are untouched).

    venv\\Scripts\\python tests\\smoke_pipeline.py [--keep] [--size 600]        (from backend\\)

1. Makes made-up input layers in a temporary folder (compaction/make_test_layers.py).
2. Runs pipeline.py on them: the compaction step (with test plots and --verify) into the temporary
   folder, the upload to Neon (all three approaches, one transaction), and the check of the test
   plots against Neon - every output must be eudr_layers.py's own from evidence.tif.
3. Rebuilds the whole map from each approach: the evidence pages vs evidence.tif (all 32 bits),
   the stencils and outlines vs the 5-colour map. 0 pixels may differ.
4. Runs compare.py on it (both parts and the storage chain); it must finish without an error.
5. Deletes the map (skipped with --keep), and checks that your other maps are untouched.
Exits with code 1 if anything doesn't match.
"""

import argparse
import os
import subprocess
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(BACKEND_DIR, "compaction"), os.path.join(BACKEND_DIR, "database"), BACKEND_DIR]

import common  # noqa: E402
import evidence_approach  # noqa: E402
import make_test_layers  # noqa: E402
import maps  # noqa: E402
import pipeline  # noqa: E402
import polygon_approach  # noqa: E402
import stencil_approach  # noqa: E402
import layers  # noqa: E402

NAME = "zz pipeline smoke"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="leave the test map in Neon")
    ap.add_argument("--size", type=int, default=600, help="pixels across the made-up map (default 600)")
    args = ap.parse_args()
    problems = []

    def expect(ok, what):
        print(f"  {'OK ' if ok else 'BAD'} {what}")
        if not ok:
            problems.append(what)

    def stored():
        with common.connect() as conn:
            return {m["name"]: m for m in maps.list_maps(conn, with_evidence=True)}

    before = stored()
    if NAME in before:                                  # left over from a run that was stopped
        with common.connect() as conn:
            maps.delete(conn, NAME)
        before.pop(NAME)
    print("Your maps:", ", ".join(before) or "none")

    with tempfile.TemporaryDirectory() as tmp:
        inputs, out = os.path.join(tmp, "inputs"), os.path.join(tmp, "layers", "smoke")
        try:
            print(f"\n1. Making made-up input layers ({args.size} x {args.size} pixels)...")
            bbox = make_test_layers.make_layers(inputs, size=args.size)

            print("\n2. pipeline.py: compaction -> upload -> check against Neon")
            code = pipeline.main(["--name", NAME, "--out-dir", out, "--data-dir", inputs,
                                  "--bbox", *map(str, bbox), "--test-plots", "300", "--verify", "--no-baseline"])
            expect(code == 0, "every test plot gets every output from Neon that eudr_layers.py gives it "
                              "from evidence.tif")
            summary = layers.summary(out) or {}
            fid = summary.get("fidelity") or {}
            expect(fid.get("decision_fidelity_pct") == 100.0,
                   f"evidence.tif vs the original layers: {fid.get('decision_fidelity_pct')}% of "
                   f"{fid.get('plots_compared')} plots identical (eudr_layers.py --verify)")
            listed = stored().get(NAME, {})
            expect(all(listed.get(k) for k in ("stencils", "outlines", "evidence")),
                   "listed, with stencils, outlines and evidence pages")

            print("\n3. Rebuilding the map from each approach...")
            with common.connect() as conn:
                grid = common.load_grid(conn, NAME)
                colours = layers.class_map(out)
                stats = evidence_approach.verify(conn, grid, out)
                expect(stats["mismatched"] == 0 and stats["same_map"],
                       f"evidence pages: {stats['mismatched']:,} of {stats['pixels']:,} pixels differ (all 32 bits)")
                for label, module in (("stencils", stencil_approach), ("outlines", polygon_approach)):
                    stats = module.verify(conn, grid, colours)
                    expect(stats["mismatched"] == 0,
                           f"{label}: {stats['mismatched']:,} of {stats['pixels']:,} pixels differ (5 colours)")

            print("\n4. compare.py...")
            run = subprocess.run([sys.executable, os.path.join(BACKEND_DIR, "compare", "compare.py"), out,
                                  "--map", NAME, "--repeat", "1", "--single", "3", "--skip-verify",
                                  "--csv", os.path.join(tmp, "comparison.csv"),
                                  "--csv-evidence", os.path.join(tmp, "comparison_evidence.csv")],
                                 capture_output=True, text=True, cwd=BACKEND_DIR)
            print("\n".join("    " + line for line in run.stdout.splitlines()
                            if "===" in line or "Neon" in line or "evidence.tif" in line or "original" in line))
            expect(run.returncode == 0, "compare.py finished" + (f": {run.stderr[-500:]}" if run.returncode else ""))
            expect("plots identical (100.00%)" in run.stdout, "compare.py: Neon vs evidence.tif identical on every plot")
        finally:
            if args.keep:
                print(f"\n5. Keeping '{NAME}' (--keep). Delete it with: python database/maps.py delete \"{NAME}\" --yes")
            else:
                print(f"\n5. Deleting '{NAME}'...")
                with common.connect() as conn:
                    if NAME in {m["name"] for m in maps.list_maps(conn)}:
                        maps.delete(conn, NAME)
                expect(NAME not in stored(), "deleted")

    after = {k: v for k, v in stored().items() if k != NAME}
    expect(after == before, "your other maps are untouched")
    if problems:
        print(f"\n{len(problems)} problem(s).")
        return 1
    print("\nEverything matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
