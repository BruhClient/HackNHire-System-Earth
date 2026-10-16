"""Protected areas (protected_areas.py): wdpa*.geojson files found by name, cut to the map, stored
with it, and read back for the app. Made-up areas in a temporary folder; a fake connection."""

import json
import os
import tempfile
import unittest

import shapely
from shapely.geometry import shape

import common
import protected_areas
from tests.fakes import Script, quietly

# a map of 0.1 x 0.1 degrees, 800 x 800 pixels of 1/8000 degree, top-left at (103.0, -1.0)
GRID = common.Grid("jambi", 103.0, -1.0, 1 / 8000, 1 / 8000, 800, 800, 4326, True, schema="map_jambi")


def square(x0, y0, size):
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + size, y0], [x0 + size, y0 + size],
                                                [x0, y0 + size], [x0, y0]]]}


def feature(geometry, **props):
    return {"type": "Feature", "properties": props, "geometry": geometry}


class ProtectedAreasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, features, folder=""):
        path = os.path.join(self.tmp.name, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": features}, fh)
        return path

    def test_files_are_found_by_a_name_starting_with_wdpa_in_sub_folders_too(self):
        wanted = [self.write("WDPA_Oct2026_IDN-polygons.geojson", []),
                  self.write("wdpa_extra.geojson", [], folder="vectors")]
        self.write("farms.geojson", [])
        self.write("my_wdpa.geojson", [])
        self.write("wdpa_notes.txt", [])
        self.assertEqual(protected_areas.find_files(self.tmp.name), sorted(wanted))
        self.assertEqual(protected_areas.find_files(None), [])

    def test_areas_are_cut_to_the_map_and_points_and_areas_elsewhere_are_left_out(self):
        path = self.write("wdpa.geojson", [
            feature(square(103.01, -1.05, 0.02), WDPAID=1, WDPA_PID="1", NAME="Inside", DESIG_ENG="National Park",
                    IUCN_CAT="II"),
            feature(square(103.09, -1.05, 0.05), WDPA_PID="2", NAME="Half in"),
            feature(square(104.0, -1.05, 0.02), WDPA_PID="3", NAME="Elsewhere"),
            feature({"type": "Point", "coordinates": [103.05, -1.05]}, WDPA_PID="4", NAME="A point"),
        ])
        areas = quietly(protected_areas.read, [path], GRID)
        self.assertEqual([a.name for a in areas], ["Inside", "Half in"])
        inside, half = areas
        self.assertEqual((inside.wdpa_id, inside.designation, inside.iucn_cat), ("1", "National Park", "II"))
        self.assertEqual(inside.geom.geom_type, "MultiPolygon")
        self.assertAlmostEqual(inside.geom.area, 0.02 ** 2)
        self.assertAlmostEqual(half.geom.bounds[2], 103.1)      # cut at the map's east edge
        self.assertAlmostEqual(half.geom.area, 0.01 * 0.05)

    def test_an_area_in_two_files_is_kept_once(self):
        first = self.write("wdpa_a.geojson", [feature(square(103.01, -1.05, 0.02), WDPA_PID="7", NAME="Berbak")])
        second = self.write("wdpa_b.geojson", [feature(square(103.01, -1.05, 0.02), WDPA_PID="7", NAME="Berbak"),
                                               feature(square(103.05, -1.05, 0.01), NAME="No id")])
        areas = quietly(protected_areas.read, [first, second], GRID)
        self.assertEqual([a.name for a in areas], ["Berbak", "No id"])

    def test_merge_and_the_cut_copy_keep_the_features(self):
        a = self.write("wdpa_a.geojson", [feature(square(103.01, -1.05, 0.02), WDPA_PID="1")])
        b = self.write("wdpa_b.geojson", [feature(square(103.09, -1.05, 0.05), WDPA_PID="2")])
        merged = os.path.join(self.tmp.name, "out", "wdpa_merged.geojson")
        protected_areas.merge([a, b], merged)
        self.assertEqual(len(common.read_features(merged)), 2)
        copy = os.path.join(self.tmp.name, "out", protected_areas.FOLDER_COPY)
        protected_areas.write_geojson(quietly(protected_areas.read, [merged], GRID), GRID, copy)
        again = quietly(protected_areas.read, [copy], GRID)
        self.assertEqual([x.wdpa_id for x in again], ["1", "2"])
        self.assertAlmostEqual(again[1].geom.area, 0.01 * 0.05)

    def test_build_stores_the_areas_in_the_maps_schema_inside_the_callers_transaction(self):
        path = self.write("wdpa.geojson", [feature(square(103.01, -1.05, 0.02), WDPA_PID="1", NAME="Inside")])
        conn = Script()
        quietly(protected_areas.build, conn, GRID, quietly(protected_areas.read, [path], GRID))
        sql = "\n".join(conn.sql)
        self.assertIn("CREATE TABLE map_jambi.protected_areas", sql)
        self.assertIn("geometry(MultiPolygon, 4326)", sql)
        self.assertIn("USING GIST", sql)
        (copied,) = conn.copied.values()
        self.assertEqual(copied[0][:4], ("1", "Inside", None, None))
        self.assertEqual(json.loads(copied[0][4]), {"WDPA_PID": "1", "NAME": "Inside"})
        self.assertEqual(conn.commits, 0)

    def test_load_gives_the_app_geojson_or_none_for_a_map_without_areas(self):
        without = Script([("to_regclass", [(False,)])])
        self.assertIsNone(protected_areas.load(without, GRID))
        grid = common.Grid("jambi", 103.0, -1.0, 1 / 8000, 1 / 8000, 800, 800, 4326, True, schema="map_jambi",
                           tables=frozenset({"protected_areas"}))
        conn = Script([("ST_AsGeoJSON", [("1", "Berbak", "National Park", "II",
                                          json.dumps(square(103.01, -1.05, 0.02)))])])
        fc = protected_areas.load(conn, grid)
        self.assertEqual(len(conn.sql), 1)                     # one round trip
        self.assertEqual(fc["features"][0]["properties"],
                         {"wdpa_id": "1", "name": "Berbak", "designation": "National Park", "iucn_cat": "II"})
        self.assertEqual(fc["features"][0]["geometry"]["type"], "Polygon")


WITH_AREAS = common.Grid("jambi", 103.0, -1.0, 1 / 8000, 1 / 8000, 800, 800, 4326, True, schema="map_jambi",
                         tables=frozenset({"protected_areas"}))


def farm(plot_id, geom):
    return common.Farm(plot_id, {}, geom, "point" if geom.geom_type == "Point" else "outline")


class ProtectedCheckTest(unittest.TestCase):
    """A farm in a protected area needs review: check() finds those farms, apply() marks them."""

    def setUp(self):
        from shapely.geometry import Point
        self.farms = [farm("in", Point(103.02, -1.04)),
                      farm("half", shape(square(103.01, -1.05, 0.002))),
                      farm("edge", shape(square(103.03, -1.05, 0.002))),
                      farm("away", Point(103.08, -1.08))]
        inside = shapely.to_wkb(shape(square(103.01, -1.05, 0.001)))   # a quarter of "half"
        sliver = shapely.to_wkb(shape(square(103.03, -1.05, 1e-9)))    # "edge": next to nothing
        self.conn = Script([("ST_Intersects(p.geom, f.geom)",
                             [(0, ["Berbak"], None), (1, ["Berbak", "WDPA 9"], inside), (2, ["Berbak"], sliver)])])

    def test_a_map_without_protected_areas_sends_no_query(self):
        conn = Script()
        self.assertEqual(protected_areas.check(conn, GRID, self.farms), {})
        self.assertEqual(conn.sql, [])

    def test_one_query_finds_points_inside_and_outlines_overlapping_by_more_than_an_edge(self):
        found = protected_areas.check(self.conn, WITH_AREAS, self.farms)
        self.assertEqual(len(self.conn.sql), 1)                    # one round trip for every farm
        self.assertIn("map_jambi.protected_areas", self.conn.sql[0])
        self.assertEqual(sorted(found), [0, 1])                    # "edge" only touches; "away" is outside
        self.assertEqual(found[0], protected_areas.Overlap(("Berbak",), 0.0))
        self.assertEqual(found[1].names, ("Berbak", "WDPA 9"))
        quarter = common.area_m2(self.farms[1].geom, WITH_AREAS) / 4
        self.assertAlmostEqual(found[1].m2, quarter, delta=quarter * 1e-6)

    def test_apply_sends_lesser_verdicts_for_review_and_keeps_deforestation(self):
        found = protected_areas.check(self.conn, WITH_AREAS, self.farms)
        results = [common.PlotResult(f.plot_id, f.kind, verdict=v) for f, v in
                   zip(self.farms, (common.PASS, common.FAIL, common.PASS, common.CHECK_LOCATION))]
        protected_areas.apply(results, found)
        self.assertEqual([r.verdict for r in results],
                         [common.MANUAL_REVIEW, common.FAIL, common.PASS, common.CHECK_LOCATION])
        self.assertEqual([r.outcome for r in results[:2]], [common.NEEDS_REVIEW, common.NON_COMPLIANT])
        self.assertEqual((results[0].map_verdict, results[1].map_verdict, results[2].map_verdict),
                         (common.PASS, common.FAIL, None))
        self.assertTrue(results[0].in_protected)
        self.assertFalse(results[2].in_protected)
        self.assertEqual(results[0].note, "in a protected area: Berbak")
        self.assertAlmostEqual(results[1].protected_ha, found[1].m2 / 10_000)


class EvidenceOutcomeTest(unittest.TestCase):
    """Evidence checks keep Joe's verdict; only the outcome counts protected areas."""

    def test_deforested_fails_protected_needs_review_otherwise_compliant(self):
        import evidence_approach as ev
        free, cleared = {"verdict": "deforestation-free"}, {"verdict": "non-compliant"}
        self.assertEqual(ev.outcome(cleared, ("Berbak",)), common.NON_COMPLIANT)
        self.assertEqual(ev.outcome(free, ("Berbak",)), common.NEEDS_REVIEW)
        self.assertEqual(ev.outcome(free), common.COMPLIANT)
        self.assertEqual(ev.outcome({"verdict": "needs review"}), common.NEEDS_REVIEW)

    def test_protected_plots_uses_the_stored_areas_or_else_the_evidence_fact(self):
        import geopandas as gpd
        import evidence_approach as ev
        plots = gpd.GeoDataFrame({"plot_id": ["a", "b", "c"]}, crs="EPSG:4326", geometry=[
            shape(square(103.01, -1.05, 0.002)), shape(square(103.05, -1.05, 0.002)),
            shape(square(103.07, -1.05, 0.002))])
        rows = [{"protected_ha": 0.0}, {"protected_ha": 1.5}, {"protected_ha": ""}]   # "": screened
        inside = shapely.to_wkb(shape(square(103.01, -1.05, 0.002)))
        conn = Script([("ST_Intersects(p.geom, f.geom)", [(0, ["Berbak"], inside)])])
        self.assertEqual(ev.protected_plots(conn, WITH_AREAS, plots, rows),
                         [("Berbak",), (ev.FROM_EVIDENCE,), ()])
        self.assertEqual(len(conn.sql), 1)


if __name__ == "__main__":
    unittest.main()
