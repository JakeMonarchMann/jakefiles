"""Plot Trawler AIM calibration response and the corresponding FF time history.

Usage:
    python lens_nudge_response.py [run folder or data CSV] [--out-dir DIR] [--title TEXT] [--no-show]

With no input a file picker opens. Unless --title is given, a box prompts for the unit under test;
every figure title starts with it and the run date/time (see plot_common.py).
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

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
TRAIN = re.compile(
    r"AIM TRAIN .+?learning how the FF beam moves with "
    r"(HEXSTAGE|HEXAPOD)_Y,\s*\1_Z"
)
SAMPLE = re.compile(
    rf"^\s+[^:]+:\s+(HEXSTAGE|HEXAPOD)_Y({NUMBER})\s+"
    rf"\1_Z({NUMBER})\s*-> centroid \(({NUMBER}),\s*({NUMBER})\) mm\s*$"
)
STAGES = {"HEXSTAGE": "shrc_hex", "HEXAPOD": "hex"}
BEAM_COLS = [
    "ff_centroid_x_mm", "ff_centroid_y_mm", "ff_div_x_mrad", "ff_div_y_mrad",
]
USECOLS = ["time_elapsed_s", "script_cmd", "ff_frame_seq", "ff_beam_found"] + BEAM_COLS + [
    f"{prefix}_{axis}_mm" for prefix in STAGES.values() for axis in "XYZ"
]
COLORS = ["#2878b5", "#e07825"]


def calibration_samples(text: str) -> pd.DataFrame:
    """Use the camera/stage pairs captured by AIM, not asynchronous CSV polls."""
    records = []
    active_stage = None
    training = 0
    pending = []
    for line in text.splitlines():
        start = TRAIN.search(line)
        if start:
            if active_stage is not None:
                raise ValueError("AIM TRAIN started before the previous training completed.")
            active_stage = start[1]
            training += 1
            pending = []
        elif active_stage is not None:
            sample = SAMPLE.match(line)
            if sample:
                if sample[1] != active_stage:
                    raise ValueError("Calibration sample stage does not match AIM TRAIN.")
                y, z, cx, cy = map(float, sample.groups()[1:])
                pending.append({
                    "training": training, "stage": active_stage,
                    "sample": len(pending), "y_um": y * 1000, "z_um": z * 1000,
                    "cx_um": cx * 1000, "cy_um": cy * 1000,
                })
            elif "AIM TRAIN done" in line:
                count = re.search(r"(\d+) samples", line)
                if count is None or len(pending) != int(count[1]):
                    raise ValueError("AIM sample count does not match the completed training.")
                records.extend(pending)
                active_stage = None
    if active_stage is not None:
        raise ValueError("Incomplete AIM TRAIN at the end of the console log.")
    if not records:
        raise ValueError("No completed FF Y/Z AIM calibration found in the console log.")
    return pd.DataFrame(records)


def fit_response(samples: pd.DataFrame) -> pd.DataFrame:
    results = []
    for training, group in samples.groupby("training", sort=False):
        design = np.column_stack([np.ones(len(group)), group[["y_um", "z_um"]]])
        if np.linalg.matrix_rank(design) < 3:
            raise ValueError(f"Training {training} does not independently excite both Y and Z.")
        observed = group[["cx_um", "cy_um"]].to_numpy()
        coefficients = np.linalg.lstsq(design, observed, rcond=None)[0]
        residual = observed - design @ coefficients
        for j, camera_axis in enumerate("XY"):
            total = np.sum((observed[:, j] - observed[:, j].mean()) ** 2)
            results.append({
                "training": training, "stage": group["stage"].iloc[0],
                "camera_axis": camera_axis, "samples": len(group),
                "intercept_um": coefficients[0, j],
                "d_centroid_d_Y_um_per_um": coefficients[1, j],
                "d_centroid_d_Z_um_per_um": coefficients[2, j],
                "r_squared": 1 - np.sum(residual[:, j] ** 2) / total if total > 0 else np.nan,
                "residual_rms_um": np.sqrt(np.mean(residual[:, j] ** 2)),
            })
    return pd.DataFrame(results)


def load_history(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, usecols=lambda c: c in USECOLS)
    missing = sorted(set(USECOLS) - set(frame.columns))
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    for col in USECOLS:
        if col != "script_cmd":
            frame[col] = pd.to_numeric(frame[col], errors="raise")
    numeric = [col for col in USECOLS if col != "script_cmd"]
    if not np.isfinite(frame[numeric].to_numpy()).all():
        raise ValueError("CSV contains missing or non-finite required measurements.")
    if frame.empty or not frame["time_elapsed_s"].is_monotonic_increasing:
        raise ValueError("CSV must contain time-ordered measurements.")
    if not frame["ff_beam_found"].eq(1).any():
        raise ValueError("No FF beam detections in the CSV.")
    return frame


def plot_response(samples: pd.DataFrame, fits: pd.DataFrame, title: str):
    groups = list(samples.groupby("training", sort=False))
    fig, axes = plt.subplots(len(groups), 2, figsize=(12, 4 * len(groups)),
                             squeeze=False, constrained_layout=True)
    fig.suptitle(f"{title}\nLens nudge -> FF beam centroid", fontweight="bold")
    for row, (training, group) in enumerate(groups):
        fitted = fits[fits["training"] == training]
        for col, axis in enumerate("YZ"):
            ax = axes[row, col]
            other = "z_um" if axis == "Y" else "y_um"
            isolated = group[np.isclose(group[other], 0)]
            x = isolated[f"{axis.lower()}_um"]
            for j, cam in enumerate("XY"):
                fit = fitted[fitted["camera_axis"] == cam].iloc[0]
                slope = fit[f"d_centroid_d_{axis}_um_per_um"]
                delta = isolated[["cx_um", "cy_um"][j]] - fit["intercept_um"]
                ax.scatter(x, delta, color=COLORS[j], s=35)
                limits = np.array([x.min(), x.max()])
                ax.plot(limits, slope * limits, color=COLORS[j],
                        label=f"FF {cam}: {slope:+.2f} um/um")
            ax.axhline(0, color="gray", lw=0.7)
            ax.axvline(0, color="gray", lw=0.7)
            ax.set(title=f"{group['stage'].iloc[0]} {axis} nudge (training {training})",
                   xlabel=f"Commanded {axis} offset from training start (um)",
                   ylabel="FF centroid change from fitted zero (um)")
            ax.grid(alpha=0.25)
            ax.legend()
    fig.supxlabel("Logged AIM pairs; points include repeated returns to zero. "
                  "Fit: centroid = intercept + a*Y + b*Z.", fontsize=9)
    return fig


def plot_history(history: pd.DataFrame, title: str):
    fig, axes = plt.subplots(4, 1, figsize=(13, 12), sharex=True, constrained_layout=True)
    fig.suptitle(f"{title}\nRecorded motion and FF beam history", fontweight="bold")
    time = history["time_elapsed_s"]
    for stage, prefix in STAGES.items():
        for axis, color in zip("YZ", COLORS):
            values = history[f"{prefix}_{axis}_mm"]
            axes[0].plot(time, (values - values.iloc[0]) * 1000, color=color,
                         ls="-" if stage == "HEXSTAGE" else "--",
                         label=f"{stage} {axis}")
        values = history[f"{prefix}_X_mm"]
        axes[2].plot(time, (values - values.iloc[0]) * 1000, label=f"{stage} X")
    beam = history[history["ff_beam_found"] == 1].drop_duplicates("ff_frame_seq")
    for col, color in zip(BEAM_COLS[:2], COLORS):
        axes[1].plot(beam["time_elapsed_s"], beam[col], color=color,
                     label=f"FF {col.split('_')[2].upper()}")
    for col, color in zip(BEAM_COLS[2:], COLORS):
        axes[3].plot(beam["time_elapsed_s"], beam[col], color=color,
                     label=f"FF div {col.split('_')[2].upper()}")
    labels = ["Y/Z offset from run start (um)", "FF centroid (mm)",
              "X offset from run start (um)", "FF divergence (mrad)"]
    for ax, label in zip(axes, labels):
        ax.set_ylabel(label)
        ax.grid(alpha=0.25)
        ax.legend(loc="best", ncol=2, fontsize=9)
    commands = history["script_cmd"].fillna("")
    starts = history[commands.eq("AIM TRAIN") & commands.ne(commands.shift())]
    for i, (_, start) in enumerate(starts.iterrows(), 1):
        t = start["time_elapsed_s"]
        for ax in axes:
            ax.axvline(t, color="gray", ls=":", alpha=0.6)
        axes[0].text(t, 1, f" Train {i}", transform=axes[0].get_xaxis_transform(),
                     va="top", fontsize=9)
    axes[-1].set_xlabel("Elapsed time (s)")
    fig.supxlabel("One point per FF frame; camera values are frame-averaged and can lag motion. "
                  "Time traces are not settled-response fits.", fontsize=9)
    return fig


def resolve_inputs(path: Path) -> tuple[Path, Path]:
    if path.is_dir():
        candidates = sorted(path.glob("*_data_*.csv"))
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one *_data_*.csv in {path}; found {len(candidates)}.")
        path = candidates[0]
    if "_data_" not in path.name or path.suffix.lower() != ".csv":
        raise ValueError("Choose a Trawler *_data_*.csv or its run directory.")
    console = path.with_name(path.name.replace("_data_", "_console_log_", 1)).with_suffix(".txt")
    return path, console


def main(argv: list[str] | None = None) -> int:
    common.setup_stdout()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run", nargs="?", help="Trawler run folder or data CSV; omit for a file picker")
    parser.add_argument("--out-dir", type=Path)
    common.add_title_arg(parser)
    parser.add_argument("--no-show", action="store_true", help="save the PNGs without opening windows")
    args = parser.parse_args(argv)
    run = common.input_path(args.run)
    if run is None or not run.exists():
        print(f"No run folder or CSV found: {run}", file=sys.stderr)
        return 1
    try:
        csv_path, console_path = resolve_inputs(run)
        samples = calibration_samples(console_path.read_text(encoding="utf-8-sig"))
        fits = fit_response(samples)
        history = load_history(csv_path)
        out = args.out_dir or csv_path.parent / "plots"
        out.mkdir(parents=True, exist_ok=True)
        header = common.title_header(common.get_title(args.title), common.run_time(csv_path))
        title = f"{header}\n{common.run_name(csv_path)}"
        figures = [
            ("nudge_response", plot_response(samples, fits, title)),
            ("beam_history", plot_history(history, title)),
        ]
        for suffix, fig in figures:
            destination = out / f"{csv_path.stem}_{suffix}.png"
            fig.savefig(destination, dpi=160)
            print(f"Saved {destination}")
        samples.to_csv(out / f"{csv_path.stem}_calibration_samples.csv", index=False)
        fits.to_csv(out / f"{csv_path.stem}_response_fits.csv", index=False)
        print(fits.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
        if not args.no_show:
            plt.show()
        for _, fig in figures:
            plt.close(fig)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"Could not plot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
