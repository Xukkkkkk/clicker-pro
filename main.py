"""Clicker Pro - a polished Windows auto-clicker and mouse recorder."""
from __future__ import annotations

import ctypes
import json
import math
import os
import random
import shutil
import stat
import threading
import tempfile
import time
import zipfile
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Optional

from theme import (COLORS, THEMES, bind_theme, configure_theme,
                   enable_dark_title_bar, normalise_theme, refresh_theme_widgets)
from ui_assets import Tooltip, symbol_image

try:
    from pynput import keyboard, mouse
except ImportError:  # The UI still opens and explains how to install it.
    keyboard = mouse = None

try:
    from core import (
        capture_window_client, list_windows, post_window_click,
        post_window_mouse_down, post_window_mouse_move, post_window_mouse_up,
        screen_to_client, send_click, send_mouse_down, send_mouse_up,
        window_from_screen_point,
    )
except Exception:  # pragma: no cover - useful on non-Windows development hosts
    send_click = send_mouse_down = send_mouse_up = None
    capture_window_client = list_windows = post_window_click = None
    post_window_mouse_down = post_window_mouse_move = post_window_mouse_up = None
    screen_to_client = window_from_screen_point = None

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
# Portable ``.clickerprofile`` bundles keep copied template images in the
# application's data directory after import.  Keeping these assets outside
# the temporary extraction directory means an imported profile remains usable
# after the source archive is moved or deleted.
PROFILE_ASSET_DIR = DATA_DIR / "profile_assets"
# Keep archive handling deliberately bounded.  These limits protect the UI
# from accidentally importing a huge/corrupt archive while leaving ample room
# for normal screenshots and a sizeable set of templates.
PROFILE_MAX_ASSETS = 256
PROFILE_MAX_ASSET_BYTES = 256 * 1024 * 1024
PROFILE_MAX_SINGLE_ASSET_BYTES = 64 * 1024 * 1024
PROFILE_MAX_JSON_BYTES = 32 * 1024 * 1024
PROFILE_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif",
    ".tif", ".tiff", ".ico", ".jp2", ".pbm", ".pgm", ".ppm", ".pnm",
}
LEGACY_CONFIG_FILE = APP_DIR / "clicker_config.json"
LEGACY_RECORD_FILE = APP_DIR / "clicker_record.json"

HOTKEY_DEFAULTS = {
    "toggle": "<f6>",
    "record": "<f7>",
    "stop": "<f8>",
    "pause": "<f9>",
    "play": "<f10>",
}
HOTKEY_LABELS = {
    "toggle": "开始 / 停止连点",
    "record": "开始 / 停止录制",
    "stop": "停止全部任务",
    "pause": "暂停 / 继续连点",
    "play": "开始 / 停止回放",
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
        self.root.geometry("1120x760")
        self.root.minsize(900, 640)
        initial_config = self.read_json(CONFIG_FILE, None)
        if not isinstance(initial_config, dict):
            initial_config = self.read_json(LEGACY_CONFIG_FILE, {})
        self.theme_name = normalise_theme(
            initial_config.get("theme") if isinstance(initial_config, dict) else None
        )
        self.style = configure_theme(root, self.theme_name)
        enable_dark_title_bar(root)
        self.configure_app_styles()

        self.closing = False
        self.page_frames: dict[str, ttk.Frame] = {}
        self.page_meta = {
            "click": ("连点控制", "鼠标自动化 / 点击任务"),
            "record": ("录制与回放", "鼠标自动化 / 动作序列"),
            "vision": ("图片识别", "视觉自动化 / 目标管理"),
            "hotkeys": ("设置", "偏好设置 / 主题与快捷键"),
        }
        self.current_page = "click"

        # Independent stop signals keep click, replay and recording states
        # from accidentally interrupting one another.
        self.click_stop_event = threading.Event()
        # A set resume event means clicks may run.  Clearing it pauses the
        # current session without discarding its count or starting settings.
        self.click_resume_event = threading.Event()
        self.click_resume_event.set()
        self.play_stop_event = threading.Event()
        self.click_thread: Optional[threading.Thread] = None
        self.play_thread: Optional[threading.Thread] = None
        self.click_run_id = 0
        self.play_run_id = 0
        self.running = False
        self.click_paused = False
        self.playing = False
        self.recording = False
        self.record_listener = None
        self.record_start = 0.0
        self.record_include_moves = True
        self.record_background_targets: list[dict[str, Any]] = []
        self.record_last_move_time = 0.0
        self.record_last_position: Optional[tuple[int, int]] = None
        self.record_session_id = 0
        self.events: list[dict[str, Any]] = []
        self.event_lock = threading.Lock()
        self.background_targets: list[dict[str, Any]] = []
        self._background_capture_job = None

        self.hotkey_listener = None
        self.hotkey_capture_target: Optional[str] = None
        self.hotkey_capture_previous = ""
        self.hotkey_ignore_until = 0.0
        self.hotkey_specs = dict(HOTKEY_DEFAULTS)

        self.vision_templates: list[dict[str, Any]] = []
        self.vision_engine = None
        self.vision_engines: dict[int, Any] = {}
        self.vision_background_targets: list[dict[str, Any]] = []
        self.vision_running = False
        # Do not render imported target pixels inside our own window while
        # scanning; otherwise the matcher can find its own preview and click
        # the configuration UI instead of the real target application.
        self._vision_preview_hidden_for_scan = False
        # Updated on Tk's UI thread; scanner callbacks use this immutable
        # snapshot to ignore matches whose center lies inside Clicker Pro.
        self._vision_window_rect: Optional[tuple[int, int, int, int]] = None
        # Monotonically increasing token used to invalidate callbacks from a
        # scanner that is still finishing after a stop/restart request.
        self.vision_generation = 0
        self._vision_test_running = False
        self._vision_diagnostic_at = 0.0
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
        self.root.bind("<Configure>", self._update_vision_window_rect, add="+")
        self.root.bind("<Map>", self._update_vision_window_rect, add="+")
        self.root.bind("<Unmap>", self._update_vision_window_rect, add="+")
        self._update_vision_window_rect()
        self.load_config()
        self.load_recording()
        self.show_page("click")
        self.start_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI
    def configure_app_styles(self):
        self.style.configure("Page.TFrame", background=COLORS["window"])
        self.style.configure("SidebarText.TLabel", background=COLORS["sidebar"], foreground=COLORS["sidebar_muted"], font=("Microsoft YaHei UI", 9))
        self.style.configure("Brand.TLabel", background=COLORS["sidebar"], foreground=COLORS["sidebar_text"], font=("Segoe UI Semibold", 17))
        self.style.configure("HeroTitle.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 11, "bold"))
        self.style.configure("MetricTitle.TLabel", background=COLORS["surface"], foreground=COLORS["text_muted"], font=("Segoe UI", 8))
        self.style.configure("MetricValue.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 16, "bold"))
        self.style.configure("Capture.TEntry", fieldbackground=COLORS["selection"], background=COLORS["selection"], foreground=COLORS["accent"], bordercolor=COLORS["accent"], lightcolor=COLORS["accent"], darkcolor=COLORS["accent"], padding=(9, 6), font=("Microsoft YaHei UI", 9))
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
        self.style.configure("Icon.TButton", padding=(9, 8), width=3)
        self.style.configure("Mode.TRadiobutton", padding=(10, 6),
                             font=("Microsoft YaHei UI", 9))
        self.style.map("Mode.TRadiobutton", background=[("selected", COLORS["selection"])],
                       foreground=[("selected", COLORS["accent"])])
        if not hasattr(self, "ui_images"):
            self.ui_images = {}
        for name in ("click", "record", "vision", "hotkeys", "import", "export", "clear", "save"):
            color = COLORS["sidebar_text"] if name in {"click", "record", "vision", "hotkeys"} else COLORS["text_secondary"]
            self.ui_images[name] = symbol_image(
                self.root, name, color, existing=self.ui_images.get(name))

    def apply_theme(self, name: str):
        name = normalise_theme(name)
        self.theme_var.set(name)
        if name == self.theme_name:
            return
        self.theme_name = name
        self.style = configure_theme(self.root, name)
        self.configure_app_styles()
        refresh_theme_widgets(self.root)
        self.set_status(getattr(self, "_status_text", "准备就绪"),
                        getattr(self, "_status_tone", "neutral"))

    def change_theme(self):
        self.apply_theme(self.theme_var.get())
        if self.write_json(CONFIG_FILE, self._collect_config()):
            self.theme_status_var.set("已保存，下次启动沿用")
        else:
            self.theme_status_var.set("已切换，但保存失败")

    def build_ui(self):
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        self.sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=194)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.content = ttk.Frame(shell, style="Page.TFrame")
        self.content.pack(side="left", fill="both", expand=True)
        self.build_sidebar()
        self.build_header()
        self.page_host = ttk.Frame(self.content, style="Page.TFrame")
        # Keep a little more horizontal room for the card layouts on compact
        # laptop displays; the cards already provide their own inner padding.
        self.page_host.pack(fill="both", expand=True, padx=24, pady=(4, 12))
        self.build_click_page()
        self.build_record_page()
        self.build_vision_page()
        self.build_hotkey_page()
        self.build_footer()

    def build_sidebar(self):
        brand = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        brand.pack(fill="x", padx=22, pady=(30, 32))
        brand_text = ttk.Frame(brand, style="Sidebar.TFrame")
        brand_text.pack(side="left")
        ttk.Label(brand_text, text="Clicker Pro", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand_text, text="AUTOMATION WORKSPACE", style="SidebarText.TLabel", font=("Segoe UI", 7)).pack(anchor="w", pady=(5, 0))
        ttk.Label(self.sidebar, text="工作区", style="SidebarText.TLabel").pack(anchor="w", padx=22, pady=(0, 8))
        self.nav_buttons: dict[str, ttk.Button] = {}
        for name, label in (("click", "  连点控制"), ("record", "  录制与回放"), ("vision", "  图片识别"), ("hotkeys", "  设置")):
            button = ttk.Button(self.sidebar, text=label, image=self.ui_images[name], compound="left", style="Nav.TButton", command=lambda page=name: self.show_page(page))
            button.pack(fill="x", padx=12, pady=4)
            self.nav_buttons[name] = button
        spacer = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        spacer.pack(fill="both", expand=True)
        tip = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        tip.pack(fill="x", padx=20, pady=(0, 24))
        ttk.Separator(tip).pack(fill="x", pady=(0, 14))
        self.hotkey_tip_var = tk.StringVar(value=self.hotkey_tip_text())
        ttk.Label(tip, textvariable=self.hotkey_tip_var, style="SidebarText.TLabel", justify="left").pack(anchor="w")
        ttk.Label(tip, text="本地工作区", style="SidebarText.TLabel").pack(anchor="w", pady=(18, 0))

    def build_header(self):
        header = ttk.Frame(self.content, style="Page.TFrame")
        header.pack(fill="x", padx=24, pady=(20, 10))
        left = ttk.Frame(header, style="Page.TFrame")
        left.pack(side="left", fill="x", expand=True)
        self.page_title_var = tk.StringVar(value="连点控制")
        self.page_subtitle_var = tk.StringVar(value="调整点击频率、鼠标位置和运行方式")
        ttk.Label(left, textvariable=self.page_title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, textvariable=self.page_subtitle_var, style="Subtitle.TLabel").pack(anchor="w", pady=(5, 0))
        right = ttk.Frame(header, style="Page.TFrame")
        right.pack(side="right")
        self.status_pill = tk.Label(right, text="●  准备就绪", bg=COLORS["surface_hover"], fg=COLORS["text_secondary"], padx=12, pady=6, font=("Segoe UI Semibold", 9))
        self.status_pill.pack(side="left", padx=(0, 10))
        self.header_stop_button = ttk.Button(right, text="停止全部", style="Danger.TButton", command=self.stop_all)
        self.header_stop_button.pack(side="left")
        for name, title, command in (("import", "导入配置", self.import_profile), ("export", "导出配置", self.export_profile)):
            button = ttk.Button(right, image=self.ui_images[name], style="Icon.TButton", command=command)
            button.pack(side="left", padx=(6, 0))
            Tooltip(button, title)

    def build_click_page(self):
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["click"] = page
        hero = ttk.Frame(page, style="Card.TFrame", padding=(0, 4))
        hero.pack(fill="x", pady=(0, 8))
        hero_left = ttk.Frame(hero, style="CardInner.TFrame")
        hero_left.pack(side="left", fill="x", expand=True)
        ttk.Label(hero_left, text="任务配置", style="HeroTitle.TLabel").pack(anchor="w")
        self.pause_button = ttk.Button(
            hero, text="Ⅱ  暂停", style="Compact.TButton",
            command=self.toggle_pause, state="disabled",
        )
        self.pause_button.pack(side="right", padx=(10, 0))
        self.start_button = ttk.Button(hero, text="▶  开始连点", style="Primary.TButton", command=self.toggle_clicking)
        self.start_button.pack(side="right", padx=(18, 0))
        ttk.Separator(page).pack(fill="x", pady=(0, 12))

        body = ttk.Frame(page, style="Page.TFrame")
        body.pack(fill="x")
        # Let the parameter card and the position card use their natural
        # widths; a shared uniform group wastes space on smaller displays.
        body.columnconfigure(0, weight=1, uniform="click-settings")
        body.columnconfigure(1, weight=1, uniform="click-settings")
        params = ttk.Frame(body, style="Card.TFrame", padding=(0, 0, 18, 0))
        params.grid(row=0, column=0, sticky="nsew")
        target = ttk.Frame(body, style="Card.TFrame", padding=(18, 0, 0, 0))
        target.grid(row=0, column=1, sticky="nsew")
        ttk.Label(params, text="点击参数", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        params.columnconfigure(0, weight=1)
        params.columnconfigure(1, weight=1)

        self.interval_var = tk.StringVar(value="100")
        self.count_var = tk.StringVar(value="0")
        self.click_button_var = tk.StringVar(value="左键")
        self.click_mode_var = tk.StringVar(value="单击")
        self.random_var = tk.BooleanVar(value=False)
        self.delay_var = tk.StringVar(value="0")
        self.random_percent_var = tk.StringVar(value="20")
        self.run_duration_var = tk.StringVar(value="0")
        self.random_range_hint_var = tk.StringVar(value="80–120%")
        ttk.Label(params, text="点击间隔", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="点击次数", style="Muted.TLabel").grid(row=1, column=1, sticky="w", padx=14, pady=(0, 6))
        interval_box = ttk.Frame(params, style="CardInner.TFrame")
        interval_box.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        interval_box.columnconfigure(0, weight=1)
        ttk.Entry(interval_box, textvariable=self.interval_var, width=8).grid(row=0, column=0, sticky="ew")
        ttk.Label(interval_box, text="ms", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        count_box = ttk.Frame(params, style="CardInner.TFrame")
        count_box.grid(row=2, column=1, sticky="ew", padx=(14, 0), pady=(0, 12))
        count_box.columnconfigure(0, weight=1)
        ttk.Entry(count_box, textvariable=self.count_var, width=6).grid(row=0, column=0, sticky="ew")
        ttk.Label(count_box, text="0 = 无限", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(params, text="鼠标按键", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="点击方式", style="Muted.TLabel").grid(row=3, column=1, sticky="w", padx=14, pady=(0, 6))
        ttk.Combobox(params, textvariable=self.click_button_var, values=["左键", "右键", "中键"], state="readonly", width=8).grid(row=4, column=0, sticky="ew", pady=(0, 16))
        ttk.Combobox(params, textvariable=self.click_mode_var, values=["单击", "双击"], state="readonly", width=8).grid(row=4, column=1, sticky="ew", padx=(14, 0), pady=(0, 16))
        ttk.Label(params, text="开始前延时", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(0, 6))
        ttk.Label(params, text="随机间隔", style="Muted.TLabel").grid(row=5, column=1, sticky="w", padx=14, pady=(0, 6))
        delay_box = ttk.Frame(params, style="CardInner.TFrame")
        delay_box.grid(row=6, column=0, sticky="ew")
        delay_box.columnconfigure(0, weight=1)
        ttk.Entry(delay_box, textvariable=self.delay_var, width=8).grid(row=0, column=0, sticky="ew")
        ttk.Label(delay_box, text="秒", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))
        random_box = ttk.Frame(params, style="CardInner.TFrame")
        random_box.grid(row=6, column=1, sticky="ew", padx=(14, 0))
        ttk.Checkbutton(random_box, text="±", variable=self.random_var).pack(side="left")
        self.random_percent_entry = ttk.Entry(random_box, textvariable=self.random_percent_var, width=5)
        self.random_percent_entry.pack(side="left", padx=(4, 3))
        ttk.Label(random_box, text="%", style="Muted.TLabel").pack(side="left")
        ttk.Label(params, textvariable=self.random_range_hint_var, style="Hint.TLabel").grid(row=7, column=1, sticky="w", padx=(14, 0), pady=(4, 4))
        self.random_percent_var.trace_add("write", self._update_random_range_hint)
        self.random_var.trace_add("write", self._update_random_range_state)
        self._update_random_range_state()

        ttk.Label(target, text="点击位置", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.position_var = tk.StringVar(value="跟随鼠标当前位置")
        ttk.Radiobutton(target, text="跟随鼠标当前位置", variable=self.position_var, value="跟随鼠标当前位置", command=self.update_position_state).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 7))
        ttk.Radiobutton(target, text="固定坐标", variable=self.position_var, value="固定坐标", command=self.update_position_state).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Radiobutton(target, text="后台窗口（可多选）", variable=self.position_var, value="后台窗口", command=self.update_position_state).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.background_target_button = ttk.Button(
            target, text="选择目标窗口", command=self.open_background_window_selector,
        )
        self.background_target_button.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.background_target_var = tk.StringVar(value="尚未选择后台窗口")
        ttk.Label(
            target, textvariable=self.background_target_var, style="Hint.TLabel",
            wraplength=210,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.x_var, self.y_var = tk.StringVar(value="0"), tk.StringVar(value="0")
        self.restore_cursor_var = tk.BooleanVar(value=False)
        xy = ttk.Frame(target, style="CardInner.TFrame")
        xy.grid(row=6, column=0, columnspan=2, sticky="ew")
        xy.columnconfigure(1, weight=1)
        xy.columnconfigure(3, weight=1)
        ttk.Label(xy, text="X", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 7))
        self.x_entry = ttk.Entry(xy, textvariable=self.x_var, width=8)
        self.x_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(xy, text="Y", style="Muted.TLabel").grid(row=0, column=2, padx=(0, 7))
        self.y_entry = ttk.Entry(xy, textvariable=self.y_var, width=8)
        self.y_entry.grid(row=0, column=3, sticky="ew")
        self.capture_position_button = ttk.Button(
            target, text="⌖  获取当前鼠标坐标", command=self.capture_position,
        )
        self.capture_position_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(params, text="完成后恢复鼠标位置", variable=self.restore_cursor_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        runtime_box = ttk.Frame(params, style="CardInner.TFrame")
        runtime_box.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(runtime_box, text="运行时限", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        ttk.Entry(runtime_box, textvariable=self.run_duration_var, width=8).pack(side="left")
        ttk.Label(runtime_box, text="s", style="Hint.TLabel").pack(side="left", padx=(3, 0))
        ttk.Label(runtime_box, text="0 = 不限", style="Hint.TLabel").pack(side="left", padx=(10, 0))
        target.columnconfigure(0, weight=1)
        target.columnconfigure(1, weight=1)

        ttk.Separator(page).pack(fill="x", pady=(12, 0))
        stats = ttk.Frame(page, style="Card.TFrame", padding=(0, 10))
        stats.pack(fill="x")
        self.stat_vars: dict[str, tk.StringVar] = {}
        for i, (key, title, value) in enumerate((("clicks", "本次点击", "0"), ("elapsed", "运行时长", "00:00"), ("rate", "实际速度", "等待开始"))):
            if i:
                ttk.Separator(stats, orient="vertical").grid(row=0, column=i * 2 - 1, rowspan=2, sticky="ns", padx=18)
            self.stat_vars[key] = tk.StringVar(value=value)
            ttk.Label(stats, text=title, style="MetricTitle.TLabel").grid(row=0, column=i * 2, sticky="w")
            ttk.Label(stats, textvariable=self.stat_vars[key], style="MetricValue.TLabel").grid(row=1, column=i * 2, sticky="w", pady=(3, 0))
            stats.columnconfigure(i * 2, weight=1)
        progress_track = ttk.Frame(stats, height=4)
        progress_track.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        progress_track.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_track, style="Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.progress.pack(fill="both", expand=True)
        self.update_position_state()
        self._update_random_range_hint()

    def build_record_page(self):
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["record"] = page
        toolbar = ttk.Frame(page, style="Card.TFrame", padding=(0, 12))
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
        background_options = ttk.Frame(toolbar, style="CardInner.TFrame")
        background_options.pack(fill="x", pady=(14, 0))
        self.record_background_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            background_options, text="后台录制/回放", variable=self.record_background_var,
            command=self.update_record_background_state,
        ).pack(side="left")
        self.record_background_button = ttk.Button(
            background_options, text="目标窗口", style="Compact.TButton",
            command=lambda: self.open_background_window_selector(set_click_mode=False),
        )
        self.record_background_button.pack(side="left", padx=(8, 0))
        self.update_record_background_state()
        self.record_status_var = tk.StringVar(value="尚未录制动作")
        status_card = ttk.Frame(page, style="Card.TFrame", padding=(0, 8))
        status_card.pack(fill="x", pady=(0, 14))
        ttk.Label(status_card, textvariable=self.record_status_var, style="Count.TLabel").pack(side="left")
        table_card = ttk.Frame(page, style="Card.TFrame", padding=0)
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
        self.record_empty_label = tk.Label(
            self.event_tree, text="暂无录制动作",
            bg=COLORS["input"], fg=COLORS["text_muted"],
            font=("Microsoft YaHei UI", 10), pady=14,
        )
        bind_theme(self.record_empty_label, bg="input", fg="text_muted")
        self.record_empty_label.place(relx=0.5, rely=0.5, anchor="center")

    def build_vision_page(self):
        """Build the multi-template screen recognition workspace."""
        page = ttk.Frame(self.page_host, style="Page.TFrame")
        self.page_frames["vision"] = page

        intro = ttk.Frame(page, style="Card.TFrame", padding=(0, 12))
        intro.pack(fill="x", pady=(0, 14))
        intro_left = ttk.Frame(intro, style="CardInner.TFrame")
        intro_left.pack(side="left", fill="x", expand=True)
        ttk.Label(intro_left, text="识别任务", style="HeroTitle.TLabel").pack(anchor="w")
        self.vision_start_button = ttk.Button(intro, text="▶  开始识别", style="Primary.TButton", command=self.toggle_vision)
        self.vision_start_button.pack(side="right")
        self.vision_paste_button = ttk.Button(intro, text="粘贴图片", style="Compact.TButton", command=self.paste_vision_image)
        self.vision_paste_button.pack(side="right", padx=(0, 8))

        toolbar = ttk.Frame(page, style="Card.TFrame", padding=(0, 8))
        toolbar.pack(fill="x", pady=(0, 14))
        ttk.Button(toolbar, text="＋  添加图片", style="Compact.TButton", command=self.add_vision_images).pack(side="left")
        ttk.Button(toolbar, text="⌁  测试一次", style="Compact.TButton", command=self.scan_vision_once).pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="删除选中", style="Compact.TButton", command=self.remove_vision_template).pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="清空全部", style="Compact.TButton", command=self.clear_vision_templates).pack(side="left", padx=(10, 0))
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=10)
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
        log_bar = ttk.Frame(page, style="Card.TFrame", padding=(0, 8))
        log_bar.pack(fill="x", pady=(0, 14))
        ttk.Label(log_bar, text="识别日志", style="Muted.TLabel").pack(side="left")
        self.vision_background_var = tk.BooleanVar(value=False)
        self.vision_background_button = ttk.Button(
            log_bar, text="目标窗口", style="Compact.TButton",
            command=lambda: self.open_background_window_selector(set_click_mode=False),
        )
        self.vision_background_button.pack(side="right")
        ttk.Checkbutton(
            log_bar, text="后台识别", variable=self.vision_background_var,
            command=self.update_vision_background_state,
        ).pack(side="right", padx=(10, 8))
        vision_log_label = ttk.Label(
            log_bar, textvariable=self.vision_log_var, style="VisionLog.TLabel",
            anchor="w", justify="left", wraplength=360,
        )
        vision_log_label.pack(
            side="left", fill="x", expand=True, padx=(10, 0)
        )
        vision_log_label.bind("<Configure>", lambda event: vision_log_label.configure(
            wraplength=max(80, event.width - 4)))
        self.update_vision_background_state()

        body = ttk.Frame(page, style="Page.TFrame")
        body.pack(fill="both", expand=True)
        # Give the editor a little more room than the list.  The old 3:2
        # split left only about 250 px for the preview at the default window
        # size, which made most screenshots unreadable.  Give the editor the
        # larger share while keeping the target table compact and scrollable.
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=6)
        body.rowconfigure(0, weight=1)

        list_card = ttk.Frame(body, style="Card.TFrame", padding=(0, 8, 10, 0))
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
        self.vision_empty_label = tk.Label(
            self.vision_tree, text="暂无识别目标", bg=COLORS["input"],
            fg=COLORS["text_muted"], font=("Microsoft YaHei UI", 10),
        )
        bind_theme(self.vision_empty_label, bg="input", fg="text_muted")
        self.vision_empty_label.place(relx=0.5, rely=0.5, anchor="center")
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
            activeforeground=COLORS["accent"],
            disabledforeground=COLORS["text_muted"],
            borderwidth=1,
            relief="solid",
            font=("Segoe UI", 9),
        )
        bind_theme(self.vision_context_menu, background="surface_hover", foreground="text",
                   activebackground="selection", activeforeground="accent",
                   disabledforeground="text_muted")
        self.vision_context_menu.add_command(
            label="查看大图", command=self._open_vision_preview
        )
        self.vision_context_menu.add_separator()
        self.vision_context_menu.add_command(
            label="删除选中目标", accelerator="Del",
            command=self.remove_vision_template,
        )

        edit_card = ttk.Frame(body, style="Card.TFrame", padding=(10, 8, 0, 0))
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
        bind_theme(self.vision_editor_canvas, background="surface")
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
        bind_theme(self.vision_preview_frame, bg="input", highlightbackground="border")
        self.vision_preview_frame.pack(fill="x", pady=(12, 4))
        self.vision_preview_frame.pack_propagate(False)
        self.vision_preview_frame.bind("<Configure>", self._resize_vision_preview_panel)
        self.vision_preview_label = tk.Label(
            self.vision_preview_frame, text="未选择图片", anchor="center",
            justify="center", bg=COLORS["input"], fg=COLORS["text_muted"],
            font=("Segoe UI", 10),
        )
        bind_theme(self.vision_preview_label, bg="input", fg="text_muted")
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
        self.vision_max_matches_var = tk.StringVar(value="1")
        self.vision_enabled_var = tk.BooleanVar(value=True)
        self.vision_grayscale_var = tk.BooleanVar(value=False)
        ttk.Label(form, text="每次最多命中", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.vision_max_matches_var, width=10, style="Vision.TEntry").grid(row=2, column=1, sticky="ew", padx=(18, 0), pady=4)
        ttk.Label(form, text="个（1=最佳一个）", style="Hint.TLabel").grid(row=2, column=2, sticky="w", padx=(8, 0))
        ttk.Checkbutton(form, text="启用此目标", variable=self.vision_enabled_var).grid(row=3, column=1, sticky="w", padx=(18, 7), pady=(7, 0))
        ttk.Checkbutton(form, text="灰度匹配（抗颜色变化）", variable=self.vision_grayscale_var).grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(7, 0))

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
        # Kept as a compatibility variable for older integrations/configs.
        # The UI now uses an explicit add button instead of a permanently
        # visible, checkbox-controlled follow-up editor.
        self.vision_followup_var = tk.BooleanVar(value=False)
        self._vision_followup_added = False
        self.vision_followup_kind_var = tk.StringVar(value="点击")
        self.vision_followup_button_var = tk.StringVar(value="右键")
        self.vision_followup_count_var = tk.StringVar(value="1")
        self.vision_followup_interval_var = tk.StringVar(value="80")
        self.vision_followup_hold_var = tk.StringVar(value="0.30")

        ttk.Label(action_box, text="主动作", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.vision_action_kind_combo = ttk.Combobox(
            action_box, textvariable=self.vision_action_kind_var,
            values=["点击", "长按", "等待"], state="readonly", width=7,
        )
        self.vision_action_kind_combo.grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="按键", style="Hint.TLabel").grid(row=1, column=2, sticky="e", pady=2)
        self.vision_action_button_combo = ttk.Combobox(
            action_box, textvariable=self.vision_action_button_var,
            values=["左键", "右键", "中键"], state="readonly", width=7,
        )
        self.vision_action_button_combo.grid(row=1, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="次数", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.vision_action_count_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_count_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_count_entry.grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(action_box, text="间隔 ms", style="Hint.TLabel").grid(row=2, column=2, sticky="e", pady=2)
        self.vision_action_interval_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_interval_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_interval_entry.grid(row=2, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(action_box, text="长按/等待 s", style="Hint.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self.vision_action_hold_entry = ttk.Entry(
            action_box, textvariable=self.vision_action_hold_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_action_hold_entry.grid(row=3, column=1, sticky="ew", padx=(8, 4), pady=2)
        # Follow-up actions are opt-in.  Keeping this row compact is
        # important on laptop displays and, more importantly, avoids stale
        # follow-up values being mistaken for part of a normal click action.
        followup_toolbar = ttk.Frame(action_box, style="CardInner.TFrame")
        followup_toolbar.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(2, 4))
        ttk.Label(followup_toolbar, text="后续动作", style="Hint.TLabel").pack(side="left")
        self.vision_add_action_button = ttk.Button(
            followup_toolbar, text="＋", width=3, style="Compact.TButton",
            command=self._add_vision_followup,
        )
        self.vision_add_action_button.pack(side="right")
        self.vision_followup_frame = ttk.Frame(action_box, style="CardInner.TFrame")
        self.vision_followup_frame.columnconfigure(1, weight=1)
        self.vision_followup_frame.columnconfigure(3, weight=1)
        ttk.Label(self.vision_followup_frame, text="动作", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w", pady=2
        )
        self.vision_followup_kind_combo = ttk.Combobox(
            self.vision_followup_frame, textvariable=self.vision_followup_kind_var,
            values=["点击", "长按", "等待"], state="readonly", width=7,
        )
        self.vision_followup_kind_combo.grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(self.vision_followup_frame, text="按键", style="Hint.TLabel").grid(
            row=0, column=2, sticky="e", pady=2
        )
        self.vision_followup_button_combo = ttk.Combobox(
            self.vision_followup_frame, textvariable=self.vision_followup_button_var,
            values=["左键", "右键", "中键"], state="readonly", width=7,
        )
        self.vision_followup_button_combo.grid(row=0, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(self.vision_followup_frame, text="次数", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self.vision_followup_count_entry = ttk.Entry(
            self.vision_followup_frame, textvariable=self.vision_followup_count_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_count_entry.grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=2)
        ttk.Label(self.vision_followup_frame, text="间隔 ms", style="Hint.TLabel").grid(row=1, column=2, sticky="e", pady=2)
        self.vision_followup_interval_entry = ttk.Entry(
            self.vision_followup_frame, textvariable=self.vision_followup_interval_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_interval_entry.grid(row=1, column=3, sticky="ew", padx=(6, 0), pady=2)
        ttk.Label(self.vision_followup_frame, text="长按/等待 s", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self.vision_followup_hold_entry = ttk.Entry(
            self.vision_followup_frame, textvariable=self.vision_followup_hold_var, width=7,
            style="Vision.TEntry",
        )
        self.vision_followup_hold_entry.grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=2)
        self.vision_followup_frame.grid_remove()
        ttk.Label(action_box, text="动作按同一个识别中心点执行", style="Hint.TLabel").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(4, 0)
        )
        ttk.Button(editor, text="保存目标设置", style="Primary.TButton", command=self.save_vision_selection).pack(fill="x", pady=(12, 0))
        self.vision_action_kind_combo.bind("<<ComboboxSelected>>", self._update_vision_action_state)
        self.vision_followup_kind_combo.bind("<<ComboboxSelected>>", self._update_vision_action_state)
        self._update_vision_action_state()


    def refresh_vision_tree(self):
        if not hasattr(self, "vision_tree"):
            return
        self.vision_tree.delete(*self.vision_tree.get_children())
        if self.vision_templates:
            self.vision_empty_label.place_forget()
        else:
            self.vision_empty_label.place(relx=0.5, rely=0.5, anchor="center")
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
                    "max_matches": 1,
                    "grayscale": False,
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
        if self._vision_preview_hidden_for_scan:
            self.vision_preview_image = None
            label.configure(image="", text="识别运行中，目标预览已隐藏")
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
        if self._vision_preview_hidden_for_scan:
            self.vision_log_var.set("识别运行时不会显示目标图片，避免程序识别并点击自身窗口")
            return "break" if _event is not None else None
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
            bind_theme(window, bg="input")
            window.geometry("720x520")
            window.minsize(320, 240)
            enable_dark_title_bar(window)
            panel = bind_theme(tk.Frame(window), bg="input")
            panel.pack(fill="both", expand=True, padx=10, pady=10)
            image_label = bind_theme(tk.Label(panel, anchor="center"), bg="input", fg="text_muted")
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
        try:
            max_matches = max(1, min(50, int(item.get("max_matches", 1) or 1)))
        except (TypeError, ValueError):
            max_matches = 1
        self.vision_max_matches_var.set(str(max_matches))
        self.vision_button_var.set(self._vision_action_button(item.get("button"), "左键"))
        self.vision_enabled_var.set(bool(item.get("enabled", True)))
        self.vision_grayscale_var.set(bool(item.get("grayscale", False)))
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

    def _add_vision_followup(self):
        """Show the optional follow-up editor after the user presses +."""
        if getattr(self, "_vision_followup_added", False):
            self._remove_vision_followup()
            return
        self._vision_followup_added = True
        self.vision_followup_var.set(True)
        try:
            self.vision_followup_frame.grid(
                row=4, column=0, columnspan=4, sticky="ew", pady=(0, 2)
            )
            self.vision_add_action_button.configure(text="−")
            self._update_vision_action_state()
        except (AttributeError, tk.TclError):
            pass

    def _remove_vision_followup(self):
        """Hide and discard the optional follow-up editor."""
        self._vision_followup_added = False
        self.vision_followup_var.set(False)
        try:
            self.vision_followup_frame.grid_remove()
            self.vision_add_action_button.configure(text="＋")
            self._update_vision_action_state()
        except (AttributeError, tk.TclError):
            pass

    def _update_vision_action_state(self, *_args):
        """Enable only the fields that apply to the selected action kind."""
        try:
            primary_kind = self._vision_action_engine_kind(
                self.vision_action_kind_var.get()
            )
            follow_enabled = bool(getattr(self, "_vision_followup_added", False))
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
            self._vision_followup_added = True
            self.vision_followup_var.set(True)
            self.vision_followup_frame.grid(
                row=4, column=0, columnspan=4, sticky="ew", pady=(0, 2)
            )
            self.vision_add_action_button.configure(text="−")
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
            self._vision_followup_added = False
            self.vision_followup_var.set(False)
            self.vision_followup_frame.grid_remove()
            self.vision_add_action_button.configure(text="＋")
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
            # A disabled hold/wait field may still contain stale text from a
            # previous action. It must not make an ordinary click impossible
            # to save; validate duration only for action kinds that use it.
            duration = (
                parse_float(hold_var.get(), "长按/等待时长")
                if kind in {"hold", "wait"} else 0.0
            )
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
        if getattr(self, "_vision_followup_added", False):
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
            max_matches = int(str(self.vision_max_matches_var.get()).strip())
            if not math.isfinite(threshold) or threshold < 0.5 or threshold > 0.99:
                raise ValueError("阈值范围应为 0.50–0.99")
            if not math.isfinite(cooldown) or cooldown < 0:
                raise ValueError("冷却时间不能为负数")
            if max_matches < 1 or max_matches > 50:
                raise ValueError("每次最多命中范围应为 1-50")
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
            "max_matches": max_matches,
            "grayscale": bool(self.vision_grayscale_var.get()),
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
        # Treeview selection notifications are asynchronous. Re-assert the
        # canonical value immediately so a click action cannot appear to
        # change to another combo-box option while the row refreshes.
        self.vision_action_kind_var.set(
            self._vision_action_ui_kind(first_action.get("kind", "click"))
        )
        self._update_vision_action_state()
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
                max_matches=max(1, min(50, int(item.get("max_matches", 1) or 1))),
                grayscale=bool(item.get("grayscale", False)),
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
        if self._vision_test_running:
            return
        if VisionEngine is None or TemplateSpec is None:
            messagebox.showerror("缺少图片识别依赖", "请先运行：python -m pip install -r requirements.txt")
            return
        if not self.vision_templates:
            messagebox.showinfo("还没有识别目标", "请先添加一张目标图片。")
            return
        if self.vision_running:
            self.stop_vision()
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
        background = bool(self.vision_background_var.get())
        targets: list[dict[str, Any]] = []
        if background:
            if capture_window_client is None:
                messagebox.showerror("不可用", "后台窗口识别仅支持 Windows。")
                return
            try:
                targets = self._resolve_background_targets()
            except Exception as exc:
                messagebox.showerror("窗口读取失败", str(exc))
                return
            if not targets:
                messagebox.showerror("没有可用目标", "请先选择至少一个正在运行的后台窗口。")
                return
        self.vision_generation += 1
        generation = self.vision_generation
        self._vision_test_running = True
        self._update_vision_window_rect()
        self.vision_log_var.set("正在扫描目标窗口…" if background else "正在扫描当前屏幕…")
        def worker():
            try:
                # scan_once intentionally keeps matching other templates when
                # one file fails to decode.  Pass an error callback so a bad
                # image is reported in the visible log instead of looking like
                # a clean no-match scan.
                matches = []
                errors = []
                summaries = []
                scan_targets = targets if background else [None]
                for target in scan_targets:
                    capture_fn = None
                    if target is not None:
                        def capture_fn(_region=None, hwnd=target["hwnd"]):
                            return capture_window_client(hwnd)
                    engine = VisionEngine(
                        specs, interval=interval, auto_click=False,
                        capture_fn=capture_fn,
                        exclude_regions=None if background else self._vision_excluded_regions,
                    )
                    matches.extend(engine.scan_once(trigger=False))
                    summaries.append(engine.scan_summary())
                    if engine.last_error:
                        errors.append(str(engine.last_error))
                self.safe_after(
                    self.show_vision_scan_result, matches,
                    "；".join(errors), background, "；".join(summaries), generation,
                )
            except Exception as exc:
                self.safe_after(self._finish_vision_scan_error, str(exc), generation)
        self._set_vision_preview_hidden(True)
        try:
            threading.Thread(target=worker, name="vision-one-shot", daemon=True).start()
        except Exception as exc:
            self._finish_vision_scan_error(str(exc), generation)

    def show_vision_scan_result(self, matches, scan_error: str = "",
                                background: bool = False, summary: str = "",
                                generation: Optional[int] = None):
        if self.closing or (generation is not None and generation != self.vision_generation):
            return
        self._vision_test_running = False
        self._set_vision_preview_hidden(False)
        if not background:
            matches = [match for match in matches
                       if not self._vision_match_in_own_window(match)]
        # A malformed template is reported by VisionEngine while other
        # templates continue scanning.  Keep that diagnostic visible instead
        # of replacing it with a misleading generic "no match" message.
        if scan_error and not matches:
            self.vision_log_var.set(f"识别错误：{scan_error}")
            self.vision_global_status.set("扫描有错误")
            return
        if not matches:
            self.vision_log_var.set(f"未发现目标；{summary}" if summary else "未发现目标，请确认目标在截图范围内")
            self.vision_global_status.set("未发现目标")
            return
        first = matches[0]
        suffix = f"；另有模板错误：{scan_error}" if scan_error else ""
        self.vision_log_var.set(f"测试命中 {len(matches)} 个目标：{first.name} · 置信度 {first.score:.0%} · ({first.center_x}, {first.center_y}){suffix}")
        self.vision_global_status.set(f"命中 {len(matches)} 个" + (" · 有错误" if scan_error else ""))

    def _finish_vision_scan_error(self, error: str, generation: Optional[int] = None) -> None:
        if self.closing or (generation is not None and generation != self.vision_generation):
            return
        self._vision_test_running = False
        self._set_vision_preview_hidden(False)
        self.on_vision_error(error, generation)

    def _update_vision_window_rect(self, _event=None):
        """Refresh the top-level bounds used to filter self-window matches."""
        try:
            if (not self.root.winfo_exists() or not self.root.winfo_viewable()
                    or self.root.state() in {"iconic", "withdrawn"}):
                self._vision_window_rect = None
                return
            width = int(self.root.winfo_width())
            height = int(self.root.winfo_height())
            if width > 1 and height > 1:
                self._vision_window_rect = (
                    int(self.root.winfo_rootx()), int(self.root.winfo_rooty()),
                    width, height,
                )
        except (AttributeError, tk.TclError, TypeError, ValueError):
            self._vision_window_rect = None

    def _vision_excluded_regions(self):
        rect = self._vision_window_rect
        return (rect,) if rect else ()

    def _vision_match_in_own_window(self, match: Any) -> bool:
        """Return true when a match overlaps our own top-level window.

        Checking only the match centre is not sufficient for larger templates:
        an icon can straddle the edge of Clicker Pro while its centre remains
        on the desktop behind it.  Treat any rectangle intersection as a
        self-match so recognition never clicks pixels belonging to this UI.
        """
        rect = self._vision_window_rect
        if not rect:
            return False
        left, top, width, height = rect
        try:
            x = int(getattr(match, "x"))
            y = int(getattr(match, "y"))
            match_width = max(1, int(getattr(match, "width")))
            match_height = max(1, int(getattr(match, "height")))
        except (AttributeError, TypeError, ValueError):
            # Keep compatibility with lightweight callback test doubles that
            # expose only a centre coordinate.
            try:
                x, y = int(match.center_x), int(match.center_y)
            except (AttributeError, TypeError, ValueError):
                return False
            return left <= x < left + width and top <= y < top + height
        return not (
            x + match_width <= left
            or left + width <= x
            or y + match_height <= top
            or top + height <= y
        )

    def _set_vision_preview_hidden(self, hidden: bool) -> None:
        """Hide target pixels in this app while the desktop is being scanned."""
        self._vision_preview_hidden_for_scan = bool(hidden)
        if hidden:
            context_menu = getattr(self, "vision_context_menu", None)
            if context_menu is not None:
                try:
                    context_menu.unpost()
                except tk.TclError:
                    pass
            preview_window = getattr(self, "_vision_preview_window", None)
            try:
                if preview_window is not None and preview_window.winfo_exists():
                    preview_window.destroy()
            except tk.TclError:
                pass
            self._vision_preview_window = None
        self._render_vision_preview()
        if hidden:
            try:
                # Ensure the old PhotoImage is gone from the compositor before
                # the worker captures its first desktop frame.
                self.root.update_idletasks()
            except tk.TclError:
                pass

    def start_vision(self):
        if self.vision_running:
            return
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
        background = bool(self.vision_background_var.get())
        targets: list[dict[str, Any]] = []
        if background:
            if capture_window_client is None or post_window_click is None:
                messagebox.showerror("不可用", "后台窗口识别仅支持 Windows。")
                return
            try:
                targets = self._resolve_background_targets()
            except Exception as exc:
                messagebox.showerror("窗口读取失败", str(exc))
                return
            if not targets:
                messagebox.showerror("没有可用目标", "请先选择至少一个正在运行的后台窗口。")
                return
        self.vision_generation += 1
        generation = self.vision_generation
        self._vision_test_running = False
        self._vision_diagnostic_at = 0.0
        try:
            self.vision_engines = {}
            self.vision_background_targets = list(targets)
            if background:
                for target in targets:
                    hwnd = int(target["hwnd"])
                    engine = VisionEngine(
                        specs, interval=scan_interval, auto_click=False,
                        capture_fn=lambda _region=None, handle=hwnd: capture_window_client(handle),
                        on_match=lambda result, token=generation, item=target: self.on_vision_match(result, token, item),
                        on_error=lambda error, token=generation: self.on_vision_error(error, token),
                        on_scan=lambda matches, token=generation, handle=hwnd: self.on_vision_scan(matches, token, handle),
                    )
                    self.vision_engines[hwnd] = engine
                self.vision_engine = next(iter(self.vision_engines.values()))
            else:
                self.vision_engine = VisionEngine(
                    specs,
                    interval=scan_interval,
                    auto_click=False,
                    on_match=lambda result, token=generation: self.on_vision_match(result, token),
                    on_error=lambda error, token=generation: self.on_vision_error(error, token),
                    exclude_regions=self._vision_excluded_regions,
                    on_scan=lambda matches, token=generation: self.on_vision_scan(matches, token),
                )
            # Set this before starting the worker: the first scan can happen
            # immediately and should not be discarded by the callback guard.
            self.vision_running = True
            self._update_vision_window_rect()
            self._set_vision_preview_hidden(True)
            engines = list(self.vision_engines.values()) if background else [self.vision_engine]
            for engine in engines:
                engine.start()
        except Exception as exc:
            for engine in self.vision_engines.values():
                try:
                    engine.stop(wait=False)
                except Exception:
                    pass
            self.vision_engines = {}
            self.vision_engine = None
            self.vision_running = False
            self._set_vision_preview_hidden(False)
            messagebox.showerror("识别启动失败", str(exc))
            return
        self.vision_start_button.configure(text="■  停止识别")
        self.vision_global_status.set(
            f"后台识别 · {len(targets)} 窗口" if background else f"识别中 · {len(specs)}"
        )
        self.set_status("后台图片识别中" if background else "图片识别中", "success")

    def stop_vision(self):
        # Invalidate callbacks immediately, even if a slow screen capture
        # keeps the old worker alive for a short time during its join.
        self.vision_generation += 1
        self._vision_test_running = False
        engines = list(self.vision_engines.values())
        if not engines and self.vision_engine is not None:
            engines = [self.vision_engine]
        self.vision_engines = {}
        self.vision_engine = None
        self.vision_running = False
        for engine in engines:
            try:
                engine.stop(wait=True)
            except TypeError:
                engine.stop()
            except Exception:
                pass
        self._set_vision_preview_hidden(False)
        if hasattr(self, "vision_start_button"):
            self.vision_start_button.configure(text="▶  开始识别")
        self.vision_global_status.set("待机")
        self.set_status("图片识别已停止", "neutral")

    def on_vision_scan(self, matches, generation: int, hwnd: Optional[int] = None):
        if self.closing or generation != self.vision_generation or not self.vision_running or matches:
            return
        engine = self.vision_engines.get(hwnd) if hwnd is not None else self.vision_engine
        if engine is None or engine.last_error or time.monotonic() - self._vision_diagnostic_at < 2:
            return
        self._vision_diagnostic_at = time.monotonic()
        self.safe_after(self._vision_no_match_ui, engine.scan_summary(), generation)

    def _vision_no_match_ui(self, summary: str, generation: int):
        if self.closing or generation != self.vision_generation or not self.vision_running:
            return
        self.vision_log_var.set(f"未发现目标；{summary}")
        self.vision_global_status.set("扫描中 · 未命中")

    def on_vision_match(self, result, generation: Optional[int] = None,
                        background_target: Optional[dict[str, Any]] = None):
        """Called from the matcher thread. Move and execute the row's action plan."""
        if self.closing or not self.vision_running:
            return
        if generation is not None and generation != self.vision_generation:
            return
        # The screen matcher intentionally sees the whole desktop. Ignore a
        # template that happens to be present in this app's controls/preview;
        # otherwise it could click its own configuration window.
        if background_target is None and self._vision_match_in_own_window(result):
            return
        try:
            # Capture the engine object before a concurrent stop can clear
            # ``self.vision_engine``. Its event is then still set by
            # stop_vision(), allowing a long press to release promptly.
            if background_target is not None:
                hwnd = int(background_target["hwnd"])
                engine = self.vision_engines.get(hwnd)
            else:
                hwnd = 0
                engine = self.vision_engine
            if engine is None:
                return
            stop_event = engine.stop_event
            actions = getattr(result, "actions", None)
            if not actions:
                actions = [{"kind": "click", "button": getattr(result, "button", "left"), "count": 1}]
            x, y = int(result.center_x), int(result.center_y)
            if background_target is not None:
                if any(fn is None for fn in (
                    post_window_click, post_window_mouse_down,
                    post_window_mouse_move, post_window_mouse_up,
                )):
                    raise RuntimeError("后台鼠标控制不可用")
                def move_fn(px, py):
                    post_window_mouse_move(hwnd, px, py)

                def click_fn(button):
                    post_window_click(hwnd, x, y, button)

                def mouse_down_fn(button):
                    post_window_mouse_down(hwnd, x, y, button)

                def mouse_up_fn(button):
                    post_window_mouse_up(hwnd, x, y, button)
            else:
                if mouse is None or send_click is None:
                    raise RuntimeError("鼠标控制依赖不可用")
                controller = mouse.Controller()
                def move_fn(px, py):
                    controller.position = (px, py)
                click_fn = send_click
                mouse_down_fn = send_mouse_down
                mouse_up_fn = send_mouse_up
            completed = execute_template_actions(
                actions,
                x=x, y=y,
                stop_event=stop_event,
                move_fn=move_fn,
                click_fn=click_fn,
                mouse_down_fn=mouse_down_fn,
                mouse_up_fn=mouse_up_fn,
            ) if execute_template_actions is not None else 0
            name = getattr(result, "name", "目标图片")
            score = float(getattr(result, "score", 0.0))
            self.safe_after(self.vision_match_ui, name, score, x, y, generation, completed)
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
        appearance = ttk.Frame(page, style="Card.TFrame", padding=(12, 10))
        appearance.pack(fill="x", pady=(0, 14))
        ttk.Label(appearance, text="主题颜色", style="HeroTitle.TLabel").pack(side="left", padx=(0, 22))
        self.theme_var = tk.StringVar(value=self.theme_name)
        for name, label in (("dark", "深色"), ("light", "浅色")):
            ttk.Radiobutton(appearance, text=label, value=name, variable=self.theme_var,
                            command=self.change_theme).pack(side="left", padx=(0, 16))
        self.theme_status_var = tk.StringVar(value="立即生效 · 自动保存")
        ttk.Label(appearance, textvariable=self.theme_status_var, style="Hint.TLabel").pack(side="right")
        intro = ttk.Frame(page, style="Card.TFrame", padding=(0, 12))
        intro.pack(fill="x", pady=(0, 14))
        ttk.Label(intro, text="任务快捷键", style="HeroTitle.TLabel").pack(anchor="w")
        card = ttk.Frame(page, style="Card.TFrame", padding=(0, 12))
        card.pack(fill="x", pady=(0, 14))
        card.columnconfigure(1, weight=1)
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        self.hotkey_state_vars: dict[str, tk.StringVar] = {}
        self.hotkey_entries: dict[str, ttk.Entry] = {}
        hotkey_names = tuple(HOTKEY_DEFAULTS)
        for row, name in enumerate(hotkey_names):
            self.hotkey_vars[name] = tk.StringVar(value=self.display_hotkey(HOTKEY_DEFAULTS[name]))
            self.hotkey_state_vars[name] = tk.StringVar(value="点击输入框后按键")
            ttk.Label(card, text=HOTKEY_LABELS[name], style="CardText.TLabel").grid(row=row, column=0, sticky="w", pady=7)
            entry = ttk.Entry(card, textvariable=self.hotkey_vars[name], width=14)
            entry.grid(row=row, column=1, sticky="ew", padx=(28, 10), pady=7)
            entry.bind("<Button-1>", lambda event, key=name: self.arm_hotkey_capture(key))
            entry.bind("<KeyPress>", lambda event, key=name: self.capture_hotkey(event, key))
            self.hotkey_entries[name] = entry
            ttk.Label(card, textvariable=self.hotkey_state_vars[name], style="Hint.TLabel", width=19).grid(row=row, column=2, sticky="e", pady=7)
            clear = ttk.Button(card, image=self.ui_images["clear"], style="Icon.TButton", command=lambda key=name: self.clear_hotkey(key))
            clear.grid(row=row, column=3, padx=(12, 0), pady=7)
            Tooltip(clear, "清除快捷键")
        separator_row = len(hotkey_names)
        ttk.Separator(card).grid(row=separator_row, column=0, columnspan=4, sticky="ew", pady=(10, 14))
        self.hotkey_apply_status = tk.StringVar(value="修改后点击应用，快捷键会立即生效")
        action_row = separator_row + 1
        ttk.Button(card, text="应用快捷键", style="Primary.TButton", command=self.apply_hotkeys).grid(row=action_row, column=0, sticky="w")
        ttk.Button(card, text="保存配置", command=self.save_config).grid(row=action_row, column=1, sticky="w", padx=(12, 0))
        ttk.Label(card, textvariable=self.hotkey_apply_status, style="Hint.TLabel", wraplength=230).grid(row=action_row, column=2, columnspan=2, sticky="e")

    def build_footer(self):
        footer = bind_theme(tk.Frame(self.content, height=30), bg="surface_hover")
        footer.pack(fill="x", side="bottom", before=self.page_host)
        footer.pack_propagate(False)
        self.footer_var = tk.StringVar(value="就绪 · 全局快捷键已启用")
        bind_theme(tk.Label(footer, textvariable=self.footer_var, anchor="w", padx=24,
                            font=("Microsoft YaHei UI", 8)),
                   bg="surface_hover", fg="text_secondary").pack(fill="both")

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
        path = Path(path)
        temp_path = path.with_name(f".{path.name}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
            return True
        except (OSError, TypeError, ValueError):
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            return False

    def load_config(self):
        data = self.read_json(CONFIG_FILE, None)
        if not isinstance(data, dict):
            data = self.read_json(LEGACY_CONFIG_FILE, {})
        if not isinstance(data, dict):
            data = {}
        self._apply_config_data(data)

    def _apply_config_data(self, data: dict[str, Any], *,
                           profile_dir: Optional[Path] = None,
                           replace_templates: bool = False) -> dict[str, int]:
        """Apply a validated config/profile dictionary to the live UI."""
        if not isinstance(data, dict):
            raise ValueError("配置档案内容必须是对象")
        self.apply_theme(data.get("theme", self.theme_name))
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
            self.position_var.set(position if position in {"跟随鼠标当前位置", "固定坐标", "后台窗口"} else "跟随鼠标当前位置")
            self.x_var.set(str(data.get("x", 0)))
            self.y_var.set(str(data.get("y", 0)))
            self.restore_cursor_var.set(bool(data.get("restore_cursor", False)))
            self.delay_var.set(str(data.get("delay", 0)))
            self.random_percent_var.set(str(data.get("random_percent", 20)))
            self.run_duration_var.set(str(data.get("run_duration", 0)))
            mode = data.get("click_mode", "单击")
            self.click_mode_var.set(mode if mode in {"单击", "双击"} else "单击")
            self.record_include_moves_var.set(bool(data.get("record_include_moves", True)))
            self.record_background_var.set(bool(data.get("record_background", False)))
            speed = str(data.get("speed", self.speed_var.get()))
            self.speed_var.set(speed if speed in {"0.5x", "1.0x", "1.5x", "2.0x", "4.0x"} else "1.0x")
            self.loop_var.set(str(data.get("loops", self.loop_var.get())))
            self.vision_scan_var.set(str(data.get("vision_scan_interval", self.vision_scan_var.get())))
            self.vision_background_var.set(bool(data.get("vision_background", False)))
        except (ValueError, TypeError, OverflowError, tk.TclError):
            pass
        raw_background_targets = data.get("background_targets", [])
        if isinstance(raw_background_targets, list):
            self.background_targets = []
            for raw in raw_background_targets[:100]:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title", "")).strip()[:512]
                class_name = str(raw.get("class_name", "")).strip()[:256]
                if title:
                    self.background_targets.append({
                        "hwnd": 0, "title": title, "class_name": class_name,
                    })
            self._refresh_background_target_label()
        old_keys = {
            "toggle": "toggle_hotkey",
            "record": "record_hotkey",
            "stop": "stop_hotkey",
            "pause": "pause_hotkey",
            "play": "play_hotkey",
        }
        conflicting_hotkeys = []
        invalid_hotkeys = []
        loaded_hotkeys = {}
        for name in HOTKEY_DEFAULTS:
            spec = data.get(f"{name}_hotkey", data.get(old_keys.get(name), HOTKEY_DEFAULTS[name]))
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
                if self.hotkey_specs[name] and keyboard is not None:
                    try:
                        keyboard.HotKey.parse(self.hotkey_specs[name])
                    except Exception:
                        invalid_hotkeys.append(name)
                        self.hotkey_specs[name] = HOTKEY_DEFAULTS[name]
            # Do not leave a duplicate mapping in a legacy config: pynput
            # silently keeps only one callback for duplicate dictionary keys.
            loaded = self.hotkey_specs[name]
            if loaded:
                previous = loaded_hotkeys.get(loaded)
                if previous is not None:
                    conflicting_hotkeys.append(name)
                    self.hotkey_specs[name] = ""
                else:
                    loaded_hotkeys[loaded] = name
            self.hotkey_vars[name].set(self.display_hotkey(self.hotkey_specs[name]))
            if self._is_clipboard_paste_hotkey(normalized):
                self.hotkey_state_vars[name].set("Ctrl+V 已保留给图片粘贴")
        if conflicting_hotkeys:
            self.hotkey_apply_status.set("已清除与图片粘贴冲突的快捷键")
        self.refresh_hotkey_tip()
        self.update_position_state()
        self.update_record_background_state()
        self.update_vision_background_state()
        if invalid_hotkeys:
            self.hotkey_apply_status.set("已回退无效快捷键为默认值")
        # A profile import represents a complete snapshot and should clear
        # any existing templates when the profile intentionally contains none.
        # Startup loading keeps the legacy behaviour (missing key means leave
        # the in-memory list alone) for compatibility with old config files.
        vision_data = data.get("vision_templates", [] if replace_templates else None)
        skipped_templates = 0
        if isinstance(vision_data, list):
            self.vision_templates = []
            for raw in vision_data:
                if not isinstance(raw, dict) or not raw.get("path"):
                    skipped_templates += 1
                    continue
                raw_path = Path(str(raw["path"])).expanduser()
                if not raw_path.is_absolute() and profile_dir is not None:
                    raw_path = profile_dir / raw_path
                path = os.path.abspath(str(raw_path))
                if not Path(path).is_file():
                    skipped_templates += 1
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
                    "max_matches": max(1, min(50, int(raw.get("max_matches", 1) or 1))) if str(raw.get("max_matches", 1)).lstrip("-").isdigit() else 1,
                    "grayscale": bool(raw.get("grayscale", False)),
                    "button": self._vision_action_button(raw.get("button"), "左键"),
                    "actions": raw.get("actions") if isinstance(raw.get("actions"), list) else None,
                    "click_count": max(1, int(raw.get("click_count", 1) or 1)) if str(raw.get("click_count", 1)).lstrip("-").isdigit() else 1,
                    "click_interval": max(0.0, float(raw.get("click_interval", 0.08) or 0.08)) if _finite_number(raw.get("click_interval", 0.08)) else 0.08,
                    "hold_duration": max(0.0, float(raw.get("hold_duration", 0.0) or 0.0)) if _finite_number(raw.get("hold_duration", 0.0)) else 0.0,
                    "enabled": bool(raw.get("enabled", True)),
                    "source": raw.get("source", "file") if raw.get("source", "file") in {"file", "clipboard"} else "file",
                })
            self.refresh_vision_tree()
        return {
            "vision_loaded": len(self.vision_templates),
            "vision_skipped": skipped_templates,
        }

    def _collect_config(self) -> dict[str, Any]:
        """Return one canonical settings snapshot for save/export/close."""
        return {
            "schema_version": 4,
            "theme": self.theme_name,
            "interval_ms": self.interval_var.get(), "count": self.count_var.get(), "button": self.click_button_var.get(),
            "click_mode": self.click_mode_var.get(), "random": self.random_var.get(), "position": self.position_var.get(),
            "x": self.x_var.get(), "y": self.y_var.get(), "delay": self.delay_var.get(),
            "restore_cursor": self.restore_cursor_var.get(),
            "background_targets": [
                {"title": str(item.get("title", "")),
                 "class_name": str(item.get("class_name", ""))}
                for item in self.background_targets if item.get("title")
            ],
            "random_percent": self.random_percent_var.get(), "run_duration": self.run_duration_var.get(),
            "record_include_moves": self.record_include_moves_var.get(),
            "record_background": self.record_background_var.get(),
            "speed": self.speed_var.get(), "loops": self.loop_var.get(),
            "vision_scan_interval": self.vision_scan_var.get(),
            "vision_background": self.vision_background_var.get(),
            "vision_templates": [self._serialise_vision_item(item) for item in self.vision_templates],
            "toggle_hotkey": self.hotkey_specs.get("toggle", HOTKEY_DEFAULTS["toggle"]),
            "record_hotkey": self.hotkey_specs.get("record", HOTKEY_DEFAULTS["record"]),
            "stop_hotkey": self.hotkey_specs.get("stop", HOTKEY_DEFAULTS["stop"]),
            "pause_hotkey": self.hotkey_specs.get("pause", HOTKEY_DEFAULTS["pause"]),
            "play_hotkey": self.hotkey_specs.get("play", HOTKEY_DEFAULTS["play"]),
        }

    @staticmethod
    def _serialise_vision_item(item: dict[str, Any]) -> dict[str, Any]:
        """Convert a UI/engine template row to plain JSON-compatible data."""
        keys = ("path", "name", "threshold", "cooldown", "max_matches",
                "grayscale", "button", "actions", "click_count",
                "click_interval", "hold_duration", "enabled", "source")
        result = {key: item.get(key) for key in keys}
        actions = result.get("actions")
        if isinstance(actions, (list, tuple)):
            serialised = []
            for action in actions:
                if isinstance(action, dict):
                    serialised.append(dict(action))
                elif hasattr(action, "as_dict"):
                    try:
                        serialised.append(dict(action.as_dict()))
                    except Exception:
                        continue
                elif hasattr(action, "__dict__"):
                    serialised.append(dict(action.__dict__))
            result["actions"] = serialised
        elif actions is not None:
            result["actions"] = None
        return result

    def save_config(self):
        data = self._collect_config()
        if self.write_json(CONFIG_FILE, data):
            self.set_status("配置已保存", "success")
        else:
            self.set_status("配置保存失败", "danger")

    @staticmethod
    def _normalise_recording_events(data: Any) -> list[dict[str, Any]]:
        """Keep only safe, JSON-friendly mouse events from a profile/file."""
        valid: list[dict[str, Any]] = []
        if not isinstance(data, list):
            return valid
        # A corrupt profile should not be able to allocate an unbounded event
        # list when imported from an external file.
        for event in data[:100000]:
            if not isinstance(event, dict) or event.get("type", event.get("kind")) not in {"move", "click"}:
                continue
            try:
                timestamp = float(event.get("t", 0))
                if not math.isfinite(timestamp) or timestamp < 0:
                    continue
                item = {
                    "type": event.get("type", event.get("kind")), "t": timestamp,
                    "x": int(event.get("x", 0)), "y": int(event.get("y", 0)),
                    "button": _button_name(event.get("button", "left")),
                    "pressed": bool(event.get("pressed", True)),
                }
                if event.get("coordinate_space") == "client":
                    item["coordinate_space"] = "client"
                    item["window_title"] = str(event.get("window_title", ""))[:512]
                    item["window_class"] = str(event.get("window_class", ""))[:256]
                valid.append(item)
            except (TypeError, ValueError, OverflowError):
                continue
        return valid

    @classmethod
    def _validate_profile_settings(cls, data: Any) -> None:
        """Reject malformed profile-wide values before touching the UI."""
        if not isinstance(data, dict):
            raise ValueError("配置档案缺少 settings 对象")
        if "theme" in data and (not isinstance(data["theme"], str) or data["theme"] not in THEMES):
            raise ValueError("主题颜色必须是 dark 或 light")

        def number(key: str, minimum: float = 0.0, maximum: Optional[float] = None):
            if key not in data:
                return
            try:
                value = float(data[key])
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"配置项 {key} 必须是数字")
            if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
                suffix = f"-{maximum:g}" if maximum is not None else f">={minimum:g}"
                raise ValueError(f"配置项 {key} 超出范围（{suffix}）")

        def integer(key: str, minimum: int = 0, maximum: Optional[int] = None):
            if key not in data:
                return
            try:
                value = float(data[key])
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"配置项 {key} 必须是整数")
            if (not math.isfinite(value) or value != int(value)
                    or value < minimum or (maximum is not None and value > maximum)):
                suffix = f"{minimum}-{maximum}" if maximum is not None else f">={minimum}"
                raise ValueError(f"配置项 {key} 超出范围（{suffix}）")

        number("interval_ms", 0.001, 600000.0)
        integer("count", 0, 100000000)
        number("delay", 0, 86400)
        number("random_percent", 0, 90)
        number("run_duration", 0, 86400 * 30)
        integer("loops", 0, 1000000)
        number("vision_scan_interval", 0.03, 60)
        for key, choices in {
            "button": {"左键", "右键", "中键"},
            "click_mode": {"单击", "双击"},
            "position": {"跟随鼠标当前位置", "固定坐标", "后台窗口"},
            "speed": {"0.5x", "1.0x", "1.5x", "2.0x", "4.0x"},
        }.items():
            if key in data and data[key] not in choices:
                raise ValueError(f"配置项 {key} 的值无效")
        background_targets = data.get("background_targets", [])
        if not isinstance(background_targets, list) or len(background_targets) > 100:
            raise ValueError("后台目标窗口列表无效或数量超过 100")
        for target in background_targets:
            if not isinstance(target, dict):
                raise ValueError("后台目标窗口格式无效")
            title = target.get("title", "")
            class_name = target.get("class_name", "")
            if (not isinstance(title, str) or not title.strip() or len(title) > 512
                    or not isinstance(class_name, str) or len(class_name) > 256):
                raise ValueError("后台目标窗口标题或类名无效")
        # Validate shortcuts before touching any live widgets. A malformed
        # profile used to be accepted, saved, and only fail on the next
        # launch, leaving the user without global hotkeys. Empty values are
        # valid (they intentionally disable one shortcut), while non-empty
        # values must be parseable by pynput and must not collide with the
        # Ctrl+V image-paste reservation.
        hotkeys = []
        aliases = {
            "toggle": "toggle_hotkey",
            "record": "record_hotkey",
            "stop": "stop_hotkey",
            "pause": "pause_hotkey",
            "play": "play_hotkey",
        }
        for name, key in aliases.items():
            old_key = f"{name}_hotkey"
            if old_key not in data and key not in data:
                continue
            raw = data.get(old_key, data.get(key))
            normalized = cls.normalize_hotkey_spec(raw)
            if not normalized:
                # None/empty/"未设置" are treated as an explicitly disabled
                # shortcut, matching the UI's Clear action.
                continue
            if cls._is_clipboard_paste_hotkey(normalized):
                raise ValueError("Ctrl+V 已保留给图片粘贴，不能分配给任务快捷键")
            if keyboard is not None:
                try:
                    keyboard.HotKey.parse(normalized)
                except Exception as exc:
                    raise ValueError(f"快捷键 {name} 无法解析：{normalized}") from exc
            hotkeys.append((name, normalized))
        seen_hotkeys = {}
        for name, normalized in hotkeys:
            previous = seen_hotkeys.get(normalized)
            if previous is not None:
                raise ValueError(f"快捷键重复：{previous} 与 {name}")
            seen_hotkeys[normalized] = name
        if "vision_templates" in data and not isinstance(data["vision_templates"], list):
            raise ValueError("配置项 vision_templates 必须是数组")

    @staticmethod
    def _profile_safe_asset_name(original_name: str, index: int,
                                 used: set[str]) -> str:
        """Return a deterministic, filesystem-safe archive member name.

        Archive members are always written below ``assets/`` and never use a
        caller-provided path verbatim.  The index also keeps two source files
        with the same basename distinct without exposing their parent paths.
        """
        source = Path(str(original_name or "template.png"))
        suffix = source.suffix.lower()
        stem = source.stem or "template"
        safe_stem = "".join(
            char if (char.isalnum() or char in {"-", "_", "."}) else "_"
            for char in stem
        ).strip("._")[:80] or "template"
        suffix = suffix if suffix in PROFILE_IMAGE_SUFFIXES else ".img"
        candidate = f"{int(index):03d}_{safe_stem}{suffix}"
        serial = 2
        while candidate.casefold() in used:
            candidate = f"{int(index):03d}_{safe_stem}_{serial}{suffix}"
            serial += 1
        used.add(candidate.casefold())
        return candidate

    def _prepare_profile_bundle(
        self,
    ) -> tuple[dict[str, Any], dict[str, Path], list[str]]:
        """Build an export payload and copy plan for a portable profile.

        The returned settings object is a detached copy of the live config;
        template paths that can be copied are rewritten to ``assets/...``.
        Missing/unsupported/oversized files remain as their original paths so
        exporting a profile never changes the current configuration.
        """
        settings = self._collect_config()
        settings["vision_templates"] = [
            dict(item) for item in settings.get("vision_templates", [])
            if isinstance(item, dict)
        ]
        assets: dict[str, Path] = {}
        source_to_arc: dict[str, str] = {}
        used_names: set[str] = set()
        warnings: list[str] = []
        total_bytes = 0

        for index, item in enumerate(settings["vision_templates"], start=1):
            raw_path = item.get("path")
            try:
                source = Path(str(raw_path or "")).expanduser()
                # Older hand-written profiles sometimes used a path relative
                # to the application directory.  Resolve that form for the
                # copy operation while leaving an unresolvable value intact.
                if not source.is_absolute() and not source.is_file():
                    app_relative = APP_DIR / source
                    if app_relative.is_file():
                        source = app_relative
                source = source.resolve(strict=True)
                size = int(source.stat().st_size)
            except (OSError, RuntimeError, TypeError, ValueError):
                warnings.append("有模板文件不存在或无法读取")
                continue
            if source.suffix.lower() not in PROFILE_IMAGE_SUFFIXES:
                warnings.append("有模板文件格式不支持打包")
                continue
            if size > PROFILE_MAX_SINGLE_ASSET_BYTES:
                warnings.append("有模板文件超过单文件大小限制")
                continue
            source_key = os.path.normcase(str(source))
            arcname = source_to_arc.get(source_key)
            if arcname is None:
                if len(assets) >= PROFILE_MAX_ASSETS:
                    warnings.append("模板数量超过档案上限")
                    continue
                if total_bytes + size > PROFILE_MAX_ASSET_BYTES:
                    warnings.append("模板总大小超过档案上限")
                    continue
                arcname = "assets/" + self._profile_safe_asset_name(
                    source.name, index, used_names
                )
                source_to_arc[source_key] = arcname
                assets[arcname] = source
                total_bytes += size
            item["path"] = arcname

        with self.event_lock:
            recording = list(self.events)
        payload = {
            "format": "clickerpro-profile",
            "schema_version": 2,
            "bundle": "zip",
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": settings,
            "recording": self._normalise_recording_events(recording),
        }
        if warnings:
            # Keep this informational only; import still works for profiles
            # containing a deliberately external path.
            payload["warnings"] = sorted(set(warnings))
        payload["asset_count"] = len(assets)
        return payload, assets, sorted(set(warnings))

    @staticmethod
    def _write_profile_archive(path: Path, payload: dict[str, Any],
                               assets: dict[str, Path]) -> bool:
        """Atomically write a portable ZIP-backed ``.clickerprofile``."""
        target = Path(path)
        temp_path: Optional[Path] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp",
                dir=str(target.parent),
            )
            os.close(fd)
            temp_path = Path(temp_name)
            profile_text = json.dumps(
                payload, ensure_ascii=False, indent=2,
            )
            if len(profile_text.encode("utf-8")) > PROFILE_MAX_JSON_BYTES:
                raise ValueError("档案内容超过大小限制")
            with zipfile.ZipFile(
                temp_path, mode="w", compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.writestr("profile.json", profile_text)
                for arcname, source in assets.items():
                    archive.write(source, arcname)
            os.replace(temp_path, target)
            temp_path = None
            return True
        except (OSError, TypeError, ValueError, zipfile.BadZipFile):
            return False
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _discard_profile_asset_dir(path: Optional[Path]) -> None:
        """Delete only an app-owned temporary profile asset directory."""
        if path is None:
            return
        try:
            root = PROFILE_ASSET_DIR.resolve(strict=False)
            target = Path(path).resolve(strict=False)
            if target == root or root not in target.parents:
                return
            shutil.rmtree(target, ignore_errors=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    @classmethod
    def _read_profile_archive(
        cls, path: Path,
    ) -> tuple[dict[str, Any], Optional[Path], int]:
        """Read a ZIP profile and safely extract its image members.

        Only ``profile.json`` (or a legacy root JSON file) and image files
        below ``assets/`` are considered.  Path traversal and symbolic-link
        members are rejected before any file is written.
        """
        extract_dir: Optional[Path] = None
        try:
            with zipfile.ZipFile(path, mode="r") as archive:
                infos = archive.infolist()
                profile_info = next(
                    (info for info in infos
                     if info.filename.replace("\\", "/") == "profile.json"),
                    None,
                )
                if profile_info is None:
                    # Be liberal for early experimental bundles that used a
                    # different root JSON filename, while still refusing
                    # nested/ambiguous members.
                    candidates = [
                        info for info in infos
                        if "/" not in info.filename.replace("\\", "/")
                        and info.filename.lower().endswith(".json")
                    ]
                    profile_info = candidates[0] if candidates else None
                if profile_info is None:
                    raise ValueError("档案压缩包缺少 profile.json")
                if profile_info.file_size < 0 or profile_info.file_size > PROFILE_MAX_JSON_BYTES:
                    raise ValueError("档案描述文件超过大小限制")
                try:
                    payload = json.loads(
                        archive.read(profile_info).decode("utf-8-sig")
                    )
                except (UnicodeError, ValueError, TypeError) as exc:
                    raise ValueError("档案描述文件不是有效 JSON") from exc
                if not isinstance(payload, dict):
                    raise ValueError("档案描述文件必须是 JSON 对象")

                PROFILE_ASSET_DIR.mkdir(parents=True, exist_ok=True)
                extract_dir = Path(tempfile.mkdtemp(
                    prefix="profile-", dir=str(PROFILE_ASSET_DIR),
                ))
                root_real = os.path.realpath(str(extract_dir))
                seen_members: set[str] = set()
                total_bytes = 0
                asset_count = 0
                for info in infos:
                    member = str(info.filename).replace("\\", "/")
                    if member == profile_info.filename.replace("\\", "/"):
                        continue
                    # Ignore unrelated metadata files, but reject malformed
                    # paths under assets rather than normalising them away.
                    if not member.startswith("assets/"):
                        continue
                    rel = PurePosixPath(member)
                    if (
                        rel.is_absolute()
                        or not rel.parts
                        or rel.parts[0] != "assets"
                        or any(part in {"", ".", ".."} for part in rel.parts)
                    ):
                        raise ValueError("档案资源路径无效")
                    if info.is_dir():
                        # Explicit directory markers such as ``assets/`` are
                        # valid ZIP members; ignore them after path checks.
                        continue
                    if len(rel.parts) < 2:
                        raise ValueError("invalid profile asset path")
                    mode = (int(info.external_attr) >> 16) & 0o170000
                    if stat.S_ISLNK(mode):
                        raise ValueError("档案资源不允许符号链接")
                    key = member.casefold()
                    if key in seen_members:
                        raise ValueError("档案包含重复资源路径")
                    seen_members.add(key)
                    if Path(rel.name).suffix.lower() not in PROFILE_IMAGE_SUFFIXES:
                        # Unknown members cannot be used as templates and are
                        # intentionally ignored instead of being written.
                        continue
                    size = int(info.file_size)
                    if size < 0 or size > PROFILE_MAX_SINGLE_ASSET_BYTES:
                        raise ValueError("档案单个资源超过大小限制")
                    if asset_count >= PROFILE_MAX_ASSETS or total_bytes + size > PROFILE_MAX_ASSET_BYTES:
                        raise ValueError("档案资源总量超过大小限制")
                    destination = extract_dir.joinpath(*rel.parts)
                    destination_real_parent = os.path.realpath(
                        str(destination.parent)
                    )
                    if os.path.commonpath((root_real, destination_real_parent)) != root_real:
                        raise ValueError("档案资源路径越界")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, mode="r") as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    # ZipFile verifies CRC when the source handle closes; the
                    # size check also protects against a dishonest header.
                    actual_size = int(destination.stat().st_size)
                    if actual_size != size:
                        raise ValueError("档案资源大小校验失败")
                    total_bytes += actual_size
                    asset_count += 1
                return payload, extract_dir, asset_count
        except Exception:
            cls._discard_profile_asset_dir(extract_dir)
            raise

    def export_profile(self):
        """Export current settings and recording into a portable JSON profile."""
        path = filedialog.asksaveasfilename(
            title="导出配置档案",
            defaultextension=".clickerprofile",
            filetypes=[("Clicker Pro 档案", "*.clickerprofile"), ("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return False
        target = Path(path)
        if target.suffix.lower() == ".clickerprofile":
            payload, assets, warnings = self._prepare_profile_bundle()
            if not self._write_profile_archive(target, payload, assets):
                messagebox.showerror(
                    "导出失败", "无法写入配置档案，请检查目标路径和磁盘空间。"
                )
                return False
            suffix = f"（{len(assets)} 个模板已打包"
            if warnings:
                suffix += f"，{len(warnings)} 项使用原路径"
            suffix += "）"
            status = f"档案已导出：{target.name}{suffix}"
            self.set_status(status, "success")
            self.hotkey_apply_status.set(status)
            return True
        with self.event_lock:
            events = list(self.events)
        payload = {
            "format": "clickerpro-profile",
            "schema_version": 1,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "settings": self._collect_config(),
            "recording": self._normalise_recording_events(events),
        }
        if not self.write_json(Path(path), payload):
            messagebox.showerror("导出失败", "无法写入配置档案，请检查目标路径。")
            return False
        self.set_status(f"档案已导出：{Path(path).name}", "success")
        self.hotkey_apply_status.set(f"已导出配置档案：{Path(path).name}")
        return True

    def import_profile(self):
        """Import a profile without starting any task automatically."""
        path_text = filedialog.askopenfilename(
            title="导入配置档案",
            filetypes=[("Clicker Pro 档案", "*.clickerprofile *.json"), ("所有文件", "*.*")],
        )
        if not path_text:
            return False
        path = Path(path_text)
        extracted_dir: Optional[Path] = None
        asset_count = 0
        try:
            is_archive = zipfile.is_zipfile(path)
        except OSError:
            is_archive = False
        if is_archive:
            try:
                payload, extracted_dir, asset_count = self._read_profile_archive(path)
            except Exception as exc:
                messagebox.showerror("导入失败", f"无法读取配置档案：{exc}")
                return False
        else:
            payload = self.read_json(path, None)
        if not isinstance(payload, dict):
            self._discard_profile_asset_dir(extracted_dir)
            messagebox.showerror("导入失败", "档案不是有效的 JSON 对象。")
            return False
        profile_format = payload.get("format")
        if profile_format is not None and profile_format != "clickerpro-profile":
            self._discard_profile_asset_dir(extracted_dir)
            messagebox.showerror("导入失败", "这不是 Clicker Pro 配置档案。")
            return False
        settings = payload.get("settings", payload)
        try:
            self._validate_profile_settings(settings)
        except ValueError as exc:
            self._discard_profile_asset_dir(extracted_dir)
            messagebox.showerror("导入失败", str(exc))
            return False
        recording = payload.get("recording", None)
        if recording is not None and not isinstance(recording, list):
            self._discard_profile_asset_dir(extracted_dir)
            messagebox.showerror("导入失败", "档案中的 recording 必须是数组。")
            return False

        # Import is intentionally non-running: a profile should never trigger
        # mouse input merely because it was selected in a file dialog.
        previous_settings = self._collect_config()
        with self.event_lock:
            previous_events = list(self.events)
        self.stop_all()
        try:
            # Relative paths in a ZIP refer to its extracted root.  Legacy
            # JSON profiles continue to resolve relative to the JSON file's
            # directory, preserving the previous behavior.
            result = self._apply_config_data(
                settings, profile_dir=extracted_dir or path.parent,
                replace_templates=True,
            )
            if recording is not None:
                self.events = self._normalise_recording_events(recording)
                self.refresh_event_tree()
                self.save_recording()
            if keyboard is not None and not self.start_hotkeys():
                raise ValueError("配置中的快捷键无法应用")
            self.save_config()
            self._clear_vision_preview("未选择图片")
            try:
                self.vision_tree.selection_remove(self.vision_tree.selection())
            except (AttributeError, tk.TclError):
                pass
        except Exception as exc:
            # Applying a profile is a transaction from the user's point of
            # view. Restore the previous in-memory state if an unexpected
            # widget/path error occurs after validation.
            try:
                self._apply_config_data(previous_settings)
                self.events = previous_events
                self.refresh_event_tree()
                self.start_hotkeys()
            except Exception:
                pass
            self._discard_profile_asset_dir(extracted_dir)
            messagebox.showerror("导入失败", f"应用档案时发生错误：{exc}")
            return False
        skipped = result.get("vision_skipped", 0)
        suffix = f"，跳过 {skipped} 个缺失图片" if skipped else ""
        self.set_status(f"档案已导入：{path.name}{suffix}", "success")
        self.hotkey_apply_status.set(f"已导入配置档案：{path.name}{suffix}")
        return True

    def load_recording(self):
        data = self.read_json(RECORD_FILE, None)
        if not isinstance(data, list):
            data = self.read_json(LEGACY_RECORD_FILE, [])
        valid = self._normalise_recording_events(data)
        self.events = valid
        self.refresh_event_tree()
        if valid:
            self.record_status_var.set(f"已加载 {len(valid)} 个动作")

    def save_recording(self) -> bool:
        with self.event_lock:
            data = list(self.events)
        if not self.write_json(RECORD_FILE, data):
            self.set_status("录制保存失败", "danger")
            return False
        return True

    # -------------------------------------------------------------- status
    def set_status(self, text: str, tone: str = "neutral"):
        self._status_text, self._status_tone = text, tone
        colours = {
            "neutral": (COLORS["surface_hover"], COLORS["text_secondary"]),
            "success": (COLORS["success_surface"], COLORS["success"]),
            "warning": (COLORS["warning_surface"], COLORS["warning"]),
            "danger": (COLORS["danger_surface"], COLORS["danger"]),
        }
        bg, fg = colours.get(tone, colours["neutral"])
        if self.closing:
            return
        try:
            short_text = text if len(text) <= 10 else text[:9] + "…"
            self.status_pill.configure(text=f"●  {short_text}", bg=bg, fg=fg)
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
        mode = self.position_var.get()
        state = "normal" if mode in {"固定坐标", "后台窗口"} else "disabled"
        for entry in (getattr(self, "x_entry", None), getattr(self, "y_entry", None)):
            if entry is not None:
                entry.configure(state=state)
        selector = getattr(self, "background_target_button", None)
        if selector is not None:
            selector.configure(state="normal" if mode == "后台窗口" else "disabled")
        capture = getattr(self, "capture_position_button", None)
        if capture is not None:
            capture.configure(
                text=("⌖  3 秒后拾取窗口与坐标"
                      if mode == "后台窗口" else "⌖  获取当前鼠标坐标")
            )

    def update_record_background_state(self):
        button = getattr(self, "record_background_button", None)
        if button is not None:
            button.configure(
                state="normal" if self.record_background_var.get() else "disabled"
            )

    def update_vision_background_state(self):
        button = getattr(self, "vision_background_button", None)
        if button is not None:
            button.configure(
                state="normal" if self.vision_background_var.get() else "disabled"
            )

    def _refresh_background_target_label(self):
        label = getattr(self, "background_target_var", None)
        if label is None:
            return
        count = len(self.background_targets)
        if not count:
            label.set("尚未选择后台窗口")
        else:
            title = str(self.background_targets[0].get("title", "")).strip()
            title = title if len(title) <= 16 else title[:15] + "…"
            label.set(f"已选 {count} 个：{title}")
        button_text = f"目标窗口 ({count})" if count else "目标窗口"
        for name in ("record_background_button", "vision_background_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(text=button_text)

    def _resolve_background_targets(self) -> list[dict[str, Any]]:
        """Resolve saved window identities to current handles without reuse."""
        if list_windows is None:
            return []
        available = list_windows(exclude_process_id=os.getpid())
        by_hwnd = {int(item.hwnd): item for item in available}
        used: set[int] = set()
        resolved: list[dict[str, Any]] = []
        for saved in self.background_targets:
            title = str(saved.get("title", ""))
            class_name = str(saved.get("class_name", ""))
            hwnd = int(saved.get("hwnd", 0) or 0)
            match = by_hwnd.get(hwnd)
            if (match is None or match.hwnd in used or match.title != title
                    or (class_name and match.class_name != class_name)):
                match = next((
                    item for item in available
                    if item.hwnd not in used and item.title == title
                    and (not class_name or item.class_name == class_name)
                ), None)
            if match is None:
                continue
            used.add(match.hwnd)
            saved["hwnd"] = match.hwnd
            saved["class_name"] = match.class_name
            resolved.append({
                "hwnd": match.hwnd, "title": match.title,
                "class_name": match.class_name,
            })
        return resolved

    def open_background_window_selector(self, *, set_click_mode: bool = True):
        """Open a multi-select list of currently visible top-level windows."""
        if list_windows is None:
            messagebox.showerror("不可用", "后台窗口点击仅支持 Windows。")
            return
        window = tk.Toplevel(self.root)
        window.title("选择后台目标窗口")
        bind_theme(window, bg="window")
        window.geometry("720x430")
        window.minsize(520, 320)
        window.transient(self.root)
        enable_dark_title_bar(window)
        frame = ttk.Frame(window, style="Page.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame, columns=("title", "class", "pid"), show="headings",
            selectmode="extended",
        )
        tree.heading("title", text="窗口标题")
        tree.heading("class", text="窗口类")
        tree.heading("pid", text="PID")
        tree.column("title", width=390, minwidth=180)
        tree.column("class", width=170, minwidth=100)
        tree.column("pid", width=70, minwidth=55, anchor="center")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(frame, style="Page.TFrame")
        toolbar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        status = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=status, style="Muted.TLabel").pack(side="left")
        cache: dict[str, Any] = {}

        def refresh():
            tree.delete(*tree.get_children())
            cache.clear()
            try:
                windows = list_windows(exclude_process_id=os.getpid())
            except Exception as exc:
                status.set(f"读取窗口失败：{exc}")
                return
            remaining = [
                (int(item.get("hwnd", 0) or 0), str(item.get("title", "")),
                 str(item.get("class_name", "")))
                for item in self.background_targets
            ]
            for item in windows:
                iid = str(item.hwnd)
                cache[iid] = item
                tree.insert("", "end", iid=iid, values=(item.title, item.class_name, item.process_id))
                exact = next((entry for entry in remaining if entry[0] == item.hwnd), None)
                identity = exact or next((
                    entry for entry in remaining
                    if entry[1] == item.title and entry[2] == item.class_name
                ), None)
                if identity is not None:
                    tree.selection_add(iid)
                    remaining.remove(identity)
            status.set(f"找到 {len(windows)} 个窗口，可按 Ctrl/Shift 多选")

        def apply_selection():
            selected = [cache[iid] for iid in tree.selection() if iid in cache]
            self.background_targets = [
                {"hwnd": item.hwnd, "title": item.title, "class_name": item.class_name}
                for item in selected
            ]
            if set_click_mode:
                self.position_var.set("后台窗口")
            self._refresh_background_target_label()
            self.update_position_state()
            window.destroy()

        ttk.Button(toolbar, text="刷新", command=refresh).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="取消", command=window.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(toolbar, text="确定", style="Primary.TButton", command=apply_selection).pack(side="right")
        tree.bind("<Double-1>", lambda _event: apply_selection())
        window.bind("<Escape>", lambda _event: window.destroy())
        refresh()
        window.grab_set()
        tree.focus_set()

    def _update_random_range_hint(self, *_args):
        """Show the effective interval range next to the random setting."""
        try:
            percent = float(self.random_percent_var.get())
            if not math.isfinite(percent):
                raise ValueError
            percent = max(0.0, min(90.0, percent))
            lower = max(5.0, 100.0 - percent)
            upper = 100.0 + percent
            lower_text = f"{lower:g}"
            upper_text = f"{upper:g}"
            self.random_range_hint_var.set(f"{lower_text}–{upper_text}%")
        except (AttributeError, tk.TclError, TypeError, ValueError):
            try:
                self.random_range_hint_var.set("范围 0–90%")
            except (AttributeError, tk.TclError):
                pass

    def _update_random_range_state(self, *_args):
        """Disable the percentage field when random timing is turned off."""
        try:
            enabled = bool(self.random_var.get())
            self.random_percent_entry.configure(state="normal" if enabled else "disabled")
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass

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
        labels = (
            ("toggle", "连点开关"),
            ("pause", "暂停 / 继续"),
            ("record", "录制开关"),
            ("play", "回放开关"),
            ("stop", "停止全部"),
        )
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
        if keyboard is not None:
            try:
                for spec in nonempty:
                    keyboard.HotKey.parse(spec)
            except Exception as exc:
                # Keep the current listener and persisted settings intact
                # until every shortcut is known to be parseable.
                self.hotkey_apply_status.set(f"快捷键格式无效：{exc}")
                self.set_status("快捷键格式错误", "danger")
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
        callbacks = {
            "toggle": self.toggle_clicking,
            "record": self.toggle_recording,
            "stop": self.stop_all,
            "pause": self.toggle_pause,
            "play": self.play_recording,
        }
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
        if self.position_var.get() == "后台窗口":
            if window_from_screen_point is None or screen_to_client is None:
                messagebox.showerror("不可用", "后台窗口坐标拾取仅支持 Windows。")
                return
            if self._background_capture_job is not None:
                return
            if self.running:
                self.stop_clicking(wait=True)
            try:
                self._background_capture_previous_state = self.root.state()
                self.set_status("请把鼠标移到目标位置，3 秒后自动拾取", "warning")
                self.root.iconify()
                self._background_capture_job = self.root.after(
                    3000, self._capture_background_position_now
                )
            except (tk.TclError, RuntimeError) as exc:
                self._background_capture_job = None
                self.set_status(f"坐标拾取失败：{exc}", "danger")
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

    def _capture_background_position_now(self):
        self._background_capture_job = None
        try:
            screen_x, screen_y = mouse.Controller().position
            info = window_from_screen_point(int(screen_x), int(screen_y))
            if info is None or info.process_id == os.getpid():
                raise ValueError("鼠标位置下没有可用的目标窗口")
            client_x, client_y = screen_to_client(info.hwnd, int(screen_x), int(screen_y))
            if client_x < 0 or client_y < 0:
                raise ValueError("鼠标需要放在目标窗口的客户区内")
            identity = (info.title, info.class_name)
            existing = next((
                item for item in self.background_targets
                if (str(item.get("title", "")), str(item.get("class_name", ""))) == identity
            ), None)
            if existing is None:
                if len(self.background_targets) >= 100:
                    raise ValueError("后台目标窗口最多可选择 100 个")
                self.background_targets.append({
                    "hwnd": info.hwnd, "title": info.title,
                    "class_name": info.class_name,
                })
            else:
                existing["hwnd"] = info.hwnd
            self.x_var.set(str(client_x))
            self.y_var.set(str(client_y))
            self._refresh_background_target_label()
            self.set_status(
                f"已拾取 {info.title} 的客户区坐标 ({client_x}, {client_y})", "success"
            )
        except Exception as exc:
            self.set_status(f"坐标拾取失败：{exc}", "danger")
        finally:
            if not self.closing:
                try:
                    self.root.deiconify()
                    previous = getattr(self, "_background_capture_previous_state", "normal")
                    if previous == "zoomed":
                        self.root.state("zoomed")
                    self.root.lift()
                except (tk.TclError, RuntimeError):
                    pass

    def parse_click_settings(self, *, include_options: bool = False):
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
        values = (interval_ms / 1000.0, count, delay, x, y)
        if not include_options:
            # Keep the original five-value API available to integrations.
            return values
        try:
            run_duration = float(self.run_duration_var.get())
        except (TypeError, ValueError):
            raise ValueError("最长运行时间必须是数字")
        if self.random_var.get():
            try:
                random_percent = float(self.random_percent_var.get())
            except (TypeError, ValueError):
                raise ValueError("随机范围必须是数字")
            if (not math.isfinite(random_percent) or random_percent < 0
                    or random_percent > 90):
                raise ValueError("随机范围应为 0-90%")
        else:
            random_percent = 0.0
        if not math.isfinite(run_duration) or run_duration < 0 or run_duration > 86400 * 30:
            raise ValueError("最长运行时间范围应为 0-30 天")
        return values + (random_percent, run_duration)

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
            interval, count, delay, x, y, random_percent, run_duration = self.parse_click_settings(
                include_options=True
            )
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if self.recording:
            self.stop_recording()
        if self.playing:
            self.stop_playback(wait=True)
        background = self.position_var.get() == "后台窗口"
        background_targets: list[dict[str, Any]] = []
        if background:
            if post_window_click is None:
                messagebox.showerror("不可用", "后台窗口点击仅支持 Windows。")
                return
            try:
                background_targets = self._resolve_background_targets()
            except Exception as exc:
                messagebox.showerror("窗口读取失败", str(exc))
                return
            if not background_targets:
                messagebox.showerror(
                    "没有可用目标",
                    "请选择至少一个正在运行的后台窗口；已关闭或标题变化的窗口需要重新选择。",
                )
                return
        self.click_run_id += 1
        run_id = self.click_run_id
        # A fresh Event per run means an old worker can never be revived by a
        # later call to start_clicking after a quick stop/start.
        click_stop_event = threading.Event()
        self.click_stop_event = click_stop_event
        self.click_resume_event.set()
        self.click_paused = False
        self.running = True
        button_name = {"左键": "left", "右键": "right", "中键": "middle"}.get(self.click_button_var.get(), "left")
        fixed = self.position_var.get() == "固定坐标"
        randomize = bool(self.random_var.get())
        double = self.click_mode_var.get() == "双击"
        restore_cursor = bool(self.restore_cursor_var.get()) and fixed and not background
        original_position = None
        if restore_cursor:
            try:
                original_position = tuple(mouse.Controller().position)
            except Exception:
                original_position = None
        self.stat_vars["clicks"].set("0")
        self.stat_vars["elapsed"].set("00:00")
        rate_text = f"{interval * 1000:g} ms"
        if randomize and random_percent > 0:
            rate_text += f" (±{random_percent:g}%)"
        self.stat_vars["rate"].set(rate_text)
        self.progress.configure(value=0, maximum=max(1, count))
        self.pause_button.configure(text="Ⅱ  暂停", state="normal")
        self.start_button.configure(text="■  停止连点")
        active_text = f"后台连点中（{len(background_targets)} 个窗口）" if background else "连点中"
        self.set_status("准备启动…" if delay else active_text, "warning" if delay else "success")
        settings = (
            interval, count, delay, x, y, button_name, fixed, randomize, double,
            random_percent / 100.0, run_duration, restore_cursor, original_position,
            background, background_targets,
        )
        self.click_thread = threading.Thread(target=self.click_worker, args=(settings, run_id, click_stop_event), name="click-worker", daemon=True)
        try:
            self.click_thread.start()
        except Exception as exc:
            self.running = False
            self.click_thread = None
            self.click_stop_event.set()
            self.click_resume_event.set()
            self.click_paused = False
            self.start_button.configure(text="▶  开始连点")
            self.pause_button.configure(text="Ⅱ  暂停", state="disabled")
            self.set_status(f"连点启动失败：{exc}", "danger")

    def click_worker(self, settings, run_id, stop_event):
        if len(settings) >= 15:
            (
                interval, count, delay, x, y, button_name, fixed, randomize, double,
                random_fraction, run_duration, restore_cursor, original_position,
                background, background_targets,
            ) = settings
        elif len(settings) >= 13:
            (
                interval, count, delay, x, y, button_name, fixed, randomize, double,
                random_fraction, run_duration, restore_cursor, original_position,
            ) = settings
            background, background_targets = False, []
        elif len(settings) == 12:
            (
                interval, count, delay, x, y, button_name, fixed, randomize, double,
                random_fraction, run_duration, restore_cursor,
            ) = settings
            original_position = None
            background, background_targets = False, []
        elif len(settings) >= 11:
            # Compatibility with callers that still pass the pre-options
            # tuple (interval, count, delay, x, y, button, fixed, random,
            # double, random_fraction, run_duration).
            (
                interval, count, delay, x, y, button_name, fixed, randomize, double,
                random_fraction, run_duration,
            ) = settings
            restore_cursor = False
            original_position = None
            background, background_targets = False, []
        else:
            (
                interval, count, delay, x, y, button_name, fixed, randomize, double,
            ) = settings
            random_fraction = 0.2
            run_duration = 0.0
            restore_cursor = False
            original_position = None
            background, background_targets = False, []
        started = time.perf_counter()
        deadline = None
        clicks = 0
        error = None
        limit_reached = False
        controller = None
        try:
            controller = mouse.Controller()
            if delay:
                end = time.perf_counter() + delay
                while not stop_event.is_set() and time.perf_counter() < end:
                    if not self.click_resume_event.is_set():
                        pause_started = time.perf_counter()
                        if not self._wait_click_resume(stop_event, run_id):
                            break
                        paused_for = time.perf_counter() - pause_started
                        end += paused_for
                        if deadline is not None:
                            deadline += paused_for
                    if not self._wait_click_resume(stop_event, run_id):
                        break
                    remaining = max(0, end - time.perf_counter())
                    self.safe_after(self.update_delay, run_id, remaining)
                    stop_event.wait(min(0.1, remaining))
            # The user-facing runtime budget starts after the optional
            # countdown, so a five-second task still gets five seconds of
            # clicking even when a separate start delay was configured.
            started = time.perf_counter()
            deadline = started + run_duration if run_duration > 0 else None
            next_tick = time.perf_counter()
            while not stop_event.is_set() and (count == 0 or clicks < count):
                if not self.click_resume_event.is_set():
                    pause_started = time.perf_counter()
                    if not self._wait_click_resume(stop_event, run_id):
                        break
                    # A pause is a true pause: extend the deadline by the
                    # time spent waiting so users can safely inspect the
                    # target without losing their remaining run budget.
                    if deadline is not None:
                        deadline += time.perf_counter() - pause_started
                    next_tick = time.perf_counter()
                if not self._wait_click_resume(stop_event, run_id):
                    break
                if not self.click_resume_event.is_set():
                    continue
                if deadline is not None and time.perf_counter() >= deadline:
                    limit_reached = True
                    break
                if background:
                    successful_targets = []
                    last_target_error = None
                    for target in background_targets:
                        try:
                            post_window_click(target["hwnd"], x, y, button_name)
                            successful_targets.append(target)
                        except Exception as exc:
                            last_target_error = exc
                    background_targets = successful_targets
                    if not background_targets:
                        detail = f"：{last_target_error}" if last_target_error else ""
                        raise OSError(f"所有后台目标窗口均已关闭或坐标不可用{detail}")
                    if (double and not stop_event.wait(0.04)
                            and (deadline is None or time.perf_counter() < deadline)):
                        double_targets = []
                        for target in background_targets:
                            try:
                                post_window_click(
                                    target["hwnd"], x, y, button_name,
                                    double_click=True,
                                )
                                double_targets.append(target)
                            except Exception:
                                pass
                        background_targets = double_targets
                        if not background_targets:
                            raise OSError("后台目标窗口在双击过程中失效")
                else:
                    if fixed:
                        controller.position = (x, y)
                    send_click(button_name)
                    if (double and not stop_event.wait(0.04)
                            and (deadline is None or time.perf_counter() < deadline)):
                        send_click(button_name)
                clicks += 1
                elapsed = time.perf_counter() - started
                remaining = (max(0.0, deadline - time.perf_counter())
                             if deadline is not None else None)
                self.safe_after(self.update_click_stats, run_id, clicks, elapsed, count, remaining)
                factor = random.uniform(
                    max(0.05, 1.0 - random_fraction),
                    min(2.0, 1.0 + random_fraction),
                ) if randomize and random_fraction > 0 else 1.0
                # Schedule from the completed click instead of accumulating
                # against an old deadline.  If OS scheduling or a slow target
                # makes one click late, this prevents a burst of catch-up
                # clicks that users often perceive as an unsafe spike.
                next_tick = time.perf_counter() + interval * factor
                wait_for = max(0.0, next_tick - time.perf_counter())
                if deadline is not None:
                    wait_for = min(wait_for, max(0.0, deadline - time.perf_counter()))
                wait_end = time.perf_counter() + wait_for
                while not stop_event.is_set() and time.perf_counter() < wait_end:
                    if not self.click_resume_event.is_set():
                        # Let the top of the loop account for the paused
                        # duration and reset the next tick on resume.
                        break
                    now = time.perf_counter()
                    if deadline is not None:
                        self.safe_after(
                            self.update_click_stats, run_id, clicks,
                            now - started, count,
                            max(0.0, deadline - now),
                        )
                    if stop_event.wait(min(0.1, max(0.0, wait_end - now))):
                        break
                if (not stop_event.is_set() and deadline is not None
                        and time.perf_counter() >= deadline
                        and (count == 0 or clicks < count)):
                    limit_reached = True
        except Exception as exc:
            error = str(exc)
        finally:
            if restore_cursor and controller is not None and original_position is not None:
                try:
                    controller.position = original_position
                except Exception:
                    # Restoring the cursor is best-effort; never hide the
                    # actual click error or leave the worker unreported.
                    pass
        self.safe_after(self.finish_click, run_id, error, clicks, limit_reached)

    def _wait_click_resume(self, stop_event: threading.Event, run_id: int) -> bool:
        """Block a click worker while paused, while still honoring stop."""
        if self.click_resume_event.is_set():
            return not stop_event.is_set()
        self.safe_after(self.update_pause_ui, run_id, True)
        while not stop_event.is_set():
            if self.click_resume_event.wait(0.05):
                self.safe_after(self.update_pause_ui, run_id, False)
                return True
        return False

    def update_pause_ui(self, run_id: int, paused: bool):
        if self.closing or run_id != self.click_run_id:
            return
        self.click_paused = bool(paused)
        try:
            self.pause_button.configure(
                text="▶  继续" if paused else "Ⅱ  暂停",
                state="normal" if self.running else "disabled",
            )
        except (AttributeError, tk.TclError):
            pass

    def toggle_pause(self):
        """Pause or resume the active click session without resetting it."""
        if not self.running:
            return
        if self.click_paused or not self.click_resume_event.is_set():
            self.click_resume_event.set()
            self.click_paused = False
            self.pause_button.configure(text="Ⅱ  暂停", state="normal")
            self.set_status("连点已继续", "success")
        else:
            self.click_resume_event.clear()
            self.click_paused = True
            self.pause_button.configure(text="▶  继续", state="normal")
            self.set_status("连点已暂停（再次点击或按暂停热键继续）", "warning")

    def update_delay(self, run_id: int, remaining: float):
        if self.closing or run_id != self.click_run_id:
            return
        self.stat_vars["elapsed"].set(f"开始于 {remaining:.1f}s")

    def update_click_stats(self, run_id: int, clicks: int, elapsed: float, count: int,
                           remaining: Optional[float] = None):
        if self.closing or run_id != self.click_run_id:
            return
        self.stat_vars["clicks"].set(f"{clicks:,}")
        elapsed_seconds = max(0.0, float(elapsed))
        elapsed_whole = int(elapsed_seconds)
        elapsed_text = f"{elapsed_whole // 60:02d}:{elapsed_whole % 60:02d}"
        if remaining is not None:
            elapsed_text += f" · 剩余 {remaining:.1f}s"
        self.stat_vars["elapsed"].set(elapsed_text)
        if elapsed_seconds > 0:
            self.stat_vars["rate"].set(f"{clicks / elapsed_seconds:.1f} CPS")
        if count:
            self.progress.configure(value=min(clicks, count), maximum=count)

    def finish_click(self, run_id: int, error: Optional[str], clicks: int,
                     limit_reached: bool = False):
        if run_id != self.click_run_id:
            return
        self.running = False
        self.click_resume_event.set()
        self.click_paused = False
        self.click_thread = None
        self.start_button.configure(text="▶  开始连点")
        self.pause_button.configure(text="Ⅱ  暂停", state="disabled")
        if error:
            self.set_status(f"连点出错：{error}", "danger")
        elif limit_reached:
            self.set_status(f"已达到时长上限，自动停止（{clicks:,} 次）", "success")
        elif clicks:
            self.set_status("连点已完成", "success")
        else:
            self.set_status("已停止", "neutral")

    def stop_clicking(self, wait: bool = False):
        self.click_stop_event.set()
        self.click_resume_event.set()
        worker = self.click_thread
        self.click_run_id += 1
        was_running = self.running
        self.running = False
        self.click_paused = False
        self.start_button.configure(text="▶  开始连点")
        try:
            self.pause_button.configure(text="Ⅱ  暂停", state="disabled")
        except (AttributeError, tk.TclError):
            pass
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
        self.record_background = bool(self.record_background_var.get())
        self.record_background_targets = []
        if self.record_background:
            try:
                self.record_background_targets = self._resolve_background_targets()
            except Exception as exc:
                messagebox.showerror("窗口读取失败", str(exc))
                return
            if not self.record_background_targets:
                messagebox.showerror("没有可用目标", "请先选择至少一个正在运行的后台窗口。")
                return
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
        self.record_status_var.set(
            f"后台录制中… 仅记录所选窗口（{len(self.record_background_targets)} 个）"
            if self.record_background else "录制中… 请在目标窗口操作"
        )
        self.set_status("正在录制", "warning")

        # pynput 1.8+ passes an extra injected flag. Keep the session ID in
        # the closure: a positional default would be overwritten by that
        # flag and cause real mouse events to be rejected as stale.
        # The optional flag also supports the callbacks used by pynput 1.7.
        def on_move(x, y, injected=False):
            self.on_move(x, y, session_id)

        def on_click(x, y, button, pressed, injected=False):
            self.on_click(x, y, button, pressed, session_id)

        try:
            self.record_listener = mouse.Listener(
                on_move=on_move,
                on_click=on_click,
            )
            self.record_listener.start()
        except Exception as exc:
            with self.event_lock:
                self.recording = False
                self.record_session_id += 1
            listener = self.record_listener
            self.record_listener = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
            self.record_background_targets = []
            self.record_button.configure(text="●  开始录制")
            self.record_status_var.set(f"录制启动失败：{exc}")
            self.set_status(f"录制启动失败：{exc}", "danger")

    def on_move(self, x, y, session_id: Optional[int] = None):
        if not self.recording or not self.record_include_moves or (session_id is not None and session_id != self.record_session_id):
            return
        now = time.perf_counter()
        converted = self._record_event_position(int(x), int(y))
        if converted is None:
            return
        position = (converted[0], converted[1])
        if now - self.record_last_move_time < 0.03 or position == self.record_last_position:
            return
        self.record_last_move_time = now
        self.record_last_position = position
        event = {"type": "move", "t": now - self.record_start,
                 "x": position[0], "y": position[1], "button": "left",
                 "pressed": True}
        event.update(converted[2])
        self.add_record_event(event, session_id)

    def on_click(self, x, y, button, pressed, session_id: Optional[int] = None):
        if not self.recording or not pressed or (session_id is not None and session_id != self.record_session_id):
            return
        converted = self._record_event_position(int(x), int(y))
        if converted is None:
            return
        button_name = getattr(button, "name", str(button))
        event = {
            "type": "click", "t": time.perf_counter() - self.record_start,
            "x": converted[0], "y": converted[1],
            "button": _button_name(button_name), "pressed": True,
        }
        event.update(converted[2])
        self.add_record_event(event, session_id)

    def _record_event_position(self, x: int, y: int):
        """Return event coordinates and metadata for the active record mode."""
        if not getattr(self, "record_background", False):
            return int(x), int(y), {}
        if window_from_screen_point is None or screen_to_client is None:
            return None
        try:
            info = window_from_screen_point(int(x), int(y))
            if info is None:
                return None
            target = next((
                item for item in self.record_background_targets
                if int(item.get("hwnd", 0) or 0) == info.hwnd
            ), None)
            if target is None:
                return None
            client_x, client_y = screen_to_client(info.hwnd, int(x), int(y))
            if client_x < 0 or client_y < 0:
                return None
            return client_x, client_y, {
                "coordinate_space": "client",
                "window_title": info.title,
                "window_class": info.class_name,
            }
        except Exception:
            return None

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
        self.record_empty_label.place_forget()
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
        if not self.events:
            self.record_empty_label.place(relx=0.5, rely=0.5, anchor="center")
        for index, event in enumerate(self.events[-600:], start=max(1, len(self.events) - 599)):
            self.append_event_row(index, event, self.record_session_id)

    def stop_recording(self):
        with self.event_lock:
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
        self.record_background_targets = []
        saved = self.save_recording()
        self.record_button.configure(text="●  开始录制")
        # Stopping invalidates queued row callbacks; rebuild from the final
        # snapshot so a quick Stop still shows every recorded action.
        self.refresh_event_tree()
        count = len(self.events)
        if not saved:
            self.record_status_var.set(f"已录制 {count:,} 个动作，保存失败（动作仍在内存中）")
            self.set_status(f"录制保存失败，请检查目录权限和磁盘空间：{RECORD_FILE.parent}", "danger")
        elif not count:
            self.record_status_var.set("未录制到动作，请在目标窗口点击后重试")
            self.set_status("未录制到动作", "warning")
        else:
            self.record_status_var.set(f"已录制 {count:,} 个动作并保存")
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
        self.record_empty_label.place(relx=0.5, rely=0.5, anchor="center")
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
        background = bool(self.record_background_var.get())
        background_targets: list[dict[str, Any]] = []
        if background:
            try:
                background_targets = self._resolve_background_targets()
            except Exception as exc:
                messagebox.showerror("窗口读取失败", str(exc))
                return
            if not background_targets:
                messagebox.showerror("没有可用目标", "请先选择至少一个正在运行的后台窗口。")
                return
            # Legacy recordings used screen coordinates. Treat the first
            # selected window as their source so existing recordings can be
            # migrated into background playback without editing the file.
            source_hwnd = background_targets[0]["hwnd"]
            try:
                for event in events:
                    if event.get("coordinate_space") != "client":
                        event["x"], event["y"] = screen_to_client(
                            source_hwnd, int(event["x"]), int(event["y"])
                        )
                        event["coordinate_space"] = "client"
            except Exception as exc:
                messagebox.showerror("坐标转换失败", str(exc))
                return
        elif any(event.get("coordinate_space") == "client" for event in events):
            messagebox.showerror("坐标模式不匹配", "这段记录使用窗口客户区坐标，请开启后台录制/回放。")
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
        self.set_status(
            f"正在后台回放（{len(background_targets)} 个窗口）" if background else "正在回放",
            "success",
        )
        self.play_thread = threading.Thread(
            target=self.play_worker,
            args=(events, speed, loops, run_id, play_stop_event,
                  background, background_targets),
            name="play-worker", daemon=True,
        )
        try:
            self.play_thread.start()
        except Exception as exc:
            self.playing = False
            self.play_thread = None
            play_stop_event.set()
            self.play_button.configure(text="▶  回放动作")
            self.set_status(f"回放启动失败：{exc}", "danger")

    def play_worker(self, events, speed: float, loops: int, run_id: int, stop_event,
                    background: bool = False, background_targets=None):
        error = None
        completed = 0
        background_targets = list(background_targets or [])
        try:
            controller = None if background else mouse.Controller()
            while not stop_event.is_set() and (loops == 0 or completed < loops):
                previous = 0.0
                for event in events:
                    if stop_event.wait(max(0.0, (float(event.get("t", 0)) - previous) / speed)):
                        break
                    x, y = int(event["x"]), int(event["y"])
                    if background:
                        delivered = 0
                        for target in background_targets:
                            try:
                                if event.get("type") == "click" and event.get("pressed", True):
                                    post_window_click(
                                        target["hwnd"], x, y,
                                        _button_name(event.get("button", "left")),
                                    )
                                else:
                                    post_window_mouse_move(target["hwnd"], x, y)
                                delivered += 1
                            except Exception:
                                continue
                        if not delivered and event.get("type") == "click":
                            raise OSError("没有后台目标窗口能接收录制动作")
                    else:
                        controller.position = (x, y)
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
        if self.vision_running:
            self.stop_vision()
        if self.recording:
            # Keep the recording's save result visible for the Stop button
            # and F8, especially when the file could not be written.
            self.stop_recording()
        else:
            self.set_status("全部任务已停止", "neutral")

    def close(self):
        if self.closing:
            return
        self.closing = True
        background_capture_job = getattr(self, "_background_capture_job", None)
        if background_capture_job is not None:
            try:
                self.root.after_cancel(background_capture_job)
            except (tk.TclError, ValueError):
                pass
            self._background_capture_job = None
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
        self.click_resume_event.set()
        self.play_stop_event.set()
        self.vision_generation += 1
        vision_engines = list(self.vision_engines.values())
        if not vision_engines and self.vision_engine is not None:
            vision_engines = [self.vision_engine]
        self.vision_engines = {}
        for engine in vision_engines:
            try:
                engine.stop(wait=True)
            except TypeError:
                engine.stop()
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
            self.write_json(CONFIG_FILE, self._collect_config())
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
