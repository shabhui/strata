"""collect_entries 那 24 秒,摊在哪儿。

来路:tools/prof_scan_stages.py 量出整次扫描 28.8 秒里 collect_entries 占
23.96 秒(83.1%),别的段加起来不到 5 秒 —— 写库只剩 0.56 秒,页缓存那次优化
已经把它压到无关紧要了。所以继续优化只有一个地方可去。

collect_entries 走 MFT 时是三段:

    read_entries          读 MFT + 解析每条记录 → FileEntry
    resolve_paths         沿父链还原完整路径(带记忆化)
    _mft_to_scan_entries  FileEntry → ScanEntry(过滤元文件、拼路径)

这个工具把三段单独计时,并且报出每段之后的内存占用 —— 因为已知还剩一个跟
规模相关的退化:同一个进程里连跑两次完整扫描是 45.52 秒然后 89.61 秒
(config.py 里那段账)。缓冲区复用去掉了「每块新分配 8 MiB」,但「建 160 万个
FileEntry 再建 110 万个 ScanEntry」还在。要判断值不值得改成流式,得先知道
这三段各自多贵、以及内存峰值在哪一段。

⚠ 需要管理员权限。不写库,只采集和计时。

    tools\\run_elevated.bat prof_collect_stages.py C:
"""

from __future__ import annotations

import ctypes
import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import mft  # noqa: E402
from strata.ntfs.volume import Volume  # noqa: E402
from strata.scan import snapshot as snap_mod  # noqa: E402

MIB = 2**20
GIB = 2**30


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


def working_set() -> int:
    """当前进程的工作集字节数。取不到返回 0。

    用 PROCESS_MEMORY_COUNTERS 而不是 tracemalloc:后者只看 Python 分配器,
    看不到 bytearray 背后的大块和解释器自身,而这里关心的是「这个进程占了
    多少物理内存」。
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

    # argtypes/restype 必须显式声明。不声明的话 GetCurrentProcess 的返回值
    # 会按 C int 截断(64 位上伪句柄是 -1,截断后传进去无效),
    # GetProcessMemoryInfo 直接失败返回 0 —— 而且是**静默**返回 0,
    # 看起来像「这个进程不占内存」。prof_mft_fresh.py 里踩过同一个坑。
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetCurrentProcess.argtypes = []
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_uint32
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        handle = k32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            raise OSError(f"GetProcessMemoryInfo 失败,错误 "
                          f"{ctypes.get_last_error()}")
        return int(pmc.WorkingSetSize)
    except OSError as exc:
        # 报出来,不要静默给 0 —— 一个总是显示 0M 的内存列会让人以为不占内存
        print(f"  ⚠ 读不到工作集:{exc}")
        return 0


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    if not is_admin():
        print("要管理员权限 —— 直读裸卷。")
        return 2

    print(f"盘 {drive}\n")
    rows: list[tuple[str, float, int, str]] = []
    base = working_set()
    print(f"起点内存 {base / MIB:,.0f}M")

    t = time.perf_counter()
    with Volume(drive) as vol:
        reader = mft.MftReader(vol)
        raw = reader.read_entries()
    secs = time.perf_counter() - t
    rows.append(("read_entries", secs, working_set(),
                 f"{len(raw):,} 个 FileEntry"))

    t = time.perf_counter()
    paths, path_stats = mft.resolve_paths(raw)
    secs = time.perf_counter() - t
    rows.append(("resolve_paths", secs, working_set(),
                 f"{len(paths):,} 条目录路径,孤儿 {path_stats.orphaned:,}"))
    del paths                     # 下一段自己会再算一遍,别让它虚占内存
    gc.collect()

    t = time.perf_counter()
    dir_paths: dict[int, str] = {}
    entries, orphan, warns = snap_mod._mft_to_scan_entries(raw, dir_paths=dir_paths)
    secs = time.perf_counter() - t
    rows.append(("_mft_to_scan_entries", secs, working_set(),
                 f"{len(entries):,} 个 ScanEntry"))

    width = max(len(n) for n, *_ in rows)
    total = sum(s for _, s, _, _ in rows)
    print(f"\n{'段':<{width}}  {'秒':>7}  {'占比':>6}  {'之后内存':>9}  说明")
    for name, secs, mem, note in rows:
        print(f"{name:<{width}}  {secs:>7.2f}  {secs / total * 100:>5.1f}%  "
              f"{mem / MIB:>8,.0f}M  {note}")
    print(f"{'合计':<{width}}  {total:>7.2f}")

    st = reader.stats
    print(f"\nMFT 统计:见到 {st.records_seen:,} 条,在用 {st.records_in_use:,},"
          f"扩展 {st.extension_records:,},无名 {st.unnamed:,}")
    print(f"          文件 {st.files:,}  目录 {st.dirs:,}  "
          f"孤儿 {st.orphaned:,}  环 {st.cycles:,}")
    print(f"          MFT 自身 {st.mft_bytes / GIB:,.2f}G")
    print(f"resolve_paths 之后 dir_paths {len(dir_paths):,} 条,"
          f"无法归属 {orphan / GIB:,.2f}G")
    for w in warns:
        print(f"警告:{w}")

    print(f"\n峰值内存 {working_set() / MIB:,.0f}M。注意 resolve_paths 被算了两次"
          f"\n(单独一次 + _mft_to_scan_entries 内部一次),真实扫描只走后者。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
