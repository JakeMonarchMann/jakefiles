"""Plot the Hexapod X gap-close logs: the algorithmic minimum search and the broad sweep after it.

Usage:
    python divergence_sweeps.py [data.csv] [--out-dir DIR] [--title TEXT] [--no-show]

With no CSV argument, a file picker opens. The PNG is saved in a plots folder next to the CSV.
The optional hand-written title (the unit under test) is printed at the top of the plot. Unless
--title is given, a box prompts for it, pre-filled with BG-CY0000 and the cursor after it so you
can type the rest. Cancelling the box, or --title "", means no hand-written title. The plot title
also shows the run date/time (see plot_common.py).

The two phases are located from the logged script_cmd text of the recipe:
    walk  : "HEXAPOD MOVEABS X(SweepStartX)" up to (not incl.) "HEXAPOD MOVEABS X(BestX)"
    sweep : "HEXAPOD MOVEABS X(TFStartX)"    up to (not incl.) "HEXAPOD MOVEABS X(WalkBestX)"
Only settled rows (hex_moving == 0) with a found beam are plotted. Faint dots are
raw samples; solid lines are the per-position median. The star marks the lowest
worse-of(FF_div_x, FF_div_y) position, the quantity the recipe minimises.
"""

import argparse
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
POSITION_DECIMALS = 1  # um

PHASES = [
    ("Algorithmic minimum search (walk +X)", r"HEXAPOD\s+MOVEABS\s+X\(SweepStartX\)", r"HEXAPOD\s+MOVEABS\s+X\(BestX\)"),
    ("Broad through-focus sweep", r"HEXAPOD\s+MOVEABS\s+X\(TFStartX\)", r"HEXAPOD\s+MOVEABS\s+X\(WalkBestX\)"),
]


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in USECOLS, dtype={COL_CMD: str},
                     on_bad_lines="warn", encoding_errors="replace")
    missing = [c for c in USECOLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    for c in USECOLS:
        if c != COL_CMD:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.replace([np.inf, -np.inf], np.nan)


def phase_rows(df: pd.DataFrame, start_re: str, end_re: str) -> pd.DataFrame:
    cmd = df[COL_CMD].fillna("")
    starts = np.flatnonzero(cmd.str.contains(start_re, case=False, regex=True).to_numpy())
    if not starts.size:
        raise ValueError(f"No row matching /{start_re}/ in script_cmd.")
    start = starts[0]
    ends = np.flatnonzero(cmd.iloc[start:].str.contains(end_re, case=False, regex=True).to_numpy())
    stop = start + ends[0] if ends.size else len(df)  # unfinished run: plot what exists
    return df.iloc[start:stop]


def settled(rows: pd.DataFrame, origin_mm: float) -> pd.DataFrame:
    keep = (rows[COL_MOV] == 0) & (rows[COL_FOUND] == 1)
    rows = rows[keep].dropna(subset=[COL_X, COL_DX, COL_DY]).copy()
    rows["worst"] = rows[[COL_DX, COL_DY]].max(axis=1)
    rows["x_um"] = (rows[COL_X] - origin_mm) * 1000
    rows["pos"] = rows["x_um"].round(POSITION_DECIMALS)
    return rows


def plot_phase(ax, title: str, rows: pd.DataFrame) -> None:
    ax.set_title(title)
    ax.set_xlabel("Hexapod X offset from run start (um)")
    ax.set_ylabel("Far-field divergence (mrad)")
    ax.grid(True, alpha=0.3)
    if rows.empty:
        ax.text(0.5, 0.5, "No settled data", ha="center", va="center", transform=ax.transAxes)
        return
    series = [(COL_DX, "FF div X", "#2b7be0"), (COL_DY, "FF div Y", "#e05a2b"), ("worst", "Worse of X/Y", "black")]
    med = rows.groupby("pos")[[COL_DX, COL_DY, "worst"]].median()
    for col, label, color in series:
        if col != "worst":
            ax.plot(rows["x_um"], rows[col], ".", color=color, alpha=0.12, markersize=3)
        ax.plot(med.index, med[col], color=color, linewidth=1.2 if col == "worst" else 1.6,
                linestyle="--" if col == "worst" else "-", label=label)
    best_x = med["worst"].idxmin()
    best = med["worst"].min()
    ax.set_title(f"{title}\nAt min: X div {med.loc[best_x, COL_DX]:.4f} mrad, "
                 f"Y div {med.loc[best_x, COL_DY]:.4f} mrad")
    ax.plot(best_x, best, "*", color="gold", markeredgecolor="black", markersize=16, zorder=5,
            label=f"Min worse-of: {best:.4f} mrad @ {best_x:+.1f} um")
    ax.legend(fontsize=9)


def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    common.add_title_arg(ap)
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
        valid_x = df[COL_X].dropna()
        if valid_x.empty:
            raise ValueError(f"No {COL_X} readings.")
        origin = valid_x.iloc[0]  # hexapod X at run start (nominal pose)
        phases = []
        for i, (title, s, e) in enumerate(PHASES):
            try:
                phases.append((title, settled(phase_rows(df, s, e), origin)))
            except ValueError:
                if i == 0:
                    raise
                print(f"No '{title}' in this log; plotting the minimum search only.")
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    fig, axes = plt.subplots(len(phases), 1, figsize=(11, 4.5 * len(phases) + 0.5), constrained_layout=True,
                             squeeze=False)
    axes = axes[:, 0]
    fig.suptitle(f"{header}\n{common.run_name(path)} - Hexapod X divergence", fontsize=14, fontweight="bold")
    for ax, (title, rows) in zip(axes, phases):
        plot_phase(ax, title, rows)
        print(f"{title}: {len(rows)} settled samples")

    out = out_dir / f"{path.stem}_divergence_sweeps.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
