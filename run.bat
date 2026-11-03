@echo off
rem Start Voice Recorder Pro.
rem  1. Portable package: uses the Python included in the "python" folder (nothing to install).
rem  2. Source package: creates a private environment in .venv with Python 3.12/3.13 (internet needed once).
setlocal
cd /d "%~dp0"

if exist "python\pythonw.exe" (
    start "" "python\pythonw.exe" -m vrp %*
    exit /b 0
)
if exist ".venv\Scripts\pythonw.exe" goto start_venv

echo Setting up Voice Recorder Pro for the first time...
set "PYCMD="
for %%V in (3.13 3.12) do (
    if not defined PYCMD (
        py -%%V -c "import venv, ensurepip" >nul 2>&1 && set "PYCMD=py -%%V"
    )
)
if not defined PYCMD (
    python -c "import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 11) and sys.maxsize > 2**32 else 1)" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD goto no_python

echo Using: %PYCMD%
%PYCMD% -m venv .venv || goto setup_failed
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt || ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto setup_failed

:start_venv
start "" ".venv\Scripts\pythonw.exe" -m vrp %*
exit /b 0

:no_python
echo.
echo No suitable Python was found. The Python on this PC's PATH is either a
echo "portable/embeddable" Python (it has no venv module) or older than 3.11.
echo.
echo Easiest: download the PORTABLE package of Voice Recorder Pro, which already
echo contains its own Python - unzip it and double-click run.bat.
echo.
echo Or install Python 3.13 (64-bit) from https://www.python.org/downloads/windows/
echo ("Install Now" installs for your user only and does not need admin rights),
echo then run this file again.
pause
exit /b 1

:setup_failed
echo.
echo Setting up the libraries failed - see the messages above. Delete the .venv
echo folder before trying again.
pause
exit /b 1
