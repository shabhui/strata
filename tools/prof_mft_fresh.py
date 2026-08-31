"""每个规模换一个新进程量 —— 同进程连着量会互相污染。

prof_mft_gc.py 撞到一件怪事:同一个进程里,先量的 10 块是 9.0 µs/条,
后量的 10 块是 54.9 µs/条。同样代码、同样数据,差 6 倍,而 gc.disable()
只省得下 4%。拐点出现在第一组的 25 块和 50 块之间,而且过了拐点之后
再也回不去 —— 说明变的不是「这次要解多少条」,是进程自己。

嫌疑:堆长大之后分配器/操作系统给页的方式变了(1.6 万条 FileEntry 按
slots 算也有几百 MB),或者 CPU 从睿频掉回基频。

    python tools/prof_mft_fresh.py

每个规模开一个干净的子进程,只量一次就退出。谁也污染不了谁。
不需要管理员权限。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 子进程里跑的那一段。故意写成一次性的:量完就退,不给污染的机会。
CHILD = r"""
import gc, os, sys, time
sys.path.insert(0, r"{root}\src")
sys.path.insert(0, r"{root}\tools")
from strata.ntfs import mft
from bench_parse_record import CHUNK_RECORDS, FakeVol, REC, SECTOR, build_chunk

n_chunks = int(sys.argv[1])
keep = sys.argv[2] == "keep"
no_gc = sys.argv[3] == "nogc"

r = mft.MftReader.__new__(mft.MftReader)
r.vol = FakeVol(); r.boot = FakeVol.boot
r.record_size = REC; r.sector_size = SECTOR
r.stats = mft.MftStats(); r._runs = None

template = build_chunk(CHUNK_RECORDS, 100_000)
kept = []
if no_gc:
    gc.disable()
gc.collect()

total = 0
t = time.perf_counter()
for c in range(n_chunks):
    buf = bytearray(template)
    base = 100_000 + c * CHUNK_RECORDS
    for i in range(CHUNK_RECORDS):
        entry, _ext = r._parse_record(buf, i * REC, base + i)
        if keep and entry is not None:
            kept.append(entry)
        total += 1
secs = time.perf_counter() - t

# 峰值内存,用来判断是不是内存压力
peak = 0
try:
    import ctypes
    from ctypes import wintypes
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    peak = pmc.PeakWorkingSetSize
    faults = pmc.PageFaultCount
except Exception:
    faults = 0

print(f"{{secs:.3f}} {{total}} {{peak}} {{faults}} {{len(kept)}}")
"""


def child(n_chunks: int, *, keep: bool, no_gc: bool = False) -> tuple[float, int, int, int]:
    code = CHILD.format(root=str(ROOT))
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-u", "-c", code, str(n_chunks),
         "keep" if keep else "drop", "nogc" if no_gc else "gc"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise SystemExit(f"子进程失败:\n{proc.stderr}")
    secs, total, peak, faults, _kept = proc.stdout.strip().split()
    return float(secs), int(total), int(peak), int(faults)


def main() -> int:
    mib = 1024 * 1024
    print("每个规模一个新进程,留住条目(read_entries 的真实行为)")
    print(f"{'块':>5} {'条数':>11} {'秒':>7} {'条/秒':>11} {'µs/条':>8} "
          f"{'峰值内存':>10} {'缺页':>12}")
    for n in (10, 25, 50, 100, 196):
        secs, total, peak, faults = child(n, keep=True)
        print(f"{n:>5} {total:>11,} {secs:>7.2f} {total / secs:>11,.0f} "
              f"{secs / total * 1e6:>8.1f} {peak / mib:>9,.0f}M {faults:>12,}")

    print("\n不留条目(对照:排掉「攥着长列表」这个因素)")
    print(f"{'块':>5} {'条数':>11} {'秒':>7} {'条/秒':>11} {'µs/条':>8} "
          f"{'峰值内存':>10} {'缺页':>12}")
    for n in (10, 196):
        secs, total, peak, faults = child(n, keep=False)
        print(f"{n:>5} {total:>11,} {secs:>7.2f} {total / secs:>11,.0f} "
              f"{secs / total * 1e6:>8.1f} {peak / mib:>9,.0f}M {faults:>12,}")

    print("\n196 块,A/B gc.disable()(各自新进程)")
    on = child(196, keep=True)
    off = child(196, keep=True, no_gc=True)
    print(f"  GC 开   {on[0]:>6.2f}s  {on[1] / on[0]:>9,.0f} 条/秒  "
          f"峰值 {on[2] / mib:,.0f}M")
    print(f"  GC 关   {off[0]:>6.2f}s  {off[1] / off[0]:>9,.0f} 条/秒  "
          f"峰值 {off[2] / mib:,.0f}M")
    print(f"  → 关掉快 {on[0] / off[0]:.2f}x,省 {on[0] - off[0]:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
