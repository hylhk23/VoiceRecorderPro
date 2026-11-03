# Voice Recorder Pro: developer notes

End-user download and installation: see the main [README](../README.md). User help: [`docs/HELP.html`](HELP.html), also opened with **F1** in the app.

## About the code

Voice Recorder Pro (VRP) is a voice-activated sound recorder for Windows 11 (64-bit), written in Python with Qt (PySide6). Version 1 covers voice-activated (Vox) recording with segments, microphone and computer-audio capture, global hot-keys, foot pedals and other external devices, sound-driver selection, direct-to-file editing, schedules and a command line.

---

## 1. Run it

**Portable package (nothing to install, no admin rights).** The `-portable-win64` zip contains its own Python 3.13 and all libraries in the `python` folder. Unzip it anywhere and double-click **`run.bat`**. Any other Python on the PC is ignored.

**Source code (this repository).** Without the `python` folder, `run.bat` needs a full Python 3.12 or 3.13 (64-bit) from <https://www.python.org/downloads/windows/> ("Install Now" installs for the current user, no admin rights). The first run creates a private environment in `.venv` and downloads the libraries (internet needed once). "Embeddable" or other portable Pythons cannot create that environment; use the portable package instead.

To get a Desktop and Start-menu shortcut, double-click **`create-shortcut.bat`** (run it again after moving the folder; `create-shortcut.bat /remove` deletes the shortcuts).

For the command line use `vrp.bat` (for example `vrp.bat status`); it uses the same Python as `run.bat`. Run the app with `--fake-audio` to try everything without a microphone.

If Windows shows "Windows protected your PC" or another security warning for `run.bat`, right-click the downloaded zip, choose **Properties**, tick **Unblock**, click OK, and unzip it again.

**Portable settings.** Put an empty file named `portable.flag` in the program folder (next to `run.bat`); settings and logs are then kept in a `data` folder there instead of your user profile.

## 2. Optional: build a stand-alone `VoiceRecorderPro.exe`

The published portable package does **not** use this route: it runs the official, signed `pythonw.exe`. This section is for building a single program folder with its own `.exe` using PyInstaller.

**Why programs built with PyInstaller can be flagged.** PyInstaller wraps the app in a small launcher (the *bootloader*). The prebuilt bootloader is shared by many programs, so some antivirus engines flag files built with it, and an unsigned file from an unknown publisher adds to that. The build scripts reduce both:

| Measure | Where | Effect |
| --- | --- | --- |
| Bootloader compiled from source on the build machine | `build.bat` | Avoids the shared prebuilt launcher that triggers most false alarms |
| One-folder build (no self-extracting single file), no UPX packing | `packaging/vrp.spec` | Avoids "packer/dropper" heuristics |
| Version information, icon, normal install location | `packaging/version_info.txt`, installer | Looks like an ordinary Windows program |
| Authenticode code signature with a timestamp | `packaging/sign.bat` | Shows a verified publisher name, and lets Microsoft Defender SmartScreen build reputation for the publisher (see below) |

**Microsoft Defender SmartScreen** is the protection built into Windows that checks the reputation of downloaded files before they run. A new or rarely downloaded program triggers its blue "Windows protected your PC" prompt. Signing alone does not remove that prompt; it lets reputation build up for the signing identity as more people download the signed files ([Microsoft Learn](https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation)).

### Build on your own PC

1. Install a full **Python 3.13** (64-bit) from python.org (the portable package's Python cannot build).
2. Install the C++ compiler once (several GB; needs admin rights):
   `winget install Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"`
3. Optional: install **Inno Setup 6** (<https://jrsoftware.org/isinfo.php>) for a setup program.
4. Double-click **`build.bat`**. It compiles the bootloader, runs the tests, builds, signs (if configured) and writes everything to `Output\` with SHA-256 checksums.

| Output | What it is |
| --- | --- |
| `dist\VoiceRecorderPro\VoiceRecorderPro.exe` | The program (copy the whole folder anywhere) |
| `dist\VoiceRecorderPro\vrp.exe` | Command-line companion (same folder) |
| `Output\VoiceRecorderPro-1.0.2-win64.zip` | The program folder, zipped for sharing |
| `Output\VoiceRecorderPro-Setup-1.0.2.exe` | Installer (when Inno Setup 6 is installed) |
| `Output\SHA256SUMS.txt` | SHA-256 checksums of the outputs |

The installer installs per user without admin rights (all-users is offered), adds Start-menu entries, can start the app at sign-in, and on uninstall keeps your recordings and asks before removing settings.

### Sign the files (optional)

`build.bat` signs automatically when one of these is configured (details in `packaging\sign.bat`):

* **Azure Artifact Signing** (Microsoft's cloud signing service, previously called Trusted Signing): install the Artifact Signing client tools and the Windows SDK signing tools, run `az login`, then set `VRP_SIGN_DLIB` and `VRP_SIGN_METADATA` before `build.bat`. At general availability it was offered to individuals in the USA and Canada after identity validation; check Microsoft's current terms and prices before signing up ([DevClass, Jan 2026](https://www.devclass.com/security/2026/01/14/code-signing-windows-apps-may-be-easier-and-more-secure-with-new-azure-artifact-service/4079554); [Azure pricing](https://azure.microsoft.com/en-ca/pricing/details/artifact-signing/)).
* **Code-signing certificate** from a certificate authority, installed in the Windows certificate store (usually on a USB token or cloud key): set `VRP_SIGN_THUMBPRINT` to the certificate's SHA-1 thumbprint before `build.bat`.

With signing configured, `build.bat` signs both programs, the installer and its uninstaller, adds an RFC 3161 timestamp (signatures stay valid after the certificate expires) and verifies the result.

### Check the build and report false alarms

1. Scan `VoiceRecorderPro.exe`, `vrp.exe` and the installer with your antivirus, or upload them to an online multi-engine scanner such as <https://www.virustotal.com>. The SHA-256 it shows must match `SHA256SUMS.txt`.
2. If an engine flags a file you built, report it to that vendor as a false positive. For Microsoft Defender: <https://www.microsoft.com/en-us/wdsi/filesubmission>.
3. Rebuild only when the code changes: every rebuild produces a new, never-seen file.

## 3. What version 1 does

| Area | Implemented |
| --- | --- |
| Main window | Format indicator, Vox slider/value/toggle, segmented level meters with peak hold, threshold line and clip light, Segment/Sample/Size fields, disk line, position slider with selection and segment ticks, five transport buttons, tray icon coloured by state, light/dark theme. Taskbar badge: a red dot on the taskbar button while recording, amber while Vox waits for sound or recording is paused (also on the window icon) |
| Recording sources | Microphone, computer audio, or both. Computer audio is what plays through the default speakers or headphones (calls, videos, music), captured by WASAPI loopback (PyAudioWPatch) and following the Windows default output device. Two meters, **Mic** and **Computer**, each with a tick box that can be changed at any time, even while recording. Both are mixed into one file; Vox reacts to either. If the microphone is unplugged, computer audio keeps recording |
| About box | Copyright Neo Time Consulting, a no-warranty notice, and a link to https://software.4u.hk |
| Menus | File, Edit, Control, Options, Help, with keyboard shortcuts |
| Recording | Callback → queue → writer thread; MP3 (CBR/VBR), WAV 8/16/24-bit, 32-bit float, µ-law, A-law, FLAC; flush every 1 s, crash journal and recovery; pause/resume; append; resampling fallback; channel mapping; input gain; 80 Hz high-pass; 3.9 GB WAV rollover |
| Vox | Threshold, attack, hang, pre-roll, minimum segment, segment or new-file mode, calibration, disable/hide |
| Files | Naming patterns with tokens and subfolders, never overwrites, rollover by size/duration/midnight/every N hours, segment time stamps (ID3 chapters, WAV cue points, FLAC chapter tags), optional `.segments.json`, recent files, deletes go to the Recycle Bin, long paths |
| Playback | Tap to jump / hold to scan, auto-backspace, speed 50–200 % with pitch kept (WSOLA), own volume, selection play and loop, segment navigation |
| Editing | Extract, delete, crop, split, join, export segments, insert boundary, save a copy in another format, one-level undo; WAV and MP3 cut without re-encoding; progress with Cancel |
| Hot-keys | System-wide (`RegisterHotKey`), hold detection for push-to-talk and scanning, conflict warnings, media keys, start/stop beeps, suspend from tray, window-only scope |
| External devices | Game controllers, serial-port control lines, USB HID foot pedals; mappings with trigger/invert/debounce, Learn, live test panel, profiles, auto-reconnect |
| Sound devices | Host API (MME, DirectSound, WASAPI, WDM-KS) and device selection, WASAPI exclusive mode with fallback, wait-for-device, lost-device reconnection, 3-second test |
| Schedules | Timed recording; schedules (once/daily/weekdays/weekly/monthly) with their own Vox, device, format and names; overlap check; late-start window; auto-stop on silence or clock time |
| Settings | Tabbed Options dialog, JSON settings with backup, invalid-file handling, per-tab defaults, export/import |
| Command line | `vrp record/pause/resume/stop/play/status/devices/extract/split/join/convert/schedule`, headless recording, single instance over a local named pipe, exit codes, run-after-file command |
| Errors | Low-disk warning/stop, fallback folder when writing fails, microphone privacy hint, no-signal and clipping warnings, rotating logs |

**Not in version 1:** Opus and IMA-ADPCM output, a local HTTP API, GPIO/MIDI inputs, automatic clean-up of old recordings, translations beyond English, automatic updates, and speech-to-text.

## 4. Tests

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

81 automated tests run without real audio hardware, using a synthetic microphone. They cover recording, stop and playback; Vox segments and click rejection; file naming; rollover with no lost samples (including a simulated midnight); recovering a file after the process is killed mid-recording; fast extraction from a one-hour WAV; byte-identical MP3 frame copies; cancelled edits leaving the original untouched; device unplug and reconnect; late and missed schedules; command-line control of a running copy; microphone + computer-audio mixing and changing sources while recording; plus settings, formats and offscreen GUI tests.

Not covered by automated tests (they need real devices on Windows 11): global hot-keys, USB foot pedals, serial lines, physically unplugging a microphone, real computer audio, and very long (8-hour+) recordings.

## 5. Project layout

```
vrp/
  app.py            start-up, single instance, wiring          cli.py / ipc.py / control.py / headless.py
  settings.py       schema, defaults, validation, persistence  paths.py, logsetup.py, winutil.py
  core/
    engine.py       eight-state machine every input talks to
    recorder.py     capture -> queue -> writer thread, Vox, rollover, journals, fallback
    vox.py          voice-operated switch                       tsm.py   time-stretch (WSOLA)
    player.py       decoder thread + output callback            editor.py  direct-to-file edits
    encoders.py     WAV/MP3/FLAC streaming writers              metadata.py, id3.py, wavfile.py, flacmeta.py, mp3frames.py
    audio_backend.py PortAudio (sounddevice) + fake device      scheduler.py, journal.py, naming.py, dsp.py, model.py
    loopback.py     computer audio (WASAPI loopback, PyAudioWPatch), follows the default output device
  inputs/           hotkeys.py (RegisterHotKey), keymap.py, external.py (joystick/serial/HID)
  ui/               main_window.py, widgets.py, options_dialog.py, dialogs.py, icons.py, taskbar.py
packaging/          vrp.spec (PyInstaller), installer.iss (Inno Setup), version info, icon, launcher, licence collector
tests/              pytest suite
docs/HELP.html      user help
```

## 6. Where things are stored

| What | Where |
| --- | --- |
| Recordings | `Documents\VRP Recordings` (change in Options > Recording) |
| Fallback recordings (when the main folder fails) | `%LOCALAPPDATA%\VoiceRecorderPro\Recordings (fallback)` |
| Settings | `%APPDATA%\VoiceRecorderPro\settings.json` (+ `.bak`) |
| Logs | `%LOCALAPPDATA%\VoiceRecorderPro\Logs\vrp.log` (5 MB × 10 files, kept 14 days) |
| Undo backups | hidden `.vrp-undo` folder next to an edited file (moved to the Recycle Bin after 24 h) |

With `portable.flag` (section 1), settings and logs are in the `data` folder instead.

## 7. Licensing notes

VRP's own code contains no GPL components: ID3, WAV and FLAC tag writing are implemented in-house. Bundled libraries are LGPL (Qt/PySide6, libsndfile, LAME via lameenc, soxr) or permissive (NumPy, sounddevice/PortAudio, PyAudioWPatch, pyserial, hidapi under its BSD option, cffi, pycparser, typing_extensions). The LGPL libraries are shipped as separate, replaceable files. See [`THIRD-PARTY-LICENSES.txt`](../THIRD-PARTY-LICENSES.txt).
