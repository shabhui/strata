@echo off
rem Generic elevated runner for the read-only MFT tools.
rem Usage: run_elevated.bat <tool.py> [args...]
rem   e.g. run_elevated.bat prof_mft_gc.py C:
rem Result goes to tools\<tool>_result.txt
rem ASCII only on purpose -- cmd.exe mangles non-ASCII under codepage 936.
rem python -u matters: without it stdout is block-buffered into the redirect and
rem nothing shows up until the process exits.

setlocal
if "%~1"=="--elevated" goto run

if "%~1"=="" echo Usage: run_elevated.bat ^<tool.py^> [args...] & exit /b 1
echo Requesting administrator rights. Click Yes on the UAC prompt.
rem One quoted string, not a list. A list built from %~1..%~3 puts empty
rem elements in it when fewer args are passed, and Start-Process rejects those
rem with ParameterArgumentValidationError -- the whole launch fails and the
rem only clue is a PowerShell stack trace under the cheerful "Launched." below.
rem cmd re-splits the string on spaces in :run, so %~1..%~4 still work there.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated %~1 %~2 %~3' -Verb RunAs -ErrorAction Stop"
if errorlevel 1 (
  echo Launch FAILED -- see the PowerShell error above. No result file will appear.
  exit /b 1
)
echo Launched. Result will appear in tools\ when it finishes.
exit /b 0

:run
set "TOOL=%~2"
for %%F in ("%TOOL%") do set "STEM=%%~nF"
set "OUT=%~dp0%STEM%_result.txt"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0.."
python -u tools\%TOOL% %~3 %~4 > "%OUT%" 2>&1
exit /b 0
