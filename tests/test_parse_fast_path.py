"""解析热路径上抄的两条近道,以及它们为什么不改变结果。

来路:扫一次 C: 现在 23.4 秒,其中 collect_entries 占 83.1%
(tools/prof_scan_stages.py),而 collect_entries 里面 read_entries 又占 89.8%
(tools/prof_collect_stages.py)。写库那条路已经压到 0.56 秒,再挤没意义 ——
剩下的时间全在「每条记录解析」这一件事上。合成记录上量出来的上限是
109,383 条/秒(tools/bench_parse_record.py),真盘上的记录更胖,只会更慢。

近道一:先看在用位,再做 fixup。

    本机 161 万条记录里只有 120 万在用,剩下 40 万条是空闲记录 —— 给它们
    还原 USA 纯属白干。

    这么排**可以证明**是安全的,不是「试了没出事」:记录头 48 字节
    (_REC_HEADER 是 "<4sHHQHHHHIIQHHI"),而 USA 替换动的是每个扇区最后
    两字节,第一个坑在 sector_size - 2。扇区最小 512,第一个坑就在 510,
    整个记录头都在它下面 —— fixup 前后读到的头是同一个头。

    从外面能看见的差别只有一处:空闲记录的 USA 是坏的时候,以前记一次
    fixup_failures,现在不记(压根没做)。这是对的 —— 空闲记录的 USA 坏不坏
    没人关心。下面把这个差别和「在用记录照样要报」一起钉住。

近道二:只给用得上的属性建 AttributeHeader。

    _parse_record 只认三种类型码(0x10 $STANDARD_INFORMATION、0x30 $FILE_NAME、
    0x80 $DATA),但 iter_attributes 给每条属性都建一个 16 字段的数据类,再按
    常驻/非常驻多解一次 —— 目录身上的 $INDEX_ROOT、$INDEX_ALLOCATION、$BITMAP
    全是白建。120 万条记录乘每条四五条属性,是五六百万次白建。

    所以 iter_attributes 多一个 wanted 参数。**默认不过滤**:探针工具要把每条
    属性都打出来(tools/probe_wof.py),tests/test_ntfs_parsing.py 也指着全量
    行为 —— 默认变了它们就错了。

近道三:时间戳存原始 FILETIME,取的时候才换算。

    cProfile 量下来,解析 149,600 条在用记录调了 filetime_to_unix **897,600 次**
    —— 每条 6 次:$STANDARD_INFORMATION 四个 + $FILE_NAME 两个。而 mft.py 只用
    得上两个:

      * accessed 和 mft_changed:全仓库(src/tests/tools)没有一处读它们
      * $FILE_NAME 的 created / modified:只在 $STANDARD_INFORMATION 缺失时兜底,
        而 $STANDARD_INFORMATION 是每条记录都有的

    6 次里 4 次白转。所以两个数据类都改成存原始 FILETIME,created 之类变成
    property,取的时候才算。**不删** accessed / mft_changed:它们确实在记录里,
    删了就再也问不出来,而现在这样留着不花钱。

    filetime_to_unix(0) 本来就返回 None,所以默认值 0 和以前的默认 None 是
    同一个意思 —— 语义没变。

近道四:fixup 校验不切片。

    apply_fixups 原来每个扇区做两次 bytes(buf[t:t+2]),1024 字节的记录有两个
    扇区,120 万条记录就是几百万个两字节对象。改成直接比整数。

    顺带补一处**测试的**空子:原来 FixupTest 三条用例都只动偏移 510,也就是
    第一个扇区。只查第一个扇区的写法能让那三条全绿。下面按第二个扇区也钉一条。

近道五:在用位读一个字节,别为空闲记录建整个记录头。

    近道一已经让空闲记录不做 fixup 了,但它还是要先建一个 13 字段的 RecordHeader
    才能看到在用位 —— 而 MFT_RECORD_IN_USE 是 flags 的**最低位**,flags 在偏移 22,
    读一个字节就够。本机 161 万条里 40 万条空闲,就是 40 万个白建的记录头。

    偏移 22 是硬编码的,所以下面拿 parse_record_header 的结果对着钉住它 ——
    记录头的字段顺序哪天变了,这里会响,而不是安静地把在用位读成别的东西。

近道六:fixup 不重读 usa_offset / usa_count。

    调用方(_parse_record)紧接着就解了记录头,那两个字段就在头里。apply_fixups
    再从 buf 里读一遍是白读。所以它多收一个可选的 header 参数;不给还是自己读,
    因为 $MFT 自己那条记录和探针工具是先 fixup 再解头的,顺序反过来。

这个文件钉的是「近道没改变结果」,不是速度。速度归 bench_parse_record.py。
"""

from __future__ import annotations

import dataclasses
import struct
import unittest
import unittest.mock

from strata.ntfs import attributes as A
from strata.ntfs import mft

from . import mft_fixtures as fx
from .test_mft_reader import RECORD_SIZE, FakeVolume, a_file, build_image

def read_stats(records: dict[int, bytes]) -> tuple[dict[int, mft.FileEntry], mft.MftStats]:
    """读一张合成 MFT,连统计一起给出来。"""
    image, boot = build_image(records)
    reader = mft.MftReader(FakeVolume(image, boot))
    entries = {e.record: e for e in reader.read_entries()}
    return entries, reader.stats


def break_usa(raw: bytes) -> bytes:
    """把第一个扇区尾巴上的 USN 改掉,让 fixup 校验失败。"""
    buf = bytearray(raw)
    struct.pack_into("<H", buf, 510, 0xBEEF)
    return bytes(buf)


def filler_attr(type_code: int, *, attr_id: int = 9, payload: bytes = b"\x00" * 8) -> bytes:
    """任意类型码的一条常驻属性,只为占位。

    走的是通用属性头的布局:公共 16 字节 + 常驻 8 字节 + 值。目录身上的
    $INDEX_ROOT / $BITMAP 就是这个形状,但这里不写死那两个类型码 —— 要钉的是
    「不认识的类型码一律按 length 跳过」,拿真类型码测会让人以为规则跟类型有关。
    """
    value_offset = 24
    length = value_offset + len(payload)
    length += (8 - length % 8) % 8
    buf = bytearray(length)
    struct.pack_into("<IIBBHHH", buf, 0, type_code, length, 0, 0, 0, 0, attr_id)
    struct.pack_into("<IH", buf, 16, len(payload), value_offset)
    buf[value_offset : value_offset + len(payload)] = payload
    return bytes(buf)


class FreeRecordsSkipFixupTest(unittest.TestCase):
    """空闲记录不该走 fixup —— 而且这件事从外面看得见。"""

    def setUp(self) -> None:
        self.records = {
            20: fx.make_mft_record(
                record_number=20,
                attributes=[
                    fx.attr_standard_information(),
                    fx.attr_file_name(name="dir", parent=5),
                ],
                flags=0x0003,
            ),
            21: a_file(21, "alive.bin", 20, 4096),
        }

    def test_free_record_with_broken_usa_is_not_a_fixup_failure(self):
        """空闲 + USA 坏了 = 一声不响丢掉,不算 fixup 失败。

        这一条是两条近道里唯一能从外面观察到的行为差别。在用位没有提前看的
        时候,这条记录会先 fixup 失败、记一笔,再被丢掉 —— 计数里多出一笔
        跟任何真问题都无关的噪声。
        """
        records = dict(self.records)
        records[22] = break_usa(a_file(22, "free.bin", 20, 1024))
        # 清掉在用位。flags 在偏移 22,和 fixup 的坑(510 起)不重叠
        buf = bytearray(records[22])
        struct.pack_into("<H", buf, 22, 0x0000)
        records[22] = bytes(buf)

        entries, stats = read_stats(records)
        self.assertNotIn(22, entries, "空闲记录不该出条目")
        self.assertEqual(
            stats.fixup_failures, 0,
            "空闲记录的坏 USA 被算成了 fixup 失败 —— 说明在用位是在 fixup 之后才看的",
        )
        self.assertIn(21, entries, "后面的记录还得照读")

    def test_in_use_record_with_broken_usa_still_reports(self):
        """在用 + USA 坏了 = 照样记一笔。近道不能把真的损坏也一起吞掉。"""
        records = dict(self.records)
        records[22] = break_usa(a_file(22, "corrupt.bin", 20, 1024))

        entries, stats = read_stats(records)
        self.assertNotIn(22, entries)
        self.assertEqual(stats.fixup_failures, 1, "在用记录的坏 USA 没被报出来")

    def test_free_record_still_counted_as_seen(self):
        """records_seen 的口径不变 —— 它数的是「有 FILE 标记的记录」。"""
        records = dict(self.records)
        buf = bytearray(a_file(22, "free.bin", 20, 1024))
        struct.pack_into("<H", buf, 22, 0x0000)
        records[22] = bytes(buf)

        _, stats = read_stats(records)
        self.assertEqual(stats.records_seen, 4, "记录 0($MFT)+20+21+22")
        self.assertEqual(stats.records_in_use, 3, "空闲那条不算在用")

    def test_header_reads_the_same_before_and_after_fixups(self):
        """整个记录头都在第一个 USA 坑下面 —— 这是近道一成立的全部理由。

        坑在 sector_size - 2 = 510,记录头 48 字节。这条断言不是在测我们的代码,
        是在测那个算术:哪天记录头变长到跨过 510(或者有人把扇区当成 32 字节),
        近道一就不成立了,这里会先响。
        """
        raw = bytearray(a_file(23, "x.bin", 20, 4096))
        before = A.parse_record_header(raw, 0)
        A.apply_fixups(raw, 0, RECORD_SIZE, 512)
        after = A.parse_record_header(raw, 0)
        self.assertEqual(
            dataclasses.astuple(before), dataclasses.astuple(after),
            "fixup 改动了记录头里的字节,先看在用位这条近道不成立",
        )
        self.assertLess(A._REC_HEADER.size, 512 - 2, "记录头跨到 USA 坑上去了")


class AttributeFilterTest(unittest.TestCase):
    """wanted 只是少建对象,产出必须和全量走一遍一模一样。"""

    def setUp(self) -> None:
        # 顺序故意打乱,并且在想要的类型中间夹不想要的
        self.attrs = [
            fx.attr_standard_information(),
            filler_attr(0x90, attr_id=5),            # $INDEX_ROOT 的形状
            fx.attr_file_name(name="thing", parent=5),
            filler_attr(0xB0, attr_id=6),            # $BITMAP 的形状
            fx.attr_data_nonresident(allocated=8192, real=8000),
            filler_attr(0x40, attr_id=7),            # $OBJECT_ID 的形状
        ]
        self.raw = fx.make_mft_record(record_number=30, attributes=self.attrs)
        self.header = A.parse_record_header(self.raw, 0)

    def walk(self, wanted=None):
        return list(A.iter_attributes(self.raw, self.header, 0, RECORD_SIZE, wanted))

    def test_default_still_yields_everything(self):
        """不传 wanted 的行为一个字都不能变 —— 探针和别的测试指着它。"""
        codes = [a.type_code for a, _ in self.walk()]
        self.assertEqual(codes, [0x10, 0x90, 0x30, 0xB0, 0x80, 0x40])

    def test_wanted_yields_only_those(self):
        codes = [a.type_code for a, _ in self.walk({0x10, 0x30, 0x80})]
        self.assertEqual(codes, [0x10, 0x30, 0x80])

    def test_skipped_ones_do_not_shift_the_walk(self):
        """被跳过的属性得按 length 精确推进,不然后面的偏移全歪。

        这是这个改动最容易出错的地方:少加一次 length,下一条属性就从属性中间
        开始解,解出来是垃圾但**不一定报错** —— 可能只是 type_code 对不上被
        默默跳过,于是文件凭空少了大小。所以比的是偏移,不只是类型码。
        """
        full = {a.type_code: off for a, off in self.walk()}
        filtered = {a.type_code: off for a, off in self.walk({0x10, 0x30, 0x80})}
        for code in (0x10, 0x30, 0x80):
            self.assertEqual(filtered[code], full[code], f"类型 {code:#x} 的偏移不一致")

    def test_headers_are_identical_field_for_field(self):
        """产出的属性头必须逐字段相同,不只是类型码和偏移对上。"""
        full = {a.type_code: a for a, _ in self.walk()}
        filtered = {a.type_code: a for a, _ in self.walk({0x10, 0x30, 0x80})}
        for code in (0x10, 0x30, 0x80):
            self.assertEqual(
                dataclasses.astuple(full[code]), dataclasses.astuple(filtered[code]),
                f"类型 {code:#x} 的属性头不一致",
            )

    def test_empty_wanted_yields_nothing(self):
        """空集合是「什么都不要」,不是「不过滤」—— 别用真值判断。

        写成 `if wanted:` 的话空集合会静默变成全量。这里不是挑刺:调用方拿
        集合运算算出 wanted 的时候,算成空集是很自然的事。
        """
        self.assertEqual(self.walk(set()), [])

    def test_filter_still_stops_at_corruption(self):
        """长度不合理时照样停在原地,不能因为「反正不要这条」就跳过去。"""
        buf = bytearray(self.raw)
        header = A.parse_record_header(buf, 0)
        pos = header.attrs_offset
        # 第一条($STANDARD_INFORMATION)之后那条 filler 的 length 改成 0
        pos += A._U32.unpack_from(buf, pos + 4)[0]
        struct.pack_into("<I", buf, pos + 4, 0)
        got = list(A.iter_attributes(buf, header, 0, RECORD_SIZE, {0x30, 0x80}))
        self.assertEqual(got, [], "长度为 0 的属性没能让遍历停下,会死循环或读到垃圾")


class DirectoriesStillParseTest(unittest.TestCase):
    """目录身上被跳过的正是索引那几条属性 —— 它们的条目不能受影响。"""

    def test_directory_with_index_attrs(self):
        records = {
            20: fx.make_mft_record(
                record_number=20,
                attributes=[
                    fx.attr_standard_information(),
                    filler_attr(0x90, attr_id=5),
                    fx.attr_file_name(name="Windows", parent=5),
                    filler_attr(0xA0, attr_id=6),
                    filler_attr(0xB0, attr_id=7),
                ],
                flags=0x0003,
            ),
            21: a_file(21, "inside.bin", 20, 4096),
        }
        entries, _ = read_stats(records)
        self.assertIn(20, entries, "目录没解出来")
        self.assertEqual(entries[20].name, "Windows")
        self.assertTrue(entries[20].is_dir)
        self.assertEqual(entries[20].bytes, 0, "目录不该算数据占用")
        self.assertEqual(entries[21].bytes, 4096, "目录里的文件大小变了")


class TimestampsAreLazyTest(unittest.TestCase):
    """解析的时候一次都不换算,取的时候才算 —— 而且算出来的还是原来那个数。"""

    SI_TIMES = dict(
        created=1_600_000_000.0, modified=1_650_000_000.0,
        mft_changed=1_660_000_000.0, accessed=1_670_000_000.0,
    )
    FN_TIMES = dict(created=1_500_000_000.0, modified=1_550_000_000.0)

    def parsed(self):
        raw = fx.make_mft_record(
            record_number=40,
            attributes=[
                fx.attr_standard_information(**self.SI_TIMES),
                fx.attr_file_name(name="t.bin", parent=5, **self.FN_TIMES),
            ],
        )
        buf = bytearray(raw)
        A.apply_fixups(buf, 0, RECORD_SIZE, 512)
        header = A.parse_record_header(buf, 0)
        std = fn = None
        for attr, off in A.iter_attributes(buf, header, 0, RECORD_SIZE):
            if attr.type_code == A.ATTR_STANDARD_INFORMATION:
                std = A.parse_standard_information(buf, attr, off)
            elif attr.type_code == A.ATTR_FILE_NAME:
                fn = A.parse_file_name(buf, attr, off)
        self.assertIsNotNone(std)
        self.assertIsNotNone(fn)
        return std, fn

    def test_parsing_converts_no_filetime_at_all(self):
        """六次换算里有四次是白干的,所以一次都别在解析时做。"""
        with unittest.mock.patch.object(
            A, "filetime_to_unix", wraps=A.filetime_to_unix
        ) as spy:
            self.parsed()
            self.assertEqual(
                spy.call_count, 0,
                f"解析时换算了 {spy.call_count} 次时间 —— 应该等到取的时候再算",
            )

    def test_reading_converts_just_that_one(self):
        std, _ = self.parsed()
        with unittest.mock.patch.object(
            A, "filetime_to_unix", wraps=A.filetime_to_unix
        ) as spy:
            std.created
            self.assertEqual(spy.call_count, 1, "取一个时间换算了不止一次")

    def test_standard_information_values_unchanged(self):
        std, _ = self.parsed()
        for field, want in self.SI_TIMES.items():
            self.assertAlmostEqual(getattr(std, field), want, places=3, msg=field)

    def test_file_name_values_unchanged(self):
        _, fn = self.parsed()
        for field, want in self.FN_TIMES.items():
            self.assertAlmostEqual(getattr(fn, field), want, places=3, msg=field)

    def test_attributes_field_still_plain(self):
        """attributes 不是时间,别顺手也包成 property。"""
        std, fn = self.parsed()
        self.assertEqual(std.attributes, 0x20)
        self.assertEqual(fn.attributes, 0x20)

    def test_zero_filetime_still_means_none(self):
        """默认 0 和以前的默认 None 得是同一个意思。

        mft.py 那两行靠的是 `or` 兜底:`(std.created if std else None) or
        best_name.created`。要是 0 换算出 0.0 而不是 None,`or` 照样会走兜底
        (0.0 是假值),但 FileEntry 上就会留下 1601 年 —— 所以这条得钉住。
        """
        fn = A.FileNameInfo(parent=5, name="x", namespace=3,
                            allocated_hint=0, real_hint=0, attributes=0)
        self.assertIsNone(fn.created)
        self.assertIsNone(fn.modified)

    def test_end_to_end_entry_timestamps(self):
        """整条路走下来,FileEntry 上的时间还是对的。"""
        records = {
            20: fx.make_mft_record(
                record_number=20,
                attributes=[fx.attr_standard_information(),
                            fx.attr_file_name(name="d", parent=5)],
                flags=0x0003,
            ),
            21: a_file(21, "t.bin", 20, 4096),
        }
        entries, _ = read_stats(records)
        self.assertAlmostEqual(entries[21].created, 1_700_000_000.0, places=3)
        self.assertAlmostEqual(entries[21].modified, 1_700_500_000.0, places=3)


class FixupChecksEverySectorTest(unittest.TestCase):
    """USA 校验得查每个扇区,不能只查第一个。

    补的是**测试的**空子:原来 FixupTest 三条用例都只动偏移 510(第一个扇区),
    所以「只查第一个扇区」的写法能让它们全绿。1024 字节的记录有两个扇区。
    """

    def record(self) -> bytearray:
        return bytearray(fx.make_mft_record(
            record_number=41, attributes=[fx.attr_file_name(name="a.txt")]
        ))

    def test_corrupt_second_sector_is_caught(self):
        buf = self.record()
        struct.pack_into("<H", buf, 1022, 0xDEAD)
        with self.assertRaises(A.FixupError):
            A.apply_fixups(buf, 0, RECORD_SIZE, 512)

    def test_every_sector_tail_gets_restored(self):
        buf = self.record()
        for tail in (510, 1022):
            self.assertEqual(struct.unpack_from("<H", buf, tail)[0], 0x1234,
                             f"偏移 {tail} 上本该是 USN")
        A.apply_fixups(buf, 0, RECORD_SIZE, 512)
        for tail in (510, 1022):
            self.assertEqual(struct.unpack_from("<H", buf, tail)[0], 0,
                             f"偏移 {tail} 没还原")

    def test_offset_records_are_checked_too(self):
        """记录不在偏移 0 时,查的也得是那条记录自己的扇区。"""
        buf = bytearray(RECORD_SIZE * 2)
        buf[RECORD_SIZE:] = self.record()
        struct.pack_into("<H", buf, RECORD_SIZE + 1022, 0xDEAD)
        with self.assertRaises(A.FixupError):
            A.apply_fixups(buf, RECORD_SIZE, RECORD_SIZE, 512)


class FlagsBytePrecheckTest(unittest.TestCase):
    """偏移 22 的最低位就是在用位 —— 这条断言是那个硬编码偏移的全部依据。"""

    CASES = ((0x0000, False), (0x0001, True), (0x0002, False), (0x0003, True))

    def test_the_byte_agrees_with_parse_record_header(self):
        for flags, want in self.CASES:
            raw = bytearray(fx.make_mft_record(
                record_number=44, attributes=[fx.attr_file_name(name="a")], flags=flags
            ))
            header = A.parse_record_header(raw, 0)
            self.assertIs(header.in_use, want, f"flags={flags:#06x}")
            byte_says = bool(raw[A.REC_FLAGS_OFFSET] & A.MFT_RECORD_IN_USE)
            self.assertIs(
                byte_says, header.in_use,
                f"flags={flags:#06x} 时偏移 {A.REC_FLAGS_OFFSET} 那个字节和记录头不一致",
            )

    def test_it_works_for_records_not_at_offset_zero(self):
        """记录在块里的第 n 条时,偏移要跟着记录走。"""
        raw = fx.make_mft_record(
            record_number=45, attributes=[fx.attr_file_name(name="a")], flags=0x0000
        )
        buf = bytearray(RECORD_SIZE) + bytearray(raw)
        header = A.parse_record_header(buf, RECORD_SIZE)
        self.assertFalse(header.in_use)
        self.assertFalse(bool(buf[RECORD_SIZE + A.REC_FLAGS_OFFSET] & A.MFT_RECORD_IN_USE))

    def test_flags_offset_is_below_the_first_usa_slot(self):
        """顺带:这个字节也得在 fixup 坑之外,否则先读它就不成立(见近道一)。"""
        self.assertLess(A.REC_FLAGS_OFFSET + 1, 512 - 2)


class FixupTakesTheHeaderTest(unittest.TestCase):
    """给了记录头就别重读 usa_offset / usa_count,但结果必须一模一样。"""

    def record(self) -> bytes:
        return fx.make_mft_record(
            record_number=46, attributes=[fx.attr_file_name(name="a.txt")]
        )

    def test_same_bytes_with_and_without_header(self):
        raw = self.record()
        without = bytearray(raw)
        A.apply_fixups(without, 0, RECORD_SIZE, 512)
        with_hdr = bytearray(raw)
        A.apply_fixups(with_hdr, 0, RECORD_SIZE, 512,
                       A.parse_record_header(with_hdr, 0))
        self.assertEqual(bytes(without), bytes(with_hdr))

    def test_still_raises_when_usn_mismatches(self):
        buf = bytearray(self.record())
        struct.pack_into("<H", buf, 1022, 0xDEAD)
        header = A.parse_record_header(buf, 0)
        with self.assertRaises(A.FixupError):
            A.apply_fixups(buf, 0, RECORD_SIZE, 512, header)

    def test_still_raises_on_absurd_usa_count(self):
        buf = bytearray(self.record())
        struct.pack_into("<H", buf, 6, 900)
        header = A.parse_record_header(buf, 0)
        with self.assertRaises(A.FixupError):
            A.apply_fixups(buf, 0, RECORD_SIZE, 512, header)

    def test_no_usa_is_still_a_no_op(self):
        buf = bytearray(self.record())
        struct.pack_into("<H", buf, 6, 0)
        header = A.parse_record_header(buf, 0)
        before = bytes(buf)
        A.apply_fixups(buf, 0, RECORD_SIZE, 512, header)
        self.assertEqual(bytes(buf), before)


if __name__ == "__main__":
    unittest.main()
