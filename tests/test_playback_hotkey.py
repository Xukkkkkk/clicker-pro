"""Exercise global playback shortcuts without sending input to the desktop."""
import json
from unittest.mock import Mock

import pytest

import main


def test_f10_toggles_playback_and_ignores_key_repeat(app, monkeypatch):
    worker = Mock()
    monkeypatch.setattr(main.threading, "Thread", Mock(return_value=worker))
    app.events = [dict(type="click", t=0.5, x=10, y=20,
                       button="left", pressed=True)]
    app.vision_running = True
    listener = app.hotkey_listener
    key = main.keyboard.Key.f10

    listener.on_press(key, False)
    app.root.update()
    assert app.playing
    worker.start.assert_called_once()

    listener.on_press(key, False)
    app.root.update()
    assert app.playing  # A held F10 must not immediately stop the run.
    listener.on_release(key, False)
    stop_event = app.play_stop_event

    listener.on_press(key, False)
    listener.on_release(key, False)
    app.root.update()
    assert not app.playing
    assert stop_event.is_set()
    assert app.vision_running
    assert not app.click_stop_event.is_set()
    assert len(app.events) == 1


def test_legacy_config_adds_default_without_stealing_existing_binding(app):
    app._apply_config_data({"toggle_hotkey": "<f6>"})
    assert app.hotkey_specs["play"] == "<f10>"
    assert app.hotkey_vars["play"].get() == "F10"
    assert "F10  回放开关" in app.hotkey_tip_text()

    app._apply_config_data({"toggle_hotkey": "<f10>"})
    assert app.hotkey_specs["toggle"] == "<f10>"
    assert app.hotkey_specs["play"] == ""
    assert app.start_hotkeys()


@pytest.mark.parametrize("binding", ["<ctrl>+<alt>+p", ""])
def test_custom_or_cleared_binding_survives_save_and_profile_import(
        app, monkeypatch, tmp_path, binding):
    app.hotkey_specs["play"] = binding
    app.apply_hotkeys()
    saved = json.loads(main.CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["play_hotkey"] == binding
    app.hotkey_specs["play"] = "<f12>"
    app.load_config()
    assert app.hotkey_specs["play"] == binding

    payload, assets, _warnings = app._prepare_profile_bundle()
    profile = tmp_path / "shortcut.clickerprofile"
    assert app._write_profile_archive(profile, payload, assets)
    monkeypatch.setattr(main, "PROFILE_ASSET_DIR", tmp_path / "profile-assets")
    monkeypatch.setattr(main.filedialog, "askopenfilename", lambda **kwargs: str(profile))
    app.hotkey_specs["play"] = "<f12>"
    assert app.import_profile()
    assert app.hotkey_specs["play"] == binding
    assert app._collect_config()["play_hotkey"] == binding


@pytest.mark.parametrize("settings", [
    {"play_hotkey": "<f6>", "toggle_hotkey": "<f6>"},
    {"play_hotkey": "<ctrl>+v"},
    {"play_hotkey": "<invalid-key>"},
])
def test_profile_rejects_invalid_playback_shortcut(settings):
    with pytest.raises(ValueError):
        main.ClickerApp._validate_profile_settings(settings)


def test_reset_individual_and_all_hotkeys_to_default(app):
    app.hotkey_specs["toggle"] = "<ctrl>+1"
    app.hotkey_specs["play"] = "<ctrl>+2"
    app.apply_hotkeys()
    assert app.hotkey_specs["toggle"] == "<ctrl>+1"

    app.reset_hotkey_to_default("toggle")
    assert app.hotkey_specs["toggle"] == "<f6>"
    assert app.hotkey_vars["toggle"].get() == "F6"

    app.reset_all_hotkeys_to_default()
    assert app.hotkey_specs["toggle"] == "<f6>"
    assert app.hotkey_specs["record"] == "<f7>"
    assert app.hotkey_specs["stop"] == "<esc>"
    assert app.hotkey_specs["pause"] == "<f9>"
    assert app.hotkey_specs["play"] == "<f10>"


def test_apply_hotkey_presets(app):
    app.apply_hotkey_preset("ctrl")
    assert app.hotkey_specs["toggle"] == "<ctrl>+1"
    assert app.hotkey_specs["stop"] == "<ctrl>+5"
    assert app.hotkey_vars["toggle"].get() == "Ctrl + 1"

    app.apply_hotkey_preset("default")
    assert app.hotkey_specs["toggle"] == "<f6>"
    assert app.hotkey_specs["stop"] == "<esc>"
    assert app.hotkey_specs["play"] == "<f10>"

    app.apply_hotkey_preset("f_keys")
    assert app.hotkey_specs["stop"] == "<f8>"


def test_capture_hotkey_esc_cancel_clear_and_conflict(app):
    # Escape now records Esc (<esc>)
    app.arm_hotkey_capture("stop")
    assert app.hotkey_capture_target == "stop"
    esc_event = Mock(keysym="Escape", state=0)
    assert app.capture_hotkey(esc_event, "stop") == "break"
    assert app.hotkey_specs["stop"] == "<esc>"
    assert app.hotkey_vars["stop"].get() == "Esc"
    assert app.hotkey_state_vars["stop"].get() == "已生效"

    # Focus out or cancel restores previous
    app.arm_hotkey_capture("play")
    app.cancel_hotkey_capture("play")
    assert app.hotkey_specs["play"] == "<f10>"
    assert app.hotkey_state_vars["play"].get() == "已取消"

    # Backspace/Delete clears hotkey
    app.arm_hotkey_capture("play")
    del_event = Mock(keysym="Delete", state=0)
    assert app.capture_hotkey(del_event, "play") == "break"
    assert app.hotkey_specs["play"] == ""
    assert app.hotkey_vars["play"].get() == "未设置"

    # Conflict detection: trying to assign F6 (already used by toggle)
    app.arm_hotkey_capture("play")
    f6_event = Mock(keysym="F6", state=0, char="")
    assert app.capture_hotkey(f6_event, "play") == "break"
    assert "冲突" in app.hotkey_state_vars["play"].get()


def test_esc_emergency_stops_active_tasks(app):
    app.running = True
    app._emergency_stop()
    assert not app.running

    app.playing = True
    app._emergency_stop()
    assert not app.playing

    app.vision_running = True
    app._emergency_stop()
    assert not app.vision_running

    app.running = True
    app._on_root_escape()
    assert not app.running


def test_click_presets(app):
    app._apply_click_preset("10", False)
    assert app.interval_var.get() == "10"
    assert not app.random_var.get()

    app._apply_click_preset("100", True)
    assert app.interval_var.get() == "100"
    assert app.random_var.get()
    assert app.random_percent_var.get() == "20"


def test_direct_hotkey_replacement_via_entry_click_and_keypress(app):
    # Click/Focus on entry directly arms replacement without any recording button
    click_event = Mock()
    assert app._on_hotkey_entry_click(click_event, "toggle") == "break"
    assert app.hotkey_capture_target == "toggle"
    assert "请按新按键" in app.hotkey_state_vars["toggle"].get()

    # Pressing a new key directly replaces the hotkey
    f1_event = Mock(keysym="F1", state=0, char="")
    assert app.capture_hotkey(f1_event, "toggle") == "break"
    assert app.hotkey_specs["toggle"] == "<f1>"
    assert app.hotkey_vars["toggle"].get() == "F1"
    assert app.hotkey_state_vars["toggle"].get() == "已生效"
    assert app.hotkey_capture_target is None


def test_modifier_combo_feedback_and_direct_replace(app):
    app.arm_hotkey_capture("pause")
    # Press Ctrl
    ctrl_event = Mock(keysym="Control_L", state=0x0004)
    assert app.capture_hotkey(ctrl_event, "pause") == "break"
    assert "Ctrl" in app.hotkey_state_vars["pause"].get()

    # Press P while Ctrl is held
    p_event = Mock(keysym="p", state=0x0004, char="p")
    assert app.capture_hotkey(p_event, "pause") == "break"
    assert app.hotkey_specs["pause"] == "<ctrl>+p"
    assert app.hotkey_vars["pause"].get() == "Ctrl + P"
    assert app.hotkey_state_vars["pause"].get() == "已生效"


def test_switching_hotkey_target_replaces_cleanly(app):
    # Arm 'record', then immediately arm 'play' without finishing 'record'
    app.arm_hotkey_capture("record")
    assert app.hotkey_capture_target == "record"
    app.arm_hotkey_capture("play")
    assert app.hotkey_capture_target == "play"
    assert app.hotkey_specs["record"] == "<f7>"  # 'record' restored


