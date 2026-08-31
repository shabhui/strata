@echo off
rem Elevated one-shot scan: writes a real snapshot AND collects USN events.
rem
rem Why this exists separately from run_elevated.bat: that one runs
rem `python tools\<file>.py`, and this needs `python -m strata scan` --
rem a module, not a script in tools\.
rem
rem This WRITES to the real database (%LOCALAPPDATA%\Strata\strata.db).
rem It only ever adds: one snapshot row plus USN event rows. Nothing is
rem deleted or overwritten.
rem
rem Usage: run_scan_elevated.bat [C:]
rem Result goes to tools\scan_elevated_result.txt
rem ASCII only on purpose -- cmd.exe mangles non-ASCII under codepage 936.
rem python -u matters: without it stdout is block-buffered into the redirect
rem and nothing shows up until the process exits.

setlocal
if "%~1"=="--elevated" goto run

echo Requesting administrator rights. Click Yes on the UAC prompt.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated %~1' -Verb RunAs -ErrorAction Stop"
if errorlevel 1 (
  echo Launch FAILED -- see the PowerShell error above. No result file will appear.
  exit /b 1
)
echo Launched. Result will appear in tools\scan_elevated_result.txt when done.
exit /b 0

:run
set "DRIVE=%~2"
if "%DRIVE%"=="" set "DRIVE=C:"
set "OUT=%~dp0scan_elevated_result.txt"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
cd /d "%~dp0.."
set "PYTHONPATH=%~dp0..\src"
python -u -m strata scan --drives %DRIVE% > "%OUT%" 2>&1
exit /b 0
