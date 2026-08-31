"""把两件只有管理员才能验的事一次验完,省一次 UAC。

一、计划任务注册。verify_task_command.py 只验了「那条命令跑得通」,没验注册
    本身 —— 而 schtasks /Create 成功 ≠ 任务被正确创建。所以注册完必须回头去
    问系统:任务在不在、是不是最高权限、下次什么时候跑。只看 /Create 的返回码
    又是一个「只会通过的检查」。

二、USN 日志里的路径和字节数。这是唯一必须有管理员权限才能走的一段:
    读 $Extend\\$UsnJrnl 需要卷句柄。verify_usn_paths.py 把除此之外的每一步都
    验过了(39/40 条路径精确命中,0 条错),缺的就是实盘这一下。

只读为主,唯一的写操作是注册任务本身,可逆:python -m strata schedule off。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strata import privileges, schedule       # noqa: E402

DB = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Strata", "strata.db")


def hr(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def check_task() -> None:
    hr("一、注册每日任务,然后回头问系统它到底在不在")
    before = schedule.task_state()
    print(f"注册前:exists={before.exists} enabled={before.enabled} "
          f"next_run={before.next_run!r}")

    st = schedule.register(at="12:30")
    print(f"register() 返回:exists={st.exists} enabled={st.enabled} "
          f"next_run={st.next_run!r} last_result={st.last_result!r}")

    # 不信 register 的返回值,重新问一遍系统
    again = schedule.task_state()
    print(f"重新查询:exists={again.exists} next_run={again.next_run!r}")

    if not again.exists:
        print("× register() 没报错,但系统里查不到这个任务。")
        return

    # /RL HIGHEST 到底有没有生效?schtasks /Query /XML 里能看见
    import subprocess
    q = subprocess.run(
        ["schtasks", "/Query", "/TN", schedule.TASK_NAME, "/XML"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    xml = (q.stdout or "") + (q.stderr or "")
    highest = "HighestAvailable" in xml
    print(f"XML 里有 HighestAvailable:{highest}")
    if not highest:
        print("× /RL HIGHEST 没生效 —— 任务会以普通权限跑,读 MFT 会失败,"
              "然后每天静默失败一次。")
    else:
        print("√ 任务在、权限是最高、下次运行时间有值。三个证据。")
    print(f"\n不想留着就:python -m strata schedule off")


def usn_counts(drive: str) -> tuple[int, int, int]:
    """(总数, path 非空, bytes 非空)。库不在就当全 0。"""
    if not os.path.exists(DB):
        return (0, 0, 0)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        r = conn.execute(
            "SELECT COUNT(*), "
            "       SUM(CASE WHEN path IS NOT NULL AND path <> '' THEN 1 ELSE 0 END), "
            "       SUM(CASE WHEN bytes IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM usn_events WHERE drive = ?",
            (drive,),
        ).fetchone()
        return (r[0] or 0, r[1] or 0, r[2] or 0)
    finally:
        conn.close()


def check_usn(drive: str = "D:") -> None:
    """实盘跑一次带权限的扫描,看 path/bytes 这条链到底通不通。

    为什么必须实盘跑:库里现在 84,303 条 USN 事件 path 全空,但那批是
    08-30 21:13 那次 C: 扫描写的,而路径解析的代码 22:17~23:19 才写出来 ——
    也就是说这条链**一次都没在真盘上跑过**,库里既没有正面证据也没有反面证据。

    D: 那次 23:23 的扫描更说明问题:__main__.py 里 collect_usn 整段被
    `if privileges.is_admin()` 圈着,那次没提权,所以连 cursor 都没写。
    这也是为什么这个脚本必须提权跑。

    链是这样的:读日志 → 解析出 path → enrich_deleted_sizes 拿 path 去
    历史快照里反查大小 → bytes。前一环空了后一环必然空,所以两个数一起看。
    """
    hr(f"二、实盘扫一次 {drive},验 path/bytes 这条链")
    before = usn_counts(drive)
    print(f"扫描前 {drive}:共 {before[0]:,} 条,path 非空 {before[1]:,},"
          f"bytes 非空 {before[2]:,}")

    # 走和用户一样的入口(python -m strata scan),而不是在进程里拼调用 ——
    # 要验的就是真实那条路。不加 --quiet:变更日志那几行诊断正是要看的东西。
    import subprocess
    cmd = [sys.executable, "-m", "strata", "scan", "--drives", drive]
    print(f"\n跑:{' '.join(cmd)}\n")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "PYTHONPATH": os.path.join(
            os.path.dirname(__file__), "..", "src")},
    )
    print((proc.stdout or "").strip()[:2500])
    if (proc.stderr or "").strip():
        print("--- stderr ---")
        print(proc.stderr.strip()[:1500])
    print(f"\n退出码 {proc.returncode}")

    after = usn_counts(drive)
    print(f"\n扫描后 {drive}:共 {after[0]:,} 条,path 非空 {after[1]:,},"
          f"bytes 非空 {after[2]:,}")

    if after[0] == before[0]:
        print("× 一条新事件都没入库。要么日志读不到,要么这段被跳过了 —— "
              "看上面「变更日志」那几行的原因。")
        return
    new = after[0] - before[0]
    print(f"新增 {new:,} 条")
    if after[1] > before[1]:
        print(f"√ path 填上了(+{after[1] - before[1]:,})—— 路径解析在真盘上通了。")
    else:
        print("× 新事件的 path 全是空的。路径解析在真盘上没成 —— "
              "单元测试里 39/40 命中,实盘 0,差在环境而不是算法。")
    if after[2] > before[2]:
        print(f"√ bytes 填上了(+{after[2] - before[2]:,})—— 反查历史快照也通了。")
    else:
        print("· bytes 没增加。可能是这批新事件里没有「删除文件」,"
              "或者历史快照里对不上同路径 —— 后者是设计上就留空,不是错。")

    # 抽几条看看长什么样,光看计数容易自欺
    if os.path.exists(DB):
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT kind, path, bytes FROM usn_events "
                " WHERE drive = ? AND path IS NOT NULL AND path <> '' "
                " ORDER BY id DESC LIMIT 6", (drive,)).fetchall()
            if rows:
                print("\n最近几条带路径的:")
                for k, p, b in rows:
                    print(f"  [{k}] {p}  bytes={b}")
        finally:
            conn.close()


def main() -> None:
    print(f"管理员:{privileges.is_admin()}")
    if not privileges.is_admin():
        print("× 不是管理员,这个脚本要提权跑。用 tools\\run_elevated.bat 启动。")
        return

    for fn in (check_task, check_usn):
        try:
            fn()
        except Exception:
            print(f"\n× {fn.__name__} 抛异常:")
            traceback.print_exc()

    print("\n完。")


if __name__ == "__main__":
    main()
