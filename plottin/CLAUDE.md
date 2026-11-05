# plottin

Plot generators for Trawler run logs (`<run>_data_YYYYMMDD_HHMMSS.csv`, with the recipe `.txt` and
config `.json` alongside). Shared helpers live in `plot_common.py`. Run from the repository root with
`uv run python "plottin\<Folder>\<script>.py" ...`.

## Rules for new plotters

### Naming (new rule)

- **A plotter written for a specific Trawler program starts with that program's number**, then a
  CamelCase description: `6100AL1RasterHeatmap.py` for `6100 - Raster AL1`. A plotter that serves
  several programs lists their numbers joined by `-`: `6100-6200RasterHeatmap.py` (6100 AL1 + 6200 AL2).
- Each plotter gets its own folder named the same as the script without `.py`:
  `plottin\6100-6200RasterHeatmap\6100-6200RasterHeatmap.py`.
- Output files are prefixed with the input CSV stem, which already starts with the program number:
  `<csv stem>_al2_raster_heatmap.png`.
- Existing plotters predate this rule and keep their names; don't rename them unasked.
- A leading digit makes the file un-importable as a module, so these scripts are run directly. Shared
  code goes in `plot_common.py`, not in a numbered script.

### Structure

- Module docstring first: a one-line summary, a `Usage:` block, then what data is used and how it is
  filtered (rows kept/dropped, what a point/bin means, units, axis origin). It doubles as `--help`:
  `argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)`.
- Name the recipe version(s) the plotter was written against (e.g. "Written against 6200 - Raster AL2
  v1.2.0") when it depends on recipe lines, variable names or step order.
- Recipes change quickly: when a plotter depends on the recipe, look up `script_line` in the run's
  recipe `.txt` (saved in the run folder) rather than trusting `script_cmd` alone. Short moves can
  finish between CSV samples and never appear in `script_cmd`, and the logger can stall for 100s of ms.
  Check a new plotter against the values the recipe printed to its `_console_log_` file.
- Import `plot_common` with the standard block so the script works run directly or as a package:

  ```python
  if __package__:
      from .. import plot_common as common
  else:
      sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
      import plot_common as common
  ```

- CSV column names and tunables are UPPER_CASE module constants (`COL_*`, `USECOLS`, bin sizes, ...),
  plus `CSV_PATH = None` as an optional hard-coded default input.
- `main(argv=None) -> int` called via `sys.exit(main())`; call `common.setup_stdout()` first.
- Only use dependencies already in `pyproject.toml` (matplotlib, numpy, pandas). Comment only what
  needs clarifying.

### Command line

- Optional positional input: the data CSV or its run folder (which must contain exactly one
  `*_data_*.csv`). With no argument, `common.input_path(arg, CSV_PATH)` opens a file picker.
- `--title` via `common.add_title_arg(parser)` / `common.get_title(args.title)`; when omitted the user is
  prompted with `BG-CY0000` pre-filled.
- `--no-show` saves without opening a window; `--out-dir` (or `--out` for single-file outputs)
  overrides the output location.

### Loading data

- Read only the needed columns; fail with a clear message listing any missing required columns.
- Coerce numbers with `pd.to_numeric(..., errors="coerce")` and turn `inf` into NaN; read with
  `encoding_errors="replace"` and `on_bad_lines="warn"`.
- Logs may still be being written: ignore an unterminated final line and plot whatever was measured,
  saying so on the plot (e.g. "run incomplete").
- Match `script_cmd` case-insensitively and strip `//` comments first. Trawler replaces the commas in
  logged commands with spaces (`MoveLin(CenterX PtY PtZ ...)`).
- Prefer settled readings (dwells, `hex_moving == 0`) and measured readbacks over commanded values;
  document which is used.

### Figures and outputs

- Every figure title starts with `common.title_header(title, common.run_time(path))`, then
  `common.run_name(path)` and what the plot shows.
- Label axes with quantity, frame and units (`... offset from 6010 start (um)`, `Wand power (mW)`).
- Save PNGs at `dpi=150`, by default in a `plots` folder next to the CSV (`mkdir(exist_ok=True)`).
  A CSV of the plotted points/results alongside the PNG is encouraged.
- Print a short console summary (rows/points kept per category, key results) and `Saved <path>` for
  every file written.
- Expected failures (missing file, missing columns, no usable rows) print `Could not plot: <reason>`
  and return 1 instead of raising a traceback.

### Checking

- No test files in this repo. Validate by running the plotter on a real Trawler log with `--no-show`
  and `--title`, and look at the PNG. Exercise paths the log doesn't reach (e.g. later recipe steps)
  with a throwaway synthetic CSV outside the repo.
- If the plotter needs usage notes beyond its docstring, add a `README.md` in its folder.
