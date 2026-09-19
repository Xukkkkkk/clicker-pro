"""Build the single Windows executable and report failures accurately."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import stat
import subprocess
import sys


PROJECT_DIR = Path(__file__).resolve().parent


def stop_running_output(output: Path) -> None:
    """Stop only processes running this project's output executable."""
    if os.name != "nt":
        return
    script = r"""
$ErrorActionPreference = 'Stop'
$expectedPath = [IO.Path]::GetFullPath($env:CLICKER_BUILD_OUTPUT)
$targets = @(Get-CimInstance Win32_Process -Filter "Name = 'ClickerPro.exe'" |
    Where-Object { $_.ExecutablePath -and [string]::Equals(
        [IO.Path]::GetFullPath($_.ExecutablePath), $expectedPath,
        [StringComparison]::OrdinalIgnoreCase) })
foreach ($target in $targets) {
    $process = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { continue }
    try {
        # Recheck the opened process: a PID may have exited and been reused.
        if (-not [string]::Equals($process.Path, $expectedPath,
            [StringComparison]::OrdinalIgnoreCase)) { continue }
        Stop-Process -InputObject $process -Force -ErrorAction Stop
        if (-not $process.WaitForExit(15000)) {
            throw 'Timed out waiting for ClickerPro.exe to exit.'
        }
        Write-Output ('Stopped ClickerPro.exe (PID ' + $target.ProcessId + ').')
    } finally {
        $process.Dispose()
    }
}
"""
    environment = os.environ.copy()
    environment["CLICKER_BUILD_OUTPUT"] = str(output.resolve())
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=environment, capture_output=True, text=True, errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode:
        raise OSError(result.stderr.strip() or "Could not stop the running executable")


def check_output_available(output: Path) -> None:
    """Check replacement access without changing or deleting the existing EXE."""
    if not output.exists():
        return
    if os.name != "nt":
        return
    if output.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY:
        raise PermissionError(f"Output file is read-only: {output}")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                           ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    # GENERIC_WRITE | DELETE; allow other readers/writers. A running image,
    # incompatible sharing lock or ACL denial should fail before packaging.
    handle = create_file(str(output), 0x40000000 | 0x00010000, 0x7, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    close_handle(handle)


def build(*, install_dependencies: bool = True) -> int:
    output = PROJECT_DIR / "dist" / "ClickerPro.exe"
    try:
        stop_running_output(output)
        check_output_available(output)
    except OSError as exc:
        print(f"Build blocked: cannot replace {output}\n{exc}", file=sys.stderr)
        print("Automatic shutdown or replacement failed. Check Task Manager,\n"
              "read-only status, folder permissions,\n"
              "and security software's protection history.", file=sys.stderr)
        return 1

    commands = []
    if install_dependencies:
        commands.append(("Dependency installation", [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]))
    commands.append(("Packaging", [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "ClickerPro.spec"]))
    for stage, command in commands:
        try:
            result = subprocess.run(command, cwd=PROJECT_DIR, check=False)
        except OSError as exc:
            print(f"{stage} failed to start: {exc}", file=sys.stderr)
            return 1
        if result.returncode:
            print(f"{stage} failed (exit code {result.returncode}). Build NOT complete.", file=sys.stderr)
            if stage == "Packaging":
                print("If the log reports WinError 5 for ClickerPro.exe, close it and retry.\n"
                      "Any existing EXE may still be the previous version.", file=sys.stderr)
            return result.returncode

    if not output.is_file() or output.stat().st_size == 0:
        print(f"Build failed: no executable was produced at {output}", file=sys.stderr)
        return 1
    print(f"Build complete: {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-deps", action="store_true", help="Use already-installed build dependencies")
    args = parser.parse_args()
    return build(install_dependencies=not args.skip_deps)


if __name__ == "__main__":
    raise SystemExit(main())
