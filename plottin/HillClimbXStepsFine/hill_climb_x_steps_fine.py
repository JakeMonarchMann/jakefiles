"""Plot recorded power vs distance from the goalposts for a fine hill-climb log.

Every finite recorded scope voltage is plotted at its recorded hexapod X, including readings during
motion and Y/Z optimization. No smoothing, binning or averaging is applied. A star marks the highest
recorded voltage, also included in the title header. The scope voltage is the recorded power signal,
shown in mV (not calibrated optical power).

Usage:
    python hill_climb_x_steps_fine.py [data.csv] [--out-dir DIR] [--title TEXT] [--first-gap-um 65]
                                      [--no-show]

With no CSV argument a file picker opens. One PNG is saved in a plots folder next to the CSV unless
--out-dir is given. Title handling follows plot_common.py.

Distance uses XWallGapMm from the run's _config_ JSON plus hexapod X minus CadX from the run .txt.
Without those, StartGapUm anchors the first coarse climb; --first-gap-um overrides both. Without any
distance reference, the axis explicitly shows X relative to the first climb instead. Readings before
the last coordinate-frame change are excluded by the shared log loader. Partial logs are supported.
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

if __package__:
    from .. import plot_common as common
    from ..HillClimbXSteps import hill_climb_x_steps as base
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "HillClimbXSteps"))
    import plot_common as common
    import hill_climb_x_steps as base

COL_X = base.COL_X


@dataclass
class GapRef:
    offset_mm: float
    absolute: bool
    source: str

    @property
    def label(self) -> str:
        return ("Distance from the goalposts (um)" if self.absolute
                else "Hexapod X from the first climb (um)")


def sibling(csv_path: Path, tag: str, suffix: str) -> Path:
    return csv_path.with_name(csv_path.name.replace("_data_", tag)).with_suffix(suffix)


def recipe_values(csv_path: Path) -> dict[str, float]:
    """Numeric recipe settings (first assignment of each) from the matching run .txt."""
    recipe = sibling(csv_path, "_", ".txt")
    if not recipe.is_file():
        return {}
    text = recipe.read_text(encoding="utf-8-sig", errors="replace")
    values = {}
    for name in ("CadX", "StartGapUm"):
        match = re.search(rf"^\s*{name}\s*=\s*(-?\d+(?:\.\d+)?)", text, re.MULTILINE)
        if match:
            values[name] = float(match.group(1))
    return values


def remembered_wall_gap_mm(csv_path: Path) -> float | None:
    """XWallGapMm remembered when the run started."""
    config = sibling(csv_path, "_config_", ".json")
    if not config.is_file():
        return None
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig", errors="replace"))
        value = float(data["settings"]["persistent_vars"]["xwallgapmm"]["value"])
    except (ValueError, KeyError, TypeError) as e:
        print(f"Could not read XWallGapMm from {config}: {e}")
        return None
    if not np.isfinite(value) or value <= 0:
        print(f"Invalid XWallGapMm in {config}: {value}; using the fallback gap reference.")
        return None
    return value


def gap_reference(first_x_mm: float, first_gap_um: float | None, wall_mm: float | None,
                  params: dict[str, float]) -> GapRef:
    if first_gap_um is not None:
        if not np.isfinite(first_gap_um):
            raise ValueError("--first-gap-um must be finite.")
        return GapRef(first_gap_um / 1000 - first_x_mm, True, "--first-gap-um")
    if wall_mm is not None and "CadX" in params:
        return GapRef(wall_mm - params["CadX"], True,
                      f"XWallGapMm {wall_mm * 1000:.2f} um (run config)")
    if "StartGapUm" in params:
        return GapRef(params["StartGapUm"] / 1000 - first_x_mm, True,
                      "StartGapUm at the first coarse climb")
    return GapRef(-first_x_mm, False, "none (X from the first climb)")


def first_climb_x_mm(df: pd.DataFrame) -> float:
    for rows in base.optimize_runs(df):
        settled = rows[rows[base.COL_MOV] == 0]
        if not settled.empty:
            return float(settled[COL_X].median())
    print("No settled climb found: anchoring distance at the first recorded X.")
    return float(df[COL_X].iloc[0])


def plot_recorded_power(df: pd.DataFrame, ref: GapRef, heading: str):
    samples = df.dropna(subset=[COL_X, "v_mV"])
    if samples.empty:
        raise ValueError("No recorded scope voltage readings in this log.")
    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    ax.plot((samples[COL_X] + ref.offset_mm) * 1000, samples["v_mV"], ".",
            color="#2b7be0", markersize=2, alpha=0.4, linestyle="none")
    best = samples.iloc[int(np.argmax(samples["v_mV"].to_numpy()))]
    ax.plot((best[COL_X] + ref.offset_mm) * 1000, best["v_mV"], "*",
            color="#ffcc00", markeredgecolor="black", markersize=16, zorder=5)
    ax.set_xlabel(ref.label)
    ax.set_ylabel("Recorded power signal (scope voltage, mV)")
    header, _, subtitle = heading.partition("\n")
    header = f"{header} | " if header else ""
    description = ("Recorded power vs distance from the goalposts" if ref.absolute
                   else "Recorded power vs relative hexapod X")
    ax.set_title(f"{header}Max voltage: {best['v_mV']:.3f} mV\n{subtitle}{description}")
    ax.grid(True, alpha=0.3)
    return fig


def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
    ap.add_argument("--first-gap-um", type=float,
                    help="distance from the goalposts at the first coarse climb (um); "
                         "overrides the run's files")
    ap.add_argument("--no-show", action="store_true", help="save the PNG without opening a window")
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
        ref = gap_reference(first_climb_x_mm(df), a.first_gap_um,
                            remembered_wall_gap_mm(path), params)
        print(f"Gap reference: {ref.source}")
        if not ref.absolute:
            print("No absolute distance reference found; showing relative X. "
                  "Use --first-gap-um to supply the distance at the first climb.")
        heading = f"{header}\n{common.run_name(path)} - "
        fig = plot_recorded_power(df, ref, heading)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{path.stem}_fine_recorded_power.png"
        fig.savefig(out, dpi=150)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
