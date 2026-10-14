"""First-light heatmaps from a Trawler data CSV: one Y/Z heatmap per HEXSTAGE X.

Usage:
    python multi_x_heatmaps.py [data.csv] [--out-dir DIR] [--y-bin-um 1] [--z-bin-um 1]
                                  [--half-window-mm 0.5] [--scale shared|panel] [--log]

With no CSV argument a file picker opens. Outputs go in a heatmaps folder next to the CSV
unless --out-dir is given, in which case that folder is used.
Safe on empty, header-only, partially written, or unfinished CSVs.
All graph titles include starting HEXSTAGE X/Y/Z readbacks from the first
fully populated stage sample at or before the first MOVEINC scan row.
All sweeps revisiting the same rounded (1 um) stage X are combined.
Single-slice binning and plotting are provided by single_x_heatmap.py.
With only one X slice, axes use measured bounds rather than an assumed window.
The matching run .txt recipe identifies RBA optimizer commands. Readings at or
after the first detected Aligner1/Aligner2 joint movement are excluded, retaining
stationary optimizer calibration; a lone X slice
includes all positioned pre-RBA readings, including MOVEUNTIL spirals.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable

import single_x_heatmap as single
from single_x_heatmap import (
    BASE_USECOLS as USECOLS, CMAP, COL_CMD, COL_X,
    COL_Y, COL_Z, SNAKE_TOKEN, V_SCALE, V_UNIT, draw_heatmap, filter_snake,
    make_norm, plt, starting_stage_title,
)

# ---------------------------------------------------------------- parameters
CSV_PATH = None                 # set a path here to skip the file picker / CLI argument
OUT_DIR = None                  # None = next to the CSV
Y_BIN_UM = 1.0                  # heatmap bin size in hexapod Y (um)
Z_BIN_UM = 1.0                  # heatmap bin size in hexapod Z (um)
HALF_WINDOW_MM = 0.5            # retained CLI default; a lone X slice now uses measured bounds
SCALE = "shared"                # "shared" = one color scale for all passes, "panel" = per-panel scale
LOG_SCALE = False               # log color scale (signal may be ~1 mV or less)
EXPECTED_OFFSETS_UM = [-30, -24, -18, -12, -6, 0, 6, 12, 18, 24, 30]   # X offsets the script should visit
SHOW_MISSING_EXPECTED = True    # leave an empty "not scanned" panel for expected offsets with no data
REQUIRE_MOVING = False          # True = also drop snake rows where hex_moving == 0
MIN_PASS_ROWS = 25              # an X needs one run this long to be stable; short revisits are kept
COMPLETE_TOL_UM = 3.0           # pass is complete if Z reached the top of the window within this (um)
EXTEND_TOL_MM = 0.005           # data outside the window by more than this extends the axes (mm)
SUMMARY_COLS = 3


# ------------------------------------------------------------------- passes
def split_passes(snake: pd.DataFrame):
    """Combine all stable sweeps at each rounded (1 um) stage X, in first-visit order."""
    x_um = np.rint(snake[COL_X].to_numpy() * 1000).astype(np.int64)
    if not x_um.size:
        return [], []
    if np.unique(x_um).size == 1:
        return [(np.arange(len(x_um)), int(x_um[0]))], []
    brk = np.flatnonzero(np.diff(x_um) != 0) + 1
    starts = np.r_[0, brk]
    ends = np.r_[brk, len(x_um)]
    runs = []
    for s, e in zip(starts, ends):
        runs.append((np.arange(s, e), int(x_um[s])))
    stable_x = {x for idx, x in runs if len(idx) >= MIN_PASS_ROWS}
    transient = [(idx, x) for idx, x in runs if x not in stable_x]
    grouped = {}
    for idx, x in runs:
        if x in stable_x:
            grouped.setdefault(x, []).append(idx)
    return [(np.concatenate(indices), x) for x, indices in grouped.items()], transient


def pass_span(snake, idx):
    sub = snake.iloc[idx]
    y, z = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy()
    return round(float(np.ptp(y)), 3), round(float(np.ptp(z)), 3)


def analyze_pass(snake, idx, x_um, span_y, span_z, ybin_mm, zbin_mm, followed):
    sub = snake.iloc[idx]
    y, z = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy()
    # The snake starts at the window's (Ymin, Zmin) corner, so the first row anchors the window.
    ylo, zlo = y[0], z[0]
    yhi, zhi = ylo + span_y, zlo + span_z
    warn = []
    if y.min() < ylo - EXTEND_TOL_MM or y.max() > yhi + EXTEND_TOL_MM:
        warn.append(f"Y data [{y.min():.4f}, {y.max():.4f}] outside window [{ylo:.4f}, {yhi:.4f}]; axes extended")
        ylo, yhi = min(ylo, y.min()), max(yhi, y.max())
    if z.min() < zlo - EXTEND_TOL_MM or z.max() > zhi + EXTEND_TOL_MM:
        warn.append(f"Z data [{z.min():.4f}, {z.max():.4f}] outside window [{zlo:.4f}, {zhi:.4f}]; axes extended")
        zlo, zhi = min(zlo, z.min()), max(zhi, z.max())

    res = single.analyze_sweep(sub, ybin_mm, zbin_mm, bounds=(ylo, yhi, zlo, zhi))
    res.update({
        "x_um": x_um, "z_rows_expected": int(round(span_z / zbin_mm)) + 1,
        # A pass the stage moved on from is finished; only the last pass can be cut short.
        "complete": bool(followed or z.max() - z[0] >= span_z - COMPLETE_TOL_UM / 1000.0),
        "warn": warn,
    })
    return res


# -------------------------------------------------------------------- plotting
def pass_norm(res, glob_norm, scale, log):
    if scale == "panel":
        return make_norm(res["vmin"], res["vmax"], log, res["pos_min"])
    return glob_norm


def pass_title(res, small=False):
    if res["complete"] is None:
        tag = "  completion unknown (measured bounds)"
    else:
        tag = "" if res["complete"] else f"  PARTIAL/INCOMPLETE ({res['z_rows_filled']}/{res['z_rows_expected']} Z rows)"
    head = f"X {res['offset']:+d} \u00b5m" + (f" (#{res['dup']})" if res["dup"] > 1 else "") + tag
    body = single.peak_title(res)
    return head + "\n" + body


def stem_for(res, stem):
    dup = f"_n{res['dup']}" if res["dup"] > 1 else ""
    return f"{stem}_heatmap_X{res['offset']:+d}um{dup}.png"


def plot_individual(results, out_dir, stem, glob_norm, scale, log):
    paths = []
    for res in results:
        norm = pass_norm(res, glob_norm, scale, log)
        p = out_dir / stem_for(res, stem)
        paths.append(single.save_heatmap(res, p, res["stage_title"] + "\n" + pass_title(res), norm, log))
    return paths


def plot_summary(results, out_dir, stem, glob_norm, scale, log):
    panels = sorted(results, key=lambda r: (r["offset"], r["dup"]))
    if SHOW_MISSING_EXPECTED:
        have = {r["offset"] for r in results}
        panels += [{"missing": True, "offset": o, "dup": 1} for o in EXPECTED_OFFSETS_UM if o not in have]
        panels.sort(key=lambda r: (r["offset"], r["dup"]))
    n = len(panels)
    cols = min(SUMMARY_COLS, n)
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.6 * cols + 1.5, 4.6 * rows), squeeze=False,
                             constrained_layout=True)
    flat = axes.ravel()
    last_im = None
    for ax, res in zip(flat, panels):
        if res.get("missing"):
            ax.text(0.5, 0.5, "not scanned", ha="center", va="center", color="gray", transform=ax.transAxes)
            ax.set_title(f"X {res['offset']:+d} \u00b5m", fontsize=8)
            ax.set_xticks([])
            ax.set_yticks([])
            continue
        norm = pass_norm(res, glob_norm, scale, log)
        im = draw_heatmap(ax, res, norm, log, small=True)
        ax.set_title(pass_title(res), fontsize=7)
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
    fig.suptitle(f"First light, max voltage per Y/Z bin - {stem}\n"
                 f"{results[0]['stage_title']}", fontsize=10)
    p = out_dir / f"{stem}_heatmap_summary.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


def plot_peak_vs_x(results, out_dir, stem):
    rs = sorted(results, key=lambda r: (r["offset"], r["dup"]))
    x = [r["offset"] for r in rs]
    fig, axs = plt.subplots(3, 1, figsize=(7, 8), sharex=True, constrained_layout=True)
    series = [("peak_v", f"peak voltage ({V_UNIT})"), ("peak_y", "peak hexapod Y (mm)"),
              ("peak_z", "peak hexapod Z (mm)")]
    for ax, (key, label) in zip(axs, series):
        ax.plot(x, [r[key] for r in rs], "-", color="gray", lw=1)
        for r in rs:
            ax.plot(r["offset"], r[key], "o", color="C0", mfc="C0" if r["complete"] else "white", ms=7)
        ax.set_ylabel(label)
        ax.grid(alpha=0.3)
    axs[-1].set_xlabel("X offset from first pass (\u00b5m)")
    axs[0].set_title("Peak vs X (hollow marker = partial/incomplete pass)\n"
                     + results[0]["stage_title"])
    p = out_dir / f"{stem}_peak_vs_X.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return p


# ------------------------------------------------------------------------ main
def pick_csv():
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        f = filedialog.askopenfilename(title="Select Trawler data CSV", filetypes=[("CSV", "*.csv")])
        root.destroy()
        return Path(f) if f else None
    except Exception:
        return None


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    ap.add_argument("--y-bin-um", type=single.positive_float, default=Y_BIN_UM)
    ap.add_argument("--z-bin-um", type=single.positive_float, default=Z_BIN_UM)
    ap.add_argument("--half-window-mm", type=single.positive_float, default=HALF_WINDOW_MM,
                    help="legacy option; lone X slices now use measured bounds")
    ap.add_argument("--scale", choices=["shared", "panel"], default=SCALE)
    ap.add_argument("--log", action="store_true", default=LOG_SCALE)
    a = ap.parse_args(argv)

    path = Path(a.csv) if a.csv else (Path(CSV_PATH) if CSV_PATH else pick_csv())
    if path is None or not path.is_file():
        print(f"No CSV found: {path}")
        return 1
    out_dir = Path(a.out_dir) if a.out_dir else (Path(OUT_DIR) if OUT_DIR else path.parent / "heatmaps")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    print(f"Reading {path}")
    try:
        df, tail_bytes = single.load_csv(path, columns=USECOLS)
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

    try:
        snake, counts, breakdown = filter_snake(df, require_moving=REQUIRE_MOVING)
    except ValueError as e:
        print(f"Nothing to plot: {e}")
        return 1
    print("\n== Row filters ==")
    for k, v in counts.items():
        print(f"  {k:<62}{v:>10}")
    print("  Non-snake rows by script_cmd type:")
    for k, v in breakdown.items():
        print(f"    {k:<60}{v:>10}")
    if snake.empty:
        cmds = df[COL_CMD].fillna("<blank>").str.slice(0, 80).value_counts().head(25)
        print(f"\nNo snake rows left (looking for '{SNAKE_TOKEN}' in script_cmd). script_cmd strings in file:")
        print(cmds.to_string())
        return 1

    runs, transient = split_passes(snake)
    if transient:
        print(f"\n  Dropped {len(transient)} transient X run(s) shorter than {MIN_PASS_ROWS} rows "
              f"({sum(len(i) for i, _ in transient)} rows): X um values {sorted({x for _, x in transient})[:10]}")
    if not runs:
        print("No stable X pass found (every X run was shorter than MIN_PASS_ROWS).")
        return 1

    # Preserve expected-window handling for multiple X slices; a lone slice uses measured bounds.
    if len(runs) > 1:
        first_idx = runs[0][0]
        gaps = np.flatnonzero(np.diff(first_idx) != 1)
        first_pass = first_idx[:int(gaps[0]) + 1] if gaps.size else first_idx
        span_y, span_z = pass_span(snake, first_pass)
        print(f"\nSearch window from first completed pass: Y {span_y:.3f} mm x Z {span_z:.3f} mm")
    else:
        snake, _, _ = filter_snake(df, require_moving=REQUIRE_MOVING, include_all_readings=True)
        runs, _ = split_passes(snake)
        print("\nOnly one stage X; combining all pre-RBA readings using measured bounds "
              "(--half-window-mm unused).")

    base = runs[0][1]
    results = []
    for idx, x_um in runs:
        if len(runs) == 1:
            res = single.analyze_sweep(snake.iloc[idx], a.y_bin_um / 1000, a.z_bin_um / 1000)
            res.update({"x_um": x_um, "complete": None, "z_rows_expected": res["nz"],
                        "warn": ["Scan completion unknown: showing all measured sweeps."]})
        else:
            res = analyze_pass(snake, idx, x_um, span_y, span_z, a.y_bin_um / 1000, a.z_bin_um / 1000,
                               followed=idx[-1] < len(snake) - 1)
        res["offset"] = x_um - base
        res["stage_title"] = stage_title
        res["dup"] = 1
        results.append(res)

    print(f"\n== Passes (first pass X = {runs[0][1] / 1000:.3f} mm is offset 0) ==")
    print(f"  {'offset':>7} {'rows':>7} {'Z rows':>12} {'Y range (mm)':>24} {'Z range (mm)':>24}  status")
    for r in results:
        zr = f"{r['z_rows_filled']}/{r['z_rows_expected']}"
        status = "completion unknown" if r["complete"] is None else (
            "complete" if r["complete"] else "PARTIAL/INCOMPLETE")
        print(f"  {r['offset']:>+5d}um {r['rows']:>7} {zr:>12} "
              f"[{r['y_min']:+9.4f},{r['y_max']:+9.4f}] [{r['z_min']:+9.4f},{r['z_max']:+9.4f}]  "
              f"{status}")
        for w in r["warn"]:
            print(f"    WARNING offset {r['offset']:+d}: {w}")
        if r["y_step_um"] > a.y_bin_um * 1.5:
            print(f"    NOTE offset {r['offset']:+d}: median Y sample spacing is {r['y_step_um']:.1f} um but "
                  f"Y bin is {a.y_bin_um:g} um, so the map will look streaky; try --y-bin-um {r['y_step_um']:.0f}")
    found = sorted({r["offset"] for r in results})
    print(f"  Offsets found:    {found}")
    print(f"  Offsets expected: {sorted(EXPECTED_OFFSETS_UM)}"
          f"  missing: {[o for o in EXPECTED_OFFSETS_UM if o not in found]}"
          f"  unexpected: {[o for o in found if o not in EXPECTED_OFFSETS_UM]}")
    vmin, vmax = min(r["vmin"] for r in results), max(r["vmax"] for r in results)
    pos = [r["pos_min"] for r in results if np.isfinite(r["pos_min"])]
    pos_min = min(pos) if pos else np.nan
    if a.log and not np.isfinite(pos_min):
        print("WARNING: no positive voltages; log scale unavailable, using linear.")
    glob_norm = make_norm(vmin, vmax, a.log, pos_min)

    paths = plot_individual(results, out_dir, stem, glob_norm, a.scale, a.log)
    paths.append(plot_summary(results, out_dir, stem, glob_norm, a.scale, a.log))
    paths.append(plot_peak_vs_x(results, out_dir, stem))

    table = pd.DataFrame([{
        "pass_index": i + 1, "x_offset_um": r["offset"], "x_abs_mm": r["x_mm"], "rows_used": r["rows"],
        "peak_V": r["peak_v"] / V_SCALE, "peak_Y_mm": r["peak_y"], "peak_Z_mm": r["peak_z"],
        "peak_Y_rel_mm": r["peak_y"] - r["ylo"], "peak_Z_rel_mm": r["peak_z"] - r["zlo"],
        "complete": r["complete"], "z_rows_filled": r["z_rows_filled"], "z_rows_expected": r["z_rows_expected"],
        "y_min_mm": r["y_min"], "y_max_mm": r["y_max"], "z_min_mm": r["z_min"], "z_max_mm": r["z_max"],
        "t_start_s": r["t0"], "t_end_s": r["t1"],
    } for i, r in enumerate(results)])
    csv_out = out_dir / f"{stem}_pass_results.csv"
    table.to_csv(csv_out, index=False)
    paths.append(csv_out)

    print(f"\n== Saved to {out_dir} ==")
    for p in paths:
        print(f"  {p.relative_to(out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
