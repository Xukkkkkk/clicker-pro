"""Visual tokens and ttk styles for Clicker Pro.

Keep colours and widget metrics in this module so the main window can focus on
layout and behaviour.  The styles target Tk's bundled ``clam`` theme, which is
stable on the Windows versions supported by this project.
"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk


DARK_COLORS = {
    "window": "#121418",
    "sidebar": "#0d0f13",
    "surface": "#181b22",
    "surface_hover": "#222731",
    "surface_pressed": "#2b313e",
    "input": "#11141a",
    "border": "#272d3b",
    "border_focus": "#38d9a9",
    "text": "#f1f5f9",
    "text_secondary": "#94a3b8",
    "text_muted": "#64748b",
    "accent": "#10b981",
    "accent_hover": "#34d399",
    "accent_pressed": "#059669",
    "accent_text": "#ffffff",
    "success": "#34d399",
    "success_surface": "#064e3b",
    "warning": "#fbbf24",
    "warning_surface": "#451a03",
    "danger": "#f87171",
    "danger_surface": "#450a0a",
    "danger_hover": "#7f1d1d",
    "danger_pressed": "#991b1b",
    "danger_border": "#7f1d1d",
    "selection": "#064e3b",
    "sidebar_text": "#f8fafc",
    "sidebar_muted": "#94a3b8",
    "sidebar_hover": "#171c26",
    "sidebar_selected": "#1e293b",
    "sidebar_selected_hover": "#334155",
    "card_bg": "#181b22",
    "card_border": "#272d3b",
    "keycap_bg": "#222733",
    "keycap_border": "#3b4558",
    "keycap_text": "#38d9a9",
    "badge_bg": "#222733",
    "badge_text": "#94a3b8",
    "accent_subtle": "#064e3b",
}

LIGHT_COLORS = {
    "window": "#f8fafc",
    "sidebar": "#0f172a",
    "surface": "#ffffff",
    "surface_hover": "#f1f5f9",
    "surface_pressed": "#e2e8f0",
    "input": "#ffffff",
    "border": "#e2e8f0",
    "border_focus": "#059669",
    "text": "#0f172a",
    "text_secondary": "#475569",
    "text_muted": "#64748b",
    "accent": "#059669",
    "accent_hover": "#047857",
    "accent_pressed": "#065f46",
    "accent_text": "#ffffff",
    "success": "#059669",
    "success_surface": "#d1fae5",
    "warning": "#d97706",
    "warning_surface": "#fef3c7",
    "danger": "#dc2626",
    "danger_surface": "#fee2e2",
    "danger_hover": "#fecaca",
    "danger_pressed": "#fca5a5",
    "danger_border": "#fca5a5",
    "selection": "#d1fae5",
    "sidebar_text": "#f8fafc",
    "sidebar_muted": "#94a3b8",
    "sidebar_hover": "#1e293b",
    "sidebar_selected": "#334155",
    "sidebar_selected_hover": "#475569",
    "card_bg": "#ffffff",
    "card_border": "#e2e8f0",
    "keycap_bg": "#f1f5f9",
    "keycap_border": "#cbd5e1",
    "keycap_text": "#059669",
    "badge_bg": "#f1f5f9",
    "badge_text": "#475569",
    "accent_subtle": "#d1fae5",
}
THEMES = {"dark": DARK_COLORS, "light": LIGHT_COLORS}
DEFAULT_THEME = "dark"
COLORS = dict(THEMES[DEFAULT_THEME])
_active_theme = DEFAULT_THEME


def normalise_theme(name) -> str:
    return name if isinstance(name, str) and name in THEMES else DEFAULT_THEME


def bind_theme(widget: tk.Misc, **colours):
    """Retain semantic colours for classic Tk widgets during theme changes."""
    widget._theme_colours = colours
    widget.configure(**{option: COLORS[token] for option, token in colours.items()})
    return widget


def refresh_theme_widgets(root: tk.Misc) -> None:
    """Recolour existing widgets without rebuilding forms or losing state."""
    pending = [root]
    windows = []
    while pending:
        widget = pending.pop()
        pending.extend(widget.winfo_children())
        colours = getattr(widget, "_theme_colours", {})
        if colours:
            widget.configure(**{option: COLORS[token] for option, token in colours.items()})
        if isinstance(widget, ttk.Combobox):
            # option_add covers future dropdowns; already-created Tcl listboxes
            # need updating too. They are not Python child widgets.
            listbox = f"{widget}.popdown.f.l"
            if widget.tk.call("winfo", "exists", listbox):
                widget.tk.call(listbox, "configure", "-background", COLORS["surface_hover"],
                               "-foreground", COLORS["text"], "-selectbackground", COLORS["selection"],
                               "-selectforeground", COLORS["text"])
        if isinstance(widget, (tk.Tk, tk.Toplevel)) and not widget.overrideredirect():
            windows.append(widget)
    for window in windows:
        enable_dark_title_bar(window)


def configure_theme(root: tk.Misc, name: str = DEFAULT_THEME) -> ttk.Style:
    """Apply a workspace palette and return its shared styles."""
    global _active_theme
    _active_theme = normalise_theme(name)
    # Preserve references imported by main, tooltips and optional UI modules.
    COLORS.update(THEMES[_active_theme])
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(background=COLORS["window"])
    style.configure(
        ".", background=COLORS["surface"], foreground=COLORS["text"],
        bordercolor=COLORS["border"], lightcolor=COLORS["border"],
        darkcolor=COLORS["border"], troughcolor=COLORS["input"],
        selectbackground=COLORS["selection"], selectforeground=COLORS["text"],
    )

    # Containers
    style.configure("TFrame", background=COLORS["window"])
    style.configure("App.TFrame", background=COLORS["window"])
    style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
    style.configure(
        "Card.TFrame",
        background=COLORS["surface"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        borderwidth=1,
        relief="solid",
    )
    style.configure("CardInner.TFrame", background=COLORS["surface"])

    # Keycaps & Badges
    style.configure(
        "Keycap.TLabel",
        background=COLORS.get("keycap_bg", COLORS["surface_hover"]),
        foreground=COLORS.get("keycap_text", COLORS["accent"]),
        bordercolor=COLORS.get("keycap_border", COLORS["border"]),
        lightcolor=COLORS.get("keycap_border", COLORS["border"]),
        darkcolor=COLORS.get("keycap_border", COLORS["border"]),
        borderwidth=1,
        relief="solid",
        padding=(10, 5),
        font=("Segoe UI", 10, "bold"),
    )
    style.configure(
        "KeycapActive.TLabel",
        background=COLORS["selection"],
        foreground=COLORS["accent"],
        bordercolor=COLORS["border_focus"],
        lightcolor=COLORS["border_focus"],
        darkcolor=COLORS["border_focus"],
        borderwidth=1,
        relief="solid",
        padding=(10, 5),
        font=("Segoe UI", 10, "bold"),
    )
    style.configure(
        "SidebarKeycap.TLabel",
        background=COLORS["sidebar_selected"],
        foreground=COLORS["accent"],
        padding=(6, 2),
        font=("Segoe UI Semibold", 8),
    )
    style.configure(
        "ActionBadge.TLabel",
        background=COLORS.get("keycap_bg", COLORS["surface_hover"]),
        foreground=COLORS["accent"],
        padding=(6, 2),
        font=("Segoe UI Semibold", 8),
    )
    style.configure(
        "Preset.TButton",
        background=COLORS["surface_hover"],
        foreground=COLORS["text"],
        padding=(8, 4),
        font=("Microsoft YaHei UI", 8),
    )
    style.configure(
        "SectionTitle.TLabel",
        background=COLORS["surface"],
        foreground=COLORS["text"],
        font=("Microsoft YaHei UI", 10, "bold"),
    )
    style.configure(
        "CardHeader.TLabel",
        background=COLORS["surface"],
        foreground=COLORS["text"],
        font=("Microsoft YaHei UI", 11, "bold"),
    )

    # Labels
    style.configure(
        "TLabel", background=COLORS["window"], foreground=COLORS["text"],
        font=("Segoe UI", 10),
    )
    style.configure(
        "Title.TLabel", background=COLORS["window"], foreground=COLORS["text"],
        font=("Segoe UI Semibold", 20),
    )
    style.configure(
        "Subtitle.TLabel", background=COLORS["window"],
        foreground=COLORS["text_secondary"], font=("Segoe UI", 9),
    )
    style.configure(
        "CardTitle.TLabel", background=COLORS["surface"], foreground=COLORS["text"],
        font=("Segoe UI Semibold", 11),
    )
    style.configure(
        "CardText.TLabel", background=COLORS["surface"], foreground=COLORS["text"],
        font=("Segoe UI", 10),
    )
    style.configure(
        "Muted.TLabel", background=COLORS["surface"],
        foreground=COLORS["text_secondary"], font=("Segoe UI", 9),
    )
    style.configure(
        "Hint.TLabel", background=COLORS["surface"], foreground=COLORS["text_muted"],
        font=("Segoe UI", 9),
    )
    for name, colour in (
        ("Success", COLORS["success"]),
        ("Warning", COLORS["warning"]),
        ("Danger", COLORS["danger"]),
    ):
        style.configure(
            f"{name}.TLabel", background=COLORS["surface"], foreground=colour,
            font=("Segoe UI Semibold", 9),
        )

    # Buttons: a 36-40 px target height is comfortable with these paddings.
    style.configure(
        "TButton", background=COLORS["surface_hover"], foreground=COLORS["text"],
        bordercolor=COLORS["border"], lightcolor=COLORS["border"],
        darkcolor=COLORS["border"], borderwidth=1, relief="flat",
        padding=(14, 8), font=("Segoe UI Semibold", 9),
    )
    style.map(
        "TButton",
        background=[("pressed", COLORS["surface_pressed"]),
                    ("active", COLORS["surface_pressed"])],
        foreground=[("disabled", COLORS["text_muted"])],
        bordercolor=[("focus", COLORS["border_focus"])],
    )
    style.configure(
        "Primary.TButton", background=COLORS["accent"], foreground=COLORS["accent_text"],
        bordercolor=COLORS["accent"], lightcolor=COLORS["accent"],
        darkcolor=COLORS["accent"], borderwidth=1, relief="flat",
        padding=(18, 10), font=("Segoe UI Semibold", 10),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", COLORS["accent_pressed"]),
                    ("active", COLORS["accent_hover"]),
                    ("disabled", COLORS["surface_pressed"])],
        bordercolor=[("pressed", COLORS["accent_pressed"]),
                     ("active", COLORS["accent_hover"])],
        foreground=[("disabled", COLORS["text_muted"])],
    )
    style.configure(
        "Danger.TButton", background=COLORS["danger_surface"],
        foreground=COLORS["danger"], bordercolor=COLORS["danger_border"],
        lightcolor=COLORS["danger_border"], darkcolor=COLORS["danger_border"],
        padding=(12, 7), font=("Segoe UI Semibold", 9),
    )
    style.map("Danger.TButton", background=[("disabled", COLORS["surface_pressed"]),
                                             ("pressed", COLORS["danger_pressed"]),
                                             ("active", COLORS["danger_hover"])])

    # Sidebar navigation looks like a compact Windows settings rail.
    style.configure(
        "Nav.TButton", background=COLORS["sidebar"], foreground=COLORS["sidebar_muted"],
        borderwidth=0, relief="flat", padding=(14, 11), anchor="w",
        font=("Segoe UI Semibold", 10),
    )
    style.map("Nav.TButton", background=[("active", COLORS["sidebar_hover"])],
              foreground=[("active", COLORS["sidebar_text"])])
    style.configure(
        "NavSelected.TButton", background=COLORS["sidebar_selected"],
        foreground=COLORS["sidebar_text"], borderwidth=0, relief="flat",
        padding=(14, 11), anchor="w", font=("Segoe UI Semibold", 10),
    )

    # Form controls
    input_common = dict(
        fieldbackground=COLORS["input"], background=COLORS["input"],
        foreground=COLORS["text"], bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"],
        insertcolor=COLORS["text"], selectbackground=COLORS["selection"],
        selectforeground=COLORS["text"], borderwidth=1, relief="flat",
        padding=(9, 6), font=("Segoe UI", 10),
    )
    style.configure("TEntry", **input_common)
    style.map("TEntry", bordercolor=[("focus", COLORS["border_focus"])],
              lightcolor=[("focus", COLORS["border_focus"])],
              darkcolor=[("focus", COLORS["border_focus"])])
    style.configure("TCombobox", **input_common, arrowcolor=COLORS["text_secondary"])
    style.map(
        "TCombobox",
        fieldbackground=[("disabled", COLORS["surface_hover"]),
                         ("readonly", COLORS["input"])],
        background=[("disabled", COLORS["surface_hover"]),
                    ("active", COLORS["surface_pressed"]),
                    ("readonly", COLORS["input"])],
        foreground=[("disabled", COLORS["text_muted"]),
                    ("readonly", COLORS["text"])],
        bordercolor=[("focus", COLORS["border_focus"])],
        arrowcolor=[("active", COLORS["text"])],
    )
    root.option_add("*TCombobox*Listbox.background", COLORS["surface_hover"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["selection"])
    root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])

    style.configure(
        "TCheckbutton", background=COLORS["surface"], foreground=COLORS["text_secondary"],
        font=("Segoe UI", 9), padding=2,
    )
    style.map("TCheckbutton", background=[("active", COLORS["surface"])],
              foreground=[("disabled", COLORS["text_muted"]), ("active", COLORS["text"])],
              indicatorbackground=[("disabled", COLORS["surface_pressed"]),
                                   ("selected", COLORS["accent"]),
                                   ("!selected", COLORS["input"])],
              indicatorforeground=[("disabled", COLORS["text_muted"])])
    style.configure(
        "TRadiobutton", background=COLORS["surface"],
        foreground=COLORS["text_secondary"], font=("Segoe UI", 9), padding=2,
    )
    style.map("TRadiobutton", background=[("active", COLORS["surface"])],
              foreground=[("disabled", COLORS["text_muted"]), ("active", COLORS["text"])],
              indicatorbackground=[("disabled", COLORS["surface_pressed"]),
                                   ("selected", COLORS["accent"]),
                                   ("!selected", COLORS["input"])],
              indicatorforeground=[("disabled", COLORS["text_muted"])])
    for control in ("TCheckbutton", "TRadiobutton"):
        style.configure(control, indicatorbackground=COLORS["input"],
                        indicatorforeground=COLORS["accent_text"],
                        upperbordercolor=COLORS["border"],
                        lowerbordercolor=COLORS["border"],
                        lightcolor=COLORS["border"], darkcolor=COLORS["border"])

    # Recording/event table
    style.configure(
        "Treeview", background=COLORS["input"], fieldbackground=COLORS["input"],
        foreground=COLORS["text_secondary"], bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"],
        borderwidth=1, relief="flat", rowheight=32, font=("Segoe UI", 9),
    )
    style.configure(
        "Treeview.Heading", background=COLORS["surface_hover"],
        foreground=COLORS["text_secondary"], borderwidth=0, relief="flat",
        padding=(8, 8), font=("Segoe UI Semibold", 9),
    )
    style.map("Treeview", background=[("selected", COLORS["selection"])],
              foreground=[("selected", COLORS["accent"])])
    style.map("Treeview.Heading", background=[("active", COLORS["surface_pressed"])])

    style.configure(
        "TScrollbar", background=COLORS["surface_hover"],
        troughcolor=COLORS["input"], bordercolor=COLORS["border"],
        lightcolor=COLORS["surface_hover"], darkcolor=COLORS["surface_hover"],
        arrowcolor=COLORS["text_muted"], relief="flat", borderwidth=0,
    )
    style.map(
        "TScrollbar",
        background=[("active", COLORS["accent"]), ("pressed", COLORS["accent_pressed"])],
        arrowcolor=[("active", COLORS["text"])]
    )

    style.configure(
        "Horizontal.TProgressbar", background=COLORS["accent"],
        troughcolor=COLORS["input"], borderwidth=0, lightcolor=COLORS["accent"],
        darkcolor=COLORS["accent"], thickness=5,
    )
    style.configure("TSeparator", background=COLORS["border"],
                    bordercolor=COLORS["border"], lightcolor=COLORS["border"],
                    darkcolor=COLORS["border"])
    style.layout("Horizontal.TProgressbar", [("Horizontal.Progressbar.trough", {
        "sticky": "nswe", "children": [("Horizontal.Progressbar.pbar", {
            "side": "left", "sticky": "ns"})]})])
    style.configure("Horizontal.TProgressbar", thickness=4,
                    troughcolor=COLORS["border"], bordercolor=COLORS["border"],
                    background=COLORS["accent"])
    style.configure("Horizontal.TProgressbar", arrowsize=4, padding=0)
    style.map("NavSelected.TButton", background=[("active", COLORS["sidebar_selected_hover"])],
              foreground=[("active", COLORS["sidebar_text"])])
    style.map("TEntry", fieldbackground=[("disabled", COLORS["surface_hover"])],
              foreground=[("disabled", COLORS["text_muted"])])
    style.configure("TButton", padding=(12, 7))
    style.configure("Primary.TButton", padding=(16, 8))
    style.configure("Treeview.Heading", background=COLORS["surface_hover"], padding=(10, 9))
    style.configure("Treeview", rowheight=34)
    # Explicit CJK fonts avoid platform-dependent fallback and uneven baselines.
    for name in ("TLabel", "CardText.TLabel", "Muted.TLabel", "Hint.TLabel",
                 "TEntry", "TCombobox", "TCheckbutton", "TRadiobutton", "Treeview"):
        style.configure(name, font=("Microsoft YaHei UI", 9))
    for name in ("TButton", "Primary.TButton", "Danger.TButton", "Nav.TButton",
                 "NavSelected.TButton", "Treeview.Heading", "CardTitle.TLabel"):
        style.configure(name, font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
    style.configure("Subtitle.TLabel", font=("Microsoft YaHei UI", 9))
    return style


def enable_dark_title_bar(root: tk.Misc) -> None:
    """Match the native title bar to the selected workspace theme."""
    try:
        root.update_idletasks()
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [ctypes.c_void_p]
        get_parent.restype = ctypes.c_void_p
        hwnd = get_parent(root.winfo_id())
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_void_p, ctypes.c_uint]
        set_attribute.restype = ctypes.c_long
        enabled = ctypes.c_int(_active_theme == "dark")
        # Attribute 20 is supported by current Windows 10/11; 19 is the
        # corresponding value on several older Windows 10 builds.
        for attribute in (20, 19):
            result = set_attribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass
