#!/usr/bin/env python3
"""
video_chunk_speeder.py
Load an MP4 or MOV video, scrub through it to mark split points, then set a
speed multiplier for each resulting chunk and export the combined result as
an MP4 (MOV inputs are converted automatically since export is always MP4).

  1. Select Video...        -> choose the source video (.mp4 or .mov).
     Optionally also select a second video to play side by side (e.g. a
     second camera angle of the same event), then dial in a sync offset
     since the two recordings didn't start at the same time. Click
     "Loop 5s Preview" to watch a live looping clip of both and fine-tune
     the offset until they line up.
  2. Drag the slider / click Preview Frame to scrub around.
  3. Add Split at Current Time -> marks a cut point (repeat as needed).
  4. Set a speed (e.g. 2.0 = 2x) for each chunk that appears below.
  5. Export Sped-Up Video    -> renders everything into a new MP4.

Requires: pip install moviepy pillow
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    from moviepy import ColorClip, CompositeVideoClip, VideoFileClip, concatenate_videoclips, vfx
    _DEPS_OK = True
    _DEPS_ERR = ""
except Exception as exc:  # pragma: no cover - environment-specific
    _DEPS_OK = False
    _DEPS_ERR = str(exc)

SPLIT_EPS = 0.05  # seconds; how close a split can be to another point
VIDEO_FILETYPES = [
    ("Video files", "*.mp4 *.mov"),
    ("MP4 video", "*.mp4"),
    ("MOV video", "*.mov"),
    ("All files", "*.*"),
]


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{int(h)}:{int(m):02d}:{s:05.2f}"
    return f"{int(m)}:{s:05.2f}"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Video Chunk Speeder")
        root.geometry("700x760")
        root.minsize(600, 620)

        self.clip: VideoFileClip | None = None
        self.clip2: VideoFileClip | None = None
        self.video_path = ""
        self.video2_path = ""
        self.duration = 0.0
        self.trim_start = 0.0
        self.trim_end = 0.0
        self.splits: list[float] = []
        self.speed_vars: list[tk.StringVar] = []
        self.busy = False
        self._preview_img = None  # keep a reference so it isn't garbage collected
        self._loop_playing = False
        self._loop_after_id = None
        self._loop_photo_frames: list = []
        self._loop_start = 0.0
        self._offset_rebuild_after_id = None

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Button(top, text="Select Video...", command=self.select_video).pack(side="left")
        self.file_label = ttk.Label(top, text="No file selected")
        self.file_label.pack(side="left", padx=8)

        second_row = ttk.Frame(main)
        second_row.pack(fill="x", pady=(4, 0))
        ttk.Button(second_row, text="Add Second Video...", command=self.select_second_video).pack(side="left")
        self.file2_label = ttk.Label(second_row, text="No second video")
        self.file2_label.pack(side="left", padx=8)
        self.remove2_btn = ttk.Button(second_row, text="Remove", command=self.remove_second_video, state="disabled")
        self.remove2_btn.pack(side="left")

        offset_row = ttk.Frame(main)
        offset_row.pack(fill="x", pady=(4, 0))
        ttk.Label(offset_row, text="Sync offset (s), video 2 time = video 1 time + offset:").pack(side="left")
        self.offset_var = tk.StringVar(value="0.0")
        self.offset_var.trace_add("write", self._on_offset_changed)
        ttk.Entry(offset_row, textvariable=self.offset_var, width=8).pack(side="left", padx=4)
        for delta in (-1.0, -0.1, 0.1, 1.0):
            sign = "+" if delta > 0 else ""
            ttk.Button(offset_row, text=f"{sign}{delta:g}", width=5,
                       command=lambda d=delta: self._nudge_offset(d)).pack(side="left", padx=2)

        self.preview_label = ttk.Label(
            main, background="#222", foreground="white",
            text="Load a video to preview frames", anchor="center",
        )
        self.preview_label.pack(fill="x", pady=8)

        scrub = ttk.Frame(main)
        scrub.pack(fill="x")
        self.time_var = tk.DoubleVar(value=0.0)
        self.scale = ttk.Scale(scrub, from_=0, to=1, variable=self.time_var, command=self._on_scale_move)
        self.scale.pack(side="left", fill="x", expand=True)
        self.scale.bind("<Button-1>", lambda e: self._stop_loop_preview())
        self.scale.bind("<ButtonRelease-1>", lambda e: self._on_raw_scale_release())
        self.time_label = ttk.Label(scrub, text="0:00.00 / 0:00.00", width=18)
        self.time_label.pack(side="left", padx=6)

        final_scrub = ttk.Frame(main)
        final_scrub.pack(fill="x", pady=(4, 0))
        ttk.Label(final_scrub, text="Final video:").pack(side="left")
        self.final_time_var = tk.DoubleVar(value=0.0)
        self.final_scale = ttk.Scale(
            final_scrub, from_=0, to=1, variable=self.final_time_var, command=self._on_final_scale_move
        )
        self.final_scale.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.final_scale.bind("<Button-1>", lambda e: self._stop_loop_preview())
        self.final_scale.bind("<ButtonRelease-1>", lambda e: self._on_final_scale_release())
        self.final_time_label = ttk.Label(final_scrub, text="0:00.00 / 0:00.00", width=18)
        self.final_time_label.pack(side="left", padx=6)

        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Preview Frame", command=lambda: self._show_preview(self.time_var.get())).pack(side="left")
        ttk.Button(btns, text="Add Split at Current Time", command=self.add_split).pack(side="left", padx=6)
        self.loop_btn = ttk.Button(btns, text="\u25b6 Loop 5s Preview", command=self._toggle_loop_preview, state="disabled")
        self.loop_btn.pack(side="left")

        trim_row = ttk.Frame(main)
        trim_row.pack(fill="x", pady=(0, 6))
        ttk.Button(trim_row, text="Set Trim Start Here", command=self.set_trim_start).pack(side="left")
        ttk.Button(trim_row, text="Set Trim End Here", command=self.set_trim_end).pack(side="left", padx=6)
        ttk.Button(trim_row, text="Reset Trim", command=self.reset_trim).pack(side="left")
        self.trim_label = ttk.Label(trim_row, text="")
        self.trim_label.pack(side="left", padx=10)

        ttk.Label(main, text="Splits (select one, then Remove Selected):").pack(anchor="w", pady=(8, 0))
        split_row = ttk.Frame(main)
        split_row.pack(fill="x")
        self.split_list = tk.Listbox(split_row, height=4, exportselection=False)
        self.split_list.pack(side="left", fill="x", expand=True)
        ttk.Button(split_row, text="Remove Selected", command=self.remove_split).pack(side="left", padx=6)

        # Packed with side="bottom" (and before the expanding chunk list below) so it
        # always keeps its own slice of the window instead of getting squeezed out.
        bottom = ttk.Frame(main)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.export_btn = ttk.Button(bottom, text="Export Sped-Up Video", command=self.export, state="disabled")
        self.export_btn.pack(side="left")
        self.status_label = ttk.Label(bottom, text="")
        self.status_label.pack(side="left", padx=10)

        ttk.Label(main, text="Chunks (speed multiplier, e.g. 2.0 = 2x faster):").pack(anchor="w", pady=(10, 0))
        chunk_container = ttk.Frame(main)
        chunk_container.pack(fill="both", expand=True)
        canvas = tk.Canvas(chunk_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(chunk_container, orient="vertical", command=canvas.yview)
        self.chunk_frame = ttk.Frame(canvas)
        self.chunk_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.chunk_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        if not _DEPS_OK:
            messagebox.showerror("Missing dependencies", f"pip install moviepy pillow\n\n{_DEPS_ERR}")

    # ── video loading / preview ──────────────────────────────────────────

    def select_video(self):
        if not _DEPS_OK:
            messagebox.showerror("Missing dependencies", _DEPS_ERR)
            return
        path = filedialog.askopenfilename(title="Select Video", filetypes=VIDEO_FILETYPES)
        if not path:
            return
        try:
            clip = VideoFileClip(path)
        except Exception as exc:
            messagebox.showerror("Could not open video", str(exc))
            return
        if self.clip is not None:
            self.clip.close()

        self.clip = clip
        self.video_path = path
        self.duration = clip.duration
        self.trim_start = 0.0
        self.trim_end = self.duration
        self.splits = []
        self.file_label.configure(text=os.path.basename(path))
        self.scale.configure(from_=0, to=max(0.01, self.duration))
        self.time_var.set(0.0)
        self._update_time_label(0.0)
        self._show_preview(0.0)
        self._update_trim_label()
        self._rebuild_splits_list()
        self._rebuild_chunks()
        self.export_btn.configure(state="normal")
        self.loop_btn.configure(state="normal")
        self.status_label.configure(text="")

    def select_second_video(self):
        if not _DEPS_OK:
            messagebox.showerror("Missing dependencies", _DEPS_ERR)
            return
        path = filedialog.askopenfilename(title="Select Second Video", filetypes=VIDEO_FILETYPES)
        if not path:
            return
        try:
            clip2 = VideoFileClip(path)
        except Exception as exc:
            messagebox.showerror("Could not open video", str(exc))
            return
        if self.clip2 is not None:
            self.clip2.close()
        self.clip2 = clip2
        self.video2_path = path
        self.file2_label.configure(text=os.path.basename(path))
        self.remove2_btn.configure(state="normal")
        self._show_preview(self.time_var.get())

    def remove_second_video(self):
        if self.clip2 is not None:
            self.clip2.close()
        self.clip2 = None
        self.video2_path = ""
        self.file2_label.configure(text="No second video")
        self.remove2_btn.configure(state="disabled")
        self._show_preview(self.time_var.get())

    def _get_offset(self) -> float:
        try:
            return float(self.offset_var.get())
        except ValueError:
            return 0.0

    def _nudge_offset(self, delta: float):
        self.offset_var.set(f"{self._get_offset() + delta:g}")

    def _on_offset_changed(self, *_):
        """Live preview reacts to offset edits; a running loop rebuilds instead of stopping."""
        if not self._loop_playing:
            self._show_preview(self.time_var.get())
            return
        if self._offset_rebuild_after_id is not None:
            self.root.after_cancel(self._offset_rebuild_after_id)
        self._offset_rebuild_after_id = self.root.after(250, self._rebuild_loop_after_offset_change)

    def _rebuild_loop_after_offset_change(self):
        self._offset_rebuild_after_id = None
        if self._loop_playing:
            self._start_loop_preview(start=self._loop_start)

    def _on_scale_move(self, value):
        self._update_time_label(float(value))

    def _update_time_label(self, t: float):
        self.time_label.configure(text=f"{fmt_time(t)} / {fmt_time(self.duration)}")

    def _on_raw_scale_release(self):
        """Scrubbing the raw video also moves the final-video scrubber to the matching spot."""
        t = self.time_var.get()
        self._show_preview(t)
        final_t = self._source_time_to_final_time(t)
        self.final_time_var.set(final_t)
        self._update_final_time_label(final_t)

    def _on_final_scale_move(self, value):
        self._update_final_time_label(float(value))

    def _on_final_scale_release(self):
        """Scrubbing the final (post-speed) timeline maps back to a source frame to preview."""
        source_t = self._final_time_to_source_time(self.final_time_var.get())
        self.time_var.set(source_t)
        self._update_time_label(source_t)
        self._show_preview(source_t)

    def _update_final_time_label(self, t: float, total: float | None = None):
        if total is None:
            total = self._final_duration()
        self.final_time_label.configure(text=f"{fmt_time(t)} / {fmt_time(total)}")

    def _final_duration(self) -> float:
        total = 0.0
        for (start, end), var in zip(self._chunks(), self.speed_vars):
            try:
                factor = float(var.get())
            except ValueError:
                factor = 1.0
            total += (end - start) / max(factor, 0.01)
        return total

    def _final_time_to_source_time(self, final_t: float) -> float:
        """Map a point on the final (post-speed) timeline back to a source video 1 time."""
        chunks = self._chunks()
        if not chunks:
            return self.trim_start
        acc = 0.0
        for i, (start, end) in enumerate(chunks):
            try:
                factor = float(self.speed_vars[i].get())
            except (ValueError, IndexError):
                factor = 1.0
            factor = max(factor, 0.01)
            seg_len = (end - start) / factor
            if final_t <= acc + seg_len or i == len(chunks) - 1:
                local = max(0.0, final_t - acc)
                return min(end, start + local * factor)
            acc += seg_len
        return chunks[-1][1]

    def _source_time_to_final_time(self, source_t: float) -> float:
        """Map a source video 1 time to the matching point on the final (post-speed) timeline."""
        acc = 0.0
        for (start, end), var in zip(self._chunks(), self.speed_vars):
            try:
                factor = float(var.get())
            except ValueError:
                factor = 1.0
            factor = max(factor, 0.01)
            if source_t <= end:
                local = max(0.0, source_t - start)
                return acc + local / factor
            acc += (end - start) / factor
        return acc

    def _refresh_final_range(self):
        if not hasattr(self, "final_scale"):
            return
        total = self._final_duration()
        self.final_scale.configure(to=max(0.01, total))
        cur = min(self.final_time_var.get(), total)
        self.final_time_var.set(cur)
        self._update_final_time_label(cur, total)

    # ── trim (overall crop of the exported range) ───────────────────────

    def set_trim_start(self):
        if self.clip is None:
            return
        t = round(self.time_var.get(), 2)
        if t >= self.trim_end - SPLIT_EPS:
            messagebox.showinfo("Can't trim there", "Trim start must be before the trim end.")
            return
        self.trim_start = t
        self._after_trim_change()

    def set_trim_end(self):
        if self.clip is None:
            return
        t = round(self.time_var.get(), 2)
        if t <= self.trim_start + SPLIT_EPS:
            messagebox.showinfo("Can't trim there", "Trim end must be after the trim start.")
            return
        self.trim_end = t
        self._after_trim_change()

    def reset_trim(self):
        if self.clip is None:
            return
        self.trim_start, self.trim_end = 0.0, self.duration
        self._after_trim_change()

    def _after_trim_change(self):
        self.splits = [s for s in self.splits if self.trim_start + SPLIT_EPS < s < self.trim_end - SPLIT_EPS]
        self._update_trim_label()
        self._rebuild_splits_list()
        self._rebuild_chunks()

    def _update_trim_label(self):
        full = self.trim_start <= SPLIT_EPS and self.trim_end >= self.duration - SPLIT_EPS
        if full:
            self.trim_label.configure(text="Exporting full video")
        else:
            self.trim_label.configure(text=f"Exporting {fmt_time(self.trim_start)} to {fmt_time(self.trim_end)}")

    @staticmethod
    def _resize_to_height(img: Image.Image, height: int) -> Image.Image:
        if img.height == height:
            return img
        ratio = height / img.height
        return img.resize((max(1, int(img.width * ratio)), height))

    @staticmethod
    def _resize_to_width(img: Image.Image, width: int) -> Image.Image:
        if img.width == width:
            return img
        ratio = width / img.width
        return img.resize((width, max(1, int(img.height * ratio))))

    def _show_preview(self, t: float):
        self._stop_loop_preview()
        if self.clip is None:
            return
        t = min(max(0.0, t), max(0.0, self.duration - 0.05))
        img1 = self._clamped_frame(self.clip, t, self.duration)
        if img1 is None:
            return
        img2 = None
        if self.clip2 is not None:
            img2 = self._clamped_frame(self.clip2, t + self._get_offset(), self.clip2.duration)
        combined = self._combine_frame_images(img1, img2)
        self._draw_speed_badge(combined, self._current_speed_factor(t))
        self._preview_img = ImageTk.PhotoImage(combined)
        self.preview_label.configure(image=self._preview_img, text="")

    @staticmethod
    def _clamped_frame(clip, t: float, duration: float):
        """Grab a frame, clamping t so scrubbing/offsets never seek out of range."""
        t = min(max(0.0, t), max(0.0, duration - 0.05))
        try:
            return Image.fromarray(clip.get_frame(t))
        except Exception:
            return None

    def _combine_frame_images(self, img1: Image.Image, img2: Image.Image | None) -> Image.Image:
        if img2 is not None:
            target_h = 260
            img1 = self._resize_to_height(img1, target_h)
            img2 = self._resize_to_height(img2, target_h)
            combined = Image.new("RGB", (img1.width + img2.width, target_h))
            combined.paste(img1, (0, 0))
            combined.paste(img2, (img1.width, 0))
        else:
            combined = img1
        max_w = 640
        if combined.width > max_w:
            combined = self._resize_to_width(combined, max_w)
        max_h = 360  # keep portrait-oriented videos from ballooning the preview area
        if combined.height > max_h:
            combined = self._resize_to_height(combined, max_h)
        return combined

    # ── 5s loop preview (for tuning sync live) ──────────────────────────

    def _toggle_loop_preview(self):
        if self._loop_playing:
            self._stop_loop_preview()
        else:
            self._start_loop_preview()

    def _start_loop_preview(self, start: float | None = None):
        if self.clip is None or self.busy:
            return
        if start is None:
            start = self.time_var.get()
        self._loop_start = start
        offset = self._get_offset()
        chunk_factors = []
        for (cs, ce), var in zip(self._chunks(), self.speed_vars):
            try:
                factor = float(var.get())
            except ValueError:
                factor = 1.0
            chunk_factors.append((cs, ce, factor))

        self.busy = True
        self.loop_btn.configure(state="disabled")
        self.status_label.configure(text="Building loop preview...")
        threading.Thread(
            target=self._build_loop_frames, args=(start, offset, chunk_factors), daemon=True
        ).start()

    def _build_loop_frames(self, start: float, offset: float, chunk_factors):
        fps = 10
        length = min(5.0, max(0.1, self.duration - start))
        frame_count = max(1, int(length * fps))
        frames = []
        for i in range(frame_count):
            t = min(start + i / fps, self.duration - 0.02)
            img1 = self._clamped_frame(self.clip, t, self.duration)
            if img1 is None:
                continue
            img2 = self._clamped_frame(self.clip2, t + offset, self.clip2.duration) if self.clip2 is not None else None
            combined = self._combine_frame_images(img1, img2)
            factor = next((f for cs, ce, f in chunk_factors if cs <= t <= ce), 1.0)
            self._draw_speed_badge(combined, factor)
            frames.append(combined)
        self.root.after(0, lambda: self._loop_frames_ready(frames, fps))

    def _loop_frames_ready(self, pil_frames, fps: int):
        self.busy = False
        self.loop_btn.configure(state="normal")
        if not pil_frames:
            self.status_label.configure(text="Couldn't build loop preview.")
            return
        self._cancel_loop_after()
        self._loop_photo_frames = [ImageTk.PhotoImage(f) for f in pil_frames]
        self._loop_playing = True
        self._loop_index = 0
        self.loop_btn.configure(text="\u25a0 Stop Loop")
        self.status_label.configure(text=f"Looping {len(pil_frames) / fps:.1f}s (click Stop Loop to end)")
        self._play_loop_step(int(1000 / fps))

    def _play_loop_step(self, interval_ms: int):
        if not self._loop_playing or not self._loop_photo_frames:
            return
        photo = self._loop_photo_frames[self._loop_index]
        self._preview_img = photo  # keep a reference so it isn't garbage collected
        self.preview_label.configure(image=photo, text="")
        self._loop_index = (self._loop_index + 1) % len(self._loop_photo_frames)
        self._loop_after_id = self.root.after(interval_ms, lambda: self._play_loop_step(interval_ms))

    def _cancel_loop_after(self):
        if self._loop_after_id is not None:
            try:
                self.root.after_cancel(self._loop_after_id)
            except Exception:
                pass
            self._loop_after_id = None

    def _stop_loop_preview(self):
        self._loop_playing = False
        self._cancel_loop_after()
        if hasattr(self, "loop_btn"):
            self.loop_btn.configure(text="\u25b6 Loop 5s Preview")

    def _current_speed_factor(self, t: float) -> float:
        """Speed multiplier of whichever chunk the given time falls in."""
        chunks = self._chunks()
        for i, (start, end) in enumerate(chunks):
            if start <= t <= end:
                try:
                    return float(self.speed_vars[i].get())
                except (ValueError, IndexError):
                    return 1.0
        return 1.0

    @staticmethod
    def _badge_font(size: int):
        """A scalable font for the badge, since PIL's built-in default one isn't."""
        for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    @classmethod
    def _draw_speed_badge(cls, img: Image.Image, factor: float):
        """Draw a fast-forward icon + 'Nx' label in the top-right corner.

        Sized relative to the image so it also reads well when burned into
        a full-resolution exported video, not just the small preview.
        """
        label = f"{factor:g}x"
        active = factor > 1.0
        color = (255, 200, 40) if active else (230, 230, 230)
        scale = max(1.0, img.width / 640)
        font = cls._badge_font(int(20 * scale))
        draw = ImageDraw.Draw(img)
        try:
            x0, y0, x1, y1 = draw.textbbox((0, 0), label, font=font)
            text_w, text_h = x1 - x0, y1 - y0
        except AttributeError:
            text_w, text_h = int(len(label) * 11 * scale), int(16 * scale)
        icon_w, pad = int(20 * scale), int(6 * scale)
        box_h = max(int(24 * scale), text_h + int(8 * scale))
        box_w = pad * 2 + icon_w + int(6 * scale) + text_w
        x2, y1b = img.width - int(8 * scale), int(8 * scale)
        x1b, y2 = x2 - box_w, y1b + box_h
        draw.rectangle([x1b, y1b, x2, y2], fill=(20, 20, 20))
        cy, bx = (y1b + y2) // 2, x1b + pad
        tri = int(6 * scale) or 1
        tri_w = int(8 * scale) or 1
        for dx in (0, tri_w):  # two triangles = a "fast forward" glyph
            draw.polygon([(bx + dx, cy - tri), (bx + dx, cy + tri), (bx + dx + tri_w, cy)], fill=color)
        # anchor="lm" (left/vertical-middle) so the label sits centered on cy, matching the icon
        draw.text((bx + icon_w, cy), label, fill=color, font=font, anchor="lm")

    def _apply_speed_badge_to_clip(self, clip, factor: float):
        """Burn the speed badge into every frame, so it shows up in the export too."""
        def stamp(frame):
            img = Image.fromarray(frame).convert("RGB")
            self._draw_speed_badge(img, factor)
            return np.array(img)
        return clip.image_transform(stamp)

    # ── splits / chunks ──────────────────────────────────────────────────

    def add_split(self):
        if self.clip is None:
            return
        t = round(self.time_var.get(), 2)
        if t <= self.trim_start + SPLIT_EPS or t >= self.trim_end - SPLIT_EPS:
            messagebox.showinfo("Can't split there", "Pick a point inside the trimmed range, not at the very start/end.")
            return
        if any(abs(t - s) < SPLIT_EPS for s in self.splits):
            return
        self.splits.append(t)
        self.splits.sort()
        self._rebuild_splits_list()
        self._rebuild_chunks()

    def remove_split(self):
        sel = self.split_list.curselection()
        if not sel:
            return
        del self.splits[sel[0]]
        self._rebuild_splits_list()
        self._rebuild_chunks()

    def _rebuild_splits_list(self):
        self.split_list.delete(0, "end")
        for s in self.splits:
            self.split_list.insert("end", fmt_time(s))

    def _chunks(self) -> list[tuple[float, float]]:
        bounds = [self.trim_start] + self.splits + [self.trim_end]
        return list(zip(bounds[:-1], bounds[1:]))

    def _rebuild_chunks(self):
        for w in self.chunk_frame.winfo_children():
            w.destroy()
        old_vals = [v.get() for v in self.speed_vars]
        self.speed_vars = []
        for i, (start, end) in enumerate(self._chunks()):
            row = ttk.Frame(self.chunk_frame)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"Chunk {i + 1}: {fmt_time(start)} - {fmt_time(end)}", width=34).pack(side="left")
            ttk.Label(row, text="Speed x").pack(side="left")
            default = old_vals[i] if i < len(old_vals) else "1.0"
            var = tk.StringVar(value=default)
            var.trace_add("write", self._on_speed_changed)
            ttk.Entry(row, textvariable=var, width=6).pack(side="left", padx=4)
            self.speed_vars.append(var)
        self._refresh_final_range()

    def _on_speed_changed(self, *_):
        self._show_preview(self.time_var.get())
        self._refresh_final_range()

    # ── export ───────────────────────────────────────────────────────────

    def export(self):
        if self.clip is None or self.busy:
            return
        chunks = self._chunks()
        factors: list[float] = []
        for var in self.speed_vars:
            try:
                factor = float(var.get())
            except ValueError:
                messagebox.showerror("Invalid speed", f"'{var.get()}' isn't a number.")
                return
            if factor <= 0:
                messagebox.showerror("Invalid speed", "Speeds must be greater than 0.")
                return
            factors.append(factor)

        base = os.path.splitext(os.path.basename(self.video_path))[0]
        out_path = filedialog.asksaveasfilename(
            title="Save sped-up video as",
            defaultextension=".mp4",
            initialfile=f"{base}_sped.mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if not out_path:
            return

        self.busy = True
        self.export_btn.configure(state="disabled")
        self.status_label.configure(text="Exporting... this can take a while.")
        threading.Thread(target=self._export_worker, args=(chunks, factors, out_path), daemon=True).start()

    def _make_chunk_clip(self, start: float, end: float, factor: float):
        """Build one chunk's clip: video 1 alone, or stacked with synced video 2.

        When video 2 is loaded, every chunk is rendered at the same stacked
        frame size (using a black filler for video 2 if it doesn't cover this
        chunk's time range) -- mixing frame sizes between chunks makes most
        players freeze partway through the concatenated output.
        """
        sub1 = self.clip.subclipped(start, end)
        chunk_clip = sub1
        if self.clip2 is not None:
            length = end - start
            target_h = min(self.clip.h, self.clip2.h)
            left = sub1.resized(height=target_h)
            d2 = self.clip2.duration
            s2 = min(max(0.0, start + self._get_offset()), max(0.0, d2 - length))
            if d2 >= length and 0 <= s2 <= d2 - length:
                sub2 = self.clip2.subclipped(s2, s2 + length).without_audio()
                right = sub2.resized(height=target_h)
            else:
                right_w = max(1, int(target_h * self.clip2.w / self.clip2.h))
                right = ColorClip(size=(right_w, target_h), color=(0, 0, 0)).with_duration(length)
            right = right.with_position((left.w, 0))
            stacked = CompositeVideoClip([left, right], size=(left.w + right.w, target_h))
            chunk_clip = stacked.with_duration(length).with_audio(sub1.audio)
        if abs(factor - 1.0) > 1e-9:
            chunk_clip = chunk_clip.with_effects([vfx.MultiplySpeed(factor)])
        return self._apply_speed_badge_to_clip(chunk_clip, factor)

    def _export_worker(self, chunks, factors, out_path):
        pieces = []
        try:
            for (start, end), factor in zip(chunks, factors):
                pieces.append(self._make_chunk_clip(start, end, factor))
            final = concatenate_videoclips(pieces) if len(pieces) > 1 else pieces[0]
            final.write_videofile(out_path, codec="libx264", audio_codec="aac", logger=None)
            final.close()
            self.root.after(0, lambda: self._export_done(True, out_path))
        except Exception as exc:
            self.root.after(0, lambda: self._export_done(False, str(exc)))
        finally:
            for p in pieces:
                try:
                    p.close()
                except Exception:
                    pass

    def _export_done(self, ok: bool, info: str):
        self.busy = False
        self.export_btn.configure(state="normal")
        if ok:
            self.status_label.configure(text="Done!")
            messagebox.showinfo("Export complete", f"Saved to:\n{info}")
        else:
            self.status_label.configure(text="Failed.")
            messagebox.showerror("Export failed", info)

    def _on_close(self):
        self._stop_loop_preview()
        for clip in (self.clip, self.clip2):
            if clip is not None:
                try:
                    clip.close()
                except Exception:
                    pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
