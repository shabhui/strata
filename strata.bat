@echo off
rem Strata launcher. Double-click to run.
rem
rem THIS FILE IS DELIBERATELY PURE ASCII -- comments included. It is the only
rem file in the repo not commented in Chinese, and that is on purpose:
rem
rem   cmd parses a .bat using the console's code page, and tracks its position
rem   in the file as a BYTE OFFSET. Chinese Windows defaults to code page 936,
rem   so UTF-8 Chinese in here renders as mojibake -- on exactly the lines that
rem   matter ("elevation cancelled", "Python not found"). The obvious fix, a
rem   `chcp 65001` up top, is WORSE: switching the code page mid-file
rem   desynchronizes that byte offset and cmd starts reading lines at the wrong
rem   position. Reproduced: it ran 'bat' and 'un' as commands (the tails of
rem   `strata.bat` and `:run`), then opened a bare Python REPL because the
rem   `-m strata serve` arguments had been sliced off the line -- the launcher
rem   was completely dead. Saving the file as GBK would fix Chinese Windows and
rem   break every other locale.
rem
rem   So: no non-ASCII bytes here at all, and no chcp. The messages below are
rem   English. Everything from Python onwards is Chinese as usual, since Python
rem   handles its own encoding. Guarded by tests/test_launcher_bat.py.
rem
rem Reading the MFT and the USN journal needs administrator rights, so this
rem script elevates itself first: `net session` says whether we are already
rem admin; if not, relaunch through PowerShell's Start-Process -Verb RunAs.
setlocal

cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto :run

echo Administrator rights are needed to read the MFT and the change journal.
echo Requesting elevation...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" 2>nul
if errorlevel 1 (
  echo.
  echo Elevation failed or was cancelled.
  echo Running without admin still works, but only by walking directories:
  echo slower, and deleted files stay invisible.
  echo Press any key to continue, or close this window to quit.
  pause >nul
  goto :run
)
exit /b 0

:run
rem Find Python. Prefer the py launcher, fall back to python on PATH.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Python not found. Install 3.10 or newer and tick "Add to PATH".
  echo Download: https://www.python.org/downloads/
  pause
  exit /b 1
)

set "PYTHONPATH=%~dp0src"
%PY% -m strata serve
if errorlevel 1 (
  echo.
  echo Failed to start. The error above was also written to the log:
  echo   %LOCALAPPDATA%\Strata\strata.log
  pause
)

endlocal
