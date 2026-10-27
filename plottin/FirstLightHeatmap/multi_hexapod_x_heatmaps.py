"""Lens-aperture raster heatmaps from a Trawler data CSV: one Y/Z heatmap per HEXAPOD X slice.

Counterpart of multi_stage_x_heatmaps.py for runs such as "Lens Aperture Raster Script Long",
where the HEXSTAGE stays put and the hexapod itself steps in X between slices. Each slice is a
snake raster (HEXAPOD MOVEINC) of hexapod Y/Z, typically over a circle.

Usage:
    python multi_hexapod_x_heatmaps.py [data.csv] [--out-dir DIR] [--title TEXT] [--y-bin-um N]
                                       [--z-bin-um N] [--scale shared|panel] [--log] [--max-gap-s 1]
                                       [--first-gap-um 24]

With no CSV argument a file picker opens. Outputs go in a heatmaps_hexapod_x folder next to
the CSV unless --out-dir is given.

The hand-written title (the unit under test) and the run date/time head every plot; the title
is also saved in the results CSV. Unless --title is given, a box prompts for it, pre-filled with
BG-CY0000 and the cursor after it so you can type the rest. Cancelling the box, or --title "",
means no hand-written title (see plot_common.py).

Slices are the time-contiguous runs of HEXAPOD MOVEINC rows: the script moves X, settles and
returns to the scan start between slices, so consecutive raster rows more than --max-gap-s
apart start a new slice. Offsets are the median recorded hexapod X of each slice relative to the
first slice, in um (the recorded X jitters by under 1 um within a slice, so offsets are not
rounded). Slices are never merged. Runs shorter than MIN_SLICE_ROWS are dropped as transient.

If the matching run .txt recipe defines FirstGapUm (or --first-gap-um is given), slices are
also labelled with their distance from the aperture: first gap + offset. This assumes hexapod
+X points away from the aperture, as in the Lens Aperture Raster recipe.

All slices share one Y/Z window taken from the largest slice, so partial slices (a Stopped run
cuts the last one short) show only the part that was scanned. Bin sizes are auto-sized by default
(auto_bins_um in single_stage_x_heatmap.py, shared by every heatmap script): the finest, most
square Y/Z bins that leave no empty bins inside the scanned area, so the map is a solid
circle/square. --y-bin-um / --z-bin-um fix either size (the other is still auto-sized).
Binning and rendering are provided by single_stage_x_heatmap.py. The CSV is read in chunks
with only the needed columns, so multi-gigabyte files are fine. Safe on empty, header-only,
partially written, or unfinished CSVs.
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable

if __package__:
    from .. import plot_common as common
    from . import single_stage_x_heatmap as single
    from .single_stage_x_heatmap import (
        CMAP, COL_CMD, COL_MOV, COL_T, COL_V, COL_Y, COL_Z, HEXSTAGE_COLS,
        V_SCALE, V_UNIT, draw_heatmap, make_norm, plt, report_sample_spacing, starting_stage_title,
    )
else:
    import single_stage_x_heatmap as single
    from single_stage_x_heatmap import (
        CMAP, COL_CMD, COL_MOV, COL_T, COL_V, COL_Y, COL_Z, HEXSTAGE_COLS,
        V_SCALE, V_UNIT, draw_heatmap, make_norm, plt, report_sample_spacing, starting_stage_title,
    )
    common = single.common

# ---------------------------------------------------------------- parameters
CSV_PATH = None                 # set a path here to skip the file picker / CLI argument
OUT_DIR = None                  # None = heatmaps_hexapod_x next to the CSV
SCALE = "shared"                # "shared" = one color scale for all slices, "panel" = per-panel scale
LOG_SCALE = False               # log color scale (signal may be ~1 mV or less)
MAX_GAP_S = 1.0                 # raster rows further apart in time than this start a new slice (s)
MIN_SLICE_ROWS = 25             # shorter runs are transient moves, not slices
EXTEND_TOL_MM = 0.005           # data outside the window by more than this extends the axes (mm)
COMPLETE_TOL_UM = 3.0           # last slice is complete if Y reached the window's top edge within this (um)
X_SPREAD_WARN_UM = 2.0          # warn if one slice's recorded hexapod X spans more than this (um)
CHUNK_ROWS = 250_000

COL_HEX_X = "hex_X_mm"
NUMERIC_COLS = [COL_T, COL_MOV, COL_HEX_X, COL_Y, COL_Z, COL_V] + HEXSTAGE_COLS
USECOLS = list(dict.fromkeys([COL_CMD] + NUMERIC_COLS))


# --------------------------------------------------------------------- loading
def load_csv(path: Path) -> tuple[pd.DataFrame, int]:
    """Read only the needed columns in chunks; return the frame and any unterminated tail size."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(max(0, size - (1 << 20)))
        tail = stream.read()
    cut = tail.rfind(b"\n")
    if cut == -1:
        raise ValueError("CSV has no complete line (empty or still being written).")
    tail_bytes = len(tail) - cut - 1

    header = pd.read_csv(path, nrows=0, encoding_errors="replace").columns
    missing = [col for col in USECOLS if col not in header]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    frames = []
    reader = pd.read_csv(path, usecols=USECOLS, dtype=str, chunksize=CHUNK_ROWS,
                         on_bad_lines="warn", encoding_errors="replace")
    for chunk in reader:
        for col in NUMERIC_COLS:
            chunk[col] = pd.to_numeric(chunk[col], errors="coerce")
        frames.append(chunk)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=USECOLS)
    df[NUMERIC_COLS] = df[NUMERIC_COLS].replace([np.inf, -np.inf], np.nan)
    if tail_bytes and len(df):
        df = df.iloc[:-1].reset_index(drop=True)
    return df, tail_bytes


def recipe_first_gap_um(csv_path: Path):
    """FirstGapUm from the matching run .txt recipe, or None if absent."""
    recipe = csv_path.with_name(csv_path.name.replace("_data_", "_")).with_suffix(".txt")
    if not recipe.is_file():
        return None
    text = recipe.read_text(encoding="utf-8-sig", errors="replace")
    match = re.search(r"^\s*FirstGapUm\s*=\s*(-?\d+(?:\.\d+)?)", text, re.MULTILINE)
    return float(match.group(1)) if match else None


def select_raster_rows(df: pd.DataFrame):
    """Keep valid HEXAPOD MOVEINC rows (the raster); X steps use MOVEABS and are dropped."""
    counts = {"rows read": len(df)}
    cmd = df[COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()
    is_raster = cmd.str.match(r"HEXAPOD\s+MOVEINC\b")
    counts["dropped: not HEXAPOD MOVEINC (moves, dwells, script lines)"] = int((~is_raster).sum())
    scan = df[is_raster]
    counts["raster rows (HEXAPOD MOVEINC)"] = len(scan)
    valid = scan[[COL_V, COL_Y, COL_Z, COL_HEX_X]].notna().all(axis=1)
    counts["dropped: blank V/Y/Z/hexapod X"] = int((~valid).sum())
    scan = scan[valid]
    t = scan[COL_T].to_numpy()
    if len(t) > 1 and (np.diff(t) < 0).any():
        print("WARNING: time_elapsed_s is not monotonic; sorting raster rows by time.")
        scan = scan.sort_values(COL_T, kind="mergesort")
    return scan.reset_index(drop=True), counts


# --------------------------------------------------------------------- slices
def split_slices(scan: pd.DataFrame, max_gap_s: float = MAX_GAP_S):
    """Split raster rows into time-contiguous slices; return (slices, transient) index arrays."""
    if scan.empty:
        return [], []
    brk = np.flatnonzero(np.diff(scan[COL_T].to_numpy()) > max_gap_s) + 1
    starts = np.r_[0, brk]
    ends = np.r_[brk, len(scan)]
    runs = [np.arange(s, e) for s, e in zip(starts, ends)]
    return ([idx for idx in runs if len(idx) >= MIN_SLICE_ROWS],
            [idx for idx in runs if len(idx) < MIN_SLICE_ROWS])


def scan_window(scan: pd.DataFrame, slices):
    """Y/Z bounds of the largest slice: the full scan window every slice is drawn in."""
    best = None
    for idx in slices:
        sub = scan.iloc[idx]
        y, z = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy()
        area = np.ptp(y) * np.ptp(z)
        if best is None or area > best[0]:
            best = (area, float(y.min()), float(y.max()), float(z.min()), float(z.max()))
    return best[1:]


def analyze_slice(scan, idx, window, ybin_mm, zbin_mm, followed):
    sub = scan.iloc[idx]
    y, z = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy()
    ylo, yhi, zlo, zhi = window
    cy, cz = (ylo + yhi) / 2, (zlo + zhi) / 2
    warn = []
    if y.min() < ylo - EXTEND_TOL_MM or y.max() > yhi + EXTEND_TOL_MM:
        warn.append(f"Y data [{y.min():.4f}, {y.max():.4f}] outside window [{ylo:.4f}, {yhi:.4f}]; axes extended")
        ylo, yhi = min(ylo, y.min()), max(yhi, y.max())
    if z.min() < zlo - EXTEND_TOL_MM or z.max() > zhi + EXTEND_TOL_MM:
        warn.append(f"Z data [{z.min():.4f}, {z.max():.4f}] outside window [{zlo:.4f}, {zhi:.4f}]; axes extended")
        zlo, zhi = min(zlo, z.min()), max(zhi, z.max())

    res = single.analyze_sweep(sub, ybin_mm, zbin_mm, bounds=(ylo, yhi, zlo, zhi),
                               include_spiral_zoom=False)
    hex_x = sub[COL_HEX_X].to_numpy()
    res.update({
        "hex_x_mm": float(np.median(hex_x)), "x_mm": float(np.median(hex_x)),
        "x_spread_um": float(np.ptp(hex_x) * 1000),
        "cy": cy, "cz": cz,
        "y_reached": float(np.clip((y.max() - ylo) / (yhi - ylo), 0, 1)),
        # The raster runs from the -Y edge to the +Y edge; only the last slice can be cut short.
        "complete": bool(followed or y.max() >= yhi - COMPLETE_TOL_UM / 1000.0),
        "warn": warn,
    })
    return res


# -------------------------------------------------------------------- plotting
def slice_norm(res, glob_norm, scale, log):
    if scale == "panel":
        return make_norm(res["vmin"], res["vmax"], log, res["pos_min"])
    return glob_norm


def slice_title(res):
    head = f"Slice {res['n']}: hexapod X {res['offset']:+.1f} \u00b5m"
    if res["gap_um"] is not None:
        head += f" (gap {res['gap_um']:.1f} \u00b5m)"
    if not res["complete"]:
        head += f"  PARTIAL ({res['y_reached']:.0%} of Y scanned)"
    return head + "\n" + single.peak_title(res)


def file_for(res, stem, suffix=".png"):
    return f"{stem}_hexapod_x_slice{res['n']:02d}_X{res['offset']:+.1f}um{suffix}"


def title_line(res):
    """The title header (hand-written title and run date/time) as a leading line."""
    return res["header"] + "\n"


def plot_individual(results, out_dir, stem, glob_norm, scale, log):
    paths = []
    for res in results:
        norm = slice_norm(res, glob_norm, scale, log)
        title = title_line(res) + res["stage_title"] + "\n" + slice_title(res)
        paths.append(single.save_heatmap(res, out_dir / file_for(res, stem), title, norm, log))
    return paths


def plot_summary(results, out_dir, stem, glob_norm, scale, log):
    n = len(results)
    cols = min(n, 3 if n <= 9 else 6)
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.6 * cols + 1.5, 4.6 * rows), squeeze=False,
                             constrained_layout=True)
    flat = axes.ravel()
    last_im = None
    for ax, res in zip(flat, results):
        im = draw_heatmap(ax, res, slice_norm(res, glob_norm, scale, log), log, small=True)
        ax.set_title(slice_title(res), fontsize=7)
        ax.tick_params(labelsize=7)
        if scale == "panel":
            fig.colorbar(im, ax=ax, shrink=0.8).ax.tick_params(labelsize=6)
        last_im = im
    for ax in flat[n:]:
        ax.set_visible(False)
    for ax in axes[-1]:
        ax.set_xlabel("Hexapod Y (mm)", fontsize=8)
    for ax in axes[:, 0]:
        ax.set_ylabel("Hexapod Z (mm)", fontsize=8)
    if scale != "panel" and last_im is not None:
        fig.colorbar(ScalarMappable(norm=glob_norm, cmap=plt.get_cmap(CMAP)), ax=list(flat[:n]),
                     label=f"max scope voltage per bin ({V_UNIT}){' - log' if log else ''}", shrink=0.8)
    fig.suptitle(f"{title_line(results[0])}Max voltage per Y/Z bin at each hexapod X slice - {stem}\n"
                 f"{results[0]['stage_title']}", fontsize=10)
    p = out_dir / f"{stem}_hexapod_x_summary.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def plot_peak_vs_x(results, out_dir, stem):
    use_gap = all(r["gap_um"] is not None for r in results)
    x = [r["gap_um"] if use_gap else r["offset"] for r in results]
    fig, axs = plt.subplots(3, 1, figsize=(7, 8), sharex=True, constrained_layout=True)
    series = [("peak_v", f"peak voltage ({V_UNIT})"), ("peak_y", "peak hexapod Y (mm)"),
              ("peak_z", "peak hexapod Z (mm)")]
    for ax, (key, label) in zip(axs, series):
        ax.plot(x, [r[key] for r in results], "-", color="gray", lw=1)
        for xi, r in zip(x, results):
            ax.plot(xi, r[key], "o", color="C0", mfc="C0" if r["complete"] else "white", ms=6)
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
    axs[-1].set_xlabel("Gap to aperture (\u00b5m)" if use_gap
                       else "Hexapod X offset from first slice (\u00b5m)")
    axs[0].set_title(title_line(results[0]) + "Peak vs hexapod X (hollow marker = partial slice)\n"
                     + results[0]["stage_title"])
    p = out_dir / f"{stem}_hexapod_x_peak_vs_X.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


# ------------------------------------------------------------------------ main
def main(argv=None):
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
    single.add_bin_args(ap)
    ap.add_argument("--scale", choices=["shared", "panel"], default=SCALE)
    ap.add_argument("--log", action="store_true", default=LOG_SCALE)
    ap.add_argument("--max-gap-s", type=single.positive_float, default=MAX_GAP_S,
                    help="time gap between raster rows that starts a new slice")
    ap.add_argument("--first-gap-um", type=float,
                    help="aperture gap of the first slice; default is FirstGapUm from the recipe")
    a = ap.parse_args(argv)

    path = common.input_path(a.csv, CSV_PATH)
    if path is None or not path.is_file():
        print(f"No CSV found: {path}")
        return 1
    user_title = common.get_title(a.title)
    header = common.title_header(user_title, common.run_time(path))
    out_dir = Path(a.out_dir) if a.out_dir else (
        Path(OUT_DIR) if OUT_DIR else path.parent / "heatmaps_hexapod_x")
    stem = path.stem

    print(f"Reading {path}")
    try:
        df, tail_bytes = load_csv(path)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not parse CSV: {e}")
        return 1
    if tail_bytes:
        print(f"Dropped unterminated last line ({tail_bytes} bytes) - file looks mid-write.")
    if df.empty:
        print("Nothing to plot: CSV has a header but no data rows yet.")
        return 1

    try:
        stage_title = starting_stage_title(df)
    except ValueError as e:
        print(f"Nothing to plot: {e}")
        return 1
    print(stage_title)

    scan, counts = select_raster_rows(df)
    print("\n== Row filters ==")
    for k, v in counts.items():
        print(f"  {k:<62}{v:>10}")
    if scan.empty:
        cmds = df[COL_CMD].fillna("<blank>").str.slice(0, 80).value_counts().head(25)
        print("\nNo HEXAPOD MOVEINC rows with valid readings. script_cmd strings in file:")
        print(cmds.to_string())
        return 1

    slices, transient = split_slices(scan, a.max_gap_s)
    if transient:
        print(f"\n  Dropped {len(transient)} transient run(s) shorter than {MIN_SLICE_ROWS} rows "
              f"({sum(len(i) for i in transient)} rows).")
    if not slices:
        print("No slice found (every raster run was shorter than MIN_SLICE_ROWS).")
        return 1

    window = scan_window(scan, slices)
    print(f"\nScan window from the largest slice: Y [{window[0]:+.4f}, {window[1]:+.4f}] mm, "
          f"Z [{window[2]:+.4f}, {window[3]:+.4f}] mm")

    first_gap = a.first_gap_um if a.first_gap_um is not None else recipe_first_gap_um(path)
    if first_gap is None:
        print("No FirstGapUm in the recipe (and no --first-gap-um): slices are labelled by X offset only.")
    else:
        print(f"First slice gap {first_gap:g} um; gap = first gap + hexapod X offset "
              "(hexapod +X assumed to point away from the aperture).")

    groups = [(scan.iloc[idx][COL_Y].to_numpy(), scan.iloc[idx][COL_Z].to_numpy(), window)
              for idx in slices]
    y_bin_um, z_bin_um = single.choose_bins_um(groups, a.y_bin_um, a.z_bin_um)

    results = []
    for n, idx in enumerate(slices, 1):
        res = analyze_slice(scan, idx, window, y_bin_um / 1000, z_bin_um / 1000,
                            followed=n < len(slices))
        res["n"] = n
        res["stage_title"] = stage_title
        res["user_title"] = user_title
        res["header"] = header
        results.append(res)
    base = results[0]["hex_x_mm"]
    for res in results:
        res["offset"] = (res["hex_x_mm"] - base) * 1000
        res["gap_um"] = None if first_gap is None else first_gap + res["offset"]

    print(f"\n== Slices ({len(results)}; first slice hexapod X = {base:.6f} mm is offset 0) ==")
    print(f"  {'#':>3} {'offset um':>10} {'gap um':>8} {'rows':>7} {'Y range (mm)':>24} "
          f"{'Z range (mm)':>24} {'peak '+V_UNIT:>10}  status")
    for r in results:
        gap = "-" if r["gap_um"] is None else f"{r['gap_um']:.1f}"
        status = "complete" if r["complete"] else f"PARTIAL ({r['y_reached']:.0%} of Y scanned)"
        print(f"  {r['n']:>3} {r['offset']:>+10.2f} {gap:>8} {r['rows']:>7} "
              f"[{r['y_min']:+9.4f},{r['y_max']:+9.4f}] [{r['z_min']:+9.4f},{r['z_max']:+9.4f}] "
              f"{r['peak_v']:>10.4g}  {status}")
        for w in r["warn"]:
            print(f"    WARNING slice {r['n']}: {w}")
        if r["x_spread_um"] > X_SPREAD_WARN_UM:
            print(f"    WARNING slice {r['n']}: recorded hexapod X spans {r['x_spread_um']:.1f} um; "
                  f"it may contain more than one X (try a smaller --max-gap-s)")
    report_sample_spacing(results, y_bin_um, z_bin_um)
    best = max(results, key=lambda r: r["peak_v"])
    label = f" (gap {best['gap_um']:.1f} um)" if best["gap_um"] is not None else ""
    print(f"\n  Highest peak: {best['peak_v']:.4g} {V_UNIT} in slice {best['n']}{label} at "
          f"Y {best['peak_y']:+.4f}, Z {best['peak_z']:+.4f} mm")

    vmin, vmax = min(r["vmin"] for r in results), max(r["vmax"] for r in results)
    pos = [r["pos_min"] for r in results if np.isfinite(r["pos_min"])]
    pos_min = min(pos) if pos else np.nan
    if a.log and not np.isfinite(pos_min):
        print("WARNING: no positive voltages; log scale unavailable, using linear.")
    glob_norm = make_norm(vmin, vmax, a.log, pos_min)

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = plot_individual(results, out_dir, stem, glob_norm, a.scale, a.log)
    paths.append(plot_summary(results, out_dir, stem, glob_norm, a.scale, a.log))
    paths.append(plot_peak_vs_x(results, out_dir, stem))

    table = pd.DataFrame([{
        "title": r["user_title"], "slice": r["n"], "x_offset_um": r["offset"], "hexapod_x_mm": r["hex_x_mm"],
        "gap_um": r["gap_um"], "rows_used": r["rows"],
        "peak_V": r["peak_v"] / V_SCALE, "peak_Y_mm": r["peak_y"], "peak_Z_mm": r["peak_z"],
        "peak_Y_from_centre_mm": r["peak_y"] - r["cy"], "peak_Z_from_centre_mm": r["peak_z"] - r["cz"],
        "peak_radius_mm": float(np.hypot(r["peak_y"] - r["cy"], r["peak_z"] - r["cz"])),
        "complete": r["complete"], "y_scanned_fraction": r["y_reached"],
        "y_min_mm": r["y_min"], "y_max_mm": r["y_max"], "z_min_mm": r["z_min"], "z_max_mm": r["z_max"],
        "hexapod_x_spread_um": r["x_spread_um"], "t_start_s": r["t0"], "t_end_s": r["t1"],
    } for r in results])
    csv_out = out_dir / f"{stem}_hexapod_x_slice_results.csv"
    table.to_csv(csv_out, index=False)
    paths.append(csv_out)

    print(f"\n== Saved to {out_dir} ==")
    for p in paths:
        print(f"  {p.relative_to(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
