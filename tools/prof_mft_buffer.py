"""每块新分配 8 MiB,还是复用一块 —— 差多少。

前面查到的:解析每条记录 9 µs 起步,跑到第 50 块变成 53 µs,而且

    硬件不掉频        纯整数循环 80 秒只慢 1.04x(prof_cpu_sustained.py)
    不是换页          缺页每块稳定 2,800 次,从头到尾不变
    不是攥着长列表    不留条目也慢(44.4 µs/条)
    不是 GC           gc.disable() 只省 5%

剩下的嫌疑是每块那次 8 MiB 分配。196 块 = 1.5 GB,Windows 要给每次新分配
清零页面。这不是测试的毛病 —— 真实的 read_entries 也是每块
`bytearray(raw)`(mft.py:255),同一个模式。

    python tools/prof_mft_buffer.py

四个变体各开新进程。不需要管理员权限。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = r'''
import gc, sys, time
sys.path.insert(0, r"{root}\src")
sys.path.insert(0, r"{root}\tools")
from strata.ntfs import mft
from bench_parse_record import CHUNK_RECORDS, FakeVol, REC, SECTOR, build_chunk

mode = sys.argv[1]           # fresh | reuse
keep = sys.argv[2] == "keep"
n_chunks = 196

r = mft.MftReader.__new__(mft.MftReader)
r.vol = FakeVol(); r.boot = FakeVol.boot
r.record_size = REC; r.sector_size = SECTOR
r.stats = mft.MftStats(); r._runs = None

template = build_chunk(CHUNK_RECORDS, 100_000)
kept = []
gc.collect()

# 复用模式下预先分配一块,之后只做切片赋值 —— 不再向系统要新页
scratch = bytearray(len(template)) if mode == "reuse" else None

marks = []
total = 0
for c in range(n_chunks):
    if mode == "reuse":
        scratch[:] = template          # 就地覆盖,复用同一块内存
        buf = scratch
    else:
        buf = bytearray(template)      # 每块向系统要 8 MiB
    base = 100_000 + c * CHUNK_RECORDS
    t = time.perf_counter()
    for i in range(CHUNK_RECORDS):
        entry, _ext = r._parse_record(buf, i * REC, base + i)
        if keep and entry is not None:
            kept.append(entry)
        total += 1
    marks.append(time.perf_counter() - t)

first = sum(marks[:5]) / 5
last = sum(marks[-5:]) / 5
print(f"{{sum(marks):.3f}} {{total}} {{first:.5f}} {{last:.5f}}")
'''


def child(mode: str, keep: bool) -> tuple[float, int, float, float]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-u", "-c", CHILD.format(root=str(ROOT)),
         mode, "keep" if keep else "drop"],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise SystemExit(f"子进程失败:\n{proc.stderr}")
    secs, total, first, last = proc.stdout.strip().split()
    return float(secs), int(total), float(first), float(last)


def main() -> int:
    print("196 块 × 8,192 条 = 161 万条,每个变体一个新进程\n")
    print(f"{'变体':<28} {'秒':>7} {'条/秒':>10} {'前5块':>9} {'后5块':>9} {'尾/头':>7}")
    rows = []
    for mode, keep, label in (
        ("fresh", True, "每块新分配 + 留条目"),
        ("fresh", False, "每块新分配 + 不留"),
        ("reuse", True, "复用一块 + 留条目"),
        ("reuse", False, "复用一块 + 不留"),
    ):
        secs, total, first, last = child(mode, keep)
        rows.append((label, secs, total, first, last))
        print(f"{label:<28} {secs:>7.2f} {total / secs:>10,.0f} "
              f"{first * 1000:>8.0f}ms {last * 1000:>8.0f}ms "
              f"{last / first:>6.1f}x")

    fresh_keep = rows[0][1]
    reuse_keep = rows[2][1]
    print(f"\n复用 vs 每块新分配(都留条目):{fresh_keep:.1f}s → {reuse_keep:.1f}s,"
          f"快 {fresh_keep / reuse_keep:.2f}x")
    print(f"换算到 C: 的 161 万条:{reuse_keep:.1f}s(现在是 {fresh_keep:.1f}s)")
    print("\n「尾/头」那一列是关键:接近 1.0 说明整趟速度稳定,")
    print("远大于 1.0 说明越跑越慢 —— 那才是要修的东西。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
