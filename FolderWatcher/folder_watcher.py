#!/usr/bin/env python3
"""
folder_watcher.py
System-tray app: pick a folder and get a Windows toast whenever a new file or
folder shows up directly inside it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tkinter as tk
from tkinter import filedialog

import pystray
import win32api
import win32event
import winerror
from PIL import Image

APP_NAME = "Folder Watcher"
POLL_SECONDS = 2.0
MAX_NAMES_IN_TOAST = 5

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "assets", "icon.png")
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", HERE), "FolderWatcher")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_folder() -> str | None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            folder = json.load(f).get("folder")
    except (OSError, ValueError):
        return None
    return folder if folder and os.path.isdir(folder) else None


def save_folder(folder: str) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"folder": folder}, f)


def scan(folder: str) -> dict[str, bool] | None:
    """Return {name: is_dir} for the folder's direct children, or None if unreadable."""
    try:
        with os.scandir(folder) as it:
            return {e.name: e.is_dir() for e in it}
    except OSError:
        return None


class FolderWatcher:
    def __init__(self) -> None:
        self.folder = load_folder()
        self.paused = False
        self.known: set[str] = set()
        self.lock = threading.Lock()
        self.dialog_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.icon: pystray.Icon | None = None
        if self.folder:
            self.known = set(scan(self.folder) or {})

    # --- watching -------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self.stop_event.wait(POLL_SECONDS):
            with self.lock:
                if self.paused or not self.folder:
                    continue
                current = scan(self.folder)
                if current is None:
                    continue
                added = sorted(n for n in current if n not in self.known)
                self.known = set(current)
                folder = self.folder
            if added:
                self._toast(folder, added, current)

    def _toast(self, folder: str, added: list[str], current: dict[str, bool]) -> None:
        shown = [n + ("\\" if current.get(n) else "") for n in added[:MAX_NAMES_IN_TOAST]]
        extra = len(added) - len(shown)
        if extra > 0:
            shown.append(f"...and {extra} more")
        title = "New item" if len(added) == 1 else f"{len(added)} new items"
        if self.icon:
            self.icon.notify("\n".join(shown), f"{title} in {os.path.basename(folder) or folder}")

    # --- menu actions ---------------------------------------------------

    def choose_folder(self, *_args) -> None:
        threading.Thread(target=self._choose_folder, daemon=True).start()

    def _choose_folder(self) -> None:
        if not self.dialog_lock.acquire(blocking=False):
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                path = filedialog.askdirectory(
                    parent=root, title="Choose a folder to watch", initialdir=self.folder or None
                )
            finally:
                root.destroy()
            if not path:
                return
            path = os.path.normpath(path)
            with self.lock:
                self.folder = path
                self.known = set(scan(path) or {})
            save_folder(path)
            if self.icon:
                self.icon.update_menu()
                self.icon.notify(path, "Now watching")
        finally:
            self.dialog_lock.release()

    def open_folder(self, *_args) -> None:
        if self.folder and os.path.isdir(self.folder):
            os.startfile(self.folder)

    def toggle_pause(self, *_args) -> None:
        with self.lock:
            self.paused = not self.paused
            if not self.paused and self.folder:
                # Don't toast for things that arrived while paused.
                self.known = set(scan(self.folder) or {})
        if self.icon:
            self.icon.update_menu()

    def quit(self, *_args) -> None:
        self.stop_event.set()
        if self.icon:
            self.icon.stop()

    # --- tray -----------------------------------------------------------

    def _status_text(self, _item) -> str:
        if not self.folder:
            return "No folder selected"
        return f"Watching: {self.folder}"

    def run(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open folder", self.open_folder, default=True, enabled=lambda _i: bool(self.folder)),
            pystray.MenuItem("Change folder...", self.choose_folder),
            pystray.MenuItem("Pause", self.toggle_pause, checked=lambda _i: self.paused),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon(APP_NAME, Image.open(ICON_PATH), APP_NAME, menu)
        threading.Thread(target=self._poll_loop, daemon=True).start()

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            if not self.folder:
                self.choose_folder()

        self.icon.run(setup)


def main() -> None:
    # Keep a reference so the mutex lives as long as the process.
    mutex = win32event.CreateMutex(None, False, "Local\\JakefilesFolderWatcher")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        sys.exit(0)
    FolderWatcher().run()
    del mutex


if __name__ == "__main__":
    main()
