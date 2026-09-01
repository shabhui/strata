"""合掉那第二遍全表扫值多少 —— 同进程交错,只报比值。

来路:_mft_to_scan_entries 里联接点计数原来是循环外的
`sum(1 for e in entries if e.is_reparse)`,112 万条条目走第二遍。
主循环本来就在遍历同一个列表。

净差额**不是**「一整遍遍历」:property 调用两边次数一样(老代码在第二遍调,
新代码在主循环里调),省掉的只是 112 万次 genexpr 迭代 + 一次列表遍历。
所以这里量的就是这个差,不是整个函数 —— 整个函数里 resolve_paths 占大头,
会把信号淹掉。

    python tools/bench_reparse_pass.py

两个变体的循环体都是空的(真实的循环体在两边一模一样,不影响差额):

    老   for e in entries: pass          再  sum(1 for e in entries if e.is_reparse)
    新   for e in entries: if e.is_reparse: c += 1

交错三轮、两种顺序,报中位数比值。绝对值在有负载的机器上不可复现 ——
这条是花了三次教训学的(见 tests/test_bucket_fast_path.py 开头)。
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs import mft  # noqa: E402

# 本机 C: 的实测规模(tools/prof_collect_stages.py):112 万条进转换循环,
# 其中 72,381 个联接点。
N_ENTRIES = 1_120_000
N_REPARSE = 72_381


def build_entries() -> list[mft.FileEntry]:
    """照本机比例造条目。只有 attributes 这一个字段影响被量的代码。"""
    out: list[mft.FileEntry] = []
    # 每 N_ENTRIES / N_REPARSE 条里放一个联接点,别让它们挤在一起
    stride = N_ENTRIES // N_REPARSE
    for i in range(N_ENTRIES):
        attrs = A.FILE_ATTR_ARCHIVE
        if i % stride == 0:
            attrs |= A.FILE_ATTR_REPARSE_POINT
        out.append(
            mft.FileEntry(
                record=1000 + i, parent=5, name=f"f{i}.bin",
                is_dir=False, bytes=4096, attributes=attrs,
            )
        )
    return out


def old_way(entries: list[mft.FileEntry]) -> int:
    for e in entries:
        pass
    return sum(1 for e in entries if e.is_reparse)


def new_way(entries: list[mft.FileEntry]) -> int:
    count = 0
    for e in entries:
        if e.is_reparse:
            count += 1
    return count


def timed(fn, entries) -> tuple[float, int]:
    t = time.perf_counter()
    got = fn(entries)
    return time.perf_counter() - t, got


def main() -> int:
    print(f"造 {N_ENTRIES:,} 条条目({N_REPARSE:,} 个联接点)…")
    t = time.perf_counter()
    entries = build_entries()
    print(f"  {time.perf_counter() - t:.1f}s(不算在下面)\n")

    olds: list[float] = []
    news: list[float] = []
    for rnd in range(3):
        # 换顺序:后跑的那个不该占便宜也不该吃亏
        if rnd % 2 == 0:
            so, co = timed(old_way, entries)
            sn, cn = timed(new_way, entries)
        else:
            sn, cn = timed(new_way, entries)
            so, co = timed(old_way, entries)
        if co != cn:
            print(f"  ✗ 两边数出来不一样:{co} vs {cn}")
            return 2
        olds.append(so)
        news.append(sn)
        print(f"  第 {rnd + 1} 轮   老 {so:.3f}s   新 {sn:.3f}s   "
              f"{so / sn:.2f}x   (各数出 {co:,} 个)")

    mo, mn = statistics.median(olds), statistics.median(news)
    print(f"\n中位数   老 {mo:.3f}s   新 {mn:.3f}s   省 {mo - mn:.3f}s   {mo / mn:.2f}x")
    print(f"\n按整次扫描 19.13 秒算,这一处占 {(mo - mn) / 19.13 * 100:.1f}%。")
    print("绝对值别当准:机器有负载时会一起飘,比值才稳。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
