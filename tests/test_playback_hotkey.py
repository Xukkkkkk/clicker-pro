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
