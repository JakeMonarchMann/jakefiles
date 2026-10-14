"""First-light heatmaps from a Trawler data CSV: one Y/Z heatmap per HEXSTAGE X pass.

Usage:
    python first_light_heatmap.py [data.csv] [--out-dir DIR] [--y-bin-um 1] [--z-bin-um 1]
                                  [--half-window-mm 0.5] [--scale shared|panel] [--log]

With no CSV argument a file picker opens. Outputs go in a heatmaps folder next to the CSV
unless --out-dir is given, in which case that folder is used.
Safe on empty, header-only, partially written, or unfinished CSVs.
"""
import argparse
import io
import sys
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LogNorm, Normalize

# ---------------------------------------------------------------- parameters
CSV_PATH = None                 # set a path here to skip the file picker / CLI argument
OUT_DIR = None                  # None = next to the CSV
Y_BIN_UM = 1.0                  # heatmap bin size in hexapod Y (um)
Z_BIN_UM = 1.0                  # heatmap bin size in hexapod Z (um)
HALF_WINDOW_MM = 0.5            # fallback window half-size (mm), used only until a second X pass shows the real one
SCALE = "shared"                # "shared" = one color scale for all passes, "panel" = per-panel scale
LOG_SCALE = False               # log color scale (signal may be ~1 mV or less)
EXPECTED_OFFSETS_UM = [-30, -24, -18, -12, -6, 0, 6, 12, 18, 24, 30]   # X offsets the script should visit
SHOW_MISSING_EXPECTED = True    # leave an empty "not scanned" panel for expected offsets with no data
SNAKE_TOKEN = "MOVEINC"         # script_cmd text that marks snake rows
REQUIRE_MOVING = False          # True = also drop snake rows where hex_moving == 0
MIN_PASS_ROWS = 25              # an X run shorter than this (rows, 25 Hz) is treated as a transient
COMPLETE_TOL_UM = 3.0           # pass is complete if Z reached the top of the window within this (um)
EXTEND_TOL_MM = 0.005           # data outside the window by more than this extends the axes (mm)
DISPLAY_MAX_PX = 500            # heatmaps are max-pooled to at most this many pixels per side for display
SUMMARY_COLS = 3
CMAP = "viridis"
V_UNIT, V_SCALE = "mV", 1e3     # display unit for voltage

COL_T, COL_CMD, COL_MOV = "time_elapsed_s", "script_cmd", "hex_moving"
COL_X, COL_Y, COL_Z, COL_V = "shrc_hex_X_mm", "hex_Y_mm", "hex_Z_mm", "oscope_voltage_V"
NUMERIC = [COL_T, COL_MOV, COL_X, COL_Y, COL_Z, COL_V]
USECOLS = NUMERIC + [COL_CMD]
DROP_CATEGORIES = ["MOVEABS", "GOTOMAX", "MOVEUNTIL"]


# ------------------------------------------------------------------- loading
def load_csv(path: Path):
    """Return (df, info). df is None when there is nothing usable."""
    data = path.read_bytes()
    cut = data.rfind(b"\n")
    if cut == -1:
        return None, {"reason": "no complete line in file (empty or header still being written)"}
    tail_bytes = len(data) - (cut + 1)  # bytes of an unterminated last line, dropped
    buf = io.BytesIO(data[: cut + 1])
    header = pd.read_csv(buf, nrows=0, encoding_errors="replace").columns
    missing = [c for c in USECOLS if c not in header]
    if missing:
        return None, {"reason": f"missing columns {missing}; file has {len(header)} columns, "
                                f"e.g. {list(header[:12])}"}
    buf.seek(0)
    df = pd.read_csv(buf, usecols=USECOLS, dtype=str, on_bad_lines="skip", encoding_errors="replace")
    return df, {"tail_bytes": tail_bytes}


def filter_snake(df: pd.DataFrame):
    """Apply the row filters in order; return (snake_df, counts, drop_breakdown)."""
    counts = {"rows read": len(df)}
    for c in NUMERIC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    cmd = df[COL_CMD].fillna("").str.strip()
    up = cmd.str.upper()
    is_snake = up.str.contains(SNAKE_TOKEN, regex=False)

    breakdown = {}
    non = up[~is_snake]
    assigned = pd.Series(False, index=non.index)
    for cat in DROP_CATEGORIES:
        hit = non.str.contains(cat, regex=False) & ~assigned
        breakdown[f"script_cmd has {cat}"] = int(hit.sum())
        assigned |= hit
    blank = (non == "") & ~assigned
    breakdown["script_cmd blank"] = int(blank.sum())
    assigned |= blank
    breakdown["other script_cmd (DWELL, variable lines, ...)"] = int((~assigned).sum())
    counts["dropped: not a snake (MOVEINC) row"] = int((~is_snake).sum())

    snake = df[is_snake]
    counts["snake rows"] = len(snake)
    for c, label in [(COL_V, "oscope_voltage_V"), (COL_Y, "hex_Y_mm"), (COL_Z, "hex_Z_mm"),
                     (COL_X, "shrc_hex_X_mm")]:
        counts[f"  of which blank {label}"] = int(snake[c].isna().sum())
    valid = snake[[COL_V, COL_Y, COL_Z, COL_X]].notna().all(axis=1)
    counts["dropped: blank V/Y/Z/X"] = int((~valid).sum())
    snake = snake[valid]
    counts["snake rows with hex_moving == 0 (kept unless REQUIRE_MOVING)"] = int((snake[COL_MOV] == 0).sum())
    if REQUIRE_MOVING:
        keep = snake[COL_MOV].fillna(0) != 0
        counts["dropped: hex_moving == 0"] = int((~keep).sum())
        snake = snake[keep]

    t = snake[COL_T].to_numpy()
    tt = t[~np.isnan(t)]
    if len(tt) > 1 and (np.diff(tt) < 0).any():
        print("WARNING: time_elapsed_s is not monotonic; sorting snake rows by time.")
        snake = snake.sort_values(COL_T, kind="mergesort")
    return snake.reset_index(drop=True), counts, breakdown


# ------------------------------------------------------------------- passes
def split_passes(snake: pd.DataFrame):
    """Group snake rows into X passes by rounded (1 um) stage X, in time order."""
    x_um = np.rint(snake[COL_X].to_numpy() * 1000).astype(np.int64)
    brk = np.flatnonzero(np.diff(x_um) != 0) + 1
    starts = np.r_[0, brk]
    ends = np.r_[brk, len(x_um)]
    kept, transient = [], []
    for s, e in zip(starts, ends):
        (kept if e - s >= MIN_PASS_ROWS else transient).append((np.arange(s, e), int(x_um[s])))
    merged = []
    for idx, x in kept:
        if merged and merged[-1][1] == x:  # same X resumed after a transient blip
            merged[-1] = (np.concatenate([merged[-1][0], idx]), x)
        else:
            merged.append((idx, x))
    return merged, transient


def pass_span(snake, idx):
    sub = snake.iloc[idx]
    y, z = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy()
    return round(float(np.ptp(y)), 3), round(float(np.ptp(z)), 3)


def analyze_pass(snake, idx, x_um, span_y, span_z, ybin_mm, zbin_mm, followed):
    sub = snake.iloc[idx]
    y, z, v = sub[COL_Y].to_numpy(), sub[COL_Z].to_numpy(), sub[COL_V].to_numpy() * V_SCALE
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

    ny = int(round((yhi - ylo) / ybin_mm)) + 1
    nz = int(round((zhi - zlo) / zbin_mm)) + 1
    iy = np.clip(np.rint((y - ylo) / ybin_mm).astype(np.int64), 0, ny - 1)
    iz = np.clip(np.rint((z - zlo) / zbin_mm).astype(np.int64), 0, nz - 1)
    cell = pd.Series(v).groupby(iz * ny + iy).max()
    grid = np.full((nz, ny), np.nan)
    grid.flat[cell.index.to_numpy()] = cell.to_numpy()

    k = int(np.argmax(v))
    dy = np.abs(np.diff(y))
    dy = dy[(dy > 1e-6) & (dy < 0.05)]
    pos = grid[grid > 0]
    return {
        "x_um": x_um, "rows": len(sub), "grid": grid, "ylo": ylo, "yhi": yhi, "zlo": zlo, "zhi": zhi,
        "ybin": ybin_mm, "zbin": zbin_mm, "ny": ny, "nz": nz,
        "peak_v": float(v[k]), "peak_y": float(y[k]), "peak_z": float(z[k]),
        "y_min": float(y.min()), "y_max": float(y.max()), "z_min": float(z.min()), "z_max": float(z.max()),
        "t0": float(sub[COL_T].iloc[0]), "t1": float(sub[COL_T].iloc[-1]),
        "x_mm": float(sub[COL_X].median()),
        "z_rows_filled": int(np.unique(iz).size), "z_rows_expected": int(round(span_z / zbin_mm)) + 1,
        # A pass the stage moved on from is finished; only the last pass can be cut short.
        "complete": bool(followed or z.max() - z[0] >= span_z - COMPLETE_TOL_UM / 1000.0),
        "vmin": float(np.nanmin(grid)), "vmax": float(np.nanmax(grid)),
        "pos_min": float(pos.min()) if pos.size else np.nan,
        "y_step_um": float(np.median(dy) * 1000) if dy.size else np.nan,
        "warn": warn,
    }


# -------------------------------------------------------------------- plotting
def max_pool(grid, max_px):
    """Shrink for display while keeping the max of each block, so single hot bins survive."""
    nz, ny = grid.shape
    fz, fy = -(-nz // max_px), -(-ny // max_px)
    if fz == 1 and fy == 1:
        return grid, 1, 1
    pz, py = -(-nz // fz) * fz, -(-ny // fy) * fy
    padded = np.full((pz, py), np.nan)
    padded[:nz, :ny] = grid
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out = np.nanmax(padded.reshape(pz // fz, fz, py // fy, fy), axis=(1, 3))
    return out, fz, fy


def make_norm(vmin, vmax, log, pos_min):
    if log and np.isfinite(pos_min):
        vmin = pos_min
    if not vmax > vmin:
        vmax = vmin + abs(vmin) * 1e-3 + 1e-9
    return LogNorm(vmin=vmin, vmax=vmax) if log and np.isfinite(pos_min) else Normalize(vmin=vmin, vmax=vmax)


def pass_norm(res, glob_norm, scale, log):
    if scale == "panel":
        return make_norm(res["vmin"], res["vmax"], log, res["pos_min"])
    return glob_norm


def draw_heatmap(ax, res, norm, log, small=False):
    grid = np.where(res["grid"] > 0, res["grid"], np.nan) if log else res["grid"]
    pooled, fz, fy = max_pool(grid, DISPLAY_MAX_PX)
    cmap = plt.get_cmap(CMAP).copy()
    cmap.set_bad("white")
    x0 = res["ylo"] - res["ybin"] / 2
    z0 = res["zlo"] - res["zbin"] / 2
    ext = [x0, x0 + pooled.shape[1] * fy * res["ybin"], z0, z0 + pooled.shape[0] * fz * res["zbin"]]
    im = ax.imshow(pooled, origin="lower", extent=ext, aspect="equal", cmap=cmap, norm=norm,
                   interpolation="nearest")
    ax.set_xlim(res["ylo"], res["yhi"])
    ax.set_ylim(res["zlo"], res["zhi"])
    ax.scatter([res["peak_y"]], [res["peak_z"]], marker="*", s=90 if small else 160, c="red",
               edgecolors="white", linewidths=0.8, zorder=5)
    return im


def pass_title(res, small=False):
    tag = "" if res["complete"] else f"  PARTIAL/INCOMPLETE ({res['z_rows_filled']}/{res['z_rows_expected']} Z rows)"
    head = f"X {res['offset']:+d} \u00b5m" + (f" (#{res['dup']})" if res["dup"] > 1 else "") + tag
    body = f"best {res['peak_v']:.4g} {V_UNIT} at Y {res['peak_y']:+.4f} mm, Z {res['peak_z']:+.4f} mm"
    return head + "\n" + body


def stem_for(res, stem):
    dup = f"_n{res['dup']}" if res["dup"] > 1 else ""
    return f"{stem}_heatmap_X{res['offset']:+d}um{dup}.png"


def plot_individual(results, out_dir, stem, glob_norm, scale, log):
    paths = []
    for res in results:
        norm = pass_norm(res, glob_norm, scale, log)
        fig, ax = plt.subplots(figsize=(8, 7), constrained_layout=True)
        im = draw_heatmap(ax, res, norm, log)
        ax.set_title(pass_title(res), fontsize=10)
        ax.set_xlabel("Hexapod Y (mm)")
        ax.set_ylabel("Hexapod Z (mm)")
        fig.colorbar(im, ax=ax, label=f"max scope voltage per bin ({V_UNIT})")
        p = out_dir / stem_for(res, stem)
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)
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
    fig.suptitle(f"First light, max voltage per Y/Z bin - {stem}", fontsize=10)
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
    axs[0].set_title("Peak vs X (hollow marker = partial/incomplete pass)")
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
    ap.add_argument("--y-bin-um", type=float, default=Y_BIN_UM)
    ap.add_argument("--z-bin-um", type=float, default=Z_BIN_UM)
    ap.add_argument("--half-window-mm", type=float, default=HALF_WINDOW_MM)
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
        df, info = load_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        print(f"Could not parse CSV: {e}")
        return 1
    if df is None:
        print(f"Nothing to plot: {info['reason']}")
        return 1
    if info["tail_bytes"]:
        print(f"Dropped unterminated last line ({info['tail_bytes']} bytes) - file looks mid-write.")
    if df.empty:
        print("Nothing to plot: CSV has a header but no data rows yet.")
        return 1

    snake, counts, breakdown = filter_snake(df)
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

    # Window size comes from the first completed pass (any pass with a later pass after it);
    # with a single pass there is nothing to measure, so fall back to --half-window-mm.
    if len(runs) > 1:
        span_y, span_z = pass_span(snake, runs[0][0])
        print(f"\nSearch window from first completed pass: Y {span_y:.3f} mm x Z {span_z:.3f} mm")
    else:
        span_y = span_z = round(2 * a.half_window_mm, 3)
        print(f"\nOnly one pass so far; assuming a {span_y:.3f} mm square window (--half-window-mm).")

    base = runs[0][1]
    results, seen = [], {}
    for i, (idx, x_um) in enumerate(runs):
        res = analyze_pass(snake, idx, x_um, span_y, span_z, a.y_bin_um / 1000, a.z_bin_um / 1000,
                           followed=i < len(runs) - 1)
        res["offset"] = x_um - base
        seen[res["offset"]] = seen.get(res["offset"], 0) + 1
        res["dup"] = seen[res["offset"]]
        results.append(res)

    print(f"\n== Passes (first pass X = {runs[0][1] / 1000:.3f} mm is offset 0) ==")
    print(f"  {'offset':>7} {'rows':>7} {'Z rows':>12} {'Y range (mm)':>24} {'Z range (mm)':>24}  status")
    for r in results:
        zr = f"{r['z_rows_filled']}/{r['z_rows_expected']}"
        print(f"  {r['offset']:>+5d}um {r['rows']:>7} {zr:>12} "
              f"[{r['y_min']:+9.4f},{r['y_max']:+9.4f}] [{r['z_min']:+9.4f},{r['z_max']:+9.4f}]  "
              f"{'complete' if r['complete'] else 'PARTIAL/INCOMPLETE'}")
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
    dups = sorted(o for o, n in seen.items() if n > 1)
    if dups:
        print(f"  NOTE: offsets seen in more than one separate pass: {dups}")

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
