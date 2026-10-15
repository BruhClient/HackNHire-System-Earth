# CLAUDE.md

This is a student prototype for a SystemEarth challenge. EUDR maps of rubber farms in Indonesia are stored in Neon PostgreSQL in two competing ways, and farm plots (GeoJSON points and polygons) are checked against them.

The user's main map is `my_map.tif`: Jambi, 6,000 × 6,000 pixels, 5 classes. In Neon it is stored as the map `jambi-v1`.

| Folder | What it is | Read |
|---|---|---|
| `backend/` | the storage approaches, the EUDR rules, the maps (`database/maps.py`: list / upload / rename / delete / migrate), check, verify, and the evaluation (command line) | `backend/CLAUDE.md`, `backend/README.md` |
| `frontend/` | the PyQt5 desktop app: picks a map from Neon and shows it, draws and checks farms with both approaches, compares speed and query efficiency, uploads, renames and deletes maps | `frontend/CLAUDE.md`, `frontend/README.md` |
| `docs/superpowers/` | design specs and implementation plans: the frontend link (scope narrowed on 2026-10-15) and several maps (`2026-10-15-multi-map`) | |

Claude Code loads the `CLAUDE.md` of the folder you work in. The rules in each one apply there.

## The two approaches in one paragraph each

- **Stencils** (`stencil_pages`). The map is cut into 256 × 256 pages. Each page holds one bitmap per class: 8 × 8 squares packed into 64-bit numbers, then zipped. A check fetches the pages a farm touches in one query and works out the answer on the client, counting pixels. This approach means few round trips and almost no server work, but a lot of data and local compute.
- **Outlines** (`class_polys`, GiST-indexed, plus `square_labels`). Every class except the most common one is traced into PostGIS polygons. A check asks PostGIS which outline holds a point, or how much area of each class overlaps a polygon. This gives exact areas and tiny replies, at the cost of more server work and a few more round trips.

Both give the same verdict on every test farm so far: 1,111 of 1,111 in the app's benchmark.

**Maps.** Each map lives in its own schema (`map_<name>`, e.g. `map_jambi_v1`). The schema holds `grid_info`, both approaches' tables, `plots` and `plot_results`. `public.maps` lists the maps. Every query names its tables with the schema (`grid.table(...)`).

## Rules for the whole project

- **The user's Neon database holds their real map**, `jambi-v1` (schema `map_jambi_v1`).
  - Never build, upload over or delete it, or anything else that writes to its tables, without asking first.
  - Run `backend\venv\Scripts\python backend\database\maps.py list` before writing.
  - Test uploads go into a map of their own (e.g. `--name test`) and are deleted afterwards.
  - The app writes only for Maps ▸ Upload map…, Rename map…, Delete map… (all ask first) and Check ▸ Save results to Neon.
- **Never commit, print or log `.env` or `DATABASE_URL`.** The file is `backend/.env`, and both halves read it through `common.connect()`.
- **Real TIFFs and GeoJSONs are customer data.** They stay in `backend/data/` and out of Git. Only `test_map.tif` and `test_plots.geojson` may be tracked. Tests make made-up TIFFs in temporary folders.
- **The backend is the single source of truth for the EUDR rules.** The frontend imports it and never re-implements it. A backend interface change updates the frontend in the same change (see "What the frontend relies on" in `backend/CLAUDE.md`).
- **Neon is pooled (PgBouncer, transaction mode).**
  - Keep `prepare_threshold=None` and use `SET LOCAL` only.
  - End every transaction.
  - Batch farms into a few queries, never one query per farm.
- **Speed claims must be honest.** Give medians of several runs, say "over Neon" (or local), and label EXPLAIN numbers as warm re-runs.
- **Wording:** plain English and British spelling (colour, metres). Error messages say how to fix the problem.
- **Dependencies:** none beyond each folder's `requirements.txt`.
- **Commits:** this folder is a git repository (branch `main`, remote `origin` = github.com/BruhClient/HackNHire-System-Earth, private). Commit only when the user asks. `.gitignore` keeps out `.env`, the venvs and every real TIFF and GeoJSON; check `git status` before committing.

## Quick commands (from `HackNHire\`, PowerShell)

```
frontend\.venv\Scripts\python frontend\app.py                                      # the app
frontend\.venv\Scripts\python -m unittest discover -s frontend/tests -t frontend   # tests (frontend + backend helpers)
backend\venv\Scripts\python backend\database\maps.py list                          # the maps stored in Neon
cd frontend; .venv\Scripts\python smoke_neon.py --map jambi-v1; cd ..              # read-only Neon check
cd frontend; .venv\Scripts\python smoke_maps.py; cd ..                             # several maps, on a throwaway map
cd backend; python compare/compare.py my_map.tif my_plots.geojson --skip-verify --map jambi-v1   # backend evaluation
```
