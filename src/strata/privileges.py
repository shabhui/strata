"""管理员权限检测与自我提权。

直读 MFT 和读 USN 日志都需要管理员。用户选的是「一律要求管理员权限」,
所以启动器会先提权再拉起服务;这个模块负责判断当前身份,
以及在没提权时重新以管理员启动自己。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass

# ShellExecuteW 返回值 <= 32 表示失败
SE_ERR_ACCESSDENIED = 5
ERROR_CANCELLED = 1223


@dataclass(slots=True)
class PrivilegeState:
    is_admin: bool
    can_elevate: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "is_admin": self.is_admin,
            "can_elevate": self.can_elevate,
            "detail": self.detail,
        }


def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    """当前进程是否有管理员权限。"""
    if not is_windows():
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def privilege_state() -> PrivilegeState:
    if not is_windows():
        return PrivilegeState(
            is_admin=False,
            can_elevate=False,
            detail="非 Windows 系统,MFT 直读不可用。",
        )
    if is_admin():
        return PrivilegeState(
            is_admin=True, can_elevate=True, detail="已具备管理员权限,可以直读 MFT 与 USN 日志。"
        )
    return PrivilegeState(
        is_admin=False,
        can_elevate=True,
        detail="当前不是管理员。可以继续,但只能用目录遍历,硬链接会被重复计算。",
    )


def relaunch_as_admin(args: list[str] | None = None, *, wait: bool = False) -> bool:
    """以管理员身份重新启动自己。

    返回 True 表示已经拉起了提权进程,调用方应当立刻退出;
    返回 False 表示用户在 UAC 弹窗上点了取消,或者提权不可用。
    """
    if not is_windows():
        return False
    if is_admin():
        return False

    argv = args if args is not None else sys.argv[1:]

    # 打包成 exe 之后 sys.executable 就是我们自己,再把 argv[0] 塞进参数里的话
    # 会变成 `strata.exe strata.exe serve` —— argparse 会把那个路径
    # 当成子命令,直接报错退出。源码模式下 sys.executable 是 python.exe,
    # 脚本路径必须留着。
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(list(argv))
    else:
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0]), *argv])

    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
    except Exception:
        return False

    if result <= 32:
        return False
    return True


def ensure_admin(*, args: list[str] | None = None) -> bool:
    """已经是管理员就返回 True;否则尝试提权并让调用方退出。

    典型用法::

        if not privileges.ensure_admin():
            sys.exit(0)      # 提权进程已接手
    """
    if is_admin():
        return True
    if relaunch_as_admin(args):
        return False
    # 提权失败(用户取消),让调用方自己决定要不要降级运行
    raise PermissionError("提权被取消。没有管理员权限只能用目录遍历方式扫描。")
