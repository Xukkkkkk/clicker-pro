"""Recognition dispatch must not wait on unrelated matching or the Tk thread."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

import main
import vision_engine
from vision_engine import TemplateAction, TemplateSpec, VisionEngine


def test_matching_worker_keeps_clicking_while_ui_is_busy(app, monkeypatch):
    """A blocked UI log update must never hold up the next recognized click."""
    app.vision_running = True
    app.vision_engine = VisionEngine(immediate_click=True)
    app._vision_match_log_at = 0.0
    for name in ("send_click", "send_mouse_down", "send_mouse_up"):
        monkeypatch.setattr(main, name, Mock())
    monkeypatch.setattr(main.mouse, "Controller", Mock(return_value=Mock()))
    match = SimpleNamespace(
        center_x=100, center_y=100, name="target", score=1.0,
        actions=(TemplateAction("click", count=1, interval=0),),
    )
    ui_thread = threading.get_ident()
    release_ui = threading.Event()
    worker_called_tk = threading.Event()
    completed = threading.Event()
    original_after = app.root.after

    def busy_after(*args):
        if threading.get_ident() != ui_thread:
            # Tkinter's cross-thread after call waits for the main thread to
            # service Tcl. Model that wait without sending any real input or
            # relying on Tk's platform-dependent cross-thread timeout.
            worker_called_tk.set()
            release_ui.wait(2)
            return "blocked-worker-callback"
        return original_after(*args)

    monkeypatch.setattr(app.root, "after", busy_after)

    def run_matches():
        try:
            app.on_vision_match(match, app.vision_generation)
            app.on_vision_match(match, app.vision_generation)
        finally:
            completed.set()

    worker = threading.Thread(target=run_matches, daemon=True)
    worker.start()
    try:
        finished_without_ui = completed.wait(0.5)
        clicks_before_ui_available = main.send_click.call_count
    finally:
        release_ui.set()
        worker.join(2)

    assert finished_without_ui, "recognition worker waited for the busy Tk thread"
    assert clicks_before_ui_available == 2
    assert not worker_called_tk.is_set(), "worker accessed Tk to publish its log"
    # Existing UI behavior still receives the queued status once Tk resumes.
    app.root.after(50, app.root.quit)
    app.root.mainloop()
    assert "target" in app.vision_log_var.get()


def test_normal_mode_dispatches_hit_before_searching_later_template(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    events = []
    engine = VisionEngine(
        [TemplateSpec(image=image, id="visible", cooldown=0),
         TemplateSpec(image=image, id="absent", cooldown=0)],
        capture_fn=lambda: np.zeros((40, 60, 3), dtype=np.uint8),
        on_match=lambda match: events.append(("dispatch", match.template_id)),
        on_scan=lambda matches: events.append(("scan", len(matches))),
    )

    def match_template(source, template, prepared, *args, **kwargs):
        key = prepared.spec.key
        events.append(("match", key))
        return [(1.0, 4, 5, 16, 12)] if key == "visible" else []

    monkeypatch.setattr(engine, "_match_scaled", match_template)
    matches = engine.scan_once()

    assert [match.template_id for match in matches] == ["visible"]
    assert events == [("match", "visible"), ("dispatch", "visible"),
                      ("match", "absent"), ("scan", 1)]


def test_read_only_scan_never_dispatches_or_notifies(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    callback = Mock()
    engine = VisionEngine(
        [TemplateSpec(image=image)],
        capture_fn=lambda: np.zeros((40, 60, 3), dtype=np.uint8),
        on_match=callback, on_scan=callback,
    )
    monkeypatch.setattr(engine, "_match_scaled", lambda *args, **kwargs: [(1.0, 4, 5, 16, 12)])

    assert len(engine.scan_once(trigger=False)) == 1
    callback.assert_not_called()


def test_restart_during_dispatch_guard_cannot_click_old_coordinates(app, monkeypatch):
    app.vision_running = True
    app.vision_engine = VisionEngine(immediate_click=True)
    old_generation = app.vision_generation
    for name in ("send_click", "send_mouse_down", "send_mouse_up"):
        monkeypatch.setattr(main, name, Mock())
    controller = Mock()
    monkeypatch.setattr(main.mouse, "Controller", controller)

    def restart_while_checking_window(result):
        app.vision_generation += 1
        app.vision_engine = VisionEngine(immediate_click=True)
        return False

    monkeypatch.setattr(app, "_vision_match_in_own_window", restart_while_checking_window)
    match = SimpleNamespace(center_x=100, center_y=100, name="old target", score=1.0,
                            actions=(TemplateAction("click", count=1, interval=0),))
    app.on_vision_match(match, old_generation)
    controller.assert_not_called()
    main.send_click.assert_not_called()
    main.send_mouse_down.assert_not_called()
    main.send_mouse_up.assert_not_called()


def test_next_template_uses_fresh_frame_after_dispatch(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    before_click = np.zeros((40, 60, 3), dtype=np.uint8)
    after_click = np.ones_like(before_click)
    capture = Mock(side_effect=[before_click, after_click])
    dispatched = []
    notified = []
    engine = VisionEngine(
        [TemplateSpec(image=image, id="first"),
         TemplateSpec(image=image, id="disappeared")],
        capture_fn=capture,
        on_match=lambda match: dispatched.append(match.template_id),
        on_scan=lambda matches: notified.append(matches),
    )

    def match_template(source, template, prepared, *args, **kwargs):
        if prepared.spec.key == "first":
            engine._scan_images["old-frame-pixels"] = source
            return [(1.0, 4, 5, 16, 12)]
        assert not engine._scan_images
        return [] if source[0, 0, 0] == 1 else [(1.0, 30, 5, 16, 12)]

    monkeypatch.setattr(engine, "_match_scaled", match_template)
    matches = engine.scan_once()

    assert engine.last_error is None
    assert [match.template_id for match in matches] == ["first"]
    assert dispatched == ["first"]
    assert notified == [matches]
    assert capture.call_count == 2


def test_normal_cooldowns_skip_dispatch_without_extra_captures(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    capture = Mock(return_value=np.zeros((40, 60, 3), dtype=np.uint8))
    dispatch = Mock()
    notify = Mock()
    engine = VisionEngine(
        [TemplateSpec(image=image, id="first", cooldown=60),
         TemplateSpec(image=image, id="second", cooldown=60)],
        capture_fn=capture, on_match=dispatch, on_scan=notify,
    )
    monkeypatch.setattr(engine, "_match_scaled", lambda *args, **kwargs: [(1.0, 4, 5, 16, 12)])

    assert len(engine.scan_once()) == 2
    assert capture.call_count == 2
    assert dispatch.call_count == 2
    assert len(engine.scan_once()) == 2
    assert capture.call_count == 3
    assert dispatch.call_count == 2
    assert notify.call_count == 2


def test_stop_from_first_dispatch_prevents_later_capture_and_dispatch(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    capture = Mock(return_value=np.zeros((40, 60, 3), dtype=np.uint8))
    stop = threading.Event()
    dispatched = []
    notify = Mock()

    def dispatch(match):
        dispatched.append(match.template_id)
        stop.set()

    engine = VisionEngine(
        [TemplateSpec(image=image, id="first"), TemplateSpec(image=image, id="second")],
        capture_fn=capture, on_match=dispatch, on_scan=notify,
    )
    engine._stop = stop
    monkeypatch.setattr(engine, "_match_scaled", lambda *args, **kwargs: [(1.0, 4, 5, 16, 12)])

    matches = engine.scan_once()

    assert dispatched == ["first"]
    assert [match.template_id for match in matches] == ["first"]
    capture.assert_called_once()
    notify.assert_called_once_with(matches)


def test_dispatch_sees_current_detection_time_excluding_previous_actions(monkeypatch):
    image = np.arange(12 * 16 * 3, dtype=np.uint8).reshape(12, 16, 3)
    clock = [0.0]
    times_at_dispatch = []
    monkeypatch.setattr(vision_engine.time, "perf_counter", lambda: clock[0])

    def capture():
        clock[0] += .01
        return np.zeros((40, 60, 3), dtype=np.uint8)

    def match_template(*args, **kwargs):
        clock[0] += .02
        return [(1.0, 4, 5, 16, 12)]

    def dispatch(match):
        times_at_dispatch.append(round(engine.last_scan_ms))
        clock[0] += 3.0

    engine = VisionEngine(
        [TemplateSpec(image=image, id="first"), TemplateSpec(image=image, id="second")],
        capture_fn=capture, on_match=dispatch,
    )
    engine.last_scan_ms = 99999
    monkeypatch.setattr(engine, "_match_scaled", match_template)

    engine.scan_once()

    assert times_at_dispatch == [30, 60]
    assert round(engine.last_scan_ms) == 60
    assert round(engine.last_capture_ms) == 20
