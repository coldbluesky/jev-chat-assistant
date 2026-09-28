@echo off
setlocal
cd /d "%~dp0"

REM Double-click me to run the app from source. ASCII only: Chinese Windows cmd is GBK.
REM run.py does the real work: switches to the project venv, checks deps, forwards the exit code.

if not exist "run.py" (
    echo run.py not found - keep run.bat next to run.py and main.py.
    pause
    exit /b 2
)

set "PYEXE=.venv\Scripts\python.exe"
set "PYARGS="
if exist "%PYEXE%" goto :run

REM No project venv: fall back to whatever Python is on PATH (run.py will list missing deps).
set "PYEXE=py"
set "PYARGS=-3"
where py >nul 2>nul
if errorlevel 1 (
    set "PYEXE=python"
    set "PYARGS="
)

:run
"%PYEXE%" %PYARGS% "run.py" %*
set "CODE=%errorlevel%"
if "%CODE%"=="9009" (
    echo.
    echo Python not found: neither "py" nor "python" is on PATH.
    echo Install Python 3.10-3.12 first, then in this folder:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -r requirements.txt
    pause
)
exit /b %CODE%
