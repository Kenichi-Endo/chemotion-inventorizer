@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "PROJECT_DIR=%~dp0"
set "BOOTSTRAP=%PROJECT_DIR%src\chemotion_inventorizer\bootstrap.py"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%BOOTSTRAP%" (
    echo Chemotion Inventorizer source was not found. Keep this launcher in the project folder.
    goto :launch_failed
)

rem Keep the console available during automatic first-time setup. The bootstrap
rem starts the installed environment's pythonw.exe once setup has completed.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%BOOTSTRAP%" %*
    if errorlevel 1 goto :launch_failed
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%BOOTSTRAP%" %*
    if errorlevel 1 goto :launch_failed
    exit /b 0
)

echo Python 3.10 or newer with Tk support was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH" and keep "tcl/tk and IDLE" selected.

:launch_failed
echo.
echo Chemotion Inventorizer could not start. Review the message above.
echo Setup and launch errors are recorded in the current user's
echo LocalAppData\Chemotion Inventorizer\Logs\launcher.log.
pause
exit /b 1
