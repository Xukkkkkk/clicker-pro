"""Reusable Tkinter page for Clicker Pro's image recognition mode.

The page deliberately keeps image matching out of the UI.  It owns the list of
templates and emits plain dictionaries through callbacks, so it can be used
with :class:`vision_engine.VisionEngine` (or another matcher) without making
the main window depend on OpenCV/Pillow.  The backend can call ``post_match``
and ``post_cycle`` from a worker thread; updates are marshalled onto Tk's UI
thread automatically.

Typical integration::

    page = VisionPage(host, on_start=start_vision, on_stop=stop_vision)
    page.pack(fill="both", expand=True)
    # In the engine callback:
    page.post_cycle(matches)

``get_options()`` returns ``interval`` in seconds (as expected by
``VisionEngine``), ``threshold`` as a float in [0, 1], and ``region`` as
``None`` or ``(x, y, width, height)``.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Iterable, Mapping, Optional

try:  # The helper is also useful in small standalone previews.
    from theme import COLORS
except Exception:  # pragma: no cover - fallback for direct import
    COLORS = {
        "window": "#0D1017", "surface": "#161B25", "surface_hover": "#1D2431",
        "surface_pressed": "#242D3D", "input": "#10141C", "border": "#283140",
        "border_focus": "#6688FF", "text": "#F2F5FA", "text_secondary": "#AAB4C4",
        "text_muted": "#6F7C90", "accent": "#6688FF", "success": "#40C98A",
        "warning": "#F2B84B", "danger": "#F06C75", "selection": "#304887",
    }


Callback = Optional[Callable[..., Any]]


def _value(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a VisionMatch object or a mapping."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


class VisionPage(ttk.Frame):
    """Image-template management page.

    Parameters are intentionally callback based.  Each callback is optional,
    which makes the page safe to display before the matching engine is ready.

    ``on_start(options, templates)``
        Start continuous scanning.  Return ``False`` when the engine rejected
        the start request; any other return value marks the page as running.
    ``on_stop()``
        Stop continuous scanning.
    ``on_scan_once(options, templates)``
        Request one scan cycle.
    ``on_choose_region()``
        Return ``(x, y, width, height)`` (or ``None``) after showing a region
        picker.  The page does not assume a particular picker implementation.
    ``on_templates_changed(templates)``
        Called after import, remove, enable/disable, or editor changes.
    """

    IMAGE_FILETYPES = (
        ("图片文件", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
        ("PNG 图片", "*.png"),
        ("JPEG 图片", "*.jpg *.jpeg"),
        ("所有文件", "*.*"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_start: Callback = None,
        on_stop: Callback = None,
        on_scan_once: Callback = None,
        on_choose_region: Callback = None,
        on_templates_changed: Callback = None,
        initial_templates: Optional[Iterable[Any]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, style="Page.TFrame", **kwargs)
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_scan_once = on_scan_once
        self.on_choose_region = on_choose_region
        self.on_templates_changed = on_templates_changed
        self._templates: dict[str, dict[str, Any]] = {}
        self._selected_id: Optional[str] = None
        self._last_matches: dict[str, Any] = {}
        self._running = False
        self._destroyed = False
        self._scan_count = 0
        self._match_count = 0
        self._region: Optional[tuple[int, int, int, int]] = None
        self._build_styles()
        self._build_vars()
        self._build_ui()
        if initial_templates:
            self.load_templates(initial_templates, notify=False)
        self.bind("<Destroy>", self._on_destroy, add="+")

    # ------------------------------------------------------------------ setup
    def _build_styles(self) -> None:
        """Add small vision-specific styles without requiring theme changes."""
        style = ttk.Style(self)
        # A ttk style needs a layout before it can be used by a widget.  The
        # bundled theme defines layouts for ``Treeview`` and its heading, so
        # clone those layouts before applying the vision-specific colours.
        try:
            style.layout("VisionTreeview", style.layout("Treeview"))
            style.layout("VisionTreeview.Heading", style.layout("Treeview.Heading"))
        except tk.TclError:
            pass
        style.configure(
            "VisionSection.TLabel", background=COLORS["surface"],
            foreground=COLORS["text"], font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "VisionMuted.TLabel", background=COLORS["surface"],
            foreground=COLORS["text_secondary"], font=("Segoe UI", 9),
        )
        style.configure(
            "VisionHint.TLabel", background=COLORS["surface"],
            foreground=COLORS["text_muted"], font=("Segoe UI", 8),
        )
        style.configure(
            "VisionStatus.TLabel", background=COLORS["surface"],
            foreground=COLORS["accent"], font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "VisionTreeview", background=COLORS["input"],
            fieldbackground=COLORS["input"], foreground=COLORS["text_secondary"],
            rowheight=34, font=("Segoe UI", 9), borderwidth=0,
        )
        style.configure(
            "VisionTreeview.Heading", background=COLORS["surface_hover"],
            foreground=COLORS["text_secondary"], padding=(8, 8),
            font=("Segoe UI Semibold", 9), borderwidth=0,
        )
        style.map(
            "VisionTreeview", background=[("selected", COLORS["selection"])],
            foreground=[("selected", "#FFFFFF")],
        )

    def _build_vars(self) -> None:
        self.interval_var = tk.StringVar(value="100")
        self.default_threshold_var = tk.StringVar(value="0.85")
        self.auto_click_var = tk.BooleanVar(value=True)
        self.global_button_var = tk.StringVar(value="左键")
        self.region_mode_var = tk.StringVar(value="全屏")
        self.region_text_var = tk.StringVar(value="全屏扫描")
        self.running_var = tk.StringVar(value="● 识别已停止")
        self.stats_var = tk.StringVar(value="扫描 0 次  ·  命中 0 次")
        self.last_match_var = tk.StringVar(value="尚未检测到图片")
        self.template_name_var = tk.StringVar()
        self.template_threshold_var = tk.StringVar(value="0.85")
        self.template_cooldown_var = tk.StringVar(value="0.25")
        self.template_button_var = tk.StringVar(value="左键")
        self.template_gray_var = tk.BooleanVar(value=False)
        self.editor_hint_var = tk.StringVar(value="选择左侧图片后可编辑参数")

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_intro()
        self._build_body()
        self._build_status()

    def _card(self, parent: tk.Misc, **kwargs: Any) -> ttk.Frame:
        return ttk.Frame(parent, style="Card.TFrame", **kwargs)

    def _build_intro(self) -> None:
        intro = self._card(self, padding=(20, 16))
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        intro.columnconfigure(0, weight=1)
        left = ttk.Frame(intro, style="CardInner.TFrame")
        left.grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="图片识别点击", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            left,
            text="导入按钮、图标或任意屏幕截图；检测到模板后自动点击其中心位置。",
            style="VisionMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        actions = ttk.Frame(intro, style="CardInner.TFrame")
        actions.grid(row=0, column=1, sticky="e")
        self.start_button = ttk.Button(actions, text="▶  开始识别", style="Primary.TButton", command=self.toggle_running)
        self.start_button.pack(side="left")
        self.scan_button = ttk.Button(actions, text="⌁  立即扫描", command=self.scan_once)
        self.scan_button.pack(side="left", padx=(8, 0))

    def _build_body(self) -> None:
        body = ttk.Frame(self, style="Page.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=6, uniform="vision")
        body.columnconfigure(1, weight=4, uniform="vision")
        body.rowconfigure(0, weight=1)
        self._build_template_card(body)
        self._build_editor_card(body)

    def _build_template_card(self, parent: tk.Misc) -> None:
        card = self._card(parent, padding=14)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        heading = ttk.Frame(card, style="CardInner.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="识别图片", style="VisionSection.TLabel").pack(side="left")
        self.template_count_var = tk.StringVar(value="0 个模板")
        ttk.Label(heading, textvariable=self.template_count_var, style="Count.TLabel").pack(side="right")
        ttk.Label(
            card,
            text="双击或按空格切换启用状态；每张图片可设置独立阈值和点击间隔。",
            style="VisionHint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(5, 10))
        table_wrap = ttk.Frame(card, style="CardInner.TFrame")
        table_wrap.grid(row=2, column=0, sticky="nsew")
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)
        columns = ("enabled", "name", "threshold", "cooldown", "button", "status")
        self.template_tree = ttk.Treeview(
            table_wrap, columns=columns, show="headings", selectmode="browse", style="VisionTreeview",
        )
        headings = {
            "enabled": "启用", "name": "图片模板", "threshold": "阈值",
            "cooldown": "冷却", "button": "点击", "status": "最近状态",
        }
        widths = {"enabled": 48, "name": 145, "threshold": 58, "cooldown": 60, "button": 55, "status": 125}
        for col in columns:
            self.template_tree.heading(col, text=headings[col])
            self.template_tree.column(col, width=widths[col], anchor="w", stretch=col in {"name", "status"})
        scroll_y = ttk.Scrollbar(table_wrap, orient="vertical", command=self.template_tree.yview)
        scroll_x = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.template_tree.xview)
        self.template_tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.template_tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.template_tree.bind("<<TreeviewSelect>>", self._on_template_select)
        self.template_tree.bind("<Double-1>", self._on_template_toggle)
        self.template_tree.bind("<space>", self._on_template_toggle)
        toolbar = ttk.Frame(card, style="CardInner.TFrame")
        toolbar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(toolbar, text="＋ 导入图片", style="Primary.TButton", command=self.import_templates).pack(side="left")
        ttk.Button(toolbar, text="移除选中", command=self.remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="清空列表", command=self.clear_templates).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="启用/停用", command=self.toggle_selected).pack(side="right")

    def _build_editor_card(self, parent: tk.Misc) -> None:
        card = self._card(parent, padding=18)
        card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        card.columnconfigure(1, weight=1)
        card.rowconfigure(10, weight=1)
        ttk.Label(card, text="识别设置", style="VisionSection.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(card, textvariable=self.editor_hint_var, style="VisionHint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 14))
        ttk.Label(card, text="模板名称", style="VisionMuted.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.template_name_var).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(card, text="匹配阈值", style="VisionMuted.TLabel").grid(row=3, column=0, sticky="w", pady=6)
        threshold_box = ttk.Frame(card, style="CardInner.TFrame")
        threshold_box.grid(row=3, column=1, sticky="ew", pady=6)
        threshold_box.columnconfigure(0, weight=1)
        ttk.Entry(threshold_box, textvariable=self.template_threshold_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(threshold_box, text="0.00 – 1.00", style="VisionHint.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(card, text="命中冷却", style="VisionMuted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
        cooldown_box = ttk.Frame(card, style="CardInner.TFrame")
        cooldown_box.grid(row=4, column=1, sticky="ew", pady=6)
        cooldown_box.columnconfigure(0, weight=1)
        ttk.Entry(cooldown_box, textvariable=self.template_cooldown_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(cooldown_box, text="秒", style="VisionHint.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(card, text="点击按键", style="VisionMuted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Combobox(card, textvariable=self.template_button_var, values=["左键", "右键", "中键"], state="readonly").grid(row=5, column=1, sticky="ew", pady=6)
        ttk.Checkbutton(card, text="灰度匹配（速度更快）", variable=self.template_gray_var).grid(row=6, column=1, sticky="w", pady=(7, 4))
        editor_buttons = ttk.Frame(card, style="CardInner.TFrame")
        editor_buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 12))
        ttk.Button(editor_buttons, text="应用到选中", style="Primary.TButton", command=self.apply_editor).pack(side="left")
        ttk.Button(editor_buttons, text="替换图片", command=self.replace_selected).pack(side="left", padx=(8, 0))
        ttk.Separator(card).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(card, text="扫描选项", style="VisionSection.TLabel").grid(row=9, column=0, columnspan=2, sticky="w")
        options = ttk.Frame(card, style="CardInner.TFrame")
        options.grid(row=10, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="扫描间隔", style="VisionMuted.TLabel").grid(row=0, column=0, sticky="w", pady=6)
        scan_box = ttk.Frame(options, style="CardInner.TFrame")
        scan_box.grid(row=0, column=1, sticky="ew", pady=6)
        scan_box.columnconfigure(0, weight=1)
        ttk.Entry(scan_box, textvariable=self.interval_var).grid(row=0, column=0, sticky="ew")
        ttk.Label(scan_box, text="ms", style="VisionHint.TLabel").grid(row=0, column=1, padx=(8, 0))
        ttk.Label(options, text="默认阈值", style="VisionMuted.TLabel").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(options, textvariable=self.default_threshold_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(options, text="扫描区域", style="VisionMuted.TLabel").grid(row=2, column=0, sticky="w", pady=6)
        region_box = ttk.Frame(options, style="CardInner.TFrame")
        region_box.grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(region_box, textvariable=self.region_text_var, style="VisionMuted.TLabel").pack(side="left")
        ttk.Button(region_box, text="选择区域", command=self.choose_region).pack(side="right")
        ttk.Radiobutton(options, text="全屏", variable=self.region_mode_var, value="全屏", command=self._region_mode_changed).grid(row=3, column=1, sticky="w", pady=(4, 2))
        ttk.Radiobutton(options, text="使用自定义区域", variable=self.region_mode_var, value="自定义", command=self._region_mode_changed).grid(row=4, column=1, sticky="w", pady=2)
        ttk.Checkbutton(options, text="识别到后自动点击", variable=self.auto_click_var).grid(row=5, column=1, sticky="w", pady=(8, 2))
        button_box = ttk.Frame(options, style="CardInner.TFrame")
        button_box.grid(row=6, column=1, sticky="ew", pady=(4, 0))
        ttk.Label(button_box, text="默认点击按键", style="VisionHint.TLabel").pack(side="left")
        ttk.Combobox(button_box, textvariable=self.global_button_var, values=["左键", "右键", "中键"], state="readonly", width=7).pack(side="left", padx=(8, 0))

    def _build_status(self) -> None:
        status = self._card(self, padding=(16, 11))
        status.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        status.columnconfigure(1, weight=1)
        ttk.Label(status, textvariable=self.running_var, style="VisionStatus.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.stats_var, style="VisionHint.TLabel").grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Label(status, textvariable=self.last_match_var, style="VisionHint.TLabel").grid(row=0, column=2, sticky="e")

    # -------------------------------------------------------------- templates
    def _new_id(self) -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _normalise_template(item: Any) -> dict[str, Any]:
        if isinstance(item, Mapping):
            get = item.get
        else:
            get = lambda key, default=None: getattr(item, key, default)
        path = str(get("path", "") or "")
        name = str(get("name", "") or Path(path).stem or "未命名模板")
        try:
            threshold = float(get("threshold", 0.85))
        except (TypeError, ValueError):
            threshold = 0.85
        try:
            cooldown = float(get("cooldown", 0.25))
        except (TypeError, ValueError):
            cooldown = 0.25
        button = str(get("button", "left") or "left").lower()
        button = {"left": "左键", "right": "右键", "middle": "中键", "左键": "左键", "右键": "右键", "中键": "中键"}.get(button, "左键")
        region = get("region", None)
        try:
            if region is not None:
                region = tuple(int(v) for v in region)
                if len(region) != 4:
                    region = None
        except (TypeError, ValueError):
            region = None
        stable_id = get("id", None) or get("template_id", None)
        return {
            "id": str(stable_id or uuid.uuid4().hex), "name": name, "path": path,
            "threshold": min(1.0, max(0.0, threshold)), "cooldown": max(0.0, cooldown),
            "button": button, "enabled": bool(get("enabled", True)),
            "region": region, "grayscale": bool(get("grayscale", False)),
        }

    def load_templates(self, items: Iterable[Any], *, notify: bool = True) -> None:
        self._templates.clear()
        for item in items:
            spec = self._normalise_template(item)
            self._templates[spec["id"]] = spec
        self._refresh_tree()
        if notify:
            self._notify_changed()

    def add_template(self, path: str, *, name: Optional[str] = None, notify: bool = True) -> Optional[str]:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not path or any(t["path"] == path for t in self._templates.values()):
            return None
        if not name:
            name = Path(path).stem or "未命名模板"
        spec = self._normalise_template({"id": self._new_id(), "path": path, "name": name})
        self._templates[spec["id"]] = spec
        self._refresh_tree(select_id=spec["id"])
        if notify:
            self._notify_changed()
        return spec["id"]

    def import_templates(self) -> None:
        paths = filedialog.askopenfilenames(title="选择识别图片", filetypes=self.IMAGE_FILETYPES)
        if not paths:
            return
        added = 0
        first_id = None
        for path in paths:
            item_id = self.add_template(path, notify=False)
            if item_id:
                added += 1
                first_id = first_id or item_id
        self._refresh_tree(select_id=first_id)
        self._notify_changed()
        if added == 0:
            messagebox.showinfo("图片模板", "所选图片已经在列表中。", parent=self.winfo_toplevel())

    def remove_selected(self) -> None:
        if not self._selected_id:
            return
        self._templates.pop(self._selected_id, None)
        self._selected_id = None
        self._refresh_tree()
        self._clear_editor()
        self._notify_changed()

    def clear_templates(self) -> None:
        if not self._templates:
            return
        if not messagebox.askyesno("清空图片", "确定要移除全部图片模板吗？", parent=self.winfo_toplevel()):
            return
        self._templates.clear()
        self._selected_id = None
        self._refresh_tree()
        self._clear_editor()
        self._notify_changed()

    def replace_selected(self) -> None:
        if not self._selected_id:
            return
        paths = filedialog.askopenfilenames(title="选择替换图片", filetypes=self.IMAGE_FILETYPES)
        if not paths:
            return
        path = os.path.abspath(paths[0])
        item = self._templates[self._selected_id]
        item["path"] = path
        item["name"] = Path(path).stem or item["name"]
        self.template_name_var.set(item["name"])
        self._refresh_tree(select_id=self._selected_id)
        self._notify_changed()

    def _refresh_tree(self, *, select_id: Optional[str] = None) -> None:
        if not hasattr(self, "template_tree"):
            return
        wanted = select_id or self._selected_id
        for iid in self.template_tree.get_children(""):
            self.template_tree.delete(iid)
        for item_id, item in self._templates.items():
            match = self._last_matches.get(item_id)
            status = self._match_status(match) if match else "等待扫描"
            self.template_tree.insert(
                "", "end", iid=item_id,
                values=(
                    "✓" if item["enabled"] else "○", item["name"],
                    f'{item["threshold"]:.2f}', f'{item["cooldown"]:.2f}s',
                    item["button"], status,
                ),
            )
        self.template_count_var.set(f"{len(self._templates)} 个模板")
        if wanted and wanted in self._templates:
            self.template_tree.selection_set(wanted)
            self.template_tree.focus(wanted)
            self.template_tree.see(wanted)
            self._selected_id = wanted
            # ``selection_set`` does not consistently emit
            # ``<<TreeviewSelect>>`` while rows are being rebuilt, so load the
            # editor explicitly as well.
            self._load_editor(self._templates[wanted])
        elif self._templates:
            first = next(iter(self._templates))
            self.template_tree.selection_set(first)
            self._selected_id = first
            self._load_editor(self._templates[first])

    def toggle_selected(self) -> None:
        if not self._selected_id:
            return
        self._templates[self._selected_id]["enabled"] = not self._templates[self._selected_id]["enabled"]
        self._refresh_tree(select_id=self._selected_id)
        self._notify_changed()

    def _on_template_toggle(self, _event: Any = None) -> str:
        self.toggle_selected()
        return "break"

    def _on_template_select(self, _event: Any = None) -> None:
        selection = self.template_tree.selection()
        if not selection:
            return
        self._selected_id = selection[0]
        self._load_editor(self._templates.get(self._selected_id))

    def _load_editor(self, item: Optional[Mapping[str, Any]]) -> None:
        if not item:
            self._clear_editor()
            return
        self.editor_hint_var.set(str(item.get("path", "")) or "已选择模板")
        self.template_name_var.set(str(item.get("name", "")))
        self.template_threshold_var.set(f'{float(item.get("threshold", 0.85)):.2f}')
        self.template_cooldown_var.set(f'{float(item.get("cooldown", 0.25)):.2f}')
        self.template_button_var.set(str(item.get("button", "左键")))
        self.template_gray_var.set(bool(item.get("grayscale", False)))

    def _clear_editor(self) -> None:
        self.editor_hint_var.set("选择左侧图片后可编辑参数")
        self.template_name_var.set("")
        self.template_threshold_var.set("0.85")
        self.template_cooldown_var.set("0.25")
        self.template_button_var.set("左键")
        self.template_gray_var.set(False)

    def apply_editor(self) -> None:
        if not self._selected_id or self._selected_id not in self._templates:
            return
        try:
            threshold = float(self.template_threshold_var.get())
            cooldown = float(self.template_cooldown_var.get())
        except ValueError:
            messagebox.showerror("识别设置", "阈值和冷却时间必须是数字。", parent=self.winfo_toplevel())
            return
        if not 0.0 <= threshold <= 1.0:
            messagebox.showerror("识别设置", "匹配阈值应在 0.00 到 1.00 之间。", parent=self.winfo_toplevel())
            return
        if cooldown < 0:
            messagebox.showerror("识别设置", "命中冷却不能小于 0。", parent=self.winfo_toplevel())
            return
        item = self._templates[self._selected_id]
        item.update({
            "name": self.template_name_var.get().strip() or Path(item["path"]).stem or "未命名模板",
            "threshold": threshold, "cooldown": cooldown,
            "button": self.template_button_var.get(), "grayscale": self.template_gray_var.get(),
        })
        self._refresh_tree(select_id=self._selected_id)
        self._notify_changed()

    # --------------------------------------------------------------- options
    def _region_mode_changed(self) -> None:
        if self.region_mode_var.get() == "全屏":
            self._region = None
            self.region_text_var.set("全屏扫描")
        elif self._region:
            self.region_text_var.set(self._format_region(self._region))
        else:
            self.region_text_var.set("尚未选择区域")

    @staticmethod
    def _format_region(region: tuple[int, int, int, int]) -> str:
        x, y, width, height = region
        return f"{x},{y}  {width}×{height}"

    def choose_region(self) -> None:
        if not self.on_choose_region:
            messagebox.showinfo("扫描区域", "请在主程序中接入区域选择器。", parent=self.winfo_toplevel())
            return
        try:
            region = self.on_choose_region()
        except Exception as exc:
            messagebox.showerror("扫描区域", f"选择区域失败：{exc}", parent=self.winfo_toplevel())
            return
        if region is None:
            return
        try:
            if isinstance(region, Mapping):
                region = tuple(int(region[key]) for key in ("x", "y", "width", "height"))
            else:
                region = tuple(int(v) for v in region)
            if len(region) != 4 or region[2] <= 0 or region[3] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            messagebox.showerror("扫描区域", "区域格式应为 x, y, width, height。", parent=self.winfo_toplevel())
            return
        self._region = region
        self.region_mode_var.set("自定义")
        self.region_text_var.set(self._format_region(region))

    def get_templates(self) -> list[dict[str, Any]]:
        """Return serialisable template specs suitable for VisionEngine."""
        # Keep Chinese labels in the editor, but hand the engine its stable
        # English button codes.  This also makes the result directly usable
        # with ``TemplateSpec(**page.get_templates()[0])``.
        result = []
        for item in self._templates.values():
            spec = dict(item)
            spec["button"] = self._button_code(spec.get("button"))
            result.append(spec)
        return result

    @staticmethod
    def _button_code(value: Any) -> str:
        return {
            "左键": "left", "右键": "right", "中键": "middle",
            "left": "left", "right": "right", "middle": "middle",
        }.get(str(value).lower(), "left")

    def get_options(self) -> dict[str, Any]:
        """Return scan settings in the units used by ``VisionEngine``."""
        try:
            interval_ms = max(10.0, float(self.interval_var.get()))
        except ValueError:
            interval_ms = 100.0
        try:
            threshold = min(1.0, max(0.0, float(self.default_threshold_var.get())))
        except ValueError:
            threshold = 0.85
        region = self._region if self.region_mode_var.get() == "自定义" else None
        return {
            "interval": interval_ms / 1000.0,
            "interval_ms": interval_ms,
            "threshold": threshold,
            "auto_click": bool(self.auto_click_var.get()),
            "click_button": self._button_code(self.global_button_var.get()),
            "region": region,
        }

    # ---------------------------------------------------------- engine events
    def toggle_running(self) -> None:
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        templates = self.get_templates()
        if not templates:
            messagebox.showinfo("图片识别", "请先导入至少一张图片模板。", parent=self.winfo_toplevel())
            return
        try:
            result = self.on_start(self.get_options(), templates) if self.on_start else True
        except Exception as exc:
            self.set_error(str(exc))
            return
        if result is not False:
            self.set_running(True)

    def stop(self) -> None:
        try:
            if self.on_stop:
                self.on_stop()
        except Exception as exc:
            self.set_error(str(exc))
        self.set_running(False)

    def scan_once(self) -> None:
        templates = self.get_templates()
        if not templates:
            messagebox.showinfo("图片识别", "请先导入至少一张图片模板。", parent=self.winfo_toplevel())
            return
        try:
            result = self.on_scan_once(self.get_options(), templates) if self.on_scan_once else None
            if isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
                self.post_cycle(result)
        except Exception as exc:
            self.set_error(str(exc))

    def set_running(self, running: bool, text: Optional[str] = None) -> None:
        self._running = bool(running)
        self.running_var.set(text or ("● 正在识别" if self._running else "● 识别已停止"))
        self.start_button.configure(text="■  停止识别" if self._running else "▶  开始识别")
        self.scan_button.configure(state="disabled" if self._running else "normal")

    def set_error(self, text: str) -> None:
        self.running_var.set(f"⚠ {text}")

    def post_match(self, match: Any) -> None:
        self._dispatch_ui(self._apply_match, match)

    def post_cycle(self, matches: Iterable[Any]) -> None:
        try:
            values = list(matches or ())
        except TypeError:
            values = []
        self._dispatch_ui(self._apply_cycle, values)

    def _apply_match(self, match: Any) -> None:
        if self._destroyed:
            return
        item_id = str(_value(match, "template_id", "") or "")
        if not item_id:
            name = str(_value(match, "template_name", _value(match, "name", "")) or "")
            for key, item in self._templates.items():
                if item["name"] == name:
                    item_id = key
                    break
        if item_id:
            self._last_matches[item_id] = match
        self._match_count += 1
        self.last_match_var.set(self._match_status(match, detailed=True))
        self.stats_var.set(f"扫描 {self._scan_count:,} 次  ·  命中 {self._match_count:,} 次")
        self._refresh_tree(select_id=self._selected_id)

    def _apply_cycle(self, matches: list[Any]) -> None:
        if self._destroyed:
            return
        self._scan_count += 1
        for match in matches:
            item_id = str(_value(match, "template_id", "") or "")
            if item_id:
                self._last_matches[item_id] = match
        if matches:
            self._match_count += len(matches)
            self.last_match_var.set(self._match_status(matches[-1], detailed=True))
        self.stats_var.set(f"扫描 {self._scan_count:,} 次  ·  命中 {self._match_count:,} 次")
        self._refresh_tree(select_id=self._selected_id)

    def _match_status(self, match: Any, detailed: bool = False) -> str:
        score = _value(match, "score", None)
        try:
            score_text = f"{float(score):.0%}" if score is not None else "命中"
        except (TypeError, ValueError):
            score_text = "命中"
        if not detailed:
            return score_text
        name = str(_value(match, "template_name", _value(match, "name", "图片")) or "图片")
        x = _value(match, "center_x", _value(match, "x", None))
        y = _value(match, "center_y", _value(match, "y", None))
        if x is not None and y is not None:
            return f"{name}  {score_text}  ({int(x)}, {int(y)})"
        return f"{name}  {score_text}"

    def _notify_changed(self) -> None:
        if self.on_templates_changed:
            try:
                self.on_templates_changed(self.get_templates())
            except Exception as exc:
                self.set_error(str(exc))

    def _dispatch_ui(self, callback: Callable[..., Any], *args: Any) -> None:
        if self._destroyed:
            return
        try:
            self.after(0, callback, *args)
        except tk.TclError:
            pass

    def _on_destroy(self, _event: Any = None) -> None:
        self._destroyed = True


# A descriptive alias makes the helper easy to discover from older code.
ImageRecognitionPage = VisionPage


__all__ = ["VisionPage", "ImageRecognitionPage"]
