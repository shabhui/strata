"""_parse_record 每秒能解多少条 —— 这是 100 秒里最后一段没量过的。

来路(**这笔账是 100.5 秒那会儿的**,现在整次扫描 23.4 秒 —— 留着是为了看清
当初怎么把时间找出来的):C: 走 MFT 全程 100.5 秒(config.py:138)。逐段量下来:

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

量**两种**记录构成,因为只量一种会看错:

  三条全认识   $STANDARD_INFORMATION + $FILE_NAME + $DATA,全都在用。
               这是一条记录能有的最便宜的样子,所以它是速率上限。
  本机构成     照 prof_collect_stages.py 的实测比例:四分之一空闲,在用的里面
               24.4% 是目录、目录还挂着三条解析器不认识的属性。

只量前者会把「先看在用位」和「不认识的属性不建头」这两条近道的效果全抹掉 ——
那种记录里没有空闲的、也没有不认识的属性,近道一次都用不上。改这两处那天
量到的差别就是这么来的(同一份合成输入,src 用 git stash 换掉再跑一遍):

                    最初      近道一二   近道三四     累计
    三条全认识      107,224    113,702    131,982    1.23x
    本机构成        126,915    146,343    169,207    1.33x
                                              (单位:条/秒)

    近道一二  空闲记录不还原 USA;不认识的属性不建属性头
    近道三四  时间戳存原始 FILETIME、取的时候才换算;fixup 校验按整数比不切片

反直觉的一点:本机构成比「最便宜的记录」跑得**快**。因为四分之一的记录空闲,
在在用位那一步就走开了 —— 改之前它们还要先还原一遍 USA 才被丢掉。
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


def filler_attr(type_code: int, value_len: int, attr_id: int) -> bytes:
    """任意类型码的一条常驻属性,内容不重要,只为占住长度。

    真盘上的目录还挂着 $INDEX_ROOT / $INDEX_ALLOCATION / $BITMAP,文件常有
    $OBJECT_ID。解析器一个都不认,但**它们仍然要被走过**:得取出长度才能
    找到下一条。所以「一条属性有多贵」得分成「认识的」和「不认识的」两种来量,
    上面那个只有三条全认识的记录量不出后者。
    """
    length = 24 + value_len
    length += (-length) % 8
    buf = bytearray(length)
    struct.pack_into("<IIBBHHH", buf, 0, type_code, length, 0, 0, 0, 0, attr_id)
    struct.pack_into("<IH", buf, 16, value_len, 24)
    return bytes(buf)


def make_record(
    record_number: int,
    name: str,
    *,
    is_dir: bool = False,
    extra: bytes = b"",
    in_use: bool = True,
) -> bytearray:
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

    # ---- 解析器不认的那几条(目录的索引属性之类)----
    if extra:
        buf[pos : pos + len(extra)] = extra
        pos += len(extra)

    struct.pack_into("<I", buf, pos, A.ATTR_END)
    used = pos + 8

    # ---- 记录头 ----
    flags = (A.MFT_RECORD_IN_USE if in_use else 0) | (
        A.MFT_RECORD_IS_DIRECTORY if is_dir else 0
    )
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


def build_chunk_mixed(n: int, start_record: int) -> bytearray:
    """照本机 C: 的实际构成拼一块,不是照「最省事的合成记录」。

    比例取自 tools/prof_collect_stages.py 那次提权跑的统计:

        见到 1,611,542 条   在用 1,204,672 条   →  空闲 406,870 条(25.2%)
        文件 824,439        目录 293,461       →  目录占在用的 24.4%

    两处和上面那个 build_chunk 不同,而这两处正好是解析器能省下功夫的地方:

      * 目录带 $INDEX_ROOT / $INDEX_ALLOCATION / $BITMAP —— 三条都不认识,
        但都得走过去
      * 四分之一的记录是空闲的 —— 在用位一看就该走开,不必还原 USA

    所以 build_chunk 量的是「一条最便宜的记录」,这个量的是「本机的一条记录」。
    两个都要:前者是上限,后者是实际。
    """
    # 长度照真盘上常见的量级取,不必精确 —— 要紧的是「有几条不认识的属性」
    dir_extra = (
        filler_attr(A.ATTR_INDEX_ROOT, 200, 3)
        + filler_attr(A.ATTR_INDEX_ALLOCATION, 80, 4)
        + filler_attr(0xB0, 32, 5)          # $BITMAP
    )
    chunk = bytearray()
    for i in range(n):
        rec = start_record + i
        slot = i % 1000
        if slot < 252:                       # 25.2% 空闲
            chunk += make_record(rec, f"free{rec}.dat", in_use=False)
        elif slot < 252 + 183:               # 在用里 24.4% 是目录
            chunk += make_record(rec, f"dir{rec}", is_dir=True, extra=dir_extra)
        else:
            chunk += make_record(rec, f"file{rec}.dat")
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


def timed_pass(reader, template: bytearray, n_chunks: int) -> tuple[float, int, mft.MftStats]:
    """把同一块解 n_chunks 遍,返回 (秒, 条数, 统计)。"""
    reader.stats = mft.MftStats()
    gc.collect()
    total = 0
    t = time.perf_counter()
    for c in range(n_chunks):
        # 每块要新的 bytearray:apply_fixups 就地改写,同一块解第二遍
        # USN 校验就不过了(attributes.py 里已经把原值写回去了)
        buf = bytearray(template)
        base = 100_000 + c * CHUNK_RECORDS
        for i in range(CHUNK_RECORDS):
            entry, ext = reader._parse_record(buf, i * REC, base + i)
            total += 1
    return time.perf_counter() - t, total, reader.stats


def report_pass(label: str, secs: float, total: int, st: mft.MftStats) -> float:
    rate = total / secs
    print(f"{label:<12} {secs:>7.2f}s  {total:,} 条  {rate:>9,.0f} 条/秒")
    print(f"{'':<12} 在用 {st.records_in_use:,}   fixup 失败 {st.fixup_failures}   "
          f"解析失败 {st.parse_failures}   无名字 {st.unnamed}")
    return rate


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

    t = time.perf_counter()
    mixed = build_chunk_mixed(CHUNK_RECORDS, 100_000)
    print(f"  再造一块本机构成的({time.perf_counter() - t:.1f}s,也不算)\n")

    n_chunks = N_RECORDS // CHUNK_RECORDS

    # 两块都跑。顺序上后跑的那块占不到便宜也吃不到亏 —— 每块自己重建 bytearray,
    # 缓存状态一样。真要怀疑就换个顺序再跑一遍。
    secs_a, total_a, st_a = timed_pass(reader, template, n_chunks)
    rate_a = report_pass("三条全认识", secs_a, total_a, st_a)
    if st_a.fixup_failures or st_a.parse_failures or st_a.unnamed:
        print("\n⚠ 有失败 —— 量到的是失败路径的速度,不能用")
        return 2

    secs_b, total_b, st_b = timed_pass(reader, mixed, n_chunks)
    rate_b = report_pass("本机构成", secs_b, total_b, st_b)
    if st_b.fixup_failures or st_b.parse_failures or st_b.unnamed:
        print("\n⚠ 有失败 —— 量到的是失败路径的速度,不能用")
        return 2

    print(f"\n本机构成反而**快** {rate_b / rate_a:.2f}x。不是记录变便宜了 —— 是四分之一的"
          f"\n记录空闲,在「看在用位」那一步就走开了,以前它们还得先还原一遍 USA。"
          f"\n目录身上那三条不认识的属性也只是走过去,不建属性头。")
    print(f"按「本机构成」这个速率,161 万条要 {N_RECORDS / rate_b:.1f}s")
    print("\n注:这里量的只是解析。整次扫描的账在 tools/prof_scan_stages.py"
          "\n(collect_entries 占 83.1%)和 tools/prof_collect_stages.py"
          "\n(collect 里 read_entries 占 89.8%)。别拿这个数直接当扫描时间。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
