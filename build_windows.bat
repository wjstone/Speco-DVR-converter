@echo off
REM Build script for Speco DRV Extractor Windows executable
REM Usage: build_windows.bat

setlocal enabledelayedexpansion

echo ================================================
echo Speco DRV Extractor - Windows EXE Builder
echo ================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python 3 not found.
    echo Install from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo OK Python: %PYTHON_VERSION%
echo.

REM Create virtual environment if needed
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    echo OK Virtual environment created
) else (
    echo OK Virtual environment exists
)

REM Activate virtual environment
call venv\Scripts\activate.bat
echo OK Virtual environment activated
echo.

REM Install PyInstaller
echo Installing PyInstaller...
python -m pip install -q --upgrade pip
python -m pip install -q PyInstaller
echo OK PyInstaller ready
echo.

REM Check for required files
echo Checking for required files...
if not exist "drv_gui.py" (
    echo Error: drv_gui.py not found
    exit /b 1
)
if not exist "drv_extract_v11.py" (
    echo Error: drv_extract_v11.py not found
    exit /b 1
)
if not exist "drv_extractor_windows.spec" (
    echo Error: drv_extractor_windows.spec not found
    exit /b 1
)
echo OK All required files present
echo.

REM Clean build artifacts
echo Cleaning old builds...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q __pycache__ 2>nul
echo OK Cleaned
echo.

REM Build the executable
echo Building Windows executable...
python -m PyInstaller drv_extractor_windows.spec
echo.

REM Check result
if exist "dist\Speco DRV Extractor.exe" (
    echo ================================================
    echo OK Build successful!
    echo ================================================
    echo.
    echo Your executable is ready:
    echo   dist\Speco DRV Extractor.exe
    echo.
    echo To run it:
    echo   1. Double-click the .exe file, or
    echo   2. Run from Command Prompt
    echo.
    echo To install in Program Files:
    echo   Copy dist\Speco DRV Extractor.exe to C:\Program Files\
    echo.
    echo IMPORTANT: ffmpeg must be installed on the target machine!
    echo   Download from https://ffmpeg.org/download.html
    echo   Or via Chocolatey: choco install ffmpeg
    echo.
) else (
    echo Error: Build failed
    exit /b 1
)

pause
