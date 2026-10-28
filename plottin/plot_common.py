"""Helpers shared by every plot generator in plottin.

Conventions:
    * The data file is an optional positional argument; without it a file picker opens.
    * A hand-written title (the unit under test) is requested by --title or, when omitted, by a box
      pre-filled with TITLE_PREFIX and the cursor after it. Cancelling, or --title "", means none.
    * Every figure title starts with title_header(): the hand-written title and the run date/time.
      The run time comes from the YYYYMMDD_HHMMSS stamp in the file name (Trawler logs), else the
      file's modified time.

Scripts import this module both when run directly and when imported as part of the plottin package:
see the import block at the top of each script.
"""
import re
import sys
from datetime import datetime
from pathlib import Path

TITLE_PREFIX = "BG-CY0000"  # pre-filled in the title box; type the rest of the unit name after it
RUN_STAMP = re.compile(r"(?<!\d)(\d{8})_(\d{6})(?!\d)")


def setup_stdout() -> None:
    """Never crash printing non-ASCII script_cmd text to a narrow console."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")


def pick_file(prompt: str = "Select Trawler data CSV", filetypes=(("CSV", "*.csv"),)) -> Path | None:
    """File picker; None if cancelled or no display is available."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        try:
            selected = filedialog.askopenfilename(title=prompt, filetypes=list(filetypes))
        finally:
            root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None


def input_path(arg: str | None, default: str | None = None,
               prompt: str = "Select Trawler data CSV") -> Path | None:
    """The CLI path, else the script's default path if it exists, else a file picker."""
    if arg:
        return Path(arg)
    if default and Path(default).exists():
        return Path(default)
    return pick_file(prompt)


def add_title_arg(parser) -> None:
    parser.add_argument("--title", help="hand-written plot title (the unit under test); "
                        f"omit to be prompted with {TITLE_PREFIX} pre-filled")


def ask_title(default: str = TITLE_PREFIX) -> str | None:
    """Prompt for the plot title, pre-filled with default and the cursor after it (not selected).

    Returns the entered text, or None if the box was cancelled or no display is available.
    """
    try:
        import tkinter

        root = tkinter.Tk()
    except Exception:
        return None
    answer = []
    root.title("Plot title")
    root.attributes("-topmost", True)
    tkinter.Label(root, text="Title for the plots (unit under test):").pack(
        padx=12, pady=(12, 4), anchor="w")
    var = tkinter.StringVar(value=default)
    entry = tkinter.Entry(root, textvariable=var, width=60)
    entry.pack(padx=12, fill="x")

    def accept(_event=None):
        answer.append(var.get().strip())
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    buttons = tkinter.Frame(root)
    buttons.pack(padx=12, pady=12, anchor="e")
    tkinter.Button(buttons, text="OK", width=8, command=accept).pack(side="left", padx=4)
    tkinter.Button(buttons, text="Cancel", width=8, command=cancel).pack(side="left")
    root.bind("<Return>", accept)
    root.bind("<Escape>", cancel)
    root.protocol("WM_DELETE_WINDOW", cancel)
    entry.focus_force()
    entry.icursor("end")
    entry.selection_clear()
    root.mainloop()
    return answer[0] if answer else None


def get_title(arg: str | None) -> str:
    """The --title value, else the prompted title; "" when there is none."""
    title = ask_title() if arg is None else arg.strip()
    if title is None:
        print("No title entered; plots will have no hand-written title.")
        return ""
    return title


def run_time(path: Path) -> datetime:
    """Run date/time from the last YYYYMMDD_HHMMSS stamp in the file name, else its modified time."""
    for date, time in reversed(RUN_STAMP.findall(path.name)):
        try:
            return datetime.strptime(date + time, "%Y%m%d%H%M%S")
        except ValueError:
            continue
    return datetime.fromtimestamp(path.stat().st_mtime)


def title_header(user_title: str, when: datetime) -> str:
    """First line of every figure title: 'TITLE  |  Run YYYY-MM-DD HH:MM:SS' (or just the run time)."""
    stamp = f"Run {when:%Y-%m-%d %H:%M:%S}"
    return f"{user_title}  |  {stamp}" if user_title else stamp


def run_name(path: Path) -> str:
    """Trawler CSV stem without the trailing _data_YYYYMMDD_HHMMSS."""
    return re.sub(r"_data_\d+(?:_\d+)?$", "", path.stem)
