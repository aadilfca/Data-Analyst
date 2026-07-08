@echo off
title System Analyst App Server
echo Starting System Analyst App Server...

:: Check if virtual environment exists
if not exist venv\Scripts\activate (
    echo ERROR: Virtual environment not found!
    echo Please run: py -m venv venv
    echo Then run: venv\Scripts\activate
    echo Then run: pip install -r requirements.txt
    pause
    exit /b 1
)

:: Activate virtual environment
call venv\Scripts\activate

:: Check if uvicorn is installed
where uvicorn >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: uvicorn not found!
    echo Please run: pip install -r requirements.txt
    pause
    exit /b 1
)

:: Start the server
echo Server starting at http://localhost:5000
echo Press Ctrl+C to stop the server.
echo.
uvicorn main:app --reload --host 0.0.0.0 --port 5000

:: This line only runs if uvicorn crashes or stops
pause