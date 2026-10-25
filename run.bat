@echo off
rem ====================================================================
rem  pdf_outline_maker - launcher for Windows.  Just double-click it.
rem
rem  NOTE: keep this file pure ASCII.  cmd.exe reads a .bat line by line
rem  using the CURRENT code page, so Ukrainian text placed here would be
rem  mangled after "chcp 65001".  Everything the user normally reads is
rem  printed by Python (in Ukrainian); only rare setup/error messages
rem  live here.
rem ====================================================================
setlocal EnableExtensions DisableDelayedExpansion
set "RC=0"

rem ---- did Explorer open a console just for us? -----------------------
rem  A double-click runs:  cmd.exe /c ""C:\...\run.bat" "  - the command
rem  line contains our own name.  Typing run.bat in an open console does
rem  not, so there we must NOT pause.
set "PAUSE_AT_END="
setlocal EnableDelayedExpansion
set "CL=!cmdcmdline!"
if not "!CL:%~nx0=!"=="!CL!" (endlocal & set "PAUSE_AT_END=1") else (endlocal)
if defined CI set "PAUSE_AT_END="
if defined NO_PAUSE set "PAUSE_AT_END="

rem ---- console and Python both speak UTF-8 ---------------------------
set "OLDCP="
for /f "tokens=2 delims=:" %%a in ('chcp') do for /f "tokens=1 delims=. " %%b in ("%%a") do set "OLDCP=%%b"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem ---- work from the folder this file lives in ------------------------
rem  Remember where the user actually stood first: we are about to leave that
rem  folder, and relative arguments like `run.bat notes\a.pdf` must still be
rem  resolved against it.
set "PDF_OUTLINE_MAKER_CWD=%CD%"
pushd "%~dp0" || (echo [ERROR] cannot open folder "%~dp0" & set "RC=1" & goto :finish)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

rem ---- create the virtual environment on the very first run -----------
if not exist "%VENV_PY%" goto :make_venv

rem  A .venv copied from another PC (or left behind by an uninstalled Python)
rem  still has python.exe but cannot run. Check before blaming the network.
"%VENV_PY%" -c "import sys" >nul 2>nul
if not errorlevel 1 goto :have_venv
echo [ERROR] The .venv folder in this project is broken - it points at a Python
echo         that is no longer installed. Delete the .venv folder and run this
echo         file again to recreate it.
set "RC=1"
goto :finish

:make_venv
echo [setup] Creating a local Python environment (one time only)...
call :find_python
if not defined PYCMD goto :no_python
%PYCMD% -m venv "%~dp0.venv"
if not exist "%VENV_PY%" (
    echo [ERROR] Could not create the virtual environment in "%~dp0.venv".
    set "RC=1"
    goto :finish
)

:have_venv
rem ---- install dependencies only when something is missing ------------
"%VENV_PY%" -c "import pymupdf" >nul 2>nul
if errorlevel 1 (
    echo [setup] Installing dependencies from requirements.txt...
    "%VENV_PY%" -m pip install --disable-pip-version-check -q -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check the internet connection and try again.
        set "RC=1"
        goto :finish
    )
)

rem ---- run the program, passing through every argument ----------------
"%VENV_PY%" -m pdf_outline_maker %*
set "RC=%ERRORLEVEL%"
goto :finish

rem --------------------------------------------------------------------
:find_python
rem  Look for a system Python.  "py -3" first: it never hits the Microsoft
rem  Store alias that silently opens a browser window.
set "PYCMD="
py -3 -c "import sys" >nul 2>nul && set "PYCMD=py -3" && goto :eof
where python.exe >nul 2>nul || goto :eof
python -c "import sys" >nul 2>nul && set "PYCMD=python"
goto :eof

:no_python
echo.
echo [ERROR] Python 3 was not found on this computer.
echo         Install Python 3.10 or newer from https://www.python.org/downloads/
echo         and tick "Add python.exe to PATH" during installation.
set "RC=9009"
goto :finish

:finish
popd 2>nul
if not "%RC%"=="0" echo.& echo [ERROR] Finished with exit code %RC%.
if defined PAUSE_AT_END pause
if defined OLDCP chcp %OLDCP% >nul
endlocal & exit /b %RC%
