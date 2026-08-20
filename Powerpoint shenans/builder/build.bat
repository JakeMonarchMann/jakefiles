@echo off
REM Double-click to build the EXE into builder\dist\.
cd /d "%~dp0"

REM Prefer `python` on PATH -- same interpreter run_gui.bat uses, so its
REM installed deps (pywin32, etc.) get bundled. Fall back to the `py`
REM launcher only if `python` isn't on PATH.
set PYCMD=python
%PYCMD% --version >nul 2>nul
if errorlevel 1 set PYCMD=py
%PYCMD% --version >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/.
    pause
    exit /b 1
)

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
