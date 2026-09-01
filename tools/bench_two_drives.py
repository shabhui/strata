"""同一个进程里连扫两个盘,第二个要慢多少 —— 这是生产环境每次都在走的形状。

来路:__main__.py:62 是 `for raw in drives:`,两个盘在**同一个进程**里连着扫。
而 schedule.py:86 那个定时任务跑的正是 `scan --drives C: D: --quiet`。

之前量到同进程连跑两次 C: 是 45.52 秒然后 89.61 秒(见 config.py 里那段)。
如果那是堆的原因而不是缓存冷热,那 D: 每次定时扫描都在付这笔账 —— 而且
从来没在真实形状下量过:那次量的是「两次 C:」,不是「C: 然后 D:」。

需要管理员权限(MFT 直读)。**不写数据库**,只跑 collect_entries ——
那是整次扫描的 81%,而且不碰库,不会污染真实快照。

    tools\\run_elevated.bat bench_two_drives.py same
    tools\\run_elevated.bat bench_two_drives.py one C:
    tools\\run_elevated.bat bench_two_drives.py one D:

三次跑完自己对比:same 里的 D: 对上 `one D:` 的 D:,才是苹果对苹果
(D: 本来就比 C: 大,拿 same 里的 C: 和 D: 比说明不了任何事)。

为什么不在一个工具里 fork 子进程跑完全部三种:提权的子进程会各自弹一次 UAC,
而且 run_elevated.bat 一次只收一个结果文件。分三次跑,人少点一次算一次。
"""

from __future__ import annotations

import ctypes
import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.scan import snapshot as snap_mod  # noqa: E402

GIB = 2**30


def working_set() -> tuple[int, int]:
    """(当前工作集, 峰值工作集) 字节。取不到返回 (0, 0)。

    用 PROCESS_MEMORY_COUNTERS 而不是 tracemalloc:后者只看 Python 分配器,
    看不到 bytearray 背后的大块和解释器自身。这里关心的正是「这个进程占了
    多少物理内存」—— 峰值尤其要紧,它决定第二个盘面对一个多大的堆。
    """

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(pmc),
            pmc.cb,
        )
        if not ok:
            return 0, 0
        return pmc.WorkingSetSize, pmc.PeakWorkingSetSize
    except Exception:
        return 0, 0


def one_drive(drive: str, label: str) -> float:
    """跑一遍 collect_entries,报时间、条数、内存。返回秒数。"""
    ws_before, peak_before = working_set()
    print(f"--- {label} {drive} ---")
    print(f"    before: working set {ws_before / GIB:.2f} GiB   "
          f"peak {peak_before / GIB:.2f} GiB")

    t = time.perf_counter()
    entries, method, warns, reason = snap_mod.collect_entries(drive)
    secs = time.perf_counter() - t

    ws_after, peak_after = working_set()
    total = sum(e.bytes for e in entries if not e.is_dir)
    print(f"    {secs:>7.2f}s   {len(entries):,} entries   "
          f"{total / GIB:,.1f} GiB   method={method}")
    print(f"    after:  working set {ws_after / GIB:.2f} GiB   "
          f"peak {peak_after / GIB:.2f} GiB")
    if reason:
        print(f"    fallback: {reason}")
        print("    !! fell back to scandir -- not the shape we want to measure")
    for w in warns[:3]:
        print(f"    note: {w}")

    # 显式放掉,再 collect 一次。第二个盘面对的堆到底有多大,取决于这一步
    # 能还回去多少 —— 这正是要量的东西,所以要在两次之间做,而且要报出来。
    del entries
    gc.collect()
    ws_freed, peak_freed = working_set()
    print(f"    freed:  working set {ws_freed / GIB:.2f} GiB   "
          f"peak {peak_freed / GIB:.2f} GiB")
    print(f"    -> 放掉之后还占着 {ws_freed / GIB:.2f} GiB"
          f"(峰值 {peak_freed / GIB:.2f} GiB 不会退)\n")
    return secs


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "same"

    if mode == "one":
        drive = (sys.argv[2] if len(sys.argv) > 2 else "C:").upper()
        if not drive.endswith(":"):
            drive += ":"
        print(f"fresh process, single drive {drive}\n")
        secs = one_drive(drive, "fresh")
        print(f"RESULT  fresh {drive}  {secs:.2f}s")
        return 0

    if mode != "same":
        print(f"unknown mode {mode!r} -- use 'same' or 'one'")
        return 2

    print("one process, both drives back to back -- the production shape\n")
    first = one_drive("C:", "first ")
    second = one_drive("D:", "second")
    print(f"RESULT  same-process   C: {first:.2f}s   D: {second:.2f}s")
    print("\n把这里的 D: 和 `run_elevated.bat bench_two_drives.py one D:` 的数比。")
    print("别拿这里的 C: 和 D: 互比 —— D: 本来就大得多,那个比值什么都说明不了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
