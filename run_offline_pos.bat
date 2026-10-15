@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 offline_pos.py
    if not errorlevel 1 exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
    python offline_pos.py
    if not errorlevel 1 exit /b 0
)
for %%V in (Python315 Python314 Python313 Python312 Python311 Python310) do (
    if exist "%LocalAppData%\Programs\Python\%%V\python.exe" (
        "%LocalAppData%\Programs\Python\%%V\python.exe" offline_pos.py
        exit /b
    )
)
echo Python 3.10 or newer was not found. Install Python with Tkinter enabled.
pause
