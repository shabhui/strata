"""42 微秒解析一条 1 KB 记录,这 42 微秒花在哪。

bench_parse_record.py 量出解析是 23,889 条/秒 —— 161 万条 67 秒,正是
C: 那 100 秒里说不清的那段。一条记录才 1 KB,42 微秒太多了。

嫌疑是每条记录要新建一堆用完就扔的 dataclass:

    RecordHeader        1 个,unpack 14 个字段,实际只用 6 个
    AttributeHeader     每个属性 1 个,一条记录 3~4 个属性
    StandardInfo        1 个
    FileNameInfo        1 个
    DataSize            1 个

加起来 7~8 个对象、10 来次 struct.unpack_from、一个生成器。Python 里
建对象是主要开销,不是解字节。

    python tools/prof_parse_hot.py

不需要管理员权限。用 cProfile 跑 8 万条(够统计,又不至于等太久)。
"""

from __future__ import annotations

import cProfile
import pstats
import sys
import time
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import mft  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from bench_parse_record import (  # noqa: E402
    CHUNK_RECORDS, FakeVol, REC, SECTOR, build_chunk,
)

N_CHUNKS = 10          # 10 × 8192 ≈ 8.2 万条


def make_reader() -> mft.MftReader:
    r = mft.MftReader.__new__(mft.MftReader)
    r.vol = FakeVol()
    r.boot = FakeVol.boot
    r.record_size = REC
    r.sector_size = SECTOR
    r.stats = mft.MftStats()
    r._runs = None
    return r


def run(reader: mft.MftReader, template: bytearray, n_chunks: int) -> int:
    total = 0
    for c in range(n_chunks):
        buf = bytearray(template)
        base = 100_000 + c * CHUNK_RECORDS
        for i in range(CHUNK_RECORDS):
            reader._parse_record(buf, i * REC, base + i)
            total += 1
    return total


def main() -> int:
    template = build_chunk(CHUNK_RECORDS, 100_000)
    n = N_CHUNKS * CHUNK_RECORDS
    print(f"解析 {n:,} 条,先量个基准(不带 profiler)")

    reader = make_reader()
    t = time.perf_counter()
    run(reader, template, N_CHUNKS)
    plain = time.perf_counter() - t
    print(f"  {plain:.2f}s  {n / plain:,.0f} 条/秒  "
          f"每条 {plain / n * 1e6:.1f} µs\n")

    reader = make_reader()
    prof = cProfile.Profile()
    prof.enable()
    run(reader, template, N_CHUNKS)
    prof.disable()

    buf = StringIO()
    st = pstats.Stats(prof, stream=buf).sort_stats("tottime")
    st.print_stats(16)
    out = buf.getvalue()
    # 只留表格那部分,前面几行是 profiler 自己的统计
    lines = out.splitlines()
    start = next((i for i, l in enumerate(lines) if "tottime" in l), 0)
    print("按自身耗时排(profiler 本身会放大函数调用的开销,看比例不看绝对值):")
    for l in lines[start : start + 18]:
        print(l)

    print("\n每条记录建了几个对象:")
    counts: dict[str, int] = {}
    for (fn, ln, name), (cc, nc, tt, ct, cs) in st.stats.items():  # type: ignore[attr-defined]
        if name in ("__init__", "RecordHeader", "AttributeHeader", "StandardInfo",
                    "FileNameInfo", "DataSize") or "unpack_from" in name:
            counts[f"{Path(fn).name}:{name}"] = nc
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<44} {v:>10,}  ({v / n:.1f}/条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
