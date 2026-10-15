import os
import tempfile
import unittest
from unittest import mock

import common
from tests.fakes import Script


class FoldersTest(unittest.TestCase):
    """There is no data/ folder: inputs are given by path, and built maps live in layers/<map>/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.layers = os.path.join(self.tmp.name, "layers")
        os.makedirs(os.path.join(self.layers, "jambi"))
        self.plots = os.path.join(self.layers, "jambi", "test_plots.geojson")
        with open(self.plots, "w") as fh:
            fh.write("{}")
        patcher = mock.patch.object(common, "LAYERS_DIR", self.layers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_app_looks_for_uploaded_tiffs_in_the_layers_folder(self):
        self.assertEqual(os.path.basename(common.TIFF_DIR), "layers")
        self.assertFalse(hasattr(common, "DATA_DIR"))

    def test_a_file_is_found_by_its_full_path_or_its_path_inside_layers(self):
        self.assertEqual(common.find_geojson(self.plots), self.plots)
        self.assertEqual(os.path.abspath(common.find_geojson(os.path.join("jambi", "test_plots.geojson"))),
                         self.plots)

    def test_a_missing_file_says_where_to_look(self):
        with self.assertRaises(SystemExit) as cm:
            common.find_geojson("farms.geojson")
        self.assertIn("layers", str(cm.exception.code))

    def test_files_inside_layers_are_stored_by_their_path_there(self):
        self.assertEqual(common.stored_path(os.path.join(self.layers, "jambi", "outcome_5class.tif")),
                         "jambi/outcome_5class.tif")
        self.assertEqual(common.stored_path(os.path.join(self.tmp.name, "elsewhere", "my_map.tif")),
                         "my_map.tif")

    def test_the_grid_records_the_path_inside_layers(self):
        grid = common.Grid("jambi", 103.0, -1.8, 1 / 12000, 1 / 12000, 6000, 6000, 4326, True,
                           schema="map_jambi")
        conn = Script()
        common.save_grid(conn, grid, os.path.join(self.layers, "jambi", "outcome_5class.tif"))
        self.assertIn("jambi/outcome_5class.tif", conn.params[1])
        self.assertIn("jambi/outcome_5class.tif", conn.params[2])


class FarmsTest(unittest.TestCase):
    """Plots in the EU Information System format (what eudr_layers.py writes as test plots)."""

    def test_production_place_names_the_plot_and_area_is_its_declared_size(self):
        grid = common.Grid("t", 110.0, -7.0, 1 / 12000, 1 / 12000, 600, 600, 4326, True)
        feature = {"type": "Feature", "properties": {"ProductionPlace": "TEST-00007", "Area": 2.5},
                   "geometry": {"type": "Point", "coordinates": [110.01, -7.01]}}
        farm = common.farms_from_features([feature], grid)[0]
        self.assertEqual((farm.plot_id, farm.declared_ha), ("TEST-00007", 2.5))


if __name__ == "__main__":
    unittest.main()
