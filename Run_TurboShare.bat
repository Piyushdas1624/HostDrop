@echo off
title TurboShare — Cross-Device File Transfer Hub
chcp 65001 >nul
color 0b

echo.
echo  ====================================================================
echo     TurboShare  —  2-Way Cross-Device File Transfer Hub
echo  ====================================================================
echo.

:: Check Python
where python >nul 2>&1
if errorlevel 1 (
    color 0c
    echo  [ERROR] Python not found!
    echo  Install Python 3.8+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo  Found Python:
python --version
echo.

:: Install optional dependencies quietly
python -m pip install qrcode[pil] psutil --quiet --exists-action i >nul 2>&1

:: Ask for receive folder (Inbox)
echo  Where do you want to save INCOMING files on this PC (Inbox)?
echo  (Press ENTER to use D:\TurboShare, or type a custom path)
echo.
set /p RECV_DIR="  > Inbox folder path: "

if "%RECV_DIR%"=="" (
    set RECV_DIR=D:\TurboShare
)

echo.
echo  ====================================================================
echo     Starting TurboShare Hub on http://127.0.0.1:8080 ...
echo     Opening dashboard in your default browser...
echo  ====================================================================
echo.

:: Automatically open default browser in background
start "" http://127.0.0.1:8080

:: Start TurboShare server in foreground to maintain active terminal session
python "%~dp0turboshare.py" "%RECV_DIR%"

echo.
echo  TurboShare has stopped.
pause
