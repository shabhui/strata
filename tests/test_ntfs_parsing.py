import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.ntfs import attributes as attrs  # noqa: E402
from strata.ntfs import runlist as rl  # noqa: E402
from strata.ntfs.volume import NtfsError, parse_boot_sector  # noqa: E402

from . import mft_fixtures as fx  # noqa: E402


class BootSectorTest(unittest.TestCase):
    def test_parses_geometry(self):
        boot = parse_boot_sector(fx.make_boot_sector())
        self.assertEqual(boot.bytes_per_sector, 512)
        self.assertEqual(boot.sectors_per_cluster, 8)
        self.assertEqual(boot.bytes_per_cluster, 4096)
        self.assertEqual(boot.mft_cluster, 786_432)
        self.assertEqual(boot.mft_offset, 786_432 * 4096)
        self.assertEqual(boot.bytes_per_mft_record, 1024)

    def test_large_cluster_signed_encoding(self):
        """每簇扇区数 > 128 时用「负数表示 2 的幂」编码。"""
        boot = parse_boot_sector(fx.make_boot_sector(sectors_per_cluster=0xF1))
        self.assertEqual(boot.sectors_per_cluster, 1 << 15)

    def test_mft_record_size_in_clusters(self):
        """字段 <= 0x80 时单位是簇,不是字节。"""
        boot = parse_boot_sector(
            fx.make_boot_sector(clusters_per_mft_record=2, sectors_per_cluster=2)
        )
        self.assertEqual(boot.bytes_per_mft_record, 2 * 512 * 2)

    def test_rejects_non_ntfs(self):
        with self.assertRaises(NtfsError):
            parse_boot_sector(fx.make_boot_sector(oem=b"MSDOS5.0"))

    def test_rejects_short_buffer(self):
        with self.assertRaises(NtfsError):
            parse_boot_sector(b"\x00" * 100)

    def test_rejects_bad_sector_size(self):
        raw = bytearray(fx.make_boot_sector())
        struct.pack_into("<H", raw, 11, 513)
        with self.assertRaises(NtfsError):
            parse_boot_sector(bytes(raw))


class RunlistTest(unittest.TestCase):
    def test_single_run(self):
        # 头 0x21: 长度 1 字节, 偏移 2 字节
        runs = rl.decode_runlist(b"\x21\x10\x00\x01\x00")
        self.assertEqual(runs, [rl.Run(vcn=0, lcn=0x100, length=0x10)])

    def test_multiple_runs_accumulate_delta(self):
        data = (
            b"\x21\x10\x00\x01"   # 长 0x10, LCN += 0x100 → 0x100
            b"\x21\x20\x00\x01"   # 长 0x20, LCN += 0x100 → 0x200
            b"\x00"
        )
        runs = rl.decode_runlist(data)
        self.assertEqual(
            runs,
            [
                rl.Run(vcn=0, lcn=0x100, length=0x10),
                rl.Run(vcn=0x10, lcn=0x200, length=0x20),
            ],
        )

    def test_negative_delta_moves_backwards(self):
        data = b"\x21\x10\x00\x02" + b"\x21\x10\x00\xff" + b"\x00"
        runs = rl.decode_runlist(data)
        self.assertEqual(runs[0].lcn, 0x200)
        # 0xff00 作为有符号 16 位是 -256
        self.assertEqual(runs[1].lcn, 0x200 - 256)

    def test_sparse_run_has_no_lcn(self):
        data = b"\x01\x08" + b"\x21\x10\x00\x01" + b"\x00"
        runs = rl.decode_runlist(data)
        self.assertTrue(runs[0].sparse)
        self.assertIsNone(runs[0].lcn)
        self.assertEqual(runs[0].length, 8)
        # 稀疏段推进 VCN 但不影响 LCN 基准
        self.assertEqual(runs[1].vcn, 8)
        self.assertEqual(runs[1].lcn, 0x100)

    def test_total_clusters_excludes_sparse_by_default(self):
        runs = rl.decode_runlist(b"\x01\x08" + b"\x21\x10\x00\x01" + b"\x00")
        self.assertEqual(rl.total_clusters(runs), 0x10)
        self.assertEqual(rl.total_clusters(runs, include_sparse=True), 0x10 + 8)

    def test_truncated_data_raises(self):
        with self.assertRaises(ValueError):
            rl.decode_runlist(b"\x21\x10")

    def test_zero_length_field_raises(self):
        with self.assertRaises(ValueError):
            rl.decode_runlist(b"\x20\x00\x01")

    def test_negative_lcn_raises(self):
        with self.assertRaises(ValueError):
            rl.decode_runlist(b"\x11\x08\xff")

    def test_iter_extents_skips_sparse(self):
        runs = rl.decode_runlist(b"\x01\x08" + b"\x21\x10\x00\x01" + b"\x00")
        extents = list(rl.iter_extents(runs, 4096))
        self.assertEqual(extents, [(0x100 * 4096, 0x10 * 4096)])


class FixupTest(unittest.TestCase):
    def test_fixups_restore_original_bytes(self):
        raw = fx.make_mft_record(
            record_number=42,
            attributes=[fx.attr_file_name(name="a.txt")],
        )
        buf = bytearray(raw)
        # 替换过的位置此刻是 USN
        self.assertEqual(struct.unpack_from("<H", buf, 510)[0], 0x1234)
        attrs.apply_fixups(buf, 0, 1024)
        # 还原后应是原始值(本例中是 0 填充区)
        self.assertEqual(struct.unpack_from("<H", buf, 510)[0], 0)

    def test_mismatched_usn_raises(self):
        raw = bytearray(
            fx.make_mft_record(record_number=1, attributes=[fx.attr_file_name(name="a")])
        )
        struct.pack_into("<H", raw, 510, 0xDEAD)
        with self.assertRaises(attrs.FixupError):
            attrs.apply_fixups(raw, 0, 1024)

    def test_out_of_bounds_usa_raises(self):
        raw = bytearray(
            fx.make_mft_record(record_number=1, attributes=[fx.attr_file_name(name="a")])
        )
        struct.pack_into("<H", raw, 6, 900)  # 荒谬的 usa_count
        with self.assertRaises(attrs.FixupError):
            attrs.apply_fixups(raw, 0, 1024)


class RecordHeaderTest(unittest.TestCase):
    def _parsed(self, **kwargs):
        raw = bytearray(fx.make_mft_record(**kwargs))
        attrs.apply_fixups(raw, 0, kwargs.get("record_size", 1024))
        return raw, attrs.parse_record_header(raw)

    def test_header_fields(self):
        _, header = self._parsed(
            record_number=77, attributes=[fx.attr_file_name(name="x.bin")], hard_link_count=3
        )
        self.assertEqual(header.magic, b"FILE")
        self.assertEqual(header.record_number, 77)
        self.assertEqual(header.hard_link_count, 3)
        self.assertTrue(header.in_use)
        self.assertFalse(header.is_directory)
        self.assertFalse(header.is_extension)

    def test_directory_flag(self):
        _, header = self._parsed(
            record_number=5, attributes=[fx.attr_file_name(name="Users")], flags=0x0003
        )
        self.assertTrue(header.is_directory)

    def test_deleted_record_not_in_use(self):
        _, header = self._parsed(
            record_number=9, attributes=[fx.attr_file_name(name="gone")], flags=0x0000
        )
        self.assertFalse(header.in_use)

    def test_extension_record_detected(self):
        _, header = self._parsed(
            record_number=500,
            attributes=[fx.attr_data_nonresident(allocated=4096, real=4000)],
            base_reference=(1 << 48) | 123,
        )
        self.assertTrue(header.is_extension)
        self.assertEqual(header.base_record_number, 123)


class AttributeTest(unittest.TestCase):
    def _record(self, attributes, **kwargs):
        raw = bytearray(fx.make_mft_record(record_number=10, attributes=attributes, **kwargs))
        attrs.apply_fixups(raw, 0, 1024)
        header = attrs.parse_record_header(raw)
        return raw, header

    def test_iterates_all_attributes_in_order(self):
        raw, header = self._record(
            [
                fx.attr_standard_information(),
                fx.attr_file_name(name="report.pdf"),
                fx.attr_data_nonresident(allocated=8192, real=8000),
            ]
        )
        found = [a.type_code for a, _ in attrs.iter_attributes(raw, header, 0, 1024)]
        self.assertEqual(found, [0x10, 0x30, 0x80])

    def test_standard_information_timestamps(self):
        raw, header = self._record(
            [fx.attr_standard_information(created=1_600_000_000.0, modified=1_650_000_000.0)]
        )
        attr, off = next(iter(attrs.iter_attributes(raw, header, 0, 1024)))
        info = attrs.parse_standard_information(raw, attr, off)
        self.assertAlmostEqual(info.created, 1_600_000_000.0, places=3)
        self.assertAlmostEqual(info.modified, 1_650_000_000.0, places=3)

    def test_file_name_decodes_unicode_and_parent(self):
        raw, header = self._record([fx.attr_file_name(name="项目报告.docx", parent=1234)])
        attr, off = next(iter(attrs.iter_attributes(raw, header, 0, 1024)))
        info = attrs.parse_file_name(raw, attr, off)
        self.assertEqual(info.name, "项目报告.docx")
        self.assertEqual(info.parent, 1234)

    def test_namespace_ranking_prefers_win32(self):
        dos = attrs.FileNameInfo(parent=5, name="PROGRA~1", namespace=attrs.NAMESPACE_DOS,
                                 allocated_hint=0, real_hint=0, attributes=0)
        win32 = attrs.FileNameInfo(parent=5, name="Program Files", namespace=attrs.NAMESPACE_WIN32,
                                   allocated_hint=0, real_hint=0, attributes=0)
        both = attrs.FileNameInfo(parent=5, name="Windows", namespace=attrs.NAMESPACE_WIN32_DOS,
                                  allocated_hint=0, real_hint=0, attributes=0)
        self.assertLess(dos.rank, win32.rank)
        self.assertLess(win32.rank, both.rank)

    def test_nonresident_data_uses_allocated_size(self):
        raw, header = self._record(
            [fx.attr_data_nonresident(allocated=1_048_576, real=1_000_000)]
        )
        attr, off = next(
            a for a in attrs.iter_attributes(raw, header, 0, 1024) if a[0].type_code == 0x80
        )
        size = attrs.parse_data_size(raw, attr, off)
        self.assertEqual(size.allocated, 1_048_576)
        self.assertEqual(size.real, 1_000_000)
        self.assertFalse(size.resident)
        self.assertFalse(size.named)

    def test_resident_data_reports_logical_size(self):
        raw, header = self._record([fx.attr_data_resident(payload=b"x" * 300)])
        attr, off = next(
            a for a in attrs.iter_attributes(raw, header, 0, 1024) if a[0].type_code == 0x80
        )
        size = attrs.parse_data_size(raw, attr, off)
        self.assertTrue(size.resident)
        self.assertEqual(size.allocated, 300)
        self.assertEqual(size.real, 300)

    def test_named_data_stream_flagged(self):
        raw, header = self._record(
            [fx.attr_data_nonresident(allocated=4096, real=4000, name="Zone.Identifier")]
        )
        attr, off = next(
            a for a in attrs.iter_attributes(raw, header, 0, 1024) if a[0].type_code == 0x80
        )
        self.assertEqual(attrs.attribute_name(raw, attr, off), "Zone.Identifier")
        self.assertTrue(attrs.parse_data_size(raw, attr, off).named)

    def test_data_fragment_beyond_first_is_ignored(self):
        """lowest_vcn != 0 的片段里 allocated_size 无意义,必须跳过。"""
        raw, header = self._record(
            [fx.attr_data_nonresident(allocated=0, real=0, lowest_vcn=100, highest_vcn=200)]
        )
        attr, off = next(
            a for a in attrs.iter_attributes(raw, header, 0, 1024) if a[0].type_code == 0x80
        )
        self.assertIsNone(attrs.parse_data_size(raw, attr, off))

    def test_corrupt_attribute_length_stops_iteration_safely(self):
        raw, header = self._record(
            [fx.attr_standard_information(), fx.attr_file_name(name="a.txt")]
        )
        # 把第一个属性的长度改成 0,迭代必须停下而不是死循环
        struct.pack_into("<I", raw, header.attrs_offset + 4, 0)
        found = list(attrs.iter_attributes(raw, header, 0, 1024))
        self.assertEqual(found, [])

    def test_filetime_conversion_edges(self):
        self.assertIsNone(attrs.filetime_to_unix(0))
        self.assertIsNone(attrs.filetime_to_unix(1))  # 1601 年,超出合理范围
        self.assertIsNone(attrs.filetime_to_unix(0xFFFFFFFFFFFFFFFF))
        self.assertAlmostEqual(
            attrs.filetime_to_unix(fx.unix_to_filetime(1_700_000_000.0)),
            1_700_000_000.0,
            places=3,
        )


if __name__ == "__main__":
    unittest.main()
