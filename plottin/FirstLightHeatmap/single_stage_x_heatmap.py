"""One first-light Y/Z heatmap from a Trawler CSV, without splitting by stage X.

Usage:
    python single_stage_x_heatmap.py [data.csv] [--out-dir DIR]
                                  [--y-bin-um 1] [--z-bin-um 1] [--log]

With no CSV argument, a file picker opens. Outputs default to a heatmaps folder
next to the CSV: one PNG and one peak-results CSV.

All positioned readings from the first sweep until the first recorded RBA motion
are combined, including MOVEUNTIL spirals, dwells, and pre-RBA optimization.
The matching run .txt recipe resolves which optimizer moves Aligner1/Aligner2;
activation, tool setup, and axis configuration do not count as motion.
Stationary optimizer calibration is retained. Motion is a joint change greater
than 0.001 degree from the median of the last 25 complete pre-command samples.
The moving sample and everything afterward are excluded. Joint readbacks are
required if an RBA-motion command occurs. Revisits retain the highest voltage per bin. Multiple stage X
values (rounded to 1 um) require multi_stage_x_heatmaps.py instead.
Axes use measured scan bounds (unfinished scans show only the area measured).
Individual PNG dimensions follow the scan proportions, with padded tight
cropping to preserve titles, axis labels, and the colorbar.
Detected post-sweep spirals add a high-resolution second panel covering the
spiral's measured Y/Z extent with padding, using the same voltage scale.
The single-image title also reports the CSV-sampled maximum during the pre-RBA
hexapod XYZ optimizer and its recorded hexapod X/Y/Z coordinates, when present.
The title reports PACKAGE X/Y and HEXSTAGE X/Y/Z from the last settled,
fully populated row before the first hexapod move, plus the nominal hexapod
pose. These are recorded readbacks, not inferred coordinate transformations.
An unterminated final CSV line is ignored.
This module also provides CSV loading, filtering, binning, and heatmap rendering
for the multi-scan multi_stage_x_heatmaps.py and multi_hexapod_x_heatmaps.py
entry points.
"""

import argparse
import io
import re
import sys
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm, Normalize
from matplotlib.patches import Rectangle

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
ALIGNER_JOINT_COLS = [f"meca_Aligner{arm}_J{joint}_deg"
                      for arm in (1, 2) for joint in range(1, 7)]
RBA_MOTION_TOL_DEG = 0.001


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
    read_columns = list(columns)
    if "script_line" in header and "script_line" not in read_columns:
        read_columns.append("script_line")
    read_columns.extend(col for col in ALIGNER_JOINT_COLS
                        if col in header and col not in read_columns)
    df = pd.read_csv(buf, usecols=read_columns, dtype=str, on_bad_lines="warn",
                     encoding_errors="replace")
    for col in read_columns:
        if col != COL_CMD:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].replace([np.inf, -np.inf], np.nan)
    recipe = path.with_name(path.name.replace("_data_", "_")).with_suffix(".txt")
    if recipe.is_file():
        df.attrs["recipe_lines"] = recipe.read_text(encoding="utf-8-sig").splitlines()
    return df, len(data) - cut - 1


def rba_command_start(df: pd.DataFrame) -> int:
    """Return the first commanded RBA-motion row, resolving optimizer axes."""
    motion = r"(?:ALIGNER[12]\s+(?:MOVE\w*|GOTOMAX)\b|(?:TANDEM|OPPOSITE)\s+MOVE\w*\b)"
    cmd = df[COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()
    cutoff = cmd.str.match(motion).to_numpy()
    recipe_lines = df.attrs.get("recipe_lines")
    optimizers = cmd.str.match(r"OPTIMIZE2?\s+(?:MAXIMIZE|MINIMIZE)\b")
    if optimizers.any():
        if recipe_lines is None or "script_line" not in df:
            raise ValueError("Optimizer readings require the matching run .txt recipe and "
                             "CSV script_line to identify RBA motion safely.")
        rba_lines = set()
        axes = set()
        for number, text in enumerate(recipe_lines, 1):
            code = text.split("//", 1)[0].strip().upper()
            axis = re.match(r"OPTIMIZE\s+AXIS\s+(\w+)", code)
            if axis:
                axes.add(axis.group(1))
            if re.match(r"OPTIMIZE2?\s+(?:MAXIMIZE|MINIMIZE)\b", code):
                if not axes:
                    raise ValueError(f"Cannot identify optimizer axes at recipe line {number}.")
                if any(axis.startswith(("TANDEM_", "OPPOSITE_", "ALIGNER1_", "ALIGNER2_"))
                       for axis in axes):
                    rba_lines.add(number)
                axes.clear()
            elif re.match(motion, code):
                rba_lines.add(number)
        source_lines = pd.to_numeric(df["script_line"], errors="coerce")
        for number in source_lines[optimizers].unique():
            if not np.isfinite(number) or number != int(number) or not 1 <= number <= len(recipe_lines):
                raise ValueError(f"Invalid optimizer script_line: {number}")
            if not re.match(r"OPTIMIZE2?\s+(?:MAXIMIZE|MINIMIZE)\b",
                            recipe_lines[int(number) - 1].strip().upper()):
                raise ValueError(f"CSV optimizer does not match recipe line {int(number)}.")
        cutoff |= source_lines.isin(rba_lines).to_numpy()
    hits = np.flatnonzero(cutoff)
    return int(hits[0]) if hits.size else len(df)


def rba_motion_cutoff(df: pd.DataFrame) -> int:
    """Retain stationary calibration, stopping at the first detected joint motion."""
    start = rba_command_start(df)
    if start == len(df):
        return len(df)
    missing = [col for col in ALIGNER_JOINT_COLS if col not in df]
    if missing:
        raise ValueError(f"RBA motion detection requires joint readbacks: {missing}")
    joints = df[ALIGNER_JOINT_COLS].apply(pd.to_numeric, errors="coerce")
    joints = joints.replace([np.inf, -np.inf], np.nan)
    before = joints.iloc[:start].dropna().tail(25)
    if before.empty:
        raise ValueError("No complete pre-command Aligner joint samples to establish a baseline.")
    after = joints.iloc[start:]
    if after.isna().any(axis=None):
        raise ValueError("Incomplete Aligner joint readbacks after the RBA command; "
                         "cannot determine the first movement safely.")
    baseline = before.median()
    moving = (after - baseline).abs().gt(RBA_MOTION_TOL_DEG).any(axis=1).to_numpy()
    hits = np.flatnonzero(moving)
    return start + int(hits[0]) if hits.size else len(df)


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


def filter_snake(df: pd.DataFrame, require_moving: bool = False,
                 include_all_readings: bool = False):
    """Keep sweep data before RBA motion; optionally keep every positioned reading."""
    df = df.copy()
    counts = {"rows read": len(df)}
    cutoff = rba_motion_cutoff(df)
    counts["dropped: RBA motion and all later rows"] = len(df) - cutoff
    if cutoff < len(df):
        row = df.iloc[cutoff]
        print(f"RBA cutoff at {row[COL_T]:g} s: {row[COL_CMD]}")
    df = df.iloc[:cutoff]
    for col in NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    up = df[COL_CMD].fillna("").str.strip().str.upper()
    is_snake = up.str.match(r"HEXAPOD\s+MOVEINC\b")
    if include_all_readings:
        starts = np.flatnonzero(up.str.match(r"HEXAPOD\s+(?:MOVEINC|MOVEUNTIL)\b").to_numpy())
        if starts.size:
            is_snake = pd.Series(np.arange(len(df)) >= starts[0], index=df.index)
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
    counts["dropped: outside selected scan readings"] = int((~is_snake).sum())
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

    sweep, counts, breakdown = filter_snake(df, include_all_readings=True)
    print("\n== Row filters (all pre-RBA scan readings) ==")
    for label, count in counts.items():
        print(f"  {label}: {count}")
    for label, count in breakdown.items():
        if count:
            print(f"  {label}: {count}")
    if sweep.empty:
        raise ValueError("No usable voltage/Y/Z/X samples in the sweeps.")
    if np.unique(np.rint(sweep[COL_X].to_numpy() * 1000)).size > 1:
        raise ValueError("Multiple stage X values found; use multi_stage_x_heatmaps.py.")
    return sweep, nominal


def spiral_readings(sweep: pd.DataFrame) -> pd.DataFrame:
    """Identify post-sweep spiral legs, preferring explicit command names."""
    if COL_CMD not in sweep:
        return sweep.iloc[:0]
    cmd = sweep[COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()
    legs = cmd.str.match(r"HEXAPOD\s+(?:MOVEINC|MOVEUNTIL)\b")
    explicit = legs & cmd.str.contains(r"LEGLEN|SPIRAL", regex=True)
    if explicit.any():
        indices = np.flatnonzero(explicit.to_numpy())
        return sweep.iloc[indices[0]:indices[-1] + 1]

    # Compare whole command legs, not per-sample spacing: slow coarse sweeps
    # can have tiny sample spacing too.
    keys = cmd.copy()
    if "script_line" in sweep:
        keys = keys + "|" + sweep["script_line"].astype(str)
    groups = keys.ne(keys.shift()).cumsum()
    segments = []
    for _, sub in sweep.groupby(groups, sort=False):
        first = int(sweep.index.get_indexer([sub.index[0]])[0])
        last = first + len(sub)
        if not legs.iloc[first]:
            continue
        dy = float(np.ptp(sub[COL_Y].to_numpy()))
        dz = float(np.ptp(sub[COL_Z].to_numpy()))
        axis = COL_Y if dy >= dz else COL_Z
        travel = max(dy, dz)
        direction = float(sub[axis].iloc[-1] - sub[axis].iloc[0])
        segments.append((first, last, travel, axis, direction))
    if not segments:
        return sweep.iloc[:0]
    coarse = max(segment[2] for segment in segments)
    if coarse <= 0:
        return sweep.iloc[:0]
    candidates = []
    for first, last, travel, axis, direction in segments:
        if not candidates and not any(seg[0] < first and seg[2] >= coarse * .5
                                      for seg in segments):
            continue
        if 0 < travel < coarse * .1:
            candidates.append((first, last, axis, direction))
        elif candidates:
            candidates = []
    if len(candidates) < 4:
        return sweep.iloc[:0]
    # A snake's tiny row steps alone are not a spiral: both axes must reverse.
    for axis in (COL_Y, COL_Z):
        signs = {np.sign(direction) for _, _, name, direction in candidates if name == axis}
        if not {-1, 1}.issubset(signs):
            return sweep.iloc[:0]
    return sweep.iloc[candidates[0][0]:candidates[-1][1]]


def analyze_sweep(sweep: pd.DataFrame, ybin_mm: float, zbin_mm: float,
                  bounds: tuple[float, float, float, float] | None = None,
                  include_spiral_zoom: bool = True) -> dict:
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
    result = {
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
    if include_spiral_zoom:
        spiral = spiral_readings(sweep.reset_index(drop=True))
        if not spiral.empty:
            sy = float(np.ptp(spiral[COL_Y].to_numpy()))
            sz = float(np.ptp(spiral[COL_Z].to_numpy()))
            zoom_ybin = min(ybin_mm, max(sy / 100, 1e-6))
            zoom_zbin = min(zbin_mm, max(sz / 100, 1e-6))
            zoom = analyze_sweep(spiral, zoom_ybin, zoom_zbin, include_spiral_zoom=False)
            ypad = max(sy * .1, zoom_ybin * 2)
            zpad = max(sz * .1, zoom_zbin * 2)
            zoom["view_bounds"] = (zoom["y_min"] - ypad, zoom["y_max"] + ypad,
                                   zoom["z_min"] - zpad, zoom["z_max"] + zpad)
            result["spiral_zoom"] = zoom
    return result


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
    zoom = res.get("spiral_zoom")
    if zoom is None:
        fig, ax = plt.subplots(figsize=(plot_width + 2.0, plot_height + 2.0),
                               constrained_layout=True)
    else:
        fig, axes = plt.subplots(1, 2, figsize=(plot_width + 7.0, plot_height + 2.0),
                                 constrained_layout=True, gridspec_kw={"width_ratios": [plot_width, 5]})
        ax = axes[0]
    try:
        im = draw_heatmap(ax, res, norm, log)
        if zoom is None:
            ax.set_title(title, fontsize=10)
        else:
            fig.suptitle(title, fontsize=10)
            ax.set_title("All pre-RBA readings", fontsize=10)
            zoom_ax = axes[1]
            draw_heatmap(zoom_ax, zoom, norm, log)
            ylo, yhi, zlo, zhi = zoom["view_bounds"]
            zoom_ax.set_xlim(ylo, yhi)
            zoom_ax.set_ylim(zlo, zhi)
            zoom_ax.set_title("Spiral zoom (spiral readings only)\n" + peak_title(zoom), fontsize=9)
            zoom_ax.set_xlabel("Hexapod Y (mm)")
            zoom_ax.set_ylabel("Hexapod Z (mm)")
            zoom_ax.ticklabel_format(axis="both", style="plain", useOffset=False)
            ax.add_patch(Rectangle(
                (ylo, zlo), yhi - ylo, zhi - zlo, fill=False, edgecolor="red",
                linewidth=1.0, zorder=6))
        ax.set_xlabel("Hexapod Y (mm)")
        ax.set_ylabel("Hexapod Z (mm)")
        fig.colorbar(im, ax=ax if zoom is None else list(axes),
                     label=f"max scope voltage per bin ({V_UNIT})"
                     + (" - log" if log else ""))
        fig.savefig(path, dpi=130, bbox_inches="tight", pad_inches=0.2)
    finally:
        plt.close(fig)
    return path


def peak_title(res: dict) -> str:
    return (f"best {res['peak_v']:.4g} {V_UNIT} at "
            f"Y {res['peak_y']:+.4f} mm, Z {res['peak_z']:+.4f} mm")


def xyz_optimizer_peak(sweep: pd.DataFrame) -> dict | None:
    """Find the sampled maximum in a recipe-identified, hexapod-only XYZ optimizer."""
    cmd = sweep[COL_CMD].fillna("").str.strip().str.upper()
    optimizers = cmd.str.match(r"OPTIMIZE2?\s+(?:MAXIMIZE|MINIMIZE)\b")
    if not optimizers.any():
        return None
    recipe = sweep.attrs.get("recipe_lines")
    if recipe is None or "script_line" not in sweep:
        raise ValueError("XYZ optimizer title requires the matching recipe and script_line.")
    xyz_lines = set()
    axes = set()
    for number, text in enumerate(recipe, 1):
        code = text.split("//", 1)[0].strip().upper()
        axis = re.match(r"OPTIMIZE\s+AXIS\s+(\w+)", code)
        if axis:
            axes.add(axis.group(1))
        if re.match(r"OPTIMIZE2?\s+(?:MAXIMIZE|MINIMIZE)\b", code):
            if code.startswith(("OPTIMIZE MAXIMIZE", "OPTIMIZE2 MAXIMIZE")) and axes == {
                "HEXAPOD_X", "HEXAPOD_Y", "HEXAPOD_Z"
            }:
                xyz_lines.add(number)
            axes.clear()
    samples = sweep[optimizers & sweep["script_line"].isin(xyz_lines)]
    if samples.empty:
        return None
    positions = samples[HEXAPOD_COLS + [COL_V]].apply(pd.to_numeric, errors="coerce")
    valid = np.isfinite(positions.to_numpy()).all(axis=1)
    if not valid.any():
        raise ValueError("No complete voltage and hexapod X/Y/Z samples in XYZ optimization.")
    if not valid.all():
        print(f"WARNING: excluded {int((~valid).sum())} incomplete XYZ optimizer samples from title.")
    positions = positions[valid]
    best = positions.iloc[int(np.argmax(positions[COL_V].to_numpy()))]
    return {"voltage_V": float(best[COL_V]), "X_mm": float(best[HEXAPOD_COLS[0]]),
            "Y_mm": float(best[COL_Y]), "Z_mm": float(best[COL_Z])}


def sweep_title(res: dict, nominal: pd.Series) -> str:
    def positions(cols: list[str]) -> str:
        return ", ".join(f"{axis} {nominal[col]:+.4f}" for axis, col in zip("XYZ", cols))

    title = (
        "First light - pre-RBA readings at one stage X\n"
        f"Nominal PACKAGE (hexapod centered): {positions(PACKAGE_COLS)} mm\n"
        f"Nominal HEXSTAGE: {positions(HEXSTAGE_COLS)} mm\n"
        f"Reference hexapod: {positions(HEXAPOD_COLS)} mm\n"
        + peak_title(res)
    )
    xyz = res.get("xyz_optimizer_peak")
    if xyz is not None:
        title += (
            f"\nXYZ OPTIMIZE sampled best {xyz['voltage_V'] * V_SCALE:.4g} {V_UNIT}: "
            f"X {xyz['X_mm']:+.6f}, Y {xyz['Y_mm']:+.6f}, Z {xyz['Z_mm']:+.6f} mm"
        )
    return title


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
        res["xyz_optimizer_peak"] = xyz_optimizer_peak(sweep)
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
            "t_start_s": res["t0"], "t_end_s": res["t1"],
        }
        record.update({f"nominal_{col}": nominal[col] for col in NOMINAL_COLS})
        if res["xyz_optimizer_peak"] is not None:
            record.update({f"xyz_optimize_best_{key}": value
                           for key, value in res["xyz_optimizer_peak"].items()})
        pd.DataFrame([record]).to_csv(csv_out, index=False)
    except (OSError, ValueError, pd.errors.ParserError, ImportError) as exc:
        print(f"Could not produce single-sweep heatmap: {exc}", file=sys.stderr)
        return 1
    print(f"\n{sweep_title(res, nominal)}\nRows used: {res['rows']}")
    print(f"\nSaved:\n  {png_out}\n  {csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
