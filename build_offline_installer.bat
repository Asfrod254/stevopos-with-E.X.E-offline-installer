@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON=python"
    if errorlevel 1 set "PYTHON=py -3"
)

%PYTHON% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo PyInstaller is required to build the offline installer.
    echo Install it with: %PYTHON% -m pip install pyinstaller
    pause
    exit /b 1
)

%PYTHON% -m unittest -v test_pos_db
if errorlevel 1 (
    echo Tests failed. Installer build cancelled.
    pause
    exit /b 1
)

if not exist "build" mkdir "build"

%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed --name StevoPOS --distpath "build\app-dist" --workpath "build\app-work" --specpath "build" offline_pos.py
if errorlevel 1 goto build_failed

if not exist "build\app-dist\StevoPOS.exe" goto build_failed

%PYTHON% -m PyInstaller --noconfirm --clean --onefile --windowed --name StevoPOS-Offline-Setup --add-data "app-dist\StevoPOS.exe;payload" --distpath "build\setup-dist" --workpath "build\setup-work" --specpath "build" offline_installer.py
if errorlevel 1 goto build_failed

if not exist "build\setup-dist\StevoPOS-Offline-Setup.exe" goto build_failed
copy /y "build\setup-dist\StevoPOS-Offline-Setup.exe" "StevoPOS-Offline-Setup.exe" >nul
if errorlevel 1 goto build_failed

echo.
echo Offline installer created:
echo %~dp0StevoPOS-Offline-Setup.exe
echo.
exit /b 0

:build_failed
echo.
echo Executable build failed. Review the output above.
pause
exit /b 1
