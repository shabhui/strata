"""USN 日志解析测试。纯字节进、事件出,不碰真实卷。"""

from __future__ import annotations

import struct
import unittest

from strata.ntfs import usn
from tests.usn_fixtures import (
    journal_data,
    unix_to_filetime,
    usn_buffer,
    usn_record_v2,
    usn_record_v3,
    usn_record_v4,
)


class StructLayoutTest(unittest.TestCase):
    """结构体尺寸必须和 Windows 头文件一致,错一个字节后面全错。"""

    def test_sizes(self) -> None:
        self.assertEqual(usn._RECORD_V2.size, 0x3C)
        self.assertEqual(usn._READ_DATA_V0.size, 40)
        self.assertEqual(usn._JOURNAL_DATA.size, 56)
        self.assertEqual(usn._V2_HEADER_LEN, 0x3C)
        self.assertEqual(usn._V3_HEADER_LEN, 0x4C)


class JournalInfoTest(unittest.TestCase):
    def test_parses_v0(self) -> None:
        info = usn.parse_journal_info(journal_data())

        self.assertEqual(info.journal_id, 0x01D9_ABCD_1234_5678)
        self.assertEqual(info.first_usn, 4096)
        self.assertEqual(info.next_usn, 1_000_000)
        self.assertEqual(info.max_size, 32 * 1024 * 1024)
        self.assertEqual(info.allocation_delta, 4 * 1024 * 1024)

    def test_v1_and_v2_same_prefix(self) -> None:
        """新版本在后面追加字段,前 56 字节布局不变。"""
        base = usn.parse_journal_info(journal_data(version=0))
        for version in (1, 2):
            with self.subTest(version=version):
                info = usn.parse_journal_info(journal_data(version=version))
                self.assertEqual(info.journal_id, base.journal_id)
                self.assertEqual(info.next_usn, base.next_usn)

    def test_short_buffer_raises(self) -> None:
        with self.assertRaises(usn.JournalUnavailable):
            usn.parse_journal_info(b"\x00" * 20)


class ParseBufferTest(unittest.TestCase):
    def test_single_v2_record(self) -> None:
        buf = usn_buffer(9000, [usn_record_v2(usn=8888, name="setup.exe")])
        next_usn, events = usn.parse_usn_buffer(buf)

        self.assertEqual(next_usn, 9000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].usn, 8888)
        self.assertEqual(events[0].name, "setup.exe")
        self.assertEqual(events[0].kind, usn.KIND_CREATE)

    def test_multiple_records_in_order(self) -> None:
        buf = usn_buffer(
            9100,
            [
                usn_record_v2(usn=9001, name="a.bin"),
                usn_record_v2(usn=9002, name="b.bin"),
                usn_record_v2(usn=9003, name="c.bin"),
            ],
        )
        _, events = usn.parse_usn_buffer(buf)

        self.assertEqual([e.usn for e in events], [9001, 9002, 9003])
        self.assertEqual([e.name for e in events], ["a.bin", "b.bin", "c.bin"])

    def test_v3_record_takes_low_64_bits(self) -> None:
        buf = usn_buffer(
            5000, [usn_record_v3(usn=4999, name="模型.safetensors", file_ref_low=77)]
        )
        _, events = usn.parse_usn_buffer(buf)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].file_reference, 77)
        self.assertEqual(events[0].name, "模型.safetensors")
        self.assertEqual(events[0].usn, 4999)

    def test_v2_and_v3_mixed(self) -> None:
        buf = usn_buffer(
            6000,
            [
                usn_record_v2(usn=5001, name="old.dat"),
                usn_record_v3(usn=5002, name="new.dat"),
            ],
        )
        _, events = usn.parse_usn_buffer(buf)
        self.assertEqual([e.name for e in events], ["old.dat", "new.dat"])

    def test_v4_skipped_without_aborting(self) -> None:
        """认不出的版本跳过就好,不能把后面的记录一起丢掉。"""
        buf = usn_buffer(
            7000,
            [
                usn_record_v2(usn=6001, name="before.txt"),
                usn_record_v4(usn=6002),
                usn_record_v2(usn=6003, name="after.txt"),
            ],
        )
        _, events = usn.parse_usn_buffer(buf)
        self.assertEqual([e.name for e in events], ["before.txt", "after.txt"])

    def test_unicode_name_roundtrip(self) -> None:
        name = "下载\\安装包 v2.1 (最终).zip"
        buf = usn_buffer(100, [usn_record_v2(usn=99, name=name)])
        _, events = usn.parse_usn_buffer(buf)
        self.assertEqual(events[0].name, name)

    def test_sequence_number_stripped_from_reference(self) -> None:
        """文件引用高 16 位是序列号,不属于记录号。"""
        raw_ref = (0x1234 << 48) | 4242
        buf = usn_buffer(10, [usn_record_v2(usn=9, name="x", file_ref=raw_ref)])
        _, events = usn.parse_usn_buffer(buf)
        self.assertEqual(events[0].file_reference, 4242)

    def test_timestamp_converted(self) -> None:
        buf = usn_buffer(10, [usn_record_v2(usn=9, name="x", timestamp=1_700_000_000.0)])
        _, events = usn.parse_usn_buffer(buf)
        self.assertAlmostEqual(events[0].timestamp, 1_700_000_000.0, places=2)

    def test_directory_flag(self) -> None:
        buf = usn_buffer(
            10,
            [
                usn_record_v2(usn=8, name="folder", attributes=0x10),
                usn_record_v2(usn=9, name="file", attributes=0x80),
            ],
        )
        _, events = usn.parse_usn_buffer(buf)
        self.assertTrue(events[0].is_dir)
        self.assertFalse(events[1].is_dir)

    # ---- 截断与损坏 ----
    def test_empty_buffer(self) -> None:
        self.assertEqual(usn.parse_usn_buffer(b""), (0, []))
        self.assertEqual(usn.parse_usn_buffer(b"\x00" * 4), (0, []))

    def test_header_only_buffer(self) -> None:
        next_usn, events = usn.parse_usn_buffer(struct.pack("<q", 1234))
        self.assertEqual(next_usn, 1234)
        self.assertEqual(events, [])

    def test_zero_length_terminates(self) -> None:
        buf = usn_buffer(50, [usn_record_v2(usn=49, name="a")]) + b"\x00" * 64
        _, events = usn.parse_usn_buffer(buf)
        self.assertEqual(len(events), 1)

    def test_truncated_last_record_dropped_not_raised(self) -> None:
        """日志是滚动缓冲,尾部被切断是常态。前面解出来的要留住。"""
        good = usn_record_v2(usn=1, name="complete.bin")
        partial = usn_record_v2(usn=2, name="cut-off.bin")[:20]
        _, events = usn.parse_usn_buffer(usn_buffer(9, [good, partial]))

        self.assertEqual([e.name for e in events], ["complete.bin"])

    def test_record_length_shorter_than_header_skipped(self) -> None:
        bad = usn_record_v2(usn=1, name="x", pad_to=16)
        _, events = usn.parse_usn_buffer(usn_buffer(9, [bad]))
        self.assertEqual(events, [])

    def test_name_beyond_record_length_ignored(self) -> None:
        """名字长度写得比记录还长时,不能越界读到下一条记录里。"""
        rec = bytearray(usn_record_v2(usn=1, name="ok.txt"))
        struct.pack_into("<H", rec, 0x38, 4000)      # FileNameLength 撑爆
        _, events = usn.parse_usn_buffer(usn_buffer(9, [bytes(rec)]))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].name, "")

    def test_absurd_record_length_stops_parsing(self) -> None:
        rec = bytearray(usn_record_v2(usn=1, name="x"))
        struct.pack_into("<I", rec, 0, 1 << 20)
        _, events = usn.parse_usn_buffer(usn_buffer(9, [bytes(rec)]))
        self.assertEqual(events, [])


class ClassifyTest(unittest.TestCase):
    def test_delete_wins_over_write(self) -> None:
        """删除记录通常同时带 DATA_TRUNCATION,应当归为删除。"""
        reason = usn.USN_REASON_FILE_DELETE | usn.USN_REASON_DATA_TRUNCATION
        self.assertEqual(usn.classify_reason(reason), usn.KIND_DELETE)

    def test_create_wins_over_write(self) -> None:
        reason = usn.USN_REASON_FILE_CREATE | usn.USN_REASON_DATA_EXTEND
        self.assertEqual(usn.classify_reason(reason), usn.KIND_CREATE)

    def test_delete_wins_over_create(self) -> None:
        """建了又删的临时文件会带两个位,最终结果是不在盘上。"""
        reason = usn.USN_REASON_FILE_CREATE | usn.USN_REASON_FILE_DELETE
        self.assertEqual(usn.classify_reason(reason), usn.KIND_DELETE)

    def test_rename_pair(self) -> None:
        self.assertEqual(
            usn.classify_reason(usn.USN_REASON_RENAME_OLD_NAME), usn.KIND_RENAME_OLD
        )
        self.assertEqual(
            usn.classify_reason(usn.USN_REASON_RENAME_NEW_NAME), usn.KIND_RENAME_NEW
        )

    def test_write_kinds(self) -> None:
        for bit in (
            usn.USN_REASON_DATA_EXTEND,
            usn.USN_REASON_DATA_TRUNCATION,
            usn.USN_REASON_DATA_OVERWRITE,
            usn.USN_REASON_NAMED_DATA_EXTEND,
        ):
            with self.subTest(bit=hex(bit)):
                self.assertEqual(usn.classify_reason(bit), usn.KIND_WRITE)

    def test_other(self) -> None:
        self.assertEqual(
            usn.classify_reason(usn.USN_REASON_SECURITY_CHANGE), usn.KIND_OTHER
        )
        self.assertEqual(usn.classify_reason(0), usn.KIND_OTHER)

    def test_close_alone_is_other(self) -> None:
        """CLOSE 只是「操作结束了」,本身不代表空间变化。"""
        self.assertEqual(usn.classify_reason(usn.USN_REASON_CLOSE), usn.KIND_OTHER)

    def test_describe_reason_readable(self) -> None:
        text = usn.describe_reason(
            usn.USN_REASON_FILE_CREATE | usn.USN_REASON_DATA_EXTEND
        )
        self.assertIn("新建", text)
        self.assertIn("写入变大", text)

    def test_describe_unknown_falls_back_to_hex(self) -> None:
        self.assertEqual(usn.describe_reason(0x01000000), "0x01000000")


class ReasonMaskTest(unittest.TestCase):
    def test_mask_covers_space_relevant_reasons(self) -> None:
        for bit in (
            usn.USN_REASON_FILE_CREATE,
            usn.USN_REASON_FILE_DELETE,
            usn.USN_REASON_DATA_EXTEND,
            usn.USN_REASON_DATA_TRUNCATION,
            usn.USN_REASON_RENAME_OLD_NAME,
            usn.USN_REASON_RENAME_NEW_NAME,
        ):
            with self.subTest(bit=hex(bit)):
                self.assertTrue(usn.REASON_MASK_SPACE & bit)

    def test_mask_excludes_noise(self) -> None:
        """权限和属性变化跟占用无关,收进来只会淹没有用信息。"""
        for bit in (
            usn.USN_REASON_SECURITY_CHANGE,
            usn.USN_REASON_BASIC_INFO_CHANGE,
            usn.USN_REASON_EA_CHANGE,
            usn.USN_REASON_OBJECT_ID_CHANGE,
        ):
            with self.subTest(bit=hex(bit)):
                self.assertFalse(usn.REASON_MASK_SPACE & bit)


class FixtureSanityTest(unittest.TestCase):
    """夹具本身也要对,否则测的是我的误解。"""

    def test_v2_record_length_matches_declared(self) -> None:
        rec = usn_record_v2(usn=1, name="hello.txt")
        declared = struct.unpack_from("<I", rec, 0)[0]
        self.assertEqual(declared, len(rec))
        self.assertEqual(len(rec) % 8, 0)

    def test_v3_record_length_matches_declared(self) -> None:
        rec = usn_record_v3(usn=1, name="hello.txt")
        declared = struct.unpack_from("<I", rec, 0)[0]
        self.assertEqual(declared, len(rec))

    def test_filetime_conversion_roundtrip(self) -> None:
        from strata.ntfs.attributes import filetime_to_unix

        ts = 1_700_000_000.0
        self.assertAlmostEqual(filetime_to_unix(unix_to_filetime(ts)), ts, places=2)


if __name__ == "__main__":
    unittest.main()
