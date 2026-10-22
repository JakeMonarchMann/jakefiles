import tempfile
import unittest
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd

from plottin.HillClimbXSteps import hill_climb_x_steps as hc

X0 = 15.65
RECIPE = "CadX = 15.682\nGoalpostCadMm = 0.056\nFenceHalfMm = 0.020\nTotalCapMm = 0.050\n"


def make_run(n_climbs=3, seed=0):
    """Hill climbs at X0 - k um: settled readings around a peak, MOVEABS step and dwells between."""
    rng = np.random.default_rng(seed)
    rows, t = [], 0.0

    def add(cmd, x, y, z, v, moving=0):
        nonlocal t
        rows.append({"time_elapsed_s": t, "script_cmd": cmd, "hex_moving": moving, "hex_X_mm": x,
                     "hex_Y_mm": y, "hex_Z_mm": z, "oscope_voltage_V": v})
        t += 0.04

    for k in range(n_climbs):
        x = X0 - k * 0.001
        add("HEXAPOD MOVEABS X(XTarget)", x, 0, 0, 0.001, 1)
        add("DWELL XSettleMs", x, 0, 0, 0.001)
        peak_y, peak_z = 0.002 + 0.0005 * k, 0.003
        for step in range(12):
            y = peak_y * step / 11
            z = peak_z * step / 11
            volts = 0.005 + 0.001 * k - 0.0002 * (11 - step)
            for sample in range(6):
                add("OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE", x, y, z,
                    volts + rng.normal(0, 1e-5), moving=int(sample == 0))
        add("DWELL 300", x, peak_y, peak_z, 0.001)
    return pd.DataFrame(rows)


def write_run(directory: Path, df: pd.DataFrame, recipe: str | None = RECIPE) -> Path:
    csv = directory / "Hill Climb_data_20260101_000000.csv"
    df.to_csv(csv, index=False)
    if recipe is not None:
        (directory / "Hill Climb_20260101_000000.txt").write_text(recipe)
    return csv


class HillClimbTests(unittest.TestCase):
    def climbs(self, df, first_gap=24.0):
        return hc.build_climbs(hc.optimize_runs(hc.add_offsets(df)), first_gap)

    def test_one_climb_per_optimize_run_with_x_steps_and_gap(self):
        climbs = self.climbs(make_run(3))
        self.assertEqual(len(climbs), 3)
        np.testing.assert_allclose([c.x_um for c in climbs], [0, -1, -2], atol=0.01)
        np.testing.assert_allclose([c.gap_um for c in climbs], [24, 23, 22], atol=0.01)

    def test_peak_is_the_best_settled_point_not_a_noisy_sample(self):
        df = make_run(1)
        spike = df.index[df["script_cmd"].str.startswith("OPTIMIZE")][0]
        df.loc[spike, "oscope_voltage_V"] = 1.0  # one wild sample in a moving row at the start point
        climb = self.climbs(df)[0]
        self.assertLess(climb.peak["v_mV"], 10)
        self.assertAlmostEqual(climb.peak["y_um"], 2.0, places=1)
        self.assertAlmostEqual(climb.park["y_um"], 2.0, places=1)
        self.assertAlmostEqual(climb.start["y_um"], 0.0, places=1)

    def test_fence_follows_previous_park_and_is_cut_by_the_cap(self):
        climbs = self.climbs(make_run(2))
        params = {"FenceHalfMm": 0.020, "TotalCapMm": 0.050}
        first = hc.fence_box(climbs[0], None, params)
        self.assertEqual(first, (-20.0, 20.0, -20.0, 20.0))
        second = hc.fence_box(climbs[1], climbs[0], params)
        self.assertAlmostEqual(second[0], climbs[0].park["y_um"] - 20, places=6)
        capped = hc.fence_box(climbs[1], climbs[0], {"FenceHalfMm": 0.2, "TotalCapMm": 0.050})
        self.assertEqual(capped, (-50.0, 50.0, -50.0, 50.0))
        self.assertIsNone(hc.fence_box(climbs[0], None, {}))

    def test_recipe_values_read_first_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = write_run(Path(tmp), make_run(1), RECIPE + "FenceHalfMm = 9\n")
            values = hc.recipe_values(csv)
        self.assertEqual(values, {"CadX": 15.682, "GoalpostCadMm": 0.056,
                                  "FenceHalfMm": 0.02, "TotalCapMm": 0.05})

    def test_cli_saves_three_pngs_and_summary_with_recipe_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = write_run(Path(tmp), make_run(3))
            out = Path(tmp) / "out"
            self.assertEqual(hc.main([str(csv), "--out-dir", str(out), "--title", "unit", "--no-show"]), 0)
            names = sorted(p.name.split("_hill_climb_")[1] for p in out.iterdir())
            self.assertEqual(names, ["paths.png", "peak_vs_x.png", "summary.csv", "time_history.png"])
            summary = pd.read_csv(next(out.glob("*summary.csv")))
        self.assertEqual(len(summary), 3)
        self.assertAlmostEqual(summary["gap_um"].iloc[0], 24.0, places=1)

    def test_cli_without_recipe_still_plots(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv = write_run(Path(tmp), make_run(2), recipe=None)
            out = Path(tmp) / "out"
            self.assertEqual(hc.main([str(csv), "--out-dir", str(out), "--title", "unit", "--no-show"]), 0)
            self.assertEqual(len(list(out.glob("*.png"))), 3)

    def test_unterminated_last_line_and_bad_logs_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            csv = write_run(tmp, make_run(2))
            n = len(pd.read_csv(csv))
            with csv.open("a") as f:
                f.write("99.0,OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE,0,15.6")  # cut mid-write
            self.assertEqual(len(hc.load_csv(csv)), n)

            empty = tmp / "empty_data_1.csv"
            empty.write_text("")
            header_only = tmp / "header_data_2.csv"
            header_only.write_text(",".join(hc.USECOLS) + "\n")
            for bad in (empty, header_only):
                self.assertEqual(hc.main([str(bad), "--title", "x", "--no-show"]), 1)
            self.assertEqual(hc.main([str(tmp / "missing.csv"), "--title", "x", "--no-show"]), 1)

    def test_log_without_optimize_rows_is_reported(self):
        df = make_run(2)
        df["script_cmd"] = "DWELL 300"
        with tempfile.TemporaryDirectory() as tmp:
            csv = write_run(Path(tmp), df)
            self.assertEqual(hc.main([str(csv), "--title", "x", "--no-show"]), 1)


if __name__ == "__main__":
    unittest.main()
