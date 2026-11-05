"""Plot far-field X and Y divergence against Hexapod X movement from the start of the script.

Usage:
    python divergence_vs_hexapod_x.py [data.csv | run folder] [--out-dir DIR] [--title TEXT] [--no-show]

With no argument, a file picker opens. A run folder must contain exactly one *_data_*.csv.
The PNG is saved in a plots folder next to the CSV. The optional hand-written title (the unit
under test) is printed at the top of the plot (see plot_common.py).

The whole log is used. X movement is hexapod X minus its first reading, in um. Only settled rows
(hex_moving == 0) with a found beam are plotted. Faint dots are raw samples; solid lines are the
per-position median, so the walk out, the walk back and any sweep over the same positions are merged.
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

COL_MOV, COL_X, COL_DX, COL_DY, COL_FOUND = (
    "hex_moving", "hex_X_mm", "ff_div_x_mrad", "ff_div_y_mrad", "ff_beam_found")
USECOLS = [COL_MOV, COL_X, COL_DX, COL_DY, COL_FOUND]
POSITION_BIN_UM = 0.25  # readings within a bin share one median point


def resolve_csv(path: Path) -> Path:
    if not path.is_dir():
        return path
    found = sorted(path.glob("*_data_*.csv"))
    if len(found) != 1:
        raise ValueError(f"Expected exactly one *_data_*.csv in {path}, found {len(found)}.")
    return found[0]


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in USECOLS, on_bad_lines="warn", encoding_errors="replace")
    missing = [c for c in USECOLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    return df.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)


def settled(df: pd.DataFrame, origin_mm: float) -> pd.DataFrame:
    rows = df[(df[COL_MOV] == 0) & (df[COL_FOUND] == 1)].dropna(subset=[COL_X, COL_DX, COL_DY]).copy()
    rows["x_um"] = (rows[COL_X] - origin_mm) * 1000
    rows["pos"] = (rows["x_um"] / POSITION_BIN_UM).round() * POSITION_BIN_UM
    return rows


def plot(ax, rows: pd.DataFrame) -> None:
    ax.set_xlabel("Hexapod X movement from script start (um)")
    ax.set_ylabel("Far-field divergence (mrad)")
    ax.grid(True, alpha=0.3)
    ax.axvline(0, color="grey", linestyle=":", linewidth=1)
    med = rows.groupby("pos")[[COL_DX, COL_DY]].median()
    for col, label, color in ((COL_DX, "FF div X", "#2b7be0"), (COL_DY, "FF div Y", "#e05a2b")):
        ax.plot(rows["x_um"], rows[col], ".", color=color, alpha=0.12, markersize=3)
        ax.plot(med.index, med[col], color=color, linewidth=1.6, label=label)
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
    if path is None or not path.exists():
        print(f"No CSV found: {path}")
        return 1
    try:
        path = resolve_csv(path)
        print(f"Reading {path}")
        df = load_csv(path)
        valid_x = df[COL_X].dropna()
        if valid_x.empty:
            raise ValueError(f"No {COL_X} readings.")
        rows = settled(df, valid_x.iloc[0])
        if rows.empty:
            raise ValueError("No settled rows with a found beam.")
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    out_dir = Path(a.out_dir) if a.out_dir else path.parent / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    header = common.title_header(common.get_title(a.title), common.run_time(path))

    fig, ax = plt.subplots(figsize=(11, 5.5), constrained_layout=True)
    fig.suptitle(f"{header}\n{common.run_name(path)} - Divergence vs Hexapod X", fontsize=14, fontweight="bold")
    plot(ax, rows)
    print(f"{len(rows)} settled samples")

    out = out_dir / f"{path.stem}_divergence_vs_hexapod_x.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
