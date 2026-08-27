@echo off
rem Strata 启动器。双击就行。
rem
rem 读 MFT 和 USN 日志需要管理员权限,所以这里先自我提权:
rem 用 net session 判断当前是否已经是管理员,不是就通过 PowerShell 重新拉起自己。
setlocal

cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto :run

echo 需要管理员权限才能读 MFT 和变更日志,正在请求提权……
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" 2>nul
if errorlevel 1 (
  echo.
  echo 提权失败或被取消。
  echo 不提权也能跑,但只能用目录遍历:更慢,而且看不到删除记录。
  echo 要继续就按任意键,想退出直接关窗口。
  pause >nul
  goto :run
)
exit /b 0

:run
rem 找 Python。优先 py 启动器,其次 PATH 里的 python
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo 找不到 Python。装一个 3.10 以上的版本,勾上「Add to PATH」就行。
  echo 下载:https://www.python.org/downloads/
  pause
  exit /b 1
)

set "PYTHONPATH=%~dp0src"
%PY% -m strata serve
if errorlevel 1 (
  echo.
  echo 启动失败。上面的报错信息也写进了日志:
  echo   %LOCALAPPDATA%\Strata\strata.log
  pause
)

endlocal
