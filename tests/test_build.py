"""Build failures must never be reported as successful releases."""
import ctypes
import os
import shutil
import stat
import subprocess
import time
from unittest.mock import Mock

import pytest

import build


@pytest.fixture
def output(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(build, "stop_running_output", Mock())
    path = tmp_path / "dist" / "ClickerPro.exe"
    path.parent.mkdir()
    path.write_bytes(b"previous executable")
    yield path
    path.unlink(missing_ok=True)


def test_dependency_failure_does_not_start_packaging(output, monkeypatch, capsys):
    run = Mock(return_value=Mock(returncode=7))
    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.build() == 7
    run.assert_called_once()
    assert "pip" in run.call_args.args[0]
    assert "Build complete:" not in capsys.readouterr().out
    assert output.read_bytes() == b"previous executable"


def test_packaging_failure_does_not_report_old_exe_as_success(output, monkeypatch, capsys):
    run = Mock(side_effect=[Mock(returncode=0), Mock(returncode=1)])
    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.build() == 1
    assert run.call_count == 2
    captured = capsys.readouterr()
    assert "Build complete:" not in captured.out
    assert "Build NOT complete" in captured.err
    assert output.read_bytes() == b"previous executable"


def test_success_uses_current_python_and_project_directory(output, monkeypatch, capsys):
    def run(command, *, cwd, check):
        assert command[0] == build.sys.executable
        assert cwd == build.PROJECT_DIR
        assert "PyInstaller" in command
        output.write_bytes(b"new executable")
        return Mock(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.build(install_dependencies=False) == 0
    assert "Build complete:" in capsys.readouterr().out


def test_missing_output_is_failure(output, monkeypatch, capsys):
    output.unlink()
    monkeypatch.setattr(build.subprocess, "run", Mock(return_value=Mock(returncode=0)))
    assert build.build(install_dependencies=False) == 1
    assert "Build complete:" not in capsys.readouterr().out


def test_shutdown_happens_before_lock_check_and_packaging(output, monkeypatch):
    stages = []
    monkeypatch.setattr(build, "stop_running_output", lambda path: stages.append("stop"))
    monkeypatch.setattr(build, "check_output_available", lambda path: stages.append("check"))

    def run(*args, **kwargs):
        stages.append("package")
        return Mock(returncode=0)

    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.build(install_dependencies=False) == 0
    assert stages == ["stop", "check", "package"]


def test_failed_shutdown_aborts_build(output, monkeypatch, capsys):
    monkeypatch.setattr(build, "stop_running_output", Mock(side_effect=OSError("Access denied")))
    run = Mock()
    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.build() == 1
    run.assert_not_called()
    assert "Build complete:" not in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing rules")
def test_locked_exe_stops_build_before_installing_dependencies(output, monkeypatch, capsys):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    close = kernel32.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    close.restype = ctypes.c_int
    handle = create(str(output), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    run = Mock()
    monkeypatch.setattr(build.subprocess, "run", run)
    try:
        assert build.build() == 1
        run.assert_not_called()
        captured = capsys.readouterr()
        assert "Build blocked" in captured.err
        assert "Build complete:" not in captured.out
    finally:
        close(handle)
    assert output.read_bytes() == b"previous executable"


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute")
def test_read_only_exe_stops_build(output, monkeypatch):
    run = Mock()
    monkeypatch.setattr(build.subprocess, "run", run)
    output.chmod(stat.S_IREAD)
    try:
        assert build.build() == 1
        run.assert_not_called()
    finally:
        output.chmod(stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable path matching")
def test_shutdown_only_terminates_matching_executable_path(tmp_path):
    executables = []
    processes = []
    environment = os.environ.copy()
    environment["PYTHONHOME"] = build.sys.prefix
    environment["PATH"] = str(build.Path(build.sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    try:
        for name in ("current", "other"):
            folder = tmp_path / name
            folder.mkdir()
            executable = folder / "ClickerPro.exe"
            shutil.copy2(build.sys.executable, executable)
            executables.append(executable)
            marker = folder / "ready"
            process = subprocess.Popen(
                [str(executable), "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(30)", str(marker)],
                env=environment, creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            processes.append(process)
            deadline = time.monotonic() + 5
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert marker.exists(), "Test helper failed to start"
        build.stop_running_output(executables[0])
        assert processes[0].wait(timeout=5) != 0
        assert processes[1].poll() is None
        build.check_output_available(executables[0])
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            if process.stderr:
                process.stderr.close()
        for executable in executables:
            executable.unlink(missing_ok=True)
