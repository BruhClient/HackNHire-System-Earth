# HackNHire: fast, faithful EUDR checks for rubber plots

A student prototype for the **SystemEarth Hack & Hire** challenge (team hacknhire, NTU).

About 20 satellite layers (2000–2026) are squeezed into **one 32-bit number per pixel**, stored in Neon (cloud PostgreSQL), and used to check farm plots. Every plot gets exactly the answer the original layers give.

**Use case: rubber under the EU Deforestation Regulation (EUDR).** A plot passes if its land was not forest on 31 Dec 2020, or that forest has not been cleared since. Rubber is tricky: plantations look like forest, and replanting looks like clearing. The EUDR counts rubber as farmland (Art. 2(6)), so clearing on rubber land is flagged **needs review**, not failed. Test region: Jambi, Sumatra (36 million pixels at 10 m).

---

## How it works

```mermaid
flowchart LR
    A["~20 satellite layers"] --> B["1. Compaction<br/>eudr_layers.py"]
    B --> C["evidence.tif<br/>1 number per pixel"]
    C --> D[("2. Neon<br/>evidence pages,<br/>stencils, outlines")]
    P["Farm plots"] --> E["Check: one query"]
    D --> E
    E --> F["Verdict, reason,<br/>hectares, years"]
```

1. **Compaction** (`backend/compaction/eudr_layers.py`, Joe's code) packs the EUDR facts per pixel: forest in 2020, rubber, and the year each source (TMF, Hansen, RADD radar) records a clearing. Time is stored as events, not one map per year.
2. **Storage in Neon** (`backend/database/`) keeps the result three competing ways:

| | Evidence pages | Stencils | Outlines |
|---|---|---|---|
| Stores | All facts (32 bits) | 5-colour map | 5-colour map, as PostGIS shapes |
| A check | Fetch pages, run Joe's rule | Fetch pages, count pixels | PostGIS measures overlaps |
| Best at | Full answer: verdict, reason, hectares, years | Tiny storage, few round trips | Exact areas, fastest |

---

## Quick start

You need Python 3.10+ and a [Neon](https://console.neon.tech) project.

**1. Set up** (PowerShell, from this folder)

```
python -m venv backend\venv
backend\venv\Scripts\python -m pip install -r requirements.txt
python -m venv frontend\.venv
frontend\.venv\Scripts\python -m pip install -r requirements.txt
```

Create `backend\.env` with one line (your string from Neon's **Connect** button):

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
```

**2. Add a map: the ingest window**

```
backend\venv\Scripts\python backend\ingest_app.py
```

Choose your satellite-layer folder, name the map, and watch it run. It builds the compact layers, uploads them to Neon and checks the test plots.

**3. Check farms: the app**

```
frontend\.venv\Scripts\python frontend\app.py
```

Pick your map, draw farms (or open a GeoJSON file), press **Check** (Ctrl+Enter). The **Compare** tab benchmarks the approaches.

---

## Results

Jambi, 1,000 test plots.

| Measure | Result |
|---|---|
| Decision fidelity | **100%** (1,000 of 1,000): all 35 outputs match the original layers |
| Pixel fidelity | **100%** of 36 M pixels in the decision map |
| Storage | **6.7 MB** in Neon vs 245.9 MB raw (36× smaller) or 13.7 MB compressed (2×) |
| Speed over Neon | 1,000 plots in **3.5 s**, one query (median of 3) |
| Speed, local disk | ~1.1 ms per plot vs 10.9 ms from the original layers |

The local-disk timing and per-layer figures come from Joe's own runs. The rest were measured against Neon.

---

## Repository

```
backend/    compaction, Neon storage, checks, evaluation, ingest window
frontend/   the desktop app
docs/       design specs and plans
```

Details: [backend/README.md](backend/README.md) · [frontend/README.md](frontend/README.md)

Tests: `cd backend; venv\Scripts\python -m unittest discover -s tests -t .`

---

## Limitations

- Storage is only 2× smaller than compressed originals. The bigger win is less work per check.
- 100% fidelity is not accuracy: it proves nothing is lost, not that the rule is right.
- The evidence keeps only what this rule reads. A new rule needs a rebuild.
- This repo has `eudr_layers.py` v1.1. Newer versions split the evidence into three files.
- One region tested. Social risks (indigenous lands, legality) are not covered.

## Privacy

`.env`, satellite layers, TIFFs and GeoJSONs stay out of Git. Never commit or share `DATABASE_URL`.

## Credits

Compaction by Joe ([Joeongtengkiat/HacknHire](https://github.com/Joeongtengkiat/HacknHire)). Storage, checks and apps by the rest of team hacknhire. The stencil idea came from SystemEarth's CTO. Claude helped write and review code; no AI makes any compliance decision.
