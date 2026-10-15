# EUDR map storage: outlines vs stencils

Two ways to store a 5-colour EUDR map (land, water, forest, deforestation, needs review)
in a **Neon** PostgreSQL database, and check farms against it:

- **Outlines** (`database/polygon_approach.py`) – your team's vector idea. Uses PostGIS, which is switched on automatically.
- **Stencils** (`database/stencil_approach.py`) – Matthew's 8 × 8 bitmap idea. Uses plain PostgreSQL.

**Nothing is lost.** Both store every colour, including land and forest, and `verify` proves
it by rebuilding the whole map from the database and matching your TIFF pixel for pixel.

For every farm, both answer:
- **Is the point in forest? Does the outline overlap forest?** And by how many hectares.
- **Every colour the farm touches**, with hectares of each.
- **The EUDR verdict:** `FAIL`, `MANUAL_REVIEW`, `CANNOT_CHECK`, `CHECK_LOCATION` or `PASS`.

---

## Folder structure

```
eudr_storage/                    <- the project folder: run every command from here
├── .env                         <- YOU CREATE THIS: your Neon connection string
├── .env.example                 <- copy this to make .env
├── README.md
├── CLAUDE.md                    <- notes for Claude Code (read automatically)
├── requirements.txt
├── Dockerfile
│
├── data/
│   ├── tiff/                    <- PUT YOUR TIFF FILES HERE
│   │   └── test_map.tif             (made-up Jambi map, included)
│   ├── geojson/                 <- PUT YOUR FARM GEOJSON FILES HERE
│   │   └── test_plots.geojson       (303 made-up farms, included)
│   ├── crop_tiff.py             <- optional: cut a smaller piece out of a big TIFF
│   └── make_test_data.py        <- optional: makes the test map and farms
│
├── database/                    <- everything that talks to Neon
│   ├── test_connection.py       <- step 1: check Neon is reachable
│   ├── maps.py                  <- several maps: list / upload (both approaches) / rename / delete / migrate
│   ├── polygon_approach.py      <- outlines: build / check / verify
│   ├── stencil_approach.py      <- stencils: build / check / verify
│   ├── common.py                <- shared rules (used by the others, don't run it)
│   └── results/                 <- verdict CSVs (created automatically)
│
└── compare/                     <- the evaluation
    ├── compare.py               <- both approaches vs the original TIFF
    └── results/                 <- comparison.csv (created automatically)
```

**Give just file names.** The scripts look in `data/tiff/` for TIFFs and in `data/geojson/`
for GeoJSONs, so `my_map.tif` is enough. Full paths work too.

---

## How each approach stores the map

**Shared trick: leave out the most common colour.** The build counts every colour first. The
most common one (usually land) is never stored: *anything not in the database is that colour*.
This saves the most space and loses nothing.

| | Outlines | Stencils |
|---|---|---|
| Idea | Trace each patch of colour into a shape that follows the pixel edges | Per colour, pack each 8 × 8 square of yes/no pixels into one 64-bit number |
| Efficiency trick | Traced in small 64 × 64-pixel tiles, so shapes have few corners and overlap checks stay fast. Plus **square labels**: each 16 × 16-pixel square is marked "all one colour (which)" or "mixed" | A page's numbers are mostly 0 or all-ones, so each 256 × 256 page is zipped into one row (Matthew's "then compress it") |
| Tables | `class_polys`: colour + shape, with a spatial index. `square_labels`: one zipped row of labels per page | `stencil_pages`: one zipped row per page |
| Checking a point | Which shape holds the point? | Test one bit per colour |
| Checking an outline | Exact overlap area with each nearby shape | AND the farm's stencil with each colour's stencil, count the 1s |
| Shortcut | In a batch of 200+ farms, a farm sitting entirely in one-colour squares is answered from the labels, with no geometry | – |
| Hectares measured as | Exact area | Whole pixels (bigger for small farms) |

After uploading, the stencil table is packed tightly (`VACUUM`) and keeps its zipped pages
inside the table rows (`STORAGE MAIN`). Together these made it 20% smaller in tests: 1.20 MB
down to 0.96 MB on the large map.

---

## Step 0: One-time setup

1. Install **Python 3.10 or newer**.
2. Unzip the project, open a terminal and go into the folder: `cd path/to/eudr_storage`
3. Recommended: create a virtual environment so the packages stay separate.
   - Windows: `python -m venv .venv`, then `.venv\Scripts\activate`
   - macOS / Linux: `python3 -m venv .venv`, then `source .venv/bin/activate`
4. Install the packages: `pip install -r requirements.txt`

## Step 1: Connect to Neon (`.env`)

1. Go to [console.neon.tech](https://console.neon.tech) and create a project.
   Choose the **AWS Asia Pacific (Singapore)** region: it's closest to you, so uploads and queries are faster.
2. On the project dashboard, click **Connect** and copy the connection string.
3. Create `.env` in the **project folder** (next to `README.md`, not inside `database/`):
   - Windows: `copy .env.example .env`, then `notepad .env`
   - macOS / Linux: `cp .env.example .env`, then open it in any editor
4. Replace the example line with your string, on **one line** and **without quotes**:
   ```
   DATABASE_URL=postgresql://neondb_owner:abc123@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
5. Test it:
   ```
   python database/test_connection.py
   ```
   You should see `All good`.

## Step 2: Put your files in `data/`

- Your **TIFF** goes in `data/tiff/`, e.g. `data/tiff/my_map.tif`. It needs:
  - one band of whole numbers: **0 land, 1 water, 2 forest, 3 deforestation, 4 needs review**
    (any other value counts as "no data", and farms on it get `CANNOT_CHECK`)
  - a coordinate system: degrees (EPSG:4326) or metres (e.g. UTM 48S, EPSG:32748)
  - "no data" marked with a value outside 0–4, such as 255, because 0 means land
- Your **farms** go in `data/geojson/`, e.g. `data/geojson/my_plots.geojson`.
  They can be `Point` and `Polygon` features in normal longitude/latitude. The `plot_id` and
  `area_ha` properties are used if present.
- **Big map? Crop it first.** Neon's free plan has limited storage:
  ```
  python data/crop_tiff.py full_map.tif my_map.tif --size 5000
  ```

## Step 3: Upload the map to Neon (`maps.py upload`)

```
python database/maps.py upload my_map.tif --name jambi-v1
```

This builds **both approaches** (stencils and outlines) in one transaction. If anything fails, or
you press Ctrl+C, nothing is left behind. It prints the colour mix, which colour was left out, and
the size of every table. Without `--name`, the map is named after the file (`my_map`).

To rebuild just one approach of a map that is already stored:

```
python database/stencil_approach.py build my_map.tif --map jambi-v1
python database/polygon_approach.py build my_map.tif --map jambi-v1
```

If you rebuild a map from a *different* TIFF with one approach, that map's other approach is
removed, so a map never mixes two TIFFs. Just run its build too.

## Several maps

Neon can hold several maps, for example different areas. Each map lives in its own **schema**
(a folder of tables) named after it: `jambi-v1` is stored in `map_jambi_v1`, with its own
`grid_info`, `stencil_pages`, `class_polys`, `square_labels`, `plots` and `plot_results`.
The table `public.maps` lists them all.

```
python database/maps.py list                       # every map: schema, size, TIFF, approaches built
python database/maps.py upload riau.tif --name riau
python database/maps.py rename riau "Riau 2024"     # a new name (its schema is renamed too)
python database/maps.py delete riau --yes          # drops the map's schema: can't be undone
```

- With more than one map stored, tell `check`, `verify` and `compare` which one to use with
  `--map NAME`. With only one map, they use it.
- Names are 1–40 characters, with at least one letter or digit. Two names that would share a
  schema (`Jambi V1` and `jambi-v1`) aren't allowed, and nor is uploading the same TIFF twice.
- The app (`../frontend`) has the same upload, switch and delete in its **Maps** menu.
- A database set up before several maps were supported keeps its one map in the `public`
  schema. Move it once with `python database/maps.py migrate`. It takes a second, and nothing is
  re-uploaded.

## Step 4: Upload farms and get answers (`check`)

```
python database/polygon_approach.py check my_plots.geojson
python database/stencil_approach.py check my_plots.geojson
```

- The farms are saved in the `plots` table, exactly as in the GeoJSON.
- Every answer is saved in `plot_results`, one row per farm per method: the verdict, the
  colours touched, the colour under a point, and hectares of forest, land, deforestation,
  needs review, water and no data.
- A copy goes to `database/results/results_polygons.csv` and `database/results/results_stencils.csv`.

## Step 5: Prove nothing was lost, and compare (`verify`, `compare`)

```
python database/polygon_approach.py verify my_map.tif
python database/stencil_approach.py verify my_map.tif
python compare/compare.py my_map.tif my_plots.geojson
```

- `verify` rebuilds the whole map from Neon and compares every pixel with your TIFF.
- `compare` checks every farm with the original TIFF, the stencils and the outlines, and
  reports the two questions **separately**:
  - **Question 1, "Is the point in forest?"** – every point farm in the GeoJSON
  - **Question 2, "Does the outline overlap forest?"** – every outline farm in the GeoJSON

  For each question it shows decision fidelity (in forest? / overlaps forest?, every colour
  touched, the EUDR verdict) and speed: all farms at once, per farm, and one farm on its own.
  Then come pixel fidelity and storage for the whole map. The per-farm table is written to
  `compare/results/comparison.csv`.

**Test single points.** Add `--point LONGITUDE LATITUDE`, as many times as you like. Each point
gets every method's answer and time. Test points aren't saved to the database. The GeoJSON is
optional when you use `--point`:

```
python compare/compare.py my_map.tif --point 103.6236 -1.7794
python compare/compare.py my_map.tif my_plots.geojson --point 103.6236 -1.7794 --point 103.3709 -1.6326
```

To test a single outline, put it in a GeoJSON file (a FeatureCollection, one Feature, or just the
Polygon itself) in `data/geojson/`.

## Step 6: Look at the data in Neon

In the Neon console, open **Tables** to browse (pick the map's schema, e.g. `map_jambi_v1`), or
the **SQL Editor** to run queries such as:

```sql
SELECT * FROM public.maps;                                              -- the maps stored
SELECT * FROM map_jambi_v1.grid_info;                     -- default_class = the colour left out
SELECT plot_id, method, verdict, colours, point_colour, forest_ha
FROM map_jambi_v1.plot_results ORDER BY plot_id, method;
SELECT * FROM map_jambi_v1.plot_results WHERE colours LIKE '%forest%';  -- in or overlapping forest
SELECT class_id, count(*) FROM map_jambi_v1.class_polys GROUP BY class_id;
SELECT pg_size_pretty(pg_total_relation_size('map_jambi_v1.stencil_pages'));
```

## Try it first with the test data

The included made-up files work with exactly the same steps. Upload them as a map of their own,
so they never touch your real map:

```
python database/test_connection.py
python database/maps.py upload test_map.tif --name test
python database/polygon_approach.py check test_plots.geojson --map test
python database/stencil_approach.py check test_plots.geojson --map test
python compare/compare.py test_map.tif test_plots.geojson --map test
python compare/compare.py test_map.tif --point 103.6236 -1.7794 --point 103.3709 -1.6326 --map test
python database/maps.py delete test --yes
```

The first test point is in forest and the second is on land. (`maps.py upload` refuses a TIFF
that is already stored under another name.)

---

## How a farm is checked

1. **Outside the map?** → `CANNOT_CHECK`.
2. **Detailed check:** all farms are answered together, not one query per farm.
   - *Stencils:* one query fetches the pages under the farms by their position. They are
     unzipped and compared bit by bit.
   - *Outlines:* one query for the points and one for the outlines. PostGIS finds the shapes
     near each farm (spatial index) and measures the overlap. In a batch of 200+ farms, the
     square labels are read first, and farms sitting entirely in one colour skip the geometry.
3. **Verdict** (the worst colour wins):

| Verdict | When |
|---|---|
| `FAIL` | touches deforestation |
| `MANUAL_REVIEW` | touches needs review |
| `CANNOT_CHECK` | outside the map, or touches no-data pixels |
| `CHECK_LOCATION` | point on water, or outline more than half water: coordinates are probably wrong |
| `PASS` | only land and/or forest |

Forest doesn't fail a farm under the EUDR, since only *deforestation* after 31 Dec 2020 does.
It is reported separately: `touches_forest`, `forest_ha` and `point_colour`.

## Options

| Option | Command | Meaning |
|---|---|---|
| `--name NAME` | `maps.py upload` | The new map's name (default: the TIFF's file name) |
| `--map NAME` | `build`, `check`, `verify`, `compare` | Which map: the one to build, or the one to check against when several are stored |
| `--rule touched` / `centre` | stencil `check`, `compare` | Which pixels count as inside an outline: every pixel it overlaps (cautious, default) or only pixels whose centre is inside |
| `--point-mode pixel` / `circle` | `check`, `compare` | A point is checked against the pixel under it (default), or turned into a circle with its declared area |
| `--point LON LAT` | `compare` | Test one point, longitude first. Repeat it for more points |
| `--repeat N` | `compare` | Timing runs per method; the median is reported (default 3) |
| `--single N` | `compare` | How many farms per question are also timed one at a time (default 20) |
| `--skip-verify` | `compare` | Skip the pixel-by-pixel rebuild, which is slow on a big map |
| `--csv FILE` | `check`, `compare` | Where to write the CSV (default: the `results/` folder next to the script) |

Every script explains itself with `--help`, e.g. `python database/stencil_approach.py check --help`.

## Troubleshooting

| Message | Fix |
|---|---|
| `DATABASE_URL is not set` | `.env` is missing or not in the project folder. It must sit next to `README.md`. |
| `Note: using .env.txt` | Windows Notepad added `.txt`. It still works, but turn on "File name extensions" in File Explorer and rename it to `.env`. |
| `Your .env still has the example connection string` | Paste your own string from Neon's **Connect** button. |
| `Could not connect to the database` | Copy the string again from Neon. Keep `?sslmode=require` and check you're online. |
| `Can't find the TIFF file` / `GeoJSON file` | Put the file in `data/tiff/` or `data/geojson/` and check the spelling, including `.tif` vs `.tiff`. |
| `PostGIS could not be switched on` | The outlines approach needs PostGIS. The stencils approach still works. |
| `No map in the database yet` | Upload one first (Step 3). |
| `Your map is stored the old way` | Run `python database/maps.py migrate` once. It moves the map into its own schema without re-uploading. |
| `Several maps are stored (...)` | Add `--map NAME`. `python database/maps.py list` shows the names. |
| `There's no map called '...'` | Check the spelling against `python database/maps.py list`. |
| `There's already a map called '...'` | Choose another name, or delete that map first. |
| `... is too close to the map '...'` | The two names would share a schema (`Jambi V1` and `jambi-v1`). Choose another name. |
| `This TIFF is already stored as '...'` | That map is uploaded already. Use it with `--map`. |
| `This map is ... pixels` | The app keeps a whole map in memory, up to 100 million pixels. Crop the TIFF into smaller areas. |
| `The map '...' is incomplete in the database` | An upload was stopped part-way. Delete it with `maps.py delete` and upload again. |
| `Removed '...' from '...': it was built from a different TIFF` | You rebuilt a map from a new TIFF with one approach. Run the other approach's `build --map` too. |
| `This TIFF is not the map '...' in the database` | `verify` and `compare` need the TIFF that map was uploaded from. Check `--map`. |
| Almost every farm is `CANNOT_CHECK` | The farms are outside your map or crop. GeoJSON coordinates must be longitude first, then latitude. |
| `--point ... is not a longitude/latitude pair` | Give the longitude first (e.g. `103.5`), then the latitude (e.g. `-1.6`). |
| A test point says `outside the map` | `compare` prints the area your map covers. Pick a point inside it. |
| `python` is not found (Windows) | Use `py` instead, e.g. `py database/test_connection.py`. |
| The first command is slow | Neon pauses idle databases and takes a second or two to wake up. |

## Running in Docker (optional)

The database stays on Neon; the container just runs the scripts. From the project folder:

```
docker build -t eudr-storage .
docker run --rm -v "$(pwd):/project" eudr-storage database/test_connection.py
docker run --rm -v "$(pwd):/project" eudr-storage database/maps.py upload my_map.tif --name jambi-v1
docker run --rm -v "$(pwd):/project" eudr-storage compare/compare.py my_map.tif my_plots.geojson
```

`-v` shares the project folder with the container, so it reads your `.env` and `data/`, and
writes the `results/` folders back to your computer. Your `.env` is never copied into the image.
On Windows PowerShell, replace `$(pwd)` with `${PWD}`.

## Keeping things private

`.gitignore` keeps `.env` (your database password), your own TIFFs and GeoJSONs (customer data)
and the results out of Git. Only the small made-up test files are kept.

---

## Results on the test data

Measured with `compare.py` on made-up Jambi maps (land 61%, forest 32%, water 3%,
deforestation 2%, needs review 2%), using a local PostgreSQL 16 + PostGIS 3.4 database.
Times vary by about ±20% between runs.

**Fidelity and storage**

| | 2048 × 2048 map, 303 farms | 6144 × 6144 map, 2,003 farms |
|---|---|---|
| Decision fidelity, stencils / outlines | 100% / 100% | 100% / 100% |
| – Question 1, "is the point in forest?" | 100% / 100% (113 points) | 100% / 100% (694 points) |
| – Question 2, "does the outline overlap forest?" | 100% / 100% (190 outlines) | 100% / 100% (1,309 outlines) |
| Pixel fidelity (all 5 colours), stencils / outlines | 100% / 100% | 100% / 100% |
| TIFF file (compressed) | 0.10 MB | 0.86 MB |
| TIFF uncompressed | 4.19 MB | 37.75 MB |
| **Stencils database** | **0.19 MB** | **0.99 MB** |
| **Outlines database** | **2.56 MB** | **21.46 MB** |

**Speed: checking all the farms of one kind at once**

| | 113 points | 190 outlines | 694 points | 1,309 outlines |
|---|---|---|---|---|
| TIFF directly, pixel rule | 0.007 s | 0.059 s | 0.069 s | 0.52 s |
| TIFF directly, exact area | same as above | 0.19 s | same as above | 1.40 s |
| **Stencils** | **0.005 s** | **0.042 s** | **0.039 s** | **0.33 s** |
| **Outlines** | **0.003 s** | **0.020 s** | **0.030 s** | **0.15 s** |

For a point, both TIFF rules are the same thing: the colour of the pixel under it.

**Speed over a network.** Neon is reached over the internet, so every query waits for a round
trip. To estimate this, the tests were repeated through a link that adds about 7 ms per round
trip, similar to reaching Neon's Singapore region:

- **One farm on its own:** about 7 ms for both stencils and outlines, because each needs a single
  round trip. Reading a TIFF on your own computer takes about 1 ms, since there is no network.
- **Batches:** 694 points took 0.044 s (stencils) and 0.041 s (outlines). 1,309 outlines took
  0.40 s and 0.15 s.

The same 100% results held for a map in metres (UTM 48S), for the centre rule with circle
points, and for a map where forest is the most common colour (so forest was left out instead).

## Honest notes for your evaluation

- **Stencils are about the size of the compressed TIFF** (1.15× on the large map), roughly
  38× smaller than the raw pixels, and queryable in SQL. Without zipping (one row per 8 × 8
  square) they would take an estimated 19 MB on the large map.
- **Outlines are about 25× bigger than the compressed TIFF.** Pixel edges turn every curve into
  a staircase, and each corner costs 16 bytes. Their strengths are exact areas and the fastest
  checks. Tracing in 256-pixel tiles instead of 64 saves about 30% of the storage but makes
  outline checks about 50% slower. 128 is a middle ground: 25% smaller, 15% slower. To change
  it, edit `TRACE_TILE` in `polygon_approach.py`.
- **Speed:** compared with reading the TIFF using the same rule:
  - stencils were 1.4–1.8× faster than the pixel rule
  - outlines were about 10× faster than exact areas, and about 2× faster for points

  Most of the stencils' time goes into working out which pixels each outline covers (in
  Python), not into the database.
- **Storing land and forest doesn't slow the queries down.** Measuring the forest inside each
  outline costs about 0.03 ms per outline, and the speed-ups below more than make up for it:
  - **JIT switched off.** PostGIS marks its shape functions as expensive, so for a big batch
    PostgreSQL's JIT compiler spent about 150 ms compiling a query that then ran in about
    65 ms. Both the old and the new version were losing this time. The outlines check now
    switches JIT off before a big query (`SET LOCAL jit = off`, for that one transaction
    only). PostgreSQL 19 switches JIT off by default for reasons like this.
  - **Square labels** answer farms that sit entirely in one colour without any geometry.
  - **64-pixel tiles** instead of 256 give shapes with fewer corners.

  On the large map, checking all 2,002 farms that are on the map:

  | | Local database | With a 7 ms round trip |
  |---|---|---|
  | Old outlines (land and forest not stored) | 0.32 s | 0.37 s |
  | Old outlines, with JIT also switched off | 0.17 s | – |
  | **New outlines (every colour stored)** | **0.14 s** | **0.19 s** |

  Points alone took about the same time in both versions.
- **Rules matter:** the pixel rule and the exact-area rule can disagree on a farm or two, and
  pixel-rule hectares are larger for small farms. That's a policy choice to agree with
  SystemEarth, not a storage error.
