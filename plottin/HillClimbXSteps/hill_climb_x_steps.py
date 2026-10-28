"""Plot a Trawler "Lens Aperture YZ Hill Climb X Steps" log: three PNGs.

The recipe hill-climbs the PicoScope voltage (OPTIMIZE MAXIMIZE OSCOPE_VOLTAGE) in hexapod Y/Z,
steps hexapod X in 1 um increments (HEXAPOD MOVEABS X), and climbs again. There is no raster, so the
heatmap scripts do not apply. This script gives:

    1. peak voltage vs hexapod X step (top axis: gap to the goalposts, when the recipe is found)
    2. the Y/Z path of every climb coloured by voltage, with the search fence and the total cap
    3. voltage, X, Y and Z against time, with each climb shaded

Usage:
    python hill_climb_x_steps.py [data.csv] [--out-dir DIR] [--title TEXT] [--first-gap-um 24]
                                 [--no-show]

With no CSV argument a file picker opens. PNGs and a per-climb summary CSV are saved in a plots folder
next to the CSV unless --out-dir is given. Unless --title is given, a box prompts for the unit under
test, pre-filled with BG-CY0000 and the cursor after it. Cancelling the box, or --title "", means no
hand-written title. Every figure title starts with it and the run date/time (see plot_common.py).

A climb is one contiguous run of OPTIMIZE rows. Y/Z/X are shown as offsets from the first reading of the
run (um). Within a climb only settled rows (hex_moving == 0) are used, and rows at the same Y/Z (within
POINT_BIN_UM) are collapsed to one point holding their median voltage, so the peak is the best point
rather than the luckiest noisy sample. Points need MIN_POINT_SAMPLES rows to count as the peak.

If the matching run .txt recipe is found, its CadX and numeric GoalpostCadMm give the gap from CAD
nominal. If GoalpostCadMm is an expression, StartGapTargetUm anchors the first scan and measured X
changes give the gap for later climbs. FenceHalfMm and TotalCapMm give the fence/cap boxes. Each climb's
fence is +/- FenceHalfMm around where the previous climb parked (the run start for the first), cut back
to the +/- TotalCapMm cap around the start. Safe on header-only, partial or still-running logs.
"""
import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle

if __package__:
    from .. import plot_common as common
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import plot_common as common

CMAP = "viridis"
POINT_BIN_UM = 0.2          # rows within this Y/Z distance count as the same visited point
MIN_POINT_SAMPLES = 3       # a point needs this many settled rows to be eligible as a climb's peak
MIN_CLIMB_ROWS = 25         # shorter OPTIMIZE runs are not climbs
MAX_COLUMNS = 4             # panels per row in the path figure
TRF_JUMP_MM = 1.0           # a row-to-row hexapod move this big is the TRF being applied, not motion

COL_T, COL_CMD, COL_MOV = "time_elapsed_s", "script_cmd", "hex_moving"
COL_X, COL_Y, COL_Z, COL_V = "hex_X_mm", "hex_Y_mm", "hex_Z_mm", "oscope_voltage_V"
NUMERIC_COLS = [COL_T, COL_MOV, COL_X, COL_Y, COL_Z, COL_V]
USECOLS = [COL_CMD] + NUMERIC_COLS


@dataclass
class Climb:
    index: int
    rows: pd.DataFrame      # every valid row of the climb
    points: pd.DataFrame    # settled rows collapsed to visited Y/Z points, in time order
    start: pd.Series        # first settled point
    peak: pd.Series         # best-light point
    park: pd.Series         # last settled point (where the climb left the hexapod)
    x_um: float             # median hexapod X offset from the run start
    gap_um: float | None    # distance from the goalposts, if the recipe gave it


# ---------------------------------------------------------------------- inputs
def load_csv(path: Path) -> pd.DataFrame:
    """Read the needed columns; drop an unterminated last line (a log still being written)."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            raise ValueError("CSV is empty.")
        stream.seek(-1, 2)
        unterminated = stream.read(1) not in (b"\n", b"\r")
    df = pd.read_csv(path, usecols=lambda c: c in USECOLS, dtype=str,
                     on_bad_lines="warn", encoding_errors="replace")
    missing = [c for c in USECOLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    if unterminated and len(df):
        df = df.iloc[:-1]
    for col in NUMERIC_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


def recipe_values(csv_path: Path) -> dict[str, float]:
    """Numeric recipe parameters (first assignment of each) from the matching run .txt, else {}."""
    recipe = csv_path.with_name(csv_path.name.replace("_data_", "_")).with_suffix(".txt")
    if not recipe.is_file():
        return {}
    text = recipe.read_text(encoding="utf-8-sig", errors="replace")
    values = {}
    for name in ("CadX", "GoalpostCadMm", "StartGapTargetUm", "FenceHalfMm", "TotalCapMm"):
        match = re.search(rf"^\s*{name}\s*=\s*(-?\d+(?:\.\d+)?)", text, re.MULTILINE)
        if match:
            values[name] = float(match.group(1))
    return values


# -------------------------------------------------------------------- analysis
def after_trf_change(valid: pd.DataFrame) -> pd.DataFrame:
    """Rows from after the recipe's HEXAPOD TRF (X0 Y0 Z0 RX180 RY90 RZ0) took effect.

    The CSV does not log the TRF, but the readout jumps by tens of mm when it is applied, so rows up to
    the last such jump are in the old frame and are dropped.
    """
    jump = valid[[COL_X, COL_Y, COL_Z]].diff().abs().max(axis=1) > TRF_JUMP_MM
    if not jump.any():
        return valid
    return valid.loc[jump[jump].index[-1]:].reset_index(drop=True)


def add_offsets(df: pd.DataFrame) -> pd.DataFrame:
    """Add x_um/y_um/z_um (offset from the first full X/Y/Z reading) and v_mV; return the valid rows."""
    valid = df.dropna(subset=[COL_T, COL_X, COL_Y, COL_Z]).reset_index(drop=True)
    valid = after_trf_change(valid).copy()
    if valid.empty:
        raise ValueError("No rows with hexapod X/Y/Z readings.")
    origin = valid.iloc[0]
    for col, axis in ((COL_X, "x_um"), (COL_Y, "y_um"), (COL_Z, "z_um")):
        valid[axis] = (valid[col] - origin[col]) * 1000
    valid["v_mV"] = valid[COL_V] * 1000
    return valid


def optimize_runs(df: pd.DataFrame) -> list[pd.DataFrame]:
    """Contiguous runs of OPTIMIZE rows, each reduced to rows with a voltage reading."""
    cmd = df[COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()
    is_opt = cmd.str.startswith("OPTIMIZE")
    run_id = (is_opt != is_opt.shift()).cumsum()
    runs = [g.dropna(subset=["v_mV"]) for _, g in df[is_opt].groupby(run_id[is_opt], sort=True)]
    return [r for r in runs if len(r) >= MIN_CLIMB_ROWS]


def point_table(rows: pd.DataFrame) -> pd.DataFrame:
    """Settled rows collapsed to visited Y/Z points (median voltage), in order of first visit."""
    settled = rows[rows[COL_MOV] == 0]
    columns = ["t", "y_um", "z_um", "v_mV", "v_sd", "n"]
    if settled.empty:
        return pd.DataFrame(columns=columns)
    keys = [np.round(settled["y_um"] / POINT_BIN_UM).astype(int).rename("yk"),
            np.round(settled["z_um"] / POINT_BIN_UM).astype(int).rename("zk")]
    pts = settled.groupby(keys, sort=False).agg(
        t=(COL_T, "min"), y_um=("y_um", "median"), z_um=("z_um", "median"),
        v_mV=("v_mV", "median"), v_sd=("v_mV", "std"), n=("v_mV", "size")).reset_index(drop=True)
    return pts.sort_values("t", kind="mergesort").reset_index(drop=True)


def build_climbs(runs: list[pd.DataFrame], first_gap_um: float | None) -> list[Climb]:
    climbs = []
    for rows in runs:
        pts = point_table(rows)
        if pts.empty:
            print(f"Skipping a climb at t={rows[COL_T].iloc[0]:.1f}s: no settled rows.")
            continue
        eligible = pts[pts["n"] >= MIN_POINT_SAMPLES]
        pool = eligible if len(eligible) else pts
        x_um = float(rows["x_um"].median())
        gap = None if first_gap_um is None else first_gap_um + x_um
        climbs.append(Climb(len(climbs), rows, pts, pts.iloc[0], pool.loc[pool["v_mV"].idxmax()],
                            pts.iloc[-1], x_um, gap))
    return climbs


def fence_box(climb: Climb, previous: Climb | None, params: dict[str, float]):
    """(y_lo, y_hi, z_lo, z_hi) in um of this climb's search box, or None without recipe values."""
    if "FenceHalfMm" not in params:
        return None
    half = params["FenceHalfMm"] * 1000
    cap = params["TotalCapMm"] * 1000 if "TotalCapMm" in params else np.inf
    cy, cz = (0.0, 0.0) if previous is None else (previous.park["y_um"], previous.park["z_um"])
    return (max(cy - half, -cap), min(cy + half, cap), max(cz - half, -cap), min(cz + half, cap))


def summary_frame(climbs: list[Climb]) -> pd.DataFrame:
    return pd.DataFrame([{
        "climb": c.index, "t_start_s": c.rows[COL_T].iloc[0], "t_end_s": c.rows[COL_T].iloc[-1],
        "x_um": c.x_um, "gap_um": c.gap_um, "start_mV": c.start["v_mV"], "peak_mV": c.peak["v_mV"],
        "peak_sd_mV": c.peak["v_sd"], "peak_y_um": c.peak["y_um"], "peak_z_um": c.peak["z_um"],
        "park_y_um": c.park["y_um"], "park_z_um": c.park["z_um"], "points": len(c.points),
    } for c in climbs])


# ----------------------------------------------------------------------- plots
def plot_peak_vs_x(climbs: list[Climb], first_gap_um: float | None, heading: str):
    fig, ax = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    x_offset = np.array([c.x_um for c in climbs])
    x = x_offset if first_gap_um is None else x_offset + first_gap_um
    start = np.array([c.start["v_mV"] for c in climbs])
    peak = np.array([c.peak["v_mV"] for c in climbs])
    sd = np.nan_to_num(np.array([c.peak["v_sd"] for c in climbs], dtype=float))
    ax.fill_between(x, start, peak, color="#2b7be0", alpha=0.12, label="Gain from the climb")
    ax.plot(x, start, "o--", color="#8a8a8a", markersize=5, label="Light at climb start")
    ax.errorbar(x, peak, yerr=sd, fmt="o-", color="#2b7be0", linewidth=1.8, markersize=6, capsize=3,
                label="Peak light (error bar: sd of the peak point)")
    best = int(np.argmax(peak))
    ax.plot(x[best], peak[best], "*", color="gold", markeredgecolor="black", markersize=18, zorder=5,
            label=f"Best: {peak[best]:.3f} mV at X {round(x[best], 1) + 0.0:+.1f} um")
    if first_gap_um is None:
        ax.set_xlabel("Hexapod X offset from run start (um); -X is toward the goalposts")
    else:
        ax.set_xlabel("Gap to the goalposts (um)")
        top = ax.secondary_xaxis("top", functions=(lambda v: v - first_gap_um,
                                                   lambda v: v + first_gap_um))
        top.set_xlabel("Hexapod X offset from run start (um)")
    ax.set_ylabel("Scope voltage (mV)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9)
    fig.suptitle(f"{heading}Peak light vs hexapod X", fontsize=14, fontweight="bold")
    return fig


def plot_paths(climbs: list[Climb], params: dict[str, float], heading: str):
    n = len(climbs)
    cols = min(MAX_COLUMNS, n)
    rows = -(-n // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(3.6 * cols + 1.2, 3.6 * rows + 1.0), squeeze=False,
                             constrained_layout=True)
    volts = np.concatenate([c.points["v_mV"].to_numpy(dtype=float) for c in climbs])
    norm = Normalize(vmin=np.nanmin(volts), vmax=np.nanmax(volts))
    cap = params["TotalCapMm"] * 1000 if "TotalCapMm" in params else None
    boxes = [fence_box(c, climbs[i - 1] if i else None, params) for i, c in enumerate(climbs)]
    for ax, climb, box in zip(axes.flat, climbs, boxes):
        pts = climb.points
        ax.plot(pts["y_um"], pts["z_um"], "-", color="#888888", linewidth=0.5, zorder=1)
        ax.scatter(pts["y_um"], pts["z_um"], c=pts["v_mV"], cmap=CMAP, norm=norm, s=16, zorder=2)
        ax.plot(climb.start["y_um"], climb.start["z_um"], "o", mfc="none", mec="black", markersize=9,
                zorder=3)
        ax.plot(climb.park["y_um"], climb.park["z_um"], "x", color="red", markersize=8, zorder=3)
        ax.plot(climb.peak["y_um"], climb.peak["z_um"], "*", color="gold", mec="black", markersize=14,
                zorder=4)
        if cap is not None:
            ax.add_patch(Rectangle((-cap, -cap), 2 * cap, 2 * cap, fill=False, ec="#888888", ls=":", lw=1))
        if box is not None:
            ax.add_patch(Rectangle((box[0], box[2]), box[1] - box[0], box[3] - box[2], fill=False,
                                   ec="red", ls="--", lw=1))
        gap = "" if climb.gap_um is None else f", gap {climb.gap_um:.1f} um"
        ax.set_title(f"#{climb.index}  X {round(climb.x_um, 1) + 0.0:+.1f} um{gap}\n"
                     f"peak {climb.peak['v_mV']:.3f} mV", fontsize=9)
        ax.set_aspect("equal", adjustable="box")
        ax.grid(True, alpha=0.3)
    for ax in axes.flat[n:]:
        ax.set_visible(False)

    # Each panel is centred on its own path; every panel has the same span so the scales compare.
    extents = [(c.points["y_um"].min(), c.points["y_um"].max(), c.points["z_um"].min(),
                c.points["z_um"].max()) for c in climbs]
    half = 0.5 * max(max(e[1] - e[0], e[3] - e[2]) for e in extents)
    half = max(half * 1.15, 1.0)
    for ax, (y0, y1, z0, z1) in zip(axes.flat, extents):
        ax.set_xlim((y0 + y1) / 2 - half, (y0 + y1) / 2 + half)
        ax.set_ylim((z0 + z1) / 2 - half, (z0 + z1) / 2 + half)
        ax.set_xlabel("Hexapod Y offset (um)", fontsize=8)
        ax.set_ylabel("Hexapod Z offset (um)", fontsize=8)
    fig.colorbar(ScalarMappable(norm=norm, cmap=CMAP), ax=axes, label="Scope voltage (mV)", shrink=0.8)
    legend = f"panels are {2 * half:.0f} um wide; circle = start, x = parked, star = peak"
    if boxes[0] is not None:
        legend += ", dashed = fence, dotted = total cap (where in view)"
    fig.suptitle(f"{heading}Y/Z path of each climb\n({legend})", fontsize=12, fontweight="bold")
    return fig


def plot_time_history(df: pd.DataFrame, climbs: list[Climb], params: dict[str, float], heading: str):
    fig, axes = plt.subplots(4, 1, figsize=(12, 9.5), sharex=True, constrained_layout=True)
    t = df[COL_T]
    settled = df[(df[COL_MOV] == 0) & df["v_mV"].notna()]
    axes[0].plot(settled[COL_T], settled["v_mV"], ".", color="#2b7be0", alpha=0.25, markersize=2,
                 label="Settled readings")
    axes[0].plot([c.peak["t"] for c in climbs], [c.peak["v_mV"] for c in climbs], "*", color="gold",
                 mec="black", markersize=12, zorder=5, label="Climb peak")
    axes[0].set_ylabel("Scope voltage (mV)")
    axes[0].legend(fontsize=9, loc="upper right")
    cap = params["TotalCapMm"] * 1000 if "TotalCapMm" in params else None
    for ax, col, label, color in ((axes[1], "x_um", "Hexapod X offset (um)", "#e05a2b"),
                                  (axes[2], "y_um", "Hexapod Y offset (um)", "#2a9d5c"),
                                  (axes[3], "z_um", "Hexapod Z offset (um)", "#8a4fd0")):
        ax.plot(t, df[col], color=color, linewidth=0.8)
        ax.set_ylabel(label)
    axes[3].set_xlabel("Time since log start (s)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
        for c in climbs[::2]:
            ax.axvspan(c.rows[COL_T].iloc[0], c.rows[COL_T].iloc[-1], color="#999999", alpha=0.12, lw=0)
    fig.suptitle(f"{heading}Hill climb time history (shaded = every other climb)",
                 fontsize=13, fontweight="bold")
    return fig


# ------------------------------------------------------------------------- main
def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
    ap.add_argument("--first-gap-um", type=float,
                    help="gap to the goalposts at the run start (um); overrides the recipe")
    ap.add_argument("--no-show", action="store_true", help="save the PNGs without opening windows")
    a = ap.parse_args(argv)

    path = common.input_path(a.csv)
    if path is None or not path.is_file():
        print(f"No CSV found: {path}")
        return 1
    out_dir = Path(a.out_dir) if a.out_dir else path.parent / "plots"
    if a.no_show:
        plt.switch_backend("Agg")

    header = common.title_header(common.get_title(a.title), common.run_time(path))

    print(f"Reading {path}")
    try:
        df = add_offsets(load_csv(path))
        params = recipe_values(path)
        first_gap = a.first_gap_um
        if first_gap is None and {"CadX", "GoalpostCadMm"} <= params.keys():
            first_gap = (params["GoalpostCadMm"] + (df[COL_X].iloc[0] - params["CadX"])) * 1000
        runs = optimize_runs(df)
        if first_gap is None and "StartGapTargetUm" in params:
            first_scan = next((run for run in runs if not point_table(run).empty), None)
            if first_scan is not None:
                first_gap = params["StartGapTargetUm"] - float(first_scan["x_um"].median())
        climbs = build_climbs(runs, first_gap)
        if not climbs:
            raise ValueError("No OPTIMIZE climbs with settled readings in this log.")
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    if first_gap is None:
        print("No usable recipe gap target and no --first-gap-um: gap axis omitted.")
    if "FenceHalfMm" not in params:
        print("No recipe FenceHalfMm: fence boxes omitted.")

    out_dir.mkdir(parents=True, exist_ok=True)
    heading = f"{header}\n{common.run_name(path)} - "
    summary = summary_frame(climbs)
    summary.to_csv(out_dir / f"{path.stem}_hill_climb_summary.csv", index=False)
    print(summary.round(3).to_string(index=False))

    figures = {
        "peak_vs_x": plot_peak_vs_x(climbs, first_gap, heading),
        "paths": plot_paths(climbs, params, heading),
        "time_history": plot_time_history(df, climbs, params, heading),
    }
    for suffix, fig in figures.items():
        out = out_dir / f"{path.stem}_hill_climb_{suffix}.png"
        fig.savefig(out, dpi=150)
        print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
