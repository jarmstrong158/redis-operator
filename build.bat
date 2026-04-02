@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Redis Operator ^— Build Windows Installer
echo ============================================
echo.

:: Use venv Python/pip/pyinstaller if the dev venv exists
if exist "venv\Scripts\python.exe" (
    set PYTHON=venv\Scripts\python.exe
    set PIP=venv\Scripts\pip.exe
    set PYINSTALLER=venv\Scripts\pyinstaller.exe
) else (
    set PYTHON=python
    set PIP=pip
    set PYINSTALLER=pyinstaller
)

:: Verify Python is available
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ and re-run.
    pause & exit /b 1
)

:: Install / upgrade PyInstaller
echo [1/5] Checking PyInstaller...
%PIP% show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo       Installing PyInstaller...
    %PIP% install pyinstaller
    if errorlevel 1 ( echo ERROR: Failed to install PyInstaller. & pause & exit /b 1 )
) else (
    echo       PyInstaller OK.
)

:: Download bundled Redis
echo.
echo [2/5] Downloading Redis server (bundled, no install needed by end users)...
%PYTHON% download_redis.py
if errorlevel 1 ( echo ERROR: Redis download failed. & pause & exit /b 1 )

:: Generate icon
echo.
echo [3/5] Generating icon...
%PYTHON% build_icon.py
if errorlevel 1 ( echo ERROR: Icon generation failed. & pause & exit /b 1 )

:: PyInstaller build — skip if exe already exists (pass --rebuild to force)
echo.
if exist "dist\Redis Operator\Redis Operator.exe" (
    echo %* | find /i "--rebuild" >nul
    if errorlevel 1 (
        echo [4/5] Executable already built — skipping. Pass --rebuild to force.
        goto :inno
    )
)
echo [4/5] Building executable (this takes a minute)...
%PYINSTALLER% redis_operator.spec --clean --noconfirm
if errorlevel 1 ( echo ERROR: PyInstaller build failed. & pause & exit /b 1 )
echo       Executable built: dist\Redis Operator\Redis Operator.exe
:inno

:: Find Inno Setup — try PATH first, then registry (works on any drive)
echo.
echo [5/5] Building installer...
set "ISCC="

for /f "usebackq delims=" %%i in (`where ISCC.exe 2^>nul`) do (
    if not defined ISCC set "ISCC=%%i"
)

if not defined ISCC (
    set "ISCC_DIR="
    for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v "InstallLocation" 2^>nul') do set "ISCC_DIR=%%b"
    if not defined ISCC_DIR for /f "tokens=2*" %%a in ('reg query "HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v "InstallLocation" 2^>nul') do set "ISCC_DIR=%%b"
    if not defined ISCC_DIR for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 5_is1" /v "InstallLocation" 2^>nul') do set "ISCC_DIR=%%b"
    if defined ISCC_DIR if exist "!ISCC_DIR!ISCC.exe" set "ISCC=!ISCC_DIR!ISCC.exe"
)

if not defined ISCC (
    echo.
    echo ============================================
    echo   Inno Setup not found.
    echo   Download (free) from:
    echo   https://jrsoftware.org/isdl.php
    echo   Install it, then re-run build.bat.
    echo ============================================
    echo.
    echo   The executable is already built at:
    echo   dist\Redis Operator\Redis Operator.exe
    echo   You can run it directly without installing.
    pause
    exit /b 0
)

"%ISCC%" installer.iss
if errorlevel 1 ( echo ERROR: Inno Setup build failed. & pause & exit /b 1 )

echo.
echo ============================================
echo   Done!
echo   Installer: Output\Redis_Operator_Setup.exe
echo ============================================
echo.
pause
