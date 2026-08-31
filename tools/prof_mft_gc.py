"""解析速度随规模掉下去,掉的是 GC。

两个工具量同一段代码,差了 5 倍:

    prof_parse_hot.py      10 块(8.2 万条)   117,098 条/秒   8.5 µs/条
    bench_parse_record.py 196 块(161 万条)    23,889 条/秒  42.2 µs/条

解析一条记录的工作量跟总条数无关,所以这不是解析变慢了,是有个跟规模相关的
东西在拖。嫌疑是分代 GC:每条记录要建 7~8 个用完就扔的对象
(RecordHeader、每属性一个 AttributeHeader、StandardInfo、FileNameInfo、
DataSize),161 万条就是一千多万个对象过分配器。二代回收按分配量触发,
每次要扫全部存活对象 —— 而 read_entries 手里正攥着一个越来越长的
entries 列表(真实全盘 108 万个 FileEntry,全是 GC 跟踪的对象)。

    python tools/prof_mft_gc.py

不需要管理员权限。两组对照:块数递增看每条耗时怎么变,再 A/B gc.disable()。
"""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from strata.ntfs import mft  # noqa: E402

from bench_parse_record import (  # noqa: E402
    CHUNK_RECORDS, FakeVol, REC, SECTOR, build_chunk,
)


def make_reader() -> mft.MftReader:
    r = mft.MftReader.__new__(mft.MftReader)
    r.vol = FakeVol()
    r.boot = FakeVol.boot
    r.record_size = REC
    r.sector_size = SECTOR
    r.stats = mft.MftStats()
    r._runs = None
    return r


def parse_n(template: bytearray, n_chunks: int, *, keep: bool,
            no_gc: bool = False) -> tuple[float, int]:
    """解析 n_chunks 块。keep=True 时留住条目,模仿 read_entries 的真实行为。"""
    reader = make_reader()
    kept: list[mft.FileEntry] = []
    if no_gc:
        gc.disable()
    else:
        gc.enable()
    gc.collect()
    total = 0
    t = time.perf_counter()
    for c in range(n_chunks):
        buf = bytearray(template)
        base = 100_000 + c * CHUNK_RECORDS
        for i in range(CHUNK_RECORDS):
            entry, _ext = reader._parse_record(buf, i * REC, base + i)
            if keep and entry is not None:
                kept.append(entry)
            total += 1
    secs = time.perf_counter() - t
    gc.enable()
    del kept
    gc.collect()
    return secs, total


def main() -> int:
    template = build_chunk(CHUNK_RECORDS, 100_000)

    print("一、块数递增,留住条目(read_entries 就是这么干的)")
    print(f"{'块':>5} {'条数':>11} {'秒':>7} {'条/秒':>11} {'µs/条':>8}")
    for n in (5, 10, 25, 50, 100, 196):
        secs, total = parse_n(template, n, keep=True)
        print(f"{n:>5} {total:>11,} {secs:>7.2f} {total / secs:>11,.0f} "
              f"{secs / total * 1e6:>8.1f}")

    print("\n二、同样块数,不留条目 —— 看是「攥着列表」还是「建对象」的问题")
    print(f"{'块':>5} {'条数':>11} {'秒':>7} {'条/秒':>11} {'µs/条':>8}")
    for n in (10, 100, 196):
        secs, total = parse_n(template, n, keep=False)
        print(f"{n:>5} {total:>11,} {secs:>7.2f} {total / secs:>11,.0f} "
              f"{secs / total * 1e6:>8.1f}")

    print("\n三、196 块,A/B gc.disable()")
    secs_on, total = parse_n(template, 196, keep=True)
    secs_off, _ = parse_n(template, 196, keep=True, no_gc=True)
    print(f"  GC 开   {secs_on:>6.2f}s  {total / secs_on:>9,.0f} 条/秒")
    print(f"  GC 关   {secs_off:>6.2f}s  {total / secs_off:>9,.0f} 条/秒")
    if secs_off > 0:
        print(f"  → 关掉快 {secs_on / secs_off:.2f}x,省下 {secs_on - secs_off:.1f}s")
    print(f"\n换算到 C: 的 161 万条:GC 开 {1_610_000 / (total / secs_on):.1f}s,"
          f"GC 关 {1_610_000 / (total / secs_off):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
