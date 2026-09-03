@echo off
title System Analyst App Server
echo Starting System Analyst App Server...

:: Always resolve paths from this batch file, not from the caller's current directory.
set "PROJECT_DIR=%~dp0"
cd /d "%PROJECT_DIR%"
set "PYTHON_EXE=%PROJECT_DIR%venv\Scripts\python.exe"

:: Check if the project virtual environment exists
if not exist "%PYTHON_EXE%" (
    echo ERROR: Virtual environment not found!
    echo Please run: py -m venv venv
    echo Then run: venv\Scripts\activate
    echo Then run: pip install -r requirements.txt
    pause
    exit /b 1
)

:: Do not compete with the tray controller or another healthy local server.
"%PYTHON_EXE%" -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:5000/static/login.html', timeout=2); sys.exit(0 if r.status == 200 else 1)" >nul 2>nul
if not errorlevel 1 (
    echo Server is already running on port 5000.
    echo Use the system tray to stop or restart it.
    pause
    exit /b 0
)

:: Check that Uvicorn is installed in the project environment
"%PYTHON_EXE%" -c "import uvicorn" >nul 2>nul
if errorlevel 1 (
    echo ERROR: uvicorn not found!
    echo Please run: pip install -r requirements.txt
    pause
    exit /b 1
)

:: Start the server
echo Server starting at http://localhost:5000
echo Press Ctrl+C to stop the server.
echo.

:: Keep this command identical to tray_control.py. --reload creates a supervisor/worker
:: tree whose changing PIDs can conflict with tray Start/Stop/Restart management.
"%PYTHON_EXE%" -m uvicorn main:app --host 0.0.0.0 --port 5000

:: This line only runs if uvicorn crashes or stops
pause
