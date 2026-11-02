"""Plot the 5010 - Collimate Lens with Hexapod focus walk: far-field divergence vs distance from the -X wall.

Usage:
    python collimate_divergence_vs_gap.py [data.csv] [--out-dir DIR] [--title TEXT]
                                          [--gap-mm MM] [--cad-x MM] [--spec MRAD] [--zoom-um UM] [--no-show]

With no CSV argument, a file picker opens. The PNG is saved in a plots folder next to the CSV.
The optional hand-written title (the unit under test) is printed at the top of the plot. Unless
--title is given, a box prompts for it, pre-filled with BG-CY0000 (see plot_common.py).

The walk (Step 6) is located from the logged script_cmd text:
    "HEXAPOD SPEED (SpeedFocus)" up to (not incl.) "HEXAPOD MOVEABS X(DivQMinHexX)"
and split into steps at each "HEXAPOD MOVEABS X(XTarget)". A run stopped mid-walk plots what exists.

The bottom axis is the lens-to-wall gap the script itself works out:
    gap (um) = (S5000XWallGapMm + hexapod X - CadX) * 1000
S5000XWallGapMm is read from the run's *_config_*.json (persistent vars snapshot) and CadX from the
script copy saved in the run folder; --gap-mm / --cad-x override them. Without a wall gap the
bottom axis falls back to the offset from the walk start. The top axis is always that offset.

Faint dots are every settled sample (hex_moving == 0, beam found); solid lines join the last
settled reading of each step (the 50-frame average the script reads before the next step). The
star marks the lowest quadrature divergence sqrt(divX^2 + divY^2), the quantity the script
minimises. When logged, the reading after returning to that X, and the final reading after the
last aim (Step 8), are marked too. A second panel zooms to +/- --zoom-um around the minimum.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

if __package__:
    from .. import plot_common as common
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import plot_common as common

COL_T, COL_CMD, COL_MOV = "time_elapsed_s", "script_cmd", "hex_moving"
COL_X, COL_DX, COL_DY, COL_FOUND = "hex_X_mm", "ff_div_x_mrad", "ff_div_y_mrad", "ff_beam_found"
USECOLS = [COL_T, COL_CMD, COL_MOV, COL_X, COL_DX, COL_DY, COL_FOUND]

CAD_X_DEFAULT = 15.682  # mm, CadX in 5010 v1.0.0
DIV_SPEC_DEFAULT = 0.45  # mrad, DivSpecMaxMrad in 5010 v1.0.0
ZOOM_DEFAULT_UM = 20.0
GAP_VAR = "S5000XWallGapMm"

WALK_START = r"HEXAPOD\s+SPEED\s+\(\s*SpeedFocus\s*\)"
WALK_END = r"HEXAPOD\s+MOVEABS\s+X\(\s*DivQMinHexX\s*\)"
WALK_STEP = r"HEXAPOD\s+MOVEABS\s+X\(\s*XTarget\s*\)"
FINAL_AIM = r"AIM\s+CAMERA\s+FF"
FINAL_READ = r"FinalDivXMrad\s*=\s*FF_div_x"


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in USECOLS, dtype={COL_CMD: str},
                     on_bad_lines="warn", encoding_errors="replace")
    missing = [c for c in USECOLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    for c in USECOLS:
        if c != COL_CMD:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df[COL_CMD] = df[COL_CMD].fillna("")
    return df.replace([np.inf, -np.inf], np.nan).reset_index(drop=True)


def find(df: pd.DataFrame, pattern: str, start: int = 0) -> int | None:
    """Index of the first row at or after start whose script_cmd matches pattern."""
    hits = np.flatnonzero(df[COL_CMD].iloc[start:].str.contains(pattern, case=False, regex=True).to_numpy())
    return start + int(hits[0]) if hits.size else None


def run_files(csv: Path, pattern: str) -> list[Path]:
    """Sibling files of the data CSV from the same run (same stem, '_data_' swapped out)."""
    stem = csv.stem.replace("_data_", pattern)
    return sorted(csv.parent.glob(f"{stem}.*")) or sorted(csv.parent.glob(f"*{pattern}*"))


def read_wall_gap(csv: Path) -> float | None:
    """S5000XWallGapMm from the run's config JSON (persistent vars snapshot at run start)."""
    for cfg in run_files(csv, "_config_"):
        if cfg.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if str(node.get("name", "")).lower() == GAP_VAR.lower() and "value" in node:
                    try:
                        return float(node["value"])
                    except (TypeError, ValueError):
                        return None
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return None


def read_cad_x(csv: Path) -> float | None:
    """CadX from the script copy Trawler saves in the run folder."""
    stem = csv.stem.replace("_data_", "_")
    candidates = [csv.parent / f"{stem}.txt"] + sorted(csv.parent.glob("*.txt"))
    for txt in candidates:
        if not txt.is_file():
            continue
        try:
            m = re.search(r"^\s*CadX\s*=\s*([-+0-9.eE]+)", txt.read_text(encoding="utf-8", errors="replace"),
                          re.MULTILINE)
        except OSError:
            continue
        if m:
            return float(m.group(1))
    return None


def settled(rows: pd.DataFrame) -> pd.DataFrame:
    """Rows after the last moving row, with a found beam and valid readings."""
    moving = np.flatnonzero((rows[COL_MOV] != 0).to_numpy())
    if moving.size:
        rows = rows.iloc[moving[-1] + 1:]
    keep = (rows[COL_MOV] == 0) & (rows[COL_FOUND] == 1)
    rows = rows[keep].dropna(subset=[COL_X, COL_DX, COL_DY]).copy()
    rows["quad"] = np.hypot(rows[COL_DX], rows[COL_DY])
    return rows


def walk_steps(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, bool, int | None]:
    """(all settled samples tagged by step, one reading per step, walk finished?, walk-end row)."""
    start = find(df, WALK_START)
    if start is None:
        raise ValueError("No focus walk in this log (no 'HEXAPOD SPEED (SpeedFocus)' line).")
    end = find(df, WALK_END, start)
    walk = df.iloc[start:end if end is not None else len(df)]
    is_step = walk[COL_CMD].str.contains(WALK_STEP, case=False, regex=True)
    step_no = (is_step & ~is_step.shift(fill_value=False)).cumsum()  # 0 = walk start, before any move
    samples, readings = [], []
    for n, rows in walk.groupby(step_no):
        s = settled(rows)
        if s.empty:
            continue
        s["step"] = n
        samples.append(s)
        readings.append(s.iloc[-1])
    if not readings:
        raise ValueError("The focus walk has no settled readings with a beam.")
    return pd.concat(samples), pd.DataFrame(readings).reset_index(drop=True), end is not None, end


def reading_between(df: pd.DataFrame, start: int | None, end_re: str) -> pd.Series | None:
    """Last settled reading from row start up to (not incl.) the first match of end_re."""
    if start is None:
        return None
    end = find(df, end_re, start + 1)
    s = settled(df.iloc[start:end if end is not None else len(df)])
    return s.iloc[-1] if not s.empty else None


def final_reading(df: pd.DataFrame) -> pd.Series | None:
    """The reading the script grades at Step 8 (last settled row up to the FinalDivXMrad line)."""
    at = find(df, FINAL_READ)
    if at is None:
        return None
    s = settled(df.iloc[max(at - 200, 0):at + 1])
    return s.iloc[-1] if not s.empty else None


def plot(ax, samples, steps, back, final, to_axis, xlabel, spec) -> None:
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Far-field divergence (mrad)")
    ax.grid(True, alpha=0.3)
    series = [(COL_DX, "FF div X", "#2b7be0"), (COL_DY, "FF div Y", "#e05a2b"),
              ("quad", "Quadrature sqrt(X\u00b2+Y\u00b2)", "black")]
    for col, label, color in series:
        if col != "quad":
            ax.plot(to_axis(samples[COL_X]), samples[col], ".", color=color, alpha=0.12, markersize=3)
        ax.plot(to_axis(steps[COL_X]), steps[col], color=color, linewidth=1.2 if col == "quad" else 1.6,
                linestyle="--" if col == "quad" else "-", marker="o", markersize=2.5, label=label)
    ax.axhline(spec, color="green", linestyle=":", linewidth=1.2, label=f"Spec {spec:g} mrad (X and Y)")

    best = steps.loc[steps["quad"].idxmin()]
    best_pos = float(to_axis(best[COL_X]))
    ax.plot(best_pos, best["quad"], "*", color="gold", markeredgecolor="black", markersize=16, zorder=5,
            label=f"Min quadrature: {best['quad']:.4f} mrad @ {best_pos:.1f} um")
    for row, label, marker, edge in [(back, "Back at best X", "D", "purple"),
                                     (final, "Final (after aim)", "s", "green")]:
        if row is None:
            continue
        pos = float(to_axis(row[COL_X]))
        for col, color in [(COL_DX, "#2b7be0"), (COL_DY, "#e05a2b"), ("quad", "black")]:
            ax.plot(pos, row[col], marker, color=color, markeredgecolor=edge, markeredgewidth=1.8, markersize=8,
                    zorder=6, label=(f"{label}: X {row[COL_DX]:.4f}, Y {row[COL_DY]:.4f} mrad @ {pos:.1f} um"
                                     if col == "quad" else None))
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=9)


def zoom(ax, steps, extra, to_axis, half_um: float, spec: float) -> None:
    """Limit ax to +/- half_um around the minimum, scaled to the readings inside that window."""
    best_pos = float(to_axis(steps.loc[steps["quad"].idxmin(), COL_X]))
    rows = pd.concat([steps] + [r.to_frame().T for r in extra if r is not None])
    pos = to_axis(rows[COL_X])
    inside = rows[(pos >= best_pos - half_um) & (pos <= best_pos + half_um)]
    top = max(float(inside[[COL_DX, COL_DY, "quad"]].astype(float).max().max()), 2 * spec)
    ax.set_xlim(best_pos - half_um, best_pos + half_um)
    ax.set_ylim(0, top * 1.1)
    ax.set_title(f"Zoom: +/- {half_um:g} um around the minimum", fontsize=11)
    ax.legend(fontsize=8, loc="upper center")


def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
    ap.add_argument("--gap-mm", type=float, help=f"lens-to-wall gap at CadX (mm); default {GAP_VAR} from the "
                    "run's config JSON")
    ap.add_argument("--cad-x", type=float, help="hexapod X where that gap is true (mm); default CadX from the "
                    f"run's script copy, else {CAD_X_DEFAULT}")
    ap.add_argument("--spec", type=float, default=DIV_SPEC_DEFAULT, help="divergence spec line (mrad)")
    ap.add_argument("--zoom-um", type=float, default=ZOOM_DEFAULT_UM,
                    help=f"half-width of the zoom panel around the minimum (um, default {ZOOM_DEFAULT_UM:g}; "
                    "0 = no zoom panel)")
    ap.add_argument("--no-show", action="store_true", help="save the PNG without opening a window")
    a = ap.parse_args(argv)

    path = common.input_path(a.csv)
    if path is None or not path.is_file():
        print(f"No CSV found: {path}")
        return 1
    out_dir = Path(a.out_dir) if a.out_dir else path.parent / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    header = common.title_header(common.get_title(a.title), common.run_time(path))

    print(f"Reading {path}")
    try:
        df = load_csv(path)
        samples, steps, finished, end = walk_steps(df)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1
    back = reading_between(df, end, FINAL_AIM) if finished else None
    final = final_reading(df)

    walk_x0 = steps[COL_X].iloc[0]
    gap = a.gap_mm if a.gap_mm is not None else read_wall_gap(path)
    cad_x = a.cad_x if a.cad_x is not None else read_cad_x(path)
    if cad_x is None:
        cad_x = CAD_X_DEFAULT
    if gap is not None:
        print(f"Wall gap {gap:.4f} mm at CadX {cad_x:.4f} mm")
        offset_um = (gap + walk_x0 - cad_x) * 1000  # gap at the walk start (um)
        xlabel = "Lens to -X wall gap (um)"
    else:
        print(f"No {GAP_VAR} found; the X axis is the offset from the walk start.")
        offset_um = 0.0
        xlabel = "Hexapod X offset from walk start (um)"

    def to_axis(x_mm):
        return (np.asarray(x_mm, dtype=float) - walk_x0) * 1000 + offset_um

    panels = 2 if a.zoom_um > 0 else 1
    fig, axes = plt.subplots(panels, 1, figsize=(11, 6.5 + 4.5 * (panels - 1)), constrained_layout=True,
                             squeeze=False, gridspec_kw={"height_ratios": [1.4, 1][:panels]})
    axes = axes[:, 0]
    for ax in axes:
        plot(ax, samples, steps, back, final, to_axis, xlabel, a.spec)
        if gap is not None:
            top = ax.secondary_xaxis("top", functions=(lambda v: v - offset_um, lambda v: v + offset_um))
            top.set_xlabel("Hexapod X offset from walk start (um)")
    if panels == 2:
        zoom(axes[1], steps, [back, final], to_axis, a.zoom_um, a.spec)

    best = steps.loc[steps["quad"].idxmin()]
    status = "" if finished else "  (walk incomplete)"
    axes[0].set_title(f"Focus walk: {len(steps) - 1} steps{status}\nAt min quadrature: X div {best[COL_DX]:.4f} "
                      f"mrad, Y div {best[COL_DY]:.4f} mrad @ {float(to_axis(best[COL_X])):.1f} um")
    fig.suptitle(f"{header}\n{common.run_name(path)} - divergence vs X", fontsize=14, fontweight="bold")
    print(f"Focus walk: {len(steps)} readings, {len(samples)} settled samples"
          f"{'' if finished else ' (walk incomplete)'}")

    out = out_dir / f"{path.stem}_divergence_vs_gap.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
