"""Clicker Pro - a polished Windows auto-clicker and mouse recorder."""
from __future__ import annotations

import ctypes
import json
import math
import os
import random
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from theme import COLORS, configure_theme, enable_dark_title_bar

try:
    from pynput import keyboard, mouse
except ImportError:  # The UI still opens and explains how to install it.
    keyboard = mouse = None

try:
    from core import send_click, send_mouse_down, send_mouse_up
except Exception:  # pragma: no cover - useful on non-Windows development hosts
    send_click = send_mouse_down = send_mouse_up = None

try:
    from vision_engine import VisionEngine, TemplateSpec, TemplateAction, execute_template_actions
except Exception:  # Optional until the vision dependencies are installed.
    VisionEngine = TemplateSpec = TemplateAction = execute_template_actions = None

try:
    from clipboard_image import paste_to_directory
except Exception:  # Clipboard support remains optional until first paste.
    paste_to_directory = None


APP_DIR = Path(__file__).resolve().parent
try:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(APP_DIR))) / "ClickerPro"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = APP_DIR

CONFIG_FILE = DATA_DIR / "config.json"
RECORD_FILE = DATA_DIR / "clicker_record.json"
CLIPBOARD_TEMPLATE_DIR = DATA_DIR / "vision_clipboard"
LEGACY_CONFIG_FILE = APP_DIR / "clicker_config.json"
LEGACY_RECORD_FILE = APP_DIR / "clicker_record.json"

HOTKEY_DEFAULTS = {"toggle": "<f6>", "record": "<f7>", "stop": "<f8>"}
HOTKEY_LABELS = {
    "toggle": "开始 / 停止连点",
    "record": "开始 / 停止录制",
    "stop": "停止全部任务",
}


def enable_process_dpi_awareness() -> None:
    """Use physical screen coordinates on scaled and mixed-DPI monitors.

    Screen capture and injected mouse input must share the same coordinate
    space.  This has to run before Tk creates the first window; otherwise
    Windows may virtualise the UI coordinates while MSS returns physical
    pixels, causing image matches to click the wrong place at 125%/150% scale.
    """
    if os.name != "nt":
        return
    try:
        # Windows 10 Creators Update+: Per Monitor DPI Aware V2.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        # Windows 8.1+: Per Monitor DPI Aware.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) in (0, -2147024891):
            return
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _button_name(value: Any) -> str:
    value = str(value or "left").lower().replace("button.", "").replace("<", "").replace(">", "")
    return value if value in {"left", "right", "middle"} else "left"


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class ClickerApp:
    """Main window, worker coordination and persistence for Clicker Pro."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Clicker Pro")
        # Keep a comfortable default while the recognition editor provides its
        # own scrolling area for the larger preview and action plan.
        self.root.geometry("1000x680")
        self.root.minsize(900, 640)
        self.style = configure_theme(root)
        enable_dark_title_bar(root)
        self.configure_app_styles()

        self.closing = False
        self.page_frames: dict[str, ttk.Frame] = {}
        self.page_meta = {
            "click": ("连点控制", "调整点击频率、鼠标位置和运行方式"),
            "record": ("录制与回放", "把鼠标操作保存下来，随时按原节奏重播"),
            "vision": ("图片识别", "发现屏幕上的目标图片后自动点击"),
            "hotkeys": ("快捷键", "点击输入框后直接按键即可完成设置"),
        }
        self.current_page = "click"

        # Independent stop signals keep click, replay and recording states
        # from accidentally interrupting one another.
        self.click_stop_event = threading.Event()
        self.play_stop_event = threading.Event()
        self.click_thread: Optional[threading.Thread] = None
        self.play_thread: Optional[threading.Thread] = None
        self.click_run_id = 0
        self.play_run_id = 0
        self.running = False
        self.playing = False
        self.recording = False
        self.record_listener = None
        self.record_start = 0.0
        self.record_include_moves = True
        self.record_last_move_time = 0.0
        self.record_last_position: Optional[tuple[int, int]] = None
        self.record_session_id = 0
        self.events: list[dict[str, Any]] = []
        self.event_lock = threading.Lock()

        self.hotkey_listener = None
        self.hotkey_capture_target: Optional[str] = None
        self.hotkey_capture_previous = ""
        self.hotkey_ignore_until = 0.0
        self.hotkey_specs = dict(HOTKEY_DEFAULTS)

        self.vision_templates: list[dict[str, Any]] = []
        self.vision_engine = None
        self.vision_running = False
        # Monotonically increasing token used to invalidate callbacks from a
        # scanner that is still finishing after a stop/restart request.
        self.vision_generation = 0
        self.vision_template_counter = 0
        self.vision_preview_image = None
        # Keep the decoded source image separately from the PhotoImage shown
        # by Tk.  The source is immutable and lets us redraw a crisp preview
        # whenever the editor card is resized without reopening the file.
        self.vision_preview_source = None
        self.vision_preview_fit = True
        self.vision_preview_zoom = 1.0
        self._vision_preview_resize_job = None
        self.vision_paste_busy = False
        self._vision_paste_bindings = []

        self.build_ui()
        # Bind at the toplevel so Ctrl+V works when focus is on the Treeview,
        # a button, or an otherwise non-editable part of the vision page.
        # Entry widgets are explicitly allowed through in _on_vision_paste.
        for sequence in ("<Control-KeyPress-v>", "<Control-KeyPress-V>"):
            binding_id = self.root.bind(sequence, self._on_vision_paste, add="+")
            self._vision_paste_bindings.append((sequence, binding_id))
        self.load_config()
        self.load_recording()
        self.show_page("click")
        self.start_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI
    def configure_app_styles(self):
        self.style.configure("Page.TFrame", background=COLORS["window"])
        self.style.configure("SidebarText.TLabel", background=COLORS["sidebar"], foreground=COLORS["text_secondary"], font=("Segoe UI", 9))
        self.style.configure("Brand.TLabel", background=COLORS["sidebar"], foreground=COLORS["text"], font=("Segoe UI Semibold", 16))
        self.style.configure("HeroTitle.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Segoe UI Semibold", 15))
        self.style.configure("MetricTitle.TLabel", background=COLORS["surface"], foreground=COLORS["text_muted"], font=("Segoe UI", 8))
        self.style.configure("MetricValue.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Segoe UI Semibold", 16))
        self.style.configure("Capture.TEntry", fieldbackground=COLORS["selection"], background=COLORS["selection"], foreground="#FFFFFF", bordercolor=COLORS["accent"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"], padding=(10, 8), font=("Segoe UI Semibold", 10))
        self.style.configure("Vision.TEntry", fieldbackground=COLORS["input"], background=COLORS["input"], foreground=COLORS["text"], bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"], insertcolor=COLORS["text"], padding=(8, 5), font=("Segoe UI", 9))
        self.style.configure(
            "Vision.TLabelframe", background=COLORS["surface"],
            foreground=COLORS["text"], bordercolor=COLORS["border"],
            lightcolor=COLORS["border"], darkcolor=COLORS["border"],
            borderwidth=1, relief="solid",
        )
        self.style.configure(
            "Vision.TLabelframe.Label", background=COLORS["surface"],
            foreground=COLORS["text_secondary"], font=("Segoe UI Semibold", 9),
        )
        self.style.configure("Chip.TLabel", background=COLORS["surface_hover"], foreground=COLORS["text_secondary"], padding=(9, 5), font=("Segoe UI Semibold", 9))
        self.style.configure("Count.TLabel", background=COLORS["surface"], foreground=COLORS["accent"], font=("Segoe UI Semibold", 9))
        self.style.configure("VisionLog.TLabel", background=COLORS["surface"], foreground=COLORS["text_secondary"], font=("Segoe UI", 9))
        # The vision toolbar has four actions and must remain usable on a
        # 1024px display.  Keep these secondary buttons compact without
        # changing the comfortable sizing used by the rest of the app.
        self.style.configure("Compact.TButton", padding=(10, 6), font=("Segoe UI", 9))

    def build_ui(self):
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=214)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = ttk.Frame(shell, style="Page.TFrame")
        self.content.pack(side="left", fill="both", expand=True)
        self.build_sidebar()
        self.build_header()
        self.page_host = ttk.Frame(self.content, style="Page.TFrame")
        # Keep a little more horizontal room for the card layouts on compact
        # laptop displays; the cards already provide their own inner padding.
        self.page_host.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        self.build_click_page()
        self.build_record_page()
        self.build_vision_page()
        self.build_hotkey_page()
        self.build_footer()

    def build_sidebar(self):
        brand = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        brand.pack(fill="x", padx=20, pady=(26, 30))
        tk.Label(brand, text="●", bg=COLORS["accent"], fg="#FFFFFF", width=2, font=("Segoe UI", 14, "bold")).pack(side="left", padx=(0, 10))
        brand_text = ttk.Frame(brand, style="Sidebar.TFrame")
        brand_text.pack(side="left")
        ttk.Label(brand_text, text="Clicker Pro", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand_text, text="WINDOWS TOOL", style="SidebarText.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(self.sidebar, text="工作区", style="SidebarText.TLabel").pack(anchor="w", padx=22, pady=(0, 8))
        self.nav_buttons: dict[str, ttk.Button] = {}
        for name, label in (("click", "◉   连点控制"), ("record", "◌   录制与回放"), ("vision", "▣   图片识别"), ("hotkeys", "⌨   快捷键")):
            button = ttk.Button(self.sidebar, text=label, style="Nav.TButton", command=lambda page=name: self.show_page(page))
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[name] = button
        spacer = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        spacer.pack(fill="both", expand=True)
        tip = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        tip.pack(fill="x", padx=20, pady=(0, 24))
        ttk.Separator(tip).pack(fill="x", pady=(0, 14))
        self.hotkey_tip_var = tk.StringVar(value=self.hotkey_tip_text())
        ttk.Label(tip, textvariable=self.hotkey_tip_var, style="SidebarText.TLabel", justify="left").pack(anchor="w")
        ttk.Label(tip, text="v1.2  ·  Ready", style="SidebarText.TLabel").pack(anchor="w", pady=(18, 0))

    def build_header(self):
        header = ttk.Frame(self.content, style="Page.TFrame")
        header.pack(fill="x", padx=28, pady=(24, 12))
        left = ttk.Frame(header, style="Page.TFrame")
        left.pack(side="left", fill="x", expand=True)
        self.page_title_var = tk.StringVar(value="连点控制")
        self.page_subtitle_var = tk.StringVar(value="调整点击频率、鼠标位置和运行方式")
        ttk.Label(left, textvariable=self.page_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, textvariable=self.page_subtitle_var, style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))
        right = ttk.Frame(header, style="Page.TFrame")
        right.pack(side="right")
        self.status_pill = tk.Label(right, text="●  准备就绪", bg=COLORS["surface_hover"], fg=COLORS["text_secondary"], padx=12, pady=6, font=("Segoe UI Semibold", 9))
        self.status_pill.pack(side="left", padx=(0, 10))
        self.header_stop_button = ttk.Button(right, text="停止全部", style="Danger.TButton", command=self.stop_all)
        self.header_stop_button.pack(side="left")

    def build_click_page(self):
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["click"] = page
        hero = ttk.Frame(page, style="Card.TFrame", padding=(20, 16))
        hero.pack(fill="x", pady=(0, 14))
        hero_left = ttk.Frame(hero, style="CardInner.TFrame")
        hero_left.pack(side="left", fill="x", expand=True)
        ttk.Label(hero_left, text="让重复点击变得简单", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(hero_left, text="设置一次，按下快捷键即可开始。运行期间可随时暂停。", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        self.start_button = ttk.Button(hero, text="▶  开始连点", style="Primary.TButton", command=self.toggle_clicking)
        self.start_button.pack(side="right", padx=(18, 0))

        body = ttk.Frame(page, style="Page.TFrame")
        body.pack(fill="x")
        # Let the parameter card and the position card use their natural
        # widths; a shared uniform group wastes space on smaller displays.
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        params = ttk.Frame(body, style="Card.TFrame", padding=20)
        params.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        target = ttk.Frame(body, style="Card.TFrame", padding=20)
        target.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(params, text="点击参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 18))
        params.columnconfigure(0, weight=1)
        params.columnconfigure(1, weight=1)

        self.interval_var = tk.StringVar(value="100")
        self.count_var = tk.StringVar(value="0")
        self.click_button_var = tk.StringVar(value="左键")
        self.click_mode_var = tk.StringVar(value="单击")
        self.random_var = tk.BooleanVar(value=False)
        self.delay_var = tk.StringVar(value="0")
        ttk.Label(params, text="点击间隔", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="点击次数", style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=14, pady=(0, 6))
        interval_box = ttk.Frame(params, style="CardInner.TFrame")
        interval_box.grid(row=2, column=0, sticky="ew", pady=(0, 16))
        interval_box.columnconfigure(0, weight=1)
        ttk.Entry(interval_box, textvariable=self.interval_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(interval_box, text="ms", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        count_box = ttk.Frame(params, style="CardInner.TFrame")
        count_box.grid(row=2, column=1, sticky="ew", padx=(14, 0), pady=(0, 16))
        count_box.columnconfigure(0, weight=1)
        ttk.Entry(count_box, textvariable=self.count_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(count_box, text="0 = 无限", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(params, text="鼠标按键", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="点击方式", style="Muted.TLabel").grid(row=3, column=1, sticky="w", padx=14, pady=(0, 6))
        ttk.Combobox(params, textvariable=self.click_button_var, values=["左键", "右键", "中键"], state="readonly").grid(row=4, column=0, sticky="ew", pady=(0, 16))
        ttk.Combobox(params, textvariable=self.click_mode_var, values=["单击", "双击"], state="readonly").grid(row=4, column=1, sticky="ew", padx=(14, 0), pady=(0, 16))
        ttk.Label(params, text="开始前延时", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="随机间隔", style="Muted.TLabel").grid(row=5, column=1, sticky="w", padx=14, pady=(0, 6))
        delay_box = ttk.Frame(params, style="CardInner.TFrame")
        delay_box.grid(row=6, column=0, sticky="ew")
        delay_box.columnconfigure(0, weight=1)
        ttk.Entry(delay_box, textvariable=self.delay_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(delay_box, text="秒", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Checkbutton(params, text="间隔随机 ±20%", variable=self.random_var).grid(row=6, column=1, sticky="w", padx=(14, 0))

        ttk.Label(target, text="点击位置", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 18))
        self.position_var = tk.StringVar(value="跟随鼠标当前位置")
        ttk.Radiobutton(target, text="跟随鼠标当前位置", variable=self.position_var, value="跟随鼠标当前位置", command=self.update_position_state).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Radiobutton(target, text="固定坐标", variable=self.position_var, value="固定坐标", command=self.update_position_state).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.x_var, self.y_var = tk.StringVar(value="0"), tk.StringVar(value="0")
        xy = ttk.Frame(target, style="CardInner.TFrame")
        xy.grid(row=3, column=0, columnspan=2, sticky="ew")
        xy.columnconfigure(1, weight=1)
        xy.columnconfigure(3, weight=1)
        ttk.Label(xy, text="X", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 7))
        self.x_entry = ttk.Entry(xy, textvariable=self.x_var, width=8)
        self.x_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(xy, text="Y", style="Muted.TLabel").grid(row=0, column=2, padx=(0, 7))
        self.y_entry = ttk.Entry(xy, textvariable=self.y_var, width=8)
        self.y_entry.grid(row=0, column=3, sticky="ew")
        ttk.Button(target, text="⌖  获取当前鼠标坐标", command=self.capture_position).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Label(target, text="固定坐标适合重复点击同一个按钮。", style="Hint.TLabel", wraplength=250).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        target.columnconfigure(0, weight=1)
        target.columnconfigure(1, weight=1)

        stats = ttk.Frame(page, style="Card.TFrame", padding=(20, 15))
        stats.pack(fill="x", pady=(14, 0))
        self.stat_vars: dict[str, tk.StringVar] = {}
        for i, (key, title, value) in enumerate((("clicks", "本次点击", "0"), ("elapsed", "运行时长", "00:00"), ("rate", "当前间隔", "100 ms"))):
            if i:
                ttk.Separator(stats, orient="vertical").grid(row=0, column=i * 2 - 1, rowspan=2, sticky="ns", padx=18)
            self.stat_vars[key] = tk.StringVar(value=value)
            ttk.Label(stats, text=title, style="MetricTitle.TLabel").grid(row=0, column=i * 2, sticky="w")
            ttk.Label(stats, textvariable=self.stat_vars[key], style="MetricValue.TLabel").grid(row=1, column=i * 2, sticky="w", pady=(3, 0))
            stats.columnconfigure(i * 2, weight=1)
        self.progress = ttk.Progressbar(stats, style="Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.progress.grid(row=0, column=6, rowspan=2, sticky="e", padx=(12, 0))
        ttk.Label(page, text="提示：你可以在其他窗口工作，F6 会在后台切换连点状态。", style="Hint.TLabel").pack(anchor="w", pady=(12, 0))
        self.update_position_state()

    def build_record_page(self):
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["record"] = page
        toolbar = ttk.Frame(page, style="Card.TFrame", padding=18)
        toolbar.pack(fill="x", pady=(0, 14))
        controls = ttk.Frame(toolbar, style="CardInner.TFrame")
        controls.pack(fill="x")
        self.record_button = ttk.Button(controls, text="●  开始录制", style="Primary.TButton", command=self.toggle_recording)
        self.record_button.pack(side="left")
        self.play_button = ttk.Button(controls, text="▶  回放动作", command=self.play_recording)
        self.play_button.pack(side="left", padx=(10, 0))
        self.record_stop_button = ttk.Button(controls, text="■  停止", style="Danger.TButton", command=self.stop_all)
        self.record_stop_button.pack(side="left", padx=(10, 0))
        ttk.Button(controls, text="清空记录", command=self.clear_recording).pack(side="left", padx=(10, 0))
        options = ttk.Frame(toolbar, style="CardInner.TFrame")
        options.pack(fill="x", pady=(14, 0))
        self.record_include_moves_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="记录鼠标移动", variable=self.record_include_moves_var).pack(side="left", padx=(0, 14))
        ttk.Label(options, text="速度", style="Muted.TLabel").pack(side="left")
        self.speed_var = tk.StringVar(value="1.0x")
        ttk.Combobox(options, textvariable=self.speed_var, values=["0.5x", "1.0x", "1.5x", "2.0x", "4.0x"], state="readonly", width=7).pack(side="left", padx=(6, 14))
        ttk.Label(options, text="循环", style="Muted.TLabel").pack(side="left")
        self.loop_var = tk.StringVar(value="1")
        ttk.Entry(options, textvariable=self.loop_var, width=6).pack(side="left", padx=(6, 0))
        ttk.Label(options, text="0 = 无限", style="Hint.TLabel").pack(side="left", padx=(7, 0))
        self.record_status_var = tk.StringVar(value="尚未录制动作")
        status_card = ttk.Frame(page, style="Card.TFrame", padding=(18, 12))
        status_card.pack(fill="x", pady=(0, 14))
        ttk.Label(status_card, textvariable=self.record_status_var, style="Count.TLabel").pack(side="left")
        ttk.Label(status_card, text="录制时请切换到目标窗口操作，F7 可快速开始/停止。", style="Hint.TLabel").pack(side="right")
        table_card = ttk.Frame(page, style="Card.TFrame", padding=14)
        table_card.pack(fill="both", expand=True)
        table_frame = ttk.Frame(table_card, style="CardInner.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("index", "time", "action", "position", "button")
        self.event_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"index": "#", "time": "时间", "action": "动作", "position": "坐标", "button": "按键"}
        widths = {"index": 55, "time": 110, "action": 110, "position": 180, "button": 100}
        for col in columns:
            self.event_tree.heading(col, text=headings[col])
            self.event_tree.column(col, width=widths[col], anchor="w", stretch=col in {"action", "position"})
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.event_tree.yview)
        self.event_tree.configure(yscrollcommand=scroll.set)
        self.event_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def build_vision_page(self):
        """Build the multi-template screen recognition workspace."""
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["vision"] = page

        intro = ttk.Frame(page, style="Card.TFrame", padding=20)
        intro.pack(fill="x", pady=(0, 14))
        intro_left = ttk.Frame(intro, style="CardInner.TFrame")
        intro_left.pack(side="left", fill="x", expand=True)
        ttk.Label(intro_left, text="看见目标，自动点击", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(intro_left, text="添加一张或多张目标图片，程序会持续扫描屏幕并在匹配中心执行自定义动作。支持 Ctrl+V 直接粘贴截图。", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
        self.vision_start_button = ttk.Button(intro, text="▶  开始识别", style="Primary.TButton", command=self.toggle_vision)
        self.vision_start_button.pack(side="right")
        self.vision_paste_button = ttk.Button(intro, text="粘贴图片", style="Compact.TButton", command=self.paste_vision_image)
        self.vision_paste_button.pack(side="right", padx=(0, 8))

        toolbar = ttk.Frame(page, style="Card.TFrame", padding=(18, 13))
        toolbar.pack(fill="x", pady=(0, 14))
        ttk.Button(toolbar, text="＋  添加图片", style="Primary.TButton", command=self.add_vision_images).pack(side="left")
        ttk.Button(toolbar, text="⌁  测试一次", style="Compact.TButton", command=self.scan_vision_once).pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="删除选中", style="Compact.TButton", command=self.remove_vision_template).pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="清空全部", style="Compact.TButton", command=self.clear_vision_templates).pack(side="left", padx=(10, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=18)
        ttk.Label(toolbar, text="扫描间隔", style="Muted.TLabel").pack(side="left")
        self.vision_scan_var = tk.StringVar(value="0.20")
        ttk.Combobox(toolbar, textvariable=self.vision_scan_var, values=["0.05", "0.10", "0.20", "0.35", "0.50", "1.00"], state="readonly", width=7).pack(side="left", padx=(8, 4))
        ttk.Label(toolbar, text="秒", style="Muted.TLabel").pack(side="left")
        self.vision_global_status = tk.StringVar(value="待机")
        ttk.Label(toolbar, textvariable=self.vision_global_status, style="Count.TLabel").pack(side="right")
        # Keep the detailed message in a separate compact status bar.  It is
        # useful when a scan finds nothing or a template cannot be loaded,
        # while the toolbar status remains a short at-a-glance indicator.
        self.vision_log_var = tk.StringVar(value="识别日志会显示在这里")
        log_bar = ttk.Frame(page, style="Card.TFrame", padding=(14, 8))
        log_bar.pack(fill="x", pady=(0, 14))
        ttk.Label(log_bar, text="识别日志", style="Muted.TLabel").pack(side="left")
        ttk.Label(log_bar, textvariable=self.vision_log_var, style="VisionLog.TLabel", anchor="w").pack(
            side="left", fill="x", expand=True, padx=(10, 0)
        )

        body = ttk.Frame(page, style="Page.TFrame")
        body.pack(fill="both", expand=True)
        # Give the editor a little more room than the list.  The old 3:2
        # split left only about 250 px for the preview at the default window
        # size, which made most screenshots unreadable.  Give the editor the
        # larger share while keeping the target table compact and scrollable.
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=6)
        body.rowconfigure(0, weight=1)

        list_card = ttk.Frame(body, style="Card.TFrame", padding=14)
        list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(list_card, text="识别目标", style="CardTitle.TLabel").pack(anchor="w", padx=6, pady=(4, 12))
        list_inner = ttk.Frame(list_card, style="CardInner.TFrame")
        list_inner.pack(fill="both", expand=True)
        columns = ("name", "threshold", "cooldown", "button", "enabled")
        self.vision_tree = ttk.Treeview(list_inner, columns=columns, show="headings", selectmode="browse", height=6)
        labels = {"name": "图片", "threshold": "阈值", "cooldown": "冷却", "button": "按键", "enabled": "状态"}
        # Keep the list compact enough to leave a useful inline preview on a
        # 1000 px window.  Cooldown and primary-button values remain in the
        # row data and are shown in the editor after selecting a target; they
        # do not need to consume a permanent column beside the preview.
        widths = {"name": 145, "threshold": 56, "cooldown": 62, "button": 56, "enabled": 58}
        for col in columns:
            self.vision_tree.heading(col, text=labels[col])
            self.vision_tree.column(col, width=widths[col], anchor="w", stretch=col == "name")
        self.vision_tree.configure(displaycolumns=("name", "threshold", "enabled"))
        vision_scroll = ttk.Scrollbar(list_inner, orient="vertical", command=self.vision_tree.yview)
        self.vision_tree.configure(yscrollcommand=vision_scroll.set)
        vision_scroll.pack(side="right", fill="y")
        self.vision_tree.pack(side="left", fill="both", expand=True)
        self.vision_tree.bind("<<TreeviewSelect>>", self.on_vision_select)
        # Keep row actions close to the target.  A right-click first selects
        # the row under the pointer, while Delete remains local to this
        # Treeview so it cannot remove a template while the user is editing a
        # numeric field on the right.
        self.vision_tree.bind("<Button-3>", self._show_vision_context_menu)
        self.vision_tree.bind("<Delete>", self._delete_selected_vision_template)
        self.vision_context_menu = tk.Menu(
            self.vision_tree,
            tearoff=False,
            background=COLORS["surface_hover"],
            foreground=COLORS["text"],
            activebackground=COLORS["selection"],
            activeforeground="#FFFFFF",
            disabledforeground=COLORS["text_muted"],
            borderwidth=1,
            relief="solid",
            font=("Segoe UI", 9),
        )
        self.vision_context_menu.add_command(
            label="查看大图", command=self._open_vision_preview
        )
        self.vision_context_menu.add_separator()
        self.vision_context_menu.add_command(
            label="删除选中目标", accelerator="Del",
            command=self.remove_vision_template,
        )
        ttk.Label(
            list_card,
            text="选中目标后，在右侧编辑设置；右键或按 Del 可删除",
            style="Hint.TLabel",
            wraplength=260,
            justify="left",
        ).pack(anchor="w", padx=6, pady=(10, 2))

        edit_card = ttk.Frame(body, style="Card.TFrame", padding=14)
        edit_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        edit_card.columnconfigure(0, weight=1)
        edit_card.rowconfigure(1, weight=1)
        ttk.Label(edit_card, text="目标设置", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        # The preview and per-template action editor are taller than the
        # viewport on a compact laptop. Keep the card fixed in the two-column
        # layout and scroll its contents so the save button is always usable.
        editor_view = ttk.Frame(edit_card, style="CardInner.TFrame")
        editor_view.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        editor_view.columnconfigure(0, weight=1)
        editor_view.rowconfigure(0, weight=1)
        self.vision_editor_canvas = tk.Canvas(
            editor_view, background=COLORS["surface"], highlightthickness=0,
            borderwidth=0,
        )
        self.vision_editor_canvas.grid(row=0, column=0, sticky="nsew")
        self.vision_editor_scroll = ttk.Scrollbar(
            editor_view, orient="vertical", command=self.vision_editor_canvas.yview
        )
        self.vision_editor_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.vision_editor_canvas.configure(yscrollcommand=self.vision_editor_scroll.set)
        editor = ttk.Frame(self.vision_editor_canvas, style="CardInner.TFrame")
        self.vision_editor_inner = editor
        editor_window = self.vision_editor_canvas.create_window(
            (0, 0), window=editor, anchor="nw"
        )

        def update_editor_scrollregion(_event=None):
            try:
                self.vision_editor_canvas.configure(
                    scrollregion=self.vision_editor_canvas.bbox("all")
                )
            except tk.TclError:
                pass

        def resize_editor_width(event):
            try:
                self.vision_editor_canvas.itemconfigure(
                    editor_window, width=max(1, int(event.width))
                )
            except tk.TclError:
                pass

        editor.bind("<Configure>", update_editor_scrollregion)
        self.vision_editor_canvas.bind("<Configure>", resize_editor_width)
        self._vision_editor_wheel_binding = None

        def enter_editor(_event=None):
            if self._vision_editor_wheel_binding is None:
                self._vision_editor_wheel_binding = self.root.bind_all(
                    "<MouseWheel>", self._on_vision_editor_wheel, add="+"
                )

        def leave_editor(_event=None):
            binding = self._vision_editor_wheel_binding
            if binding is not None:
                try:
                    self.root.unbind_all("<MouseWheel>")
                except tk.TclError:
                    pass
                self._vision_editor_wheel_binding = None

        editor_view.bind("<Enter>", enter_editor, add="+")
        editor_view.bind("<Leave>", leave_editor, add="+")
        self.vision_editor_canvas.bind("<Enter>", enter_editor, add="+")
        self.vision_editor_canvas.bind("<Leave>", leave_editor, add="+")
        self._vision_editor_enter = enter_editor
        self._vision_editor_leave = leave_editor
        # Keep the preview in a real panel instead of relying on a Label's
        # requested size.  The panel has a predictable height, expands with
        # the editor width, and redraws the image on resize.  This also gives
        # the action editor below a stable anchor when more per-template
        # controls are added.
        self.vision_preview_frame = tk.Frame(
            editor, bg=COLORS["input"], height=190,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self.vision_preview_frame.pack(fill="x", pady=(12, 4))
        self.vision_preview_frame.pack_propagate(False)
        self.vision_preview_frame.bind("<Configure>", self._resize_vision_preview_panel)
        self.vision_preview_label = tk.Label(
            self.vision_preview_frame, text="未选择图片", anchor="center",
            justify="center", bg=COLORS["input"], fg=COLORS["text_muted"],
            font=("Segoe UI", 10),
        )
        self.vision_preview_label.pack(fill="both", expand=True)
        self.vision_preview_label.bind("<Configure>", self._schedule_vision_preview_render)
        self.vision_preview_label.bind("<Double-Button-1>", self._open_vision_preview)
        self.vision_preview_label.bind("<MouseWheel>", self._on_vision_preview_wheel)
        # Tk reports wheel events as Button-4/5 on some Windows-compatible
        # builds and on X11, so keep those bindings as a harmless fallback.
        self.vision_preview_label.bind("<Button-4>", lambda _event: self._change_vision_preview_zoom(0.1))
        self.vision_preview_label.bind("<Button-5>", lambda _event: self._change_vision_preview_zoom(-0.1))
        preview_tools = ttk.Frame(editor, style="CardInner.TFrame")
        preview_tools.pack(fill="x", pady=(0, 2))
        ttk.Label(preview_tools, text="预览", style="Hint.TLabel").pack(side="left")
        ttk.Button(preview_tools, text="−", width=3, style="Compact.TButton", command=lambda: self._change_vision_preview_zoom(-0.1)).pack(side="right", padx=(4, 0))
        ttk.Button(preview_tools, text="＋", width=3, style="Compact.TButton", command=lambda: self._change_vision_preview_zoom(0.1)).pack(side="right", padx=(4, 0))
        ttk.Button(preview_tools, text="适应", width=5, style="Compact.TButton", command=self._fit_vision_preview).pack(side="right", padx=(4, 0))
        ttk.Button(preview_tools, text="查看大图", style="Compact.TButton", command=self._open_vision_preview).pack(side="right")
        self.vision_path_var = tk.StringVar(value="请从左侧选择一个目标")
        # The filename is already visible in the table; the path remains a
        # model variable for status/config compatibility and is intentionally
        # not given another tall row in this compact editor.
        form = ttk.Frame(editor, style="CardInner.TFrame")
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="匹配阈值", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        self.vision_threshold_var = tk.StringVar(value="0.85")
        ttk.Entry(form, textvariable=self.vision_threshold_var, width=10, style="Vision.TEntry").grid(row=0, column=1, sticky="ew", padx=(18, 0), pady=4)
        ttk.Label(form, text="0.50–0.99", style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(form, text="重复冷却", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        self.vision_cooldown_var = tk.StringVar(value="0.60")
        ttk.Entry(form, textvariable=self.vision_cooldown_var, width=10, style="Vision.TEntry").grid(row=1, column=1, sticky="ew", padx=(18, 0), pady=4)
        ttk.Label(form, text="秒", style="Hint.TLabel").grid(row=1, column=2, sticky="w", padx=(8, 0))
        # The primary mouse button is configured in the action editor below;
        # keeping a second independent button field here made it unclear
        # which value would be used for a multi-step action.
        self.vision_button_var = tk.StringVar(value="左键")
        self.vision_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="启用此目标", variable=self.vision_enabled_var).grid(row=2, column=1, columnspan=2, sticky="w", padx=(18, 7), pady=(7, 0))

        # Per-template action editor.  The compact controls cover the common
        # workflow (repeat clicks, long press, and an optional follow-up
        # button) while the underlying ``actions`` list remains extensible
        # for integrations that need more than two steps.
        action_box = ttk.LabelFrame(editor, text="命中后动作", style="Vision.TLabelframe", padding=(9, 6))
        action_box.pack(fill="x", pady=(10, 0))
        action_box.columnconfigure(1, weight=1)
        action_box.columnconfigure(3, weight=1)
        self.vision_action_kind_var = tk.StringVar(value="点击")
        self.vision_action_button_var = tk.StringVar(value="左键")
        self.vision_action_count_var = tk.StringVar(value="1")
        self.vision_action_interval_var = tk.StringVar(value="80")
        self.vision_action_hold_var = tk.StringVar(value="0.50")
        self.vision_followup_var = tk.BooleanVar(value=False)
        self.vision_followup_kind_var = tk.StringVar(value="点击")
        self.vision_followup_button_var = tk.StringVar(value="右键")
        self.vision_followup_count_var = tk.StringVar(value="1")
        self.vision_followup_interval_var = tk.StringVar(value="80")
        self.vision_followup_hold_var = tk.StringVar(value="0.30")

        ttk.Label(action_box, text="主动作", style="Hint.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        self.vision_action_kind_combo = ttk.Combobox(
            action_box, textvariable=self.vision_action_kind_var,
            values=["点击", "长按", "等待"], state="readonly", width=7,
        )
        self.vision_action_kind_combo.grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="按键", style="Hint.TLabel").grid(row=0, column=2, sticky="e", pady=2)
        self.vision_action_button_combo = ttk.Combobox(
            action_box, textvariable=self.vision_action_button_var,
            values=["左键", "右键", "中键"], state="readonly", width=7,
        )
        self.vision_action_button_combo.grid(row=0, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="次数", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.vision_action_count_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_count_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_count_entry.grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="间隔 ms", style="Hint.TLabel").grid(row=1, column=2, sticky="e", pady=2)
        self.vision_action_interval_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_interval_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_interval_entry.grid(row=1, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="长按/等待 s", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.vision_action_hold_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_hold_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_hold_entry.grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Checkbutton(action_box, text="完成后再执行", variable=self.vision_followup_var).grid(
            row=3, column=0, columnspan=4, sticky="w", pady=(5, 2))
        ttk.Label(action_box, text="后续动作", style="Hint.TLabel").grid(
            row=4, column=0, sticky="w", pady=2
        )
        self.vision_followup_kind_combo = ttk.Combobox(
            action_box, textvariable=self.vision_followup_kind_var,
            values=["点击", "长按", "等待"], state="readonly", width=7,
        )
        self.vision_followup_kind_combo.grid(row=4, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="按键", style="Hint.TLabel").grid(
            row=4, column=2, sticky="e", pady=2
        )
        self.vision_followup_button_combo = ttk.Combobox(
            action_box, textvariable=self.vision_followup_button_var,
            values=["左键", "右键", "中键"], state="readonly", width=7,
        )
        self.vision_followup_button_combo.grid(row=4, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="次数", style="Hint.TLabel").grid(row=5, column=0, sticky="w", pady=2)
        self.vision_followup_count_entry = ttk.Entry(
            action_box, textvariable=self.vision_followup_count_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_count_entry.grid(row=5, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="间隔 ms", style="Hint.TLabel").grid(row=5, column=2, sticky="e", pady=2)
        self.vision_followup_interval_entry = ttk.Entry(
            action_box, textvariable=self.vision_followup_interval_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_interval_entry.grid(row=5, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="长按/等待 s", style="Hint.TLabel").grid(row=6, column=0, sticky="w", pady=2)
        self.vision_followup_hold_entry = ttk.Entry(
            action_box, textvariable=self.vision_followup_hold_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_hold_entry.grid(row=6, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="动作按同一个识别中心点执行", style="Hint.TLabel").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )
        ttk.Button(editor, text="保存目标设置", style="Primary.TButton", command=self.save_vision_selection).pack(fill="x", pady=(12, 0))
        self.vision_action_kind_combo.bind("<<ComboboxSelected>>", self._update_vision_action_state)
        self.vision_followup_kind_combo.bind("<<ComboboxSelected>>", self._update_vision_action_state)
        self.vision_followup_var.trace_add("write", self._update_vision_action_state)
        self._update_vision_action_state()


    def refresh_vision_tree(self):
        if not hasattr(self, "vision_tree"):
            return
        self.vision_tree.delete(*self.vision_tree.get_children())
        for item in self.vision_templates:
            iid = item["id"]
            self.vision_tree.insert("", "end", iid=iid, values=(
                item.get("name", Path(item.get("path", "")).name),
                f"{float(item.get('threshold', 0.85)):.2f}",
                f"{float(item.get('cooldown', 0.60)):.2f}s",
                item.get("button", "左键"),
                "已启用" if item.get("enabled", True) else "已停用",
            ))

    @staticmethod
    def _is_image_path(path: Any) -> bool:
        """Return whether a clipboard file path is a supported image."""
        try:
            return Path(path).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif",
            }
        except (TypeError, ValueError, OSError):
            return False

    def _add_vision_paths(self, paths, *, source: str = "file"):
        """Add existing image paths and keep a running matcher in sync."""
        normalised_paths = []
        seen = set()
        for raw_path in paths or ():
            try:
                path = os.path.abspath(os.path.expanduser(str(raw_path)))
            except (TypeError, ValueError, OSError):
                continue
            key = os.path.normcase(path)
            if key in seen or not Path(path).is_file():
                continue
            seen.add(key)
            normalised_paths.append(path)
        if not normalised_paths:
            return []

        # The matcher takes a snapshot of the enabled templates when it
        # starts. Pause it while the list changes and resume afterwards so a
        # newly imported image is effective immediately.
        was_running = self.vision_running
        if was_running:
            self.stop_vision()
        existing = {
            os.path.normcase(os.path.abspath(item["path"]))
            for item in self.vision_templates
        }
        added_items = []
        try:
            for path in normalised_paths:
                key = os.path.normcase(path)
                if key in existing:
                    continue
                self.vision_template_counter += 1
                item = {
                    "id": f"vision-{self.vision_template_counter}",
                    "path": path,
                    "name": Path(path).name,
                    "threshold": 0.85,
                    "cooldown": 0.60,
                    "button": "左键",
                    "actions": [{"kind": "click", "button": "left", "count": 1, "interval": 0.08, "duration": 0.0}],
                    "click_count": 1,
                    "click_interval": 0.08,
                    "hold_duration": 0.0,
                    "enabled": True,
                    "source": source if source in {"file", "clipboard"} else "file",
                }
                self.vision_templates.append(item)
                added_items.append(item)
                existing.add(key)
            self.refresh_vision_tree()
            if added_items:
                first = added_items[0]
                self.vision_tree.selection_set(first["id"])
                self.vision_tree.focus(first["id"])
                self.on_vision_select()
            return added_items
        finally:
            if was_running and not self.closing:
                # If all rows were disabled while editing, start_vision will
                # leave the matcher stopped and show the actionable status.
                self.start_vision()

    def add_vision_images(self):
        paths = filedialog.askopenfilenames(
            title="选择识别图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        added_items = self._add_vision_paths(paths, source="file")
        if added_items:
            self.vision_log_var.set(f"已添加 {len(added_items)} 张图片，请为每张图片调整匹配阈值")
        else:
            self.vision_log_var.set("所选图片已在列表中")

    @staticmethod
    def _vision_widget_is_text_input(widget: Any) -> bool:
        """Keep Ctrl+V's normal text behavior in form fields."""
        try:
            return widget.winfo_class() in {
                "Entry", "TEntry", "Text", "Spinbox", "TSpinbox",
                "Combobox", "TCombobox",
            }
        except (AttributeError, tk.TclError):
            return False

    def _on_vision_paste(self, _event=None):
        """Handle Ctrl+V on the vision page without hijacking text fields."""
        if self.closing or self.current_page != "vision":
            return None
        try:
            focus = self.root.focus_get()
        except tk.TclError:
            focus = None
        if focus is not None and self._vision_widget_is_text_input(focus):
            # Let Tk's Entry/Combobox class binding paste text as usual.
            return None
        self.paste_vision_image()
        return "break"

    def paste_vision_image(self):
        """Read an image (or copied image files) from the Windows clipboard."""
        if self.vision_paste_busy:
            return
        if paste_to_directory is None:
            self.vision_log_var.set("剪贴板图片功能需要 Pillow，请先安装依赖")
            self.set_status("缺少 Pillow 依赖", "warning")
            return
        self.vision_paste_busy = True
        if hasattr(self, "vision_paste_button"):
            self.vision_paste_button.configure(state="disabled")
        self.vision_log_var.set("正在读取剪贴板图片…")
        threading.Thread(
            target=self._paste_vision_worker,
            name="vision-clipboard-worker",
            daemon=True,
        ).start()

    def _paste_vision_worker(self):
        paths = ()
        error = ""
        try:
            paths = tuple(paste_to_directory(
                CLIPBOARD_TEMPLATE_DIR,
                prefix="clipboard",
                # Copy Explorer-selected image files into our app directory so
                # a later move of the original does not break the template.
                copy_files=True,
            ))
            if not paths:
                error = "剪贴板中没有图片；请先复制截图或图片文件"
        except Exception as exc:
            error = str(exc)
        self.safe_after(self._finish_vision_paste, paths, error)

    def _finish_vision_paste(self, paths, error: str = ""):
        self.vision_paste_busy = False
        if hasattr(self, "vision_paste_button"):
            self.vision_paste_button.configure(state="normal")
        if error:
            self.vision_log_var.set(error)
            self.set_status("剪贴板中没有可用图片", "warning")
            return
        added_items = self._add_vision_paths(paths, source="clipboard")
        if added_items:
            self.vision_log_var.set(
                f"已从剪贴板粘贴 {len(added_items)} 张图片，可直接开始识别"
            )
            self.save_config()
        else:
            self.vision_log_var.set("剪贴板图片已经在列表中")

    def _show_vision_context_menu(self, event):
        """Select the row under the pointer and show its context actions."""
        row_id = self.vision_tree.identify_row(event.y)
        if not row_id:
            return "break"
        self.vision_tree.selection_set(row_id)
        self.vision_tree.focus(row_id)
        self.vision_tree.focus_set()
        # Render the selected preview before the menu command can open it.
        self.on_vision_select()
        try:
            self.vision_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.vision_context_menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _delete_selected_vision_template(self, _event=None):
        """Handle Delete while the target list owns keyboard focus."""
        self.remove_vision_template()
        return "break"

    def remove_vision_template(self):
        selected = self.vision_tree.selection()
        if not selected:
            return
        children_before = list(self.vision_tree.get_children())
        try:
            next_index = min(children_before.index(item_id) for item_id in selected)
        except ValueError:
            next_index = 0
        # The running matcher owns a snapshot of TemplateSpec objects.  Stop
        # it before removing a row so a deleted image cannot keep scanning and
        # clicking until the user happens to restart recognition.
        was_running = self.vision_running
        if was_running:
            self.stop_vision()
        selected_ids = set(selected)
        removed_items = [
            item for item in self.vision_templates if item["id"] in selected_ids
        ]
        for item in removed_items:
            self._cleanup_clipboard_template(item)
        self.vision_templates = [item for item in self.vision_templates if item["id"] not in selected_ids]
        self.refresh_vision_tree()
        remaining_ids = list(self.vision_tree.get_children())
        if remaining_ids:
            next_id = remaining_ids[min(next_index, len(remaining_ids) - 1)]
            self.vision_tree.selection_set(next_id)
            self.vision_tree.focus(next_id)
            self.vision_tree.see(next_id)
            self.on_vision_select()
        else:
            self.vision_path_var.set("请从左侧选择一个目标")
            self._clear_vision_preview("未选择图片")
        self.vision_log_var.set("已删除选中的识别目标")
        self.save_config()
        if was_running and any(item.get("enabled", True) for item in self.vision_templates):
            self.start_vision()

    def clear_vision_templates(self):
        if not self.vision_templates:
            return
        # Stop first: the worker holds its own template snapshot and could
        # otherwise deliver one more click while the list is being cleared.
        if self.vision_running:
            self.stop_vision()
        for item in self.vision_templates:
            self._cleanup_clipboard_template(item)
        self.vision_templates = []
        self.refresh_vision_tree()
        self._clear_vision_preview("未选择图片")
        self.vision_path_var.set("请从左侧选择一个目标")
        self.vision_log_var.set("识别目标已清空")
        # Keep the persisted template list in sync with the UI.  Without this
        # write, a freshly cleared list would be restored from config.json on
        # the next launch (single-row deletion already persists immediately).
        self.save_config()

    @staticmethod
    def _cleanup_clipboard_template(item: dict[str, Any]) -> None:
        """Remove only app-owned files created from clipboard images."""
        if item.get("source") != "clipboard":
            return
        try:
            path = Path(item.get("path", "")).resolve(strict=False)
            root = CLIPBOARD_TEMPLATE_DIR.resolve(strict=False)
            path.relative_to(root)
        except (OSError, RuntimeError, ValueError, TypeError):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _selected_vision_item(self):
        selected = self.vision_tree.selection()
        if not selected:
            return None
        selected_id = selected[0]
        return next((item for item in self.vision_templates if item["id"] == selected_id), None)

    def _schedule_vision_preview_render(self, _event=None):
        """Coalesce resize notifications before redrawing the preview.

        Tk can emit a burst of ``<Configure>`` events while a window is being
        resized.  Re-rendering on every event makes large screenshots feel
        sluggish, so defer the work by one short idle interval.
        """
        if self.closing:
            return
        if self._vision_preview_resize_job is not None:
            try:
                self.root.after_cancel(self._vision_preview_resize_job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._vision_preview_resize_job = self.root.after(
                35, self._render_vision_preview
            )
        except tk.TclError:
            self._vision_preview_resize_job = None

    def _resize_vision_preview_panel(self, _event=None):
        """Keep a useful preview aspect on both compact and wide windows."""
        frame = getattr(self, "vision_preview_frame", None)
        if frame is None:
            return
        try:
            width = int(frame.winfo_width())
            if width <= 2:
                return
            # Keep a readable inline preview even at the minimum window size;
            # the scrolling editor below absorbs the extra vertical space.
            desired_height = max(190, min(300, int(width * 0.60)))
            if abs(int(frame.winfo_height()) - desired_height) > 1:
                frame.configure(height=desired_height)
        except (tk.TclError, TypeError, ValueError):
            return

    def _render_vision_preview(self):
        """Scale the selected source image to the current preview panel."""
        if self.closing:
            self._vision_preview_resize_job = None
            return
        # ``on_vision_select`` can render synchronously while a Configure
        # callback from the previous image is still queued. Cancel that stale
        # callback before clearing the handle, otherwise it survives until
        # window destruction and Tk reports an invalid command.
        pending_job = self._vision_preview_resize_job
        if pending_job is not None:
            try:
                self.root.after_cancel(pending_job)
            except (tk.TclError, ValueError):
                pass
        self._vision_preview_resize_job = None
        label = getattr(self, "vision_preview_label", None)
        if label is None or not label.winfo_exists():
            return
        source = self.vision_preview_source
        if source is None:
            label.configure(image="", text=getattr(self, "_vision_preview_message", "未选择图片"))
            self.vision_preview_image = None
            return
        try:
            from PIL import Image, ImageTk

            panel_w = int(label.winfo_width())
            panel_h = int(label.winfo_height())
            # ``on_vision_select`` can run before Tk has laid out the newly
            # selected row.  Do not create a tiny 48 px placeholder in that
            # case; wait for the first real geometry and render once.
            if panel_w <= 2 or panel_h <= 2:
                try:
                    self._vision_preview_resize_job = self.root.after(
                        40, self._render_vision_preview
                    )
                except tk.TclError:
                    self._vision_preview_resize_job = None
                return
            available_w = max(48, panel_w - 14)
            available_h = max(48, panel_h - 14)
            source_w, source_h = source.size
            if source_w <= 0 or source_h <= 0:
                raise ValueError("empty image")
            fit_scale = min(available_w / source_w, available_h / source_h)
            if self.vision_preview_fit:
                scale = fit_scale
            else:
                # Manual zoom is relative to the panel's fit size.  Limit it
                # to a useful range so a single click cannot allocate a huge
                # bitmap for a very large screenshot.
                scale = fit_scale * max(0.35, min(3.0, self.vision_preview_zoom))
            target_w = max(1, min(4096, int(round(source_w * scale))))
            target_h = max(1, min(4096, int(round(source_h * scale))))
            image = source.copy()
            if image.size != (target_w, target_h):
                try:
                    resample = Image.Resampling.LANCZOS
                except AttributeError:  # Pillow < 9
                    resample = Image.LANCZOS
                image = image.resize((target_w, target_h), resample)
            self.vision_preview_image = ImageTk.PhotoImage(image)
            label.configure(image=self.vision_preview_image, text="")
        except Exception:
            self.vision_preview_image = None
            label.configure(image="", text="无法预览此图片")

    def _change_vision_preview_zoom(self, delta: float):
        """Adjust inline preview zoom while keeping the source untouched."""
        if self.vision_preview_source is None:
            return
        self.vision_preview_fit = False
        self.vision_preview_zoom = max(0.35, min(3.0, self.vision_preview_zoom + float(delta)))
        self._render_vision_preview()

    def _fit_vision_preview(self):
        self.vision_preview_fit = True
        self.vision_preview_zoom = 1.0
        self._render_vision_preview()

    def _on_vision_preview_wheel(self, event):
        """Use the mouse wheel over the preview as a convenient zoom control."""
        try:
            delta = 0.1 if event.delta > 0 else -0.1
        except (AttributeError, TypeError):
            delta = 0.1
        self._change_vision_preview_zoom(delta)
        return "break"

    def _on_vision_editor_wheel(self, event):
        """Scroll the target editor while the pointer is over its controls."""
        canvas = getattr(self, "vision_editor_canvas", None)
        if canvas is None:
            return
        try:
            delta = int(getattr(event, "delta", 0))
            steps = -int(delta / 120) if delta else 0
            if not steps:
                steps = -1 if delta < 0 else 1
            canvas.yview_scroll(steps, "units")
        except (tk.TclError, TypeError, ValueError):
            pass
        return "break"

    def _open_vision_preview(self, _event=None):
        """Open the selected target in a resizable, larger preview window."""
        source = self.vision_preview_source
        if source is None:
            return "break" if _event is not None else None
        old_window = getattr(self, "_vision_preview_window", None)
        try:
            if old_window is not None and old_window.winfo_exists():
                old_window.lift()
                return "break" if _event is not None else None
        except tk.TclError:
            pass
        try:
            from PIL import Image, ImageTk

            window = tk.Toplevel(self.root)
            self._vision_preview_window = window
            window.title("目标图片预览")
            window.configure(bg=COLORS["input"])
            window.geometry("720x520")
            window.minsize(320, 240)
            panel = tk.Frame(window, bg=COLORS["input"])
            panel.pack(fill="both", expand=True, padx=10, pady=10)
            image_label = tk.Label(panel, bg=COLORS["input"], fg=COLORS["text_muted"], anchor="center")
            image_label.pack(fill="both", expand=True)
            preview_source = source.copy()

            def redraw(_event=None):
                try:
                    if not image_label.winfo_exists():
                        return
                except tk.TclError:
                    return
                width = max(80, image_label.winfo_width() - 12)
                height = max(80, image_label.winfo_height() - 12)
                scale = min(width / preview_source.width, height / preview_source.height)
                target = (max(1, int(preview_source.width * scale)), max(1, int(preview_source.height * scale)))
                image = preview_source.copy()
                if image.size != target:
                    try:
                        resample = Image.Resampling.LANCZOS
                    except AttributeError:
                        resample = Image.LANCZOS
                    image = image.resize(target, resample)
                image_label._preview_image = ImageTk.PhotoImage(image)
                image_label.configure(image=image_label._preview_image, text="")

            image_label.bind("<Configure>", redraw)
            window.bind("<Escape>", lambda _event: window.destroy())
            window.protocol("WM_DELETE_WINDOW", window.destroy)
            # Lay out once synchronously instead of queuing an ``after_idle``
            # callback that could outlive the preview window when it is closed
            # immediately after opening.
            window.update_idletasks()
            redraw()
        except Exception:
            try:
                window.destroy()
            except Exception:
                pass
        return "break" if _event is not None else None

    def _clear_vision_preview(self, message: str = "未选择图片"):
        """Release the source and displayed PhotoImage for a deleted row."""
        self.vision_preview_source = None
        self.vision_preview_image = None
        self._vision_preview_message = message
        label = getattr(self, "vision_preview_label", None)
        if label is not None:
            try:
                label.configure(image="", text=message)
            except tk.TclError:
                pass

    def on_vision_select(self, _event=None):
        item = self._selected_vision_item()
        if not item:
            return
        try:
            self.vision_editor_canvas.yview_moveto(0.0)
        except (AttributeError, tk.TclError):
            pass
        self.vision_path_var.set(f"文件：{Path(item['path']).name}")
        self.vision_threshold_var.set(f"{float(item.get('threshold', 0.85)):.2f}")
        self.vision_cooldown_var.set(f"{float(item.get('cooldown', 0.60)):.2f}")
        self.vision_button_var.set(self._vision_action_button(item.get("button"), "左键"))
        self.vision_enabled_var.set(bool(item.get("enabled", True)))
        self._load_vision_action_fields(item)
        self.vision_preview_image = None
        self.vision_preview_source = None
        self.vision_preview_fit = True
        self.vision_preview_zoom = 1.0
        self._vision_preview_message = "未选择图片"
        try:
            from PIL import Image
            # Decode a copy so Image.open's file handle is closed promptly.
            # Leaving it open can lock the selected file on Windows and make
            # replacing or deleting that template fail.
            with Image.open(item["path"]) as source:
                image = source.copy()
            self.vision_preview_source = image
            self._render_vision_preview()
        except Exception:
            self._clear_vision_preview("无法预览此图片")

    @staticmethod
    def _vision_action_ui_kind(kind: Any) -> str:
        text = str(kind or "click").strip().lower()
        return {
            "click": "点击", "点击": "点击", "tap": "点击", "press": "点击",
            "hold": "长按", "长按": "长按", "long_press": "长按", "longpress": "长按",
            "wait": "等待", "等待": "等待", "delay": "等待", "延时": "等待",
        }.get(text, "点击")

    @staticmethod
    def _vision_action_engine_kind(kind: Any) -> str:
        text = str(kind or "点击").strip().lower()
        return {
            "点击": "click", "click": "click", "tap": "click", "press": "click",
            "长按": "hold", "hold": "hold", "long_press": "hold", "longpress": "hold",
            "等待": "wait", "wait": "wait", "delay": "wait", "延时": "wait",
        }.get(text, "click")

    @staticmethod
    def _vision_action_button(value: Any, default: str = "左键") -> str:
        aliases = {
            "left": "左键", "left click": "左键", "左键": "左键",
            "right": "右键", "right click": "右键", "右键": "右键",
            "middle": "中键", "middle click": "中键", "中键": "中键",
        }
        return aliases.get(str(value or "").strip().lower(), default)

    def _update_vision_action_state(self, *_args):
        """Enable only the fields that apply to the selected action kind."""
        try:
            primary_kind = self._vision_action_engine_kind(
                self.vision_action_kind_var.get()
            )
            follow_enabled = bool(self.vision_followup_var.get())
            follow_kind = self._vision_action_engine_kind(
                self.vision_followup_kind_var.get()
            )

            def set_entry(widget, enabled: bool):
                widget.configure(state="normal" if enabled else "disabled")

            def set_combo(widget, enabled: bool):
                widget.configure(state="readonly" if enabled else "disabled")

            set_entry(self.vision_action_count_entry, primary_kind == "click")
            set_entry(self.vision_action_interval_entry, primary_kind == "click")
            set_entry(self.vision_action_hold_entry, primary_kind in {"hold", "wait"})
            set_combo(self.vision_action_button_combo, primary_kind != "wait")

            set_combo(self.vision_followup_kind_combo, follow_enabled)
            set_combo(self.vision_followup_button_combo, follow_enabled and follow_kind != "wait")
            set_entry(self.vision_followup_count_entry, follow_enabled and follow_kind == "click")
            set_entry(self.vision_followup_interval_entry, follow_enabled and follow_kind == "click")
            set_entry(self.vision_followup_hold_entry, follow_enabled and follow_kind in {"hold", "wait"})
        except (AttributeError, tk.TclError, TypeError, ValueError):
            # During initial widget construction some references are not yet
            # available; the final call at the end of build_vision_page applies
            # the correct states once all controls exist.
            pass

    def _load_vision_action_fields(self, item: dict[str, Any]) -> None:
        """Populate the compact action editor from a saved template row."""
        raw = item.get("actions")
        if not isinstance(raw, (list, tuple)) or not raw:
            try:
                legacy_hold = max(0.0, float(item.get("hold_duration", 0) or 0))
            except (TypeError, ValueError):
                legacy_hold = 0.0
            try:
                legacy_interval = max(0.0, float(item.get("click_interval", 0.08) or 0.08))
            except (TypeError, ValueError):
                legacy_interval = 0.08
            raw = [{
                "kind": "hold" if legacy_hold > 0 else "click",
                "button": item.get("button", "左键"),
                "count": item.get("click_count", 1),
                "interval": legacy_interval,
                "duration": legacy_hold,
            }]
        first = raw[0] if isinstance(raw[0], dict) else getattr(raw[0], "__dict__", {})
        self.vision_action_kind_var.set(self._vision_action_ui_kind(first.get("kind", first.get("type", "click"))))
        self.vision_action_button_var.set(self._vision_action_button(first.get("button"), item.get("button", "左键")))
        try:
            count = max(1, int(first.get("count", first.get("clicks", 1))))
        except (TypeError, ValueError):
            count = 1
        try:
            interval = max(0.0, float(first.get("interval", first.get("gap", 0.08))))
        except (TypeError, ValueError):
            interval = 0.08
        try:
            duration = max(0.0, float(first.get("duration", first.get("hold_duration", 0.0))))
        except (TypeError, ValueError):
            duration = 0.0
        self.vision_action_count_var.set(str(count))
        self.vision_action_interval_var.set(str(round(interval * 1000, 3)).rstrip("0").rstrip("."))
        self.vision_action_hold_var.set(f"{duration:.3f}".rstrip("0").rstrip("."))
        if len(raw) > 1:
            second = raw[1] if isinstance(raw[1], dict) else getattr(raw[1], "__dict__", {})
            self.vision_followup_var.set(True)
            self.vision_followup_kind_var.set(self._vision_action_ui_kind(second.get("kind", second.get("type", "click"))))
            self.vision_followup_button_var.set(self._vision_action_button(second.get("button"), "右键"))
            try:
                follow_count = max(1, int(second.get("count", second.get("clicks", 1))))
            except (TypeError, ValueError):
                follow_count = 1
            try:
                follow_interval = max(0.0, float(second.get("interval", second.get("gap", 0.08))))
            except (TypeError, ValueError):
                follow_interval = 0.08
            try:
                follow_duration = max(0.0, float(second.get("duration", second.get("hold_duration", 0.0))))
            except (TypeError, ValueError):
                follow_duration = 0.0
            self.vision_followup_count_var.set(str(follow_count))
            self.vision_followup_interval_var.set(str(round(follow_interval * 1000, 3)).rstrip("0").rstrip("."))
            self.vision_followup_hold_var.set(f"{follow_duration:.3f}".rstrip("0").rstrip("."))
        else:
            self.vision_followup_var.set(False)
            self.vision_followup_kind_var.set("点击")
            self.vision_followup_button_var.set("右键")
            self.vision_followup_count_var.set("1")
            self.vision_followup_interval_var.set("80")
            self.vision_followup_hold_var.set("0.30")
        self._update_vision_action_state()

    def _read_vision_action_fields(self) -> tuple[list[dict[str, Any]], str]:
        """Validate editor values and return JSON-friendly action steps."""
        def parse_int(value: Any, label: str) -> int:
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(f"{label}必须是整数")
            if number < 1 or number > 999:
                raise ValueError(f"{label}范围应为 1-999")
            return number

        def parse_float(value: Any, label: str, maximum: float = 3600.0) -> float:
            try:
                number = float(str(value).strip())
            except (TypeError, ValueError):
                raise ValueError(f"{label}必须是数字")
            if not math.isfinite(number) or number < 0 or number > maximum:
                raise ValueError(f"{label}范围应为 0-{maximum:g}")
            return number

        def one(kind_var, button_var, count_var, interval_var, hold_var):
            kind = self._vision_action_engine_kind(kind_var.get())
            # Count and interval only affect click actions.  Do not reject a
            # blank/disabled value when the user selected hold or wait.
            if kind == "click":
                count = parse_int(count_var.get(), "点击次数")
                interval_ms = parse_float(interval_var.get(), "动作间隔", 60000.0)
            else:
                count = 1
                interval_ms = 0.0
            duration = parse_float(hold_var.get(), "长按/等待时长")
            return {
                "kind": kind,
                "button": {"左键": "left", "右键": "right", "中键": "middle"}.get(button_var.get(), "left"),
                "count": count,
                "interval": interval_ms / 1000.0,
                "duration": duration if kind in {"hold", "wait"} else 0.0,
            }

        actions = [one(self.vision_action_kind_var, self.vision_action_button_var,
                       self.vision_action_count_var, self.vision_action_interval_var,
                       self.vision_action_hold_var)]
        if self.vision_followup_var.get():
            actions.append(one(self.vision_followup_kind_var, self.vision_followup_button_var,
                               self.vision_followup_count_var, self.vision_followup_interval_var,
                               self.vision_followup_hold_var))
        return actions, actions[0]["button"]

    def save_vision_selection(self):
        item = self._selected_vision_item()
        if not item:
            self.vision_log_var.set("请先从左侧选择一个目标")
            return False
        try:
            threshold = float(self.vision_threshold_var.get())
            cooldown = float(self.vision_cooldown_var.get())
            if not math.isfinite(threshold) or threshold < 0.5 or threshold > 0.99:
                raise ValueError("阈值范围应为 0.50–0.99")
            if not math.isfinite(cooldown) or cooldown < 0:
                raise ValueError("冷却时间不能为负数")
            actions, primary_button = self._read_vision_action_fields()
        except ValueError as exc:
            messagebox.showerror("目标设置错误", str(exc))
            return False
        # The engine receives a TemplateSpec snapshot at start.  If settings
        # are edited while recognition is active, restart it after updating so
        # the new threshold/cooldown/button takes effect immediately.
        was_running = self.vision_running
        if was_running:
            self.stop_vision()
        # Keep the legacy button/count fields in sync for older callers while
        # persisting the richer action list used by the current engine.
        first_action = actions[0]
        item.update({
            "threshold": threshold,
            "cooldown": cooldown,
            "button": {"left": "左键", "right": "右键", "middle": "中键"}.get(primary_button, "左键"),
            "enabled": bool(self.vision_enabled_var.get()),
            "actions": actions,
            "click_count": int(first_action.get("count", 1)),
            "click_interval": float(first_action.get("interval", 0.08)),
            "hold_duration": float(first_action.get("duration", 0.0)) if first_action.get("kind") == "hold" else 0.0,
        })
        self.vision_button_var.set(item["button"])
        self.refresh_vision_tree()
        self.vision_tree.selection_set(item["id"])
        self.vision_log_var.set(f"已保存：{item['name']}")
        # Persist per-template actions immediately so a configured sequence is
        # not lost if the app is closed before the next global save.
        self.save_config()
        if was_running and not self.closing:
            self.start_vision()
        return True

    def toggle_vision(self):
        if self.vision_running:
            self.stop_vision()
        else:
            self.start_vision()

    def build_vision_specs(self):
        """Convert saved UI rows into validated engine templates."""
        specs = []
        for item in self.vision_templates:
            if not item.get("enabled", True):
                continue
            specs.append(TemplateSpec(
                path=item["path"], name=item.get("name", Path(item["path"]).name),
                threshold=float(item.get("threshold", 0.85)), cooldown=float(item.get("cooldown", 0.60)),
                button={"左键": "left", "右键": "right", "中键": "middle"}.get(item.get("button", "左键"), "left"),
                actions=item.get("actions"),
                click_count=int(item.get("click_count", 1) or 1),
                click_interval=float(item.get("click_interval", 0.08) or 0.08),
                hold_duration=float(item.get("hold_duration", 0.0) or 0.0),
                id=item["id"],
            ))
        return specs

    def scan_vision_once(self):
        """Run one non-clicking scan for safely tuning a template."""
        if VisionEngine is None or TemplateSpec is None:
            messagebox.showerror("缺少图片识别依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        if not self.vision_templates:
            messagebox.showinfo("还没有识别目标", "请先添加一张目标图片。")
            return
        if self._selected_vision_item() and not self.save_vision_selection():
            return
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            specs = self.build_vision_specs()
            interval = max(0.03, float(self.vision_scan_var.get()))
            if not specs:
                messagebox.showinfo("没有启用目标", "请至少启用一张识别图片。")
                return
        except ImportError:
            messagebox.showerror("缺少图片识别依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        except (TypeError, ValueError) as exc:
            messagebox.showerror("识别准备失败", str(exc))
            return
        self.vision_log_var.set("正在扫描当前屏幕…")
        def worker():
            try:
                # scan_once intentionally keeps matching other templates when
                # one file fails to decode.  Pass an error callback so a bad
                # image is reported in the visible log instead of looking like
                # a clean no-match scan.
                engine = VisionEngine(
                    specs, interval=interval, auto_click=False,
                    on_error=self.on_vision_error,
                )
                matches = engine.scan_once(trigger=False)
                scan_error = str(engine.last_error) if engine.last_error else ""
                self.safe_after(self.show_vision_scan_result, matches, scan_error)
            except Exception as exc:
                self.safe_after(self.on_vision_error, str(exc))
        threading.Thread(target=worker, name="vision-one-shot", daemon=True).start()

    def show_vision_scan_result(self, matches, scan_error: str = ""):
        # A malformed template is reported by VisionEngine while other
        # templates continue scanning.  Keep that diagnostic visible instead
        # of replacing it with a misleading generic "no match" message.
        if scan_error and not matches:
            self.vision_log_var.set(f"识别错误：{scan_error}")
            self.vision_global_status.set("扫描有错误")
            return
        if not matches:
            self.vision_log_var.set("本次扫描没有找到目标；可以降低匹配阈值或重新截取模板")
            self.vision_global_status.set("未发现目标")
            return
        first = matches[0]
        suffix = f"；另有模板错误：{scan_error}" if scan_error else ""
        self.vision_log_var.set(f"测试命中 {len(matches)} 个目标：{first.name} · 置信度 {first.score:.0%} · ({first.center_x}, {first.center_y}){suffix}")
        self.vision_global_status.set(f"命中 {len(matches)} 个" + (" · 有错误" if scan_error else ""))

    def start_vision(self):
        if VisionEngine is None or TemplateSpec is None:
            messagebox.showerror("缺少图片识别依赖", "请先运行：python -m pip install -r requirements.txt\n然后重新启动程序。")
            return
        if not self.vision_templates:
            messagebox.showinfo("还没有识别目标", "请先点击“添加图片”，选择要识别的目标。")
            return
        if self._selected_vision_item() and not self.save_vision_selection():
            return
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            scan_interval = float(self.vision_scan_var.get())
            if not math.isfinite(scan_interval) or scan_interval < 0.03:
                raise ValueError
        except ImportError:
            messagebox.showerror("缺少图片识别依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        except (TypeError, ValueError):
            messagebox.showerror("扫描设置错误", "扫描间隔必须是大于 0.03 秒的数字")
            return
        try:
            specs = self.build_vision_specs()
        except (TypeError, ValueError) as exc:
            messagebox.showerror("识别目标设置错误", str(exc))
            return
        if not specs:
            messagebox.showinfo("没有启用目标", "请至少启用一张识别图片。")
            return
        self.vision_generation += 1
        generation = self.vision_generation
        try:
            self.vision_engine = VisionEngine(
                specs,
                interval=scan_interval,
                auto_click=False,
                on_match=lambda result, token=generation: self.on_vision_match(result, token),
                on_error=lambda error, token=generation: self.on_vision_error(error, token),
            )
            # Set this before starting the worker: the first scan can happen
            # immediately and should not be discarded by the callback guard.
            self.vision_running = True
            self.vision_engine.start()
        except Exception as exc:
            self.vision_engine = None
            self.vision_running = False
            messagebox.showerror("识别启动失败", str(exc))
            return
        self.vision_start_button.configure(text="■  停止识别")
        self.vision_global_status.set(f"识别中 · {len(specs)}")
        self.set_status("图片识别中", "success")

    def stop_vision(self):
        # Invalidate callbacks immediately, even if a slow screen capture
        # keeps the old worker alive for a short time during its join.
        self.vision_generation += 1
        engine = self.vision_engine
        self.vision_engine = None
        self.vision_running = False
        if engine:
            try:
                engine.stop(wait=True)
            except TypeError:
                engine.stop()
            except Exception:
                pass
        if hasattr(self, "vision_start_button"):
            self.vision_start_button.configure(text="▶  开始识别")
        self.vision_global_status.set("待机")
        self.set_status("图片识别已停止", "neutral")

    def on_vision_match(self, result, generation: Optional[int] = None):
        """Called from the matcher thread. Move and execute the row's action plan."""
        if self.closing or not self.vision_running:
            return
        if generation is not None and generation != self.vision_generation:
            return
        try:
            if mouse is None or send_click is None:
                raise RuntimeError("鼠标控制依赖不可用")
            controller = mouse.Controller()
            # Capture the engine object before a concurrent stop can clear
            # ``self.vision_engine``. Its event is then still set by
            # stop_vision(), allowing a long press to release promptly.
            engine = self.vision_engine
            if engine is None:
                return
            stop_event = engine.stop_event
            actions = getattr(result, "actions", None)
            if not actions:
                actions = [{"kind": "click", "button": getattr(result, "button", "left"), "count": 1}]
            completed = execute_template_actions(
                actions,
                x=int(result.center_x), y=int(result.center_y),
                stop_event=stop_event,
                move_fn=lambda px, py: setattr(controller, "position", (px, py)),
                click_fn=send_click,
                mouse_down_fn=send_mouse_down,
                mouse_up_fn=send_mouse_up,
            ) if execute_template_actions is not None else 0
            name = getattr(result, "name", "目标图片")
            score = float(getattr(result, "score", 0.0))
            self.safe_after(self.vision_match_ui, name, score, int(result.center_x), int(result.center_y), generation, completed)
        except Exception as exc:
            self.safe_after(self.on_vision_error, str(exc), generation)

    def vision_match_ui(self, name: str, score: float, x: int, y: int,
                        generation: Optional[int] = None, completed: int = 1):
        if generation is not None and generation != self.vision_generation:
            return
        self.vision_log_var.set(f"已执行 {completed} 次动作：{name} · 置信度 {score:.0%} · ({x}, {y})")
        short_name = name if len(name) <= 12 else name[:12] + "…"
        self.vision_global_status.set(f"命中 · {short_name}")

    def on_vision_error(self, error, generation: Optional[int] = None):
        self.safe_after(self._vision_error_ui, str(error), generation)

    def _vision_error_ui(self, error: str, generation: Optional[int] = None):
        if generation is not None and generation != self.vision_generation:
            return
        self.vision_log_var.set(f"识别错误：{error}")
        self.set_status("图片识别出错", "danger")

    def build_hotkey_page(self):
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["hotkeys"] = page
        intro = ttk.Frame(page, style="Card.TFrame", padding=20)
        intro.pack(fill="x", pady=(0, 14))
        ttk.Label(intro, text="按键捕获", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(intro, text="点击任意输入框，然后按下一个键或组合键。程序会自动识别并显示友好名称。", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
        card = ttk.Frame(page, style="Card.TFrame", padding=20)
        card.pack(fill="x", pady=(0, 14))
        card.columnconfigure(1, weight=1)
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        self.hotkey_state_vars: dict[str, tk.StringVar] = {}
        self.hotkey_entries: dict[str, ttk.Entry] = {}
        for row, name in enumerate(("toggle", "record", "stop")):
            self.hotkey_vars[name] = tk.StringVar(value=self.display_hotkey(HOTKEY_DEFAULTS[name]))
            self.hotkey_state_vars[name] = tk.StringVar(value="点击输入框后按键")
            ttk.Label(card, text=HOTKEY_LABELS[name], style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=10)
            entry = ttk.Entry(card, textvariable=self.hotkey_vars[name], width=25)
            entry.grid(row=row, column=1, sticky="ew", padx=(28, 10), pady=10)
            entry.bind("<Button-1>", lambda event, key=name: self.arm_hotkey_capture(key))
            entry.bind("<KeyPress>", lambda event, key=name: self.capture_hotkey(event, key))
            self.hotkey_entries[name] = entry
            ttk.Label(card, textvariable=self.hotkey_state_vars[name], style="Hint.TLabel", width=19).grid(row=row, column=2, sticky="e", pady=10)
            ttk.Button(card, text="清除", command=lambda key=name: self.clear_hotkey(key)).grid(row=row, column=3, padx=(12, 0), pady=10)
        ttk.Separator(card).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 14))
        self.hotkey_apply_status = tk.StringVar(value="修改后点击应用，快捷键会立即生效")
        ttk.Button(card, text="应用快捷键", style="Primary.TButton", command=self.apply_hotkeys).grid(row=4, column=0, sticky="w")
        ttk.Button(card, text="保存配置", command=self.save_config).grid(row=4, column=1, sticky="w", padx=(12, 0))
        ttk.Label(card, textvariable=self.hotkey_apply_status, style="Hint.TLabel").grid(row=4, column=2, columnspan=2, sticky="e")
        help_card = ttk.Frame(page, style="Card.TFrame", padding=20)
        help_card.pack(fill="x")
        ttk.Label(help_card, text="使用小贴士", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(help_card, text="支持 F1–F12、字母、数字、空格、回车，以及 Ctrl / Alt / Shift 组合键。\n图片识别页的 Ctrl+V 用于直接粘贴图片，不能分配给其他任务。\n如果快捷键没有反应，请换一个没有被其他软件占用的组合。", style="Muted.TLabel", justify="left").pack(anchor="w", pady=(8, 0))

    def build_footer(self):
        footer = tk.Frame(self.content, bg=COLORS["sidebar"], height=30)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        self.footer_var = tk.StringVar(value="就绪 · 全局快捷键已启用")
        tk.Label(footer, textvariable=self.footer_var, bg=COLORS["sidebar"], fg=COLORS["text_muted"], anchor="w", padx=18, font=("Segoe UI", 8)).pack(fill="both")

    def show_page(self, name: str):
        if name not in self.page_frames:
            return
        for frame in self.page_frames.values():
            frame.pack_forget()
        self.page_frames[name].pack(fill="both", expand=True)
        self.current_page = name
        title, subtitle = self.page_meta[name]
        self.page_title_var.set(title)
        self.page_subtitle_var.set(subtitle)
        for page, button in self.nav_buttons.items():
            button.configure(style="NavSelected.TButton" if page == name else "Nav.TButton")

    # ------------------------------------------------------------ persistence
    @staticmethod
    def read_json(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return default

    @staticmethod
    def write_json(path: Path, data) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False

    def load_config(self):
        data = self.read_json(CONFIG_FILE, None)
        if not isinstance(data, dict):
            data = self.read_json(LEGACY_CONFIG_FILE, {})
        if not isinstance(data, dict):
            data = {}
        try:
            if "interval_ms" in data:
                self.interval_var.set(str(data["interval_ms"]))
            elif "interval" in data:
                self.interval_var.set(str(round(float(data["interval"]) * 1000, 3)))
            self.count_var.set(str(data.get("count", self.count_var.get())))
            button = data.get("button", self.click_button_var.get())
            self.click_button_var.set(button if button in {"左键", "右键", "中键"} else "左键")
            self.random_var.set(bool(data.get("random", False)))
            position = data.get("position", self.position_var.get())
            self.position_var.set(position if position in {"跟随鼠标当前位置", "固定坐标"} else "跟随鼠标当前位置")
            self.x_var.set(str(data.get("x", 0)))
            self.y_var.set(str(data.get("y", 0)))
            self.delay_var.set(str(data.get("delay", 0)))
            mode = data.get("click_mode", "单击")
            self.click_mode_var.set(mode if mode in {"单击", "双击"} else "单击")
            self.record_include_moves_var.set(bool(data.get("record_include_moves", True)))
            speed = str(data.get("speed", self.speed_var.get()))
            self.speed_var.set(speed if speed in {"0.5x", "1.0x", "1.5x", "2.0x", "4.0x"} else "1.0x")
            self.loop_var.set(str(data.get("loops", self.loop_var.get())))
            self.vision_scan_var.set(str(data.get("vision_scan_interval", self.vision_scan_var.get())))
        except (ValueError, tk.TclError):
            pass
        old_keys = {"toggle": "toggle_hotkey", "record": "record_hotkey", "stop": "stop_hotkey"}
        conflicting_hotkeys = []
        for name in HOTKEY_DEFAULTS:
            spec = data.get(f"{name}_hotkey", data.get(old_keys[name], HOTKEY_DEFAULTS[name]))
            # An explicitly cleared shortcut remains cleared across restarts.
            normalized = self.normalize_hotkey_spec(spec)
            if isinstance(spec, str) and spec.strip().lower() in {"", "未设置", "none", "无"}:
                self.hotkey_specs[name] = ""
            elif self._is_clipboard_paste_hotkey(normalized):
                # Older config files may contain Ctrl+V from before direct
                # image paste was added.  Clear only the conflicting entry so
                # valid shortcuts (for example F7/F8) keep working.
                self.hotkey_specs[name] = ""
                conflicting_hotkeys.append(name)
            else:
                self.hotkey_specs[name] = normalized or HOTKEY_DEFAULTS[name]
            self.hotkey_vars[name].set(self.display_hotkey(self.hotkey_specs[name]))
            if self._is_clipboard_paste_hotkey(normalized):
                self.hotkey_state_vars[name].set("Ctrl+V 已保留给图片粘贴")
        if conflicting_hotkeys:
            self.hotkey_apply_status.set("已清除与图片粘贴冲突的快捷键")
        self.refresh_hotkey_tip()
        self.update_position_state()
        vision_data = data.get("vision_templates", [])
        if isinstance(vision_data, list):
            self.vision_templates = []
            for raw in vision_data:
                if not isinstance(raw, dict) or not raw.get("path"):
                    continue
                path = os.path.abspath(str(raw["path"]))
                if not Path(path).is_file():
                    continue
                self.vision_template_counter += 1
                try:
                    threshold = float(raw.get("threshold", 0.85))
                    cooldown = float(raw.get("cooldown", 0.60))
                except (TypeError, ValueError):
                    threshold, cooldown = 0.85, 0.60
                self.vision_templates.append({
                    "id": f"vision-{self.vision_template_counter}", "path": path,
                    "name": str(raw.get("name", Path(path).name)),
                    "threshold": min(0.99, max(0.5, threshold)) if math.isfinite(threshold) else 0.85,
                    "cooldown": max(0.0, cooldown) if math.isfinite(cooldown) else 0.60,
                    "button": self._vision_action_button(raw.get("button"), "左键"),
                    "actions": raw.get("actions") if isinstance(raw.get("actions"), list) else None,
                    "click_count": max(1, int(raw.get("click_count", 1) or 1)) if str(raw.get("click_count", 1)).lstrip("-").isdigit() else 1,
                    "click_interval": max(0.0, float(raw.get("click_interval", 0.08) or 0.08)) if _finite_number(raw.get("click_interval", 0.08)) else 0.08,
                    "hold_duration": max(0.0, float(raw.get("hold_duration", 0.0) or 0.0)) if _finite_number(raw.get("hold_duration", 0.0)) else 0.0,
                    "enabled": bool(raw.get("enabled", True)),
                    "source": raw.get("source", "file") if raw.get("source", "file") in {"file", "clipboard"} else "file",
                })
            self.refresh_vision_tree()

    def save_config(self):
        data = {
            "interval_ms": self.interval_var.get(), "count": self.count_var.get(), "button": self.click_button_var.get(),
            "click_mode": self.click_mode_var.get(), "random": self.random_var.get(), "position": self.position_var.get(),
            "x": self.x_var.get(), "y": self.y_var.get(), "delay": self.delay_var.get(),
            "record_include_moves": self.record_include_moves_var.get(), "speed": self.speed_var.get(), "loops": self.loop_var.get(),
            "vision_scan_interval": self.vision_scan_var.get(),
            "vision_templates": [{key: item.get(key) for key in ("path", "name", "threshold", "cooldown", "button", "actions", "click_count", "click_interval", "hold_duration", "enabled", "source")} for item in self.vision_templates],
            "toggle_hotkey": self.hotkey_specs.get("toggle", HOTKEY_DEFAULTS["toggle"]),
            "record_hotkey": self.hotkey_specs.get("record", HOTKEY_DEFAULTS["record"]),
            "stop_hotkey": self.hotkey_specs.get("stop", HOTKEY_DEFAULTS["stop"]),
        }
        if self.write_json(CONFIG_FILE, data):
            self.set_status("配置已保存", "success")
        else:
            self.set_status("配置保存失败", "danger")

    def load_recording(self):
        data = self.read_json(RECORD_FILE, None)
        if not isinstance(data, list):
            data = self.read_json(LEGACY_RECORD_FILE, [])
        valid = []
        for event in data if isinstance(data, list) else []:
            if not isinstance(event, dict) or event.get("type", event.get("kind")) not in {"move", "click"}:
                continue
            try:
                timestamp = float(event.get("t", 0))
                if not math.isfinite(timestamp) or timestamp < 0:
                    continue
                valid.append({
                    "type": event.get("type", event.get("kind")), "t": timestamp,
                    "x": int(event.get("x", 0)), "y": int(event.get("y", 0)),
                    "button": _button_name(event.get("button", "left")), "pressed": bool(event.get("pressed", True)),
                })
            except (TypeError, ValueError):
                continue
        self.events = valid
        self.refresh_event_tree()
        if valid:
            self.record_status_var.set(f"已加载 {len(valid)} 个动作")

    def save_recording(self):
        with self.event_lock:
            data = list(self.events)
        if not self.write_json(RECORD_FILE, data):
            self.set_status("录制保存失败", "danger")

    # -------------------------------------------------------------- status
    def set_status(self, text: str, tone: str = "neutral"):
        colours = {
            "neutral": (COLORS["surface_hover"], COLORS["text_secondary"]),
            "success": (COLORS["success"], "#071A12"),
            "warning": (COLORS["warning"], "#211706"),
            "danger": (COLORS["danger"], "#260A0E"),
        }
        bg, fg = colours.get(tone, colours["neutral"])
        if self.closing:
            return
        try:
            self.status_pill.configure(text=f"●  {text}", bg=bg, fg=fg)
            self.footer_var.set(text)
        except (tk.TclError, RuntimeError):
            pass

    def safe_after(self, callback: Callable, *args):
        if self.closing:
            return
        try:
            self.root.after(0, callback, *args)
        except (tk.TclError, RuntimeError):
            pass

    def update_position_state(self):
        state = "normal" if self.position_var.get() == "固定坐标" else "disabled"
        for entry in (getattr(self, "x_entry", None), getattr(self, "y_entry", None)):
            if entry is not None:
                entry.configure(state=state)

    # ----------------------------------------------------------- hotkeys
    @staticmethod
    def normalize_hotkey_spec(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        value = value.strip().lower()
        if not value or value in {"未设置", "none", "无"}:
            return ""
        # A literal '+' is represented by a trailing '+' in pynput syntax:
        # '+' or '<ctrl>++'.  Keep that second plus instead of treating it as
        # an empty separator.
        literal_plus = value == "+" or value.endswith("++")
        core = value[:-1] if literal_plus and value != "+" else ("" if value == "+" else value)
        parts = [part.strip() for part in core.split("+") if part.strip()]
        normalized = "+".join(parts)
        if literal_plus:
            return normalized + "++" if normalized else "+"
        return normalized

    @staticmethod
    def display_hotkey(spec: str) -> str:
        names = {"<ctrl>": "Ctrl", "<alt>": "Alt", "<shift>": "Shift", "<enter>": "Enter", "<esc>": "Esc", "<space>": "Space", "<tab>": "Tab", "<backspace>": "Backspace", "<delete>": "Delete", "<page_up>": "Page Up", "<page_down>": "Page Down"}
        value = str(spec).strip().lower()
        literal_plus = value == "+" or value.endswith("++")
        core = value[:-1] if literal_plus and value != "+" else ("" if value == "+" else value)
        parts = []
        for part in core.split("+"):
            part = part.strip()
            if not part:
                continue
            if part in names:
                parts.append(names[part])
            elif part.startswith("<") and part.endswith(">"):
                parts.append(part[1:-1].replace("_", " ").title())
            else:
                parts.append(part.upper() if len(part) == 1 else part.title())
        if literal_plus:
            parts.append("+")
        return " + ".join(parts) if parts else "未设置"

    def hotkey_tip_text(self) -> str:
        """Return the sidebar shortcut reminder using the active bindings."""
        labels = (("toggle", "连点开关"), ("record", "录制开关"), ("stop", "停止全部"))
        return "\n".join(
            f"{self.display_hotkey(self.hotkey_specs.get(name, ''))}  {label}"
            for name, label in labels
        )

    def refresh_hotkey_tip(self) -> None:
        if hasattr(self, "hotkey_tip_var"):
            self.hotkey_tip_var.set(self.hotkey_tip_text())

    def arm_hotkey_capture(self, name: str):
        self.hotkey_ignore_until = time.monotonic() + 0.5
        if self.hotkey_listener:
            old_listener = self.hotkey_listener
            old_listener.stop()
            try:
                old_listener.join(timeout=0.5)
            except Exception:
                pass
            self.hotkey_listener = None
        self.hotkey_capture_target = name
        self.hotkey_capture_previous = self.hotkey_specs.get(name, HOTKEY_DEFAULTS[name])
        entry = self.hotkey_entries[name]
        entry.configure(style="Capture.TEntry")
        entry.focus_set()
        entry.selection_range(0, tk.END)
        self.hotkey_state_vars[name].set("等待按键…")
        self.set_status("请按下要设置的快捷键", "warning")

    def clear_hotkey(self, name: str):
        self.hotkey_specs[name] = ""
        self.hotkey_vars[name].set("未设置")
        self.hotkey_state_vars[name].set("已清除")
        self.hotkey_apply_status.set("有快捷键未设置，应用后将保持空闲")
        self.refresh_hotkey_tip()

    def capture_hotkey(self, event, name: str):
        if self.hotkey_capture_target != name:
            return "break"
        keysym = str(event.keysym)
        state = int(getattr(event, "state", 0))
        if keysym in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Win_L", "Win_R"}:
            self.hotkey_state_vars[name].set("继续按组合键…")
            return "break"
        if keysym in {"Escape", "Esc"} and not state & (0x0001 | 0x0004 | 0x0008 | 0x20000):
            spec = self.hotkey_capture_previous
            self.hotkey_specs[name] = spec
            self.hotkey_vars[name].set(self.display_hotkey(spec))
            self.hotkey_state_vars[name].set("已取消")
            self.finish_hotkey_capture()
            return "break"
        parts = []
        if state & 0x0004:
            parts.append("<ctrl>")
        if state & (0x0008 | 0x20000):
            parts.append("<alt>")
        if state & 0x0001:
            parts.append("<shift>")
        printable = getattr(event, "char", "") or ""
        if keysym.lower() == "space" or printable == " ":
            key = "space"
        elif printable == "+":
            key = "+"
        elif len(printable) == 1 and printable.isprintable():
            key = printable.lower()
        else:
            key_map = {"Return": "enter", "Escape": "esc", "space": "space", "Tab": "tab", "BackSpace": "backspace", "Delete": "delete", "Prior": "page_up", "Next": "page_down"}
            key = key_map.get(keysym, keysym.lower())
        special = key.startswith("f") and key[1:].isdigit() or key in {"enter", "esc", "space", "tab", "backspace", "delete", "page_up", "page_down", "up", "down", "left", "right", "home", "end"}
        token = f"<{key}>" if special else key
        spec = "+".join(parts + [token])
        # Ctrl+V is reserved by the vision page for direct image paste.  A
        # global pynput binding would otherwise fire at the same time as the
        # Tk clipboard handler (and could start/stop another task while the
        # user is only trying to paste a screenshot).  Reject the conflict at
        # capture time and restore the previous shortcut so the listener can
        # be restarted immediately after the capture finishes.
        if self._is_clipboard_paste_hotkey(spec):
            previous = self.hotkey_capture_previous
            self.hotkey_specs[name] = previous
            self.hotkey_vars[name].set(self.display_hotkey(previous))
            self.hotkey_state_vars[name].set("Ctrl+V 已保留给图片粘贴")
            self.finish_hotkey_capture()
            return "break"
        self.hotkey_specs[name] = spec
        self.hotkey_vars[name].set(self.display_hotkey(spec))
        self.hotkey_state_vars[name].set("已捕获")
        self.refresh_hotkey_tip()
        self.finish_hotkey_capture()
        return "break"

    def finish_hotkey_capture(self):
        name = self.hotkey_capture_target
        self.hotkey_capture_target = None
        if name and name in self.hotkey_entries:
            self.hotkey_entries[name].configure(style="TEntry")
        self.hotkey_apply_status.set("快捷键已更新，请点击应用")
        self.hotkey_ignore_until = time.monotonic() + 0.5
        if not self.closing:
            self.root.after(350, self.start_hotkeys)

    def apply_hotkeys(self):
        specs = [self.normalize_hotkey_spec(self.hotkey_specs.get(name, "")) for name in HOTKEY_DEFAULTS]
        if any(self._is_clipboard_paste_hotkey(spec) for spec in specs):
            self.hotkey_apply_status.set("Ctrl+V 已保留给图片粘贴，请选择其他快捷键")
            self.set_status("快捷键与图片粘贴冲突", "danger")
            return
        nonempty = [s for s in specs if s]
        if len(nonempty) != len(set(nonempty)):
            self.hotkey_apply_status.set("快捷键重复，请为每项设置不同按键")
            self.set_status("快捷键重复", "danger")
            return
        self.hotkey_specs = dict(zip(HOTKEY_DEFAULTS, specs))
        self.refresh_hotkey_tip()
        if self.start_hotkeys():
            self.save_config()
            self.hotkey_apply_status.set("已应用并保存")

    def start_hotkeys(self):
        if self.closing:
            return False
        if keyboard is None:
            self.set_status("未安装 pynput", "warning")
            return False
        specs = {name: self.normalize_hotkey_spec(value) for name, value in self.hotkey_specs.items()}
        conflicting = [name for name, spec in specs.items() if self._is_clipboard_paste_hotkey(spec)]
        if conflicting:
            # Keep the listener usable when a legacy config still contains a
            # Ctrl+V binding.  The capture/apply paths reject new conflicts;
            # this branch is only a defensive migration for direct callers or
            # old settings loaded before the reservation was introduced.
            for name in conflicting:
                specs[name] = ""
                self.hotkey_specs[name] = ""
                if name in getattr(self, "hotkey_vars", {}):
                    self.hotkey_vars[name].set("未设置")
                if name in getattr(self, "hotkey_state_vars", {}):
                    self.hotkey_state_vars[name].set("Ctrl+V 已保留给图片粘贴")
            self.hotkey_apply_status.set("已跳过与图片粘贴冲突的快捷键")
        if len([v for v in specs.values() if v]) != len(set(v for v in specs.values() if v)):
            self.set_status("快捷键重复", "danger")
            return False
        mapping = {}
        callbacks = {"toggle": self.toggle_clicking, "record": self.toggle_recording, "stop": self.stop_all}
        try:
            for name, spec in specs.items():
                if not spec:
                    continue
                keyboard.HotKey.parse(spec)
                callback = callbacks[name]
                mapping[spec] = lambda cb=callback: self._hotkey_callback(cb)
            new_listener = keyboard.GlobalHotKeys(mapping)
            new_listener.start()
        except Exception as exc:
            self.set_status("快捷键格式错误", "danger")
            if self.current_page == "hotkeys":
                self.hotkey_apply_status.set(f"无法应用：{exc}")
            return False
        old = self.hotkey_listener
        self.hotkey_listener = new_listener
        if old:
            old.stop()
            try:
                old.join(timeout=0.5)
            except Exception:
                pass
        self.set_status("快捷键已启用" if not conflicting else "部分快捷键已启用", "success" if not conflicting else "warning")
        return True

    @classmethod
    def _is_clipboard_paste_hotkey(cls, spec: Any) -> bool:
        """Return whether a shortcut contains the Ctrl+V paste combination.

        Tk's ``<Control-KeyPress-v>`` binding also matches extra modifiers
        (for example Ctrl+Shift+V), so every pynput shortcut containing both
        Ctrl and ``v`` would race with the image-paste handler.  Compare the
        canonical token names rather than raw strings to cover persisted
        variants such as ``<ctrl>+<shift>+v``.
        """
        normalized = cls.normalize_hotkey_spec(spec)
        if not normalized:
            return False
        tokens = {
            part.strip().strip("<>").lower()
            for part in normalized.split("+")
            if part.strip().strip("<>")
        }
        return bool(tokens.intersection({"ctrl", "control"}) and "v" in tokens)

    def _hotkey_callback(self, callback: Callable):
        if self.closing or time.monotonic() < self.hotkey_ignore_until:
            return
        self.safe_after(callback)

    # ------------------------------------------------------------- clicking
    def capture_position(self):
        if mouse is None:
            return
        try:
            x, y = mouse.Controller().position
            self.x_var.set(str(x))
            self.y_var.set(str(y))
            self.position_var.set("固定坐标")
            self.update_position_state()
            self.set_status(f"已获取坐标 ({x}, {y})", "success")
        except Exception as exc:
            self.set_status(f"坐标获取失败：{exc}", "danger")

    def parse_click_settings(self):
        try:
            interval_ms = float(self.interval_var.get())
            count = int(self.count_var.get())
            delay = float(self.delay_var.get())
            x, y = int(self.x_var.get()), int(self.y_var.get())
        except (TypeError, ValueError):
            raise ValueError("请检查间隔、次数、延时和坐标格式")
        if not math.isfinite(interval_ms) or interval_ms <= 0:
            raise ValueError("点击间隔必须大于 0 毫秒")
        if count < 0 or delay < 0 or not math.isfinite(delay):
            raise ValueError("次数和开始延时不能为负数")
        return interval_ms / 1000.0, count, delay, x, y

    def toggle_clicking(self):
        if self.running:
            self.stop_clicking()
        else:
            self.start_clicking()

    def start_clicking(self):
        if mouse is None or send_click is None:
            messagebox.showerror("缺少依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        try:
            interval, count, delay, x, y = self.parse_click_settings()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if self.recording:
            self.stop_recording()
        if self.playing:
            self.stop_playback(wait=True)
        self.click_run_id += 1
        run_id = self.click_run_id
        # A fresh Event per run means an old worker can never be revived by a
        # later call to start_clicking after a quick stop/start.
        click_stop_event = threading.Event()
        self.click_stop_event = click_stop_event
        self.running = True
        button_name = {"左键": "left", "右键": "right", "中键": "middle"}.get(self.click_button_var.get(), "left")
        fixed = self.position_var.get() == "固定坐标"
        randomize = bool(self.random_var.get())
        double = self.click_mode_var.get() == "双击"
        self.stat_vars["clicks"].set("0")
        self.stat_vars["elapsed"].set("00:00")
        self.stat_vars["rate"].set(f"{interval * 1000:g} ms")
        self.progress.configure(value=0, maximum=max(1, count))
        self.start_button.configure(text="■  停止连点")
        self.set_status("准备启动…" if delay else "连点中", "warning" if delay else "success")
        settings = (interval, count, delay, x, y, button_name, fixed, randomize, double)
        self.click_thread = threading.Thread(target=self.click_worker, args=(settings, run_id, click_stop_event), name="click-worker", daemon=True)
        try:
            self.click_thread.start()
        except Exception as exc:
            self.running = False
            self.click_thread = None
            self.click_stop_event.set()
            self.start_button.configure(text="▶  开始连点")
            self.set_status(f"连点启动失败：{exc}", "danger")

    def click_worker(self, settings, run_id, stop_event):
        interval, count, delay, x, y, button_name, fixed, randomize, double = settings
        started = time.perf_counter()
        clicks = 0
        error = None
        try:
            controller = mouse.Controller()
            if delay:
                end = time.perf_counter() + delay
                while not stop_event.is_set() and time.perf_counter() < end:
                    remaining = max(0, end - time.perf_counter())
                    self.safe_after(self.update_delay, run_id, remaining)
                    stop_event.wait(min(0.1, remaining))
            next_tick = time.perf_counter()
            while not stop_event.is_set() and (count == 0 or clicks < count):
                if fixed:
                    controller.position = (x, y)
                send_click(button_name)
                if double and not stop_event.wait(0.04):
                    send_click(button_name)
                clicks += 1
                elapsed = int(time.perf_counter() - started)
                self.safe_after(self.update_click_stats, run_id, clicks, elapsed, count)
                next_tick += interval * (random.uniform(0.8, 1.2) if randomize else 1.0)
                stop_event.wait(max(0.0, next_tick - time.perf_counter()))
        except Exception as exc:
            error = str(exc)
        self.safe_after(self.finish_click, run_id, error, clicks)

    def update_delay(self, run_id: int, remaining: float):
        if self.closing or run_id != self.click_run_id:
            return
        self.stat_vars["elapsed"].set(f"开始于 {remaining:.1f}s")

    def update_click_stats(self, run_id: int, clicks: int, elapsed: int, count: int):
        if self.closing or run_id != self.click_run_id:
            return
        self.stat_vars["clicks"].set(f"{clicks:,}")
        self.stat_vars["elapsed"].set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        if count:
            self.progress.configure(value=min(clicks, count), maximum=count)

    def finish_click(self, run_id: int, error: Optional[str], clicks: int):
        if run_id != self.click_run_id:
            return
        self.running = False
        self.click_thread = None
        self.start_button.configure(text="▶  开始连点")
        if error:
            self.set_status(f"连点出错：{error}", "danger")
        elif clicks:
            self.set_status("连点已完成", "success")
        else:
            self.set_status("已停止", "neutral")

    def stop_clicking(self, wait: bool = False):
        self.click_stop_event.set()
        worker = self.click_thread
        self.click_run_id += 1
        was_running = self.running
        self.running = False
        self.start_button.configure(text="▶  开始连点")
        if was_running:
            self.set_status("连点已停止", "neutral")
        if wait and worker and worker is not threading.current_thread():
            worker.join(timeout=0.25)

    # -------------------------------------------------------- recording/replay
    def toggle_recording(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if mouse is None:
            messagebox.showerror("缺少依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        if self.running:
            self.stop_clicking(wait=True)
        if self.playing:
            self.stop_playback(wait=True)
        with self.event_lock:
            self.events = []
        self.refresh_event_tree()
        self.record_include_moves = bool(self.record_include_moves_var.get())
        self.record_session_id += 1
        session_id = self.record_session_id
        self.record_last_move_time = 0.0
        self.record_last_position = None
        self.record_start = time.perf_counter()
        self.recording = True
        self.record_button.configure(text="■  停止录制")
        self.record_status_var.set("录制中… 请在目标窗口操作")
        self.set_status("正在录制", "warning")
        try:
            self.record_listener = mouse.Listener(
                on_move=lambda x, y, sid=session_id: self.on_move(x, y, sid),
                on_click=lambda x, y, button, pressed, sid=session_id: self.on_click(x, y, button, pressed, sid),
            )
            self.record_listener.start()
        except Exception as exc:
            self.recording = False
            self.set_status(f"录制启动失败：{exc}", "danger")

    def on_move(self, x, y, session_id: Optional[int] = None):
        if not self.recording or not self.record_include_moves or (session_id is not None and session_id != self.record_session_id):
            return
        now = time.perf_counter()
        position = (int(x), int(y))
        if now - self.record_last_move_time < 0.03 or position == self.record_last_position:
            return
        self.record_last_move_time = now
        self.record_last_position = position
        self.add_record_event({"type": "move", "t": now - self.record_start, "x": position[0], "y": position[1], "button": "left", "pressed": True}, session_id)

    def on_click(self, x, y, button, pressed, session_id: Optional[int] = None):
        if not self.recording or not pressed or (session_id is not None and session_id != self.record_session_id):
            return
        button_name = getattr(button, "name", str(button))
        self.add_record_event({"type": "click", "t": time.perf_counter() - self.record_start, "x": int(x), "y": int(y), "button": _button_name(button_name), "pressed": True}, session_id)

    def add_record_event(self, event: dict[str, Any], session_id: Optional[int] = None):
        with self.event_lock:
            if not self.recording or (session_id is not None and session_id != self.record_session_id):
                return
            self.events.append(event)
            index = len(self.events)
            current_session = self.record_session_id
        self.safe_after(self.append_event_row, index, event, current_session)

    def append_event_row(self, index: int, event: dict[str, Any], session_id: Optional[int] = None):
        if self.closing:
            return
        if session_id is not None and session_id != self.record_session_id:
            return
        action = "移动" if event["type"] == "move" else "点击"
        button = {"left": "左键", "right": "右键", "middle": "中键"}.get(event.get("button", "left"), "左键") if event["type"] == "click" else "—"
        self.event_tree.insert("", "end", values=(index, f"{event['t']:.2f}s", action, f"{event['x']}, {event['y']}", button))
        items = self.event_tree.get_children()
        if len(items) > 600:
            self.event_tree.delete(items[0])
        self.event_tree.yview_moveto(1.0)
        self.record_status_var.set(f"已录制 {len(self.events):,} 个动作")

    def refresh_event_tree(self):
        if not hasattr(self, "event_tree"):
            return
        self.event_tree.delete(*self.event_tree.get_children())
        for index, event in enumerate(self.events[-600:], start=max(1, len(self.events) - 599)):
            self.append_event_row(index, event, self.record_session_id)

    def stop_recording(self):
        self.recording = False
        self.record_session_id += 1
        listener = self.record_listener
        self.record_listener = None
        if listener:
            try:
                listener.stop()
                listener.join(timeout=1.0)
            except Exception:
                pass
        self.save_recording()
        self.record_button.configure(text="●  开始录制")
        self.record_status_var.set(f"已录制 {len(self.events):,} 个动作并保存")
        self.set_status("录制已保存", "success")

    def clear_recording(self):
        if self.recording:
            self.stop_recording()
        if self.playing:
            self.stop_playback(wait=True)
        with self.event_lock:
            self.events = []
            self.record_session_id += 1
        self.event_tree.delete(*self.event_tree.get_children())
        self.write_json(RECORD_FILE, [])
        self.record_status_var.set("尚未录制动作")
        self.set_status("记录已清空", "neutral")

    def play_recording(self):
        if mouse is None or send_click is None:
            messagebox.showerror("缺少依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        if self.playing:
            self.stop_playback()
            return
        if self.recording:
            self.stop_recording()
        if self.running:
            self.stop_clicking(wait=True)
        with self.event_lock:
            events = []
            for event in self.events:
                try:
                    timestamp = float(event.get("t", 0))
                    if not math.isfinite(timestamp) or timestamp < 0:
                        continue
                    item = dict(event)
                    item["t"] = timestamp
                    item["x"], item["y"] = int(item.get("x", 0)), int(item.get("y", 0))
                    events.append(item)
                except (TypeError, ValueError):
                    continue
        events.sort(key=lambda item: item["t"])
        if not events:
            messagebox.showinfo("没有记录", "请先录制一段鼠标动作")
            return
        try:
            speed = float(self.speed_var.get().lower().replace("x", ""))
            loops = int(self.loop_var.get())
            if not math.isfinite(speed) or speed <= 0 or loops < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("参数错误", "回放速度必须大于 0，循环次数不能为负数")
            return
        self.play_run_id += 1
        run_id = self.play_run_id
        play_stop_event = threading.Event()
        self.play_stop_event = play_stop_event
        self.playing = True
        self.play_button.configure(text="■  回放中…")
        self.set_status("正在回放", "success")
        self.play_thread = threading.Thread(target=self.play_worker, args=(events, speed, loops, run_id, play_stop_event), name="play-worker", daemon=True)
        try:
            self.play_thread.start()
        except Exception as exc:
            self.playing = False
            self.play_thread = None
            play_stop_event.set()
            self.play_button.configure(text="▶  回放动作")
            self.set_status(f"回放启动失败：{exc}", "danger")

    def play_worker(self, events, speed: float, loops: int, run_id: int, stop_event):
        error = None
        completed = 0
        try:
            controller = mouse.Controller()
            while not stop_event.is_set() and (loops == 0 or completed < loops):
                previous = 0.0
                for event in events:
                    if stop_event.wait(max(0.0, (float(event.get("t", 0)) - previous) / speed)):
                        break
                    controller.position = (int(event["x"]), int(event["y"]))
                    if event.get("type") == "click" and event.get("pressed", True):
                        send_click(_button_name(event.get("button", "left")))
                    previous = float(event.get("t", 0))
                else:
                    completed += 1
                    self.safe_after(self.update_play_progress, run_id, completed)
                    continue
                break
        except Exception as exc:
            error = str(exc)
        self.safe_after(self.finish_playback, run_id, error, completed)

    def update_play_progress(self, run_id: int, completed: int):
        if self.closing or run_id != self.play_run_id:
            return
        self.record_status_var.set(f"回放进度：第 {completed} 次")

    def finish_playback(self, run_id: int, error: Optional[str], completed: int):
        if run_id != self.play_run_id:
            return
        self.playing = False
        self.play_thread = None
        self.play_button.configure(text="▶  回放动作")
        if error:
            self.set_status(f"回放出错：{error}", "danger")
        elif completed:
            self.set_status("回放已完成", "success")
        else:
            self.set_status("回放已停止", "neutral")

    def stop_playback(self, wait: bool = False):
        self.play_stop_event.set()
        worker = self.play_thread
        self.play_run_id += 1
        was_playing = self.playing
        self.playing = False
        self.play_button.configure(text="▶  回放动作")
        if was_playing:
            self.set_status("回放已停止", "neutral")
        if wait and worker and worker is not threading.current_thread():
            worker.join(timeout=0.25)

    def stop_all(self):
        self.stop_clicking(wait=True)
        self.stop_playback(wait=True)
        if self.recording:
            self.stop_recording()
        if self.vision_running:
            self.stop_vision()
        self.set_status("全部任务已停止", "neutral")

    def close(self):
        if self.closing:
            return
        self.closing = True
        context_menu = getattr(self, "vision_context_menu", None)
        if context_menu is not None:
            try:
                context_menu.unpost()
                context_menu.destroy()
            except tk.TclError:
                pass
            self.vision_context_menu = None
        preview_job = getattr(self, "_vision_preview_resize_job", None)
        if preview_job is not None:
            try:
                self.root.after_cancel(preview_job)
            except (tk.TclError, ValueError):
                pass
            self._vision_preview_resize_job = None
        preview_window = getattr(self, "_vision_preview_window", None)
        if preview_window is not None:
            try:
                if preview_window.winfo_exists():
                    preview_window.destroy()
            except tk.TclError:
                pass
            self._vision_preview_window = None
        self.click_stop_event.set()
        self.play_stop_event.set()
        self.vision_generation += 1
        if self.vision_engine:
            try:
                self.vision_engine.stop(wait=True)
            except TypeError:
                self.vision_engine.stop()
            except Exception:
                pass
        self.vision_engine = None
        self.vision_running = False
        self.recording = False
        for worker in (self.click_thread, self.play_thread):
            if worker and worker is not threading.current_thread():
                try:
                    worker.join(timeout=0.5)
                except RuntimeError:
                    pass
        if self.record_listener:
            try:
                self.record_listener.stop()
                self.record_listener.join(timeout=0.5)
            except Exception:
                pass
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
                self.hotkey_listener.join(timeout=0.5)
            except Exception:
                pass
        for sequence, binding_id in getattr(self, "_vision_paste_bindings", ()):
            try:
                self.root.unbind(sequence, binding_id)
            except (tk.TclError, RuntimeError):
                pass
        try:
            self.write_json(CONFIG_FILE, {
                "interval_ms": self.interval_var.get(), "count": self.count_var.get(), "button": self.click_button_var.get(),
                "click_mode": self.click_mode_var.get(), "random": self.random_var.get(), "position": self.position_var.get(),
                "x": self.x_var.get(), "y": self.y_var.get(), "delay": self.delay_var.get(),
                "record_include_moves": self.record_include_moves_var.get(), "speed": self.speed_var.get(), "loops": self.loop_var.get(),
                "vision_scan_interval": self.vision_scan_var.get(),
                "vision_templates": [{key: item.get(key) for key in ("path", "name", "threshold", "cooldown", "button", "actions", "click_count", "click_interval", "hold_duration", "enabled", "source")} for item in self.vision_templates],
                "toggle_hotkey": self.hotkey_specs.get("toggle", HOTKEY_DEFAULTS["toggle"]),
                "record_hotkey": self.hotkey_specs.get("record", HOTKEY_DEFAULTS["record"]),
                "stop_hotkey": self.hotkey_specs.get("stop", HOTKEY_DEFAULTS["stop"]),
            })
            self.save_recording()
        except Exception:
            pass
        self.root.destroy()


def main():
    enable_process_dpi_awareness()
    root = tk.Tk()
    ClickerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
