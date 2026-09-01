@echo off
rem Elevated runner for the packaged dist\Strata.exe -- verifies the build.
rem Usage: run_exe_elevated.bat <subcommand> [args...]
rem   e.g. run_exe_elevated.bat doctor
rem Result goes to dist\Strata_<subcommand>_result.txt
rem
rem ASCII only on purpose -- cmd.exe mangles non-ASCII under codepage 936.
rem This mirrors run_elevated.bat, and for the same reasons:
rem   - chcp 65001 + PYTHONIOENCODING/PYTHONUTF8: without them the exe's Chinese
rem     output dies on the way into the redirect and the result file stays empty.
rem     Tried plain `cmd /c ... > file` twice first; both gave exit=1 and no file.
rem   - one quoted ArgumentList string, not a list: empty elements from unpassed
rem     args make Start-Process reject the whole launch.
rem   - `cd /d` before running: an elevated cmd starts in System32, so a relative
rem     output path lands there (or fails) instead of in the project.

setlocal
if "%~1"=="--elevated" goto run

if "%~1"=="" echo Usage: run_exe_elevated.bat ^<subcommand^> [args...] & exit /b 1
if not exist "%~dp0..\dist\Strata.exe" (
  echo dist\Strata.exe not found -- run `python tools\build_exe.py` first.
  exit /b 1
)
echo Requesting administrator rights. Click Yes on the UAC prompt.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated %~1 %~2 %~3' -Verb RunAs -ErrorAction Stop"
if errorlevel 1 (
  echo Launch FAILED -- see the PowerShell error above. No result file will appear.
  exit /b 1
)
echo Launched. Result will appear in dist\ when it finishes.
exit /b 0

:run
set "SUB=%~2"
set "OUT=%~dp0..\dist\Strata_%SUB%_result.txt"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0.."
dist\Strata.exe %SUB% %~3 %~4 > "%OUT%" 2>&1
echo exit=%errorlevel% >> "%OUT%"
exit /b 0
