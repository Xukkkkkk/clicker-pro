"""Recording regressions without installing hooks or sending desktop input."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    app = main.ClickerApp.__new__(main.ClickerApp)
    app.root = Mock()
    app.closing = app.running = app.playing = app.recording = False
    app.vision_running = False
    app.record_session_id = 0
    app.record_listener = None
    app.events = []
    app.event_lock = threading.Lock()
    app.record_background_var = Mock(get=Mock(return_value=False))
    app.record_include_moves_var = Mock(get=Mock(return_value=True))
    app.record_button = Mock()
    app.record_status_var = Mock()
    app.record_empty_label = Mock()
    app.event_tree = Mock(get_children=Mock(return_value=()))
    app.set_status = Mock()
    app.stop_clicking = Mock()
    app.stop_playback = Mock()
    app.play_run_id = 1
    app.play_button = Mock()
    listeners, callbacks = [], []
    listener_class = main.mouse.Listener

    def make_listener(**kwargs):
        # Use pynput's real callback argument adaptation, but never start a
        # system hook. In 1.8+, dispatch includes an extra injected flag.
        listener = listener_class(**kwargs)
        listener.start = Mock()
        listener.stop = Mock()
        listener.join = Mock()
        listeners.append(listener)
        callbacks.append(kwargs)
        return listener

    monkeypatch.setattr(main.mouse, "Listener", make_listener)
    monkeypatch.setattr(main, "RECORD_FILE", tmp_path / "recording.json")
    return SimpleNamespace(app=app, listeners=listeners, callbacks=callbacks)


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("previous_session", [0, 6])
def test_listener_records_saves_reloads_and_replays(
        recorder, monkeypatch, injected, previous_session):
    app = recorder.app
    app.record_session_id = previous_session
    app.start_recording()
    listener = recorder.listeners[-1]
    listener.on_move(120, 240, injected)
    listener.on_click(130, 250, main.mouse.Button.right, True, injected)
    listener.on_click(130, 250, main.mouse.Button.right, False, injected)
    assert [event["type"] for event in app.events] == ["move", "click"]

    # Simulate pressing Stop before Tk has rendered queued event rows.
    app.stop_recording()
    saved = json.loads(main.RECORD_FILE.read_text(encoding="utf-8"))
    assert saved == app.events
    assert app.event_tree.insert.call_count == 2
    app.set_status.assert_called_with("录制已保存", "success")

    app.events = []
    app.load_recording()
    assert app.events == saved
    controller = Mock()
    click = Mock()
    monkeypatch.setattr(main.mouse, "Controller", Mock(return_value=controller))
    monkeypatch.setattr(main, "send_click", click)
    app.play_worker(app.events, 1000, 1, app.play_run_id, threading.Event())
    assert controller.position == (130, 250)
    click.assert_called_once_with("right")
    app.root.after.assert_called_with(
        0, app.finish_playback, app.play_run_id, None, 1)


def test_legacy_callbacks_and_stale_listener_after_restart(recorder):
    app = recorder.app
    app.start_recording()
    callbacks = recorder.callbacks[-1]
    callbacks["on_move"](10, 20)
    callbacks["on_click"](30, 40, main.mouse.Button.left, True)
    assert len(app.events) == 2
    app.stop_recording()
    app.start_recording()

    callbacks["on_move"](50, 60)
    callbacks["on_click"](50, 60, main.mouse.Button.left, True)
    assert app.events == []
    recorder.listeners[-1].on_click(70, 80, main.mouse.Button.middle, True, False)
    assert len(app.events) == 1
    assert app.events[0]["button"] == "middle"


def test_stop_all_keeps_save_failure_visible(recorder, monkeypatch):
    app = recorder.app
    app.start_recording()
    recorder.callbacks[-1]["on_click"](30, 40, main.mouse.Button.left, True)
    monkeypatch.setattr(app, "write_json", Mock(return_value=False))
    app.stop_all()
    assert len(app.events) == 1
    assert app.set_status.call_args.args[1] == "danger"
    assert "保存失败" in app.record_status_var.set.call_args.args[0]
    app.record_button.configure.assert_called_with(text="●  开始录制")


def test_empty_recording_is_not_reported_as_success(recorder):
    app = recorder.app
    app.start_recording()
    app.stop_all()
    assert app.set_status.call_args.args[1] == "warning"
    assert "未录制到动作" in app.record_status_var.set.call_args.args[0]


def test_listener_start_failure_resets_record_button(recorder, monkeypatch):
    listener = Mock()
    listener.start.side_effect = OSError("hook unavailable")
    monkeypatch.setattr(main.mouse, "Listener", Mock(return_value=listener))
    app = recorder.app
    app.start_recording()
    assert not app.recording
    assert app.record_listener is None
    app.record_button.configure.assert_called_with(text="●  开始录制")
    assert app.set_status.call_args.args[1] == "danger"
