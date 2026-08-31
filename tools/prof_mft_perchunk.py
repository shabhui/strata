"""196 块里,每一块各花多少秒 —— 定位那个 N² 从哪来。

prof_mft_fresh.py 量到解析速度随规模超线性下降(新进程也一样,不是污染):

    25 块   9.1 µs/条
    50 块  20.6 µs/条
   196 块  49.0 µs/条

工作量涨 8 倍,每条耗时涨 5.4 倍 —— 总时间接近 N²。但解析一条记录的
工作量跟总条数无关,所以这个 N² 是外面来的。两种可能:

    「越跑越慢」  第 1 块快、第 196 块慢 → 有东西在随时间累积
    「一开始就慢」 每块一样慢 → 是启动条件不同(睿频、别的进程)

这个工具逐块打时间,一眼就能分开。

    python tools/prof_mft_perchunk.py

不需要管理员权限。顺便报每块之后的进程内存和 GC 存活对象数。
"""

from __future__ import annotations

import ctypes
import gc
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from strata.ntfs import mft  # noqa: E402

from bench_parse_record import (  # noqa: E402
    CHUNK_RECORDS, FakeVol, REC, SECTOR, build_chunk,
)

N_CHUNKS = 196
MIB = 1024 * 1024


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage2", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def mem() -> tuple[int, int]:
    """(当前工作集字节, 累计缺页数)。取不到就返回 (0, 0)。"""
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD,
        ]
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(
            k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb
        ):
            return 0, 0
        return pmc.WorkingSetSize, pmc.PageFaultCount
    except Exception:
        return 0, 0


def main() -> int:
    reader = mft.MftReader.__new__(mft.MftReader)
    reader.vol = FakeVol()
    reader.boot = FakeVol.boot
    reader.record_size = REC
    reader.sector_size = SECTOR
    reader.stats = mft.MftStats()
    reader._runs = None

    template = build_chunk(CHUNK_RECORDS, 100_000)
    kept: list[mft.FileEntry] = []
    gc.collect()

    w0, f0 = mem()
    print(f"起点:工作集 {w0 / MIB:,.0f} MiB,缺页 {f0:,}\n")
    print(f"{'块':>5} {'本块秒':>8} {'µs/条':>8} {'累计条数':>11} "
          f"{'工作集':>9} {'缺页增量':>11} {'GC 对象':>11}")

    marks: list[float] = []
    t_all = time.perf_counter()
    prev_faults = f0
    for c in range(N_CHUNKS):
        buf = bytearray(template)
        base = 100_000 + c * CHUNK_RECORDS
        t = time.perf_counter()
        for i in range(CHUNK_RECORDS):
            entry, _ext = reader._parse_record(buf, i * REC, base + i)
            if entry is not None:
                kept.append(entry)
        secs = time.perf_counter() - t
        marks.append(secs)

        # 只打头几块、尾几块和每 25 块,别刷屏
        if c < 5 or c >= N_CHUNKS - 3 or c % 25 == 0:
            ws, fa = mem()
            n_obj = len(gc.get_objects()) if c < 5 or c >= N_CHUNKS - 3 else -1
            obj_s = f"{n_obj:,}" if n_obj >= 0 else "(略)"
            print(f"{c:>5} {secs:>8.3f} {secs / CHUNK_RECORDS * 1e6:>8.1f} "
                  f"{len(kept):>11,} {ws / MIB:>8,.0f}M {fa - prev_faults:>11,} "
                  f"{obj_s:>11}")
            prev_faults = fa

    total = time.perf_counter() - t_all
    first5 = sum(marks[:5]) / 5
    last5 = sum(marks[-5:]) / 5
    print(f"\n全程 {total:.1f}s,{len(kept):,} 条")
    print(f"前 5 块平均 {first5 * 1000:.0f} ms/块 = {first5 / CHUNK_RECORDS * 1e6:.1f} µs/条")
    print(f"后 5 块平均 {last5 * 1000:.0f} ms/块 = {last5 / CHUNK_RECORDS * 1e6:.1f} µs/条")
    print(f"末尾比开头慢 {last5 / first5:.1f}x")
    if last5 / first5 > 1.5:
        print("\n→「越跑越慢」。有东西在随时间累积,不是启动条件的问题。")
    else:
        print("\n→ 每块一样慢。是启动条件不同(睿频/别的进程),不是累积。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
