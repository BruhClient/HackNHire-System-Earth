import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import rasterio

import common
import eudr_layers as el
import layers
from tests.fakes import built_results


class FiveColoursTest(unittest.TestCase):
    """The compaction step's outcome classes -> the 5 colours the stencils and outlines store, as in
    Joe's own 5-colour map (eudr_layers.py 1.4: 0 land, 1 water, 2 forest, 3 deforestation, 4 needs
    review, 255 no data)."""

    def test_every_outcome_class_gets_its_colour(self):
        expected = {el.NODATA: layers.NO_DATA_VALUE, el.WATER: common.WATER, el.NOT_FOREST: common.LAND,
                    el.FOREST: common.FOREST, el.FOREST_RUBBER: common.FOREST,
                    el.CLEARED_NON_FOREST: common.LAND, el.DEGRADED: common.FOREST}
        for year in range(1, 8):
            expected[el.CONFIRMED + year] = common.DEFORESTATION
            expected[el.PROBABLE + year] = common.REVIEW
            expected[el.REVIEW + year] = common.REVIEW
        codes = np.array(sorted(expected), np.uint8)
        got = layers.five_classes(codes)
        self.assertEqual(got.dtype, np.uint8)
        self.assertEqual(dict(zip(codes.tolist(), got.tolist())), expected)

    def test_no_data_reads_back_as_the_no_data_colour(self):
        self.assertEqual(common.normalise(layers.five_classes(np.array([el.NODATA], np.uint8)))[0],
                         common.NO_DATA)


class ClassMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = built_results()
        cls.path = layers.class_map(cls.results)

    def test_the_class_map_sits_next_to_the_outcome(self):
        self.assertEqual(self.path, os.path.join(self.results, layers.CLASS_MAP))

    def test_it_has_the_outcomes_grid_and_its_classes_in_five_colours(self):
        with rasterio.open(os.path.join(self.results, "outcome.tif")) as src:
            outcome, transform, crs = src.read(1), src.transform, src.crs
        with rasterio.open(self.path) as src:
            self.assertEqual((src.transform, src.crs), (transform, crs))
            self.assertEqual(src.nodata, layers.NO_DATA_VALUE)
            np.testing.assert_array_equal(src.read(1), layers.five_classes(outcome))

    def test_it_says_what_it_is_and_where_it_comes_from(self):
        with rasterio.open(self.path) as src:
            tags = src.tags()
        self.assertIn("RULE_PARAMS", tags)
        self.assertEqual(json.loads(tags["DERIVED_FROM"]), ["outcome.tif"])
        self.assertEqual(sorted(json.loads(tags["LEGEND"])), ["0", "1", "2", "255", "3", "4"])

    def test_a_second_call_reuses_the_file(self):
        before = os.path.getmtime(self.path)
        self.assertEqual(layers.class_map(self.results), self.path)
        self.assertEqual(os.path.getmtime(self.path), before)

    def test_an_outcome_already_in_five_colours_is_used_as_it_is(self):
        """eudr_layers.py 1.4 and later write outcome.tif in the 5 colours themselves."""
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("evidence.tif", "outcome.tif"):
                shutil.copy(os.path.join(self.results, name), tmp)
            with rasterio.open(os.path.join(tmp, "outcome.tif"), "r+", IGNORE_COG_LAYOUT_BREAK="YES") as dst:
                dst.update_tags(OUTCOME_SCHEME="1.4: 0 land, 1 water, 2 forest, 3 deforestation, "
                                               "4 needs review, 255 no data")
            self.assertEqual(layers.class_map(tmp), os.path.join(tmp, "outcome.tif"))
            self.assertFalse(os.path.exists(os.path.join(tmp, layers.CLASS_MAP)))


class ResultsFolderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = built_results()

    def test_a_folder_or_one_of_its_files_names_the_folder(self):
        for given in (self.results, os.path.join(self.results, "evidence.tif"),
                      os.path.join(self.results, "outcome.tif")):
            self.assertEqual(layers.results_folder(given), os.path.abspath(self.results))

    def test_a_name_is_looked_up_in_the_layers_folder(self):
        parent, name = os.path.split(self.results)
        with mock.patch.object(common, "LAYERS_DIR", parent):
            self.assertEqual(layers.results_folder(name), os.path.abspath(self.results))

    def test_a_folder_without_evidence_says_how_to_make_one(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as cm:
            layers.results_folder(tmp)
        self.assertIn("pipeline.py", str(cm.exception.code))

    def test_a_missing_folder_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            layers.results_folder(os.path.join(self.results, "no such folder"))
        self.assertIn("Can't find", str(cm.exception.code))


class EvidenceTagsTest(unittest.TestCase):
    def test_the_rule_comes_from_the_rule_parameters(self):
        params = {"hansen_only": "settle", "settled_until": 2022, "tmf_last_year": 2025,
                  "alerts_after_year": 2025}
        self.assertEqual(layers.rule_from(params), ("settle", 2022, 2025))
        self.assertEqual(layers.rule_from({"hansen_only": "review", "tmf_last_year": 2025}),
                         ("review", None, None))

    def test_the_bit_layout_of_this_version_is_accepted(self):
        layers.check_layout(el.field_layout())
        layers.check_layout([name for name, _ in el.FIELDS])

    def test_another_bit_layout_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            layers.check_layout([{"field": "gfc_forest"}, {"field": "something_else"}])
        self.assertIn("bit layout", str(cm.exception.code))


class GridTest(unittest.TestCase):
    def test_the_database_grid_gives_eudr_layers_the_same_grid(self):
        results = built_results()
        with rasterio.open(os.path.join(results, "evidence.tif")) as src:
            t, width, height = src.transform, src.width, src.height
            grid = common.Grid.from_dataset(src, "test")
        g = layers.joe_grid(grid)
        self.assertEqual((g.x0, g.y0, g.dx, g.dy, g.width, g.height), (t.c, t.f, t.a, t.e, width, height))
        self.assertEqual(g.transform, t)


if __name__ == "__main__":
    unittest.main()
