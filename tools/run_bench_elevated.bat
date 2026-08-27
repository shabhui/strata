@echo off
rem Run the MFT benchmark elevated. Read-only: touches no database, writes nothing to disk.
rem Result goes to tools\bench_result.txt
rem ASCII only on purpose -- cmd.exe mangles non-ASCII under codepage 936.

setlocal
set "OUT=%~dp0bench_result.txt"

if "%~1"=="--elevated" goto run

echo Requesting administrator rights. Click Yes on the UAC prompt.
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--elevated' -Verb RunAs -Wait"
echo.
if exist "%OUT%" (echo Done. Result: %OUT%) else (echo Cancelled or failed - no result file.)
exit /b 0

:run
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0.."
python tools\bench_mft.py C: D: > "%OUT%" 2>&1
exit /b 0
