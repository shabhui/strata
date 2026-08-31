"""WOF(Compact OS)压缩的文件,不能把逻辑大小当占盘大小。

这是实测出来的,不是推的。两条扫描路都把 C: 虚报 27 GB(+15.9%),而更早的
快照只差 +1.0~1.9% —— 说明是后来盘上多了什么东西。拿 GetCompressedFileSizeW
(它返回真实占盘字节)逐个比,前 378 个大文件里 342 个被高估,但加起来只解释
那 27 GB 的 20%。剩下 80% 摊在别处。

摊在哪:整个 Windows 目录。tools/probe_wof.py 提权直读 MFT,四个文件的记录
长这样(记录号是本机 C: 上的真实值):

    kernel32.dll   基记录 1,135,937 只有 $SI + $FILE_NAME + $ATTRIBUTE_LIST
                   扩展记录 729,364 里:
                     $DATA(未命名)              allocated 0.81M  real 0.80M  稀疏位
                     $DATA:"WofCompressedData"  allocated 0.45M
                     $REPARSE_POINT 标记 0x80000017
                   真实占盘 0.45M,我们算成 1.26M —— 高估 2.8 倍

    notepad.exe    同一个形状,高估 2.7 倍
    explorer.exe   同一个形状,高估 2.8 倍
    Sessions.xml   反过来:基记录 1,052,208 里有 WofCompressedData(17.85M),
                   幻影未命名流在扩展记录里(库里记着 137.88M)

这是 Compact OS 的 WOF(Windows Overlay Filter)压缩:真实数据搬进一条名为
WofCompressedData 的备用流,主数据流变成重解析点。整件事对普通 API 保持透明 ——
**故意不设 FILE_ATTRIBUTE_COMPRESSED 位**,免得老程序以为自己在处理压缩文件。
所以「看压缩位」这条路走不通:kernel32.dll 只有 ARCHIVE 位,却压着 1.8 倍。

两个反直觉的点,都是这个文件要钉住的:

1. **未命名流带稀疏位,但 allocated 报的是逻辑大小,不是 0。**
   平时「稀疏」意味着 allocated 远小于 real,这里正好相反 —— NTFS 为了让
   WOF 透明,照逻辑大小报分配量。所以不能靠稀疏位判断,也不能相信这个数。

2. **真实字节在备用流里,而且和 GetCompressedFileSizeW 逐字节相等。**
   四个文件全部相等(0.45M / 0.21M / 1.77M / 17.85M)。这是修法的依据:
   认出 WOF 就只算备用流,不用为 115 万个文件各调一次系统 API —— 那个代价
   (每个文件一次 CreateFile + 一次查询)在 tools/probe_overcount.py 里量过,
   400 个文件就要几秒,全盘不可行。

规则:**只要出现 WofCompressedData 这条流,未命名流的 allocated 就是幻影,
一律不计;真实占用是所有备用流的 allocated 之和。**

保留「所有备用流之和」而不是「只要 WofCompressedData」:一个文件可以既被 WOF
压着、又带别的备用流(Zone.Identifier 之类),那些是真占盘的。

难点在于属性会跨记录:WOF 文件的幻影流和真实流不一定在同一条 MFT 记录里
(Sessions.xml 就分开了)。所以「这个文件是不是 WOF」必须在读完它所有记录
之后才能定,不能一条记录一条记录地判 —— 否则只带幻影的那条扩展记录会用
max() 顶掉真实值,那正是库里 137.88M 的来路。
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.ntfs import mft  # noqa: E402
from strata.ntfs.volume import parse_boot_sector  # noqa: E402

from . import mft_fixtures as fx  # noqa: E402
from .test_mft_reader import (  # noqa: E402
    BYTES_PER_CLUSTER,
    MFT_CLUSTER,
    RECORD_SIZE,
    FakeVolume,
)

CLUSTER = BYTES_PER_CLUSTER  # 4096

# 照本机实测值取,凑到整簇。
KERNEL32_PHANTOM_ALLOC = 208 * CLUSTER      # 0.81M,幻影未命名流
KERNEL32_LOGICAL = 838_000                  # 0.80M,真实逻辑大小
KERNEL32_WOF_ALLOC = 115 * CLUSTER          # 0.45M,真实占盘

SESSIONS_WOF_ALLOC = 4_570 * CLUSTER        # 17.85M,在基记录里
SESSIONS_PHANTOM_ALLOC = 35_295 * CLUSTER   # 137.88M,在扩展记录里
SESSIONS_LOGICAL = 144_560_000

PLAIN_ALLOC = 64 * CLUSTER                  # 普通文件,不该受影响
ZONE_ALLOC = 1 * CLUSTER                    # 普通备用流,该照算

SPARSE_FLAG = 0x8000
WOF_STREAM = "WofCompressedData"


def wof_stream(alloc: int, attr_id: int = 3) -> bytes:
    return fx.attr_data_nonresident(
        allocated=alloc, real=alloc, name=WOF_STREAM, attr_id=attr_id
    )


def phantom_stream(alloc: int, logical: int, attr_id: int = 2) -> bytes:
    """WOF 的未命名主流:带稀疏位,allocated 却报逻辑大小。"""
    return fx.attr_data_nonresident(
        allocated=alloc, real=logical, attr_id=attr_id, flags=SPARSE_FLAG
    )


def build_image(records: dict[int, bytes], *, clusters: int = 16) -> bytes:
    """把记录摆成一张合成 MFT 镜像。记录 0 是 $MFT 自身。"""
    runlist = bytes([0x21, clusters & 0xFF]) + struct.pack("<h", MFT_CLUSTER)
    self_record = fx.make_mft_record(
        record_number=0,
        attributes=[
            fx.attr_standard_information(),
            fx.attr_file_name(name="$MFT", parent=5),
            fx.attr_data_nonresident(
                allocated=clusters * CLUSTER,
                real=clusters * CLUSTER,
                runlist=runlist,
            ),
        ],
    )
    table = {0: self_record}
    table.update(records)

    size = clusters * CLUSTER
    buf = bytearray(size)
    for number, raw in table.items():
        start = number * RECORD_SIZE
        if start + RECORD_SIZE > size:
            raise ValueError(f"记录 {number} 超出镜像({clusters} 簇装不下)")
        buf[start : start + RECORD_SIZE] = raw
    return bytes(b"\x00" * (MFT_CLUSTER * CLUSTER)) + bytes(buf)


def read_all(records: dict[int, bytes]) -> dict[int, mft.FileEntry]:
    boot = parse_boot_sector(
        fx.make_boot_sector(mft_cluster=MFT_CLUSTER, total_sectors=4_000_000)
    )
    vol = FakeVolume(build_image(records), boot)
    reader = mft.MftReader(vol)
    return {e.record: e for e in reader.read_entries()}


def base_with_attr_list(record: int, name: str) -> bytes:
    """基记录只有 $SI 和 $FILE_NAME —— 数据属性全在扩展记录里。

    这是 kernel32.dll 的真实形状。不放 $ATTRIBUTE_LIST:解析器不读它
    (它靠扩展记录自己的 base_reference 找回来),放了只是多占字节。
    """
    return fx.make_mft_record(
        record_number=record,
        attributes=[
            fx.attr_standard_information(),
            fx.attr_file_name(name=name, parent=5),
        ],
    )


def extension(record: int, base: int, attrs: list[bytes]) -> bytes:
    return fx.make_mft_record(
        record_number=record, base_reference=base, attributes=attrs
    )


class WofInExtensionRecordTest(unittest.TestCase):
    """kernel32.dll 的形状:幻影流和真实流都在同一条扩展记录里。"""

    def entries(self) -> dict[int, mft.FileEntry]:
        return read_all(
            {
                20: base_with_attr_list(20, "kernel32.dll"),
                21: extension(
                    21,
                    20,
                    [
                        phantom_stream(KERNEL32_PHANTOM_ALLOC, KERNEL32_LOGICAL),
                        wof_stream(KERNEL32_WOF_ALLOC),
                    ],
                ),
            }
        )

    def test_bytes_is_the_compressed_stream_only(self):
        """核心那条:只算 WofCompressedData,不加幻影流。

        断言等于精确值而不是「小于相加的和」—— 后者在幻影只被算一半时也能过。
        """
        entry = self.entries()[20]
        self.assertEqual(entry.bytes, KERNEL32_WOF_ALLOC)

    def test_it_is_not_the_sum(self):
        """把当初的错法单独钉一遍:相加是 2.8 倍,不能再回去。"""
        entry = self.entries()[20]
        wrong = KERNEL32_PHANTOM_ALLOC + KERNEL32_WOF_ALLOC
        self.assertNotEqual(entry.bytes, wrong)

    def test_logical_size_still_comes_from_the_unnamed_stream(self):
        """逻辑大小要留着 —— 界面上「文件多大」问的是这个,不是占盘。"""
        entry = self.entries()[20]
        self.assertEqual(entry.logical_bytes, KERNEL32_LOGICAL)

    def test_total_matches_the_entry(self):
        """stats.bytes_total 必须跟条目一致,否则总数和树又要对不上。

        求和时带上元文件($MFT 自身):bytes_total 是照所有非目录条目累加的,
        元文件要到 _mft_to_scan_entries 才被排掉(见 test_mft_metafiles.py)。
        这里把它们剔掉的话,差额恰好是 $MFT 的 64 KiB,看着像 WOF 算错了。
        """
        boot = parse_boot_sector(
            fx.make_boot_sector(mft_cluster=MFT_CLUSTER, total_sectors=4_000_000)
        )
        records = {
            20: base_with_attr_list(20, "kernel32.dll"),
            21: extension(
                21,
                20,
                [
                    phantom_stream(KERNEL32_PHANTOM_ALLOC, KERNEL32_LOGICAL),
                    wof_stream(KERNEL32_WOF_ALLOC),
                ],
            ),
        }
        vol = FakeVolume(build_image(records), boot)
        reader = mft.MftReader(vol)
        entries = reader.read_entries()
        files = [e for e in entries if not e.is_dir]
        self.assertEqual(reader.stats.bytes_total, sum(e.bytes for e in files))
        # 而且 kernel32.dll 那条本身就得是压缩流的大小,不是相加
        self.assertEqual(
            next(e for e in files if e.name == "kernel32.dll").bytes,
            KERNEL32_WOF_ALLOC,
        )


class WofSplitAcrossRecordsTest(unittest.TestCase):
    """Sessions.xml 的形状:真实流在基记录,幻影流在扩展记录。

    这一组是最关键的。旧代码在 _apply_pending 里对基记录已有的值取 max(),
    于是 137.88M 的幻影顶掉了 17.85M 的真实值 —— 库里记的就是 137.88M。
    「这个文件是不是 WOF」必须在读完所有记录之后才能定。
    """

    def entries(self) -> dict[int, mft.FileEntry]:
        return read_all(
            {
                22: fx.make_mft_record(
                    record_number=22,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="Sessions.xml", parent=5),
                        wof_stream(SESSIONS_WOF_ALLOC, attr_id=2),
                    ],
                ),
                23: extension(
                    23,
                    22,
                    [phantom_stream(SESSIONS_PHANTOM_ALLOC, SESSIONS_LOGICAL)],
                ),
            }
        )

    def test_phantom_in_extension_does_not_win(self):
        entry = self.entries()[22]
        self.assertEqual(entry.bytes, SESSIONS_WOF_ALLOC)

    def test_the_old_max_result_is_gone(self):
        entry = self.entries()[22]
        self.assertNotEqual(
            entry.bytes, SESSIONS_PHANTOM_ALLOC,
            "幻影流又顶掉真实值了 —— _apply_pending 的 max() 回来了",
        )

    def test_logical_size_survives_from_the_extension(self):
        """基记录没有未命名流,逻辑大小只能从扩展记录来 —— 别丢了。"""
        entry = self.entries()[22]
        self.assertEqual(entry.logical_bytes, SESSIONS_LOGICAL)


class OtherStreamsStillCountTest(unittest.TestCase):
    """WOF 文件可以同时带别的备用流,那些是真占盘的。"""

    def test_zone_identifier_is_added_to_the_wof_stream(self):
        entries = read_all(
            {
                24: fx.make_mft_record(
                    record_number=24,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="downloaded.exe", parent=5),
                        phantom_stream(KERNEL32_PHANTOM_ALLOC, KERNEL32_LOGICAL),
                        wof_stream(KERNEL32_WOF_ALLOC),
                        fx.attr_data_nonresident(
                            allocated=ZONE_ALLOC, real=ZONE_ALLOC,
                            name="Zone.Identifier", attr_id=4,
                        ),
                    ],
                )
            }
        )
        self.assertEqual(entries[24].bytes, KERNEL32_WOF_ALLOC + ZONE_ALLOC)


class NonWofFilesUnchangedTest(unittest.TestCase):
    """没有 WofCompressedData 的文件,行为必须一个字节都不变。

    这一组是防回归的:上面那条规则很容易写宽,把普通文件的未命名流也扣掉。
    """

    def test_plain_file(self):
        entries = read_all(
            {
                25: fx.make_mft_record(
                    record_number=25,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="plain.bin", parent=5),
                        fx.attr_data_nonresident(
                            allocated=PLAIN_ALLOC, real=PLAIN_ALLOC - 100
                        ),
                    ],
                )
            }
        )
        self.assertEqual(entries[25].bytes, PLAIN_ALLOC)
        self.assertEqual(entries[25].logical_bytes, PLAIN_ALLOC - 100)

    def test_plain_file_with_an_alternate_stream(self):
        """普通文件的备用流照旧相加 —— 这条以前就对,别改坏。"""
        entries = read_all(
            {
                26: fx.make_mft_record(
                    record_number=26,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="tagged.bin", parent=5),
                        fx.attr_data_nonresident(
                            allocated=PLAIN_ALLOC, real=PLAIN_ALLOC
                        ),
                        fx.attr_data_nonresident(
                            allocated=ZONE_ALLOC, real=ZONE_ALLOC,
                            name="Zone.Identifier", attr_id=4,
                        ),
                    ],
                )
            }
        )
        self.assertEqual(entries[26].bytes, PLAIN_ALLOC + ZONE_ALLOC)

    def test_ordinary_fragmented_file_still_merges_from_extension(self):
        """未命名流在扩展记录里的普通大文件 —— _apply_pending 原本的用途。"""
        entries = read_all(
            {
                27: base_with_attr_list(27, "huge.iso"),
                28: extension(
                    28,
                    27,
                    [
                        fx.attr_data_nonresident(
                            allocated=PLAIN_ALLOC, real=PLAIN_ALLOC
                        )
                    ],
                ),
            }
        )
        self.assertEqual(entries[27].bytes, PLAIN_ALLOC)

    def test_legacy_ntfs_compressed_file_is_left_alone(self):
        """老式 NTFS 压缩(有压缩位)的 allocated 本来就是压缩后的,别再动它。"""
        entries = read_all(
            {
                29: fx.make_mft_record(
                    record_number=29,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="old.txt", parent=5),
                        fx.attr_data_nonresident(
                            allocated=PLAIN_ALLOC,
                            real=PLAIN_ALLOC * 4,
                            flags=0x0001,        # 压缩位
                        ),
                    ],
                )
            }
        )
        self.assertEqual(entries[29].bytes, PLAIN_ALLOC)


class NamedOnlyExtensionRecordTest(unittest.TestCase):
    """只有备用流、没有未命名流的扩展记录,不计入总量。

    这条看着像漏算,是刻意的,而且是量出来的。改 WOF 的时候顺手把
    _parse_record 的扩展记录条件放宽成 `alloc or logical or named_alloc`,
    理由听起来很正当:「只有 WofCompressedData 的扩展记录也该报,
    否则那些字节凭空消失」。放宽之后真盘实测总量从 +15.9% 涨到 +19.5%
    (tools/verify_wof_fix.py) —— 方向反了。

    被放进来的最大一条是 $UsnJrnl 的 $J 流,39.83 GiB,比当时剩下的整个
    差额(34.08G)还大(tools/probe_wof_shapes.py)。USN 变更日志是环形缓冲:
    旧区间早就释放了,allocated_size 报的是整个逻辑区间,真实占用只有活动
    窗口那几十 MB。按 allocated_size 把它算进来,比丢掉它错得更多。

    所以维持原样。要真算准得走运行列表、只数非稀疏的段 —— 那条规则还没在
    真盘上验证过,没验过的东西不进代码。这个用例的作用是:哪天有人又想放宽
    这个条件,先看见这段账。
    """

    def test_named_only_extension_is_not_counted(self):
        entries = read_all(
            {
                31: base_with_attr_list(31, "journal-ish"),
                32: extension(
                    32,
                    31,
                    [
                        fx.attr_data_nonresident(
                            allocated=SESSIONS_PHANTOM_ALLOC,
                            real=SESSIONS_PHANTOM_ALLOC,
                            name="$J",
                            attr_id=3,
                            flags=SPARSE_FLAG,
                        )
                    ],
                ),
            }
        )
        self.assertEqual(entries[31].bytes, 0)

    def test_an_unnamed_stream_in_the_extension_still_counts(self):
        """把上面那条的边界钉住:有未命名流的扩展记录照旧要算。

        只断言「只有备用流的不算」的话,把整个扩展记录合并逻辑删掉也能过。
        """
        entries = read_all(
            {
                33: base_with_attr_list(33, "big.bin"),
                34: extension(
                    34,
                    33,
                    [
                        fx.attr_data_nonresident(
                            allocated=PLAIN_ALLOC, real=PLAIN_ALLOC
                        ),
                        fx.attr_data_nonresident(
                            allocated=ZONE_ALLOC, real=ZONE_ALLOC,
                            name="Zone.Identifier", attr_id=4,
                        ),
                    ],
                ),
            }
        )
        self.assertEqual(entries[33].bytes, PLAIN_ALLOC + ZONE_ALLOC)


class WofStreamNameMatchingTest(unittest.TestCase):
    """流名匹配不区分大小写,但不能把别的名字误当成它。"""

    def _one(self, stream_name: str) -> mft.FileEntry:
        entries = read_all(
            {
                30: fx.make_mft_record(
                    record_number=30,
                    attributes=[
                        fx.attr_standard_information(),
                        fx.attr_file_name(name="x.dll", parent=5),
                        phantom_stream(KERNEL32_PHANTOM_ALLOC, KERNEL32_LOGICAL),
                        fx.attr_data_nonresident(
                            allocated=KERNEL32_WOF_ALLOC,
                            real=KERNEL32_WOF_ALLOC,
                            name=stream_name,
                            attr_id=3,
                        ),
                    ],
                )
            }
        )
        return entries[30]

    def test_lowercase_name_still_recognised(self):
        self.assertEqual(self._one("wofcompresseddata").bytes, KERNEL32_WOF_ALLOC)

    def test_a_different_stream_name_is_not_treated_as_wof(self):
        """名字只是相似的流不算 —— 这时未命名流是真的,要照加。"""
        entry = self._one("WofCompressedDataBackup")
        self.assertEqual(
            entry.bytes, KERNEL32_PHANTOM_ALLOC + KERNEL32_WOF_ALLOC
        )


if __name__ == "__main__":
    unittest.main()
