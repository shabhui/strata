"""在资源管理器里定位一个目录或文件。

这个模块要拿浏览器传来的路径去启动进程,所以它是整个工具里最需要小心的
一段代码。三条底线:

1. 绝不执行目标。os.startfile 会用默认程序「打开」文件 —— 对着 .exe 或
   .bat 就是直接运行。这里一律走 explorer.exe:目录就打开目录,文件用
   /select 只是在父目录里把它选中,不会运行它。
2. 路径必须落在盘符里面。前端传来的是相对盘符的路径,拼完解析一遍,
   跑出盘符范围就拒绝。
3. 不进 shell。参数以列表形式交给 subprocess,不做字符串拼接。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class RevealError(Exception):
    """带 HTTP 状态码的错误。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_drive(raw: str) -> str:
    """把 'c' / 'C:' / 'c:\\' 规整成 'C:'。"""
    text = (raw or "").strip()
    if not text:
        raise RevealError("缺少盘符")
    drive = text.rstrip("\\").rstrip(":").upper() + ":"
    if len(drive) != 2 or not drive[0].isalpha():
        raise RevealError(f"盘符不合法:{raw}")
    return drive


def resolve_target(drive: str, rel: str) -> Path:
    """把盘符和相对路径拼成绝对路径,并确认它没跑出盘符。

    不检查存不存在 —— 那是调用方的事,好让「已经删掉了」和「路径不合法」
    是两种不同的错误。
    """
    drive = normalize_drive(drive)
    rel = (rel or "").strip().strip('"')

    # 绝对路径、UNC、shell: 之类的写法一律不收。这里只接受盘内相对路径,
    # 收窄输入形状比事后逐条排查各种特殊写法可靠。
    if ":" in rel:
        raise RevealError(f"路径里不该有冒号:{rel}")
    if rel.startswith("\\\\") or rel.startswith("//"):
        raise RevealError("不支持网络路径")
    if "\0" in rel:
        raise RevealError("路径含非法字符")

    root = Path(drive + "\\")
    target = (root / rel) if rel else root
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise RevealError(f"路径无法解析:{exc}") from exc

    # 解析之后必须还在这个盘里。用 is_relative_to 而不是比字符串前缀 ——
    # 前缀比较挡不住 C:\ 和 C:\..\D: 之类绕出去的写法。
    if not resolved.is_relative_to(root.resolve()):
        raise RevealError(f"路径跑出了 {drive}:{rel}", status=403)
    return resolved


def _default_runner(argv: list[str]) -> int:
    """启动 explorer。

    不看返回码:explorer.exe 成功时也经常返回 1,拿它判断成败只会误报。
    """
    completed = subprocess.run(argv, check=False)      # noqa: S603
    return completed.returncode


def reveal(drive: str, rel: str, *, runner=None) -> dict:
    """在资源管理器里定位。返回给前端看的结果。

    runner 是为了测试:注入之后可以断言到底拼出了什么命令行,
    而不用真的弹出一堆窗口。
    """
    if sys.platform != "win32":
        raise RevealError("只支持 Windows", status=501)

    target = resolve_target(drive, rel)
    if not target.exists():
        raise RevealError(
            f"这个路径已经不在了:{target}(可能在上次扫描之后被删了)",
            status=404,
        )

    run = runner or _default_runner
    if target.is_dir():
        argv = ["explorer.exe", str(target)]
    else:
        # /select 只是在父目录里选中它。逗号后面不留空格,这是 explorer 的
        # 写法要求;整体作为一个参数交出去,不经过 shell。
        argv = ["explorer.exe", f"/select,{target}"]

    try:
        run(argv)
    except OSError as exc:
        raise RevealError(f"启动资源管理器失败:{exc}", status=500) from exc

    return {"ok": True, "path": str(target), "kind": "dir" if target.is_dir() else "file"}
