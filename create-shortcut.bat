@echo off
rem Creates "Voice Recorder Pro" shortcuts on your Desktop and in the Start menu.
rem The shortcut starts the app with the signed pythonw.exe in the "python" folder (portable package),
rem or from .venv (source package), with the app's own icon and no console window.
rem   create-shortcut.bat           create or update the shortcuts
rem   create-shortcut.bat /remove   delete them
rem Run it again if you move or rename this folder.
rem Works with Desktop folders in any language (for example a OneDrive Desktop named in Chinese).
setlocal
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "PYW=%APPDIR%\python\pythonw.exe"
if not exist "%PYW%" set "PYW=%APPDIR%\.venv\Scripts\pythonw.exe"
set "MODE=create"
if /i "%~1"=="/remove" set "MODE=remove"
if "%MODE%"=="create" if not exist "%PYW%" (
    echo Python was not found in the "python" or ".venv" folder here.
    echo Unzip the portable package into this folder, or run run.bat once first.
    pause
    exit /b 1
)
set "SELF=%~f0"

rem The PowerShell part of this file is below the marker line at the end.
powershell -NoProfile -Command "$vrpText=[IO.File]::ReadAllText($env:SELF); $vrpMark='#__'+'POWERSHELL__'; $global:rc=1; & ([ScriptBlock]::Create($vrpText.Substring($vrpText.LastIndexOf($vrpMark) + $vrpMark.Length))); exit $global:rc"
if errorlevel 1 (
    echo.
    echo Some shortcuts could not be created - see the message above.
    pause
    exit /b 1
)
echo.
echo Done.
pause
exit /b 0

#__POWERSHELL__
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}   # show non-English folder names

$app   = $env:APPDIR
$pyw   = $env:PYW
$name  = 'Voice Recorder Pro'
$desc  = 'Voice-activated sound recorder'
$appId = 'VRP.VoiceRecorderPro'      # same taskbar ID the app sets, so pinning to the taskbar works
$icon  = Join-Path $app 'packaging\vrp.ico'
if (-not (Test-Path -LiteralPath $icon)) { $icon = '' }

# Windows' own Unicode shortcut interface (IShellLinkW). The older WScript.Shell object
# cannot save into folders whose names use characters outside the PC's "non-Unicode
# programs" language, e.g. a OneDrive Desktop folder named in Chinese.
$code = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace VrpShortcut
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    public class CShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    public interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cch, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out ushort pwHotkey);
        void SetHotkey(ushort wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cch, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey
    {
        public Guid fmtid;
        public uint pid;
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant
    {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pointerValue;
        [FieldOffset(16)] public long padding;
    }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    public interface IPropertyStore
    {
        void GetCount(out uint cProps);
        void GetAt(uint iProp, out PropertyKey pkey);
        void GetValue(ref PropertyKey key, out PropVariant pv);
        void SetValue(ref PropertyKey key, ref PropVariant pv);
        void Commit();
    }

    public static class Lnk
    {
        public static void Save(string lnk, string target, string args, string workDir,
                                string icon, string description, string appId)
        {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            try
            {
                link.SetPath(target);
                link.SetArguments(args);
                link.SetWorkingDirectory(workDir);
                link.SetDescription(description);
                if (!String.IsNullOrEmpty(icon)) link.SetIconLocation(icon, 0);
                if (!String.IsNullOrEmpty(appId))
                {
                    try
                    {
                        IPropertyStore store = (IPropertyStore)link;
                        PropertyKey key = new PropertyKey();
                        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");   // System.AppUserModel.ID
                        key.pid = 5;
                        PropVariant pv = new PropVariant();
                        pv.vt = 31;                                                      // VT_LPWSTR
                        pv.pointerValue = Marshal.StringToCoTaskMemUni(appId);
                        try { store.SetValue(ref key, ref pv); store.Commit(); }
                        finally { Marshal.FreeCoTaskMem(pv.pointerValue); }
                    }
                    catch (Exception) { }   // the taskbar ID is a nicety; the shortcut works without it
                }
                ((IPersistFile)link).Save(lnk, true);
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }
    }
}
'@

$native = $true
try { Add-Type -TypeDefinition $code -Language CSharp }
catch { $native = $false; Write-Host ('(Using the simpler shortcut method: ' + $_.Exception.Message + ')') }

function Save-Shortcut([string]$lnk) {
    if ($native) {
        [VrpShortcut.Lnk]::Save($lnk, $pyw, '-m vrp', $app, $icon, $desc, $appId)
        return
    }
    # Fallback: build the shortcut under a plain English temp name, then move it into place.
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('vrp-shortcut-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($tmp)
    $s.TargetPath = $pyw
    $s.Arguments = '-m vrp'
    $s.WorkingDirectory = $app
    if ($icon) { $s.IconLocation = $icon + ',0' }
    $s.Description = $desc
    $s.Save()
    Move-Item -LiteralPath $tmp -Destination $lnk -Force
}

$places = @(
    @{ Label = 'Desktop';    Dir = [Environment]::GetFolderPath('Desktop') },
    @{ Label = 'Start menu'; Dir = [Environment]::GetFolderPath('Programs') }
)
$failed = 0
foreach ($place in $places) {
    if (-not $place.Dir) { continue }
    $lnk = Join-Path $place.Dir ($name + '.lnk')
    try {
        if ($env:MODE -eq 'remove') {
            if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; Write-Host ('Removed   ' + $lnk) }
            else { Write-Host ('Not found ' + $lnk) }
            continue
        }
        if (-not (Test-Path -LiteralPath $place.Dir)) { New-Item -ItemType Directory -Path $place.Dir -Force | Out-Null }
        Save-Shortcut $lnk
        if (-not (Test-Path -LiteralPath $lnk)) { throw 'Windows reported success but the file is not there.' }
        Write-Host ('Created   ' + $lnk)
    }
    catch {
        $failed++
        Write-Host ('Could not create the ' + $place.Label + ' shortcut (' + $lnk + '): ' + $_.Exception.Message) -ForegroundColor Red
    }
}
$global:rc = [int]($failed -gt 0)
