import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

from plottin import plot_common as common


def figure_texts(fig) -> list[str]:
    texts = [ax.get_title() for ax in fig.axes]
    if fig._suptitle is not None:
        texts.append(fig._suptitle.get_text())
    return texts


class PlotCommonTests(unittest.TestCase):
    def test_run_time_from_trawler_stamp(self):
        path = Path("Lens Aperture_data_20261017_185456.csv")
        self.assertEqual(common.run_time(path), datetime(2026, 10, 17, 18, 54, 56))

    def test_run_time_falls_back_to_modified_time(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ["run_data_1.csv", "bad_data_20261399_250000.csv"]:
                path = Path(folder) / name
                path.write_text("x\n")
                os.utime(path, (0, datetime(2025, 3, 4, 5, 6, 7).timestamp()))
                self.assertEqual(common.run_time(path), datetime(2025, 3, 4, 5, 6, 7))

    def test_title_header_and_run_name(self):
        when = datetime(2026, 10, 17, 18, 54, 56)
        self.assertEqual(common.title_header("BG-CY0000-12", when), "BG-CY0000-12  |  Run 2026-10-17 18:54:56")
        self.assertEqual(common.title_header("", when), "Run 2026-10-17 18:54:56")
        self.assertEqual(common.run_name(Path("Hill Climb_data_20261017_185456.csv")), "Hill Climb")

    def test_get_title_uses_argument_else_prompt(self):
        with patch.object(common, "ask_title", Mock(side_effect=AssertionError("prompted"))):
            self.assertEqual(common.get_title("  unit A "), "unit A")
            self.assertEqual(common.get_title(""), "")
        with patch.object(common, "ask_title", Mock(return_value=None)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(common.get_title(None), "")
        self.assertIn("No title entered", out.getvalue())

    def test_input_path_prefers_argument_then_existing_default(self):
        with tempfile.TemporaryDirectory() as folder:
            default = Path(folder) / "default.csv"
            default.write_text("x\n")
            self.assertEqual(common.input_path("given.csv", str(default)), Path("given.csv"))
            self.assertEqual(common.input_path(None, str(default)), default)
            with patch.object(common, "pick_file", Mock(return_value=None)) as pick:
                self.assertIsNone(common.input_path(None, str(Path(folder) / "missing.csv")))
            pick.assert_called_once()


class DivergenceTitleTests(unittest.TestCase):
    def test_title_has_user_title_and_run_time(self):
        script = Path(__file__).parent / "Divergence in X" / "divergence_sweeps.py"
        spec = importlib.util.spec_from_file_location("divergence_sweeps", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        n = 40
        df = pd.DataFrame({
            "time_elapsed_s": np.arange(n, dtype=float),
            "script_cmd": ["HEXAPOD MOVEABS X(SweepStartX)"] + ["HEXAPOD MOVEINC X(Step)"] * (n - 2)
                          + ["HEXAPOD MOVEABS X(BestX)"],
            "hex_moving": 0, "hex_X_mm": 15.65 + np.arange(n) * 1e-4,
            "ff_div_x_mrad": 1 + (np.arange(n) - 20) ** 2 * 1e-3, "ff_div_y_mrad": 1.0,
            "ff_beam_found": 1,
        })
        texts = []
        original = Figure.savefig

        def capture(fig, *args, **kwargs):
            texts.extend(figure_texts(fig))
            return original(fig, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Gap close_data_20261020_101112.csv"
            df.to_csv(path, index=False)
            with patch.object(Figure, "savefig", capture), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main([str(path), "--title", "BG-CY0000-3", "--no-show"]), 0)
        self.assertTrue(any(t.startswith("BG-CY0000-3  |  Run 2026-10-20 10:11:12\nGap close")
                            for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
