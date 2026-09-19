# Windows build

1. Install Python 3.10+ (64-bit recommended), then run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

2. Run from source:

```powershell
python main.py
```

3. Double-click `build.bat` or build a standalone executable:

```powershell
python build.py
```

Use `dist\ClickerPro.exe` as the single executable for all themes and features on Windows 10/11. Before building, the script automatically terminates processes running that exact executable path, including PyInstaller's parent/child processes; copies in other directories are left running. Unsaved work in the terminated instance may be lost. Dark/light themes can be switched in Settings and are saved automatically. User settings and image templates are stored in `%LOCALAPPDATA%\ClickerPro`, so they survive rebuilding or moving the executable. OpenCV, NumPy, Pillow and MSS are bundled for image recognition.

The `.clickerprofile` export is a ZIP bundle containing `profile.json` and copied image assets, so recognition templates remain usable when the profile is moved to another computer. Plain `.json` exports remain supported for legacy configurations.

If dependencies are already installed, use `python build.py --skip-deps`. After stopping the matching application, the builder checks whether the existing EXE can be replaced, stops on dependency or packaging failures, and prints `Build complete` only after successful packaging.

`PermissionError: [WinError 5]` while removing `dist\ClickerPro.exe` means Windows denied replacement of the old file. Close Clicker Pro and check Task Manager for remaining `ClickerPro.exe` processes, then retry. If no process remains, check the file's read-only attribute, directory permissions, and security software's protection history. This error alone does not indicate a Python version problem. Old versions of `build.bat` printed `Build complete` even when packaging failed; run `git pull` to update the script. Existing user settings in `%LOCALAPPDATA%\ClickerPro` do not need to be deleted.
