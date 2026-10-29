"""Plot a Trawler "Lens Aperture YZ Hill Climb X Steps FINE" log: how the run homes in on the peak.

The FINE recipe hill-climbs the PicoScope voltage in hexapod Y/Z at each X from StartGapUm to EndGapUm in
coarse steps, then runs FineRounds rounds that each go to the best light so far, halve the X step and climb
at that peak +/- FineSideSteps steps, and ends with one fine Y/Z climb at the final peak. Three PNGs:

    1. homing_in    light vs gap for every climb, one colour per phase, the point each phase picked and
                    arrows from pick to pick; under it a funnel of the X points each phase visited inside
                    the window it was allowed to search, narrowing onto the peak (a pick at the edge
                    of what its phase visited is flagged in red: the peak may lie beyond it)
    2. convergence  per phase: the picked gap (+/- half the X step), the X step and range halving, the
                    light picked and its spread across the phase, and the Y/Z each climb parked at
    3. time_history voltage, gap, Y and Z against time, each climb shaded in its phase colour

Usage:
    python hill_climb_x_steps_fine.py [data.csv] [--out-dir DIR] [--title TEXT] [--first-gap-um 65]
                                      [--no-show]

With no CSV argument a file picker opens. PNGs and a per-climb summary CSV are saved in a plots folder
next to the CSV unless --out-dir is given. Title handling is the same as every plottin script
(plot_common.py).

Phases come from the script commands logged between climbs: a DWELL ParkSettleMs (going to the peak)
starts a new phase, a DWELL FineSettleMs means a fine X point, and the FinalOpt... settings or a go-to-peak
with no fine point after it mean the final climb. A climb's light is the parked read the script takes after
it (settled rows within READ_WINDOW_S of the climb ending; median, so it can differ slightly from the
script's own unlogged 20-read mean). Each phase's pick is the climb the next go-to-peak move (the last
DWELL ParkSettleMs before the next phase) lands on, i.e. exactly what the script chose; a phase with no
next phase yet falls back to its highest light. A climb still running when the log ends uses its best
visited point as its light.

The gap to the aperture is worked out as the script does: XWallGapMm (from the run's _config_ JSON) plus
hexapod X minus CadX (from the run .txt). Without those, the first coarse climb is taken to sit StartGapUm
from the aperture; --first-gap-um overrides both. Y/Z are microns from CadY/CadZ (0 if not found).
Safe on partial or still-running logs.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

if __package__:
    from .. import plot_common as common
    from ..HillClimbXSteps import hill_climb_x_steps as base
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "HillClimbXSteps"))
    import plot_common as common
    import hill_climb_x_steps as base

FINAL = 99                  # phase key of the final climb (coarse is 0, fine rounds are 1..FineRounds)
READ_WINDOW_S = 3.0         # the parked read is taken within this long after a climb ends
PICK_MATCH_MM = 0.002       # a go-to-peak move must land this close to a climb's parked point to name it
EDGE_TOL_UM = 0.1           # a pick this close to the nearest/farthest X its phase visited is "at the edge"
FINAL_COLOR = "#ffcc00"
PHASE_CMAP = "plasma"

COL_T, COL_MOV = base.COL_T, base.COL_MOV
COL_X, COL_Y, COL_Z = base.COL_X, base.COL_Y, base.COL_Z
RECIPE_NAMES = ("CadX", "CadY", "CadZ", "StartGapUm", "EndGapUm", "CoarseStepUm", "FineRounds",
                "FineSideSteps", "RangeTolUm")


@dataclass
class Climb:
    index: int
    phase: int
    rows: pd.DataFrame      # every valid row of the climb
    points: pd.DataFrame    # settled rows collapsed to visited Y/Z points
    x_mm: float             # hexapod X during the climb
    y_mm: float             # hexapod Y/Z where the climb parked
    z_mm: float
    light_mV: float         # parked read after the climb (what the script compares)
    from_read: bool         # False: no parked read in the log, light_mV is the climb's best point
    t_end: float
    went_to: tuple[float, float, float] | None  # X/Y/Z (mm) of the go-to-peak move just before this climb
    gap_um: float = np.nan  # set by anchor(): gap to the aperture (or X from the first climb)
    y_um: float = np.nan    # set by anchor(): from CadY / CadZ
    z_um: float = np.nan


@dataclass
class Phase:
    key: int
    climbs: list[Climb]
    step_um: float | None               # X step used in this phase (None for the final climb)
    window: tuple[float, float] | None  # gaps this phase was allowed to visit
    color: tuple | str
    best: Climb                         # the phase's pick
    confirmed: bool                     # True: the script was seen going to it; False: highest light so far

    @property
    def label(self) -> str:
        return {0: "Coarse", FINAL: "Final climb"}.get(self.key, f"Round {self.key}")

    @property
    def short(self) -> str:
        return {0: "Coarse", FINAL: "Final"}.get(self.key, f"R{self.key}")

    @property
    def tick(self) -> str:
        return f"{self.label}\n" + ("Y/Z only" if self.step_um is None else f"{self.step_um:.3g} um step")

    @property
    def at_edge(self) -> bool:
        """The pick is the nearest or farthest X the phase visited: the peak may lie outside its window."""
        gaps = [c.gap_um for c in self.climbs]
        if self.key == FINAL or len(gaps) < 3:
            return False
        return min(abs(self.best.gap_um - min(gaps)), abs(self.best.gap_um - max(gaps))) < EDGE_TOL_UM


@dataclass
class GapRef:
    offset_mm: float        # gap_um = (hexapod X + offset_mm) * 1000
    absolute: bool          # False: no reference, the "gap" is X from the first climb
    source: str
    cad_y: float
    cad_z: float

    @property
    def label(self) -> str:
        return "Gap to the aperture (um)" if self.absolute else "Hexapod X from the first climb (um)"


# ---------------------------------------------------------------------- inputs
def sibling(csv_path: Path, tag: str, suffix: str) -> Path:
    return csv_path.with_name(csv_path.name.replace("_data_", tag)).with_suffix(suffix)


def recipe_values(csv_path: Path) -> dict[str, float]:
    """Numeric recipe settings (first assignment of each) from the matching run .txt, else {}."""
    recipe = sibling(csv_path, "_", ".txt")
    if not recipe.is_file():
        return {}
    text = recipe.read_text(encoding="utf-8-sig", errors="replace")
    values = {}
    for name in RECIPE_NAMES:
        match = re.search(rf"^\s*{name}\s*=\s*(-?\d+(?:\.\d+)?)", text, re.MULTILINE)
        if match:
            values[name] = float(match.group(1))
    return values


def remembered_wall_gap_mm(csv_path: Path) -> float | None:
    """XWallGapMm as remembered when the run started (persistent vars in the run's config JSON)."""
    config = sibling(csv_path, "_config_", ".json")
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig", errors="replace"))
        value = float(data["settings"]["persistent_vars"]["xwallgapmm"]["value"])
    except (ValueError, KeyError, TypeError):
        return None
    return value if value > 0 else None


def gap_reference(first_x_mm: float, first_gap_um: float | None, wall_mm: float | None,
                  params: dict[str, float]) -> GapRef:
    cad_y, cad_z = params.get("CadY", 0.0), params.get("CadZ", 0.0)
    if first_gap_um is not None:
        return GapRef(first_gap_um / 1000 - first_x_mm, True, "--first-gap-um", cad_y, cad_z)
    if wall_mm is not None and "CadX" in params:
        return GapRef(wall_mm - params["CadX"], True, f"XWallGapMm {wall_mm * 1000:.2f} um (run config)",
                      cad_y, cad_z)
    if "StartGapUm" in params:
        return GapRef(params["StartGapUm"] / 1000 - first_x_mm, True,
                      "StartGapUm at the first coarse climb", cad_y, cad_z)
    return GapRef(-first_x_mm, False, "none (X from the first climb)", cad_y, cad_z)


# -------------------------------------------------------------------- analysis
def commands(df: pd.DataFrame) -> pd.Series:
    return df[base.COL_CMD].fillna("").str.split("//").str[0].str.strip().str.upper()


def phase_keys(cmd: pd.Series, runs: list[pd.DataFrame]) -> list[int]:
    """Phase of each climb, from the commands logged since the previous climb (and its own setup)."""
    keys, round_no, prev_end = [], 0, -1
    for rows in runs:
        logged = " | ".join(cmd.iloc[prev_end + 1:rows.index[-1] + 1].unique())
        to_peak = any(m in logged for m in ("PARKSETTLEMS", "(PEAKX)", "(PEAKY)"))
        fine = any(m in logged for m in ("FINESETTLEMS", "(FINEX)"))
        if "FINALOPT" in logged or (to_peak and not fine):
            keys.append(FINAL)
        elif to_peak:
            round_no += 1
            keys.append(round_no)
        elif fine:
            keys.append(max(round_no, 1))
        else:
            keys.append(0)
        prev_end = rows.index[-1]
    return keys


def parked_rows(df: pd.DataFrame, cmd: pd.Series, end: int) -> pd.DataFrame:
    """Settled rows of the parked read after a climb: up to the next move/climb, within READ_WINDOW_S."""
    after = df.iloc[end + 1:]
    stop = cmd.loc[after.index].str.startswith(("HEXAPOD", "OPTIMIZE"))
    if stop.any():
        after = after.loc[:stop.idxmax() - 1]
    after = after[(after[COL_T] <= df.at[end, COL_T] + READ_WINDOW_S) & (after[COL_MOV] == 0)]
    return after.dropna(subset=["v_mV"])


def went_to_peak(df: pd.DataFrame, cmd: pd.Series, start: int, end: int):
    """X/Y/Z (mm) at the end of the last DWELL ParkSettleMs in rows start..end, else None.

    The script's go-to-peak moves each finish with that dwell; after the last one the hexapod sits on the
    peak it picked, which is the only exact record of the pick (its 20-read averages are not logged).
    """
    park = cmd.iloc[start:end].str.contains("PARKSETTLEMS", regex=False)
    if not park.any():
        return None
    last = park[park].index[-1]
    tail = df.loc[last - 2:last, [COL_X, COL_Y, COL_Z]].dropna()
    return tuple(tail.median()) if len(tail) else None


def build_climbs(df: pd.DataFrame) -> list[Climb]:
    cmd = commands(df)
    runs = base.optimize_runs(df)
    climbs, prev_end = [], -1
    for rows, key in zip(runs, phase_keys(cmd, runs)):
        went_to = went_to_peak(df, cmd, prev_end + 1, rows.index[0])
        prev_end = rows.index[-1]
        pts = base.point_table(rows)
        if pts.empty:
            print(f"Skipping a climb at t={rows[COL_T].iloc[0]:.1f}s: no settled rows.")
            continue
        eligible = pts[pts["n"] >= base.MIN_POINT_SAMPLES]
        peak = (eligible if len(eligible) else pts)["v_mV"].max()
        settled = rows[rows[COL_MOV] == 0]
        read = parked_rows(df, cmd, rows.index[-1])
        where = read if len(read) else settled.tail(1)
        climbs.append(Climb(
            index=len(climbs), phase=key, rows=rows, points=pts, x_mm=float(settled[COL_X].median()),
            y_mm=float(where[COL_Y].median()), z_mm=float(where[COL_Z].median()),
            light_mV=float(read["v_mV"].median()) if len(read) else float(peak),
            from_read=bool(len(read)), t_end=float(rows[COL_T].iloc[-1]), went_to=went_to))
    return climbs


def script_pick(members: list[Climb], later: list[Climb]) -> Climb | None:
    """The climb of a phase that the next go-to-peak move went to (nearest in X/Y/Z), if it was logged."""
    target = next((c.went_to for c in later if c.went_to is not None), None)
    if target is None:
        return None
    dist = [np.hypot(np.hypot(c.x_mm - target[0], c.y_mm - target[1]), c.z_mm - target[2])
            for c in members]
    best = int(np.argmin(dist))
    return members[best] if dist[best] <= PICK_MATCH_MM else None


def anchor(climbs: list[Climb], ref: GapRef) -> None:
    for c in climbs:
        c.gap_um = (c.x_mm + ref.offset_mm) * 1000
        c.y_um = (c.y_mm - ref.cad_y) * 1000
        c.z_um = (c.z_mm - ref.cad_z) * 1000


def build_phases(climbs: list[Climb], params: dict[str, float], ref: GapRef) -> list[Phase]:
    keys = sorted({c.phase for c in climbs})
    coarse_gaps = sorted(c.gap_um for c in climbs if c.phase == 0)
    if len(coarse_gaps) >= 2:
        coarse_step = float(np.median(np.diff(coarse_gaps)))
    else:
        coarse_step = params.get("CoarseStepUm")

    lo = hi = None
    if ref.absolute and coarse_gaps:
        lo, hi = coarse_gaps[0], params.get("EndGapUm", coarse_gaps[-1])
    tol = params.get("RangeTolUm", 0.1)
    side = params.get("FineSideSteps", 3)

    n_rounds = max([k for k in keys if k != FINAL] + [int(params.get("FineRounds", 0))])
    cmap = plt.get_cmap(PHASE_CMAP)
    phases: list[Phase] = []
    for key in keys:
        members = [c for c in climbs if c.phase == key]
        pick = script_pick(members, [c for c in climbs if c.phase > key])
        confirmed = pick is not None or key == FINAL
        if pick is None:
            pick = max(members, key=lambda c: c.light_mV)
        color = FINAL_COLOR if key == FINAL else cmap(0.05 + 0.7 * key / max(n_rounds, 1))
        step = None if key == FINAL or coarse_step is None else coarse_step / 2 ** key
        window = None
        if key == 0 and coarse_gaps:
            window = (lo, hi) if lo is not None else (coarse_gaps[0], coarse_gaps[-1])
        elif phases:
            centre = phases[-1].best.gap_um
            half = 0.0 if step is None else side * step
            window = (centre - half, centre + half)
            if lo is not None:
                window = (max(window[0], lo - tol), min(window[1], hi + tol))
        phases.append(Phase(key, members, step, window, color, pick, confirmed))
    return phases


def summary_frame(climbs: list[Climb], phases: list[Phase]) -> pd.DataFrame:
    picks = {id(p.best) for p in phases}
    label = {p.key: p.label for p in phases}
    return pd.DataFrame([{
        "climb": c.index, "phase": label[c.phase], "t_start_s": c.rows[COL_T].iloc[0], "t_end_s": c.t_end,
        "gap_um": c.gap_um, "light_mV": c.light_mV,
        "light_from": "parked read" if c.from_read else "climb best point",
        "parked_y_um": c.y_um, "parked_z_um": c.z_um, "points": len(c.points), "picked": id(c) in picks,
    } for c in climbs])


def phase_frame(phases: list[Phase]) -> pd.DataFrame:
    rows = []
    for p in phases:
        gaps = [c.gap_um for c in p.climbs]
        light = [c.light_mV for c in p.climbs]
        rows.append({"phase": p.label, "climbs": len(p.climbs), "step_um": p.step_um,
                     "visited_um": max(gaps) - min(gaps), "pick_gap_um": p.best.gap_um,
                     "pick_mV": p.best.light_mV, "spread_mV": max(light) - min(light),
                     "pick_y_um": p.best.y_um, "pick_z_um": p.best.z_um,
                     "pick_from": "script went there" if p.confirmed else "highest light",
                     "at_edge": p.at_edge})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------- plots
def pick_resolution(phases: list[Phase]) -> list[float]:
    """+/- half the X step for each phase's pick (the final climb keeps the last step it was given)."""
    out, last = [], 0.0
    for p in phases:
        if p.step_um is not None:
            last = p.step_um / 2
        out.append(last)
    return out


def plot_homing_in(phases: list[Phase], ref: GapRef, heading: str):
    fig, (top, funnel) = plt.subplots(2, 1, figsize=(12, 10), sharex=True, constrained_layout=True,
                                      gridspec_kw={"height_ratios": [3, 2]})
    for p in phases:
        cs = sorted(p.climbs, key=lambda c: c.gap_um)
        gaps, light = [c.gap_um for c in cs], [c.light_mV for c in cs]
        if p.key == FINAL:
            top.plot(gaps, light, "*", color=FINAL_COLOR, mec="black", markersize=22, zorder=6,
                     label=f"{p.label}: {p.best.light_mV:.3f} mV at {p.best.gap_um:.2f} um")
            continue
        step = "" if p.step_um is None else f", {p.step_um:.3g} um steps"
        note = "" if p.confirmed else " (highest so far)"
        top.plot(gaps, light, "o-", color=p.color, markersize=6, linewidth=1.4, zorder=3,
                 label=f"{p.label}{step}: pick {p.best.gap_um:.2f} um{note}")
        top.plot(p.best.gap_um, p.best.light_mV, "o", markersize=16, mfc="none", mec=p.color, mew=2.2,
                 zorder=5)
    for a, b in zip(phases, phases[1:]):
        top.annotate("", xy=(b.best.gap_um, b.best.light_mV), xytext=(a.best.gap_um, a.best.light_mV),
                     arrowprops=dict(arrowstyle="-|>", color="black", lw=1.2, shrinkA=9, shrinkB=9),
                     zorder=7)
    top.plot([], [], "o", markersize=12, mfc="none", mec="black", mew=1.8,
             label="ring = the phase's pick (where the script went next); arrows follow it")
    top.set_ylabel("Light after the climb (mV)")
    top.set_title("Parked light after every Y/Z climb (light drifts between phases; each round "
                  "re-measures its centre)", fontsize=10)
    top.legend(fontsize=9, loc="best")

    ys = np.arange(len(phases))
    for y, p in zip(ys, phases):
        if p.window is not None:
            funnel.hlines(y, *p.window, color=p.color, linewidth=14, alpha=0.2, zorder=1)
        gaps = np.array([c.gap_um for c in p.climbs])
        light = np.array([c.light_mV for c in p.climbs])
        span = np.ptp(light)
        size = 30 + 130 * ((light - light.min()) / span if span > 0 else np.ones_like(light))
        funnel.scatter(gaps, np.full(len(gaps), y), s=size, color=p.color, edgecolor="black",
                       linewidth=0.4, zorder=3)
        funnel.plot(p.best.gap_um, y, "*", color=FINAL_COLOR, mec="black", markersize=17, zorder=5)
        edge = p.at_edge
        funnel.annotate(f"{p.best.gap_um:.2f}" + (" - at window edge" if edge else ""),
                        (p.best.gap_um, y), xytext=(0, 11), textcoords="offset points", ha="center",
                        fontsize=8, fontweight="bold", color="#d62728" if edge else "black")
    funnel.plot([p.best.gap_um for p in phases], ys, "-", color="black", linewidth=1, zorder=4)
    funnel.set_yticks(ys, [p.tick for p in phases], fontsize=9)
    funnel.set_ylim(len(phases) - 0.4, -0.6)
    funnel.set_xlabel(ref.label)
    funnel.set_title("X points each phase visited (band = window it may search, dot size = light "
                     "within the phase, star = pick)", fontsize=10)

    if ref.absolute and phases[0].key == 0 and phases[0].window is not None:
        for ax in (top, funnel):
            for edge in phases[0].window:
                ax.axvline(edge, color="#777777", linestyle=":", linewidth=1, zorder=0)
    for ax in (top, funnel):
        ax.grid(True, alpha=0.3)
    fig.suptitle(f"{heading}Homing in on the peak", fontsize=14, fontweight="bold")
    return fig


def plot_convergence(phases: list[Phase], ref: GapRef, heading: str):
    fig, axes = plt.subplots(2, 2, figsize=(13, 9.5), constrained_layout=True)
    ax_gap, ax_step, ax_light, ax_yz = axes.flat
    xs = np.arange(len(phases))

    for x, p in zip(xs, phases):
        ax_gap.plot(np.full(len(p.climbs), x), [c.gap_um for c in p.climbs], "o", color=p.color,
                    alpha=0.35, markersize=5)
    best_gap = [p.best.gap_um for p in phases]
    ax_gap.errorbar(xs, best_gap, yerr=pick_resolution(phases), fmt="-", color="black", capsize=6,
                    linewidth=1, zorder=4)
    ax_gap.plot(xs, best_gap, "*", color=FINAL_COLOR, mec="black", markersize=15, zorder=5)
    ax_gap.axhline(best_gap[-1], color="#555555", linestyle="--", linewidth=1)
    ax_gap.set_ylabel(ref.label)
    ax_gap.set_title("Picked gap (error bar: +/- half the X step); dots = points visited", fontsize=10)

    steps = [(x, p.step_um) for x, p in zip(xs, phases) if p.step_um]
    widths = [(x, np.ptp([c.gap_um for c in p.climbs])) for x, p in zip(xs, phases)]
    widths = [(x, w) for x, w in widths if w > 0]
    if steps:
        ax_step.plot(*zip(*steps), "o-", color="#2b7be0", linewidth=1.8, label="X step")
        for x, s in steps:
            ax_step.annotate(f"{s:.3g}", (x, s), xytext=(6, 4), textcoords="offset points", fontsize=8)
    if widths:
        ax_step.plot(*zip(*widths), "s--", color="#e05a2b", label="X range visited")
        for x, w in widths:
            ax_step.annotate(f"{w:.3g}", (x, w), xytext=(6, -11), textcoords="offset points", fontsize=8)
    if steps or widths:
        ax_step.set_yscale("log")
        ax_step.legend(fontsize=9)
    ax_step.set_ylabel("um (log scale)")
    ax_step.set_title("X step and range visited halve each round", fontsize=10)

    for x, p in zip(xs, phases):
        light = [c.light_mV for c in p.climbs]
        ax_light.vlines(x, min(light), max(light), color=p.color, linewidth=6, alpha=0.5)
        ax_light.annotate(f"{p.best.light_mV:.3f}", (x, p.best.light_mV), xytext=(9, -3),
                          textcoords="offset points", fontsize=8)
    ax_light.plot(xs, [p.best.light_mV for p in phases], "-", color="black", linewidth=1, zorder=4)
    ax_light.plot(xs, [p.best.light_mV for p in phases], "*", color=FINAL_COLOR, mec="black",
                  markersize=15, zorder=5)
    ax_light.set_ylabel("Light after the climb (mV)")
    ax_light.set_title("Picked light (star) and the range of light across the phase's climbs (bar)",
                       fontsize=10)

    for p in phases:
        ax_yz.plot([c.y_um for c in p.climbs], [c.z_um for c in p.climbs], "o", color=p.color,
                   alpha=0.55, markersize=6, label=p.label)
    for a, b in zip(phases, phases[1:]):
        ax_yz.annotate("", xy=(b.best.y_um, b.best.z_um), xytext=(a.best.y_um, a.best.z_um),
                       arrowprops=dict(arrowstyle="-|>", color="black", lw=1, shrinkA=4, shrinkB=4))
    ax_yz.plot(phases[-1].best.y_um, phases[-1].best.z_um, "*", color=FINAL_COLOR, mec="black",
               markersize=18, zorder=5, label="Last pick")
    ax_yz.set_xlabel("Hexapod Y from CAD nominal (um)")
    ax_yz.set_ylabel("Hexapod Z from CAD nominal (um)")
    ax_yz.set_aspect("equal", adjustable="datalim")
    ax_yz.legend(fontsize=8)
    ax_yz.set_title("Where each climb parked in Y/Z (arrows follow the pick)", fontsize=10)

    for ax in (ax_gap, ax_step, ax_light):
        ax.set_xticks(xs, [p.short for p in phases])
        ax.set_xlim(-0.5, len(phases) - 0.5)
    for ax in axes.flat:
        ax.grid(True, alpha=0.3, which="both")
    fig.suptitle(f"{heading}Convergence by phase", fontsize=14, fontweight="bold")
    return fig


def plot_time_history(df: pd.DataFrame, phases: list[Phase], ref: GapRef, heading: str):
    fig, axes = plt.subplots(4, 1, figsize=(12, 9.5), sharex=True, constrained_layout=True)
    t = df[COL_T]
    settled = df[(df[COL_MOV] == 0) & df["v_mV"].notna()]
    picks_t = [p.best.t_end for p in phases]
    axes[0].plot(settled[COL_T], settled["v_mV"], ".", color="#2b7be0", alpha=0.25, markersize=2)
    axes[0].plot(picks_t, [p.best.light_mV for p in phases], "*", color=FINAL_COLOR, mec="black",
                 markersize=13, zorder=5)
    axes[0].set_ylabel("Scope voltage (mV)")
    traces = ((axes[1], (df[COL_X] + ref.offset_mm) * 1000, ref.label, "#e05a2b"),
              (axes[2], (df[COL_Y] - ref.cad_y) * 1000, "Y from CAD (um)", "#2a9d5c"),
              (axes[3], (df[COL_Z] - ref.cad_z) * 1000, "Z from CAD (um)", "#8a4fd0"))
    for ax, values, label, color in traces:
        ax.plot(t, values, color=color, linewidth=0.8)
        ax.set_ylabel(label)
    axes[1].plot(picks_t, [p.best.gap_um for p in phases], "*", color=FINAL_COLOR, mec="black",
                 markersize=13, zorder=5)
    axes[3].set_xlabel("Time since log start (s)")
    for ax in axes:
        ax.grid(True, alpha=0.3)
        for p in phases:
            for c in p.climbs:
                ax.axvspan(c.rows[COL_T].iloc[0], c.t_end, color=p.color, alpha=0.15, lw=0)
    handles = [Patch(color=p.color, alpha=0.4, label=p.label) for p in phases]
    handles.append(Line2D([], [], marker="*", linestyle="", color=FINAL_COLOR, mec="black",
                          markersize=12, label="Phase pick"))
    axes[0].legend(handles=handles, fontsize=8, loc="lower right", ncol=len(handles))
    fig.suptitle(f"{heading}Time history (shaded = climbs, coloured by phase)", fontsize=13,
                 fontweight="bold")
    return fig


# ------------------------------------------------------------------------- main
def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
    ap.add_argument("--first-gap-um", type=float,
                    help="gap to the aperture at the first coarse climb (um); overrides the run's files")
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
        df = base.add_offsets(base.load_csv(path))
        params = recipe_values(path)
        climbs = build_climbs(df)
        if not climbs:
            raise ValueError("No OPTIMIZE climbs with settled readings in this log.")
        first_x = next((c.x_mm for c in climbs if c.phase == 0), climbs[0].x_mm)
        ref = gap_reference(first_x, a.first_gap_um, remembered_wall_gap_mm(path), params)
        anchor(climbs, ref)
        phases = build_phases(climbs, params, ref)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    print(f"Gap reference: {ref.source}")
    if not params:
        print("No run .txt recipe found: CAD nominal taken as Y/Z 0 and the range from the log.")
    unread = [c.index for c in climbs if not c.from_read]
    if unread:
        print(f"No parked read after climb(s) {unread}: their best visited point is used instead.")

    out_dir.mkdir(parents=True, exist_ok=True)
    heading = f"{header}\n{common.run_name(path)} - "
    summary_frame(climbs, phases).to_csv(out_dir / f"{path.stem}_fine_summary.csv", index=False)
    print(phase_frame(phases).round(3).to_string(index=False))
    for p in phases:
        if p.at_edge:
            print(f"Note: {p.label} picked {p.best.gap_um:.2f} um, the edge of the X it visited; "
                  "the peak may lie beyond its window.")

    figures = {
        "homing_in": plot_homing_in(phases, ref, heading),
        "convergence": plot_convergence(phases, ref, heading),
        "time_history": plot_time_history(df, phases, ref, heading),
    }
    for suffix, fig in figures.items():
        out = out_dir / f"{path.stem}_fine_{suffix}.png"
        fig.savefig(out, dpi=150)
        print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
