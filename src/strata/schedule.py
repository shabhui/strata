"""Windows 计划任务 —— 每天自动拍一次快照。

用 schtasks.exe 而不是装服务:任务能在界面上看到、能手动删,
出问题时用户自己就能查明白。

任务以最高权限运行(直读 MFT 需要),所以注册时要求当前进程已经是管理员。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, privileges

TASK_NAME = "Strata 每日快照"
DEFAULT_TIME = "12:30"


@dataclass(slots=True)
class TaskState:
    exists: bool
    enabled: bool
    schedule: str | None = None
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "exists": self.exists,
            "enabled": self.enabled,
            "schedule": self.schedule,
            "next_run": self.next_run,
            "last_run": self.last_run,
            "last_result": self.last_result,
            "detail": self.detail,
            "task_name": TASK_NAME,
        }


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """跑 schtasks。用 mbcs 解码 —— 中文 Windows 上它输出的是本地代码页。"""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="mbcs" if sys.platform == "win32" else "utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _scan_command() -> tuple[str, str]:
    """返回 (可执行文件, 参数)。

    两种运行形态要分开处理:

    打包成 exe:sys.executable 就是我们自己,直接 `strata.exe scan ...`。
    不能去找 pythonw.exe —— 用户机器上可能根本没有 Python。

    源码模式:走 python.exe -m strata,并且优先用 pythonw.exe,
    免得每天弹一个黑窗口。这时 strata 包不一定在 sys.path 上
    (仓库里是 src/ 布局,靠 PYTHONPATH 撐着),而计划任务不继承
    当前进程的环境变量,所以要把 src 目录显式插进 sys.path。
    """
    drives = " ".join(config.DEFAULT_DRIVES)

    if getattr(sys, "frozen", False):
        return sys.executable, f"scan --drives {drives} --quiet"

    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    runner = quiet if quiet.exists() else exe

    src = Path(__file__).resolve().parent.parent          # .../src
    # -c 里用 repr 出来的字符串字面量,反斜杠会被正确转义
    code = (
        f"import sys; sys.path.insert(0, {str(src)!r}); "
        "from strata.__main__ import main; sys.exit(main())"
    )
    args = subprocess.list2cmdline(
        ["-c", code, "scan", "--drives", *config.DEFAULT_DRIVES, "--quiet"]
    )
    return str(runner), args


def task_state() -> TaskState:
    if sys.platform != "win32":
        return TaskState(exists=False, enabled=False, detail="非 Windows,不支持计划任务。")

    proc = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    if proc.returncode != 0:
        return TaskState(exists=False, enabled=False, detail="尚未注册计划任务。")

    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    # 中英文系统的字段名不一样,两个都试
    def pick(*names: str) -> str | None:
        for name in names:
            if name in fields and fields[name]:
                return fields[name]
        return None

    status = pick("Scheduled Task State", "计划任务状态", "状态") or ""
    return TaskState(
        exists=True,
        enabled=status.lower() not in ("disabled", "已禁用"),
        schedule=pick("Schedule Type", "计划类型", "Start Time", "开始时间"),
        next_run=pick("Next Run Time", "下次运行时间"),
        last_run=pick("Last Run Time", "上次运行时间"),
        last_result=pick("Last Result", "上次结果"),
    )


def register(*, at: str = DEFAULT_TIME) -> TaskState:
    """注册每日任务。已存在则覆盖。"""
    if sys.platform != "win32":
        raise RuntimeError("只支持 Windows。")
    if not privileges.is_admin():
        raise PermissionError("注册以最高权限运行的计划任务需要管理员身份。")

    runner, args = _scan_command()
    # schtasks 的 /TR 整体是一个字符串,里面的引号要留给它自己解析
    command = f'"{runner}" {args}'

    proc = _run(
        [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", command,
            "/SC", "DAILY",
            "/ST", at,
            "/RL", "HIGHEST",
            "/F",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"注册失败(返回码 {proc.returncode}):{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return task_state()


def unregister() -> TaskState:
    if sys.platform != "win32":
        raise RuntimeError("只支持 Windows。")
    proc = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if proc.returncode != 0 and "找不到" not in proc.stderr and "cannot find" not in proc.stderr.lower():
        raise RuntimeError(
            f"删除失败(返回码 {proc.returncode}):{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return task_state()


def set_enabled(enabled: bool, *, at: str = DEFAULT_TIME) -> dict:
    """界面上的开关。开就注册,关就删掉。"""
    state = register(at=at) if enabled else unregister()
    return state.as_dict()


def run_now() -> TaskState:
    """立刻跑一次已注册的任务,用来验证它真的能工作。"""
    proc = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    if proc.returncode != 0:
        raise RuntimeError(
            f"启动失败:{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return task_state()
