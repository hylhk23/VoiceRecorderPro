@echo off
rem Authenticode-sign files:   packaging\sign.bat file1.exe [file2.exe ...]
rem Configure ONE of these before running build.bat:
rem  A) A code-signing certificate in your Windows certificate store (for example an OV certificate on
rem     a USB token):           set VRP_SIGN_THUMBPRINT=<SHA-1 thumbprint of the certificate>
rem  B) Azure Artifact Signing (formerly Trusted Signing; individuals in Canada and the USA can use it):
rem        set VRP_SIGN_DLIB=<path to Azure.CodeSigning.Dlib.dll from the Artifact Signing client tools>
rem        set VRP_SIGN_METADATA=<path to metadata.json with Endpoint, CodeSigningAccountName,
rem                               CertificateProfileName>
rem     and sign in first with "az login".
setlocal EnableExtensions
set "SIGNTOOL="
for /f "delims=" %%S in ('where signtool.exe 2^>nul') do if not defined SIGNTOOL set "SIGNTOOL=%%S"
if not defined SIGNTOOL (
    for /f "delims=" %%S in ('dir /b /s /o:n "%ProgramFiles(x86)%\Windows Kits\10\bin\signtool.exe" 2^>nul ^| find /i "\x64\signtool.exe"') do set "SIGNTOOL=%%S"
)
if not defined SIGNTOOL (
    echo signtool.exe was not found. Install the Windows SDK - only the "Signing Tools" feature is needed:
    echo   https://developer.microsoft.com/windows/downloads/windows-sdk/
    exit /b 1
)
if defined VRP_SIGN_DLIB goto azure
if defined VRP_SIGN_THUMBPRINT goto certstore
echo Nothing to sign with: set VRP_SIGN_THUMBPRINT, or VRP_SIGN_DLIB and VRP_SIGN_METADATA.
exit /b 1

:azure
"%SIGNTOOL%" sign /v /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 /dlib "%VRP_SIGN_DLIB%" /dmdf "%VRP_SIGN_METADATA%" %*
if errorlevel 1 exit /b 1
goto verify

:certstore
"%SIGNTOOL%" sign /v /sha1 %VRP_SIGN_THUMBPRINT% /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 %*
if errorlevel 1 exit /b 1

:verify
"%SIGNTOOL%" verify /pa %*
if errorlevel 1 (
    echo The signature check failed.
    exit /b 1
)
exit /b 0
