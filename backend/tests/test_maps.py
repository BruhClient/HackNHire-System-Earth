import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import common
import evidence_approach
import layers
import maps
from tests.fakes import Script, built_results, quietly


class UploadResultsTest(unittest.TestCase):
    """A results folder of the compaction step becomes a map with all three approaches, in one
    transaction: the stencils and outlines from its 5 colours, the evidence pages from evidence.tif."""

    @classmethod
    def setUpClass(cls):
        cls.results = built_results()

    def setUp(self):
        self.calls, self.fail = [], None
        grid = common.Grid("jambi", 110.0, -7.0, 1 / 12000, 1 / 12000, 600, 600, 4326, True, schema="map_jambi")

        def builder(kind):
            def build(conn, source, name, report=None, tidy=True):
                self.calls.append((kind, source, name, tidy))
                if kind == self.fail:
                    raise RuntimeError("the connection to Neon dropped")
                return grid
            return build

        for target, attr, new in ((maps.stencil_approach, "build", builder("stencils")),
                                  (maps.polygon_approach, "build", builder("outlines")),
                                  (evidence_approach, "build", builder("evidence")),
                                  (maps.common, "tidy_up", lambda conn, table: self.calls.append(("tidy", table))),
                                  (maps.common, "print_storage", lambda conn, tables: None)):
            patcher = mock.patch.object(target, attr, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_all_three_approaches_are_built_in_one_transaction_then_tidied(self):
        conn = Script([("'public.maps'", [(True,)])])
        quietly(maps.upload, conn, self.results, "jambi")
        colours = layers.class_map(self.results)
        self.assertEqual(self.calls, [
            ("stencils", colours, "jambi", False), ("outlines", colours, "jambi", False),
            ("evidence", os.path.abspath(self.results), "jambi", False),
            ("tidy", "map_jambi.stencil_pages"), ("tidy", "map_jambi.class_polys"),
            ("tidy", "map_jambi.square_labels"), ("tidy", "map_jambi.evidence_pages")])
        self.assertEqual((conn.commits, conn.rollbacks), (1, 0))

    def test_a_failure_in_the_evidence_rolls_everything_back(self):
        self.fail = "evidence"
        conn = Script([("'public.maps'", [(True,)])])
        with self.assertRaises(RuntimeError):
            quietly(maps.upload, conn, self.results, "jambi")
        self.assertEqual((conn.commits, conn.rollbacks), (0, 1))
        self.assertNotIn("tidy", [c[0] for c in self.calls])

    def test_a_folder_that_is_not_a_results_folder_is_refused_before_anything_is_built(self):
        conn = Script([("'public.maps'", [(True,)])])
        with self.assertRaises(SystemExit) as cm:
            quietly(maps.upload, conn, os.path.dirname(self.results), "jambi")
        self.assertIn("isn't a results folder", str(cm.exception.code))
        self.assertEqual(self.calls, [])


class ListMapsTest(unittest.TestCase):
    def test_the_app_gets_the_list_it_always_got(self):
        added = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
        conn = Script([("'public.maps'", [(True,)]),
                       ("FROM public.maps m", [("riau", "map_riau", "riau.tif", 700, 600, added, True, True)])])
        self.assertEqual(maps.list_maps(conn), [{
            "name": "riau", "schema": "map_riau", "source_file": "riau.tif", "width": 700, "height": 600,
            "added_at": added.isoformat(), "stencils": True, "outlines": True}])
        self.assertNotIn("evidence_pages", conn.sql[-1])

    def test_with_evidence_it_also_says_which_maps_have_evidence_pages(self):
        conn = Script([("'public.maps'", [(True,)]),
                       ("FROM public.maps m", [("jambi", "map_jambi", "jambi/outcome_5class.tif", 6000, 6000,
                                                None, True, True, True)])])
        listed = maps.list_maps(conn, with_evidence=True)
        self.assertTrue(listed[0]["evidence"])
        self.assertIn("evidence_pages", conn.sql[-1])

    def test_every_map_table_is_known(self):
        for table in ("evidence_info", "evidence_pages", "evidence_results"):
            self.assertIn(table, maps.MAP_TABLES)


if __name__ == "__main__":
    unittest.main()
