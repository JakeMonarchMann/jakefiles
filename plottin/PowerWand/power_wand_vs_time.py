"""Thorlabs power wand reading vs time from a Trawler data CSV.

Usage:
    python power_wand_vs_time.py [data.csv] [--out FILE] [--title TEXT] [--no-max] [--no-show]
                                 [--t-min S] [--t-max S]

With no CSV argument a file picker opens. Plots pm_Wand_mW (live reading) and, unless --no-max,
pm_Wand_max_mW (the meter's running max) against time_elapsed_s. --t-min / --t-max crop the plot
to that time window (s); the peak is then the peak inside the window. The PNG is saved next to the
CSV as <run>_power_wand.png (<run>_power_wand_<t-min>-<t-max>s.png when cropped) unless --out is
given. Title handling follows plot_common.py.
"""
import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

if __package__:
    from .. import plot_common as common
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import plot_common as common

CSV_PATH = None
COL_T = "time_elapsed_s"
COL_P = "pm_Wand_mW"
COL_PMAX = "pm_Wand_max_mW"


def load(path: Path) -> pd.DataFrame:
    header = pd.read_csv(path, nrows=0).columns
    if COL_P not in header:
        raise SystemExit(f"{path.name} has no {COL_P} column")
    cols = [c for c in (COL_T, COL_P, COL_PMAX) if c in header]
    df = pd.read_csv(path, usecols=cols).apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=[COL_T, COL_P])


def plot(df: pd.DataFrame, path: Path, title: str, show_max: bool) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(df[COL_T], df[COL_P], lw=1.2, label="Wand power")
    if show_max and COL_PMAX in df:
        ax.plot(df[COL_T], df[COL_PMAX], lw=1, ls="--", color="tab:red", alpha=0.7,
                label="Wand max (running)")
    peak = df[COL_P].max()
    t_peak = df.loc[df[COL_P].idxmax(), COL_T]
    ax.plot(t_peak, peak, "o", color="tab:red", ms=5)
    ax.annotate(f"peak {peak:.3f} mW @ {t_peak:.2f} s", (t_peak, peak),
                textcoords="offset points", xytext=(8, -14), fontsize=9)
    ax.set_xlabel("Time elapsed (s)")
    ax.set_ylabel("Power (mW)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    header = common.title_header(title, common.run_time(path))
    ax.set_title(f"{header}\n{common.run_name(path)}: Thorlabs power wand vs time")
    fig.tight_layout()
    return fig


def main() -> None:
    common.setup_stdout()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", help="Trawler data CSV")
    parser.add_argument("--out", help="output PNG path")
    parser.add_argument("--t-min", type=float, help="crop: first time to plot (s)")
    parser.add_argument("--t-max", type=float, help="crop: last time to plot (s)")
    parser.add_argument("--no-max", action="store_true", help="hide the running-max trace")
    parser.add_argument("--no-show", action="store_true", help="save without opening a window")
    common.add_title_arg(parser)
    args = parser.parse_args()

    path = common.input_path(args.csv, CSV_PATH)
    if path is None:
        raise SystemExit("No CSV selected.")
    df = load(path)
    if args.t_min is not None:
        df = df[df[COL_T] >= args.t_min]
    if args.t_max is not None:
        df = df[df[COL_T] <= args.t_max]
    if df.empty:
        raise SystemExit(f"No power wand data in {path.name} for the requested time window")
    title = common.get_title(args.title)

    fig = plot(df, path, title, show_max=not args.no_max)
    crop = ""
    if args.t_min is not None or args.t_max is not None:
        lo = f"{args.t_min:g}" if args.t_min is not None else "start"
        hi = f"{args.t_max:g}" if args.t_max is not None else "end"
        crop = f"_{lo}-{hi}s"
    out = (Path(args.out) if args.out
           else path.with_name(f"{common.run_name(path)}_power_wand{crop}.png"))
    fig.savefig(out, dpi=150)
    print(f"{len(df)} samples over {df[COL_T].iloc[-1] - df[COL_T].iloc[0]:.2f} s, "
          f"peak {df[COL_P].max():.3f} mW. Saved {out}")
    if not args.no_show:
        plt.show()


if __name__ == "__main__":
    main()
