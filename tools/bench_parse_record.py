"""_parse_record 每秒能解多少条 —— 这是 100 秒里最后一段没量过的。

要回答的问题:C: 走 MFT 全程 100.5 秒(config.py:138)。逐段量下来:

    收集后那五遍            8.8s   prof_pipeline.py
    _mft_to_scan_entries   11.6s   prof_mft_convert.py(其中 resolve_paths 只 1.1s)
    读 1.5 GiB 的 MFT       1.3s   bench_nobuffering.py(NO_BUFFERING 无罪,
                                   两种读法都 583~1699 MB/s)
    ─────────────────────────────
    剩给解析               ~79s

bench_mft_read.py 的开头假定解析只值 12.7 秒(按「127,109 条/秒」外推),
**那个速率没有出处**。这个工具直接量。

不需要管理员权限:自己合成符合格式的 MFT 记录,不碰真盘。

    python tools/bench_parse_record.py

合成记录比真盘上的简单(3 个属性、名字短、没有 $ATTRIBUTE_LIST),所以量出来的
速率是**上限** —— 真盘只会更慢。要是连上限都远低于 127,109 条/秒,那 79 秒
就找到主人了。
"""

from __future__ import annotations

import gc
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs import mft  # noqa: E402

REC = 1024
SECTOR = 512
N_RECORDS = 1_610_000        # C: 的 MFT 记录数
CHUNK_RECORDS = mft.CHUNK_RECORDS


def make_record(record_number: int, name: str, *, is_dir: bool = False) -> bytearray:
    """造一条能通过 fixup 校验、三个属性都解得出来的 MFT 记录。

    布局(和真盘一致):
        0x00  记录头,0x30 字节
        0x30  更新序列数组:USN(2) + 每扇区原值(2 × 2)
        0x38  $STANDARD_INFORMATION
        ...   $FILE_NAME
        ...   $DATA(非常驻)
        ...   0xFFFFFFFF 结束标记

    fixup 是关键:NTFS 把每个扇区最后两字节换成 USN,原值存数组里。
    造记录得反着来 —— 先把原值写进数组,再把扇区尾巴改成 USN,
    这样 apply_fixups 校验才过(attributes.py:187)。
    """
    buf = bytearray(REC)
    usn = b"\x07\x00"
    usa_offset = 0x30
    usa_count = 3                    # 1 个 USN + 2 个扇区
    attrs_offset = 0x38

    pos = attrs_offset

    # ---- $STANDARD_INFORMATION(常驻,0x48 字节值)----
    ft = (int(time.time()) + 11_644_473_600) * 10_000_000
    si_val = struct.pack("<QQQQI", ft, ft, ft, ft,
                         A.FILE_ATTR_DIRECTORY if is_dir else A.FILE_ATTR_ARCHIVE)
    si_val += b"\x00" * (0x48 - len(si_val))
    si_len = 24 + len(si_val)
    si_len += (-si_len) % 8
    struct.pack_into("<IIBBHHH", buf, pos, A.ATTR_STANDARD_INFORMATION, si_len,
                     0, 0, 0, 0, 0)
    struct.pack_into("<IH", buf, pos + 16, len(si_val), 24)
    buf[pos + 24 : pos + 24 + len(si_val)] = si_val
    pos += si_len

    # ---- $FILE_NAME(常驻)----
    nb = name.encode("utf-16-le")
    fn_val = struct.pack("<QQQQQQQIIBB",
                         5,          # 父引用 = 根
                         ft, ft, ft, ft,
                         4096, 4096,
                         A.FILE_ATTR_DIRECTORY if is_dir else A.FILE_ATTR_ARCHIVE,
                         0, len(name), A.NAMESPACE_WIN32) + nb
    fn_len = 24 + len(fn_val)
    fn_len += (-fn_len) % 8
    struct.pack_into("<IIBBHHH", buf, pos, A.ATTR_FILE_NAME, fn_len, 0, 0, 0, 0, 1)
    struct.pack_into("<IH", buf, pos + 16, len(fn_val), 24)
    buf[pos + 24 : pos + 24 + len(fn_val)] = fn_val
    pos += fn_len

    # ---- $DATA(非常驻,只要头,不要运行列表)----
    if not is_dir:
        da_len = 16 + A._ATTR_NONRESIDENT.size
        da_len += (-da_len) % 8
        struct.pack_into("<IIBBHHH", buf, pos, A.ATTR_DATA, da_len, 1, 0, 0, 0, 2)
        struct.pack_into("<QQHHIQQQ", buf, pos + 16,
                         0,              # lowest_vcn
                         3,              # highest_vcn
                         0x40,           # runlist_offset
                         0, 0,
                         16384,          # allocated_size
                         12345,          # real_size
                         12345)          # initialized_size
        pos += da_len

    struct.pack_into("<I", buf, pos, A.ATTR_END)
    used = pos + 8

    # ---- 记录头 ----
    flags = A.MFT_RECORD_IN_USE | (A.MFT_RECORD_IS_DIRECTORY if is_dir else 0)
    struct.pack_into("<4sHHQHHHHIIQHHI", buf, 0,
                     A.MAGIC_FILE, usa_offset, usa_count, 0,
                     1,              # sequence
                     1,              # hard_links
                     attrs_offset, flags, used, REC,
                     0,              # base_reference(0 = 基记录)
                     3, 0, record_number)

    # ---- fixup:先存原值,再把扇区尾巴换成 USN ----
    buf[usa_offset : usa_offset + 2] = usn
    for i in range(usa_count - 1):
        tail = (i + 1) * SECTOR - 2
        orig = bytes(buf[tail : tail + 2])
        buf[usa_offset + 2 + i * 2 : usa_offset + 4 + i * 2] = orig
        buf[tail : tail + 2] = usn
    return buf


def build_chunk(n: int, start_record: int) -> bytearray:
    """拼出一个装满记录的块,模仿 read_entries 读到的 8 MiB。"""
    chunk = bytearray()
    for i in range(n):
        rec = start_record + i
        # 每 5 条里一个目录,和真盘的比例接近(290k 目录 / 1.6M 记录 ≈ 18%)
        is_dir = (i % 5 == 0)
        chunk += make_record(rec, f"file{rec}.dat" if not is_dir else f"dir{rec}", is_dir=is_dir)
    return chunk


class FakeVol:
    """MftReader 只在初始化时读引导扇区,给它一个假的就够。"""

    class _Boot:
        bytes_per_sector = SECTOR
        bytes_per_cluster = 4096
        bytes_per_mft_record = REC
        mft_cluster = 0
        mft_offset = 0

    boot = _Boot()

    def read(self, offset: int, length: int) -> bytes:
        return b"\x00" * length


def main() -> int:
    print("先自检:合成的记录得真能解出来,不然量的是失败路径的速度")
    reader = mft.MftReader.__new__(mft.MftReader)
    reader.vol = FakeVol()
    reader.boot = FakeVol.boot
    reader.record_size = REC
    reader.sector_size = SECTOR
    reader.stats = mft.MftStats()
    reader._runs = None

    probe = make_record(1234, "hello.txt")
    entry, ext = reader._parse_record(probe, 0, 1234)
    if entry is None:
        print(f"  ✗ 解不出来。stats={reader.stats}")
        return 2
    print(f"  ✓ record={entry.record} name={entry.name!r} parent={entry.parent} "
          f"is_dir={entry.is_dir} bytes={entry.bytes:,}")
    d = make_record(1235, "somedir", is_dir=True)
    e2, _ = reader._parse_record(d, 0, 1235)
    if e2 is None or not e2.is_dir:
        print("  ✗ 目录那条解不出来")
        return 2
    print(f"  ✓ record={e2.record} name={e2.name!r} is_dir={e2.is_dir}")

    print(f"\n造 {CHUNK_RECORDS:,} 条一块({CHUNK_RECORDS * REC / 2**20:.0f} MiB),"
          f"重复解到 {N_RECORDS:,} 条")
    t = time.perf_counter()
    template = build_chunk(CHUNK_RECORDS, 100_000)
    print(f"  造块本身 {time.perf_counter() - t:.1f}s(不算在下面)\n")

    n_chunks = N_RECORDS // CHUNK_RECORDS
    gc.collect()

    reader.stats = mft.MftStats()
    total = 0
    t = time.perf_counter()
    for c in range(n_chunks):
        # 每块要新的 bytearray:apply_fixups 就地改写,同一块解第二遍
        # USN 校验就不过了(attributes.py:190 已经把原值写回去了)
        buf = bytearray(template)
        base = 100_000 + c * CHUNK_RECORDS
        for i in range(CHUNK_RECORDS):
            entry, ext = reader._parse_record(buf, i * REC, base + i)
            total += 1
    secs = time.perf_counter() - t

    st = reader.stats
    rate = total / secs
    print(f"{'解析':<26} {secs:>7.2f}s   {total:,} 条   {rate:,.0f} 条/秒")
    print(f"{'  其中 in_use':<26} {st.records_in_use:>12,}")
    print(f"{'  fixup 失败':<26} {st.fixup_failures:>12,}")
    print(f"{'  解析失败':<26} {st.parse_failures:>12,}")
    print(f"{'  无名字':<26} {st.unnamed:>12,}")

    if st.fixup_failures or st.parse_failures or st.unnamed:
        print("\n⚠ 有失败 —— 量到的是失败路径的速度,不能用")
        return 2

    print(f"\n对照 bench_mft_read.py 假定的 127,109 条/秒:{rate / 127_109:.2f}x")
    print(f"按这个速率,C: 的 161 万条要 {N_RECORDS / rate:.1f}s")
    print("\nC: 走 MFT 全程 100.5s 的账:")
    print(f"  解析(本工具)              {N_RECORDS / rate:>5.1f}s")
    print(f"  读 1.5 GiB(bench_nobuffering) 1.3s")
    print(f"  _mft_to_scan_entries        11.6s")
    print(f"  收集后五遍                    8.8s")
    print(f"  ──────────────────────────────────")
    print(f"  合计                        {N_RECORDS / rate + 1.3 + 11.6 + 8.8:>5.1f}s"
          f"   (实测 100.5s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
