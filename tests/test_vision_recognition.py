"""Regression coverage for scaled captures and recognition lifecycle."""
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image

import main
from vision_engine import ScreenFrame, TemplateSpec, VisionEngine


def button_image():
    image = np.full((48, 128, 3), (30, 170, 240), dtype=np.uint8)
    cv2.rectangle(image, (3, 3), (124, 44), (10, 40, 70), 2)
    cv2.putText(image, "START", (12, 33), cv2.FONT_HERSHEY_SIMPLEX, .8, (250, 250, 250), 2)
    return image


@pytest.mark.parametrize("scale", [.5, .75, 1., 1.25, 1.5, 2.])
@pytest.mark.parametrize("background", [False, True])
def test_scaled_buttons_match_desktop_and_window_captures(scale, background):
    template = button_image()
    target = cv2.resize(template, (round(128*scale), round(48*scale)),
                        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    frame = np.full((360, 540, 3), 40, dtype=np.uint8)
    frame[80:80+target.shape[0], 110:110+target.shape[1]] = target
    capture = (Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)) if background
               else ScreenFrame(frame, origin_x=-600, origin_y=20))
    engine = VisionEngine([TemplateSpec(image=template, threshold=.90)], capture_fn=lambda: capture)
    matches = engine.scan_once(trigger=False)
    assert engine.last_error is None
    assert len(matches) == 1
    match = matches[0]
    assert match.score >= .90
    origin_x, origin_y = (0, 0) if background else (-600, 20)
    assert abs(match.center_x - (origin_x + 110 + target.shape[1]//2)) <= 2
    assert abs(match.center_y - (origin_y + 80 + target.shape[0]//2)) <= 2
    # After finding the scale, repeating scans use that size first.
    assert engine.scan_once(trigger=False)[0].center == match.center


def test_self_preview_does_not_consume_only_match_slot():
    template = button_image()
    frame = np.full((300, 500, 3), 35, dtype=np.uint8)
    frame[20:68, 20:148] = template
    frame[160:208, 300:428] = template
    engine = VisionEngine([TemplateSpec(image=template, threshold=.95, max_matches=1)],
                          capture_fn=lambda: frame,
                          exclude_regions=lambda: [(0, 0, 180, 100)])
    matches = engine.scan_once(trigger=False)
    assert [(m.x, m.y) for m in matches] == [(300, 160)]


def test_blank_capture_diagnostic_and_no_false_clicks():
    on_match = Mock()
    engine = VisionEngine([TemplateSpec(image=button_image())],
                          capture_fn=lambda: np.zeros((300, 500, 3), dtype=np.uint8),
                          on_match=on_match)
    assert engine.scan_once() == []
    on_match.assert_not_called()
    assert "截图为空白" in engine.scan_summary()


def test_unrelated_frame_does_not_pass_threshold():
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 256, (300, 500, 3), dtype=np.uint8)
    engine = VisionEngine([TemplateSpec(image=button_image(), threshold=.85)], capture_fn=lambda: frame)
    assert engine.scan_once(trigger=False) == []
    assert "最高匹配度" in engine.scan_summary()
    assert engine.last_scan_details[0]["score"] < .85


def test_multiple_occurrences_are_distinct():
    template = button_image()
    frame = np.full((300, 500, 3), 35, dtype=np.uint8)
    frame[20:68, 20:148] = template
    frame[160:208, 300:428] = template
    engine = VisionEngine([TemplateSpec(image=template, threshold=.95, max_matches=2)], capture_fn=lambda: frame)
    assert {(m.x, m.y) for m in engine.scan_once(trigger=False)} == {(20, 20), (300, 160)}


def test_old_capture_error_is_cleared_after_recovery():
    template = button_image()
    engine = VisionEngine([TemplateSpec(image=template)], capture_fn=lambda: template)
    engine.last_error = RuntimeError("previous error")
    assert engine.scan_once(trigger=False)
    assert engine.last_error is None


def test_minimizing_clears_excluded_window_region(app):
    app._vision_window_rect = (0, 0, 1120, 760)
    app.root.withdraw()
    app._update_vision_window_rect()
    assert app._vision_excluded_regions() == ()
    assert not app._vision_match_in_own_window(SimpleNamespace(x=50, y=50, width=20, height=20))


def test_stale_test_scan_does_not_reveal_preview_of_running_scan(app):
    app.vision_generation = 10
    app.vision_running = True
    app._set_vision_preview_hidden(True)
    app.vision_log_var.set("active scan")
    app.show_vision_scan_result([], generation=9)
    assert app._vision_preview_hidden_for_scan
    assert app.vision_log_var.get() == "active scan"


@pytest.mark.parametrize("background", [False, True])
def test_test_scan_stops_active_clicking_and_remains_read_only(app, monkeypatch, tmp_path, background):
    path = tmp_path / "button.png"
    Image.fromarray(cv2.cvtColor(button_image(), cv2.COLOR_BGR2RGB)).save(path)
    app.vision_templates = [dict(id="target", path=str(path), name="target", enabled=True)]
    app.vision_background_var.set(background)
    app.vision_running = True
    previous = Mock()
    app.vision_engine = previous
    worker = Mock()
    monkeypatch.setattr(main.threading, "Thread", Mock(return_value=worker))
    if background:
        monkeypatch.setattr(app, "_resolve_background_targets", lambda: [dict(hwnd=123, title="Target")])
    app.scan_vision_once()
    previous.stop.assert_called_once_with(wait=True)
    assert not app.vision_running
    assert app._vision_test_running
    worker.start.assert_called_once()
    app.stop_vision()
    app.show_vision_scan_result([], generation=app.vision_generation-1)
    assert not app._vision_test_running
    assert not app._vision_preview_hidden_for_scan
