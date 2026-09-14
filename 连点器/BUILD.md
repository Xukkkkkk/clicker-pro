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

The executable is written to `dist\ClickerPro.exe` and runs on Windows 10/11. User settings and image templates are stored in `%LOCALAPPDATA%\ClickerPro`, so they survive rebuilding or moving the executable. OpenCV, NumPy, Pillow and MSS are bundled for image recognition.
