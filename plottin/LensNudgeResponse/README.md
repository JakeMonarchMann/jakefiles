# Lens nudge -> far-field response

From the repository root:

```powershell
uv run python "plottin\LensNudgeResponse\lens_nudge_response.py" "C:\Users\jake.mann\Downloads\Center divmin and aim CL_20261017_185456" --no-show
```

Accepts a run folder containing exactly one Trawler data CSV, or the CSV itself.
The matching console log must be alongside it. Omit `--no-show` to open the plots;
use `--out-dir` to override the default `plots` folder next to the input.

Outputs (prefixed with the input CSV stem):

- `nudge_response.png`: separate Y/Z response panels for every completed FF AIM
  training, distinguishing the coarse HEXSTAGE from the lens-holding HEXAPOD.
  Both camera axes are shown, including cross-coupling.
- `beam_history.png`: recorded Y/Z motion, FF centroid, X motion, and divergence.
- `calibration_samples.csv`: the paired commanded offsets and camera readings.
- `response_fits.csv`: signed sensitivity in camera um per stage um, intercept,
  R-squared, and residual RMS for each camera axis.

The calibration fit is `centroid = intercept + a*Y + b*Z`, using all logged AIM
pairs (including repeated returns to zero). Panels show points with the other
stage axis at zero and subtract the fitted intercept. These are **commanded
relative offsets**, not measured encoder displacement. A positive slope means
the camera coordinate increases for a positive stage command. No sensor-axis
flip or optical-angle conversion is assumed.

The time history uses `shrc_hex_*` for HEXSTAGE and `hex_*` for HEXAPOD. It shows
only detected beams, with repeated `ff_frame_seq` values removed. Stage traces
retain all polls. FF readings are frame-averaged and may lag motion: the example
run reports 10-frame averaging initially and changes to 25 samples before the
focus walk. Therefore the time traces, including divergence during motion, are
not used as settled sensitivity fits or evidence of a focus optimum. The
exported TIFF is a final snapshot, not a per-nudge image sequence.

Validation:

```powershell
uv run python -m unittest plottin.LensNudgeResponse.test_lens_nudge_response
```
