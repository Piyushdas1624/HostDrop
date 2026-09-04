@echo off
title HostDrop — Cross-Device File Transfer Hub
chcp 65001 >nul
color 0b

echo.
echo  ====================================================================
echo     HostDrop  —  2-Way Cross-Device File Transfer Hub
echo  ====================================================================
echo.

:: Check Python 3.8+
set "PYTHON_CMD="
where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_CMD=py -3"
    ) else (
        where python3 >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_CMD=python3"
        )
    )
)

if "%PYTHON_CMD%"=="" (
    color 0c
    echo  [ERROR] Python not found!
    echo  Install Python 3.8+ from https://python.org
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: Validate Python version >= 3.8
%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 (
    color 0c
    echo  [ERROR] Python 3.8+ is required to run HostDrop!
    echo  Current Python version is too old. Please upgrade from https://python.org
    echo.
    pause
    exit /b 1
)

echo  Found Python:
%PYTHON_CMD% --version
echo.

:: Install optional dependencies quietly
%PYTHON_CMD% -m pip install qrcode[pil] psutil --quiet --exists-action i >nul 2>&1

:: Determine default receive folder (D:\HostDrop if D:\ drive exists, else Downloads\HostDrop)
set "DEFAULT_DIR=D:\HostDrop"
if not exist "D:\" set "DEFAULT_DIR=%USERPROFILE%\Downloads\HostDrop"

if not "%~1"=="" goto got_param
echo  Where do you want to save INCOMING files on this PC (Inbox)?
echo  (Press ENTER to use %DEFAULT_DIR%, or type a custom path)
echo.
set /p RECV_DIR="  > Inbox folder path: "
if "%RECV_DIR%"=="" set "RECV_DIR=%DEFAULT_DIR%"
set RECV_DIR=%RECV_DIR:"=%
if not exist "%RECV_DIR%" mkdir "%RECV_DIR%" 2>nul
goto start_server

:got_param
set "FIRST_PARAM=%~1"
:: If argument starts with hyphen or slash, it is a flag
if "%FIRST_PARAM:~0,1%"=="-" goto flag_start
if "%FIRST_PARAM:~0,1%"=="/" goto flag_start

:: Otherwise it is a directory path
set "RECV_DIR=%~1"
set RECV_DIR=%RECV_DIR:"=%
if not exist "%RECV_DIR%" mkdir "%RECV_DIR%" 2>nul
goto start_server

:flag_start
if /i "%FIRST_PARAM%"=="--help" goto run_help
if /i "%FIRST_PARAM%"=="-h" goto run_help
if /i "%FIRST_PARAM%"=="/?" goto run_help
goto start_server

:run_help
%PYTHON_CMD% "%~dp0hostdrop.py" %*
exit /b %ERRORLEVEL%

:start_server
echo.
echo  ====================================================================
echo     Starting HostDrop Hub on http://127.0.0.1:8080 ...
echo     Opening dashboard in your default browser...
echo  ====================================================================
echo.

:: Automatically open default browser in background
start "" http://127.0.0.1:8080

:: Start HostDrop server in foreground to maintain active terminal session
if not "%~1"=="" (
    %PYTHON_CMD% "%~dp0hostdrop.py" %*
) else (
    %PYTHON_CMD% "%~dp0hostdrop.py" "%RECV_DIR%"
)

echo.
echo  HostDrop has stopped.
pause
