@echo off
REM ===================================================================
REM  CR RTIS Analysis Tool - Windows EXE Build Script
REM ===================================================================
REM  This script automates the entire build process
REM  Just double-click to build!
REM ===================================================================

echo.
echo ========================================
echo  CR RTIS Analysis Tool Builder
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    echo Please install Python 3.10 or later from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation
    pause
    exit /b 1
)

echo [OK] Python found
echo.

REM Check if virtual environment exists
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
    echo.
)

REM Activate virtual environment
echo Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment
    pause
    exit /b 1
)
echo [OK] Virtual environment activated
echo.

REM Install dependencies
echo Installing dependencies...
echo This may take a few minutes on first run...
echo.
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

REM Install PyInstaller if not present
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller --quiet
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller
        pause
        exit /b 1
    )
    echo [OK] PyInstaller installed
    echo.
)

REM Clean previous build
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo [OK] Cleaned previous build
echo.

REM Build EXE
echo Building Windows EXE...
echo This takes 3-5 minutes, please wait...
echo.
pyinstaller RailDataApp.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    echo Check the error messages above
    pause
    exit /b 1
)

echo.
echo ========================================
echo  Build Complete!
echo ========================================
echo.
echo Output location: dist\RailDataApp.exe
echo.
echo Next steps:
echo   1. Test: cd dist ^&^& RailDataApp.exe
echo   2. Package for distribution (see BUILD_INSTRUCTIONS.md)
echo.
echo Size info:
dir dist\RailDataApp.exe | find "RailDataApp.exe"
echo.

pause
