# CLAUDE.md

Student prototype for a SystemEarth challenge: store an EUDR map for rubber farms in Jambi,
Indonesia in a Neon PostgreSQL database, compactly and with nothing lost, then check farm plots
(GeoJSON points and polygons) against it. Two approaches compete against the original TIFF on
storage, query speed, decision fidelity and pixel fidelity. The brief also asks for an EUDR layer
analysis, an encoding strategy and, as a bonus, a cost estimate. SystemEarth's CTO said a local
demo is fine and to containerise it (`Dockerfile`); the stencil idea is his. `README.md` is the
user guide and holds the published results.

## The map and the rules
Each TIFF pixel holds one value:

| Value | Colour | What it means | Effect on a farm that touches it |
|---|---|---|---|
| 0 | land | Not forest: farmland, grass, bare ground, buildings. Rubber plantations count as farmland under the EUDR, not forest | `PASS` |
| 1 | water | Rivers, lakes, sea. A farm on water probably has wrong coordinates | `CHECK_LOCATION` if the point, or more than half the outline, is water |
| 2 | forest | Forest still standing. EUDR definition: over 0.5 ha, trees over 5 m, canopy cover over 10%, not mainly farmland or town | `PASS`, reported separately (`forest_ha`) |
| 3 | deforestation | Forest turned into farmland after the EUDR cut-off date, 31 Dec 2020 | `FAIL` |
| 4 | needs review | Pixels the map flags as uncertain, so a person has to look | `MANUAL_REVIEW` |
| anything else (e.g. 255) | no data | No information for that pixel; stored as class 5 | `CANNOT_CHECK` |

- The worst colour a farm touches decides its verdict (`common.decide`): `FAIL`, then
  `MANUAL_REVIEW`, then `CANNOT_CHECK` (also for farms outside the map), then `CHECK_LOCATION`,
  then `PASS`.
- Forest alone never fails a farm: under the EUDR only deforestation after the cut-off does.
- The two key queries: "is the point in forest?" and "does this outline overlap forest?".
- A point takes the colour of the pixel it lands on.
- Exact-area rule (outlines): a colour counts if its overlap is more than
  max(1e-4 m², 1e-6 × farm area).
- Pixel rule (stencils): `touched` = the pixel overlaps the farm by some area, not just along an
  edge; `centre` = the pixel's centre is inside. The stencils and the TIFF pixel baseline both use
  `common.farm_pixels`, so they always look at the same pixels.
- Decision fidelity compares `PlotResult.key()` = (verdict, colours touched, point colour).
  Stencils are compared with the TIFF using the pixel rule, outlines using exact areas.

## Layout
- `database/common.py`: shared rules, `Grid`, farm loading, results and the TIFF baseline
  (`check_with_tiff`). Imported by the others, never run.
- `database/polygon_approach.py`: outlines in PostGIS (`build` / `check` / `verify`).
- `database/stencil_approach.py`: stencils, 8 × 8 bitmaps packed into 64-bit numbers and zipped
  per 256 × 256 page (same commands).
- `database/maps.py`: several maps, one schema each (`list` / `upload` both approaches / `rename` / `delete` /
  `migrate`). `public.maps` is the catalogue.
- `compare/compare.py`: the evaluation. It imports the database modules through `sys.path`.
- `data/tiff/`, `data/geojson/`: inputs. Scripts take a bare file name and look there.
  `data/make_test_data.py` makes the synthetic test map and farms.

## Commands
Run everything from the project root (the folder with `README.md`). Commands must also work in
Windows PowerShell: use `py` if `python` isn't found, put file names with spaces in quotes, and
don't rely on shell wildcards. To open PowerShell there: open the folder in File Explorer, click
the address bar, type `powershell` and press Enter.

### First-time setup
```
python -m venv .venv                  # optional, once
.venv\Scripts\activate                # every new terminal; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                # then paste the Neon connection string into .env (macOS/Linux: cp)
python database/test_connection.py    # should print "All good"
```
If PowerShell refuses to run the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### The user's own map and farms
The user's files are in `data/tiff/` and `data/geojson/` (everything except the test files). List
those folders to get the real names, and put them in place of `my_map.tif` and `my_plots.geojson`.
Run the steps in order:
```
# 0. Which maps are stored (the user's real map is jambi-v1, in schema map_jambi_v1)
python database/maps.py list
# 1. Upload a map: both approaches, one transaction (already done for jambi-v1)
python database/maps.py upload my_map.tif --name jambi-v1
# 2. Check the farms: verdicts go to Neon (the map's plot_results) and to database/results/
python database/polygon_approach.py check my_plots.geojson --map jambi-v1
python database/stencil_approach.py check my_plots.geojson --map jambi-v1
# 3. Prove nothing was lost: should say "0 differ". Downloads the whole map, so it's slow.
python database/polygon_approach.py verify my_map.tif --map jambi-v1
python database/stencil_approach.py verify my_map.tif --map jambi-v1
# 4. The evaluation numbers: writes compare/results/comparison.csv
python compare/compare.py my_map.tif my_plots.geojson --skip-verify --map jambi-v1
```
`--map` can be left out while only one map is stored.
- After step 1, read the printed map size and colour mix. A big "no data" share means the TIFF
  uses values other than 0–4. Crop a map much bigger than about 10,000 × 10,000 pixels first.
- `--skip-verify` skips the pixel rebuild that step 3 already did. Leave it out to include pixel
  fidelity in the `compare` output.

### The test data (to check a change)
Test data goes into a map of its own (`--name test`), so it never touches the user's maps:
```
python database/maps.py upload test_map.tif --name test
python database/polygon_approach.py check test_plots.geojson --map test
python database/stencil_approach.py check test_plots.geojson --map test
python database/polygon_approach.py verify test_map.tif --map test
python database/stencil_approach.py verify test_map.tif --map test
python compare/compare.py test_map.tif test_plots.geojson --map test
python compare/compare.py test_map.tif --point 103.6236 -1.7794 --point 103.3709 -1.6326 --map test   # forest, then land
python database/maps.py delete test --yes                # clean up afterwards
```
IMPORTANT: the user's Neon database holds their real map, `jambi-v1` (schema `map_jambi_v1`).
- `build --map X` replaces map X's tables, and `maps.py delete X` drops map X's schema for good.
- Never build, upload over or delete `jambi-v1` (or another map you didn't create) without asking.
- Run `python database/maps.py list` before writing anything.
- Test maps get their own name and are deleted afterwards.
`DATABASE_URL` is read from `.env` in the project root, unless it is already set in the
environment (that wins).

### Other commands and options
| What | Command or option |
|---|---|
| Test single points (longitude first; repeat for more) | `python compare/compare.py my_map.tif --point 103.6236 -1.7794` |
| Crop a big map by pixels | `python data/crop_tiff.py full_map.tif my_map.tif --size 5000` |
| Crop starting at a pixel | `python data/crop_tiff.py full_map.tif my_map.tif --col 12000 --row 8000 --size 5000` |
| Crop by coordinates (west, south, east, north) | `python data/crop_tiff.py full_map.tif my_map.tif --bounds 103.2 -1.9 103.7 -1.4` |
| Treat points as circles of their declared area | `--point-mode circle` on `check` or `compare` |
| Count only pixels whose centre is inside (stencils) | `--rule centre` on the stencil `check` or `compare` |
| List / upload / rename / delete maps | `python database/maps.py list`, `upload TIF --name NAME`, `rename NAME NEW_NAME`, `delete NAME --yes` |
| Which map (when several are stored) | `--map NAME` on `build`, `check`, `verify`, `compare` |
| Move a one-map database into the new layout | `python database/maps.py migrate [--name NAME]` |
| Where to write the CSV | `--csv FILE` on `check` or `compare` |
| Timing runs per method / farms timed one at a time | `--repeat N` / `--single N` on `compare` |
| Make new synthetic test data | `python data/make_test_data.py --help` |
| Every option of a script | add `--help`, e.g. `python compare/compare.py --help` |
| Lint (`pip install pyflakes` first) | `python -m pyflakes database compare data` |
| Docker | `docker build -t eudr-storage .`, then `docker run --rm -v "$(pwd):/project" eudr-storage compare/compare.py my_map.tif my_plots.geojson` (PowerShell: `${PWD}`) |

The example points are on the made-up test map. On the user's map, use points inside it;
`compare` prints the map's extent when a point falls outside.

## Checking a change
Unit tests for the backend's map handling (names, schemas, `find_map`, `start_build`, `maps.py`,
qualified tables) live in the frontend's suite (`../frontend/tests/test_map_names.py`,
`test_approach_tables.py`, `test_maps.py`), using a fake connection. A change is done when:
1. pyflakes is clean, and the frontend's unit tests pass.
2. On the test data, `verify` reports 0 differing pixels for both approaches, and every
   decision-fidelity row in `compare` is 100.00%.
3. For changes to geometry, rules or speed, the same holds for a map in metres (the pixels of
   `test_map.tif` saved with EPSG:32748 and 30 m pixels), for `--rule centre --point-mode circle`,
   and for a map where forest is the most common colour (swap values 0 and 2).

Show the commands and their output as evidence.

## Design invariants
- **One schema per map.**
  - `common.map_schema(name)` gives the schema: `map_` + the lowercase name, with other characters becoming `_`.
  - `public.maps` is the catalogue (name, schema, source file, fingerprint, size).
  - Every query names its tables with `grid.table("...")`, never through `search_path`. A `SET LOCAL` would cost a round trip per transaction, and it doesn't reach `VACUUM`.
  - `load_grid(conn, name)` and `find_map` choose the map.
- **Uploads are atomic.** `maps.upload` builds both approaches in one transaction with `tidy=False`, then runs `VACUUM`. A failure or Ctrl+C leaves nothing behind.
- **PostGIS stays in `public`** (`CREATE EXTENSION ... SCHEMA public`), so dropping a map's schema can never remove it.
- **A map's schema always follows its name.** `maps.rename` renames the schema (`ALTER SCHEMA ... RENAME`) and updates `grid_info.map_version`, `plot_results.map_version` and `public.maps`, in one transaction.
- **`maps.delete` is guarded.** It needs the exact name, and drops only a schema that is listed in `public.maps` and matches `^map_[a-z0-9_]+$`.
- Lossless: only the most common colour (`grid_info.default_class`) is left out, and every other
  colour is stored. Never hard-code land as the default.
- `grid_info.fingerprint` identifies the exact map. `compare` refuses a different TIFF; `verify`
  warns.
- Outlines are traced in 64 × 64 tiles (`TRACE_TILE`, measured fastest). Square labels (16 × 16
  pixels, 255 = mixed, zipped per page) are used only for checks of at least `LABELS_FROM` (200)
  farms, and must never change an answer.
- Farm areas come from pyproj (`common.area_m2`); the default colour's area is whatever remains.
- Stencils: bit `r*8+c` of each square, little-endian `packbits`, 6 layers per page, zlib level 9.
  Pages that are all the default colour get no row. `STORAGE MAIN` and `VACUUM ANALYZE` after
  upload.

## Gotchas
- Neon's pooled connection is PgBouncer in transaction mode: keep `prepare_threshold=None`, and
  use `SET LOCAL`, never plain `SET`.
- PostGIS functions declare high costs, so PostgreSQL's JIT compiler can spend ~150 ms on a big
  overlap query that then runs in ~65 ms. `common.no_jit` runs before big queries (50+ outlines or
  800+ points). Profile with `EXPLAIN (ANALYZE)` and costs on: `COSTS OFF` hides the JIT lines.
- PostGIS geometry arrays use `:` as their delimiter, so `%s::geometry[]` with a Python list
  fails. Send binary EWKB as `bytea[]` with `ST_GeomFromEWKB`, as `polygon_approach.check` does.
- Every query is a network round trip to Neon (several milliseconds). Answer all farms in a few
  batched queries (`unnest` arrays), never one query per farm.
- Timings vary by ±20%. Interleave runs, report medians, and say whether numbers come from a
  local database or Neon. Don't publish a speed claim measured any other way.

## What the frontend relies on
`../frontend` (the PyQt app; see `../frontend/CLAUDE.md`) imports these modules through
`frontend/backend.py`. If you rename or change any of the following, update the frontend in the
same change, then run its tests (`frontend\.venv\Scripts\python -m unittest discover -s
frontend/tests -t frontend`), `frontend\smoke_neon.py --map jambi-v1` and, for anything about
maps, `frontend\smoke_maps.py` (writes only a throwaway map).
- `maps`: `list_maps`, `upload(conn, tif, name, report)`, `describe`, `rename(conn, name, new_name)` (returns `{old, name, schema}`), `delete`, `UPLOAD_STEPS`, `MAX_PIXELS`, `size_problem`, `MAP_TABLES`.
- `common`:
  - `connect`, `load_grid(conn, name)`, `find_map`, `table_exists`, `scan_map`, `save_results`, `check_with_tiff`, `area_m2`
  - `map_schema`, `MAP_NAME_MAX`, `default_map_name`, `normalise`, `Progress(total, label, report)`
  - `farms_from_features`, `load_farms`, `farm_outside`, `point_result`, `exact_result`
  - `TIFF_DIR`, `PAGE`, `COLOURS`, `VERDICTS`, the class constants
  - `Grid`: its fields (`x0`, `y0`, `pixel_w`, `pixel_h`, `width`, `height`, `srid`, `default_class`, `fingerprint`, `map_version`, `schema`), plus `table()`, `point_to_pixel`, `pixel_centre`, `inside`, `bounds_box`, `from_dataset` and `==` (which ignores `schema`)
  - `Farm` (`plot_id`, `kind` = `point` / `outline`, `geom`)
  - `PlotResult` (`verdict`, `present`, `point_colour`, `ha()`, `water_share`, `outside`, `note`, `key()`)
- Both approaches:
  - `check(conn, grid, farms[, all_touched])` returns `(results in farm order, {"total_s": ...})`.
  - It must accept any connection-like object, because the frontend passes a `MeteredConnection` proxy to count queries.
  - It must only use `conn.cursor()` (as a context manager), `execute`, `fetchall` and `fetchone`.
  - It must also work when given only points or only outlines.
- Both approaches: `ensure_built(conn, grid)`, `build(conn, tif, map_name, report=None, tidy=True)` (returns the `Grid`), `verify(conn, grid, tif)`.
- `stencil_approach`: `page_size`, `unzip_page`, `to_pixels`, `pack_page`, `LAYERS`. The frontend rebuilds the whole map from `SELECT page_col, page_row, stencils FROM {grid.table('stencil_pages')}`, and pages with no row are `default_class`.
- `grid_info.source_file` names the uploaded TIFF. The frontend uses it to find the reference TIFF in `data/tiff/`.

## Style
- Plain English for a student audience, British/Singapore spelling (colour, metres). Error
  messages say how to fix the problem (`SystemExit` with the next step).
- Keep the code Python 3.10 compatible (no 3.12-only f-string syntax). Don't add dependencies
  beyond `requirements.txt`.
- When behaviour or numbers change, update `README.md` (steps, options, results, honest notes)
  and the docstring at the top of the script.

## Privacy
- Never commit or print `.env` (it holds the Neon password), or copy it into the Docker image.
- Real TIFFs and GeoJSONs are customer data and stay out of Git. Only `test_map.tif` and
  `test_plots.geojson` are tracked.
