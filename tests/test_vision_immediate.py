"""Immediate recognition reduces dispatch latency without changing thresholds."""
import json
import threading
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image

import main
from vision_engine import TemplateAction, TemplateSpec, VisionEngine


def target(text="GO"):
    image = np.full((40, 96, 3), (15, 110, 190), dtype=np.uint8)
    cv2.rectangle(image, (1, 1), (94, 38), (235, 235, 235), 2)
    cv2.putText(image, text, (10, 29), cv2.FONT_HERSHEY_SIMPLEX, .8, (255, 255, 255), 2)
    return image


def frame_with(image):
    frame = np.full((240, 480, 3), 30, dtype=np.uint8)
    frame[60:60+image.shape[0], 100:100+image.shape[1]] = image
    return frame


def test_click_precedes_later_template_and_scan_notification(tmp_path):
    events = []
    image = target()
    engine = VisionEngine(
        [TemplateSpec(image=image, id="visible"),
         TemplateSpec(path=tmp_path / "missing.png", id="later")],
        capture_fn=lambda: frame_with(image), immediate_click=True,
        on_match=lambda match: events.append("click"),
        on_scan=lambda matches: events.append("scan"),
        on_error=lambda error: events.append("error"),
    )
    engine.scan_once()
    assert events == ["click", "scan"]
    assert engine.last_error is None


def test_absent_template_does_not_delay_visible_target_with_scale_sweep(monkeypatch):
    image = target()
    engine = VisionEngine(
        [TemplateSpec(image=target("NO"), id="absent", threshold=.99),
         TemplateSpec(image=image, id="visible", threshold=.99)],
        capture_fn=lambda: frame_with(image), immediate_click=True,
    )
    score_map = engine._score_map
    sizes = []

    def score(source, template, cv, np_module):
        sizes.append(template.shape[:2])
        return score_map(source, template, cv, np_module)

    monkeypatch.setattr(engine, "_score_map", score)
    matches = engine.scan_once()
    assert [match.template_id for match in matches] == ["visible"]
    assert sizes == [image.shape[:2], image.shape[:2]]


@pytest.mark.parametrize("immediate, expected", [(False, 1), (True, 2)])
def test_immediate_bypasses_cooldown_and_uses_single_click(immediate, expected):
    image = target()
    spec = TemplateSpec(image=image, cooldown=60, button="right",
                        actions=[TemplateAction("wait", duration=10), TemplateAction("hold", duration=5)])
    received = []
    engine = VisionEngine([spec], capture_fn=lambda: frame_with(image),
                          immediate_click=immediate, on_match=received.append)
    engine.scan_once()
    engine.scan_once()
    assert len(received) == expected
    assert received[0].actions == ((TemplateAction("click", "right", count=1, interval=0),)
                                   if immediate else spec.action_plan)
    assert spec.cooldown == 60 and spec.action_plan[0].kind == "wait"


def test_rotation_is_fair_and_recaptures_between_clicks():
    image = target()
    other = target("NEXT")
    frame = frame_with(image)
    frame[150:190, 300:396] = other
    events = []
    capture = Mock(return_value=frame)
    engine = VisionEngine([TemplateSpec(image=image, id="one", threshold=.99),
                           TemplateSpec(image=other, id="two", threshold=.99)],
                          immediate_click=True, capture_fn=capture,
                          on_match=lambda match: events.append(match.template_id))
    for _ in range(4):
        engine.scan_once()
    assert events == ["one", "two", "one", "two"]
    assert capture.call_count == 4


def test_immediate_test_scan_is_read_only_and_finds_scaled_targets():
    image = target()
    scaled = cv2.resize(image, (144, 60))
    callback = Mock()
    engine = VisionEngine([TemplateSpec(image=image, threshold=.90)],
                          immediate_click=True, capture_fn=lambda: frame_with(scaled),
                          on_match=callback, on_scan=callback)
    assert engine.scan_once(trigger=False)
    callback.assert_not_called()
    assert not engine._last_fired


def test_incremental_scale_search_eventually_finds_target():
    image = target()
    scaled = cv2.resize(image, (round(96*1.65), round(40*1.65)))
    engine = VisionEngine([TemplateSpec(image=image, threshold=.95)],
                          immediate_click=True, capture_fn=lambda: frame_with(scaled))
    matches = []
    for _ in range(12):
        matches = engine.scan_once()
        if matches:
            break
    assert matches
    assert matches[0].score >= .95
    assert abs(matches[0].center_x - (100 + scaled.shape[1]//2)) <= 2


def test_stop_during_capture_prevents_immediate_click():
    stop = threading.Event()
    callback = Mock()
    image = target()

    def capture():
        stop.set()
        return frame_with(image)

    engine = VisionEngine([TemplateSpec(image=image)], immediate_click=True,
                          capture_fn=capture, on_match=callback)
    engine._stop = stop
    assert engine.scan_once() == []
    callback.assert_not_called()


def test_immediate_loop_does_not_wait_configured_interval():
    engine = VisionEngine(immediate_click=True, interval=10)
    stop = Mock()
    stop.is_set.side_effect = [False, True]
    engine.scan_once = Mock()
    engine._run(stop, 0)
    stop.wait.assert_called_once_with(.001)


@pytest.mark.parametrize("background", [False, True])
def test_ui_runs_immediate_mode_and_keeps_saved_actions(app, monkeypatch, tmp_path, background):
    image = target()
    path = tmp_path / "target.png"
    Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).save(path)
    saved_actions = [dict(kind="hold", button="right", duration=5)]
    app.vision_templates = [dict(id="target", path=str(path), name="target", enabled=True,
                                 button="右键", cooldown=60, actions=saved_actions)]
    app.vision_background_var.set(background)
    app.vision_immediate_var.set(True)
    app.change_vision_immediate()
    assert json.loads(main.CONFIG_FILE.read_text(encoding="utf-8"))["vision_immediate"] is True
    assert str(app.vision_scan_combo.cget("state")) == "disabled"
    monkeypatch.setattr(main.VisionEngine, "start", Mock())
    if background:
        monkeypatch.setattr(app, "_resolve_background_targets", lambda: [dict(hwnd=123, title="Target")])
    for method in ("post_window_mouse_move", "post_window_click", "post_window_mouse_down", "post_window_mouse_up", "send_click", "send_mouse_down", "send_mouse_up"):
        monkeypatch.setattr(main, method, Mock())
    monkeypatch.setattr(main.mouse, "Controller", Mock(return_value=Mock()))
    app.start_vision()
    engine = app.vision_engine
    assert engine.immediate_click
    engine.capture_fn = lambda: frame_with(image)
    engine.scan_once()
    if background:
        main.post_window_click.assert_called_once_with(123, 148, 80, "right")
        main.post_window_mouse_down.assert_not_called()
    else:
        main.send_click.assert_called_once_with("right")
        main.send_mouse_down.assert_not_called()
    assert app.vision_templates[0]["actions"] == saved_actions
    assert app.vision_templates[0]["cooldown"] == 60
    app.root.update()
    assert "检测" in app.vision_log_var.get() and "执行" in app.vision_log_var.get()
    app.vision_immediate_var.set(False)
    app.change_vision_immediate()
    assert not app.vision_engine.immediate_click
    assert app.vision_engine.templates[0].action_plan[0].kind == "hold"
    assert str(app.vision_scan_combo.cget("state")) == "readonly"


def test_immediate_setting_profile_roundtrip(app, monkeypatch, tmp_path):
    app.vision_immediate_var.set(True)
    payload, assets, _ = app._prepare_profile_bundle()
    profile = tmp_path / "immediate.clickerprofile"
    assert app._write_profile_archive(profile, payload, assets)
    monkeypatch.setattr(main, "PROFILE_ASSET_DIR", tmp_path / "assets")
    monkeypatch.setattr(main.filedialog, "askopenfilename", lambda **kwargs: str(profile))
    app.vision_immediate_var.set(False)
    assert app.import_profile()
    assert app.vision_immediate_var.get()
    app.vision_immediate_var.set(False)
    app.load_config()
    assert app.vision_immediate_var.get()
