#!/usr/bin/env python3
"""
heic_to_png.py
Minimal helper: choose JPEG quality, then click Yes to convert every HEIC/HEIF
in Downloads to JPEG and delete the originals.

Requires: pip install pillow pillow-heif
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    _PIL_OK = True
    _PIL_ERR = ""
except Exception as exc:  # pragma: no cover - environment-specific
    _PIL_OK = False
    _PIL_ERR = str(exc)

HEIC_EXTS = (".heic", ".heif")


def downloads_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


def find_heic_files(folder: str) -> list[str]:
    paths: list[str] = []
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(HEIC_EXTS):
                paths.append(path)
    except Exception:
        return []
    return sorted(paths)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False

        root.title("HEIC -> JPEG")
        root.geometry("360x200")
        root.resizable(False, False)

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="JPEG quality").pack(anchor="w")

        self.quality = tk.IntVar(value=70)
        self.q_text = ttk.Label(main, text="70%")
        self.q_text.pack(anchor="e")

        self.slider = ttk.Scale(
            main,
            from_=1,
            to=100,
            orient="horizontal",
            variable=self.quality,
            command=self._on_quality_change,
        )
        self.slider.pack(fill="x", pady=(2, 10))

        ttk.Label(
            main,
            text=f"Yes = convert all HEIC files in {downloads_dir()} and replace originals",
            wraplength=330,
            justify="left",
        ).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(main)
        buttons.pack(fill="x")

        self.yes_btn = ttk.Button(buttons, text="Yes", command=self.start_convert)
        self.yes_btn.pack(side="left")
        ttk.Button(buttons, text="No", command=root.destroy).pack(side="left", padx=(8, 0))

        self.status = ttk.Label(main, text="", anchor="w")
        self.status.pack(fill="x", pady=(10, 0))

        if not _PIL_OK:
            self.set_status("Missing deps: pip install pillow pillow-heif", error=True)
            self.yes_btn.state(["disabled"])

    def _on_quality_change(self, _value: str) -> None:
        self.q_text.config(text=f"{self.quality.get()}%")

    def set_status(self, text: str, error: bool = False) -> None:
        self.status.config(text=text, foreground="#c0392b" if error else "#333")

    def start_convert(self) -> None:
        if self.busy or not _PIL_OK:
            return
        self.busy = True
        self.yes_btn.state(["disabled"])
        self.set_status("Converting in Downloads...")
        threading.Thread(
            target=self._convert_worker,
            args=(self.quality.get(),),
            daemon=True,
        ).start()

    def _convert_worker(self, quality: int) -> None:
        folder = downloads_dir()
        files = find_heic_files(folder)
        converted = 0
        removed = 0
        errors: list[str] = []

        for src in files:
            dst = os.path.splitext(src)[0] + ".jpg"
            try:
                with Image.open(src) as img:
                    if img.mode != "RGB":
                        img = img.convert("RGB")
                    img.save(dst, "JPEG", quality=quality, optimize=True)
                converted += 1
                os.remove(src)
                removed += 1
            except Exception as exc:
                errors.append(f"{os.path.basename(src)}: {exc}")

        self.root.after(0, self._done, len(files), converted, removed, errors)

    def _done(
        self,
        total: int,
        converted: int,
        removed: int,
        errors: list[str],
    ) -> None:
        self.busy = False
        self.yes_btn.state(["!disabled"])

        if total == 0:
            self.set_status("No HEIC files found in Downloads.")
            return

        if errors:
            messagebox.showwarning(
                "Some files failed",
                "Could not process:\n\n" + "\n".join(errors),
            )

        self.set_status(
            f"Done. {converted}/{total} converted, {removed} HEIC removed."
        )


def main() -> int:
    if not _PIL_OK:
        print("HEIC support unavailable:", _PIL_ERR, file=sys.stderr)
        print("Install with: pip install pillow pillow-heif", file=sys.stderr)

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
