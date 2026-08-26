#!/usr/bin/env python3
"""
snake_gui.py
Themed desktop app that builds a snake-flow block diagram directly inside an
*already-open* PowerPoint window (via COM automation), not a separate file.

  1. Open PowerPoint with the presentation you want to draw on.
  2. Run:  python snake_gui.py     (or double-click run_gui.bat)
  3. Type one item per line in the app, tweak the settings, click
     "Insert into PowerPoint". The in-app "How to use" panel walks through
     the rest (loading an existing diagram back in, reformatting, etc.).

Round-trips too: "Load from slide notes" / "Load from slide content" pull
the list back out of the active slide so you can edit and re-insert.

Requires:  pip install pywin32
Windows + desktop PowerPoint only (COM automation).
"""

from __future__ import annotations

import re
import math
import tkinter as tk
from tkinter import ttk, messagebox

import win32com.client
import pythoncom

import theme


# ── PowerPoint COM constants ─────────────────────────────────────────────────
PT = 72.0                       # points per inch (PowerPoint measures in points)

MSO_RECTANGLE       = 1
MSO_CONNECTOR_STRAIGHT = 1
MSO_CONNECTOR_ELBOW    = 2
MSO_TEXT_HORIZONTAL = 1
PP_LAYOUT_BLANK     = 12
PP_PLACEHOLDER_BODY = 2
MSO_ANCHOR_MIDDLE   = 3
PP_ALIGN_CENTER     = 2
MSO_AUTOSIZE_TEXT_TO_FIT = 2    # "shrink text on overflow"
MSO_ARROWHEAD_NONE       = 1
MSO_ARROWHEAD_TRIANGLE   = 2
MSO_TRUE, MSO_FALSE = -1, 0

# Rectangle connection-site indices (COM is 1-based; verified by rendering).
SITE_TOP, SITE_LEFT, SITE_BOTTOM, SITE_RIGHT = 1, 2, 3, 4

# Theme-colour slots (MsoThemeColorIndex).
THEME_ACCENT1     = 5
THEME_BACKGROUND1 = 14          # usually white — used for box text
THEME_TEXT1       = 13          # Dark Grey, Text 1 — used for box border


# ── settings ─────────────────────────────────────────────────────────────────
DEFAULTS = dict(
    boxes_per_row = 7,
    box_w         = 1.58,       # inches
    box_h         = 1.0,
    gap_x         = 0.22,
    gap_y         = 0.2,
    margin_left   = 0.45,
    margin_top    = 1.2,
    font_name     = "Lato",
    font_size     = 10,
    number_items  = False,
    layout        = "wrap",     # "wrap" (all rows L→R) | "serpentine"
    title         = "Process Flow",
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _numbered(items, number):
    return [f"{i+1}. {t}" if number else t for i, t in enumerate(items)]


def _strip_number(text):
    return re.sub(r'^\s*\d+\s*[.)\-:]\s*', '', text).strip()


def _extract_runs(text_rng, skip_chars=0):
    """Return [(text, bold, italic, underline), ...] from a COM TextRange.
    The first skip_chars characters (e.g. a number prefix) are ignored."""
    result = []
    pos = 0
    try:
        for ri in range(1, text_rng.Runs().Count + 1):
            run  = text_rng.Runs(ri)
            rt   = run.Text
            end  = pos + len(rt)
            if end <= skip_chars:
                pos = end
                continue
            effective = rt[max(0, skip_chars - pos):]
            if effective:
                try:
                    b  = run.Font.Bold
                    it = run.Font.Italic
                    ul = run.Font.Underline
                except pythoncom.com_error:
                    b, it, ul = MSO_FALSE, MSO_FALSE, 0
                result.append((effective, b, it, ul))
            pos = end
    except pythoncom.com_error:
        try:
            txt = text_rng.Text[skip_chars:]
        except pythoncom.com_error:
            txt = ""
        if txt:
            result = [(txt, MSO_FALSE, MSO_FALSE, 0)]
    return result


def _apply_runs(text_rng, runs, prefix_len=0):
    """Apply per-run bold/italic/underline to text_rng starting after prefix_len chars.
    Assumes text_rng.Text is already set correctly."""
    pos = prefix_len + 1            # 1-based COM character index
    for rt, b, it, ul in runs:
        if not rt:
            continue
        try:
            cr = text_rng.Characters(pos, len(rt))
            cr.Font.Bold      = MSO_TRUE if b  else MSO_FALSE
            cr.Font.Italic    = MSO_TRUE if it else MSO_FALSE
            cr.Font.Underline = MSO_TRUE if ul else MSO_FALSE
        except pythoncom.com_error:
            pass
        pos += len(rt)


def _pos(idx, cfg):
    """(left, top) in points for the box at list-index idx."""
    n   = cfg["boxes_per_row"]
    row = idx // n
    col = idx % n
    if cfg["layout"] == "serpentine" and row % 2 == 1:
        col = n - 1 - col
    left = (cfg["margin_left"] + col * (cfg["box_w"] + cfg["gap_x"])) * PT
    top  = (cfg["margin_top"]  + row * (cfg["box_h"] + cfg["gap_y"])) * PT
    return left, top


# ── PowerPoint access ────────────────────────────────────────────────────────

def get_powerpoint():
    """Attach to a running PowerPoint, or start one if none is open."""
    try:
        app = win32com.client.GetActiveObject("PowerPoint.Application")
    except pythoncom.com_error:
        app = win32com.client.Dispatch("PowerPoint.Application")
    app.Visible = True
    return app


def _active_presentation(app):
    if app.Presentations.Count == 0:
        return app.Presentations.Add()
    return app.ActivePresentation


def _notes_textframe(slide):
    for ph in slide.NotesPage.Shapes.Placeholders:
        if ph.PlaceholderFormat.Type == PP_PLACEHOLDER_BODY:
            return ph.TextFrame
    return slide.NotesPage.Shapes.Placeholders(2).TextFrame


# ── drawing ──────────────────────────────────────────────────────────────────

def _draw_box(slide, text, idx, cfg, runs=None, prefix_len=0):
    left, top = _pos(idx, cfg)
    w = cfg["box_w"] * PT
    h = cfg["box_h"] * PT
    shp = slide.Shapes.AddShape(MSO_RECTANGLE, left, top, w, h)
    shp.Name = f"box_{idx:03d}"

    shp.Fill.ForeColor.ObjectThemeColor = THEME_ACCENT1
    shp.Line.ForeColor.ObjectThemeColor = THEME_TEXT1   # Dark Grey, Text 1
    shp.Line.Weight = 1.25

    tf = shp.TextFrame
    tf.WordWrap = MSO_TRUE
    rng = tf.TextRange
    rng.Text = text
    rng.ParagraphFormat.Alignment = PP_ALIGN_CENTER
    rng.Font.Name = cfg["font_name"]
    rng.Font.Size = 14          # maximum; AutoSize shrinks only when text overflows the box
    rng.Font.Bold = MSO_FALSE
    rng.Font.Color.ObjectThemeColor = THEME_BACKGROUND1
    if runs:
        _apply_runs(rng, runs, prefix_len)
    try:
        tf2 = shp.TextFrame2
        tf2.VerticalAnchor = MSO_ANCHOR_MIDDLE
        tf2.AutoSize = MSO_AUTOSIZE_TEXT_TO_FIT      # shrink to fit the box
    except pythoncom.com_error:
        tf.VerticalAnchor = MSO_ANCHOR_MIDDLE
    return shp


def _style_line(conn, arrow):
    conn.Line.ForeColor.ObjectThemeColor = THEME_ACCENT1
    conn.Line.Weight = 1.5
    conn.Line.BeginArrowheadStyle = MSO_ARROWHEAD_NONE
    conn.Line.EndArrowheadStyle = MSO_ARROWHEAD_TRIANGLE if arrow else MSO_ARROWHEAD_NONE
    return conn


def _draw_connector(slide, boxes, ia, ib, cfg):
    n        = cfg["boxes_per_row"]
    row_a    = ia // n
    same_row = row_a == (ib // n)
    xa, ya   = _pos(ia, cfg)
    xb, yb   = _pos(ib, cfg)
    w = cfg["box_w"] * PT
    h = cfg["box_h"] * PT

    if same_row:
        reversed_row = (cfg["layout"] == "serpentine" and row_a % 2 == 1)
        beg, end = (SITE_LEFT, SITE_RIGHT) if reversed_row else (SITE_RIGHT, SITE_LEFT)
        conn = slide.Shapes.AddConnector(MSO_CONNECTOR_STRAIGHT,
                                         xa + w, ya + h / 2, xb, yb + h / 2)
        conn.ConnectorFormat.BeginConnect(boxes[ia], beg)
        conn.ConnectorFormat.EndConnect(boxes[ib], end)
        return _style_line(conn, arrow=True)

    # Row break → a single elbow connector that snaps to both boxes.  In the
    # live app PowerPoint routes it as a clean ⊐ through the row gap.  For wrap
    # it enters the left side of the first box on the next row (matching the
    # reference); for serpentine the box sits directly below, so it enters the
    # top as a short vertical drop.
    if cfg["layout"] == "wrap":
        end_site = SITE_LEFT
        ex, ey = xb, yb + h / 2
    else:
        end_site = SITE_TOP
        ex, ey = xb + w / 2, yb
    conn = slide.Shapes.AddConnector(MSO_CONNECTOR_ELBOW,
                                     xa + w / 2, ya + h, ex, ey)
    conn.ConnectorFormat.BeginConnect(boxes[ia], SITE_BOTTOM)
    conn.ConnectorFormat.EndConnect(boxes[ib], end_site)
    return _style_line(conn, arrow=True)


# ── title helpers ─────────────────────────────────────────────────────────────

def _get_slide_title(slide):
    """Return the text of the slide's title placeholder (type 1 or 3), or ''."""
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            if s.PlaceholderFormat.Type in (1, 3):   # title / centre-title
                return s.TextFrame.TextRange.Text.strip()
        except pythoncom.com_error:
            continue
    return ""


def _set_slide_title(slide, pres, text, cfg):
    """Write *text* into the slide title.
    If the slide has a title placeholder, update its text only so the
    placeholder's existing font/colour/size are preserved.
    Otherwise fall back to adding a plain textbox."""
    if not text:
        return
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            if s.PlaceholderFormat.Type in (1, 3):
                s.TextFrame.TextRange.Text = text
                return
        except pythoncom.com_error:
            continue
    # No title placeholder on this layout — add (or update) a plain textbox.
    sw = pres.PageSetup.SlideWidth
    ml = cfg["margin_left"] * PT
    ty = cfg.get("_title_top", 0.2) * PT
    # Look for a previously-created snake title textbox and reuse it.
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            if s.Name == "_snake_title_":
                s.TextFrame.TextRange.Text = text
                s.Left  = ml
                s.Top   = ty
                s.Width = sw - 2 * ml
                return
        except pythoncom.com_error:
            continue
    tb = slide.Shapes.AddTextbox(MSO_TEXT_HORIZONTAL,
                                 ml, ty, sw - 2 * ml, 0.75 * PT)
    tb.Name = "_snake_title_"
    rng = tb.TextFrame.TextRange
    rng.Text = text
    rng.Font.Name = cfg["font_name"]
    rng.Font.Size = 20
    rng.Font.Bold = MSO_TRUE


def _load_notes_runs(slide, max_items):
    """Read per-paragraph bold/italic/underline from existing notes (up to max_items)."""
    result = []
    try:
        notes_tf = _notes_textframe(slide)
        n_paras  = notes_tf.TextRange.Paragraphs().Count
        for pi in range(1, min(n_paras, max_items) + 1):
            para  = notes_tf.TextRange.Paragraphs(pi)
            ptxt  = para.Text.rstrip('\r\n')
            skip  = len(ptxt) - len(_strip_number(ptxt))   # handle legacy numbers
            result.append(_extract_runs(para, max(0, skip)))
    except pythoncom.com_error:
        pass
    return result


def _write_notes_from_boxes(notes_tf, box_shapes, items, labels):
    """Write slide notes: one paragraph per item (no numbers), formatting from boxes."""
    notes_tf.TextRange.Text = "\r".join(items)   # \r = paragraph break in PPT
    for i, (item, shape, label) in enumerate(zip(items, box_shapes, labels)):
        try:
            prefix_len = max(0, len(label) - len(item))
            runs = _extract_runs(shape.TextFrame.TextRange, prefix_len)
            if runs:
                _apply_runs(notes_tf.TextRange.Paragraphs(i + 1), runs)
        except pythoncom.com_error:
            continue


def draw_snake(app, items, cfg, new_slide=True):
    pres = _active_presentation(app)

    if new_slide or pres.Slides.Count == 0:
        slide = pres.Slides.Add(pres.Slides.Count + 1, PP_LAYOUT_BLANK)
        saved_runs = []
    else:
        slide = app.ActiveWindow.View.Slide
        saved_runs = _load_notes_runs(slide, len(items))

    # Auto-centre the diagram (title + boxes) both horizontally and
    # vertically on the slide.
    n_rows = math.ceil(len(items) / cfg["boxes_per_row"])
    n_cols = min(len(items), cfg["boxes_per_row"])
    diagram_w_in  = n_cols * cfg["box_w"] + (n_cols - 1) * cfg["gap_x"]
    box_area_h_in = n_rows * cfg["box_h"] + (n_rows - 1) * cfg["gap_y"]
    slide_w_in    = pres.PageSetup.SlideWidth  / PT
    slide_h_in    = pres.PageSetup.SlideHeight / PT

    title_zone_in = 1.0 if cfg["title"] else 0.0   # title 0.75" + gap 0.25"
    content_h_in  = title_zone_in + box_area_h_in
    title_top_in  = max(0.15, (slide_h_in - content_h_in) / 2)

    cfg = dict(cfg)                                 # don't mutate caller's dict
    cfg["margin_left"] = max(0.1,  (slide_w_in - diagram_w_in) / 2)
    cfg["margin_top"]  = max(0.15, title_top_in + title_zone_in)
    cfg["_title_top"]  = title_top_in              # passed to _set_slide_title

    _set_slide_title(slide, pres, cfg["title"], cfg)

    labels = _numbered(items, cfg["number_items"])
    boxes  = {}
    for i, (lbl, item) in enumerate(zip(labels, items)):
        prefix_len = len(lbl) - len(item)
        runs = saved_runs[i] if i < len(saved_runs) else None
        boxes[i] = _draw_box(slide, lbl, i, cfg, runs=runs, prefix_len=prefix_len)

    for i in range(len(items) - 1):
        _draw_connector(slide, boxes, i, i + 1, cfg)

    _write_notes_from_boxes(
        _notes_textframe(slide),
        [boxes[i] for i in range(len(items))],
        items,
        labels,
    )

    try:
        slide.Select()
    except pythoncom.com_error:
        pass
    return slide, len(items)


def reformat_in_place(app, items, cfg):
    """Reformat a snake diagram by MOVING existing boxes to their new positions
    rather than deleting and recreating them.  Only connectors are redrawn, so
    any formatting, hyperlinks, or edits the user made to the boxes are kept."""
    pres  = _active_presentation(app)
    slide = app.ActiveWindow.View.Slide

    # Compute centred layout with current settings.
    n      = len(items)
    n_rows = math.ceil(n / cfg["boxes_per_row"])
    n_cols = min(n, cfg["boxes_per_row"])
    diagram_w_in  = n_cols * cfg["box_w"] + (n_cols - 1) * cfg["gap_x"]
    box_area_h_in = n_rows * cfg["box_h"] + (n_rows - 1) * cfg["gap_y"]
    slide_w_in    = pres.PageSetup.SlideWidth  / PT
    slide_h_in    = pres.PageSetup.SlideHeight / PT
    title_zone_in = 1.0 if cfg["title"] else 0.0
    content_h_in  = title_zone_in + box_area_h_in
    title_top_in  = max(0.15, (slide_h_in - content_h_in) / 2)

    cfg = dict(cfg)
    cfg["margin_left"] = max(0.1,  (slide_w_in - diagram_w_in) / 2)
    cfg["margin_top"]  = max(0.15, title_top_in + title_zone_in)
    cfg["_title_top"]  = title_top_in

    # Update the title in-place (never adds a duplicate).
    _set_slide_title(slide, pres, cfg["title"], cfg)

    # Snapshot existing boxes in reading order BEFORE any changes.
    _, existing_shapes = _ordered_shapes_and_items(slide)

    # Delete only connectors (msoConnector = 9).
    to_delete = []
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            if s.Type == 9:
                to_delete.append(s)
        except pythoncom.com_error:
            continue
    for s in to_delete:
        try:
            s.Delete()
        except pythoncom.com_error:
            pass

    # Load notes runs for any items that need new boxes.
    saved_runs = _load_notes_runs(slide, n)

    # Move/resize existing boxes to new positions; create new ones for extras.
    labels = _numbered(items, cfg["number_items"])
    boxes  = {}
    for i, (lbl, item) in enumerate(zip(labels, items)):
        if i < len(existing_shapes):
            shp        = existing_shapes[i]
            left, top  = _pos(i, cfg)
            shp.Left   = left
            shp.Top    = top
            shp.Width  = cfg["box_w"] * PT
            shp.Height = cfg["box_h"] * PT
            shp.Name   = f"box_{i:03d}"
            boxes[i]   = shp
        else:
            runs       = saved_runs[i] if i < len(saved_runs) else None
            prefix_len = len(lbl) - len(item)
            boxes[i]   = _draw_box(slide, lbl, i, cfg, runs=runs, prefix_len=prefix_len)

    # Redraw connectors.
    for i in range(n - 1):
        _draw_connector(slide, boxes, i, i + 1, cfg)

    # Update notes (preserves formatting from boxes).
    _write_notes_from_boxes(
        _notes_textframe(slide),
        [boxes[i] for i in range(n)],
        items,
        labels,
    )

    try:
        slide.Select()
    except pythoncom.com_error:
        pass
    return slide, n


def _read_autoboxes_ordered(slide):
    """Read text from every AutoShape (Type 1) on the slide that has text,
    sorted into snake reading order: rows top-to-bottom, left-to-right within
    each row.  Works on any existing diagram regardless of how it was made."""
    raw = []
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            # msoAutoShape = 1; skip connectors (9), textboxes (17), etc.
            if s.Type == 1 and s.HasTextFrame:
                text = s.TextFrame.TextRange.Text.strip()
                if text:
                    raw.append((s.Top, s.Left, s.Height, text))
        except pythoncom.com_error:
            continue

    if not raw:
        return []

    raw.sort(key=lambda x: x[0])   # sort by Top first

    # Cluster into rows: shapes whose Top is within 60 % of the average height
    # of a neighbour's top are considered the same row.
    avg_h = sum(h for _, _, h, _ in raw) / len(raw)
    threshold = avg_h * 0.6

    rows, current = [], [raw[0]]
    for shape in raw[1:]:
        if shape[0] - current[0][0] <= threshold:
            current.append(shape)
        else:
            rows.append(sorted(current, key=lambda x: x[1]))   # sort row L→R
            current = [shape]
    rows.append(sorted(current, key=lambda x: x[1]))

    return [_strip_number(text) for row in rows for _, _, _, text in row]


def _ordered_shapes_and_items(slide):
    """Like _read_autoboxes_ordered but also returns the shape objects in order."""
    raw = []
    for i in range(1, slide.Shapes.Count + 1):
        try:
            s = slide.Shapes(i)
            if s.Type == 1 and s.HasTextFrame:
                text = s.TextFrame.TextRange.Text.strip()
                if text:
                    raw.append((s.Top, s.Left, s.Height, text, s))
        except pythoncom.com_error:
            continue

    if not raw:
        return [], []

    raw.sort(key=lambda x: x[0])
    avg_h     = sum(h for _, _, h, _, _ in raw) / len(raw)
    threshold = avg_h * 0.6

    rows, current = [], [raw[0]]
    for entry in raw[1:]:
        if entry[0] - current[0][0] <= threshold:
            current.append(entry)
        else:
            rows.append(sorted(current, key=lambda x: x[1]))
            current = [entry]
    rows.append(sorted(current, key=lambda x: x[1]))

    items  = [_strip_number(t) for row in rows for _, _, _, t, _ in row]
    shapes = [s              for row in rows for _, _, _, _, s in row]
    return items, shapes


# ══════════════════════════════ GUI ══════════════════════════════════════════

class _Tooltip:
    """Hover tooltip for any widget, styled like a sticky note (matches the
    brand theme's TIP_BG/TIP_FG). Attach with `_tip(widget, "explanation")`."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _show(self, _event=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left",
                background=theme.TIP_BG, foreground=theme.TIP_FG,
                relief="solid", borderwidth=1, padx=6, pady=4,
                wraplength=280).pack()

    def _hide(self, _event=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _tip(widget, text):
    _Tooltip(widget, text)
    return widget


HOW_TO_USE = (
    "1.  Open PowerPoint with the presentation you want to draw on.\n"
    "2.  Type one item per line below — each line becomes one box.\n"
    "3.  Adjust the settings on the right if you want (hover over a "
    "setting to see what it does).\n"
    "4.  Pick \"New slide\" or \"Active slide\" under Draw on.\n"
    "5.  Click \"Insert into PowerPoint\".\n\n"
    "Already have a diagram on the slide? Click \"Load from slide notes\" "
    "or \"Load from slide content\" first to pull its list into the box "
    "below, edit it, then use \"Insert\" (redraws everything) or "
    "\"Reformat slide (beta)\" (just moves the existing boxes — keeps "
    "their wording and formatting; save first, it's still rough)."
)


class SnakeApp:
    def __init__(self, root):
        self.root = root
        root.title("Snake Diagram Builder  v1.1.0")
        root.minsize(760, 560)
        self.family = theme.apply_theme(root)

        self._build()

    # ---- layout ----
    def _build(self):
        root = self.root
        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        # How-to-use guide — always visible so nothing is assumed knowledge.
        guide = ttk.Labelframe(outer, text="How to use", padding=8)
        guide.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(guide, text=HOW_TO_USE, justify="left",
                 wraplength=700).pack(anchor="w")

        # Items (the diagram's content, typed directly here)
        left = ttk.Labelframe(outer, text="Diagram items", padding=8)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))

        ttk.Label(left, text="One item per line:").pack(anchor="w")
        items_frame = ttk.Frame(left)
        items_frame.pack(fill="both", expand=True, pady=(2, 6))
        self.items_txt = tk.Text(items_frame, height=12, width=32, wrap="word",
                                 undo=True)
        items_scroll = ttk.Scrollbar(items_frame, orient="vertical",
                                    command=self.items_txt.yview)
        self.items_txt.configure(yscrollcommand=items_scroll.set)
        self.items_txt.pack(side="left", fill="both", expand=True)
        items_scroll.pack(side="right", fill="y")
        _tip(self.items_txt,
            "Each line becomes one box, in order. Blank lines are ignored.")

        load_row = ttk.Frame(left)
        load_row.pack(fill="x")
        load_notes_btn = ttk.Button(load_row, text="Load from slide notes",
                                   command=self.on_load_notes)
        load_notes_btn.pack(side="left")
        _tip(load_notes_btn,
            "Reads the item list typed in the active slide's Notes pane "
            "into the box above, so you can edit it here.")
        load_shapes_btn = ttk.Button(load_row, text="Load from slide content",
                                    command=self.on_load_shapes)
        load_shapes_btn.pack(side="left", padx=(6, 0))
        _tip(load_shapes_btn,
            "Ignores the notes and instead reads the text already on the "
            "slide's boxes/shapes into the box above, so you can edit it "
            "here.")
        sync_btn = ttk.Button(load_row, text="Sync edited boxes",
                              command=self.on_extract)
        sync_btn.pack(side="left", padx=(6, 0))
        _tip(sync_btn,
            "If you manually edited a box's text or formatting directly in "
            "PowerPoint, click this to pull those changes back into the "
            "item list (and the slide's notes) so Reformat/Insert stay in "
            "sync.")

        # Settings
        right = ttk.Labelframe(outer, text="Settings", padding=8)
        right.grid(row=1, column=1, sticky="nsew")

        r = 0
        ttk.Label(right, text="Slide title").grid(row=r, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar(value=DEFAULTS["title"])
        _tip(ttk.Entry(right, textvariable=self.title_var, width=22),
            "Optional heading placed above the diagram. Leave blank for no "
            "title.").grid(row=r, column=1, sticky="ew", pady=3)

        r += 1
        ttk.Label(right, text="Boxes per row").grid(row=r, column=0, sticky="w", pady=3)
        self.bpr_var = tk.IntVar(value=DEFAULTS["boxes_per_row"])
        _tip(ttk.Spinbox(right, from_=1, to=20, textvariable=self.bpr_var,
                        width=6),
            "How many boxes fit in one row before wrapping to the next "
            "row.").grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text="Layout").grid(row=r, column=0, sticky="w", pady=3)
        self.layout_var = tk.StringVar(value=DEFAULTS["layout"])
        _tip(ttk.Combobox(right, textvariable=self.layout_var, state="readonly",
                         width=14, values=["wrap", "serpentine"]),
            "wrap: every row reads left to right.\n"
            "serpentine: alternate rows reverse direction (like a snake) — "
            "useful for long lists that fold back and forth.").grid(
            row=r, column=1, sticky="w", pady=3)

        r += 1
        self.num_var = tk.BooleanVar(value=DEFAULTS["number_items"])
        _tip(ttk.Checkbutton(right, text="Number the items",
                            variable=self.num_var),
            "Adds \"1.\", \"2.\", ... in front of each box's text.").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 3))

        r += 1
        ttk.Label(right, text="Row gap (in)").grid(row=r, column=0, sticky="w", pady=3)
        self.gapy_var = tk.DoubleVar(value=DEFAULTS["gap_y"])
        _tip(ttk.Spinbox(right, from_=0.1, to=5.0, increment=0.05,
                        textvariable=self.gapy_var, width=6, format="%.2f"),
            "Vertical space between rows of boxes, in inches.").grid(
            row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text="Column gap (in)").grid(row=r, column=0, sticky="w", pady=3)
        self.gapx_var = tk.DoubleVar(value=DEFAULTS["gap_x"])
        _tip(ttk.Spinbox(right, from_=0.05, to=5.0, increment=0.05,
                        textvariable=self.gapx_var, width=6, format="%.2f"),
            "Horizontal space between boxes in the same row, in inches.").grid(
            row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text="Box width (in)").grid(row=r, column=0, sticky="w", pady=3)
        self.boxw_var = tk.DoubleVar(value=DEFAULTS["box_w"])
        _tip(ttk.Spinbox(right, from_=0.5, to=6.0, increment=0.05,
                        textvariable=self.boxw_var, width=6, format="%.2f"),
            "Width of each box, in inches.").grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text="Box height (in)").grid(row=r, column=0, sticky="w", pady=3)
        self.boxh_var = tk.DoubleVar(value=DEFAULTS["box_h"])
        _tip(ttk.Spinbox(right, from_=0.3, to=6.0, increment=0.05,
                        textvariable=self.boxh_var, width=6, format="%.2f"),
            "Height of each box, in inches.").grid(row=r, column=1, sticky="w", pady=3)

        r += 1
        ttk.Label(right, text=f"Font: {DEFAULTS['font_name']} "
                             f"{DEFAULTS['font_size']}pt (fixed, shrinks to "
                             "fit)", foreground=theme.MUTED,
                 wraplength=200, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(2, 0))

        r += 1
        ttk.Separator(right).grid(row=r, column=0, columnspan=2,
                                  sticky="ew", pady=8)

        r += 1
        ttk.Label(right, text="Draw on").grid(row=r, column=0, sticky="w", pady=3)
        self.target_var = tk.StringVar(value="new")
        tf = ttk.Frame(right)
        tf.grid(row=r, column=1, sticky="w")
        _tip(ttk.Radiobutton(tf, text="New slide", value="new",
                            variable=self.target_var),
            "Adds a fresh slide for this diagram.").pack(anchor="w")
        _tip(ttk.Radiobutton(tf, text="Active slide", value="active",
                            variable=self.target_var),
            "Draws directly on whichever slide is open in PowerPoint right "
            "now. Use this the first time you draw onto a specific "
            "existing slide.").pack(anchor="w")

        # Buttons
        btns = ttk.Frame(outer)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 6))
        reformat_btn = ttk.Button(btns, text="Reformat slide (beta)",
                                  command=self.on_reformat)
        reformat_btn.pack(side="left")
        _tip(reformat_btn,
            "BETA — repositions the boxes already on the active slide to "
            "match the settings on the right (spacing, boxes-per-row, "
            "layout). Does NOT change the wording already in the boxes. "
            "This feature is still rough and may not do what you expect — "
            "save your presentation before using it.")
        insert_btn = ttk.Button(btns, text="Insert into PowerPoint",
                                style="Accent.TButton",
                                command=self.on_insert)
        insert_btn.pack(side="right")
        _tip(insert_btn,
            "Draws a new snake diagram from the items above, using the "
            "settings on the right.")

        # Status log
        logf = ttk.Labelframe(outer, text="Status", padding=6)
        logf.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.log_txt = tk.Text(logf, height=6, wrap="word")
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.configure(state="disabled")

        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=0)
        outer.rowconfigure(1, weight=1)
        outer.rowconfigure(3, weight=1)
        self._log("Ready. Type your items on the left, then click "
                  "\"Insert into PowerPoint\". See \"How to use\" above for "
                  "the full walkthrough.")

    # ---- helpers ----
    def _log(self, msg):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _items(self):
        text = self.items_txt.get("1.0", "end-1c")
        return [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _set_items(self, items):
        self.items_txt.delete("1.0", "end")
        self.items_txt.insert("1.0", "\n".join(items))

    def _cfg(self):
        cfg = dict(DEFAULTS)
        cfg.update(
            boxes_per_row=max(1, self.bpr_var.get()),
            layout=self.layout_var.get(),
            gap_y=max(0.1, self.gapy_var.get()),
            gap_x=max(0.05, self.gapx_var.get()),
            box_w=max(0.3, self.boxw_var.get()),
            box_h=max(0.2, self.boxh_var.get()),
            number_items=self.num_var.get(),
            title=self.title_var.get().strip(),
        )
        return cfg

    # ---- actions ----
    def on_insert(self):
        items = self._items()
        if not items:
            self._log("Type at least one item on the left (one per line), "
                      "or click \"Load from slide notes/content\" first.")
            return
        try:
            app = get_powerpoint()
            new_slide = self.target_var.get() == "new"
            _, n = draw_snake(app, items, self._cfg(), new_slide=new_slide)
            where = "a new slide" if new_slide else "the active slide"
            self._log(f"Inserted {n} boxes ({self.layout_var.get()} layout) "
                      f"on {where}.")
        except pythoncom.com_error as exc:
            self._log(f"PowerPoint error: {exc}")
            messagebox.showerror("PowerPoint error", str(exc))
        except Exception as exc:                                # noqa: BLE001
            self._log(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))

    def on_load_notes(self):
        """Pull the item list typed in the active slide's Notes pane into
        the items box for editing."""
        try:
            app = get_powerpoint()
            slide = app.ActiveWindow.View.Slide
            title = _get_slide_title(slide)
            if title:
                self.title_var.set(title)
            lines = _notes_textframe(slide).TextRange.Text.splitlines()
            items = [_strip_number(ln) for ln in lines if ln.strip()]
            if not items:
                self._log("The active slide's notes are empty — type your "
                          "list on the left, then click Insert.")
                return
            self._set_items(items)
            self._log(f"Loaded {len(items)} items from the slide's notes. "
                      "Edit them on the left, then click Insert or "
                      "Reformat slide.")
        except pythoncom.com_error as exc:
            self._log(f"PowerPoint error: {exc}")
            messagebox.showerror("PowerPoint error", str(exc))
        except Exception as exc:                                # noqa: BLE001
            self._log(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))

    def on_load_shapes(self):
        """Pull the text already on the active slide's boxes/shapes (ignoring
        notes) into the items box for editing."""
        try:
            app = get_powerpoint()
            slide = app.ActiveWindow.View.Slide
            title = _get_slide_title(slide)
            if title:
                self.title_var.set(title)
            boxes = [s for s in slide.Shapes if s.Name.startswith("box_")]
            if boxes:
                boxes.sort(key=lambda s: int(s.Name.split("_")[1]))
                items = [_strip_number(s.TextFrame.TextRange.Text) for s in boxes]
            else:
                items = _read_autoboxes_ordered(slide)
            if not items:
                self._log("No shapes with text found on the active slide.")
                return
            self._set_items(items)
            self._log(f"Loaded {len(items)} items from the slide's shapes. "
                      "Edit them on the left, then click Insert or "
                      "Reformat slide.")
        except pythoncom.com_error as exc:
            self._log(f"PowerPoint error: {exc}")
            messagebox.showerror("PowerPoint error", str(exc))
        except Exception as exc:                                # noqa: BLE001
            self._log(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))

    def on_extract(self):
        """Read every AutoShape with text from the active slide in positional
        reading order, write the numbered list to the slide notes, and load
        the items into the box on the left.  After this, use 'Reformat
        slide' to redraw everything cleanly using the current settings."""
        try:
            app = get_powerpoint()
            slide = app.ActiveWindow.View.Slide
            title = _get_slide_title(slide)
            if title:
                self.title_var.set(title)
            items, shapes = _ordered_shapes_and_items(slide)
            if not items:
                self._log("No shapes with text found on the active slide.")
                return
            _write_notes_from_boxes(
                _notes_textframe(slide), shapes, items,
                [s.TextFrame.TextRange.Text.strip() for s in shapes],
            )
            self._set_items(items)
            self._log(f"Synced {len(items)} items from the slide's boxes. "
                      "Edit them on the left, then click \"Reformat slide\".")
        except pythoncom.com_error as exc:
            self._log(f"PowerPoint error: {exc}")
            messagebox.showerror("PowerPoint error", str(exc))
        except Exception as exc:                                # noqa: BLE001
            self._log(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))

    def on_reformat(self):
        """Move existing boxes to new positions and redraw connectors."""
        items = self._items()
        if not items:
            self._log("Type at least one item on the left (one per line), "
                      "or click \"Load from slide notes/content\" first.")
            return
        proceed = messagebox.askyesno(
            "Reformat slide (beta)",
            "Reformat slide is still beta and may not do what you expect.\n\n"
            "Save your presentation before continuing, in case you need to "
            "undo the result.\n\nContinue anyway?",
            default="no",
        )
        if not proceed:
            self._log("Reformat cancelled.")
            return
        try:
            app = get_powerpoint()
            _, n = reformat_in_place(app, items, self._cfg())
            self._log(f"Reformatted slide: {n} boxes repositioned "
                      f"({self.layout_var.get()} layout).")
        except pythoncom.com_error as exc:
            self._log(f"PowerPoint error: {exc}")
            messagebox.showerror("PowerPoint error", str(exc))
        except Exception as exc:                                # noqa: BLE001
            self._log(f"Error: {exc}")
            messagebox.showerror("Error", str(exc))


def main():
    theme.set_dpi_aware()
    root = tk.Tk()
    SnakeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
