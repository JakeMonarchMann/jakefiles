import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import plottin.FirstLightHeatmap.single_stage_x_heatmap as single
import plottin.FirstLightHeatmap.multi_stage_x_heatmaps as multi


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
        self.assertEqual(len(sweep), 6)
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
        self.assertEqual(res["vmin"], 0)
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
            self.assertEqual(single.main([str(path), "--title", "", "--log"]), 0)
            out_dir = Path(folder) / "heatmaps"
            self.assertGreater((out_dir / "scan_single_sweep_heatmap.png").stat().st_size, 1000)
            results = pd.read_csv(out_dir / "scan_single_sweep_results.csv")
            self.assertEqual(results.loc[0, "rows_used"], 6)
            self.assertAlmostEqual(results.loc[0, "peak_V"], .005)
            self.assertAlmostEqual(results.loc[0, "nominal_shrc_pkg_X_mm"], -93.696)

    def test_nonpositive_voltage_linear_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "negative.csv"
            df = sample_frame()
            df["oscope_voltage_V"] = -.001
            df.to_csv(path, index=False)
            self.assertEqual(single.main([str(path), "--title", "", "--log"]), 0)

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

    def test_spiral_and_calibration_stop_at_first_pair_movement(self):
        df = sample_frame()
        df.loc[1, single.COL_CMD] = "Aligner1 SET TOOL RBA"
        df.loc[2, single.COL_CMD] = "Aligner2 ActivateRobot()"
        df.loc[4, single.COL_CMD] = "HEXAPOD MOVEUNTIL Z+(LegLen) VOLTAGE > Target"
        df.loc[5, single.COL_CMD] = "DWELL 200"
        df.loc[6, single.COL_CMD] = "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE"
        df.loc[7, single.COL_CMD] = "OPTIMIZE2 MAXIMIZE OSCOPE_VOLTAGE"
        df.loc[8, single.COL_CMD] = "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE"
        df["script_line"] = [1, 2, 3, 4, 5, 6, 8, 10, 12]
        df.attrs["recipe_lines"] = [
            "DWELL 200", "Aligner1 SET TOOL RBA", "Aligner2 ActivateRobot()",
            "HEXAPOD MOVEINC Z(.1)", "HEXAPOD MOVEUNTIL Z+(.01) VOLTAGE > Target",
            "DWELL 200", "OPTIMIZE AXIS HEXAPOD_Y STEP .001 MAX .01",
            "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE",
            "OPTIMIZE AXIS TANDEM_YAW STEP .5 MAX 5",
            "OPTIMIZE2 MAXIMIZE OSCOPE_VOLTAGE",
            "OPTIMIZE AXIS HEXAPOD_Y STEP .001 MAX .01",
            "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE",
        ]
        for col in single.ALIGNER_JOINT_COLS:
            df[col] = 0.0
        df.loc[8, single.ALIGNER_JOINT_COLS[0]] = .002
        sweep, _ = single.select_sweep(df)
        self.assertEqual(sweep[single.COL_T].tolist(), [3, 4, 5, 6, 7])
        self.assertEqual(single.rba_motion_cutoff(df), 8)
        self.assertEqual(sweep[single.COL_V].max(), .004)

    def test_direct_rba_motion_is_cutoff_not_activation_or_tool_setup(self):
        df = sample_frame()
        df.loc[1, single.COL_CMD] = "Aligner1 SET TOOL RBA"
        df.loc[2, single.COL_CMD] = "Aligner2 ActivateRobot()"
        df.loc[7, single.COL_CMD] = "Aligner1 MovePose(1,2,3,4,5,6)"
        for col in single.ALIGNER_JOINT_COLS:
            df[col] = 0.0
        df.loc[7, single.ALIGNER_JOINT_COLS[-1]] = .002
        sweep, _ = single.select_sweep(df)
        self.assertEqual(sweep[single.COL_T].tolist(), [3, 4, 5, 6])

    def test_command_without_movement_keeps_later_readings_and_ignores_jitter(self):
        df = sample_frame()
        df.loc[7, single.COL_CMD] = "Aligner2 MovePose(1,2,3,4,5,6)"
        for col in single.ALIGNER_JOINT_COLS:
            df[col] = 0.0
        df.loc[7:, single.ALIGNER_JOINT_COLS[-1]] = .0008
        self.assertEqual(single.rba_motion_cutoff(df), len(df))
        sweep, _ = single.select_sweep(df)
        self.assertEqual(sweep[single.COL_T].max(), 8)

    def test_missing_joint_readbacks_fail_explicitly_when_motion_command_occurs(self):
        df = sample_frame()
        df.loc[7, single.COL_CMD] = "Aligner1 MovePose(1,2,3,4,5,6)"
        with self.assertRaisesRegex(ValueError, "requires joint readbacks"):
            single.rba_motion_cutoff(df)
        for col in single.ALIGNER_JOINT_COLS:
            df[col] = 0.0
        df.loc[8, single.ALIGNER_JOINT_COLS[0]] = np.nan
        with self.assertRaisesRegex(ValueError, "Incomplete Aligner"):
            single.rba_motion_cutoff(df)

    def test_unresolved_optimizer_fails_instead_of_including_rba_data(self):
        df = sample_frame()
        df.loc[7, single.COL_CMD] = "OPTIMIZE2 MAXIMIZE OSCOPE_VOLTAGE"
        with self.assertRaisesRegex(ValueError, "matching run .txt recipe"):
            single.select_sweep(df)

    def test_loader_finds_recipe_and_preserves_source_line_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run_data_20261014.csv"
            recipe = Path(folder) / "run_20261014.txt"
            recipe.write_text("Aligner1 MovePose(1,2,3,4,5,6)\n", encoding="utf-8")
            df = sample_frame()
            df["script_line"] = 1
            df.to_csv(path, index=False)
            loaded, _ = single.load_csv(path)
            self.assertEqual(loaded.attrs["recipe_lines"], [recipe.read_text().strip()])
            self.assertEqual(loaded["script_line"].iloc[0], 1)

    def test_rectangular_outputs_shrink_without_clipping_labels(self):
        sweep, nominal = single.select_sweep(sample_frame())
        original_save = single.plt.Figure.savefig
        with tempfile.TemporaryDirectory() as folder:
            for ratio in [0.2, 0.7, 1.0, 2.0, 5.0]:
                with self.subTest(ratio=ratio):
                    res = single.analyze_sweep(sweep, .001, .001)
                    res.pop("spiral_zoom", None)
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


class XYZOptimizerTitleTests(unittest.TestCase):
    def test_xyz_peak_is_separate_from_coarse_and_rba_optimizer_peaks(self):
        df = sample_frame()
        df["script_line"] = 1
        df.loc[3, single.COL_V] = .02
        df.loc[6:7, single.COL_CMD] = "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE"
        df.loc[6:7, "script_line"] = 4
        df.loc[6, single.COL_V] = .006
        df.loc[7, single.COL_V] = .008
        df.loc[7, "hex_X_mm"] = 15.679
        df.loc[8, single.COL_CMD] = "OPTIMIZE2 MAXIMIZE OSCOPE_VOLTAGE"
        df.loc[8, "script_line"] = 6
        df.loc[8, single.COL_V] = .03
        df.attrs["recipe_lines"] = [
            "OPTIMIZE AXIS HEXAPOD_X STEP .001 MAX .01",
            "OPTIMIZE AXIS HEXAPOD_Y STEP .001 MAX .01",
            "OPTIMIZE AXIS HEXAPOD_Z STEP .001 MAX .01",
            "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE",
            "OPTIMIZE AXIS TANDEM_ROLL STEP .1 MAX 1",
            "OPTIMIZE2 MAXIMIZE OSCOPE_VOLTAGE",
        ]
        peak = single.xyz_optimizer_peak(df)
        self.assertEqual(peak, {"voltage_V": .008, "X_mm": 15.679,
                                "Y_mm": -.1, "Z_mm": -.15})
        res = single.analyze_sweep(df, .001, .001)
        res["xyz_optimizer_peak"] = peak
        title = single.sweep_title(res, df.iloc[0])
        self.assertIn("XYZ OPTIMIZE sampled best 8 mV", title)
        self.assertIn("X +15.679000, Y -0.100000, Z -0.150000 mm", title)

    def test_no_xyz_optimizer_omits_annotation(self):
        df = sample_frame()
        self.assertIsNone(single.xyz_optimizer_peak(df))
        res = single.analyze_sweep(df, .001, .001)
        self.assertNotIn("XYZ OPTIMIZE", single.sweep_title(res, df.iloc[0]))


class SpiralZoomTests(unittest.TestCase):
    def test_explicit_spiral_zoom_has_fine_bins_padded_bounds_and_no_optimizer(self):
        df = sample_frame()
        df.loc[7, single.COL_Y] = .005
        df.loc[8, single.COL_Y] = .00505
        df.loc[7, single.COL_Z] = -.05
        df.loc[8, single.COL_Z] = -.0501
        later = df.iloc[-1:].copy()
        later[single.COL_CMD] = "OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE"
        later[single.COL_Y] = .02
        later[single.COL_V] = .1
        selected = pd.concat([df.iloc[3:], later], ignore_index=True)
        res = single.analyze_sweep(selected, .001, .001)
        zoom = res["spiral_zoom"]
        self.assertEqual(zoom["rows"], 2)
        self.assertEqual(zoom["peak_v"], 5)
        self.assertLess(zoom["ybin"], .001)
        self.assertLess(zoom["zbin"], .001)
        ylo, yhi, zlo, zhi = zoom["view_bounds"]
        self.assertLess(ylo, .005)
        self.assertGreater(yhi, .00505)
        self.assertLess(zlo, -.0501)
        self.assertGreater(zhi, -.05)
        with tempfile.TemporaryDirectory() as folder:
            original = single.plt.Figure.savefig

            def capture(fig, path, *args, **kwargs):
                self.assertEqual(len(fig.axes), 3)
                self.assertIn("Spiral zoom", fig.axes[1].get_title())
                np.testing.assert_allclose(fig.axes[1].get_xlim(), [ylo, yhi])
                self.assertIs(fig.axes[0].images[0].norm, fig.axes[1].images[0].norm)
                self.assertEqual(len(fig.axes[0].patches), 1)
                self.assertIsNotNone(fig._suptitle)
                return original(fig, path, *args, **kwargs)

            with patch.object(single.plt.Figure, "savefig", capture):
                single.save_heatmap(res, Path(folder) / "spiral.png", "Test metadata",
                                    single.make_norm(res["vmin"], res["vmax"], False,
                                                     res["pos_min"]), False)

    def test_no_spiral_does_not_add_a_panel(self):
        res = single.analyze_sweep(sample_frame().iloc[3:6], .001, .001)
        self.assertNotIn("spiral_zoom", res)

    def test_small_alternating_legs_after_coarse_sweep_detected_from_data(self):
        df = sample_frame().iloc[[3]].copy()
        frames = []
        legs = [
            ("Z(Travel)", [0, 0], [0, .1]),
            ("Y(.00005)", [0, .00005], [.05, .05]),
            ("Z(.00005)", [.00005, .00005], [.05, .05005]),
            ("Y(-.0001)", [.00005, -.00005], [.05005, .05005]),
            ("Z(-.0001)", [-.00005, -.00005], [.05005, .04995]),
        ]
        for expression, ys, zs in legs:
            sub = pd.concat([df, df], ignore_index=True)
            sub[single.COL_CMD] = "HEXAPOD MOVEINC " + expression
            sub[single.COL_Y] = ys
            sub[single.COL_Z] = zs
            frames.append(sub)
        sweep = pd.concat(frames, ignore_index=True)
        spiral = single.spiral_readings(sweep)
        self.assertEqual(len(spiral), 8)
        self.assertIn("spiral_zoom", single.analyze_sweep(sweep, .001, .001))


class MultiSweepTests(unittest.TestCase):
    def test_script_runs_directly(self):
        script = Path(multi.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())

    def test_arbitrary_fractional_x_steps_have_distinct_slices_and_outputs(self):
        seed = sample_frame().iloc[:6]
        for offsets in [[0, .25, .5], [0, -2, -5.5]]:
            with self.subTest(offsets=offsets), tempfile.TemporaryDirectory() as folder:
                frames = [seed.iloc[:3]]
                for offset in offsets:
                    frame = pd.concat([seed.iloc[3:6]] * 9, ignore_index=True)
                    frame[single.COL_X] = 11.4125 + offset / 1000
                    frames.append(frame)
                df = pd.concat(frames, ignore_index=True)
                df[single.COL_T] = np.arange(len(df))
                path = Path(folder) / "fractional.csv"
                df.to_csv(path, index=False)
                original = single.plt.Figure.savefig

                def capture(fig, path, *args, **kwargs):
                    if str(path).endswith("_heatmap_summary.png"):
                        panels = [ax for ax in fig.axes if ax.images]
                        self.assertEqual(len(panels), len(offsets))
                        self.assertFalse(any("not scanned" in text.get_text()
                                             for ax in fig.axes for text in ax.texts))
                    return original(fig, path, *args, **kwargs)

                with patch.object(single.plt.Figure, "savefig", capture):
                    self.assertEqual(multi.main([str(path), "--title", ""]), 0)
                out = Path(folder) / "heatmaps"
                results = pd.read_csv(out / "fractional_pass_results.csv")
                np.testing.assert_allclose(results["x_offset_um"], offsets)
                self.assertEqual(results["rows_used"].tolist(), [27] * len(offsets))
                for offset in offsets:
                    self.assertTrue((out / f"fractional_heatmap_X{offset:+.12g}um.png").is_file())

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
        self.assertEqual(len(runs[0][0]), 6)
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
                self.assertEqual(multi.main([str(path), "--title", "", "--scale", "panel", "--log"]), 0)
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
            self.assertEqual(multi.main([str(path), "--title", ""]), 0)
            self.assertEqual(single.main([str(path), "--title", ""]), 0)
            out = Path(folder) / "heatmaps"
            standalone = pd.read_csv(out / "one_single_sweep_results.csv")
            grouped = pd.read_csv(out / "one_pass_results.csv")
            for key in ["rows_used", "peak_V", "peak_Y_mm", "peak_Z_mm",
                        "y_min_mm", "y_max_mm", "z_min_mm", "z_max_mm"]:
                self.assertEqual(standalone.loc[0, key], grouped.loc[0, key])


class AutoBinTests(unittest.TestCase):
    @staticmethod
    def raster_frame(x_offsets_um):
        """Snake raster per stage X: Y columns 3 um apart, Z sampled every 0.5 um over 30 um."""
        seed = sample_frame().iloc[:3]
        frames = [seed]
        ys = np.arange(11) * .003
        zs = np.arange(61) * .0005
        for offset in x_offsets_um:
            y = np.repeat(ys, len(zs))
            z = np.concatenate([zs if i % 2 == 0 else zs[::-1] for i in range(len(ys))])
            frame = pd.concat([seed.iloc[[0]]] * len(y), ignore_index=True)
            frame[single.COL_CMD] = "HEXAPOD MOVEINC Z(ZTravel*ZDir)"
            frame[single.COL_Y], frame[single.COL_Z] = y, z
            frame[single.COL_V] = .001 + y * z
            frame[single.COL_X] = 11.4125 + offset / 1000
            frames.append(frame)
        df = pd.concat(frames, ignore_index=True)
        df[single.COL_T] = np.arange(len(df))
        return df

    def test_auto_bins_cover_the_coarse_axis_and_keep_the_fine_axis(self):
        df = self.raster_frame([0])
        sweep = df.iloc[3:]
        y, z = sweep[single.COL_Y].to_numpy(), sweep[single.COL_Z].to_numpy()
        window = (y.min(), y.max(), z.min(), z.max())
        yb, zb, holes = single.auto_bins_um([(y, z, window)])
        self.assertAlmostEqual(yb, single.AUTO_GROWTH ** 6)  # 2.99 um: the finest that bridges 3 um columns
        self.assertEqual(zb, 1)
        self.assertLessEqual(holes, single.AUTO_HOLE_TOL)
        self.assertGreater(single.hole_fraction(y, z, window, .001, .001), single.AUTO_HOLE_TOL)
        fixed = single.auto_bins_um([(y, z, window)], y_um=5, z_um=None)
        self.assertEqual(fixed[:2], (5, 1))

    def test_every_heatmap_cli_auto_sizes_bins_by_default(self):
        cases = [(single, [0]), (multi, [0]), (multi, [0, 6])]
        for module, offsets in cases:
            with self.subTest(module=module.__name__, offsets=offsets), \
                    tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "raster_data_20260102_030405.csv"
                self.raster_frame(offsets).to_csv(path, index=False)
                with patch.object(single, "auto_bins_um", wraps=single.auto_bins_um) as auto, \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(module.main([str(path), "--title", ""]), 0)
                auto.assert_called_once()
                self.assertEqual(len(auto.call_args.args[0]), len(offsets))
                self.assertIn("Bin size (auto): Y 2.99 um x Z 1.00 um; 0.00% of scanned bins empty",
                              out.getvalue())


class TitleTests(unittest.TestCase):
    def test_every_heatmap_title_starts_with_user_title_and_run_time(self):
        for module, offsets in [(single, [0]), (multi, [0, 6])]:
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "raster_data_20260102_030405.csv"
                AutoBinTests.raster_frame(offsets).to_csv(path, index=False)
                pages = []
                original = single.plt.Figure.savefig

                def capture(fig, *args, **kwargs):
                    texts = [ax.get_title() for ax in fig.axes]
                    if fig._suptitle is not None:
                        texts.append(fig._suptitle.get_text())
                    pages.append(texts)
                    return original(fig, *args, **kwargs)

                with patch.object(single.plt.Figure, "savefig", capture), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main([str(path), "--title", "BG-CY0000-5"]), 0)
                self.assertEqual(len(pages), 1 if module is single else 4)
                for texts in pages:
                    self.assertTrue(any(t.startswith("BG-CY0000-5  |  Run 2026-01-02 03:04:05\n")
                                        for t in texts), texts)


if __name__ == "__main__":
    unittest.main()
