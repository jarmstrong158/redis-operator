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
echo [1/4] Checking PyInstaller...
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

:: PyInstaller build
echo.
echo [4/5] Building executable (this takes a minute)...
%PYINSTALLER% redis_operator.spec --clean --noconfirm
if errorlevel 1 ( echo ERROR: PyInstaller build failed. & pause & exit /b 1 )
echo       Executable built: dist\Redis Operator\Redis Operator.exe

:: Find Inno Setup — check PATH first, then common locations on all drives
echo.
echo [5/5] Building installer...
set ISCC=
where ISCC.exe >nul 2>&1
if not errorlevel 1 ( set ISCC=ISCC.exe )

if not defined ISCC (
    for %%d in (A B C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
        for %%v in (6 5) do (
            for %%f in (
                "%%d:\Program Files (x86)\Inno Setup %%v\ISCC.exe"
                "%%d:\Program Files\Inno Setup %%v\ISCC.exe"
                "%%d:\Inno Setup %%v\ISCC.exe"
            ) do (
                if exist %%f if not defined ISCC set ISCC=%%f
            )
        )
    )
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

%ISCC% installer.iss
if errorlevel 1 ( echo ERROR: Inno Setup build failed. & pause & exit /b 1 )

echo.
echo ============================================
echo   Done!
echo   Installer: Output\Redis_Operator_Setup.exe
echo ============================================
echo.
pause
