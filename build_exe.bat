@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   GeminiWatch build script (Windows)
echo ============================================
echo.

echo [1/6] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo     python not found. Install Python 3.9+ and add it to PATH.
    goto :fail
)

echo [2/6] Checking source files...
if not exist "gemini_watch.py" goto :miss_src
if not exist "panel.html" goto :miss_src
if not exist "assets\app.ico" echo     warning: assets\app.ico not found

echo [3/6] Installing dependencies...
python -m pip install --quiet --disable-pip-version-check pyinstaller requests pillow pystray winotify
if errorlevel 1 (
    echo     pip install failed. Check network or pip index.
    goto :fail
)

echo [4/6] Stopping running instance...
taskkill /im GeminiWatch.exe /f >nul 2>&1
ping -n 3 127.0.0.1 >nul

echo [5/6] Building...
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name GeminiWatch ^
  --icon "assets\app.ico" ^
  --add-data "panel.html;." ^
  --collect-submodules pystray ^
  --collect-submodules winotify ^
  gemini_watch.py
if errorlevel 1 (
    echo     Build failed. See the output above.
    goto :fail
)

if not exist "dist\GeminiWatch.exe" (
    echo     dist\GeminiWatch.exe was not created.
    goto :fail
)

echo [6/6] Cleaning intermediates...
if exist "build" rmdir /s /q "build"
if exist "GeminiWatch.spec" del /q "GeminiWatch.spec"

echo.
echo ============================================
echo   Done: dist\GeminiWatch.exe
echo   Data: %%APPDATA%%\GeminiWatch
echo   Panel: http://127.0.0.1:47131
echo ============================================
if not defined CI pause
exit /b 0

:miss_src
echo     gemini_watch.py or panel.html is missing.
goto :fail

:fail
echo.
echo Build did not complete.
if not defined CI pause
exit /b 1
