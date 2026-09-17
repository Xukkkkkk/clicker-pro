"""Visual tokens and ttk styles for Clicker Pro.

Keep colours and widget metrics in this module so the main window can focus on
layout and behaviour.  The styles target Tk's bundled ``clam`` theme, which is
stable on the Windows versions supported by this project.
"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk


COLORS = {
    "window": "#0D1017",
    "sidebar": "#11151E",
    "surface": "#161B25",
    "surface_hover": "#1D2431",
    "surface_pressed": "#242D3D",
    "input": "#10141C",
    "border": "#283140",
    "border_focus": "#6688FF",
    "text": "#F2F5FA",
    "text_secondary": "#AAB4C4",
    "text_muted": "#6F7C90",
    "accent": "#6688FF",
    "accent_hover": "#7C99FF",
    "accent_pressed": "#5376ED",
    "success": "#40C98A",
    "warning": "#F2B84B",
    "danger": "#F06C75",
    "danger_surface": "#3A2028",
    "selection": "#304887",
}


def configure_theme(root: tk.Misc) -> ttk.Style:
    """Apply the Clicker Pro dark theme and return the configured style."""
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(background=COLORS["window"])

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
        "Primary.TButton", background=COLORS["accent"], foreground="#FFFFFF",
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
        foreground="#FFB8BD", bordercolor="#59303A", lightcolor="#59303A",
        darkcolor="#59303A", padding=(18, 10), font=("Segoe UI Semibold", 10),
    )
    style.map("Danger.TButton", background=[("active", "#4A2832"),
                                             ("pressed", "#552D38")])

    # Sidebar navigation looks like a compact Windows settings rail.
    style.configure(
        "Nav.TButton", background=COLORS["sidebar"], foreground=COLORS["text_secondary"],
        borderwidth=0, relief="flat", padding=(14, 11), anchor="w",
        font=("Segoe UI Semibold", 10),
    )
    style.map("Nav.TButton", background=[("active", COLORS["surface_hover"])],
              foreground=[("active", COLORS["text"])])
    style.configure(
        "NavSelected.TButton", background=COLORS["surface_pressed"],
        foreground=COLORS["text"], borderwidth=0, relief="flat",
        padding=(14, 11), anchor="w", font=("Segoe UI Semibold", 10),
    )

    # Form controls
    input_common = dict(
        fieldbackground=COLORS["input"], background=COLORS["input"],
        foreground=COLORS["text"], bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"],
        insertcolor=COLORS["text"], selectbackground=COLORS["selection"],
        selectforeground="#FFFFFF", borderwidth=1, relief="flat",
        padding=(10, 8), font=("Segoe UI", 10),
    )
    style.configure("TEntry", **input_common)
    style.map("TEntry", bordercolor=[("focus", COLORS["border_focus"])],
              lightcolor=[("focus", COLORS["border_focus"])],
              darkcolor=[("focus", COLORS["border_focus"])])
    style.configure("TCombobox", **input_common, arrowcolor=COLORS["text_secondary"])
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", COLORS["input"])],
        foreground=[("readonly", COLORS["text"])],
        bordercolor=[("focus", COLORS["border_focus"])],
        arrowcolor=[("active", COLORS["text"])],
    )
    root.option_add("*TCombobox*Listbox.background", COLORS["surface_hover"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["selection"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

    style.configure(
        "TCheckbutton", background=COLORS["surface"], foreground=COLORS["text_secondary"],
        font=("Segoe UI", 9), padding=2,
    )
    style.map("TCheckbutton", background=[("active", COLORS["surface"])],
              foreground=[("active", COLORS["text"])],
              indicatorcolor=[("selected", COLORS["accent"]),
                              ("!selected", COLORS["input"])])
    style.configure(
        "TRadiobutton", background=COLORS["surface"],
        foreground=COLORS["text_secondary"], font=("Segoe UI", 9), padding=2,
    )
    style.map("TRadiobutton", background=[("active", COLORS["surface"])],
              foreground=[("active", COLORS["text"])],
              indicatorcolor=[("selected", COLORS["accent"]),
                              ("!selected", COLORS["input"])])

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
              foreground=[("selected", "#FFFFFF")])
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
    return style


def enable_dark_title_bar(root: tk.Misc) -> None:
    """Request the native dark Windows title bar; harmless on older builds."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        enabled = ctypes.c_int(1)
        # Attribute 20 is supported by current Windows 10/11; 19 is the
        # corresponding value on several older Windows 10 builds.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass
