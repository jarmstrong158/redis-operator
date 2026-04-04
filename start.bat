@echo off
setlocal

echo.
echo  Conductor
echo  ==============
echo.

REM --- Check Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.
    echo  Please install Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM --- Create venv on first run ---
if not exist ".venv\Scripts\activate.bat" (
    echo  First run: setting up virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM --- Activate venv ---
call .venv\Scripts\activate.bat

REM --- Install / update dependencies ---
echo  Checking dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo  ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo  Starting Conductor...
echo.

REM --- Launch ---
python launch.py

pause
