@echo off
title GM League Season 4 Server
cd /d "%~dp0"

echo ========================================================
echo   GM League Season 4 - College Windows VM Launcher
echo ========================================================

if not exist ".venv" (
    echo [.venv not found. Creating Python virtual environment...]
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Python is not installed or not added to PATH.
        echo Please install Python 3.11 or 3.12 and check "Add python.exe to PATH".
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    echo [Installing requirements...]
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)

if not exist ".env" (
    if exist ".env.college_vm_example" (
        echo [Copying .env.college_vm_example to .env...]
        copy .env.college_vm_example .env
    )
)

if not exist "instance" (
    mkdir instance
)

echo.
echo Starting GM League application server on http://localhost:5000 ...
python run_server.py
pause

