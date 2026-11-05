"""Heatmap of power-wand reading over the Y/Z raster of a "6100 - Raster AL1" or "6200 - Raster AL2" run.

Usage:
    python 6100-6200RasterHeatmap.py [data.csv | run folder] [--out-dir DIR] [--title TEXT] [--no-show]

With no argument, a file picker opens. A run folder must contain exactly one *_data_*.csv. Outputs go
to a plots folder next to the CSV: <csv stem>_al<N>_raster_heatmap.png and
<csv stem>_al<N>_raster_points.csv (one row per raster point). Title handling follows plot_common.py.

Written against 6100 - Raster AL1 v1.0.1-v1.2.0 and 6200 - Raster AL2 v1.1.0-v1.4.0; also reads the
older "6100 - Raster AL2" v1.0.1 logs. The rastered aligner (1 or 2) is the one whose MoveLin commands
target the raster variables below; other moves (e.g. Aligner 2 stepping out of the way in 6100 AL1,
MoveLinRelWrf) are ignored. Ring sizes and radii are not assumed; they come from the measured points.

Each "DWELL SettleMs" block is one raster point: the recipe reads PMAVG_Wand straight after it. A
point's power is the mean pm_Wand_mW over its dwell and its position the mean meca_Aligner<N>_Y/Z
readback (measured, not commanded). The phase comes from the Y argument of the MoveLin before the dwell:
    CenterY / PtY -> coarse raster (centre point, then the rings)
    CoarseBestY   -> re-read at the coarse best spot (centre of the fine raster)
    FinePtY       -> fine raster (rings out to 20 um)
    FineBestY     -> final park on the best spot
That MoveLin is looked up in the run's recipe .txt (the one non-console .txt in the CSV's folder) from
the dwell's script_line, because a zero-length move (e.g. to the centre when already there) can finish
between CSV samples and never appear in script_cmd. Without the recipe, the last logged MoveLin is used.
When the wand stops answering, the recipe moves to LastGoodY/Z, waits (DWELL WandLostDwellMs) and later
re-reads the point; the dwell just before that had no wand reading and is dropped (the re-read is kept).
If the CSV logger stalls for a whole dwell, that point is simply not in the CSV; such gaps are reported.
Offsets are in um from the coarse centre point. That is the saved 6000 start (AL1) / 6010 start (AL2)
unless the recipe's start setting (StartFrom..., e.g. StartFromCADorHere / StartfromHereorCAD) is 1 or
"HERE", when the raster was centred on wherever the aligner already was; with no recipe it is just called
the raster centre. The colour map is a linear interpolation between the measured points (Delaunay
triangles); black dots are the points themselves. The coarse and fine panels each have their own colour
scale. Unfinished runs plot what was measured; an unterminated final CSV line is ignored.
"""

import argparse
import io
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Circle
from matplotlib.tri import Triangulation

if __package__:
    from .. import plot_common as common
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import plot_common as common

CSV_PATH = None
COL_T, COL_CMD, COL_P, COL_LINE = "time_elapsed_s", "script_cmd", "pm_Wand_mW", "script_line"
BASE_COLS = [COL_T, COL_CMD, COL_P]
POSE_COLS = {arm: [f"meca_Aligner{arm}_{axis}_mm" for axis in "XYZ"] for arm in (1, 2)}
START_PROGRAM = {1: "6000", 2: "6010"}  # program that saved each aligner's start point (raster centre)
START_SETTING = re.compile(r"STARTFROM\w*\s*=\s*\"?(\w+)")  # 0 / "NOMINAL" = saved start, 1 / "HERE" = live pose
DWELL = re.compile(r"DWELL\s+SETTLEMS\b")
LOST_DWELL = re.compile(r"DWELL\s+WANDLOSTDWELLMS\b")
MOVE = re.compile(r"ALIGNER([12])\s+MOVELIN\s*\(([^)]*)\)")
PHASE_BY_Y_ARG = {"CENTERY": "coarse", "PTY": "coarse", "COARSEBESTY": "recheck",
                  "FINEPTY": "fine", "FINEBESTY": "park"}
RECOVERY_Y_ARG = "LASTGOODY"
LOG_GAP_S = 0.15  # SettleMs: a CSV logging gap this long can swallow a whole dwell (and its point)
FINE_RADIUS_UM = 20  # FineMaxRadiusUm in the recipe; only used to outline the fine raster
CMAP = "viridis"


def resolve_csv(path: Path) -> Path:
    if not path.is_dir():
        return path
    found = sorted(path.glob("*_data_*.csv"))
    if len(found) != 1:
        raise ValueError(f"Expected exactly one *_data_*.csv in {path}, found {len(found)}.")
    return found[0]


def load_csv(path: Path) -> pd.DataFrame:
    data = path.read_bytes()
    cut = data.rfind(b"\n")
    if cut == -1:
        raise ValueError("CSV has no complete line (empty or still being written).")
    buf = io.BytesIO(data[:cut + 1])
    header = pd.read_csv(buf, nrows=0, encoding_errors="replace").columns
    missing = [c for c in BASE_COLS if c not in header]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    columns = BASE_COLS + [c for c in [COL_LINE] + [c for cols in POSE_COLS.values() for c in cols]
                           if c in header]
    buf.seek(0)
    df = pd.read_csv(buf, usecols=columns, dtype=str, on_bad_lines="warn", encoding_errors="replace")
    for col in columns:
        if col != COL_CMD:
            df[col] = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    recipes = [p for p in path.parent.glob("*.txt") if "_console_log_" not in p.name]
    if len(recipes) == 1:
        df.attrs["recipe"] = recipes[0].read_text(encoding="utf-8-sig", errors="replace").splitlines()
    else:
        print(f"WARNING: expected one recipe .txt next to the CSV, found {len(recipes)}; "
              "phases come from the logged MoveLin commands instead.")
    return df


def raster_origin(recipe: list[str] | None, arm: int) -> str:
    """What the coarse centre point is: the saved start point, or wherever the aligner already was."""
    if recipe is None:
        return "raster centre"
    for text in recipe:
        m = START_SETTING.match(text.split("//", 1)[0].strip().upper())
        if m and m.group(1) in ("1", "HERE"):
            return f"AL{arm} pose at run start"
    return f"{START_PROGRAM[arm]} start"


def dwell_moves(recipe: list[str]) -> dict[int, tuple[str, str]]:
    """Recipe line of each DWELL SettleMs -> (aligner, Y argument) of the MoveLin right before it."""
    moves, prev = {}, None
    for number, text in enumerate(recipe, 1):
        code = text.split("//", 1)[0].strip().upper()
        if not code:
            continue
        if DWELL.match(code) and prev:
            moves[number] = prev
        m = MOVE.match(code)
        prev = (m.group(1), re.split(r"[\s,]+", m.group(2).strip())[1]) if m else None
    return moves


def raster_points(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """One row per settled raster point, tagged with its phase, and the rastered aligner (1 or 2)."""
    cmd = df[COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()
    move = cmd.str.extract(MOVE)
    y_arg = move[1].str.split(r"[\s,]+").str[1]
    arms = move.loc[y_arg.isin(PHASE_BY_Y_ARG.keys()), 0].value_counts()
    if arms.empty:
        raise ValueError("No raster moves (Aligner1/2 MoveLin to CenterY, PtY, ...) found.")
    arm = int(arms.idxmax())
    col_x, col_y, col_z = POSE_COLS[arm]
    missing = [c for c in POSE_COLS[arm] if c not in df]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    y_arg = y_arg.where(move[0] == str(arm))
    dwell = cmd.str.match(DWELL)
    phase = y_arg.map(PHASE_BY_Y_ARG)
    phase[y_arg.notna() & phase.isna()] = "other"
    phase = phase.ffill().fillna("other")
    by_line = dwell_moves(df.attrs.get("recipe", []))
    if by_line and COL_LINE in df:
        lines = df.loc[dwell, COL_LINE]
        if lines.isin(by_line.keys()).all():
            phase[dwell] = [PHASE_BY_Y_ARG.get(y, "other") if a == str(arm) else "other"
                            for a, y in lines.map(by_line)]
        else:
            print("WARNING: the recipe .txt does not match the CSV's dwell lines; "
                  "phases come from the logged MoveLin commands instead.")
    df = df.assign(phase=phase)
    block = dwell.ne(dwell.shift()).cumsum()
    recovery = y_arg.eq(RECOVERY_Y_ARG) | cmd.str.match(LOST_DWELL)
    recovery_start = recovery & ~recovery.shift(fill_value=False)
    lost = set(block.where(dwell).ffill()[recovery_start].dropna())
    if lost:
        print(f"Wand lost {len(lost)} time(s); dropped the dwell(s) with no reading (the re-reads are kept).")
    rows = df[dwell & ~block.isin(lost)].dropna(subset=[COL_P, col_x, col_y, col_z])
    pts = rows.groupby(block.loc[rows.index], sort=True).agg(
        phase=("phase", "first"), t_s=(COL_T, "first"), n_samples=(COL_P, "size"),
        power_mW=(COL_P, "mean"), X_mm=(col_x, "mean"), Y_mm=(col_y, "mean"), Z_mm=(col_z, "mean"))
    other = int((pts["phase"] == "other").sum())
    if other:
        print(f"WARNING: skipping {other} dwell(s) not after a recognised Aligner{arm} MoveLin.")
    pts = pts[pts["phase"] != "other"].reset_index(drop=True)
    t = df[COL_T]
    in_raster = t.between(pts["t_s"].min(), pts["t_s"].max()) if not pts.empty else t.isna()
    gaps = t[in_raster].diff()
    gaps = gaps[gaps >= LOG_GAP_S]
    if not gaps.empty:
        print(f"WARNING: {len(gaps)} CSV logging gap(s) of {LOG_GAP_S * 1000:.0f} ms or more during the raster "
              f"(longest {gaps.max() * 1000:.0f} ms, ending at {', '.join(f'{v:g}' for v in t[gaps.index][:5])} s"
              f"{', ...' if len(gaps) > 5 else ''}); a point whose whole dwell fell in one is missing.")
    coarse = pts[pts["phase"] == "coarse"]
    if coarse.empty:
        raise ValueError(f"No coarse raster points (DWELL SettleMs after an Aligner{arm} MoveLin) found.")
    origin = coarse.iloc[0]
    for axis in "XYZ":
        pts[f"{axis.lower()}_um"] = (pts[f"{axis}_mm"] - origin[f"{axis}_mm"]) * 1000
    return pts, arm


def peak(pts: pd.DataFrame) -> pd.Series | None:
    return pts.loc[pts["power_mW"].idxmax()] if not pts.empty else None


def draw_panel(fig, ax, pts: pd.DataFrame, title: str, arm: int, origin: str) -> None:
    y, z, p = pts["y_um"].to_numpy(), pts["z_um"].to_numpy(), pts["power_mW"].to_numpy()
    mappable = None
    if len(pts) >= 3:
        try:
            mappable = ax.tripcolor(Triangulation(y, z), p, shading="gouraud", cmap=CMAP)
        except (ValueError, RuntimeError):
            mappable = None  # collinear or duplicate points: fall back to coloured dots
    if mappable is None:
        mappable = ax.scatter(y, z, c=p, cmap=CMAP, s=60, edgecolors="k", linewidths=0.5)
    else:
        ax.plot(y, z, ".", color="k", markersize=2, alpha=0.5)
    fig.colorbar(mappable, ax=ax, shrink=0.85, label="Wand power (mW)")
    ax.set_title(title, fontsize=11)
    ax.set_xlabel(f"Aligner {arm} world Y offset from {origin} (um)")
    ax.set_ylabel(f"Aligner {arm} world Z offset from {origin} (um)")
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.2)


def mark(ax, pt: pd.Series | None, marker: str, color: str, label: str, size: float = 14) -> None:
    if pt is not None:
        ax.plot(pt["y_um"], pt["z_um"], marker, color=color, markersize=size, markeredgecolor="k",
                markeredgewidth=0.8, linestyle="none",
                label=f"{label}: {pt['power_mW']:.4f} mW at ({pt['y_um']:+.1f}, {pt['z_um']:+.1f})")


def summary(label: str, pt: pd.Series | None) -> str:
    if pt is None:
        return f"{label}: n/a"
    return f"{label} {pt['power_mW']:.4f} mW @ Y {pt['y_um']:+.1f}, Z {pt['z_um']:+.1f} um"


def plot(pts: pd.DataFrame, arm: int, origin: str, header: str, name: str) -> plt.Figure:
    coarse = pts[pts["phase"] == "coarse"]
    fine = pts[pts["phase"].isin(["recheck", "fine"])]
    recheck = pts[pts["phase"] == "recheck"]
    park = pts[pts["phase"] == "park"]
    start, coarse_pk, fine_pk = coarse.iloc[0], peak(coarse), peak(fine)
    final = park.iloc[-1] if not park.empty else None

    panels = 2 if not fine.empty else 1
    fig, axes = plt.subplots(1, panels, figsize=(max(10, 7.5 * panels), 7.5), constrained_layout=True,
                             squeeze=False)
    status = "" if final is not None else "  (run incomplete: no final park)"
    fig.suptitle(f"{header}\n{name} - AL{arm} raster heatmap (power wand){status}", fontsize=13,
                 fontweight="bold")
    fig.supxlabel(f"{summary('Centre', start)}   |   {summary('Coarse peak', coarse_pk)}   |   "
                  f"{summary('Final', final)}", fontsize=10)

    ax = axes[0][0]
    draw_panel(fig, ax, coarse, f"Coarse raster: {len(coarse)} points", arm, origin)
    mark(ax, start, "P", "white", "Centre", 12)
    mark(ax, coarse_pk, "*", "red", "Coarse peak", 18)
    if not recheck.empty:
        c = recheck.iloc[0]
        ax.add_patch(Circle((c["y_um"], c["z_um"]), FINE_RADIUS_UM, fill=False, color="red",
                            linestyle="--", linewidth=1.2, label="Fine raster area"))
    mark(ax, final, "X", "orange", "Final park", 12)
    ax.legend(fontsize=8, loc="best", framealpha=0.85)

    if panels == 2:
        ax = axes[0][1]
        draw_panel(fig, ax, fine, f"Fine raster: {len(fine)} points (incl. re-read at coarse best)", arm,
                   origin)
        mark(ax, fine_pk, "*", "red", "Fine peak", 18)
        mark(ax, final, "X", "orange", "Final park", 12)
        ax.legend(fontsize=8, loc="best", framealpha=0.85)
    return fig


def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="Trawler data CSV or its run folder")
    ap.add_argument("--out-dir", help="output folder (default: plots next to the CSV)")
    common.add_title_arg(ap)
    ap.add_argument("--no-show", action="store_true", help="save the PNG without opening a window")
    a = ap.parse_args(argv)

    path = common.input_path(a.csv, CSV_PATH)
    if path is None or not path.exists():
        print(f"No CSV found: {path}")
        return 1
    try:
        path = resolve_csv(path)
        print(f"Reading {path}")
        df = load_csv(path)
        pts, arm = raster_points(df)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    origin = raster_origin(df.attrs.get("recipe"), arm)
    print(f"Aligner {arm} raster, offsets from the {origin}")
    for phase, count in pts["phase"].value_counts(sort=False).items():
        print(f"  {phase}: {count} points")
    print(f"  Aligner {arm} X spread over the raster: {np.ptp(pts['x_um']):.1f} um")

    out_dir = Path(a.out_dir) if a.out_dir else path.parent / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = common.title_header(common.get_title(a.title), common.run_time(path))
    fig = plot(pts, arm, origin, header, common.run_name(path))

    points_csv = out_dir / f"{path.stem}_al{arm}_raster_points.csv"
    pts.to_csv(points_csv, index=False, float_format="%.6g")
    out = out_dir / f"{path.stem}_al{arm}_raster_heatmap.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}\nSaved {points_csv}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
