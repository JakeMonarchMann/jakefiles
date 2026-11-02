"""Plot divergence against distance from the goalposts for a "5010 - Collimate Lens with Hexapod" run.

Usage:
    python divergence_vs_goalposts.py [data.csv] [--out-dir DIR] [--title TEXT] [--nominal MRAD] [--no-show]

With no CSV argument, a file picker opens. The PNG is saved in a plots folder next to the CSV.
The title prompt and run date/time work as in the other plottin scripts (see plot_common.py).

Distance from the goalposts is the lens-to-wall gap (the -X wall is the goalpost; hexapod +X is
away from it), as the recipe computes it:
    gap = GapFromWallMm + (hex_X_mm - CadX)
GapFromWallMm (S5000XWallGapMm), CadX and DivSpecMaxMrad are read from the console log that sits next
to the CSV (the _console_log_ file with the same run stamp).

Only the focus walk is plotted: from "HEXAPOD MOVEABS X(XTarget)" up to (not incl.)
"HEXAPOD MOVEABS X(DivQMinHexX)". Only settled rows (hex_moving == 0) with a found beam are shown.
The dashed Max line is DivSpecMaxMrad; a Nominal line is drawn only when --nominal is given.
"""

import argparse
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

COL_CMD, COL_MOV, COL_X, COL_FOUND = "script_cmd", "hex_moving", "hex_X_mm", "cam_beam_found"
COL_DX, COL_DY = "cam_div_x_mrad", "cam_div_y_mrad"
USECOLS = [COL_CMD, COL_MOV, COL_X, COL_FOUND, COL_DX, COL_DY]
WALK_START_RE = r"HEXAPOD\s+MOVEABS\s+X\(XTarget\)"
WALK_END_RE = r"HEXAPOD\s+MOVEABS\s+X\(DivQMinHexX\)"
NUMBER = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
CONSOLE_VALUES = {  # name -> regex over the console log (first match wins)
    "gap_mm": rf"^GapFromWallMm\s*=\s*S5000XWallGapMm\s*=\s*{NUMBER}",
    "cad_x": rf"^CadX\s*=\s*{NUMBER}\s*$",
    "div_max": rf"^DivSpecMaxMrad\s*=\s*{NUMBER}\s*$",
}


def console_log_for(path: Path) -> Path | None:
    stem = re.sub(r"_data_", "_console_log_", path.stem, count=1)
    found = path.with_name(stem + ".txt")
    return found if found.is_file() else None


def read_console_values(log: Path) -> dict[str, float]:
    """GapFromWallMm, CadX and DivSpecMaxMrad as the run logged them."""
    text = log.read_text(encoding="utf-8", errors="replace")
    values = {}
    for key, pattern in CONSOLE_VALUES.items():
        m = re.search(pattern, text, flags=re.MULTILINE)
        if m:
            values[key] = float(m.group(1))
    missing = [k for k in ("gap_mm", "cad_x") if k not in values]
    if missing:
        raise ValueError(f"{log.name} has no value for {missing}; cannot work out the gap to the goalposts.")
    return values


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


def walk_rows(df: pd.DataFrame) -> pd.DataFrame:
    cmd = df[COL_CMD].fillna("")
    starts = np.flatnonzero(cmd.str.contains(WALK_START_RE, case=False, regex=True).to_numpy())
    if not starts.size:
        raise ValueError(f"No row matching /{WALK_START_RE}/ in script_cmd.")
    start = starts[0]
    ends = np.flatnonzero(cmd.iloc[start:].str.contains(WALK_END_RE, case=False, regex=True).to_numpy())
    stop = start + ends[0] if ends.size else len(df)  # unfinished run: plot what exists
    return df.iloc[start:stop]


def settled(rows: pd.DataFrame, gap_mm: float, cad_x: float) -> pd.DataFrame:
    keep = (rows[COL_MOV] == 0) & (rows[COL_FOUND] == 1)
    rows = rows[keep].dropna(subset=[COL_X, COL_DX, COL_DY]).copy()
    rows["gap_um"] = (gap_mm + rows[COL_X] - cad_x) * 1000
    return rows


def plot(ax, rows: pd.DataFrame, title: str, div_max: float | None, nominal: float | None) -> None:
    ax.set_title(title, fontsize=14)
    ax.set_xlabel("Distance from the goalposts (um)")
    ax.set_ylabel("Divergence (mRad FW 1/e2)")
    ax.grid(True, alpha=0.3)
    ax.scatter(rows["gap_um"], rows[COL_DX], s=18, color="#1f5f8b", label=COL_DX)
    ax.scatter(rows["gap_um"], rows[COL_DY], s=18, color="#ed7d31", label=COL_DY)
    for value, label in ((div_max, "Max"), (nominal, "Nominal")):
        if value is not None:
            ax.axhline(value, color="#1f5f8b", linestyle="--", linewidth=2, label=f"{label} ({value:g})")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper center", ncol=4, bbox_to_anchor=(0.5, -0.12), frameon=False)


def main(argv=None) -> int:
    common.setup_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--out-dir")
    ap.add_argument("--nominal", type=float, help="draw a Nominal divergence line at this value (mrad)")
    common.add_title_arg(ap)
    ap.add_argument("--no-show", action="store_true", help="save the PNG without opening a window")
    a = ap.parse_args(argv)

    path = common.input_path(a.csv)
    if path is None or not path.is_file():
        print(f"No CSV found: {path}")
        return 1
    log = console_log_for(path)
    if log is None:
        print(f"No console log next to {path.name}; it holds the gap to the wall the plot needs.")
        return 1
    out_dir = Path(a.out_dir) if a.out_dir else path.parent / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    header = common.title_header(common.get_title(a.title), common.run_time(path))

    print(f"Reading {path}")
    try:
        values = read_console_values(log)
        print(f"From {log.name}: gap at CadX {values['gap_mm']} mm, CadX {values['cad_x']} mm, "
              f"max divergence {values.get('div_max', 'n/a')} mrad")
        rows = settled(walk_rows(load_csv(path)), values["gap_mm"], values["cad_x"])
        if rows.empty:
            raise ValueError("No settled samples in the focus walk.")
    except (OSError, ValueError, pd.errors.ParserError) as e:
        print(f"Could not plot: {e}")
        return 1

    fig, ax = plt.subplots(figsize=(11, 7), constrained_layout=True)
    plot(ax, rows, f"{header}\n{common.run_name(path)}", values.get("div_max"), a.nominal)
    print(f"{len(rows)} settled samples, gap {rows['gap_um'].min():.1f} to {rows['gap_um'].max():.1f} um")

    out = out_dir / f"{path.stem}_divergence_vs_goalposts.png"
    fig.savefig(out, dpi=150)
    print(f"Saved {out}")
    if not a.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
