@echo off
REM Double-click to build the EXE into builder\dist\.
cd /d "%~dp0"

REM Prefer the project's own .venv (has the app's deps, e.g. pywin32)
REM so the build doesn't silently omit them; fall back to `py`/`python`.
if exist "..\.venv\Scripts\python.exe" (
    set PYCMD="..\.venv\Scripts\python.exe"
    goto :run
)
set PYCMD=py
%PYCMD% --version >nul 2>nul
if errorlevel 1 set PYCMD=python
%PYCMD% --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/.
    pause
    exit /b 1
)

:run
%PYCMD% build_exe.py
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo Build succeeded. EXE^(s^) in: %~dp0dist\
) else (
    echo Build FAILED with exit code %RC%.
)
echo.
pause
exit /b %RC%
