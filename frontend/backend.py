"""backend.py - makes the backend's own modules importable, the same way compare/compare.py does.

    from backend import common, polygon_approach, stencil_approach

Everything the app knows about the map and the EUDR rules comes from these modules, so the app
always gives the same answers as the command-line tools. The app changes the database only through
these modules: maps.upload / maps.delete (asked for in the Maps menu) and common.save_results
(Check ▸ Save results to Neon).
"""

import os
import sys

DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "backend", "database")
if DATABASE_DIR not in sys.path:
    sys.path.insert(0, DATABASE_DIR)

import common  # noqa: E402
import maps  # noqa: E402
import polygon_approach  # noqa: E402
import stencil_approach  # noqa: E402

__all__ = ["common", "maps", "polygon_approach", "stencil_approach"]
