"""MFT 的 100 秒里,读盘占多少、解析占多少。

要回答的问题:实测 C: 走 MFT 是 100.5 秒,但解析只值约 12.7 秒
(127,109 条/秒 × 161 万条),解析之后的所有活加起来 11.2 秒。
**76 秒没有着落。** 这个工具把读和解析分开量,顺便 A/B 一下
FILE_FLAG_NO_BUFFERING —— 它现在默认开着(volume.py:172),
snapshot.py:205 没关它。

需要管理员权限(直读裸卷)。只读,不写库、不删东西。

    python tools/bench_mft_read.py C:

四个变体读的是同一段数据,比的是彼此的比值。缓存说明:
NO_BUFFERING 绕开 OS 缓存,但绕不开磁盘自己的预读,所以变体顺序有影响 ——
下面跑两轮,交替着来,只信两轮一致的结论。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs.mft import CHUNK_RECORDS, MftReader  # noqa: E402
from strata.ntfs.volume import AccessDenied, Volume  # noqa: E402

MIB = 1024 * 1024


def mft_extent(vol: Volume) -> tuple[list[tuple[int, int]], int]:
    """$MFT 的 (字节偏移, 字节长度) 段列表,以及总字节数。

    跳过稀疏段 —— 那里没有记录,read_entries 也是跳过的。
    """
    reader = MftReader(vol)
    bpc = vol.boot.bytes_per_cluster
    spans: list[tuple[int, int]] = []
    for run in reader.mft_runs():
        if run.sparse or run.lcn is None:
            continue
        spans.append((run.lcn * bpc, run.length * bpc))
    return spans, sum(n for _, n in spans)


def read_only(drive: str, *, no_buffering: bool, chunk: int) -> tuple[float, int]:
    """只读不解析。返回 (秒, 字节数)。"""
    with Volume(drive, no_buffering=no_buffering) as vol:
        spans, total = mft_extent(vol)
        t = time.perf_counter()
        got = 0
        for base, length in spans:
            done = 0
            while done < length:
                want = min(chunk, length - done)
                raw = vol.read(base + done, want)
                if not raw:
                    break
                got += len(raw)
                done += len(raw)
        return time.perf_counter() - t, got


def read_and_copy(drive: str, *, chunk: int) -> tuple[float, int]:
    """读 + bytearray(raw),复现 read_entries 里那次拷贝(mft.py:255)。

    和 read_only 的差就是拷贝的代价。每块其实被碰四遍:
    create_string_buffer 分配填零、buf.raw[:n]、raw[head:head+len]、
    再加这里的 bytearray。
    """
    with Volume(drive, no_buffering=True) as vol:
        spans, _ = mft_extent(vol)
        t = time.perf_counter()
        got = 0
        for base, length in spans:
            done = 0
            while done < length:
                want = min(chunk, length - done)
                raw = vol.read(base + done, want)
                if not raw:
                    break
                buf = bytearray(raw)
                got += len(buf)
                done += len(raw)
        return time.perf_counter() - t, got


def full_parse(drive: str) -> tuple[float, int, int]:
    """真正的 read_entries。返回 (秒, 条目数, 字节数)。"""
    with Volume(drive, no_buffering=True) as vol:
        _, total = mft_extent(vol)
        reader = MftReader(vol)
        t = time.perf_counter()
        entries = reader.read_entries()
        return time.perf_counter() - t, len(entries), total


def line(label: str, secs: float, nbytes: int) -> None:
    mbs = (nbytes / MIB / secs) if secs > 0 else 0.0
    print(f"  {label:<34} {secs:>7.1f}s  {nbytes / MIB:>8.1f} MiB  {mbs:>7.1f} MB/s")


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    chunk = CHUNK_RECORDS * 1024  # 和 read_entries 一样,8 MiB

    try:
        with Volume(drive, no_buffering=True) as vol:
            spans, total = mft_extent(vol)
            rec_size = MftReader(vol).record_size
    except AccessDenied as exc:
        print(f"打不开裸卷:{exc}")
        print("这个工具必须用管理员权限跑。")
        return 2

    print(f"盘 {drive}  $MFT {len(spans)} 段  {total / MIB:.1f} MiB  "
          f"约 {total // rec_size:,} 条记录")
    print(f"每次读 {chunk // MIB} MiB,共约 {-(-total // chunk):,} 次\n")

    for round_no in (1, 2):
        print(f"--- 第 {round_no} 轮 ---")
        secs, got = read_only(drive, no_buffering=True, chunk=chunk)
        line("只读(NO_BUFFERING,现在的设置)", secs, got)
        no_buf = secs

        secs, got = read_only(drive, no_buffering=False, chunk=chunk)
        line("只读(走 OS 缓存)", secs, got)
        buffered = secs

        secs, got = read_and_copy(drive, chunk=chunk)
        line("只读 + bytearray 拷贝", secs, got)
        copied = secs

        secs, got = read_only(drive, no_buffering=True, chunk=chunk * 4)
        line(f"只读(NO_BUFFERING,{chunk * 4 // MIB} MiB 一次)", secs, got)

        secs, n, nbytes = full_parse(drive)
        line(f"完整 read_entries({n:,} 条)", secs, nbytes)

        if secs > 0:
            print(f"  → 读占完整的 {no_buf / secs * 100:.0f}%,"
                  f"解析等杂活占 {(secs - no_buf) / secs * 100:.0f}%")
        if buffered > 0:
            print(f"  → 关掉 NO_BUFFERING:{no_buf / buffered:.2f}x")
        print(f"  → bytearray 那次拷贝:{copied - no_buf:+.1f}s\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
