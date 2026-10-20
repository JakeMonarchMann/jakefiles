import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

import plottin.FirstLightHeatmap.multi_hexapod_x_heatmaps as hexx

RADIUS = 0.01
X0 = 15.65
X_STEP = 0.006
STAGE = {"shrc_hex_X_mm": 15.806, "shrc_hex_Y_mm": 51.3665, "shrc_hex_Z_mm": 14.806}


def make_run(n_slices=3, partial_last=False):
    """Circular snake raster at each hexapod X, with a MOVEABS X step and a 6 s gap between slices."""
    rows, peaks, t = [], [], 0.0

    def add(cmd, x, y, z, v, moving=1):
        rows.append({"time_elapsed_s": t, "script_cmd": cmd, "hex_moving": moving,
                     "hex_X_mm": x, "hex_Y_mm": y, "hex_Z_mm": z, "oscope_voltage_V": v, **STAGE})

    add("DWELL 250", X0 - .03, 0, 0, .001, 0)
    for k in range(n_slices):
        x = X0 + k * X_STEP
        t += 6.0
        add("HEXAPOD MOVEABS X(SliceX)", x, 0, 0, .001, 1)
        t += .04
        add("HEXAPOD MOVEABS Y(YC - SearchRadiusMm) Z(ZC)", x, -RADIUS, 0, .001, 1)
        ys = np.arange(-RADIUS, RADIUS + 1e-9, .002)
        if partial_last and k == n_slices - 1:
            ys = ys[:6]
        start = len(rows)
        for j, y in enumerate(ys):
            half = float(np.sqrt(max(RADIUS ** 2 - y ** 2, 0)))
            zs = np.linspace(-half, half, 5)
            for z in (zs if j % 2 == 0 else zs[::-1]):
                t += .04
                jitter = (len(rows) % 3 - 1) * 0.0002
                add("HEXAPOD MOVEINC Z(2 * RowHalfZ * ZDir)", x + jitter, y, z, .001)
        peak_row = start + 17 + k
        rows[peak_row]["oscope_voltage_V"] = .005 + .001 * k
        peaks.append((rows[peak_row]["hex_Y_mm"], rows[peak_row]["hex_Z_mm"], .005 + .001 * k))
    return pd.DataFrame(rows), peaks


def run_main(argv):
    if "--title" not in argv:
        argv = [*argv, "--title", ""]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = hexx.main(argv)
    return code, out.getvalue()


class TitleTests(unittest.TestCase):
    def setUp(self):
        df, _ = make_run(2)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "run_data_1.csv"
        df.to_csv(self.path, index=False)
        self.table_path = (Path(self.folder.name) / "heatmaps_hexapod_x"
                           / "run_data_1_hexapod_x_slice_results.csv")

    def captured_titles(self, argv, ask=None):
        texts = []
        original = hexx.single.plt.Figure.savefig

        def capture(fig, path, *args, **kwargs):
            texts.append([*(ax.get_title() for ax in fig.axes),
                          fig._suptitle.get_text() if fig._suptitle else ""])
            return original(fig, path, *args, **kwargs)

        with patch.object(hexx.single.plt.Figure, "savefig", capture), \
                patch.object(hexx, "ask_title", ask or Mock(side_effect=AssertionError("prompted"))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = hexx.main([str(self.path), "--y-bin-um", "2", "--z-bin-um", "2", *argv])
        self.assertEqual(code, 0, out.getvalue())
        return texts, out.getvalue()

    def test_title_is_on_every_plot_and_in_results(self):
        texts, _ = self.captured_titles(["--title", "BG-CY000-12 unit A"])
        self.assertEqual(len(texts), 4)  # 2 slices + summary + peak vs X
        for page in texts:
            self.assertTrue(any(t.startswith("BG-CY000-12 unit A\n") for t in page), page)
        table = pd.read_csv(self.table_path)
        self.assertEqual(table["title"].tolist(), ["BG-CY000-12 unit A"] * 2)

    def test_empty_title_adds_nothing(self):
        texts, _ = self.captured_titles(["--title", ""])
        for page in texts:
            self.assertFalse(any(t.startswith("\n") for t in page), page)
        self.assertTrue(pd.read_csv(self.table_path)["title"].isna().all())

    def test_missing_title_prompts_with_default_and_uses_answer(self):
        ask = Mock(return_value="BG-CY000-7")
        texts, _ = self.captured_titles([], ask=ask)
        ask.assert_called_once_with()
        self.assertEqual(hexx.TITLE_PREFIX, "BG-CY000")
        self.assertTrue(any(t.startswith("BG-CY000-7\n") for t in texts[0]))
        self.assertEqual(pd.read_csv(self.table_path)["title"].tolist(), ["BG-CY000-7"] * 2)

    def test_cancelled_prompt_runs_without_title(self):
        _, text = self.captured_titles([], ask=Mock(return_value=None))
        self.assertIn("No title entered", text)
        self.assertTrue(pd.read_csv(self.table_path)["title"].isna().all())

    def test_bad_csv_path_does_not_prompt(self):
        ask = Mock(side_effect=AssertionError("prompted"))
        with patch.object(hexx, "ask_title", ask), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hexx.main([str(Path(self.folder.name) / "nope.csv")]), 1)
        ask.assert_not_called()

    def test_dialog_is_prefilled_with_cursor_at_end_and_returns_edit(self):
        import tkinter

        seen = {}

        def fake_mainloop(root, *args):
            entry = next(w for w in root.winfo_children() if isinstance(w, tkinter.Entry))
            root.update()
            seen.update(text=entry.get(), cursor=entry.index("insert"),
                        selected=entry.selection_present())
            entry.insert("end", "-12 unit A")
            root.event_generate("<Return>")
            root.update()

        try:
            with patch.object(tkinter.Tk, "mainloop", fake_mainloop):
                answer = hexx.ask_title()
        except tkinter.TclError:
            self.skipTest("no display available")
        self.assertEqual(seen, {"text": "BG-CY000", "cursor": 8, "selected": False})
        self.assertEqual(answer, "BG-CY000-12 unit A")


class HexapodXSliceTests(unittest.TestCase):
    def test_slices_follow_hexapod_x_and_report_peaks(self):
        df, peaks = make_run(3)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run_data_1.csv"
            df.to_csv(path, index=False)
            code, text = run_main([str(path), "--y-bin-um", "2", "--z-bin-um", "2",
                                   "--first-gap-um", "24"])
            self.assertEqual(code, 0, text)
            out = Path(folder) / "heatmaps_hexapod_x"
            table = pd.read_csv(out / "run_data_1_hexapod_x_slice_results.csv")
            self.assertEqual(table["slice"].tolist(), [1, 2, 3])
            np.testing.assert_allclose(table["x_offset_um"], [0, 6, 12], atol=0.5)
            np.testing.assert_allclose(table["gap_um"], [24, 30, 36], atol=0.5)
            np.testing.assert_allclose(table["peak_Y_mm"], [p[0] for p in peaks])
            np.testing.assert_allclose(table["peak_Z_mm"], [p[1] for p in peaks])
            np.testing.assert_allclose(table["peak_V"], [p[2] for p in peaks])
            self.assertEqual(table["rows_used"].tolist(), [55, 55, 55])
            self.assertTrue(table["complete"].all())
            for name in ["slice01_X+0.0um", "slice02_X+6.0um", "slice03_X+12.0um"]:
                self.assertGreater((out / f"run_data_1_hexapod_x_{name}.png").stat().st_size, 1000)
            self.assertGreater((out / "run_data_1_hexapod_x_summary.png").stat().st_size, 1000)
            self.assertGreater((out / "run_data_1_hexapod_x_peak_vs_X.png").stat().st_size, 1000)

    def test_stopped_run_marks_last_slice_partial_and_keeps_full_window(self):
        df, _ = make_run(3, partial_last=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run_data_1.csv"
            df.to_csv(path, index=False)
            code, text = run_main([str(path), "--y-bin-um", "2", "--z-bin-um", "2"])
            self.assertEqual(code, 0, text)
            self.assertIn("PARTIAL", text)
            table = pd.read_csv(Path(folder) / "heatmaps_hexapod_x" / "run_data_1_hexapod_x_slice_results.csv")
            self.assertEqual(table["complete"].tolist(), [True, True, False])
            self.assertLess(table.loc[2, "y_scanned_fraction"], 1)
            self.assertTrue(table["gap_um"].isna().all())

    def test_split_slices_by_time_gap_drops_transient_runs(self):
        t = np.r_[np.arange(30) * .04, 10 + np.arange(3) * .04, 20 + np.arange(40) * .04]
        scan = pd.DataFrame({hexx.COL_T: t})
        slices, transient = hexx.split_slices(scan)
        self.assertEqual([len(s) for s in slices], [30, 40])
        self.assertEqual([len(s) for s in transient], [3])
        self.assertEqual(hexx.split_slices(scan.iloc[:0]), ([], []))
        merged, _ = hexx.split_slices(scan, max_gap_s=100)
        self.assertEqual([len(s) for s in merged], [73])

    def test_x_steps_and_dwells_are_not_raster_rows(self):
        df, _ = make_run(2)
        scan, counts = hexx.select_raster_rows(df)
        self.assertEqual(len(scan), 110)
        self.assertEqual(counts["raster rows (HEXAPOD MOVEINC)"], 110)
        self.assertFalse(scan[hexx.COL_CMD].str.contains("MOVEABS").any())

    def test_recipe_first_gap_is_read_from_matching_txt(self):
        df, _ = make_run(2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run_data_20261019.csv"
            df.to_csv(path, index=False)
            self.assertIsNone(hexx.recipe_first_gap_um(path))
            (Path(folder) / "run_20261019.txt").write_text(
                "// FirstGapUm = 99\nFirstGapUm  = 24   // um\n", encoding="utf-8")
            self.assertEqual(hexx.recipe_first_gap_um(path), 24.0)
            code, text = run_main([str(path), "--y-bin-um", "2", "--z-bin-um", "2"])
            self.assertEqual(code, 0, text)
            table = pd.read_csv(Path(folder) / "heatmaps_hexapod_x" / "run_data_20261019_hexapod_x_slice_results.csv")
            np.testing.assert_allclose(table["gap_um"], [24, 30], atol=0.5)

    def test_unterminated_last_line_is_ignored(self):
        df, _ = make_run(2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run_data_1.csv"
            df.to_csv(path, index=False)
            with path.open("ab") as stream:
                stream.write(b"1.0,HEXAPOD MOVEINC Z(1),1,15.6")
            loaded, tail = hexx.load_csv(path)
            self.assertEqual(tail, 31)
            self.assertEqual(len(loaded), len(df))
            code, text = run_main([str(path), "--y-bin-um", "2", "--z-bin-um", "2"])
            self.assertEqual(code, 0, text)
            self.assertIn("Dropped unterminated last line", text)

    def test_unusable_csvs_fail_cleanly(self):
        df, _ = make_run(2)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            empty = folder / "empty.csv"
            empty.write_bytes(b"")
            self.assertEqual(run_main([str(empty)])[0], 1)
            header_only = folder / "header_only.csv"
            df.iloc[:0].to_csv(header_only, index=False)
            code, text = run_main([str(header_only)])
            self.assertEqual(code, 1)
            self.assertIn("no data rows", text)
            missing = folder / "missing.csv"
            df.drop(columns=["hex_X_mm"]).to_csv(missing, index=False)
            code, text = run_main([str(missing)])
            self.assertEqual(code, 1)
            self.assertIn("hex_X_mm", text)
            no_raster = folder / "no_raster.csv"
            df[~df["script_cmd"].str.contains("MOVEINC")].to_csv(no_raster, index=False)
            code, text = run_main([str(no_raster)])
            self.assertEqual(code, 1)
            self.assertIn("No HEXAPOD MOVEINC rows", text)

    def test_script_runs_directly(self):
        script = Path(hexx.__file__).resolve()
        result = subprocess.run([sys.executable, str(script), "--help"], cwd=script.parents[2],
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
