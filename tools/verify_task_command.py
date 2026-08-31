"""计划任务那条命令,真的跑得起来吗?

为什么单独验这个:注册成功 ≠ 任务能跑。schtasks /Create 只检查语法,
命令本身错了它照样注册。之后每天静默失败一次,而 doctor 只会说「已注册」,
下次运行时间也照常往后推 —— 又是一个「只会通过的检查」。

必须按任务计划程序真正的方式转交命令:它**不走 cmd**,是 CreateProcess
直接拿整条字符串,按 Windows 的 argv 规则拆。所以不能用 bash 或 cmd 试
(它们的引号和反斜杠规则都不一样,会把 'D:\\\\AI项目\\\\...' 里的双反斜杠
吃成单个,于是 \\t 变成制表符、路径直接坏掉 —— 我第一次就是这么误报的)。

subprocess.run 传**字符串**(不是列表)时,Windows 上就是直接送给
CreateProcess,和任务计划程序一致。这是唯一忠实的测法。

只扫 D:(比 C: 快),--quiet,看退出码和有没有真写进库。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strata import schedule                      # noqa: E402


def snapshot_count(drive: str) -> int:
    path = os.path.join(os.environ["LOCALAPPDATA"], "Strata", "strata.db")
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE drive = ?", (drive,)
        ).fetchone()[0]
    finally:
        conn.close()


def main() -> None:
    runner, args = schedule._scan_command()
    # 注册时用的是 --drives C: D:,这里换成只扫 D: 省时间。
    # 其余原样 —— 尤其是 -c 那一大坨,它才是容易坏的部分。
    args = args.replace("--drives C: D:", "--drives D:")
    command = f'"{runner}" {args}'
    print("要跑的命令(原样,和注册进任务里的一致):")
    print(f"  {command}\n")

    # pythonw.exe 没有控制台,拿不到输出。验的时候换成 python.exe,
    # 这样出错能看见;注册进任务里的仍然是 pythonw(免得每天弹黑窗口)。
    console = command.replace("pythonw.exe", "python.exe")
    if console != command:
        print("(验证时换成 python.exe 以便看到输出;任务里仍用 pythonw)\n")

    before = snapshot_count("D:")
    t0 = time.perf_counter()
    # 关键:传字符串而不是列表 —— 走 CreateProcess,和任务计划程序同一条路。
    proc = subprocess.run(console, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    dt = time.perf_counter() - t0
    after = snapshot_count("D:")

    print(f"退出码 {proc.returncode},耗时 {dt:.1f}s")
    if proc.stdout.strip():
        print("--- stdout ---")
        print(proc.stdout.strip()[:1500])
    if proc.stderr.strip():
        print("--- stderr ---")
        print(proc.stderr.strip()[:1500])

    print(f"\nD: 快照数 {before} → {after}")
    if proc.returncode == 0 and after > before:
        print("√ 命令跑得通,而且真写进库了 —— 两个证据,不是只看退出码。")
    elif proc.returncode == 0:
        print("× 退出码 0 但快照没增加。这正是「静默失败」的样子:"
              "光看退出码会以为好着。")
    else:
        print("× 命令跑不通。注册进任务里的话每天失败一次,而 doctor 只会说「已注册」。")


if __name__ == "__main__":
    main()
