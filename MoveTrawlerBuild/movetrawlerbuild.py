#!/usr/bin/env python3
"""
movetrawlerbuild.py
Runs the Trawler build script, then finds the resulting build folder in
C:\\MonarchDevBuild\\dist and copies it whole to the correct destination:
  - contains "experimental" (case-insensitive) -> S:\\AOA Team\\Trawler\\Experimental Builds
  - otherwise                                  -> S:\\AOA Team\\Trawler
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

BUILD_BAT = r"C:\Users\jake.mann\Documents\Trawler\builder\builds\build.bat"
REPO_DIR = r"C:\Users\jake.mann\Documents\Trawler"
SRC_DIR = r"C:\MonarchDevBuild\dist"
DEST_NORMAL = r"S:\AOA Team\Trawler"
DEST_EXPERIMENTAL = r"S:\AOA Team\Trawler\Experimental Builds"


def current_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        branch = result.stdout.strip()
        return branch if result.returncode == 0 and branch else "(unknown)"
    except Exception:
        return "(unknown)"


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


def run_build() -> str:
    """Runs build.bat to completion. Returns an error message, or "" on success."""
    if not os.path.isfile(BUILD_BAT):
        return f"Build script not found: {BUILD_BAT}"
    try:
        result = subprocess.run(
            ["cmd", "/c", BUILD_BAT],
            cwd=os.path.dirname(BUILD_BAT),
            input="\n",  # answers the bat's trailing `pause` so it can't hang
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return str(exc)
    if result.returncode != 0:
        tail = ((result.stdout or "") + (result.stderr or ""))[-1000:]
        return f"Build failed (exit {result.returncode}):\n{tail}"
    return ""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False

        scale = dpi_scale(root)
        root.tk.call("tk", "scaling", scale * (96 / 72))

        root.title("Move Trawler Build")
        root.geometry(f"{int(420 * scale)}x{int(200 * scale)}")
        root.resizable(False, False)

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        self.branch_label = ttk.Label(main, text=f"Branch: {current_branch()}", font=("Segoe UI", 9, "bold"))
        self.branch_label.pack(anchor="w", pady=(0, 8))

        ttk.Label(
            main,
            text="Builds the latest Trawler EXE, then copies the new build folder to the Trawler share.",
            wraplength=390,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 12))

        self.go_btn = ttk.Button(main, text="Build & Copy", command=self.start)
        self.go_btn.pack(anchor="w")

        self.status = ttk.Label(main, text="", anchor="w", wraplength=390)
        self.status.pack(fill="x", pady=(10, 0))

        if not os.path.isfile(BUILD_BAT):
            self.set_status(f"Build script not found: {BUILD_BAT}", error=True)
            self.go_btn.state(["disabled"])

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.config(text=text, foreground="#c0392b" if error else "#333")

    def start(self) -> None:
        if self.busy:
            return

        branch = current_branch()
        self.branch_label.config(text=f"Branch: {branch}")

        if branch.lower() == "main":
            proceed = messagebox.askyesno(
                "Confirm official build",
                "You're on main - this will send a build to the official Trawler folder. Continue?",
            )
            if not proceed:
                return

        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress.start(12)
        self.set_status("Building...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        build_error = run_build()
        if build_error:
            self.root.after(0, self._done, None, None, build_error)
            return

        self.root.after(0, self.set_status, "Build finished, copying...")

        folder = find_latest_folder(SRC_DIR)
        if not folder:
            self.root.after(0, self._done, None, None, f"No folders found in {SRC_DIR} after build.")
            return

        dest_dir = destination_for(folder)
        dest_path = os.path.join(dest_dir, os.path.basename(folder))
        error = ""
        exe_names: list[str] = []
        try:
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
            shutil.copytree(folder, dest_path)

            # The exe is the whole point of the copy - antivirus/network
            # hiccups can silently drop it, so confirm one landed. The exe
            # name drops any "-branch-EXPERIMENTAL" suffix from the folder
            # name, so just look for any .exe directly inside instead of an
            # exact name match.
            exe_names = [name for name in os.listdir(dest_path) if name.lower().endswith(".exe")]
            if not exe_names:
                error = (
                    "Copy finished but no .exe was found in the destination "
                    "folder (antivirus may have quarantined it)."
                )
        except Exception as exc:
            error = str(exc)

        exe_name = exe_names[0] if not error and exe_names else None
        self.root.after(0, self._done, dest_dir, exe_name, error)

    def _done(self, dest_dir: str | None, exe_name: str | None, error: str) -> None:
        self.busy = False
        self.go_btn.state(["!disabled"])
        self.progress.stop()

        if error:
            messagebox.showerror("Failed", error)
            self.set_status("Failed.", error=True)
            return

        self.set_status(f"Done: {exe_name} -> {dest_dir}")


def main() -> int:
    apply_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
