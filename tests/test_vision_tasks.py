"""Tests for multi-task vision scheme management, batch selection, and persistence."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image

import main


def create_dummy_image(path: Path, color=(100, 150, 200), size=(32, 32)) -> Path:
    img = Image.new("RGB", size, color=color)
    img.save(path)
    return path


def test_initial_vision_task_state(app):
    assert app.active_vision_task_name == "默认任务"
    assert "默认任务" in app.vision_tasks
    assert app.vision_task_var.get() == "默认任务"
    assert "默认任务" in app.vision_task_combo["values"]


def test_add_vision_task_blank_and_inherited(app, tmp_path, monkeypatch):
    img_path = create_dummy_image(tmp_path / "target1.png")
    app.vision_templates = [{
        "id": "vision-1", "path": str(img_path), "name": "target1.png",
        "threshold": 0.85, "cooldown": 0.6, "max_matches": 1,
        "grayscale": False, "button": "左键", "actions": None,
        "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
        "enabled": True, "source": "file",
    }]
    app._sync_current_vision_task()

    # Add blank task (answer "no" to inherit)
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "任务A")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt, **kw: False)

    app.add_vision_task()
    assert app.active_vision_task_name == "任务A"
    assert "任务A" in app.vision_tasks
    assert len(app.vision_templates) == 0
    assert len(app.vision_tasks["默认任务"]["templates"]) == 1

    # Switch back to default task and create inherited task (answer "yes")
    app.vision_task_var.set("默认任务")
    app.select_vision_task()
    assert app.active_vision_task_name == "默认任务"
    assert len(app.vision_templates) == 1

    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "任务B")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt, **kw: True)
    app.add_vision_task()
    assert app.active_vision_task_name == "任务B"
    assert len(app.vision_templates) == 1
    assert app.vision_templates[0]["name"] == "target1.png"


def test_select_and_switch_vision_tasks(app, tmp_path):
    img1 = create_dummy_image(tmp_path / "img1.png", color=(10, 20, 30))
    img2 = create_dummy_image(tmp_path / "img2.png", color=(40, 50, 60))

    app.vision_tasks = {
        "方案一": {
            "templates": [{
                "id": "vision-1", "path": str(img1), "name": "img1.png",
                "threshold": 0.90, "cooldown": 0.5, "max_matches": 1,
                "grayscale": False, "button": "左键", "actions": None,
                "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
                "enabled": True, "source": "file",
            }],
            "scan_interval": "0.10",
            "immediate": True,
            "background": False,
        },
        "方案二": {
            "templates": [{
                "id": "vision-2", "path": str(img2), "name": "img2.png",
                "threshold": 0.80, "cooldown": 1.0, "max_matches": 2,
                "grayscale": True, "button": "右键", "actions": None,
                "click_count": 2, "click_interval": 0.10, "hold_duration": 0.0,
                "enabled": False, "source": "file",
            }],
            "scan_interval": "0.50",
            "immediate": False,
            "background": True,
        },
    }
    app.active_vision_task_name = "方案一"
    app.vision_templates = [dict(app.vision_tasks["方案一"]["templates"][0])]
    app.vision_scan_var.set("0.10")
    app.vision_immediate_var.set(True)
    app.vision_background_var.set(False)
    app.vision_task_var.set("方案一")
    app.refresh_vision_task_ui()

    # Switch to 方案二
    app.vision_task_var.set("方案二")
    app.select_vision_task()
    assert app.active_vision_task_name == "方案二"
    assert len(app.vision_templates) == 1
    assert app.vision_templates[0]["name"] == "img2.png"
    assert app.vision_scan_var.get() == "0.50"
    assert app.vision_immediate_var.get() is False
    assert app.vision_background_var.get() is True

    # Switch back to 方案一
    app.vision_task_var.set("方案一")
    app.select_vision_task()
    assert app.active_vision_task_name == "方案一"
    assert app.vision_templates[0]["name"] == "img1.png"
    assert app.vision_scan_var.get() == "0.10"
    assert app.vision_immediate_var.get() is True


def test_save_vision_task_as_and_rename(app, tmp_path, monkeypatch):
    img = create_dummy_image(tmp_path / "item.png")
    app.vision_templates = [{
        "id": "vision-1", "path": str(img), "name": "item.png",
        "threshold": 0.85, "cooldown": 0.6, "max_matches": 1,
        "grayscale": False, "button": "左键", "actions": None,
        "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
        "enabled": True, "source": "file",
    }]
    app._sync_current_vision_task()

    # Save as
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "任务备份")
    app.save_vision_task_as()
    assert app.active_vision_task_name == "任务备份"
    assert "任务备份" in app.vision_tasks
    assert len(app.vision_tasks["任务备份"]["templates"]) == 1

    # Rename
    monkeypatch.setattr(app, "_prompt_task_name", lambda title, prompt, default: "新名称")
    app.rename_vision_task()
    assert app.active_vision_task_name == "新名称"
    assert "新名称" in app.vision_tasks
    assert "任务备份" not in app.vision_tasks


def test_delete_vision_task(app, tmp_path, monkeypatch):
    assert len(app.vision_tasks) == 1
    # Try deleting when only 1 task exists
    info_mock = Mock()
    monkeypatch.setattr(main.messagebox, "showinfo", info_mock)
    app.delete_vision_task()
    info_mock.assert_called_once()
    assert len(app.vision_tasks) == 1

    # Add second task then delete
    app.vision_tasks["任务二"] = {"templates": [], "scan_interval": "0.20", "immediate": False, "background": False}
    app.active_vision_task_name = "任务二"
    app.refresh_vision_task_ui()

    monkeypatch.setattr(main.messagebox, "askyesno", lambda title, prompt: True)
    app.delete_vision_task()
    assert "任务二" not in app.vision_tasks
    assert app.active_vision_task_name == "默认任务"


def test_batch_template_selection_and_toggle(app, tmp_path):
    img = create_dummy_image(tmp_path / "img.png")
    app.vision_templates = [
        {
            "id": f"vision-{i}", "path": str(img), "name": f"img{i}.png",
            "threshold": 0.85, "cooldown": 0.6, "max_matches": 1,
            "grayscale": False, "button": "左键", "actions": None,
            "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
            "enabled": True, "source": "file",
        }
        for i in range(1, 4)
    ]
    app.refresh_vision_tree()

    # Disable all
    app.disable_all_vision_templates()
    assert all(not item["enabled"] for item in app.vision_templates)

    # Enable all
    app.enable_all_vision_templates()
    assert all(item["enabled"] for item in app.vision_templates)

    # Invert
    app.vision_templates[0]["enabled"] = False
    app.invert_vision_templates()
    assert app.vision_templates[0]["enabled"] is True
    assert app.vision_templates[1]["enabled"] is False
    assert app.vision_templates[2]["enabled"] is False

    # Toggle selected
    app.vision_tree.selection_set("vision-1")
    app.toggle_selected_vision_template()
    assert app.vision_templates[0]["enabled"] is False
    app.toggle_selected_vision_template()
    assert app.vision_templates[0]["enabled"] is True


def test_vision_tasks_config_and_profile_roundtrip(app, tmp_path, monkeypatch):
    img1 = create_dummy_image(tmp_path / "img1.png", color=(10, 20, 30))
    img2 = create_dummy_image(tmp_path / "img2.png", color=(40, 50, 60))

    app.vision_tasks = {
        "日常日常": {
            "templates": [{
                "id": "vision-1", "path": str(img1), "name": "img1.png",
                "threshold": 0.90, "cooldown": 0.5, "max_matches": 1,
                "grayscale": False, "button": "左键", "actions": None,
                "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
                "enabled": True, "source": "file",
            }],
            "scan_interval": "0.15",
            "immediate": True,
            "background": False,
        },
        "特殊活动": {
            "templates": [{
                "id": "vision-2", "path": str(img2), "name": "img2.png",
                "threshold": 0.82, "cooldown": 0.8, "max_matches": 1,
                "grayscale": True, "button": "右键", "actions": None,
                "click_count": 1, "click_interval": 0.08, "hold_duration": 0.0,
                "enabled": True, "source": "file",
            }],
            "scan_interval": "0.35",
            "immediate": False,
            "background": True,
        },
    }
    app.active_vision_task_name = "日常日常"
    app.vision_templates = [dict(app.vision_tasks["日常日常"]["templates"][0])]
    app.refresh_vision_task_ui()

    config = app._collect_config()
    assert "vision_tasks" in config
    assert "日常日常" in config["vision_tasks"]
    assert "特殊活动" in config["vision_tasks"]
    assert config["vision_active_task"] == "日常日常"

    # Profile bundle test
    payload, assets, warnings = app._prepare_profile_bundle()
    assert len(assets) == 2
    profile_path = tmp_path / "tasks.clickerprofile"
    assert app._write_profile_archive(profile_path, payload, assets)

    # Import profile into fresh app state
    monkeypatch.setattr(main, "PROFILE_ASSET_DIR", tmp_path / "assets_extracted")
    monkeypatch.setattr(main.filedialog, "askopenfilename", lambda **kwargs: str(profile_path))
    app.vision_tasks = {}
    app.vision_templates = []
    assert app.import_profile()

    assert "日常日常" in app.vision_tasks
    assert "特殊活动" in app.vision_tasks
    assert app.active_vision_task_name == "日常日常"
    assert len(app.vision_templates) == 1
    assert Path(app.vision_templates[0]["path"]).is_file()


def test_legacy_profile_backward_compatibility(app, tmp_path):
    img = create_dummy_image(tmp_path / "legacy.png")
    legacy_data = {
        "theme": "dark",
        "vision_templates": [{
            "path": str(img), "name": "legacy.png",
            "threshold": 0.88, "cooldown": 0.5,
            "enabled": True,
        }],
    }
    result = app._apply_config_data(legacy_data, replace_templates=True)
    assert result["vision_loaded"] == 1
    assert app.active_vision_task_name == "默认任务"
    assert "默认任务" in app.vision_tasks
    assert app.vision_templates[0]["name"] == "legacy.png"


def test_prompt_task_name_dialog_geometry_and_visibility(app, monkeypatch):
    seen = {}

    def fake_wait(dlg):
        min_w, min_h = dlg.minsize()
        seen["min_width"] = min_w
        seen["min_height"] = min_h
        for child in dlg.winfo_children():
            for grandchild in child.winfo_children():
                for item in grandchild.winfo_children():
                    if isinstance(item, main.ttk.Button):
                        seen[item.cget("text")] = True
        dlg.destroy()

    monkeypatch.setattr(app.root, "wait_window", fake_wait)
    app._prompt_task_name("另存为新任务", "请输入另存为的新任务名称：", "测试任务")
    assert seen.get("min_height", 0) >= 190
    assert seen.get("min_width", 0) >= 400
    assert seen.get("确定") is True
    assert seen.get("取消") is True

