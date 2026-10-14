"""One first-light Y/Z heatmap from a Trawler CSV, without splitting by stage X.

Usage:
    python single_x_heatmap.py [data.csv] [--out-dir DIR]
                                  [--y-bin-um 1] [--z-bin-um 1] [--log]

With no CSV argument, a file picker opens. Outputs default to a heatmaps folder
next to the CSV: one PNG and one peak-results CSV.

All HEXAPOD MOVEINC sweeps at one stage X are combined, including later sweeps
and spirals. Revisits retain the highest voltage per bin. Multiple stage X
values (rounded to 1 um) require multi_x_heatmaps.py instead.
Axes use measured scan bounds (unfinished scans show only the area measured).
Individual PNG dimensions follow the scan proportions, with padded tight
cropping to preserve titles, axis labels, and the colorbar.
The title reports PACKAGE X/Y and HEXSTAGE X/Y/Z from the last settled,
fully populated row before the first hexapod move, plus the nominal hexapod
pose. These are recorded readbacks, not inferred coordinate transformations.
An unterminated final CSV line is ignored.
This module also provides CSV loading, filtering, binning, and heatmap rendering
for the multi-scan multi_x_heatmaps.py entry point.
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
from matplotlib.colors import LogNorm, Normalize

CSV_PATH = None
OUT_DIR = None
Y_BIN_UM = 1.0
Z_BIN_UM = 1.0
LOG_SCALE = False
DISPLAY_MAX_PX = 500
CMAP = "viridis"
V_UNIT, V_SCALE = "mV", 1e3
COL_T, COL_CMD, COL_MOV = "time_elapsed_s", "script_cmd", "hex_moving"
COL_X, COL_Y, COL_Z, COL_V = "shrc_hex_X_mm", "hex_Y_mm", "hex_Z_mm", "oscope_voltage_V"
NUMERIC = [COL_T, COL_MOV, COL_X, COL_Y, COL_Z, COL_V]
SNAKE_TOKEN = "MOVEINC"
DROP_CATEGORIES = ["MOVEABS", "GOTOMAX", "MOVEUNTIL"]

PACKAGE_COLS = ["shrc_pkg_X_mm", "shrc_pkg_Y_mm"]
HEXSTAGE_COLS = ["shrc_hex_X_mm", "shrc_hex_Y_mm", "shrc_hex_Z_mm"]
HEXAPOD_COLS = ["hex_X_mm", COL_Y, COL_Z]
NOMINAL_COLS = PACKAGE_COLS + HEXSTAGE_COLS + HEXAPOD_COLS
BASE_USECOLS = NUMERIC + [COL_CMD] + HEXSTAGE_COLS[1:]
USECOLS = list(dict.fromkeys(BASE_USECOLS + NOMINAL_COLS))


def load_csv(path: Path, columns: list[str] = USECOLS) -> tuple[pd.DataFrame, int]:
    data = path.read_bytes()
    cut = data.rfind(b"\n")
    if cut == -1:
        raise ValueError("CSV has no complete line (empty or still being written).")
    buf = io.BytesIO(data[:cut + 1])
    header = pd.read_csv(buf, nrows=0, encoding_errors="replace").columns
    missing = [col for col in columns if col not in header]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    buf.seek(0)
    df = pd.read_csv(buf, usecols=columns, dtype=str, on_bad_lines="warn",
                     encoding_errors="replace")
    for col in columns:
        if col != COL_CMD:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)
    return df, len(data) - cut - 1


def starting_stage_title(df: pd.DataFrame) -> str:
    """Use initial stage readbacks, not the first pass's offset reference."""
    cmd = df[COL_CMD].fillna("").str.upper().str.strip()
    scan_rows = np.flatnonzero(cmd.str.match(r"HEXAPOD\s+MOVEINC\b").to_numpy())
    initial = df.iloc[:int(scan_rows[0]) + 1] if scan_rows.size else df
    stage = initial[HEXSTAGE_COLS].apply(pd.to_numeric, errors="coerce")
    valid = stage[np.isfinite(stage.to_numpy()).all(axis=1)]
    if valid.empty:
        raise ValueError("No complete starting HEXSTAGE X/Y/Z readbacks before the scan.")
    start = valid.iloc[0]
    positions = ", ".join(f"{axis} {start[col]:+.4f}" for axis, col in zip("XYZ", HEXSTAGE_COLS))
    return f"Starting HEXSTAGE: {positions} mm"


def filter_snake(df: pd.DataFrame, require_moving: bool = False):
    """Keep all hexapod incremental sweeps; report discarded rows explicitly."""
    df = df.copy()
    counts = {"rows read": len(df)}
    for col in NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    up = df[COL_CMD].fillna("").str.strip().str.upper()
    is_snake = up.str.match(r"HEXAPOD\s+MOVEINC\b")
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
    for col in [COL_V, COL_Y, COL_Z, COL_X]:
        counts[f"  of which blank {col}"] = int(snake[col].isna().sum())
    valid = snake[[COL_V, COL_Y, COL_Z, COL_X]].notna().all(axis=1)
    counts["dropped: blank V/Y/Z/X"] = int((~valid).sum())
    snake = snake[valid]
    counts["snake rows with hex_moving == 0 (kept unless REQUIRE_MOVING)"] = int((snake[COL_MOV] == 0).sum())
    if require_moving:
        keep = snake[COL_MOV].fillna(0) != 0
        counts["dropped: hex_moving == 0"] = int((~keep).sum())
        snake = snake[keep]
    tt = snake[COL_T].dropna().to_numpy()
    if len(tt) > 1 and (np.diff(tt) < 0).any():
        print("WARNING: time_elapsed_s is not monotonic; sorting snake rows by time.")
        snake = snake.sort_values(COL_T, kind="mergesort")
    return snake.reset_index(drop=True), counts, breakdown


def select_sweep(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Combine all sweeps at a single stage X and capture pre-move readbacks."""
    cmd = df[COL_CMD].fillna("").str.upper().str.strip()
    moves = cmd.str.match(r"HEXAPOD\s+(?:MOVEABS|MOVEINC|MOVEUNTIL|GOTOMAX)\b")
    increments = cmd.str.match(r"HEXAPOD\s+MOVEINC\b")
    if not increments.any():
        raise ValueError("No HEXAPOD MOVEINC sweep rows found.")
    first_move = int(np.flatnonzero(moves.to_numpy())[0])
    before = df.iloc[:first_move]
    settled = before[COL_MOV].eq(0)
    valid = before[NOMINAL_COLS].notna().all(axis=1)
    nominal_rows = before[settled & valid]
    if nominal_rows.empty:
        raise ValueError("No settled nominal stage/hexapod readbacks before the first move.")
    nominal = nominal_rows.iloc[-1]

    sweep, counts, breakdown = filter_snake(df)
    print("\n== Row filters (all sweeps) ==")
    for label, count in counts.items():
        print(f"  {label}: {count}")
    for label, count in breakdown.items():
        if count:
            print(f"  {label}: {count}")
    if sweep.empty:
        raise ValueError("No usable voltage/Y/Z/X samples in the sweeps.")
    if np.unique(np.rint(sweep[COL_X].to_numpy() * 1000)).size > 1:
        raise ValueError("Multiple stage X values found; use multi_x_heatmaps.py.")
    return sweep, nominal


def analyze_sweep(sweep: pd.DataFrame, ybin_mm: float, zbin_mm: float,
                  bounds: tuple[float, float, float, float] | None = None) -> dict:
    """Bin one X slice; multi-scan callers may supply an expected scan window."""
    if sweep.empty:
        raise ValueError("Cannot analyze an empty sweep.")
    if not all(np.isfinite(size) and size > 0 for size in (ybin_mm, zbin_mm)):
        raise ValueError("Bin sizes must be finite and greater than zero.")
    y = sweep[COL_Y].to_numpy()
    z = sweep[COL_Z].to_numpy()
    v = sweep[COL_V].to_numpy() * V_SCALE
    if bounds is None:
        ylo, zlo = float(y.min()), float(z.min())
        ny = int(np.ceil((y.max() - ylo) / ybin_mm)) + 1
        nz = int(np.ceil((z.max() - zlo) / zbin_mm)) + 1
        yhi = ylo + max(ny - 1, 1) * ybin_mm
        zhi = zlo + max(nz - 1, 1) * zbin_mm
    else:
        ylo, yhi, zlo, zhi = bounds
        ny = max(int(round((yhi - ylo) / ybin_mm)) + 1, 1)
        nz = max(int(round((zhi - zlo) / zbin_mm)) + 1, 1)
        yhi, zhi = max(yhi, ylo + ybin_mm), max(zhi, zlo + zbin_mm)
    iy = np.clip(np.rint((y - ylo) / ybin_mm).astype(np.int64), 0, ny - 1)
    iz = np.clip(np.rint((z - zlo) / zbin_mm).astype(np.int64), 0, nz - 1)
    cell = pd.Series(v).groupby(iz * ny + iy).max()
    grid = np.full((nz, ny), np.nan)
    grid.flat[cell.index.to_numpy()] = cell.to_numpy()
    k = int(np.argmax(v))
    positive = grid[grid > 0]
    dy = np.abs(np.diff(y))
    dy = dy[(dy > 1e-6) & (dy < 0.05)]
    return {
        "grid": grid, "rows": len(sweep), "ylo": ylo, "zlo": zlo,
        "yhi": yhi, "zhi": zhi,
        "ybin": ybin_mm, "zbin": zbin_mm, "ny": ny, "nz": nz,
        "peak_v": float(v[k]), "peak_y": float(y[k]), "peak_z": float(z[k]),
        "vmin": float(np.nanmin(grid)), "vmax": float(np.nanmax(grid)),
        "pos_min": float(positive.min()) if positive.size else np.nan,
        "y_min": float(y.min()), "y_max": float(y.max()),
        "z_min": float(z.min()), "z_max": float(z.max()),
        "t0": float(sweep[COL_T].iloc[0]), "t1": float(sweep[COL_T].iloc[-1]),
        "x_mm": float(sweep[COL_X].median()),
        "z_rows_filled": int(np.unique(iz).size),
        "y_step_um": float(np.median(dy) * 1000) if dy.size else np.nan,
    }


def max_pool(grid, max_px):
    """Shrink for display while keeping single hot bins."""
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


def save_heatmap(res: dict, path: Path, title: str, norm, log: bool) -> Path:
    """Save one heatmap, for either the standalone CLI or a multi-scan slice."""
    ratio = (res["yhi"] - res["ylo"]) / (res["zhi"] - res["zlo"])
    plot_width = max(2.0, 6.0 * min(ratio, 1.0))
    plot_height = max(2.0, 6.0 / max(ratio, 1.0))
    fig, ax = plt.subplots(figsize=(plot_width + 2.0, plot_height + 2.0),
                           constrained_layout=True)
    try:
        im = draw_heatmap(ax, res, norm, log)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("Hexapod Y (mm)")
        ax.set_ylabel("Hexapod Z (mm)")
        fig.colorbar(im, ax=ax, label=f"max scope voltage per bin ({V_UNIT})"
                     + (" - log" if log else ""))
        fig.savefig(path, dpi=130, bbox_inches="tight", pad_inches=0.2)
    finally:
        plt.close(fig)
    return path


def peak_title(res: dict) -> str:
    return (f"best {res['peak_v']:.4g} {V_UNIT} at "
            f"Y {res['peak_y']:+.4f} mm, Z {res['peak_z']:+.4f} mm")


def sweep_title(res: dict, nominal: pd.Series) -> str:
    def positions(cols: list[str]) -> str:
        return ", ".join(f"{axis} {nominal[col]:+.4f}" for axis, col in zip("XYZ", cols))

    return (
        "First light - Y/Z sweeps at one stage X\n"
        f"Nominal PACKAGE (hexapod centered): {positions(PACKAGE_COLS)} mm\n"
        f"Nominal HEXSTAGE: {positions(HEXSTAGE_COLS)} mm\n"
        f"Reference hexapod: {positions(HEXAPOD_COLS)} mm\n"
        + peak_title(res)
    )


def positive_float(value: str) -> float:
    number = float(value)
    if not np.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("bin size must be finite and greater than zero")
    return number


def pick_csv() -> Path | None:
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(title="Select single-sweep Trawler CSV",
                                             filetypes=[("CSV", "*.csv")])
    finally:
        root.destroy()
    return Path(selected) if selected else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    ap.add_argument("--y-bin-um", type=positive_float, default=Y_BIN_UM)
    ap.add_argument("--z-bin-um", type=positive_float, default=Z_BIN_UM)
    ap.add_argument("--log", action="store_true", default=LOG_SCALE)
    args = ap.parse_args(argv)
    try:
        path = Path(args.csv) if args.csv else (Path(CSV_PATH) if CSV_PATH else pick_csv())
        if path is None or not path.is_file():
            print(f"No CSV found: {path}", file=sys.stderr)
            return 1
        df, tail_bytes = load_csv(path)
        if tail_bytes:
            print(f"Dropped unterminated last line ({tail_bytes} bytes).")
        sweep, nominal = select_sweep(df)
        res = analyze_sweep(sweep, args.y_bin_um / 1000, args.z_bin_um / 1000)
        log = args.log and np.isfinite(res["pos_min"])
        if args.log and not log:
            print("WARNING: no positive voltages; using a linear color scale.")
        norm = make_norm(res["vmin"], res["vmax"], log, res["pos_min"])
        out_dir = Path(args.out_dir) if args.out_dir else (
            Path(OUT_DIR) if OUT_DIR else path.parent / "heatmaps")
        out_dir.mkdir(parents=True, exist_ok=True)
        png_out = out_dir / f"{path.stem}_single_sweep_heatmap.png"
        save_heatmap(res, png_out, sweep_title(res, nominal), norm, log)
        csv_out = out_dir / f"{path.stem}_single_sweep_results.csv"
        record = {
            "rows_used": res["rows"], "peak_V": res["peak_v"] / V_SCALE,
            "peak_Y_mm": res["peak_y"], "peak_Z_mm": res["peak_z"],
            "y_min_mm": res["y_min"], "y_max_mm": res["y_max"],
            "z_min_mm": res["z_min"], "z_max_mm": res["z_max"],
        }
        record.update({f"nominal_{col}": nominal[col] for col in NOMINAL_COLS})
        pd.DataFrame([record]).to_csv(csv_out, index=False)
    except (OSError, ValueError, pd.errors.ParserError, ImportError) as exc:
        print(f"Could not produce single-sweep heatmap: {exc}", file=sys.stderr)
        return 1
    print(f"\n{sweep_title(res, nominal)}\nRows used: {res['rows']}")
    print(f"\nSaved:\n  {png_out}\n  {csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
