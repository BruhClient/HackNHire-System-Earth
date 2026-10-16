# HackNHire: fast, faithful EUDR checks for rubber plots

A student prototype for the **SystemEarth Hack & Hire** challenge (team hacknhire, NTU).

**The problem.** Deciding whether a farm plot is deforestation-free means combining about 20 satellite layers across 2000–2026. A compliance decision rarely needs the raw values. It needs a few facts per pixel: was it forest on 31 December 2020, and has it been cleared since?

**Our answer.** Compress the layers into one 32-bit "evidence" number per pixel. Store it in Neon (cloud PostgreSQL), and check any number of plots in one query. Every plot gets exactly the answer the original layers give.

**The use case.** Rubber plots under the EU Deforestation Regulation (EUDR, Regulation (EU) 2023/1115).

- Rubber is one of the seven commodities the EUDR covers.
- Rubber plantations look like forest from space, and replanting looks like clearing.
- The EUDR counts rubber plantations as farmland, not forest (Art. 2(6)). So clearing on rubber land is flagged for review as likely replanting, not failed as deforestation.
- The test region is a 0.5° × 0.5° box in Jambi, Sumatra: about 3,075 km², or 36 million pixels at 10 m.

---

## Contents

1. [How it works](#how-it-works)
2. [The EUDR rule we implement](#the-eudr-rule-we-implement)
3. [The evidence number](#the-evidence-number)
4. [Three ways to store it in Neon](#three-ways-to-store-it-in-neon)
5. [Results](#results)
6. [Repository layout](#repository-layout)
7. [Setup](#setup)
8. [Quick start](#quick-start)
9. [The desktop app](#the-desktop-app)
10. [Testing](#testing)
11. [Limitations and next steps](#limitations-and-next-steps)
12. [Privacy and safety](#privacy-and-safety)

---

## How it works

Two steps, run together by `backend/pipeline.py`.

```mermaid
flowchart LR
    subgraph IN["About 20 satellite layers, 2000-2026"]
        A1["JRC forest 2020<br/>and forest type"]
        A2["Hansen tree loss"]
        A3["JRC TMF yearly change"]
        A4["RADD radar and optical alerts"]
        A5["Rubber map, land cover,<br/>protected areas"]
    end

    IN --> C["Step 1: Compaction<br/>eudr_layers.py"]
    C --> E["evidence.tif<br/>one 32-bit number per pixel"]
    C --> O["outcome.tif<br/>5-colour map"]

    E --> N1[("Neon:<br/>evidence pages")]
    O --> N2[("Neon:<br/>stencils")]
    O --> N3[("Neon:<br/>outlines (PostGIS)")]

    P["Farm plots<br/>GeoJSON points and polygons"] --> Q["Step 2: Check<br/>one batched query"]
    N1 --> Q
    N2 --> Q
    N3 --> Q
    Q --> R["Verdict, reason,<br/>hectares, clearing years"]
```

1. **Compaction** (`backend/compaction/eudr_layers.py`, Joe's code). It reads the layers for a box, puts them on one 10 m grid, and packs the EUDR decision facts into `evidence.tif`. It also applies the rule to every pixel to make `outcome.tif`.
2. **Storage in Neon** (`backend/database/`). The compact layers are stored in Neon PostgreSQL in three competing ways, and farm plots are checked against them.

### One check, end to end

```mermaid
sequenceDiagram
    participant U as User or app
    participant B as Backend check()
    participant N as Neon
    U->>B: Farm plots (GeoJSON)
    B->>B: Work out which 256 x 256 pages the plots touch
    B->>N: One query: fetch those pages
    N-->>B: Compressed pages
    B->>B: Unzip, run the EUDR rule on the pixels under each plot
    B-->>U: Verdict, reason, hectares and years per plot
```

Neon sits behind a pooler, and every query is a network round trip. So all plots are answered in a few batched queries, never one query per plot.

---

## The EUDR rule we implement

The rule lives only in `eudr_layers.py`. Nothing else re-implements it.

| Question | How the rule answers it |
|---|---|
| Was it forest on the cut-off date? | JRC GFC2020 says forest on 31 Dec 2020 (over 0.5 ha, trees over 5 m, canopy over 10%). |
| Has it been cleared since? | Years 2021–2027, from three independent sources: JRC TMF, Hansen and RADD radar, plus optical alerts after the yearly data ends. |
| Is it rubber? | The rubber map. Clearing on rubber land goes to **needs review** (possible replanting), never **confirmed**. |
| How sure are we? | **Confirmed** needs two independent sources (TMF with Hansen or RADD). One source gives **probable** or **needs review**. |

Other rules the evidence supports:

- **Degradation** is reported, but it only fails *wood* under the EUDR. For rubber it does not fail a plot.
- **Points** become a circle of their declared area (4 ha if none is given), as the EU system does.
- **Missing data** is never treated as deforestation-free.
- **Gaps** of up to about 60 m inside a confirmed clearing count as confirmed, because TMF's and Hansen's 30 m grids do not line up.

Verdicts from the evidence check: `deforestation-free`, `non-compliant`, `probable deforestation`, `needs review`.

---

## The evidence number

Each pixel holds 17 facts in 32 bits. Time is stored as **events** (the first clearing year per source), not one map per year.

```mermaid
flowchart LR
    subgraph L["Original layers"]
        L1["Forest 2020 + type"]
        L2["Rubber"]
        L3["TMF 2020 + yearly"]
        L4["Hansen loss year"]
        L5["RADD alerts"]
        L6["Optical alerts"]
        L7["Land cover, land/water,<br/>protected areas"]
    end
    subgraph B["32-bit evidence value"]
        B1["bits 0-3: forest, type, rubber"]
        B2["bits 4-10: TMF class, clearing year, degradation"]
        B3["bits 11-15: Hansen loss, protected"]
        B4["bits 16-20: RADD year, confidence, watched"]
        B5["bits 21-24: land cover after clearing, land/water"]
        B6["bits 25-31: optical alert, neighbour context"]
    end
    L1 --> B1
    L2 --> B1
    L3 --> B2
    L4 --> B3
    L7 --> B3
    L5 --> B4
    L7 --> B5
    L6 --> B6
```

| Treatment | What |
|---|---|
| **Kept exactly** | Forest on the cut-off date, forest type, rubber, land and water, TMF class in 2020, protected areas |
| **Summarised** | TMF, Hansen, RADD and optical alerts become "first clearing year after 2020" plus a confidence flag. Land cover becomes one 2-bit fact: what replaced the trees |
| **Dropped** | What the decision never reads: alert days, yearly land-cover classes, Hansen tree cover 2000, the TMF transition map |

Some facts depend on neighbouring pixels, such as the gaps above. These are worked out once, at build time, so a check only reads the pixels under the plot.

The full bit layout is in the docstring at the top of `backend/compaction/eudr_layers.py`, and inside `evidence.tif` itself.

---

## Three ways to store it in Neon

Each map lives in its own schema (`map_<name>`), listed in `public.maps`.

| | Evidence pages | Stencils | Outlines |
|---|---|---|---|
| **Stores** | `evidence.tif`: all 17 facts, 32 bits | The 5-colour outcome | The 5-colour outcome |
| **Idea** | Each 256 × 256 page holds its distinct values (about 25) plus one index per pixel, zipped | Each page holds one colour code per pixel, packed and zipped | Each patch of colour is traced into a PostGIS polygon (64 × 64 tiles), plus square labels |
| **Tables** | `evidence_pages`, `evidence_info` | `stencil_pages` | `class_polys` (GiST index), `square_labels` |
| **A check** | One query fetches the pages. Joe's own rule runs on them | One query fetches the pages. Pixels are counted on the client | PostGIS measures exact overlaps on the server |
| **Answers** | Joe's verdict and reason, hectares by source, years | Colours touched, hectares, verdict | Colours touched, exact hectares, verdict |
| **Strength** | Full answer, identical to the original layers | Few round trips, tiny storage | Exact areas, fastest checks |
| **Cost** | Needs the rule code on the client | Local compute and more data | Larger storage, more server work |

Stencils and outlines give the same verdict as each other on every test farm so far: 1,111 of 1,111 in the app's benchmark.

```mermaid
flowchart TD
    M["Neon schema map_name"] --> GI["grid_info<br/>extent, pixel size, default class"]
    M --> EV["Evidence pages<br/>evidence_pages, evidence_info"]
    M --> ST["Stencils<br/>stencil_pages"]
    M --> OL["Outlines<br/>class_polys, square_labels"]
    M --> PA["protected_areas<br/>WDPA polygons, display only"]
    M --> RS["plots, plot_results,<br/>evidence_results"]
```

Every table is defined in `backend/database/schema.sql`. Uploads are atomic: a failure or Ctrl+C leaves nothing behind.

---

## Results

All figures are for the Jambi box, with 1,000 test plots spread across every outcome, unless stated.

| Measure | Result |
|---|---|
| **Decision fidelity** | **1,000 of 1,000 plots (100%)** match the original layers on all 35 outputs: verdict, reason, every hectare figure and the clearing years |
| **Pixel fidelity** | The decision map rebuilt from the evidence matches the original on 100% of 36 M pixels. Every Neon store rebuilds the raster it came from with 0 differing pixels |
| **Storage** | Evidence pages in Neon: **6.7 MB**. Original layers: 245.9 MB raw, 13.7 MB compressed. That is 36× smaller than raw and 2× smaller than compressed |
| **Speed over Neon** | 1,000 plots in **3.5 s** (median of 3), in one query. One plot alone takes 27–30 ms, mostly network |
| **Speed on local disk** | About 1.1 ms per plot from the evidence, against 10.9 ms from the original layers (10,000 plots) |
| **Query work** | A check reads 3 evidence files instead of 21 original files |

**Per-layer pixel fidelity.** Reading each original layer back from the evidence:

- Seven layers are 100% exact.
- TMF yearly is 96.6%, RADD 98.2% and DIST-ALERT 89.4%.
- The losses are alert days and yearly classes, which the decision never uses.

**Cost estimate (draft).** Evidence for all of Indonesia is about 4–6 GB, or about $0.10–0.16 a month on S3. One million checks cost about $1.60 in reads, against $12 on the original layers. An always-on server (about $31 a month) costs far more than storage.

**How the numbers were measured**

- Timings are medians of several runs, and say "over Neon" or "local disk".
- The Neon figures come from this repository's code against a real Neon database.
- The 10,000-plot local timing, the per-layer pixel figures and the cost estimate come from the compaction team's own runs, not from this repository's tests.

---

## Repository layout

```
HackNHire/
├── README.md                  <- you are here
├── CLAUDE.md                  <- project rules for Claude Code
├── MEMO.txt                   <- the short challenge memo
├── requirements.txt           <- the one list of Python packages
├── backend/                   <- compaction, storage, checks, evaluation
│   ├── pipeline.py            <- runs compaction -> upload -> check
│   ├── ingest_app.py          <- the same chain in a window (PyQt5)
│   ├── compaction/
│   │   ├── eudr_layers.py     <- Joe's code: satellite layers -> evidence.tif, outcome.tif
│   │   └── make_test_layers.py<- made-up satellite layers for trying things out
│   ├── database/
│   │   ├── schema.sql         <- every table definition
│   │   ├── maps.py            <- list / upload / rename / delete / migrate maps
│   │   ├── evidence_approach.py, stencil_approach.py, polygon_approach.py
│   │   ├── protected_areas.py <- WDPA polygons stored with a map
│   │   ├── layers.py          <- reads a results folder for the database
│   │   └── common.py          <- shared rules, grid, farm loading
│   ├── compare/compare.py     <- the evaluation: Neon vs files vs original layers
│   ├── tests/                 <- unit tests and a live smoke test
│   └── Dockerfile
├── frontend/                  <- PyQt5 desktop app (map, checks, benchmark)
└── docs/superpowers/          <- design specs and implementation plans
```

More detail: [backend/README.md](backend/README.md), [frontend/README.md](frontend/README.md), [backend/compaction/README.md](backend/compaction/README.md).

---

## Setup

You need **Python 3.10 or newer** and a [Neon](https://console.neon.tech) project (the AWS Asia Pacific, Singapore region works well).

From the `HackNHire` folder (PowerShell):

```
python -m venv backend\venv
backend\venv\Scripts\python -m pip install -r requirements.txt

python -m venv frontend\.venv
frontend\.venv\Scripts\python -m pip install -r requirements.txt
```

Create `backend/.env` with one line and no quotes, using your own connection string from Neon's **Connect** button:

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
```

Then test the connection and set up a fresh database once:

```
backend\venv\Scripts\python backend\database\test_connection.py
backend\venv\Scripts\python backend\database\maps.py init
```

`.env` is ignored by Git. Never commit it or paste it anywhere.

---

## Quick start

### Try it without real data

```
cd backend
venv\Scripts\python compaction\make_test_layers.py C:\temp\test_layers    # prints the --bbox to use
venv\Scripts\python pipeline.py --name test --data-dir C:\temp\test_layers --bbox <the printed box> --test-plots 300 --verify
venv\Scripts\python compare\compare.py test --map test
venv\Scripts\python database\maps.py delete test --yes
```

### Build and upload a real map

```
cd backend
venv\Scripts\python pipeline.py --name jambi --data-dir D:\eudr\inputs --bbox 103.0 -2.3 103.5 -1.8 --test-plots 1000 --verify
```

This does three things:

1. **Compaction.** Reads the layers in `--data-dir` for the box and writes `layers/jambi/` (`evidence.tif`, `outcome.tif`, test plots and, with `--verify`, the check against the original layers).
2. **Upload.** Stores the evidence pages, stencils and outlines in Neon as the map `jambi`, in one transaction.
3. **Check.** Checks the test plots against Neon, and compares every answer with `eudr_layers.py`'s own.

Put WDPA protected-area GeoJSON files (names starting `wdpa`) in `--data-dir` and the pipeline finds them by itself.

Prefer a window? Run `backend\venv\Scripts\python backend\ingest_app.py`.

### Check your own farms

```
cd backend
venv\Scripts\python database\evidence_approach.py check farms.geojson --map jambi    # verdict, reason, hectares, years
venv\Scripts\python database\stencil_approach.py check farms.geojson --map jambi
venv\Scripts\python database\polygon_approach.py check farms.geojson --map jambi
```

Plots can be in the EU Information System format (`ProductionPlace`, `Area`) or have `plot_id` and `area_ha`. Coordinates are longitude first, then latitude.

### Prove nothing was lost

```
venv\Scripts\python database\evidence_approach.py verify jambi --map jambi
venv\Scripts\python compare\compare.py jambi --map jambi
```

`verify` rebuilds the whole map from Neon and matches the file bit for bit. `compare.py` reports decision fidelity, pixel fidelity, speed and storage for every approach.

### See what is stored

```
backend\venv\Scripts\python backend\database\maps.py list
```

### Docker (optional)

The database stays on Neon. The container only runs the scripts. From the `HackNHire` folder:

```
docker build -f backend/Dockerfile -t eudr-storage .
docker run --rm -v "$(pwd)/backend:/project" eudr-storage database/test_connection.py
```

Your `.env` is never copied into the image. See [backend/README.md](backend/README.md) for more.

---

## The desktop app

```
frontend\.venv\Scripts\python frontend\app.py
```

```mermaid
flowchart LR
    W["MainWindow"] --> MP["Map tab<br/>Leaflet map of the stored map"]
    W --> RP["Results panel<br/>verdicts per farm"]
    W --> CT["Compare tab<br/>benchmark: speed and query efficiency"]
    W --> WK["One worker thread"]
    WK --> NS["NeonService"]
    NS --> BE["Backend modules"]
    BE --> NE[("Neon")]
```

- Pick a map from the **Map** dropdown. It loads from Neon and is drawn colour-coded by class.
- Draw farms with **Polygon (P)** or **Point (O)**, or open a GeoJSON file.
- Press **Check (Ctrl+Enter)**. Each farm is checked by stencils and outlines, and the verdict is the worse of the two.
- The **Compare** tab benchmarks batches of 1, 10, 100 and 1,000 farms, and shows round trips, data returned and server time.
- **Maps ▸ Rename** and **Maps ▸ Delete** manage stored maps. Both ask first. The app does not upload maps: use the ingest window or `pipeline.py`.

The app calls the backend's own functions, so its answers match the command-line tools. Map tiles are drawn locally from the downloaded pages, so no tile server is needed.

---

## Testing

```
cd backend; venv\Scripts\python -m unittest discover -s tests -t .; cd ..          # backend (no database needed)
frontend\.venv\Scripts\python -m unittest discover -s frontend/tests -t frontend   # frontend
cd backend; venv\Scripts\python tests\smoke_pipeline.py; cd ..                     # whole chain on a throwaway map
cd frontend; .venv\Scripts\python smoke_neon.py --map NAME; cd ..                  # read-only Neon check
```

The smoke test builds a made-up 600 × 600 map, uploads it to a map of its own, and checks fidelity. It deletes the map afterwards. A change is done when the unit tests pass and the smoke test says "Everything matches": decision fidelity 100% and 0 differing pixels for all three approaches.

---

## Limitations and next steps

- **The storage gain against compressed originals is modest (2×).** The bigger win is the work each check saves. *Next:* store the 30 m sources at 30 m, and try sparse alert lists.
- **Fidelity is not accuracy.** 100% fidelity shows the compression loses nothing the rule reads, not that the rule is right. Our test plots are made up, and we have only a small comparison with the organiser's field-verified plots. *Next:* an independent checker and a larger accuracy sample.
- **The evidence keeps only what this rule reads.** A new canopy threshold, cut-off date or alert window needs a rebuild. *Next:* archive the original layers beside the evidence, and test many rule variants.
- **Version gap.** The compaction code in this repository is `eudr_layers.py` version 1.1, which writes a single `evidence.tif`. Newer versions split the evidence into three files by how often they change (baseline, yearly, alerts) and keep exact Hansen loss years. *Next:* move the Neon evidence pages to the newer layout.
- **Scope.**
  - One region was tested.
  - TMF covers only tropical moist forest, so other regions need a rule variant.
  - Social and governance risks, such as indigenous lands and legality, are not covered yet.
- **Serving.** The evidence check runs the rule in Python on the client. *Next:* cache each farm's answer until its map tile changes, and use pay-per-use functions instead of an always-on server.
- **Rule choices.** The stencils' pixel rule and the outlines' exact-area rule can disagree on a farm or two. That is a policy choice to agree with SystemEarth, not a storage error.

---

## Privacy and safety

- **`.env` and `DATABASE_URL`** are never committed, printed or logged.
- **Satellite layers, TIFFs and GeoJSONs are customer data.** `.gitignore` ignores `backend/layers/`, every `*.tif`, `*.tiff` and `*.geojson`. There is no `data/` folder: inputs are given by path. Tests use made-up data in temporary folders.
- **Writes to Neon** are limited to uploads through `pipeline.py` or the ingest window, **Maps ▸ Rename**, **Maps ▸ Delete**, and **Check ▸ Save results to Neon**. Run `maps.py list` before writing, and use a map name of your own for tests.

---

## Credits

- **Compaction** (`eudr_layers.py`): Joe, from [Joeongtengkiat/HacknHire](https://github.com/Joeongtengkiat/HacknHire).
- **Storage, checks, evaluation and apps:** the rest of team hacknhire. The stencil idea came from SystemEarth's CTO.
- **Data:** JRC GFC2020, GFT2020 and TMF; Hansen Global Forest Change; RADD (WUR); UMD primary forest; DIST-ALERT; a Southeast Asia rubber map; Impact Observatory land cover; WDPA.
- Claude (Anthropic) helped write and review code. No AI model makes any compliance decision.
