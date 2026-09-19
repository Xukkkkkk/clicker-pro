"""Input delivery contracts; never inject into the user's desktop."""
import ctypes
import threading
from unittest.mock import Mock

import pytest

import core


@pytest.fixture(params=["foreground", "background"])
def delivery(request, monkeypatch):
    edges = []
    if request.param == "foreground":
        monkeypatch.setattr(core, "send_mouse_down", lambda button: edges.append("down"))
        monkeypatch.setattr(core, "send_mouse_up", lambda button: edges.append("up"))
        click = core.send_click
    else:
        def post(hwnd, message, flags, point):
            if message == 0x0201:
                edges.append("down")
            elif message == 0x0202:
                edges.append("up")
            return True

        monkeypatch.setattr(core, "_user32", Mock(PostMessageW=Mock(side_effect=post)))
        monkeypatch.setattr(core, "_window_message_point", Mock(return_value=(456, 123)))
        click = lambda **kwargs: core.post_window_click(123, 10, 20, **kwargs)
    return click, edges


def test_short_press_sends_down_before_wait_and_releases(delivery):
    click, edges = delivery
    stop = Mock(is_set=Mock(return_value=False))

    def wait(duration):
        assert edges == ["down"]
        assert duration == 0.04
        return False

    stop.wait.side_effect = wait
    click(duration=0.04, stop_event=stop)
    assert edges == ["down", "up"]
    stop.wait.assert_called_once_with(0.04)


def test_default_click_has_no_added_wait(delivery, monkeypatch):
    click, edges = delivery
    sleep = Mock()
    monkeypatch.setattr(core.time, "sleep", sleep)
    click()
    assert edges == ["down", "up"]
    sleep.assert_not_called()


def test_short_press_without_stop_event_still_releases(delivery, monkeypatch):
    click, edges = delivery

    def sleep(duration):
        assert edges == ["down"]
        assert duration == 0.04

    monkeypatch.setattr(core.time, "sleep", sleep)
    click(duration=0.04)
    assert edges == ["down", "up"]


def test_preexisting_stop_sends_no_button_event(delivery):
    click, edges = delivery
    stop = threading.Event()
    stop.set()
    click(duration=0.04, stop_event=stop)
    assert not edges


def test_stop_during_press_releases(delivery):
    click, edges = delivery
    stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))
    click(duration=0.04, stop_event=stop)
    assert edges == ["down", "up"]


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_interrupted_wait_releases_button(delivery, error_type):
    click, edges = delivery
    stop = Mock(is_set=Mock(return_value=False),
                wait=Mock(side_effect=error_type("interrupted")))
    with pytest.raises(error_type, match="interrupted"):
        click(duration=0.04, stop_event=stop)
    assert edges == ["down", "up"]


@pytest.mark.parametrize("duration", [-0.1, float("nan"), float("inf")])
def test_invalid_duration_never_presses(delivery, duration):
    click, edges = delivery
    with pytest.raises(ValueError, match="duration"):
        click(duration=duration)
    assert not edges


@pytest.mark.parametrize("button, messages", [
    ("left", [(0x0200, 0), (0x0201, 1), (0x0202, 0)]),
    ("right", [(0x0200, 0), (0x0204, 2), (0x0205, 0)]),
    ("Button.middle", [(0x0200, 0), (0x0207, 16), (0x0208, 0)]),
])
def test_background_down_up_keep_same_control_and_point(monkeypatch, button, messages):
    resolve = Mock(side_effect=[(456, 0x00200010), (789, 999)])
    post = Mock(return_value=True)
    monkeypatch.setattr(core, "_window_message_point", resolve)
    monkeypatch.setattr(core, "_user32", Mock(PostMessageW=post))
    core.post_window_click(123, 10, 20, button)
    resolve.assert_called_once_with(123, 10, 20)
    assert [call.args for call in post.call_args_list] == [
        (456, message, flags, 0x00200010) for message, flags in messages
    ]


def test_background_double_click_keeps_second_half_message(monkeypatch):
    post = Mock(return_value=True)
    monkeypatch.setattr(core, "_window_message_point", Mock(return_value=(456, 123)))
    monkeypatch.setattr(core, "_user32", Mock(PostMessageW=post))
    core.post_window_click(123, 10, 20, "right", double_click=True)
    assert [call.args[1] for call in post.call_args_list] == [0x0200, 0x0206, 0x0205]


def test_foreground_down_failure_attempts_release(monkeypatch):
    up = Mock()
    monkeypatch.setattr(core, "send_mouse_down", Mock(side_effect=OSError("down failed")))
    monkeypatch.setattr(core, "send_mouse_up", up)
    with pytest.raises(OSError, match="down failed"):
        core.send_click("right", duration=0.04)
    up.assert_called_once_with("right")


@pytest.mark.skipif(core._send_input is None, reason="Windows SendInput ABI")
@pytest.mark.parametrize("error_code", [0, 5])
def test_sendinput_failure_does_not_report_success_or_stale_error(monkeypatch, error_code):
    def rejected(count, inputs, size):
        assert ctypes.get_last_error() == 0
        ctypes.set_last_error(error_code)
        return 0

    monkeypatch.setattr(core, "_send_input", rejected)
    ctypes.set_last_error(123)
    with pytest.raises(OSError) as raised:
        core._send_mouse_flag(core._MOUSEEVENTF_LEFTDOWN)
    if error_code:
        assert raised.value.winerror == error_code
    else:
        assert "SendInput" in str(raised.value)
        assert "权限" in str(raised.value)
        assert getattr(raised.value, "winerror", None) != 123


@pytest.mark.skipif(core._send_input is None, reason="Windows SendInput ABI")
def test_sendinput_mouse_structure_and_flags(monkeypatch):
    sent = []

    def accepted(count, inputs, size):
        assert count == 1
        assert size == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        event = ctypes.cast(inputs, ctypes.POINTER(core._INPUT)).contents
        assert event.type == 0
        assert (event.mi.dx, event.mi.dy, event.mi.mouseData) == (0, 0, 0)
        sent.append(event.mi.dwFlags)
        return 1

    monkeypatch.setattr(core, "_send_input", accepted)
    core.send_click("left")
    core.send_click("right")
    core.send_click("middle")
    assert sent == [0x0002, 0x0004, 0x0008, 0x0010, 0x0020, 0x0040]
