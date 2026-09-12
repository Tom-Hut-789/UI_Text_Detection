@echo off
REM ============================================================
REM  UI Text Detection - one-click launcher (Windows)
REM
REM  IMPORTANT: keep this file PURE ASCII.
REM  Non-ASCII characters + `chcp` make cmd.exe resume parsing at
REM  a wrong byte offset, which silently mangles the script (the
REM  window flashes and closes without ever running python).
REM  All Chinese output belongs in start.py, not here.
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   UI Text Detection - one-click launcher
echo ============================================================
echo.

REM ---- locate a usable Python (3.10+) ----
set "PYEXE="
python -c "import sys;sys.exit(sys.hexversion<0x30A00F0)" >nul 2>nul
if not errorlevel 1 set "PYEXE=python"

if not defined PYEXE (
    py -3 -c "import sys;sys.exit(sys.hexversion<0x30A00F0)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=py -3"
)

if not defined PYEXE (
    echo [ERROR] No usable Python 3.10+ interpreter found.
    echo.
    echo   Tried:  python   - not found, or version is below 3.10
    echo           py -3    - not found, or version is below 3.10
    echo.
    echo   Install Python 3.10+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    goto :done
)

echo Interpreter:
%PYEXE% -c "import sys;print('  Python',sys.version.split()[0]);print('  ',sys.executable)"
echo.

%PYEXE% start.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [ERROR] Launcher exited with code %EXITCODE% - see messages above.
    echo         Full log: run "python start.py" in a terminal to see it live.
)

:done
echo.
echo Press any key to close this window . . .
pause >nul
endlocal
