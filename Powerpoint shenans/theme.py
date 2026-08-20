"""Monarch Quantum brand theme for the MCC E-1608 I/O Control Tk UI.

Dark navy theme matching Multi Meca Moves / Fiber Source Coupling, so the
bench apps feel like a set. Per brand guide we do NOT place the logo or
wordmark in the UI; only the palette, font, and sentence-case rule. Call
`apply_theme(root)` once, right after `tk.Tk()` and before building
widgets.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk

# --- palette (HEX) ----------------------------------------------------------

# Brand primaries
NAVY = '#04124C'         # logo navy — used for accent borders, deep emphasis
BRIGHT_BLUE = '#4C81F1'  # primary interactive (buttons, focus, links)
ORANGE = '#FF9018'       # primary CTA highlight + selected marker
ORANGE_RED = '#E24B37'   # destructive / warn (sparing)
OK_GREEN = '#3FB984'     # nominal / settled status (status chips, sparing)
LIGHT_BLUE = '#BFD8FF'   # soft text on dark, banner / chip foreground

# Dark surface stack (matches Multi Meca Moves)
BG_DARK = '#1A1A2E'      # window background
BG_PANEL = '#16213E'     # LabelFrame / panel background
BG_INPUT = '#0F3460'     # entry / spinbox / combobox field
BG_CODE = '#0B0E1A'      # tk.Text / Listbox content (CSV / code-like)

# Text on dark
TEXT = '#E5EAF0'         # body text (high-contrast off-white)
MUTED = '#A4B0C5'        # secondary / hint text
DISABLED = '#5C6A82'     # disabled

# Neutrals retained for matplotlib & banners
WHITE = '#FFFFFF'
DARK_GRAY = '#333333'

# semantic aliases — call sites should prefer these
HEADING = LIGHT_BLUE     # LabelFrame titles read in light-blue
ACCENT = BRIGHT_BLUE
HIGHLIGHT = ORANGE
WARN = ORANGE_RED
OK = OK_GREEN
SURFACE = BG_PANEL       # legacy alias used by app.py
TIP_BG = LIGHT_BLUE      # tooltips: light sticky-note over dark UI
TIP_FG = NAVY
CHIP_BG = LIGHT_BLUE
CHIP_FG = NAVY

# Filled in by apply_theme()
BRAND_FAMILY = 'TkDefaultFont'


def set_dpi_aware() -> None:
    """Mark the process as per-monitor DPI aware (Windows). Must be called
    BEFORE the first `tk.Tk()` — Tk caches scaling at root creation time, so
    flipping awareness afterwards leaves text blurry on high-DPI displays.
    No-op on non-Windows or if the API is unavailable."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _resolve_brand_family() -> str:
    families = set(font.families())
    if 'Lato' in families:
        return 'Lato'
    if 'Arial' in families:
        return 'Arial'
    return 'TkDefaultFont'


def apply_theme(root: tk.Tk) -> str:
    """Configure ttk styles, default fonts, and non-ttk option DB."""
    global BRAND_FAMILY
    BRAND_FAMILY = _resolve_brand_family()
    family = BRAND_FAMILY

    style = ttk.Style(root)
    # clam respects color overrides on Windows; vista/xpnative don't.
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass

    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont',
                 'TkHeadingFont', 'TkCaptionFont', 'TkSmallCaptionFont',
                 'TkIconFont'):
        try:
            font.nametofont(name).configure(family=family, size=10)
        except tk.TclError:
            pass
    try:
        font.nametofont('TkFixedFont').configure(family='Consolas', size=10)
    except tk.TclError:
        pass

    root.configure(background=BG_DARK)

    # base — applies to everything not explicitly overridden
    style.configure('.',
                    background=BG_DARK, foreground=TEXT,
                    fieldbackground=BG_INPUT,
                    bordercolor=NAVY, lightcolor=NAVY, darkcolor=NAVY,
                    troughcolor=BG_PANEL,
                    selectbackground=BRIGHT_BLUE, selectforeground=WHITE,
                    font=(family, 10))

    # Unify the main surface to BG_DARK so labels inside LabelFrames don't
    # appear as floating dark rectangles on a different shade.
    style.configure('TFrame', background=BG_DARK)
    style.configure('TPanedwindow', background=BG_DARK)
    style.configure('TLabel', background=BG_DARK, foreground=TEXT)
    style.configure('TLabelframe', background=BG_DARK,
                    bordercolor=BRIGHT_BLUE,
                    lightcolor=BRIGHT_BLUE, darkcolor=BRIGHT_BLUE)
    style.configure('TLabelframe.Label', background=BG_DARK,
                    foreground=LIGHT_BLUE, font=(family, 10, 'bold'))

    # Standard button: bright-blue fill, white text. Matches Multi Meca Moves.
    style.configure('TButton',
                    background=BRIGHT_BLUE, foreground=WHITE,
                    bordercolor=BRIGHT_BLUE,
                    lightcolor=BRIGHT_BLUE, darkcolor=BRIGHT_BLUE,
                    focusthickness=1, focuscolor=ORANGE,
                    padding=(10, 4))
    style.map('TButton',
              background=[('active', NAVY),
                          ('disabled', BG_PANEL)],
              foreground=[('disabled', DISABLED)],
              bordercolor=[('active', NAVY),
                           ('disabled', BG_PANEL)])

    # Primary CTA — orange
    style.configure('Accent.TButton',
                    background=ORANGE, foreground=WHITE,
                    bordercolor=ORANGE,
                    lightcolor=ORANGE, darkcolor=ORANGE,
                    padding=(12, 4))
    style.map('Accent.TButton',
              background=[('active', ORANGE_RED),
                          ('disabled', BG_PANEL)],
              foreground=[('disabled', DISABLED)])

    # Inputs — dark field, light text, bright-blue focus ring.
    for cls in ('TEntry', 'TSpinbox', 'TCombobox'):
        style.configure(cls,
                        fieldbackground=BG_INPUT, background=BG_INPUT,
                        foreground=TEXT,
                        bordercolor=NAVY,
                        lightcolor=NAVY, darkcolor=NAVY,
                        insertcolor=LIGHT_BLUE,
                        arrowcolor=LIGHT_BLUE,
                        selectbackground=BRIGHT_BLUE,
                        selectforeground=WHITE,
                        padding=2)
        style.map(cls,
                  fieldbackground=[('disabled', BG_PANEL),
                                   ('readonly', BG_INPUT)],
                  foreground=[('disabled', MUTED)],
                  bordercolor=[('focus', BRIGHT_BLUE)],
                  lightcolor=[('focus', BRIGHT_BLUE)],
                  darkcolor=[('focus', BRIGHT_BLUE)])

    for cls in ('TCheckbutton', 'TRadiobutton'):
        style.configure(cls, background=BG_DARK, foreground=TEXT,
                        focuscolor=BRIGHT_BLUE,
                        indicatorbackground=BG_INPUT,
                        indicatorforeground=BRIGHT_BLUE)
        style.map(cls,
                  background=[('active', BG_DARK)],
                  foreground=[('disabled', DISABLED)],
                  indicatorcolor=[('selected', BRIGHT_BLUE),
                                  ('!selected', BG_INPUT)])

    # Scale (analog-output slider) — bright-blue trough/grip on dark.
    style.configure('Horizontal.TScale',
                    background=BG_DARK, troughcolor=BG_INPUT)

    style.configure('Vertical.TScrollbar',
                    background=BG_PANEL, troughcolor=BG_DARK,
                    bordercolor=NAVY, arrowcolor=LIGHT_BLUE,
                    lightcolor=NAVY, darkcolor=NAVY)
    style.configure('Horizontal.TScrollbar',
                    background=BG_PANEL, troughcolor=BG_DARK,
                    bordercolor=NAVY, arrowcolor=LIGHT_BLUE,
                    lightcolor=NAVY, darkcolor=NAVY)

    # non-ttk widgets: tk.Text (log panel) inherits the code-editor look.
    root.option_add('*Text.background', BG_CODE)
    root.option_add('*Text.foreground', TEXT)
    root.option_add('*Text.font', 'Consolas 10')
    root.option_add('*Text.insertBackground', LIGHT_BLUE)
    root.option_add('*Text.selectBackground', BRIGHT_BLUE)
    root.option_add('*Text.selectForeground', WHITE)
    root.option_add('*Text.borderWidth', 1)
    root.option_add('*Text.highlightThickness', 0)

    return family
