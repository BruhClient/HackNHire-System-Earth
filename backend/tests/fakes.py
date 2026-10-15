"""fakes.py - test doubles: a scripted fake database connection, and a results folder built once
from made-up layers (the compaction step, run for real)."""

import atexit
import contextlib
import functools
import io
import os
import shutil
import tempfile

import eudr_layers
import make_test_layers


class Script:
    """A fake psycopg connection. Each query is answered by the first answer whose fragment appears in
    the SQL or in its parameters; an answer is a list of rows, or a function (sql, params) -> rows.
    Everything executed is kept in .sql and .params, and rows sent with COPY in .copied."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.sql, self.params = [], []
        self.copied = {}                     # COPY statement -> rows written
        self.commits = self.rollbacks = 0
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return ScriptCursor(self)

    def execute(self, sql, params=None):
        cur = ScriptCursor(self)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True

    def answer(self, sql, params):
        text = f"{sql} | {params!r}"
        for fragment, rows in self.answers:
            if fragment in text:
                return list(rows(sql, params) if callable(rows) else rows)
        return []

    def round_trips(self):
        return len(self.sql)


class ScriptCursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        self.conn.params.append(params)
        self.rows = self.conn.answer(sql, params)

    def executemany(self, sql, rows):
        for params in rows:
            self.execute(sql, params)

    def copy(self, sql):
        self.conn.sql.append(sql)
        return ScriptCopy(self.conn.copied.setdefault(sql, []))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class ScriptCopy:
    """COPY ... FROM STDIN: the rows are kept."""

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.rows.append(tuple(row))


def quietly(work, *args, **kwargs):
    """Run work(*args, **kwargs) without its printing."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return work(*args, **kwargs)


@functools.lru_cache(maxsize=None)
def built_results(size: int = 600, test_plots: int = 120) -> str:
    """A results folder from the compaction step (eudr_layers.py, run for real) on made-up layers:
    evidence.tif, outcome.tif, test_plots.geojson, summary.json, ... Built once per test run (a few
    seconds) and removed at the end."""
    root = tempfile.mkdtemp(prefix="eudr_test_")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    inputs, out = os.path.join(root, "inputs"), os.path.join(root, "results")
    bbox = make_test_layers.make_layers(inputs, size=size)
    quietly(eudr_layers.main, ["--data-dir", inputs, "--bbox", *map(str, bbox), "--out-dir", out,
                               "--test-plots", str(test_plots), "--no-baseline"])
    return out
