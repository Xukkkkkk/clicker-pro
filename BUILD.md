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

3. Build a standalone executable:

```powershell
python -m PyInstaller --clean --noconfirm ClickerPro.spec
```

Use `dist\ClickerPro.exe` as the single executable for all themes and features on Windows 10/11. Close it before rebuilding to update that same path. Dark/light themes can be switched in Settings and are saved automatically. User settings and image templates are stored in `%LOCALAPPDATA%\ClickerPro`, so they survive rebuilding or moving the executable. OpenCV, NumPy, Pillow and MSS are bundled for image recognition.

The `.clickerprofile` export is a ZIP bundle containing `profile.json` and copied image assets, so recognition templates remain usable when the profile is moved to another computer. Plain `.json` exports remain supported for legacy configurations.
