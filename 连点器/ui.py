"""Modern Tkinter UI for the Windows auto-clicker.

The UI intentionally contains no platform-specific automation code.  Wire the
callbacks (``on_start``, ``on_stop``, ``on_record`` and ``on_play``) to the
click/recording engine from :mod:`main`.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


BG = "#151a22"
PANEL = "#1d2430"
PANEL_2 = "#232c3a"
TEXT = "#e9eef5"
MUTED = "#98a6b9"
ACCENT = "#5b8cff"
GREEN = "#39d98a"
RED = "#ff6b78"


class ClickerApp(tk.Tk):
    """Application window and controls for a feature-rich clicker."""

    def __init__(
        self,
        *,
        on_start: Optional[Callable[[dict], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        on_record: Optional[Callable[[], None]] = None,
        on_play: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.title("连点器 · Auto Clicker")
        self.geometry("760x560")
        self.minsize(700, 500)
        self.configure(bg=BG)
        self.on_start, self.on_stop = on_start, on_stop
        self.on_record, self.on_play = on_record, on_play
        self.running = False

        self._configure_style()
        self._build_vars()
        self._build_header()
        self._build_body()
        self._build_status()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Segoe UI Semibold", 16))
        style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=("Segoe UI Semibold", 11))
        style.configure("TButton", background=PANEL_2, foreground=TEXT, borderwidth=0,
                        padding=(14, 8), font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#303d52")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", borderwidth=0,
                        padding=(20, 10), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#739cff")])
        style.configure("Danger.TButton", background="#702d3a", foreground="#ffdce0", borderwidth=0,
                        padding=(20, 10), font=("Segoe UI Semibold", 10))
        style.map("Danger.TButton", background=[("active", "#8d3848")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(18, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", PANEL_2)], foreground=[("selected", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL_2, foreground=TEXT, insertcolor=TEXT,
                        borderwidth=0, padding=7)
        style.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2, foreground=TEXT,
                        arrowcolor=MUTED, borderwidth=0, padding=6)
        style.configure("Treeview", background=PANEL_2, fieldbackground=PANEL_2, foreground=TEXT,
                        rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                        font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", ACCENT)])
        style.configure("Horizontal.TProgressbar", troughcolor=PANEL_2, background=ACCENT, borderwidth=0)

    def _build_vars(self) -> None:
        self.interval = tk.StringVar(value="100")
        self.count = tk.StringVar(value="0")
        self.mode = tk.StringVar(value="无限")
        self.button = tk.StringVar(value="左键")
        self.click_type = tk.StringVar(value="单击")
        self.start_hotkey = tk.StringVar(value="F6")
        self.stop_hotkey = tk.StringVar(value="F7")
        self.status = tk.StringVar(value="就绪 · 等待开始")
        self.counter = tk.StringVar(value="0 次点击")

    def _build_header(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=26, pady=(22, 12))
        ttk.Label(head, text="连点器", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  Auto Clicker", style="Muted.TLabel").pack(side="left", pady=(4, 0))
        self.indicator = tk.Canvas(head, width=12, height=12, bg=BG, highlightthickness=0)
        self.indicator.pack(side="right", padx=(8, 0), pady=4)
        self.indicator.create_oval(2, 2, 10, 10, fill=MUTED, outline="")
        ttk.Label(head, text="未运行", style="Muted.TLabel").pack(side="right", pady=(3, 0))

    def _build_body(self) -> None:
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        self._build_click_tab()
        self._build_record_tab()
        self._build_hotkey_tab()

    def _panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        frame.pack(fill="x", pady=(0, 12))
        return frame

    def _build_click_tab(self) -> None:
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="  连点设置  ")
        p = self._panel(tab)
        ttk.Label(p, text="点击参数", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))
        fields = [("间隔 (毫秒)", self.interval), ("点击次数 (0=无限)", self.count)]
        for i, (label, var) in enumerate(fields):
            ttk.Label(p, text=label, style="Panel.TLabel").grid(row=1, column=i * 2, sticky="w", padx=(0, 8))
            ttk.Entry(p, textvariable=var, width=14).grid(row=1, column=i * 2 + 1, sticky="w", padx=(0, 24))
        ttk.Label(p, text="运行模式", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(15, 0))
        ttk.Combobox(p, textvariable=self.mode, values=("无限", "固定次数"), state="readonly", width=12).grid(row=2, column=1, sticky="w", pady=(15, 0))
        ttk.Label(p, text="鼠标按键", style="Panel.TLabel").grid(row=2, column=2, sticky="w", pady=(15, 0))
        ttk.Combobox(p, textvariable=self.button, values=("左键", "右键", "中键"), state="readonly", width=12).grid(row=2, column=3, sticky="w", pady=(15, 0))
        ttk.Label(p, text="点击类型", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(15, 0))
        ttk.Combobox(p, textvariable=self.click_type, values=("单击", "双击"), state="readonly", width=12).grid(row=3, column=1, sticky="w", pady=(15, 0))
        self.random_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(p, text="随机化间隔 ±20%", variable=self.random_var).grid(row=3, column=2, columnspan=2, sticky="w", pady=(15, 0))

        control = ttk.Frame(tab, style="Panel.TFrame", padding=18)
        control.pack(fill="x", pady=(0, 12))
        self.start_btn = ttk.Button(control, text="▶  开始连点", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(control, text="■  停止", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(10, 0))
        ttk.Label(control, textvariable=self.counter, style="Panel.TLabel").pack(side="right", pady=8)

    def _build_record_tab(self) -> None:
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="  录制动作  ")
        p = self._panel(tab)
        ttk.Label(p, text="动作序列", style="Section.TLabel").pack(anchor="w")
        cols = ("no", "action", "position", "delay")
        self.events = ttk.Treeview(p, columns=cols, show="headings", height=8)
        for c, h, w in (("no", "#", 45), ("action", "动作", 140), ("position", "坐标", 180), ("delay", "延迟", 100)):
            self.events.heading(c, text=h); self.events.column(c, width=w, anchor="center")
        self.events.pack(fill="both", expand=True, pady=(12, 10))
        bar = ttk.Frame(p, style="Panel.TFrame"); bar.pack(fill="x")
        ttk.Button(bar, text="●  开始录制", command=self._record).pack(side="left")
        ttk.Button(bar, text="▶  播放序列", command=self._play).pack(side="left", padx=8)
        ttk.Button(bar, text="清空", command=lambda: self.events.delete(*self.events.get_children())).pack(side="right")

    def _build_hotkey_tab(self) -> None:
        tab = ttk.Frame(self.tabs)
        self.tabs.add(tab, text="  快捷键  ")
        p = self._panel(tab)
        ttk.Label(p, text="全局快捷键", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))
        ttk.Label(p, text="开始 / 暂停", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Entry(p, textvariable=self.start_hotkey, width=16).grid(row=1, column=1, padx=15)
        ttk.Label(p, text="按下后输入组合键", style="Muted.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Label(p, text="停止", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Entry(p, textvariable=self.stop_hotkey, width=16).grid(row=2, column=1, padx=15)
        ttk.Label(p, text="建议使用 F6 / F7 或 Ctrl+Alt 组合", style="Muted.TLabel").grid(row=2, column=2, sticky="w")

    def _start(self) -> None:
        self.set_running(True)
        if self.on_start:
            self.on_start({"interval": self.interval.get(), "count": self.count.get(), "button": self.button.get(), "click_type": self.click_type.get(), "random": self.random_var.get()})

    def _stop(self) -> None:
        self.set_running(False)
        if self.on_stop: self.on_stop()

    def _record(self) -> None:
        if self.on_record: self.on_record()

    def _play(self) -> None:
        if self.on_play: self.on_play()

    def set_running(self, running: bool) -> None:
        self.running = running
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.status.set("运行中 · 连点已启动" if running else "已停止 · 就绪")
        self.indicator.delete("all")
        self.indicator.create_oval(2, 2, 10, 10, fill=GREEN if running else MUTED, outline="")

    def set_counter(self, count: int) -> None:
        self.counter.set(f"{count:,} 次点击")

    def add_event(self, action: str, position: str, delay: str) -> None:
        no = len(self.events.get_children()) + 1
        self.events.insert("", "end", values=(no, action, position, delay))

    def set_status(self, message: str) -> None:
        self.status.set(message)

    def _build_status(self) -> None:
        bar = tk.Frame(self, bg="#10141b", height=30)
        bar.pack(fill="x", side="bottom")
        tk.Label(bar, textvariable=self.status, bg="#10141b", fg=MUTED, font=("Segoe UI", 9), anchor="w").pack(side="left", padx=20, pady=6)
        tk.Label(bar, text="Windows · v1.0", bg="#10141b", fg="#627086", font=("Segoe UI", 9)).pack(side="right", padx=20)


if __name__ == "__main__":
    ClickerApp().mainloop()
