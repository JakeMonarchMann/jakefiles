import tempfile
import unittest
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd

from plottin.LensNudgeResponse import lens_nudge_response as plotter


def training_log(stage: str) -> str:
    lines = [f"AIM TRAIN — learning how the FF beam moves with {stage}_Y, {stage}_Z"]
    for y, z in [(0, 0), (.005, 0), (-.005, 0), (0, 0),
                 (0, .005), (0, -.005), (0, 0)]:
        cx, cy = -1 - 28 * y + .2 * z, 1 - .4 * y + 25 * z
        lines.append(f"  rest: {stage}_Y{y:+g}  {stage}_Z{z:+g}"
                     f" -> centroid ({cx:+.6f}, {cy:+.6f}) mm")
    return "\n".join(lines + ["AIM TRAIN done — 7 samples, fit R^2 camX=1 camY=1"])


class NudgeResponseTests(unittest.TestCase):
    def test_two_stages_and_signed_cross_axis_fit(self):
        samples = plotter.calibration_samples(training_log("HEXSTAGE") + "\n" +
                                              training_log("HEXAPOD"))
        self.assertEqual(len(samples), 14)
        fits = plotter.fit_response(samples)
        self.assertEqual(len(fits), 4)
        np.testing.assert_allclose(fits["d_centroid_d_Y_um_per_um"], [-28, -.4] * 2)
        np.testing.assert_allclose(fits["d_centroid_d_Z_um_per_um"], [.2, 25] * 2)
        np.testing.assert_allclose(fits["r_squared"], 1)

    def test_incomplete_missing_and_wrong_count_are_errors(self):
        for text in ["", training_log("HEXSTAGE").split("AIM TRAIN done")[0],
                     training_log("HEXSTAGE").replace("7 samples", "8 samples")]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                plotter.calibration_samples(text)

    def test_rank_deficient_training_is_error(self):
        samples = plotter.calibration_samples(training_log("HEXSTAGE"))
        samples["z_um"] = 0
        with self.assertRaisesRegex(ValueError, "independently excite"):
            plotter.fit_response(samples)

    def test_cli_outputs_and_unique_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv = root / "run_data_20261017_185456.csv"
            data = pd.DataFrame({col: np.zeros(6) for col in plotter.USECOLS})
            data["time_elapsed_s"] = np.arange(6)
            data["ff_frame_seq"] = [1, 1, 2, 2, 3, 3]
            data["ff_beam_found"] = 1
            data["script_cmd"] = ["AIM TRAIN"] * 3 + ["DWELL 250"] * 3
            data.to_csv(csv, index=False)
            console = root / "run_console_log_20261017_185456.txt"
            console.write_text(training_log("HEXSTAGE"), encoding="utf-8")
            self.assertEqual(plotter.main([str(root), "--no-show"]), 0)
            out = root / "plots"
            self.assertEqual(len(list(out.glob("*.png"))), 2)
            for image in out.glob("*.png"):
                self.assertGreater(image.stat().st_size, 10000)
            fits = pd.read_csv(out / f"{csv.stem}_response_fits.csv")
            self.assertEqual(len(fits), 2)
            history = plotter.load_history(csv)
            fig = plotter.plot_history(history, "test")
            self.assertEqual(len(fig.axes[1].lines[0].get_xdata()), 3)
            plotter.plt.close(fig)
            data.loc[0, "ff_div_x_mrad"] = np.nan
            data.to_csv(csv, index=False)
            with self.assertRaisesRegex(ValueError, "missing or non-finite"):
                plotter.load_history(csv)

    def test_ambiguous_directory_and_missing_columns_are_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                plotter.resolve_inputs(root)
            csv = root / "run_data_123.csv"
            csv.write_text("time_elapsed_s\n0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required columns"):
                plotter.load_history(csv)


if __name__ == "__main__":
    unittest.main()
