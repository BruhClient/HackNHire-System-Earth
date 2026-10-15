import os
import tempfile
import unittest
from unittest import mock

import common
import pipeline
from tests.fakes import built_results, quietly


class PipelineTest(unittest.TestCase):
    """pipeline.py runs the compaction step into layers/<name>/, uploads the folder, then checks the
    plots against the database. Here the steps are stand-ins that record what they were given."""

    def setUp(self):
        self.calls, self.stored = [], []
        self.results = built_results()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        def compaction(argv):
            self.calls.append(("compaction", argv))
            out = argv[argv.index("--out-dir") + 1]
            os.makedirs(out, exist_ok=True)
            if "--test-plots" in argv:                    # as eudr_layers.py does
                with open(os.path.join(out, "test_plots.geojson"), "w") as fh:
                    fh.write('{"type": "FeatureCollection", "features": []}')

        def upload(conn, source, name, report=None):
            self.calls.append(("upload", source, name))

        def check_plots(out, name, plot_files, min_ha):
            self.calls.append(("check", out, name, plot_files, min_ha))
            return True

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        for target, attr, new in ((pipeline.eudr_layers, "main", compaction), (pipeline.maps, "upload", upload),
                                  (pipeline, "check_plots", check_plots),
                                  (pipeline.maps, "list_maps", lambda conn: [{"name": n} for n in self.stored]),
                                  (pipeline.common, "connect", Conn),
                                  (pipeline.common, "LAYERS_DIR", os.path.join(self.tmp.name, "layers"))):
            patcher = mock.patch.object(target, attr, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_pipeline(self, *argv):
        return quietly(pipeline.main, list(argv))

    def test_the_layers_are_built_into_the_maps_folder_then_uploaded_and_checked(self):
        out = os.path.join(common.LAYERS_DIR, "jambi_v1")
        code = self.run_pipeline("--name", "Jambi v1", "--data-dir", "D:\\inputs", "--bbox", "103", "-2.3",
                                 "103.5", "-1.8", "--test-plots", "100", "--verify")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls[0], ("compaction", ["--data-dir", "D:\\inputs", "--bbox", "103", "-2.3",
                                                        "103.5", "-1.8", "--test-plots", "100", "--verify",
                                                        "--out-dir", out]))
        self.assertEqual(self.calls[1], ("upload", out, "Jambi v1"))
        self.assertEqual(self.calls[2], ("check", out, "Jambi v1", [os.path.join(out, "test_plots.geojson")], 0.0))

    def test_the_plots_given_are_checked_too(self):
        self.run_pipeline("--name", "x", "--out-dir", self.results, "--skip-build", "--plots", "farms.geojson",
                          "--min-plot-ha", "0.1")
        self.assertEqual(self.calls[-1], ("check", self.results, "x",
                                          ["farms.geojson", os.path.join(self.results, "test_plots.geojson")], 0.1))

    def test_a_name_already_in_the_database_stops_before_the_long_build(self):
        self.stored = ["Jambi v1"]
        with self.assertRaises(SystemExit) as cm:
            self.run_pipeline("--name", "Jambi v1", "--data-dir", "D", "--bbox", "1", "2", "3", "4")
        self.assertIn("maps.py delete", str(cm.exception.code))
        self.assertEqual(self.calls, [])

    def test_skip_build_uploads_the_folder_already_there(self):
        self.run_pipeline("--name", "x", "--out-dir", self.results, "--skip-build")
        self.assertEqual([c[0] for c in self.calls], ["upload", "check"])

    def test_skip_build_needs_a_results_folder(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_pipeline("--name", "x", "--out-dir", os.path.dirname(self.results), "--skip-build")
        self.assertIn("isn't a results folder", str(cm.exception.code))

    def test_no_upload_stops_after_the_build(self):
        self.run_pipeline("--name", "x", "--data-dir", "D", "--bbox", "1", "2", "3", "4", "--no-upload")
        self.assertEqual([c[0] for c in self.calls], ["compaction"])

    def test_without_plots_nothing_is_checked(self):
        self.run_pipeline("--name", "x", "--data-dir", "D", "--bbox", "1", "2", "3", "4")
        self.assertEqual([c[0] for c in self.calls], ["compaction", "upload"])

    def test_a_name_that_cant_be_used_stops_at_once(self):
        with self.assertRaises(SystemExit):
            self.run_pipeline("--name", "???", "--data-dir", "D", "--bbox", "1", "2", "3", "4")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
