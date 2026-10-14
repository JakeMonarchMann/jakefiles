import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import single_x_heatmap as single
import multi_x_heatmaps as multi


def sample_frame() -> pd.DataFrame:
    commands = [
        "DWELL 200", "HEXAPOD MOVEABS Y(YMin) Z(ZMin)", "DWELL 200",
        "HEXAPOD MOVEINC Z(ZTravel*ZDir)", "HEXAPOD MOVEINC Y(YStep)",
        "HEXAPOD MOVEINC Z(ZTravel*ZDir)", "HEXAPOD GOTOMAX Peak",
        "HEXAPOD MOVEINC Y(LegLen)", "HEXAPOD MOVEINC Z(LegLen)",
    ]
    df = pd.DataFrame({col: np.zeros(len(commands)) for col in single.USECOLS})
    df[single.COL_CMD] = commands
    df[single.COL_T] = np.arange(len(commands))
    df["shrc_pkg_X_mm"] = -93.696
    df["shrc_pkg_Y_mm"] = 411.157
    df["shrc_hex_X_mm"] = 11.4125
    df["shrc_hex_Y_mm"] = 51.515
    df["shrc_hex_Z_mm"] = 15.84
    df["hex_X_mm"] = 15.682
    df["hex_Y_mm"] = [0, -.1, -.1, -.1, -.099, -.099, 0, -.1, -.099]
    df["hex_Z_mm"] = [0, -.15, -.15, -.15, -.15, .15, 0, -.15, .15]
    df["oscope_voltage_V"] = [0, 0, 0, .001, .003, .002, 0, .004, .005]
    return df


class SingleSweepTests(unittest.TestCase):
    def test_selection_excludes_corner_but_includes_later_sweeps(self):
        sweep, nominal = single.select_sweep(sample_frame())
        self.assertEqual(len(sweep), 5)
        self.assertEqual(nominal["hex_Y_mm"], 0)
        self.assertEqual(nominal["hex_Z_mm"], 0)
        self.assertEqual(sweep["oscope_voltage_V"].max(), .005)

    def test_small_scan_is_not_dropped_as_transient(self):
        sweep, nominal = single.select_sweep(sample_frame().iloc[:6])
        res = single.analyze_sweep(sweep, .001, .001)
        self.assertEqual(res["rows"], 3)
        self.assertAlmostEqual(res["peak_v"], 3)
        self.assertAlmostEqual(res["peak_y"], -.099)
        self.assertAlmostEqual(res["zhi"], .15)
        title = single.sweep_title(res, nominal)
        self.assertIn("PACKAGE (hexapod centered): X -93.6960, Y +411.1570 mm", title)
        self.assertIn("HEXSTAGE: X +11.4125, Y +51.5150, Z +15.8400 mm", title)

    def test_max_per_bin_and_missing_bins(self):
        sweep, _ = single.select_sweep(sample_frame())
        sweep.loc[1, "hex_Y_mm"] = sweep.loc[0, "hex_Y_mm"]
        res = single.analyze_sweep(sweep, .001, .001)
        self.assertEqual(res["grid"][0, 0], 4)
        self.assertEqual(res["grid"][300, 1], 5)
        self.assertTrue(np.isnan(res["grid"][1, 0]))
        self.assertEqual(res["vmin"], 4)
        self.assertEqual(res["pos_min"], 4)

    def test_single_point_has_nonzero_axis_limits(self):
        sweep, _ = single.select_sweep(sample_frame())
        res = single.analyze_sweep(sweep.iloc[:1], .001, .001)
        self.assertGreater(res["yhi"], res["ylo"])
        self.assertGreater(res["zhi"], res["zlo"])

    def test_missing_reference_and_no_scan_fail_explicitly(self):
        df = sample_frame()
        with self.assertRaisesRegex(ValueError, "No settled nominal"):
            single.select_sweep(df.iloc[1:].reset_index(drop=True))
        with self.assertRaisesRegex(ValueError, "No HEXAPOD MOVEINC"):
            single.select_sweep(df.iloc[:3])

    def test_invalid_bins(self):
        for value in ["0", "-1", "nan", "inf"]:
            with self.assertRaises(single.argparse.ArgumentTypeError):
                single.positive_float(value)

    def test_cli_outputs_and_unfinished_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scan.csv"
            sample_frame().to_csv(path, index=False)
            with path.open("ab") as stream:
                stream.write(b"incomplete")
            df, tail = single.load_csv(path)
            self.assertEqual(tail, 10)
            self.assertEqual(len(df), 9)
            self.assertEqual(single.main([str(path), "--log"]), 0)
            out_dir = Path(folder) / "heatmaps"
            self.assertGreater((out_dir / "scan_single_sweep_heatmap.png").stat().st_size, 1000)
            results = pd.read_csv(out_dir / "scan_single_sweep_results.csv")
            self.assertEqual(results.loc[0, "rows_used"], 5)
            self.assertAlmostEqual(results.loc[0, "peak_V"], .005)
            self.assertAlmostEqual(results.loc[0, "nominal_shrc_pkg_X_mm"], -93.696)

    def test_nonpositive_voltage_linear_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "negative.csv"
            df = sample_frame()
            df["oscope_voltage_V"] = -.001
            df.to_csv(path, index=False)
            self.assertEqual(single.main([str(path), "--log"]), 0)

    def test_standalone_rejects_multiple_stage_x_values(self):
        df = sample_frame()
        df.loc[7:, single.COL_X] += .006
        with self.assertRaisesRegex(ValueError, "Multiple stage X"):
            single.select_sweep(df)

    def test_filter_excludes_stage_moves_comments_and_invalid_samples(self):
        df = sample_frame()
        df.loc[1, single.COL_CMD] = "HEXSTAGE MOVEINC X(.006)"
        df.loc[2, single.COL_CMD] = "// HEXAPOD MOVEINC Y(.001)"
        df.loc[3, single.COL_V] = np.inf
        sweep, counts, _ = single.filter_snake(df)
        self.assertEqual(len(sweep), 4)
        self.assertEqual(counts["dropped: blank V/Y/Z/X"], 1)

    def test_rectangular_outputs_shrink_without_clipping_labels(self):
        sweep, nominal = single.select_sweep(sample_frame())
        original_save = single.plt.Figure.savefig
        with tempfile.TemporaryDirectory() as folder:
            for ratio in [0.2, 0.7, 1.0, 2.0, 5.0]:
                with self.subTest(ratio=ratio):
                    res = single.analyze_sweep(sweep, .001, .001)
                    res["yhi"] = res["ylo"] + ratio
                    res["zhi"] = res["zlo"] + 1.0

                    def capture(fig, path, *args, **kwargs):
                        width, height = fig.get_size_inches()
                        self.assertLess(width, 9)
                        self.assertLessEqual(height, 8)
                        if ratio > 1:
                            self.assertLess(height, 8)
                        self.assertEqual(kwargs["bbox_inches"], "tight")
                        self.assertGreaterEqual(kwargs["pad_inches"], 0.2)
                        fig.canvas.draw()
                        renderer = fig.canvas.get_renderer()
                        bounds = fig.get_tightbbox(renderer).transformed(fig.dpi_scale_trans)
                        for ax in fig.axes:
                            xlo, xhi = sorted(ax.get_xlim())
                            ylo, yhi = sorted(ax.get_ylim())
                            xticks = [label for pos, label in zip(ax.get_xticks(), ax.get_xticklabels())
                                      if xlo <= pos <= xhi]
                            yticks = [label for pos, label in zip(ax.get_yticks(), ax.get_yticklabels())
                                      if ylo <= pos <= yhi]
                            for text in [ax.title, ax.xaxis.label, ax.yaxis.label,
                                         *xticks, *yticks]:
                                if text.get_visible() and text.get_text():
                                    box = text.get_window_extent(renderer)
                                    self.assertGreaterEqual(box.x0, bounds.x0 - 1)
                                    self.assertGreaterEqual(box.y0, bounds.y0 - 1)
                                    self.assertLessEqual(box.x1, bounds.x1 + 1)
                                    self.assertLessEqual(box.y1, bounds.y1 + 1)
                        plot_box = fig.axes[0].get_window_extent(renderer)
                        self.assertAlmostEqual(plot_box.width / plot_box.height, ratio)
                        return original_save(fig, path, *args, **kwargs)

                    with patch.object(single.plt.Figure, "savefig", capture):
                        single.save_heatmap(
                            res, Path(folder) / f"ratio_{ratio}.png",
                            single.sweep_title(res, nominal),
                            single.make_norm(res["vmin"], res["vmax"], False, res["pos_min"]),
                            False,
                        )


class MultiSweepTests(unittest.TestCase):
    def test_revisits_combine_and_transient_stage_positions_are_excluded(self):
        xs = [11.4] * 25 + [11.401] + [11.406] * 25 + [11.4] * 3
        df = pd.DataFrame({single.COL_X: xs})
        runs, transient = multi.split_passes(df)
        self.assertEqual([x for _, x in runs], [11400, 11406])
        self.assertEqual([len(idx) for idx, _ in runs], [28, 25])
        self.assertEqual(len(transient), 1)
        np.testing.assert_array_equal(runs[0][0][-3:], [51, 52, 53])

    def test_one_x_short_scan_uses_all_rows(self):
        sweep, _ = single.select_sweep(sample_frame())
        runs, transient = multi.split_passes(sweep)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0][0]), 5)
        self.assertEqual(transient, [])

    def test_shared_binning_preserves_expected_window_and_incomplete_status(self):
        sweep, _ = single.select_sweep(sample_frame().iloc[:6])
        idx = np.arange(len(sweep))
        with patch.object(single, "analyze_sweep", wraps=single.analyze_sweep) as analyze:
            res = multi.analyze_pass(sweep, idx, 11412, .01, .3, .001, .001, False)
        analyze.assert_called_once()
        expected = single.analyze_sweep(sweep, .001, .001, bounds=(-.1, -.09, -.15, .15))
        np.testing.assert_array_equal(res["grid"], expected["grid"])
        self.assertTrue(res["complete"])
        partial = multi.analyze_pass(sweep.iloc[:2], np.arange(2), 11412,
                                     .01, .3, .001, .001, False)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["z_rows_expected"], 301)

    def test_starting_stage_title_is_not_first_slice_x(self):
        df = sample_frame()
        df.loc[0, single.COL_X] = 11.5
        self.assertEqual(single.starting_stage_title(df),
                         "Starting HEXSTAGE: X +11.5000, Y +51.5150, Z +15.8400 mm")
        df.loc[:3, "shrc_hex_Z_mm"] = np.nan
        with self.assertRaisesRegex(ValueError, "No complete starting"):
            single.starting_stage_title(df)

    def test_multi_cli_calls_single_renderer_for_each_x_and_keeps_titles(self):
        seed = sample_frame().iloc[:6]
        first = pd.concat([seed.iloc[3:6]] * 9, ignore_index=True)
        second = first.copy()
        second[single.COL_X] += .006
        # A short later sweep at a known stable X must also be included.
        revisit = sample_frame().iloc[7:9].copy()
        df = pd.concat([seed.iloc[:3], first, second, revisit], ignore_index=True)
        df[single.COL_T] = np.arange(len(df))
        expected_title = single.starting_stage_title(df)
        titles = []
        original_save = single.plt.Figure.savefig

        def capture(fig, path, *args, **kwargs):
            text = [ax.get_title() for ax in fig.axes]
            if fig._suptitle is not None:
                text.append(fig._suptitle.get_text())
            self.assertTrue(any(expected_title in t for t in text))
            titles.append(Path(path).name)
            return original_save(fig, path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "multi.csv"
            df.to_csv(path, index=False)
            with patch.object(single, "save_heatmap", wraps=single.save_heatmap) as render, \
                    patch.object(single.plt.Figure, "savefig", capture):
                self.assertEqual(multi.main([str(path), "--scale", "panel", "--log"]), 0)
                self.assertEqual(render.call_count, 2)
            table = pd.read_csv(Path(folder) / "heatmaps" / "multi_pass_results.csv")
            self.assertEqual(table["x_offset_um"].tolist(), [0, 6])
            self.assertEqual(table["rows_used"].tolist(), [29, 27])
            self.assertAlmostEqual(table.loc[0, "peak_V"], .005)
            self.assertEqual(len(titles), 4)

    def test_single_x_multi_cli_matches_standalone_peak(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "one.csv"
            sample_frame().to_csv(path, index=False)
            self.assertEqual(multi.main([str(path)]), 0)
            self.assertEqual(single.main([str(path)]), 0)
            out = Path(folder) / "heatmaps"
            standalone = pd.read_csv(out / "one_single_sweep_results.csv")
            grouped = pd.read_csv(out / "one_pass_results.csv")
            for key in ["rows_used", "peak_V", "peak_Y_mm", "peak_Z_mm",
                        "y_min_mm", "y_max_mm", "z_min_mm", "z_max_mm"]:
                self.assertEqual(standalone.loc[0, key], grouped.loc[0, key])


if __name__ == "__main__":
    unittest.main()
