import json
import os
import tempfile
import unittest
import zlib

import numpy as np
import rasterio

import common
import eudr_layers as el
import evidence_approach as ev
import layers
from tests.fakes import Script, built_results, quietly


class PagesTest(unittest.TestCase):
    """A page = its distinct evidence values (the palette) + one index per pixel, zipped."""

    def test_a_page_round_trips(self):
        rng = np.random.default_rng(1)
        page = rng.choice(np.array([0x00800060, 0x00800001, 0x01801235, 7], np.uint32), size=(256, 256))
        back = ev.unpack_page(ev.pack_page(page), 256, 256)
        self.assertEqual(back.dtype, np.uint32)
        np.testing.assert_array_equal(back, page)

    def test_a_page_with_many_values_uses_two_byte_indexes(self):
        page = np.arange(256 * 256, dtype=np.uint32).reshape(256, 256) % 1000 + 0xFFFF0000
        blob = ev.pack_page(page)
        np.testing.assert_array_equal(ev.unpack_page(blob, 256, 256), page)
        raw = zlib.decompress(blob)
        self.assertEqual(len(raw), 4 + 4 * 1000 + 2 * 256 * 256)

    def test_pages_on_the_edges_keep_their_size(self):
        page = np.full((37, 100), 5, np.uint32)
        page[10:20, 30:60] = 9
        np.testing.assert_array_equal(ev.unpack_page(ev.pack_page(page), 37, 100), page)

    def test_a_window_is_put_together_from_its_pages(self):
        full = np.arange(600 * 520, dtype=np.uint32).reshape(600, 520)
        pages = {(pc, pr): full[pr * 256:(pr + 1) * 256, pc * 256:(pc + 1) * 256]
                 for pr in range(3) for pc in range(3) if (pc, pr) != (1, 1)}
        got = ev.window(pages, 77, 200, 530, 240, 515)
        expected = full[200:530, 240:515].copy()
        expected[256 - 200:512 - 200, 256 - 240:512 - 240] = 77     # page (1, 1) has no row: default
        np.testing.assert_array_equal(got, expected)


def stored_grid(results):
    """The map's grid as maps.upload stores it (built from the 5-colour map)."""
    path = layers.class_map(results)
    with rasterio.open(path) as src:
        grid = common.Grid.from_dataset(src, "test")
        counts, fingerprint = common.scan_map(src, grid)
    return common.Grid(grid.map_version, grid.x0, grid.y0, grid.pixel_w, grid.pixel_h, grid.width,
                       grid.height, grid.srid, grid.geographic, int(np.argmax(counts)), fingerprint,
                       schema="map_test")


def grid_row(grid):
    return (grid.map_version, grid.x0, grid.y0, grid.pixel_w, grid.pixel_h, grid.width, grid.height,
            grid.srid, grid.geographic, grid.default_class, grid.fingerprint)


def map_answers(grid):
    """What load_grid needs to find the map 'test' in a fake database."""
    return [("'public.maps'", [(True,)]), ("ORDER BY lower(name)", [("test", "map_test")]),
            ("FROM map_test.grid_info", [grid_row(grid)])]


def build_into_fake(results, grid):
    """evidence_approach.build into a fake database: returns (connection, page rows, info dict)."""
    conn = Script(map_answers(grid))
    quietly(ev.build, conn, results, "test", tidy=False)
    rows = next(r for sql, r in conn.copied.items() if "evidence_pages" in sql)
    i = next(k for k, sql in enumerate(conn.sql) if sql.startswith("INSERT INTO map_test.evidence_info"))
    columns = conn.sql[i].split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
    info = {c: getattr(v, "obj", v) for c, v in zip(columns, conn.params[i])}
    return conn, rows, info


def neon(rows, info):
    """A fake database holding the evidence pages: answers the queries check and verify send."""
    by_page = {(pc, pr): blob for pc, pr, blob in rows}

    def pages(sql, params):
        return [(pc, pr, by_page[(pc, pr)]) for pc, pr in zip(*params) if (pc, pr) in by_page]

    def page_row(sql, params):
        return [(pc, blob) for (pc, pr), blob in sorted(by_page.items()) if pr == params[0]]

    return Script([("to_regclass", [(True,)]),
                   ("FROM map_test.evidence_info", [(info["default_value"], info["rule_params"],
                                                     info["bit_layout"], info["fingerprint"])]),
                   ("unnest(", pages), ("WHERE page_row = %s", page_row)])


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = built_results()
        cls.grid = stored_grid(cls.results)
        cls.conn, cls.rows, cls.info = build_into_fake(cls.results, cls.grid)
        with rasterio.open(layers.evidence_file(cls.results)) as src:
            cls.evidence, cls.tags = src.read(1), src.tags()

    def test_every_page_gets_a_row_unless_it_is_all_the_most_common_value(self):
        values, counts = np.unique(self.evidence, return_counts=True)
        default = int(values[np.argmax(counts)])
        self.assertEqual(self.info["default_value"], default)
        expected = {(c // 256, r // 256) for r in range(0, self.grid.height, 256)
                    for c in range(0, self.grid.width, 256)
                    if not (self.evidence[r:r + 256, c:c + 256] == default).all()}
        self.assertEqual({(pc, pr) for pc, pr, _ in self.rows}, expected)
        self.assertLess(len(expected), 9)          # the plain corner's page is left out

    def test_the_pages_hold_every_pixel(self):
        default = self.info["default_value"]
        rebuilt = np.full(self.evidence.shape, default, np.uint32)
        for pc, pr, blob in self.rows:
            h, w = min(256, self.grid.height - pr * 256), min(256, self.grid.width - pc * 256)
            rebuilt[pr * 256:pr * 256 + h, pc * 256:pc * 256 + w] = ev.unpack_page(blob, h, w)
        np.testing.assert_array_equal(rebuilt, self.evidence)

    def test_the_rule_and_bit_layout_go_with_the_pages(self):
        self.assertEqual(self.info["rule_params"], json.loads(self.tags["RULE_PARAMS"]))
        self.assertEqual(self.info["bit_layout"], json.loads(self.tags["BIT_LAYOUT"]))
        self.assertEqual(self.info["source_file"], os.path.basename(layers.evidence_file(self.results)))

    def test_the_tables_live_in_the_maps_schema_with_a_primary_key(self):
        sql = "\n".join(self.conn.sql)
        self.assertIn("CREATE TABLE map_test.evidence_pages", sql)
        self.assertIn("ALTER TABLE map_test.evidence_pages ADD PRIMARY KEY (page_row, page_col)", sql)
        self.assertEqual(self.conn.commits, 0)                  # tidy=False: maps.upload commits

    def test_evidence_of_another_map_is_refused(self):
        other = common.Grid("test", self.grid.x0 + 1, self.grid.y0, self.grid.pixel_w, self.grid.pixel_h,
                            self.grid.width, self.grid.height, 4326, True, self.grid.default_class,
                            self.grid.fingerprint, schema="map_test")
        with self.assertRaises(SystemExit) as cm:
            quietly(ev.build, Script(map_answers(other)), self.results, "test", tidy=False)
        self.assertIn("not the map 'test'", str(cm.exception.code))

    def test_a_results_folder_with_other_colours_is_refused(self):
        changed = common.Grid(*grid_row(self.grid)[:10], "another fingerprint", schema="map_test")
        with self.assertRaises(SystemExit) as cm:
            quietly(ev.build, Script(map_answers(changed)), self.results, "test", tidy=False)
        self.assertIn("not the one the map 'test' was uploaded from", str(cm.exception.code))


class CheckTest(unittest.TestCase):
    """A check against the pages in the database gives every plot exactly what eudr_layers.py gives it
    from evidence.tif: the same 35 outputs (verdict, reason, every hectare figure, the years)."""

    @classmethod
    def setUpClass(cls):
        cls.results = built_results()
        cls.grid = stored_grid(cls.results)
        _, cls.rows, cls.info = build_into_fake(cls.results, cls.grid)
        with open(os.path.join(cls.results, "test_plots.geojson"), encoding="utf-8") as fh:
            features = json.load(fh)["features"]
        g = layers.joe_grid(cls.grid)
        x_mid, y_mid = g.x0 + g.dx * g.width / 2, g.y0 + g.dy * g.height / 2
        x_q, y_q = g.x0 + g.dx * g.width * 0.92, g.y0 + g.dy * g.height * 0.92   # the plain corner
        features += [  # outside the map; smaller than a pixel; a point of a declared size; on the edge;
                       # on a page that has no row (all the most common value)
            {"type": "Feature", "properties": {"ProductionPlace": "QUIET"}, "geometry": {
                "type": "Polygon", "coordinates": [[[x_q, y_q], [x_q + 20 * g.dx, y_q],
                                                    [x_q + 20 * g.dx, y_q + 20 * g.dy], [x_q, y_q]]]}},
            {"type": "Feature", "properties": {"ProductionPlace": "OUTSIDE"},
             "geometry": {"type": "Point", "coordinates": [g.x0 - 1.0, g.y0]}},
            {"type": "Feature", "properties": {"ProductionPlace": "TINY"}, "geometry": {
                "type": "Polygon", "coordinates": [[[x_mid, y_mid], [x_mid + g.dx / 4, y_mid],
                                                    [x_mid + g.dx / 4, y_mid + g.dy / 4], [x_mid, y_mid]]]}},
            {"type": "Feature", "properties": {"ProductionPlace": "POINT", "Area": 3.0},
             "geometry": {"type": "Point", "coordinates": [x_mid, y_mid]}},
            {"type": "Feature", "properties": {"ProductionPlace": "EDGE"}, "geometry": {
                "type": "Polygon", "coordinates": [[[g.x0 - 0.001, y_mid], [g.x0 + 0.002, y_mid],
                                                    [g.x0 + 0.002, y_mid + 0.002], [g.x0 - 0.001, y_mid]]]}},
        ]
        cls.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(cls.tmp.name, "plots.geojson")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": features}, fh)
        cls.plots, _ = ev.read_plots(path)
        rule = layers.rule_from(cls.info["rule_params"])
        cls.expected, _ = el.query_plots(cls.plots, layers.evidence_file(cls.results), g,
                                         el.row_areas_m2(g), rule)
        cls.conn = neon(cls.rows, cls.info)
        cls.got, cls.timings = ev.check(cls.conn, cls.grid, cls.plots)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_output_of_every_plot_matches_eudr_layers_on_evidence_tif(self):
        self.assertEqual(len(self.got), len(self.expected))
        for got, expected in zip(self.got, self.expected):
            self.assertEqual(el.compare_outputs(got, expected), [], got["plot_id"])

    def test_the_made_up_plots_reach_every_verdict(self):
        self.assertEqual({r["verdict"] for r in self.got},
                         {"non-compliant", "probable deforestation", "needs review", "deforestation-free",
                          "not assessed"})

    def test_one_query_fetches_every_page_the_plots_need(self):
        page_queries = [p for s, p in zip(self.conn.sql, self.conn.params) if "unnest(" in s]
        self.assertEqual(len(page_queries), 1)
        self.assertEqual(self.conn.round_trips(), 3)              # tables there? / rule / pages
        g = layers.joe_grid(self.grid)
        needed = set()
        for geom in self.plots.geometry:
            r0, r1, c0, c1 = el.plot_window(geom, g)
            if r1 > r0 and c1 > c0:
                needed |= {(pc, pr) for pr in range(r0 // 256, (r1 - 1) // 256 + 1)
                           for pc in range(c0 // 256, (c1 - 1) // 256 + 1)}
        self.assertEqual(set(zip(*page_queries[0])), needed)

    def test_the_timings_say_how_long_and_how_much(self):
        self.assertGreater(self.timings["total_s"], 0)
        self.assertEqual(self.timings["pages"], len(set(zip(*[p for s, p in zip(self.conn.sql, self.conn.params)
                                                               if "unnest(" in s][0]))
                                                    & {(pc, pr) for pc, pr, _ in self.rows}))

    def test_plots_get_their_production_place_as_id(self):
        self.assertEqual([r["plot_id"] for r in self.got[-5:]], ["QUIET", "OUTSIDE", "TINY", "POINT", "EDGE"])
        self.assertEqual(self.got[0]["plot_id"], "TEST-00001")

    def test_a_plot_on_a_page_with_no_row_gets_the_most_common_value(self):
        quiet = next(r for r in self.got if r["plot_id"] == "QUIET")
        self.assertEqual((quiet["verdict"], quiet["forest_2020_ha"]), ("deforestation-free", 0.0))
        self.assertGreater(quiet["pixels"], 0)

    def test_a_map_without_evidence_says_how_to_add_it(self):
        with self.assertRaises(SystemExit) as cm:
            ev.check(Script([("to_regclass", [(False,)])]), self.grid, self.plots)
        self.assertIn("pipeline.py", str(cm.exception.code))


class VerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = built_results()
        cls.grid = stored_grid(cls.results)
        _, cls.rows, cls.info = build_into_fake(cls.results, cls.grid)

    def test_the_whole_evidence_comes_back_bit_for_bit(self):
        stats = quietly(ev.verify, neon(self.rows, self.info), self.grid, self.results)
        self.assertEqual((stats["mismatched"], stats["pixels"]), (0, self.grid.width * self.grid.height))
        self.assertTrue(stats["same_map"])

    def test_a_damaged_page_is_noticed(self):
        rows = list(self.rows)
        pc, pr, blob = rows[0]
        h, w = min(256, self.grid.height - pr * 256), min(256, self.grid.width - pc * 256)
        page = ev.unpack_page(blob, h, w).copy()
        page[0, :10] ^= np.uint32(1)                              # flip the forest bit of 10 pixels
        rows[0] = (pc, pr, ev.pack_page(page))
        stats = quietly(ev.verify, neon(rows, self.info), self.grid, self.results)
        self.assertEqual(stats["mismatched"], 10)


class ReadPlotsTest(unittest.TestCase):
    def test_ids_are_the_same_ones_the_stencils_and_outlines_use(self):
        features = [{"type": "Feature", "properties": {"plot_id": "A"},
                     "geometry": {"type": "Point", "coordinates": [110.01, -7.01]}},
                    {"type": "Feature", "properties": {},
                     "geometry": {"type": "Point", "coordinates": [110.02, -7.02]}},
                    {"type": "Feature", "properties": {"plot_id": "A"},
                     "geometry": {"type": "Point", "coordinates": [110.03, -7.03]}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "farms.geojson")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection", "features": features}, fh)
            plots, raw = ev.read_plots(path)
            grid = common.Grid("t", 110.0, -7.0, 1 / 12000, 1 / 12000, 600, 600, 4326, True)
            farms = quietly(common.farms_from_features, raw, grid)
        self.assertEqual(list(plots["plot_id"]), [f.plot_id for f in farms])
        self.assertEqual(list(plots["plot_id"]), ["A", "plot-2", "A-2"])
        self.assertEqual(plots.geometry.iloc[0].geom_type, "Polygon")   # a point is a circle of its area


if __name__ == "__main__":
    unittest.main()
