#!/usr/bin/env python3
"""
movetrawlerbuild.py
Finds the most recently modified build folder in C:\\MonarchDevBuild\\dist and
moves it whole to the correct Trawler destination:
  - contains "experimental" (case-insensitive) -> S:\\AOA Team\\Trawler\\Experimental Builds
  - otherwise                                  -> S:\\AOA Team\\Trawler
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

SRC_DIR = r"C:\MonarchDevBuild\dist"
DEST_NORMAL = r"S:\AOA Team\Trawler"
DEST_EXPERIMENTAL = r"S:\AOA Team\Trawler\Experimental Builds"


def apply_dpi_awareness() -> None:
    # pythonw.exe is manifested as DPI-aware; without this, launching via the
    # shortcut renders the window at raw pixel size (tiny on a scaled
    # display), while running under VS Code's debugger (not DPI-aware) gets
    # stretched to look normal. Forcing awareness here + scaling below makes
    # both launch paths consistent.
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def dpi_scale(root: tk.Tk) -> float:
    try:
        return root.winfo_fpixels("1i") / 96.0
    except Exception:
        return 1.0


def find_latest_folder(src: str) -> str | None:
    try:
        folders = [
            os.path.join(src, name)
            for name in os.listdir(src)
            if os.path.isdir(os.path.join(src, name))
        ]
    except Exception:
        return None
    if not folders:
        return None
    return max(folders, key=os.path.getmtime)


def destination_for(folder_path: str) -> str:
    name = os.path.basename(folder_path)
    if "experimental" in name.lower():
        return DEST_EXPERIMENTAL
    return DEST_NORMAL


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        self.latest_folder = find_latest_folder(SRC_DIR)

        scale = dpi_scale(root)
        root.tk.call("tk", "scaling", scale * (96 / 72))

        root.title("Move Trawler Build")
        root.geometry(f"{int(420 * scale)}x{int(220 * scale)}")
        root.resizable(False, False)

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="Latest build folder:").pack(anchor="w")
        self.folder_label = ttk.Label(
            main,
            text=os.path.basename(self.latest_folder) if self.latest_folder else "(none found)",
            font=("Segoe UI", 10, "bold"),
            wraplength=390,
        )
        self.folder_label.pack(anchor="w", pady=(0, 10))

        ttk.Label(main, text="Destination:").pack(anchor="w")
        dest = destination_for(self.latest_folder) if self.latest_folder else "-"
        self.dest_label = ttk.Label(main, text=dest, wraplength=390)
        self.dest_label.pack(anchor="w", pady=(0, 12))

        ttk.Label(
            main,
            text="Yes = move this folder to the destination above",
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x")

        self.yes_btn = ttk.Button(buttons, text="Yes", command=self.start_move)
        self.yes_btn.pack(side="left")
        ttk.Button(buttons, text="No", command=root.destroy).pack(side="left", padx=(8, 0))

        self.status = ttk.Label(main, text="", anchor="w")
        self.status.pack(fill="x", pady=(10, 0))

        if not self.latest_folder:
            self.set_status(f"No folders found in {SRC_DIR}", error=True)
            self.yes_btn.state(["disabled"])

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.config(text=text, foreground="#c0392b" if error else "#333")

    def start_move(self) -> None:
        if self.busy or not self.latest_folder:
            return
        self.busy = True
        self.yes_btn.state(["disabled"])
        self.set_status("Moving...")
        threading.Thread(target=self._move_worker, args=(self.latest_folder,), daemon=True).start()

    def _move_worker(self, folder: str) -> None:
        dest_dir = destination_for(folder)
        error = ""
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(folder, dest_dir)
        except Exception as exc:
            error = str(exc)
        self.root.after(0, self._done, folder, dest_dir, error)

    def _done(self, folder: str, dest_dir: str, error: str) -> None:
        self.busy = False
        self.yes_btn.state(["!disabled"])

        if error:
            messagebox.showerror("Move failed", error)
            self.set_status("Move failed.", error=True)
            return

        self.set_status(f"Moved {os.path.basename(folder)} to {dest_dir}")


def main() -> int:
    apply_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
