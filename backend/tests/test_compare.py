import os
import unittest

import compare
import layers
from tests.fakes import built_results


class StorageChainTest(unittest.TestCase):
    """From the original layers to Neon, one row per form the map takes, biggest first."""

    @classmethod
    def setUpClass(cls):
        cls.results = built_results()
        layers.class_map(cls.results)
        cls.summary = {"sizes_bytes": {"inputs_raw_for_bbox": 5_800_000, "inputs_clipped_deflate": 98_500}}
        cls.db = {"evidence": 41_000, "stencils": 12_000, "outlines": 230_000, "grid_info": 16_384}
        cls.rows = compare.storage_rows(cls.summary, cls.results, cls.db)

    def test_every_form_is_there_in_order(self):
        self.assertEqual([label for label, _, _ in self.rows], [
            "original layers for the box, uncompressed", "original layers, clipped + DEFLATE",
            "evidence.tif (every fact, 32 bits a pixel)", "outcome.tif (outcome classes with years)",
            "outcome in 5 colours (the TIFF the stencils and outlines are built from)",
            "Neon: evidence pages", "Neon: stencils (5 colours)", "Neon: outlines (5 colours)"])

    def test_files_are_measured_and_tables_include_the_shared_grid_info(self):
        sizes = {label: size for label, size, _ in self.rows}
        self.assertEqual(sizes["evidence.tif (every fact, 32 bits a pixel)"],
                         os.path.getsize(os.path.join(self.results, "evidence.tif")))
        self.assertEqual(sizes["Neon: evidence pages"], 41_000 + 16_384)
        self.assertEqual(sizes["Neon: stencils (5 colours)"], 12_000 + 16_384)

    def test_each_form_is_compared_with_the_original_layers(self):
        notes = {label: note for label, _, note in self.rows}
        self.assertEqual(notes["original layers, clipped + DEFLATE"], "1.7% of the uncompressed layers")
        self.assertIn("% of the uncompressed layers", notes["Neon: evidence pages"])

    def test_without_a_summary_the_original_layers_are_left_out(self):
        rows = compare.storage_rows(None, self.results, self.db)
        self.assertFalse(any(label.startswith("original") for label, _, _ in rows))
        self.assertEqual(rows[0][2], "")


class EvidenceFidelityTest(unittest.TestCase):
    def test_plots_are_identical_only_if_every_output_is(self):
        a = [{"plot_id": "1", "verdict": "needs review", "review_ha": 0.5},
             {"plot_id": "2", "verdict": "deforestation-free", "review_ha": 0.0},
             {"plot_id": "3", "verdict": "non-compliant", "review_ha": 0.0}]
        b = [dict(a[0]), dict(a[1], review_ha=0.25), dict(a[2], verdict="needs review")]
        f = compare.evidence_fidelity(a, b)
        self.assertEqual((f["plots"], f["identical"], f["same_verdict"]), (3, 1, 2))
        self.assertEqual(f["differ"], [("2", ["review_ha"]), ("3", ["verdict"])])


if __name__ == "__main__":
    unittest.main()
