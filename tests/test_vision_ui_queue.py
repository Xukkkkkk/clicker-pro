"""Recognition notifications remain bounded and respect stop/restart."""
import threading
from unittest.mock import Mock

import pytest


def pump_ui(app):
    app.root.after(50, app.root.quit)
    app.root.mainloop()


@pytest.mark.parametrize("error_first", [False, True])
def test_busy_ui_coalesces_updates_and_retains_error(app, monkeypatch, error_first):
    app.vision_running = True
    engine = Mock(last_error=None)
    engine.scan_summary.return_value = "missing target"
    app.vision_engine = engine
    tk_calls = Mock(side_effect=AssertionError("worker called Tk"))
    original_after = app.root.after
    monkeypatch.setattr(app.root, "after", tk_calls)

    def publish():
        if error_first:
            app.on_vision_error("input rejected", app.vision_generation)
        for _ in range(1000):
            app._vision_diagnostic_at = 0
            app.on_vision_scan([], app.vision_generation)
        if not error_first:
            app.on_vision_error("input rejected", app.vision_generation)

    worker = threading.Thread(target=publish)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    tk_calls.assert_not_called()
    assert len(app._vision_ui_pending) == 2
    monkeypatch.setattr(app.root, "after", original_after)
    pump_ui(app)
    assert "input rejected" in app.vision_log_var.get()


def test_notifications_from_stopped_session_cannot_overwrite_current_status(app):
    generation = app.vision_generation
    app._queue_vision_ui("status", app.vision_match_ui, "old", 1.0, 10, 20, generation,
                         generation=generation)
    app.on_vision_error("old error", generation)
    app._queue_vision_ui("test", app.show_vision_scan_result,
                         [], "", False, "old scan", generation, generation=generation)
    app.stop_vision()
    app.vision_log_var.set("new session")
    pump_ui(app)
    assert app.vision_log_var.get() == "new session"


def test_late_old_scan_cannot_replace_new_test_completion(app):
    old_generation = app.vision_generation
    app.vision_generation += 1
    generation = app.vision_generation
    app._vision_test_running = True
    app._queue_vision_ui("test", app.show_vision_scan_result,
                         [], "", False, "new scan", generation, generation=generation)
    app._queue_vision_ui("test", app.show_vision_scan_result,
                         [], "", False, "old scan", old_generation, generation=old_generation)
    pump_ui(app)
    assert "new scan" in app.vision_log_var.get()
    assert not app._vision_test_running


def test_close_discards_pending_callbacks_and_cancels_poll(app):
    callback = Mock()
    app._queue_vision_ui("status", callback, generation=app.vision_generation)
    app.close()
    app._queue_vision_ui("status", callback, generation=app.vision_generation)
    assert app._vision_ui_job is None
    assert app._vision_ui_pending == {}
    callback.assert_not_called()
