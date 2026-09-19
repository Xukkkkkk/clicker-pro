"""Theme changes retain live application state and survive saved profiles."""
import json
import tkinter as tk
from tkinter import ttk
from unittest.mock import Mock

import pytest

import main
from theme import COLORS, THEMES, bind_theme


def test_switch_updates_existing_widgets_without_resetting_state(app):
    original_colours = COLORS
    original_icons = dict(app.ui_images)
    app.events = [dict(type="click", t=0.5, x=12, y=34, button="left", pressed=True)]
    app.refresh_event_tree()
    rows = app.event_tree.get_children()
    app.event_tree.selection_set(rows[0])
    app.interval_var.set("123")
    app.hotkey_specs["play"] = "<f11>"
    app.recording = app.playing = True
    app.set_status("正在回放", "success")
    listener = app.hotkey_listener
    # Exercise an already-created Tcl combobox dropdown and another window.
    combo = ttk.Combobox(app.root, values=("one", "two"))
    popup = str(combo.tk.call("ttk::combobox::PopdownWindow", str(combo)))
    window = bind_theme(tk.Toplevel(app.root), bg="input")
    window.withdraw()
    label = bind_theme(tk.Label(window), bg="input", fg="text_muted")
    for name in ("light", "dark", "light"):
        app.theme_var.set(name)
        app.change_theme()
        app.root.update()
        palette = THEMES[name]
        assert COLORS is original_colours
        assert app.root.cget("background") == palette["window"]
        assert app.style.lookup("Treeview", "fieldbackground") == palette["input"]
        assert app.record_empty_label.cget("background") == palette["input"]
        assert app.vision_editor_canvas.cget("background") == palette["surface"]
        assert str(app.vision_context_menu.cget("background")) == palette["surface_hover"]
        assert app.status_pill.cget("background") == palette["success_surface"]
        assert window.cget("background") == palette["input"]
        assert label.cget("foreground") == palette["text_muted"]
        assert combo.tk.call(popup + ".f.l", "cget", "-background") == palette["surface_hover"]
        assert app.event_tree.get_children() == rows
        assert app.event_tree.selection() == (rows[0],)
        assert app.interval_var.get() == "123"
        assert app.hotkey_specs["play"] == "<f11>"
        assert app.hotkey_listener is listener
        assert app.recording and app.playing
        assert not app.play_stop_event.is_set()
        assert app.footer_var.get() == "正在回放"
        assert all(app.ui_images[key] is icon for key, icon in original_icons.items())
        assert len(app.events) == 1


def test_theme_saved_and_restored_on_new_app(app):
    app.theme_var.set("light")
    app.change_theme()
    assert json.loads(main.CONFIG_FILE.read_text(encoding="utf-8"))["theme"] == "light"
    app.close()
    root = tk.Tk()
    root.withdraw()
    reopened = main.ClickerApp(root)
    try:
        assert reopened.theme_var.get() == "light"
        assert root.cget("background") == THEMES["light"]["window"]
        assert reopened.hotkey_specs["play"] == "<f10>"
    finally:
        reopened.close()


def test_theme_profile_roundtrip_and_legacy_profile(app, monkeypatch, tmp_path):
    app.apply_theme("light")
    payload, assets, _ = app._prepare_profile_bundle()
    profile = tmp_path / "appearance.clickerprofile"
    assert app._write_profile_archive(profile, payload, assets)
    monkeypatch.setattr(main, "PROFILE_ASSET_DIR", tmp_path / "assets")
    monkeypatch.setattr(main.filedialog, "askopenfilename", lambda **kwargs: str(profile))
    app.apply_theme("dark")
    assert app.import_profile()
    assert app.theme_name == "light"
    app._apply_config_data({"interval_ms": "200"})
    assert app.theme_name == "light"  # Older profiles retain the user's theme.


def test_failed_theme_save_reports_failure(app, monkeypatch):
    monkeypatch.setattr(app, "write_json", Mock(return_value=False))
    app.theme_var.set("light")
    app.change_theme()
    assert app.theme_name == "light"
    assert "保存失败" in app.theme_status_var.get()


@pytest.mark.parametrize("theme", ["unknown", None, {}])
def test_invalid_theme_profiles_are_rejected(theme):
    with pytest.raises(ValueError, match="主题颜色"):
        main.ClickerApp._validate_profile_settings({"theme": theme})
