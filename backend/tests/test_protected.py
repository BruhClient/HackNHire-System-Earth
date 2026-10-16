"""Protected areas (protected_areas.py): wdpa*.geojson files found by name, cut to the map, stored
with it, and read back for the app. Made-up areas in a temporary folder; a fake connection."""

import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
