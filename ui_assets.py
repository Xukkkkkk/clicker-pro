"""Native Windows symbol assets and compact tooltips for the desktop UI."""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from theme import bind_theme


SYMBOLS = {
    "click": "\ue8b0", "record": "\ue7c8", "vision": "\ue722",
    "hotkeys": "\ue765", "import": "\ue8b5", "export": "\ue74e",
    "clear": "\ue74d", "save": "\ue74e", "window": "\ue737",
}


def symbol_image(master, name: str, color: str, size: int = 18, *, existing=None):
    """Rasterize the system symbol font with supersampling for crisp icons."""
    scale = 3
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segmdl2.ttf"
    image = Image.new("RGBA", (size * scale, size * scale))
    font = (ImageFont.truetype(str(path), size * scale - 4)
            if path.is_file() else ImageFont.load_default(size=size * scale - 4))
    draw = ImageDraw.Draw(image)
    text = SYMBOLS[name] if path.is_file() else name[0].upper()
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((size * scale - right + left) / 2 - left,
               (size * scale - bottom + top) / 2 - top), text, font=font, fill=color)
    rendered = image.resize((size, size), Image.Resampling.LANCZOS)
    if existing is not None:
        existing.paste(rendered)
        return existing
    return ImageTk.PhotoImage(rendered, master=master)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.job = self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        for event in ("<Leave>", "<ButtonPress>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.job = self.widget.after(500, self.show)

    def show(self):
        self.job = None
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        bind_theme(tk.Label(self.window, text=self.text, highlightthickness=1,
                            padx=9, pady=5, font=("Microsoft YaHei UI", 9)),
                   bg="surface_hover", fg="text", highlightbackground="border").pack()
        self.window.update_idletasks()
        x = min(self.widget.winfo_rootx(),
                self.widget.winfo_screenwidth() - self.window.winfo_reqwidth() - 8)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window.geometry(f"+{max(0, x)}+{y}")

    def hide(self, _event=None):
        if self.job is not None:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None
