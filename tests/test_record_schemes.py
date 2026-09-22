"""Tests for mouse recording schemes management, switching, renaming, and persistence."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import main


def test_initial_record_scheme_state(app):
    assert app.active_record_scheme_name == "默认方案"
    assert "默认方案" in app.record_schemes
    assert app.record_scheme_var.get() == "默认方案"
    assert "默认方案" in app.record_scheme_combo["values"]


def test_add_record_scheme_blank_and_inherited(app, monkeypatch):
    app.events = [{
        "type": "click", "t": 0.5, "x": 100, "y": 200, "button": "left", "pressed": True,
    }]
    app._sync_current_record_scheme()

    # Add blank scheme (answer "no" to inherit)
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "方案A")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt, **kw: False)

    app.add_record_scheme()
    assert app.active_record_scheme_name == "方案A"
    assert "方案A" in app.record_schemes
    assert len(app.events) == 0
    assert len(app.record_schemes["默认方案"]["events"]) == 1

    # Switch back to default scheme and create inherited scheme (answer "yes")
    app.record_scheme_var.set("默认方案")
    app.select_record_scheme()
    assert app.active_record_scheme_name == "默认方案"
    assert len(app.events) == 1

    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "方案B")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt, **kw: True)
    app.add_record_scheme()
    assert app.active_record_scheme_name == "方案B"
    assert len(app.events) == 1
    assert app.events[0]["x"] == 100


def test_select_and_switch_record_schemes(app):
    app.record_schemes = {
        "方案一": {
            "events": [{
                "type": "click", "t": 0.1, "x": 10, "y": 20, "button": "left", "pressed": True,
            }],
            "include_moves": False,
            "speed": "2.0x",
            "loops": "3",
            "background": False,
        },
        "方案二": {
            "events": [{
                "type": "click", "t": 0.2, "x": 30, "y": 40, "button": "right", "pressed": True,
            }, {
                "type": "move", "t": 0.3, "x": 50, "y": 60, "button": "left", "pressed": True,
            }],
            "include_moves": True,
            "speed": "0.5x",
            "loops": "10",
            "background": True,
        },
    }
    app.active_record_scheme_name = "方案一"
    app.events = list(app.record_schemes["方案一"]["events"])
    app.record_include_moves_var.set(False)
    app.speed_var.set("2.0x")
    app.loop_var.set("3")
    app.record_background_var.set(False)
    app.record_scheme_var.set("方案一")
    app.refresh_record_scheme_ui()

    # Switch to 方案二
    app.record_scheme_var.set("方案二")
    app.select_record_scheme()
    assert app.active_record_scheme_name == "方案二"
    assert len(app.events) == 2
    assert app.events[0]["button"] == "right"
    assert app.record_include_moves_var.get() is True
    assert app.speed_var.get() == "0.5x"
    assert app.loop_var.get() == "10"
    assert app.record_background_var.get() is True

    # Switch back to 方案一
    app.record_scheme_var.set("方案一")
    app.select_record_scheme()
    assert app.active_record_scheme_name == "方案一"
    assert len(app.events) == 1
    assert app.events[0]["x"] == 10
    assert app.speed_var.get() == "2.0x"
    assert app.loop_var.get() == "3"


def test_save_record_scheme_as_and_rename(app, monkeypatch):
    app.events = [{
        "type": "click", "t": 0.5, "x": 100, "y": 200, "button": "left", "pressed": True,
    }]
    app._sync_current_record_scheme()

    # Save as
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "方案备份")
    app.save_record_scheme_as()
    assert app.active_record_scheme_name == "方案备份"
    assert "方案备份" in app.record_schemes
    assert len(app.record_schemes["方案备份"]["events"]) == 1

    # Rename (and change name)
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "重命名后的方案")
    app.rename_record_scheme()
    assert app.active_record_scheme_name == "重命名后的方案"
    assert "重命名后的方案" in app.record_schemes
    assert "方案备份" not in app.record_schemes


def test_delete_record_scheme(app, monkeypatch):
    assert len(app.record_schemes) == 1
    info_mock = Mock()
    monkeypatch.setattr(main.messagebox, "showinfo", info_mock)
    app.delete_record_scheme()
    info_mock.assert_called_once()
    assert len(app.record_schemes) == 1

    # Add second scheme then delete
    app.record_schemes["方案二"] = {
        "events": [], "include_moves": True, "speed": "1.0x", "loops": "1", "background": False,
    }
    app.active_record_scheme_name = "方案二"
    app.refresh_record_scheme_ui()

    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt: True)
    app.delete_record_scheme()
    assert "方案二" not in app.record_schemes
    assert app.active_record_scheme_name == "默认方案"


def test_record_schemes_config_and_profile_roundtrip(app, tmp_path, monkeypatch):
    app.record_schemes = {
        "刷金币": {
            "events": [{
                "type": "click", "t": 0.1, "x": 100, "y": 150, "button": "left", "pressed": True,
            }],
            "include_moves": False,
            "speed": "1.5x",
            "loops": "20",
            "background": False,
        },
        "刷经验": {
            "events": [{
                "type": "click", "t": 0.2, "x": 200, "y": 250, "button": "right", "pressed": True,
            }],
            "include_moves": True,
            "speed": "1.0x",
            "loops": "5",
            "background": True,
        },
    }
    app.active_record_scheme_name = "刷金币"
    app.events = list(app.record_schemes["刷金币"]["events"])
    app.refresh_record_scheme_ui()

    config = app._collect_config()
    assert "record_schemes" in config
    assert "刷金币" in config["record_schemes"]
    assert "刷经验" in config["record_schemes"]
    assert config["record_active_scheme"] == "刷金币"

    # Profile export
    payload, assets, warnings = app._prepare_profile_bundle()
    profile_path = tmp_path / "record_schemes.clickerprofile"
    assert app._write_profile_archive(profile_path, payload, assets)

    # Import profile
    monkeypatch.setattr(main, "PROFILE_ASSET_DIR", tmp_path / "assets_extracted")
    monkeypatch.setattr(main.filedialog, "askopenfilename", lambda **kwargs: str(profile_path))
    app.record_schemes = {}
    app.events = []
    assert app.import_profile()

    assert "刷金币" in app.record_schemes
    assert "刷经验" in app.record_schemes
    assert app.active_record_scheme_name == "刷金币"
    assert len(app.events) == 1
    assert app.events[0]["x"] == 100


def test_legacy_record_file_backward_compatibility(app):
    legacy_data = {
        "theme": "dark",
        "speed": "1.5x",
    }
    app._apply_config_data(legacy_data, replace_templates=True)
    assert app.active_record_scheme_name == "默认方案"
    assert "默认方案" in app.record_schemes
