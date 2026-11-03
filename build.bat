@echo off
rem Build Voice Recorder Pro as Windows programs that antivirus engines accept.
rem   1. PyInstaller with a bootloader COMPILED ON THIS PC. The prebuilt bootloader that ships with
rem      PyInstaller is shared with countless other programs, including malware, and causes most
rem      VirusTotal false alarms. One-folder layout, no UPX packing, version info and icon.
rem   2. Optional Authenticode signing (packaging\sign.bat) of both .exe files and the installer.
rem   3. A zip, the installer (when Inno Setup 6 is installed) and SHA-256 checksums in Output\.
rem Needs: full Python 3.12/3.13 64-bit and Visual Studio Build Tools ("Desktop development with C++").
setlocal EnableExtensions
cd /d "%~dp0"
set "PYINSTALLER_VERSION=6.22.3"
set "APPVER=1.0.2"

if not exist ".venv-build\Scripts\python.exe" (
    py -3.13 -m venv .venv-build 2>nul || py -3.12 -m venv .venv-build 2>nul || python -m venv .venv-build
)
if not exist ".venv-build\Scripts\python.exe" (
    echo A full Python 3.12 or 3.13 install is needed to build - see README section 2.
    goto fail
)
set "PY=.venv-build\Scripts\python.exe"
"%PY%" -m pip install --upgrade pip || goto fail
"%PY%" -m pip install -r requirements-dev.txt || goto fail

rem ---- 1. PyInstaller with a self-compiled bootloader ----------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "MSVC="
if exist "%VSWHERE%" (
    for /f "usebackq delims=" %%I in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "MSVC=%%I"
)
if defined MSVC goto compile_bootloader
echo.
echo Visual Studio Build Tools with C++ were not found, so the PyInstaller bootloader cannot be
echo compiled here. Programs built with the standard prebuilt bootloader get more antivirus false alarms.
echo Install the Build Tools once, then run build.bat again:
echo   winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
echo Or let GitHub build it for you - see README section 2.
choice /c YN /n /m "Continue with the prebuilt bootloader anyway? [Y/N] "
if errorlevel 2 goto fail
"%PY%" -m pip install pyinstaller==%PYINSTALLER_VERSION% || goto fail
goto pyinstaller_ready

:compile_bootloader
echo Compiling the PyInstaller bootloader with Visual C++ from %MSVC% ...
"%PY%" -m pip uninstall -y pyinstaller >nul 2>&1
"%PY%" -m pip cache remove pyinstaller >nul 2>&1
set "PYINSTALLER_COMPILE_BOOTLOADER=1"
"%PY%" -m pip install --verbose --no-binary=pyinstaller pyinstaller==%PYINSTALLER_VERSION% > build-bootloader.log 2>&1
if errorlevel 1 (
    echo Compiling the bootloader failed - see build-bootloader.log
    goto fail
)
set "PYINSTALLER_COMPILE_BOOTLOADER="
findstr /c:"Waf: Leaving directory" build-bootloader.log >nul && echo Bootloader compiled from source.

:pyinstaller_ready
echo Running tests...
"%PY%" -m pytest -q tests
if errorlevel 1 (
    choice /c YN /n /m "Some tests failed. Build anyway? [Y/N] "
    if errorlevel 2 goto fail
)
"%PY%" packaging\make_icon.py || goto fail
"%PY%" -m PyInstaller --noconfirm --clean packaging\vrp.spec || goto fail
"%PY%" packaging\collect_licenses.py dist\VoiceRecorderPro || goto fail

rem ---- 2. optional code signing ------------------------------------------------------------------
set "SIGN="
if defined VRP_SIGN_THUMBPRINT set "SIGN=1"
if defined VRP_SIGN_DLIB set "SIGN=1"
if defined SIGN (
    call packaging\sign.bat "dist\VoiceRecorderPro\VoiceRecorderPro.exe" "dist\VoiceRecorderPro\vrp.exe" || goto fail
) else (
    echo Not signing: no certificate configured. See README section 2, step 3.
)

rem ---- 3. zip, installer, checksums --------------------------------------------------------------
if not exist Output mkdir Output
powershell -NoProfile -Command "Compress-Archive -Path dist\VoiceRecorderPro -DestinationPath Output\VoiceRecorderPro-%APPVER%-win64.zip -Force" || goto fail
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" goto no_inno
if defined SIGN (
    "%ISCC%" /DSIGN "/Svrpsign=cmd.exe /c $q$q%~dp0packaging\sign.bat$q $f$q" packaging\installer.iss || goto fail
) else (
    "%ISCC%" packaging\installer.iss || goto fail
)
goto checksums
:no_inno
echo Inno Setup 6 not found - skipping the installer. Get it from https://jrsoftware.org/isinfo.php

:checksums
powershell -NoProfile -Command "Get-ChildItem dist\VoiceRecorderPro\*.exe, Output\*.exe, Output\*.zip -ErrorAction SilentlyContinue | Get-FileHash -Algorithm SHA256 | ForEach-Object { '{0}  {1}' -f $_.Hash.ToLower(), (Split-Path $_.Path -Leaf) } | Set-Content -Encoding ascii Output\SHA256SUMS.txt"
echo.
echo Done. Results in Output\ :
dir /b Output
echo.
type Output\SHA256SUMS.txt
echo.
echo Check them at https://www.virustotal.com before sharing - the SHA-256 shown there must match.
exit /b 0

:fail
echo BUILD FAILED
exit /b 1
