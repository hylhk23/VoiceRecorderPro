@echo off
rem Command line for Voice Recorder Pro, e.g.
rem   vrp status          vrp record --duration 0:10:00          vrp stop
rem   vrp extract "C:\rec\Oct02_1.mp3" --from 1:00 --to 2:30
setlocal
set "HERE=%~dp0"
if exist "%HERE%python\python.exe" (
    "%HERE%python\python.exe" -m vrp %*
    exit /b %errorlevel%
)
if exist "%HERE%.venv\Scripts\python.exe" (
    "%HERE%.venv\Scripts\python.exe" -m vrp %*
    exit /b %errorlevel%
)
echo Run run.bat once first to set up Voice Recorder Pro.
exit /b 1
