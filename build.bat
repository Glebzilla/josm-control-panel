@echo off
rem Build JOSM_Control_Panel.exe with PyInstaller.
rem Result: dist\JOSM_Control_Panel.exe
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH. Install Python 3.10+ and retry.
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is not installed. Run:
    echo     pip install -r requirements.txt
    pause
    exit /b 1
)

echo Building JOSM_Control_Panel.exe ...
python -m PyInstaller --noconfirm --onefile --noconsole ^
    --name JOSM_Control_Panel ^
    --icon "%~dp0josm_control_panel.ico" ^
    --distpath dist --workpath build --specpath build ^
    josm_control_panel.pyw

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Done: dist\JOSM_Control_Panel.exe
pause
