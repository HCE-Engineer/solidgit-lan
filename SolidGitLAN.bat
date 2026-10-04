@echo off
rem Launcher: sets up the virtual environment on first run, then opens the window.
setlocal
cd /d "%~dp0"

rem The marker is written only after a *successful* install. Checking for python.exe alone
rem was not enough: an install cut short (dropped Wi-Fi) left .venv behind, every later
rem launch skipped setup, and the console-less window then failed without a word.
if not exist ".venv\kurulum-tamam" (
    echo First run - preparing the environment, this takes a few minutes...
    if not exist ".venv\Scripts\python.exe" (
        python -m venv .venv || goto :failed
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -e ".[net,ui]" || goto :failed
    echo ok> ".venv\kurulum-tamam"
)

start "" ".venv\Scripts\pythonw.exe" -m solidgit_lan.ui %*
goto :eof

:failed
echo.
echo Setup failed. Check that Python 3.12 or newer is installed and on PATH,
echo and that this computer is connected to the internet for the first run.
pause
