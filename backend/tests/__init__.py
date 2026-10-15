"""The backend's unit tests. Run from the backend folder:

    venv\\Scripts\\python -m unittest discover -s tests -t .

They need no database (a fake connection answers the queries) and no real data (made-up layers are
written into temporary folders). The scripts in database/ and compaction/ import each other by
file name, as when they are run directly, so both folders go on the import path here."""

import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for folder in ("compaction", "database", "compare"):
    path = os.path.join(BACKEND_DIR, folder)
    if path not in sys.path:
        sys.path.insert(0, path)
