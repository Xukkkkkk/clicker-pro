"""Tests for sequential vision execution and target template drag/reordering."""
import threading
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

import main
from vision_engine import TemplateSpec, VisionEngine


def make_pattern(text: str, color: tuple[int, int, int]) -> np.ndarray:
    image = np.full((40, 96, 3), color, dtype=np.uint8)
    cv2.rectangle(image, (1, 1), (94, 38), (240, 240, 240), 2)
    cv2.putText(image, text, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return image


def frame_with_pattern(image: np.ndarray) -> np.ndarray:
    frame = np.full((240, 480, 3), 40, dtype=np.uint8)
    frame[50:50 + image.shape[0], 80:80 + image.shape[1]] = image
    return frame


def test_vision_engine_sequential_execution_advances_in_order():
    img_a = make_pattern("STEP1", (20, 30, 180))
    img_b = make_pattern("STEP2", (30, 180, 30))
    img_c = make_pattern("STEP3", (180, 30, 30))

    spec_a = TemplateSpec(image=img_a, id="a", name="步骤A", threshold=0.9)
    spec_b = TemplateSpec(image=img_b, id="b", name="步骤B", threshold=0.9)
    spec_c = TemplateSpec(image=img_c, id="c", name="步骤C", threshold=0.9)

    current_frame = [frame_with_pattern(img_b)]  # Initially screen only has B
    dispatched = []

    engine = VisionEngine(
        [spec_a, spec_b, spec_c],
        sequential=True,
        capture_fn=lambda: current_frame[0],
        on_match=lambda match: dispatched.append(match.template_id),
    )

    # 1. At step 0 (waiting for A), screen only has B -> no match, step does not advance
    matches = engine.scan_once(trigger=True)
    assert matches == []
    assert dispatched == []
    assert engine.sequence_step == 0
    assert engine.current_sequence_spec().id == "a"

    # 2. Screen now displays A
    current_frame[0] = frame_with_pattern(img_a)
    matches = engine.scan_once(trigger=True)
    assert len(matches) == 1
    assert matches[0].template_id == "a"
    assert dispatched == ["a"]
    assert engine.sequence_step == 1
    assert engine.current_sequence_spec().id == "b"

    # 3. Screen still displays A, but step 1 is waiting for B -> no match
    matches = engine.scan_once(trigger=True)
    assert matches == []
    assert dispatched == ["a"]
    assert engine.sequence_step == 1

    # 4. Screen now displays B -> matches B, step advances to 2
    current_frame[0] = frame_with_pattern(img_b)
    matches = engine.scan_once(trigger=True)
    assert len(matches) == 1
    assert matches[0].template_id == "b"
    assert dispatched == ["a", "b"]
    assert engine.sequence_step == 2
    assert engine.current_sequence_spec().id == "c"

    # 5. Screen now displays C -> matches C, step wraps around to 0
    current_frame[0] = frame_with_pattern(img_c)
    matches = engine.scan_once(trigger=True)
    assert len(matches) == 1
    assert matches[0].template_id == "c"
    assert dispatched == ["a", "b", "c"]
    assert engine.sequence_step == 0
    assert engine.current_sequence_spec().id == "a"


def test_vision_engine_sequential_skips_disabled_templates():
    img_a = make_pattern("STEP1", (20, 30, 180))
    img_b = make_pattern("STEP2", (30, 180, 30))
    img_c = make_pattern("STEP3", (180, 30, 30))

    spec_a = TemplateSpec(image=img_a, id="a", name="步骤A", threshold=0.9, enabled=True)
    spec_b = TemplateSpec(image=img_b, id="b", name="步骤B", threshold=0.9, enabled=False)
    spec_c = TemplateSpec(image=img_c, id="c", name="步骤C", threshold=0.9, enabled=True)

    current_frame = [frame_with_pattern(img_a)]
    dispatched = []

    engine = VisionEngine(
        [spec_a, spec_b, spec_c],
        sequential=True,
        capture_fn=lambda: current_frame[0],
        on_match=lambda match: dispatched.append(match.template_id),
    )

    # Step 0 matches A
    matches = engine.scan_once(trigger=True)
    assert len(matches) == 1
    assert matches[0].template_id == "a"
    # Next step should be C (skipping disabled B)
    assert engine.current_sequence_spec().id == "c"

    current_frame[0] = frame_with_pattern(img_c)
    matches = engine.scan_once(trigger=True)
    assert len(matches) == 1
    assert matches[0].template_id == "c"
    # Wraps back to A
    assert engine.current_sequence_spec().id == "a"


def test_vision_engine_sequential_reset_and_summary():
    img = make_pattern("TEST", (100, 100, 100))
    spec1 = TemplateSpec(image=img, id="s1", name="第一步")
    spec2 = TemplateSpec(image=img, id="s2", name="第二步")

    engine = VisionEngine([spec1, spec2], sequential=True, capture_fn=lambda: frame_with_pattern(img))
    assert engine.sequence_step == 0
    engine.sequence_step = 1
    assert engine.sequence_step == 1
    assert engine.current_sequence_spec().name == "第二步"

    engine.reset_sequence()
    assert engine.sequence_step == 0
    assert engine.current_sequence_spec().name == "第一步"

    engine.scan_once(trigger=False)
    summary = engine.scan_summary()
    assert "[顺序 1/2: 第一步]" in summary


def test_vision_templates_reordering_and_movement(app):
    app.vision_templates = [
        {"id": "t1", "name": "模板1", "threshold": 0.85, "cooldown": 0.5, "button": "左键", "enabled": True, "path": "t1.png"},
        {"id": "t2", "name": "模板2", "threshold": 0.85, "cooldown": 0.5, "button": "左键", "enabled": True, "path": "t2.png"},
        {"id": "t3", "name": "模板3", "threshold": 0.85, "cooldown": 0.5, "button": "左键", "enabled": True, "path": "t3.png"},
    ]
    app.refresh_vision_tree()
    children = app.vision_tree.get_children()
    assert list(children) == ["t1", "t2", "t3"]

    # Reorder drag t1 -> t3
    app._reorder_vision_templates("t1", "t3")
    assert [item["id"] for item in app.vision_templates] == ["t2", "t3", "t1"]
    assert list(app.vision_tree.get_children()) == ["t2", "t3", "t1"]

    # Move t1 up
    app.vision_tree.selection_set("t1")
    app.move_vision_template_up()
    assert [item["id"] for item in app.vision_templates] == ["t2", "t1", "t3"]

    # Move t1 up again
    app.move_vision_template_up()
    assert [item["id"] for item in app.vision_templates] == ["t1", "t2", "t3"]

    # Move t1 up at top boundary (should stay at top)
    app.move_vision_template_up()
    assert [item["id"] for item in app.vision_templates] == ["t1", "t2", "t3"]

    # Move t1 down
    app.move_vision_template_down()
    assert [item["id"] for item in app.vision_templates] == ["t2", "t1", "t3"]

    # Move t3 down at bottom boundary (should stay at bottom)
    app.vision_tree.selection_set("t3")
    app.move_vision_template_down()
    assert [item["id"] for item in app.vision_templates] == ["t2", "t1", "t3"]


def test_vision_sequential_config_and_task_sync(app):
    app.vision_sequential_var.set(True)
    app.change_vision_sequential()
    assert app.vision_sequential_var.get() is True

    # Check sync to current task
    app._sync_current_vision_task()
    task = app.vision_tasks[app.active_vision_task_name]
    assert task["sequential"] is True

    # Check config collection
    config = app._collect_config()
    assert config["vision_sequential"] is True
    assert config["vision_tasks"][app.active_vision_task_name]["sequential"] is True

    # Switch tasks with different sequential settings
    app.vision_tasks["任务2"] = {
        "templates": [],
        "scan_interval": "0.20",
        "sequential": False,
        "immediate": False,
        "background": False,
    }
    app.vision_task_var.set("任务2")
    app.select_vision_task()
    assert app.vision_sequential_var.get() is False

    app.vision_task_var.set(app.active_vision_task_name)
    # Switch back to first task
    first_task = [k for k in app.vision_tasks.keys() if k != "任务2"][0]
    app.vision_task_var.set(first_task)
    app.select_vision_task()
    assert app.vision_sequential_var.get() is True


def test_profile_validation_rejects_non_bool_sequential(app):
    data = {
        "theme": "dark",
        "vision_scan_interval": 0.2,
        "vision_sequential": "not_a_bool",
    }
    with pytest.raises(ValueError, match="顺序执行设置必须是布尔值"):
        app._validate_profile_settings(data)
