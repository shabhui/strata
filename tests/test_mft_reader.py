"""用合成 MFT 镜像测试 MftReader,不需要真实磁盘或管理员权限。"""

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs import mft  # noqa: E402
from strata.ntfs.volume import NtfsError, parse_boot_sector  # noqa: E402

from . import mft_fixtures as fx  # noqa: E402

BYTES_PER_SECTOR = 512
SECTORS_PER_CLUSTER = 8
BYTES_PER_CLUSTER = BYTES_PER_SECTOR * SECTORS_PER_CLUSTER  # 4096
RECORD_SIZE = 1024
MFT_CLUSTER = 100
RECORDS_PER_CLUSTER = BYTES_PER_CLUSTER // RECORD_SIZE  # 4


class FakeVolume:
    """按偏移提供合成磁盘内容,接口与 Volume 兼容。"""

    def __init__(self, image: bytes, boot):
        self.image = image
        self.boot = boot
        self.reads = 0

    def read(self, offset: int, length: int) -> bytes:
        self.reads += 1
        if offset >= len(self.image):
            return b""
        return self.image[offset : offset + length]

    def read_into(self, offset: int, length: int, buf: bytearray) -> int:
        """填调用方的缓冲区,返回字节数。语义照真实 Volume.read_into。

        read_entries 走的是这条路(整趟复用一块缓冲区,见
        test_mft_buffer_reuse.py)。这里**不清空** buf 里读到长度之后的部分 ——
        真实实现也不清,调用方必须按返回值截断。清了的话「解析器有没有正确
        按 got 截断」就测不出来了。
        """
        self.reads += 1
        if offset >= len(self.image):
            return 0
        chunk = self.image[offset : offset + length]
        buf[: len(chunk)] = chunk
        return len(chunk)

    def read_clusters(self, lcn: int, count: int) -> bytes:
        return self.read(lcn * BYTES_PER_CLUSTER, count * BYTES_PER_CLUSTER)


def mft_self_record(*, record_clusters: int) -> bytes:
    """记录 0:$MFT 自身,$DATA 指向 MFT 所在的簇。"""
    # 运行:长度 record_clusters 簇,起始 LCN = MFT_CLUSTER
    runlist = bytes([0x21, record_clusters & 0xFF]) + struct.pack("<h", MFT_CLUSTER)
    return fx.make_mft_record(
        record_number=0,
        flags=0x0001,
        attributes=[
            fx.attr_standard_information(),
            fx.attr_file_name(name="$MFT", parent=5),
            fx.attr_data_nonresident(
                allocated=record_clusters * BYTES_PER_CLUSTER,
                real=record_clusters * BYTES_PER_CLUSTER,
                runlist=runlist,
            ),
        ],
    )


def build_image(records: dict[int, bytes], *, total_records: int = 32) -> tuple[bytes, object]:
    """把记录字典摊进一个磁盘镜像,返回 (镜像, 引导扇区)。"""
    record_clusters = (total_records * RECORD_SIZE + BYTES_PER_CLUSTER - 1) // BYTES_PER_CLUSTER
    records = dict(records)
    records[0] = mft_self_record(record_clusters=record_clusters)

    mft_offset = MFT_CLUSTER * BYTES_PER_CLUSTER
    image = bytearray(mft_offset + record_clusters * BYTES_PER_CLUSTER)

    boot_bytes = fx.make_boot_sector(
        bytes_per_sector=BYTES_PER_SECTOR,
        sectors_per_cluster=SECTORS_PER_CLUSTER,
        total_sectors=len(image) // BYTES_PER_SECTOR,
        mft_cluster=MFT_CLUSTER,
        clusters_per_mft_record=0xF6,  # 1024 字节
    )
    image[0:512] = boot_bytes

    for index, raw in records.items():
        start = mft_offset + index * RECORD_SIZE
        image[start : start + RECORD_SIZE] = raw

    return bytes(image), parse_boot_sector(boot_bytes)


def a_dir(record: int, name: str, parent: int) -> bytes:
    return fx.make_mft_record(
        record_number=record,
        flags=0x0003,  # IN_USE | DIRECTORY
        attributes=[
            fx.attr_standard_information(attributes=0x10),
            fx.attr_file_name(name=name, parent=parent, attributes=0x10),
        ],
    )


def a_file(
    record: int,
    name: str,
    parent: int,
    size: int,
    *,
    created: float = 1_700_000_000.0,
    modified: float = 1_700_500_000.0,
    hard_links: int = 1,
    extra_attrs: list[bytes] | None = None,
) -> bytes:
    attributes = [
        fx.attr_standard_information(created=created, modified=modified),
        fx.attr_file_name(name=name, parent=parent, created=created, modified=modified),
        fx.attr_data_nonresident(allocated=size, real=max(0, size - 100)),
    ]
    if extra_attrs:
        attributes.extend(extra_attrs)
    return fx.make_mft_record(
        record_number=record,
        flags=0x0001,
        attributes=attributes,
        hard_link_count=hard_links,
    )


class MftReaderTest(unittest.TestCase):
    def setUp(self):
        # 目录树:
        #   \Users              (16)
        #   \Users\alice        (17)
        #   \Users\alice\big.iso   (18)  8 MB
        #   \Users\alice\small.txt (19)  常驻 200 字节
        #   \Games              (20)
        #   \Games\game.pak     (21)  4 MB
        self.records = {
            5: a_dir(5, ".", 5),
            16: a_dir(16, "Users", 5),
            17: a_dir(17, "alice", 16),
            18: a_file(18, "big.iso", 17, 8 * 1024 * 1024),
            19: fx.make_mft_record(
                record_number=19,
                flags=0x0001,
                attributes=[
                    fx.attr_standard_information(),
                    fx.attr_file_name(name="small.txt", parent=17),
                    fx.attr_data_resident(payload=b"x" * 200),
                ],
            ),
            20: a_dir(20, "Games", 5),
            21: a_file(21, "game.pak", 20, 4 * 1024 * 1024),
        }
        image, boot = build_image(self.records)
        self.vol = FakeVolume(image, boot)
        self.reader = mft.MftReader(self.vol)

    def test_mft_runs_from_record_zero(self):
        runs = self.reader.mft_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].lcn, MFT_CLUSTER)

    def test_reads_all_entries(self):
        entries = self.reader.read_entries()
        by_name = {e.name: e for e in entries}
        self.assertIn("big.iso", by_name)
        self.assertIn("game.pak", by_name)
        self.assertIn("Users", by_name)
        self.assertTrue(by_name["Users"].is_dir)
        self.assertFalse(by_name["big.iso"].is_dir)

    def test_file_sizes_use_allocated(self):
        entries = self.reader.read_entries()
        by_name = {e.name: e for e in entries}
        self.assertEqual(by_name["big.iso"].bytes, 8 * 1024 * 1024)
        self.assertEqual(by_name["game.pak"].bytes, 4 * 1024 * 1024)

    def test_resident_file_reports_logical_size(self):
        entries = self.reader.read_entries()
        by_name = {e.name: e for e in entries}
        self.assertEqual(by_name["small.txt"].bytes, 200)

    def test_directories_report_zero_bytes(self):
        entries = self.reader.read_entries()
        for e in entries:
            if e.is_dir:
                self.assertEqual(e.bytes, 0, e.name)

    def test_stats_counts(self):
        self.reader.read_entries()
        stats = self.reader.stats
        self.assertEqual(stats.files, 4)   # big.iso, small.txt, game.pak, $MFT
        self.assertGreaterEqual(stats.dirs, 3)
        self.assertEqual(stats.fixup_failures, 0)
        self.assertEqual(stats.parse_failures, 0)

    def test_resolve_paths_builds_full_directory_paths(self):
        entries = self.reader.read_entries()
        paths, _ = mft.resolve_paths(entries)
        self.assertEqual(paths[16], "Users")
        self.assertEqual(paths[17], "Users\\alice")
        self.assertEqual(paths[20], "Games")
        self.assertEqual(paths[5], "")

    def test_timestamps_survive_roundtrip(self):
        entries = self.reader.read_entries()
        big = next(e for e in entries if e.name == "big.iso")
        self.assertAlmostEqual(big.created, 1_700_000_000.0, places=2)
        self.assertAlmostEqual(big.modified, 1_700_500_000.0, places=2)

    def test_deleted_records_skipped(self):
        records = dict(self.records)
        records[22] = a_file(22, "deleted.bin", 20, 999_999)
        # 清掉 IN_USE 位
        raw = bytearray(records[22])
        struct.pack_into("<H", raw, 0x16, 0x0000)
        # 重新做 fixup(改了字节,USA 需要一致)
        records[22] = bytes(raw)
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        names = {e.name for e in reader.read_entries()}
        self.assertNotIn("deleted.bin", names)

    def test_named_data_stream_adds_to_size(self):
        records = dict(self.records)
        records[23] = a_file(
            23,
            "downloaded.zip",
            20,
            1_000_000,
            extra_attrs=[
                fx.attr_data_nonresident(
                    allocated=4096, real=26, name="Zone.Identifier", attr_id=3
                )
            ],
        )
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        entry = next(e for e in reader.read_entries() if e.name == "downloaded.zip")
        self.assertEqual(entry.bytes, 1_000_000 + 4096)

    def test_extension_record_size_merged_into_base(self):
        """碎片化文件的 $DATA 在扩展记录里,大小必须合并回基记录。"""
        records = dict(self.records)
        # 基记录:有名字但没有 $DATA
        records[24] = fx.make_mft_record(
            record_number=24,
            flags=0x0001,
            attributes=[
                fx.attr_standard_information(),
                fx.attr_file_name(name="fragmented.vhdx", parent=20),
            ],
        )
        # 扩展记录:只有 $DATA,base_reference 指回 24
        records[25] = fx.make_mft_record(
            record_number=25,
            flags=0x0001,
            base_reference=(1 << 48) | 24,
            attributes=[
                fx.attr_data_nonresident(allocated=64 * 1024 * 1024, real=64 * 1024 * 1024)
            ],
        )
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        entries = reader.read_entries()
        entry = next(e for e in entries if e.name == "fragmented.vhdx")
        self.assertEqual(entry.bytes, 64 * 1024 * 1024)
        self.assertEqual(reader.stats.extension_records, 1)
        # 扩展记录本身不应产生独立条目
        self.assertEqual(sum(1 for e in entries if e.record == 25), 0)

    def test_hardlinked_file_counted_once(self):
        """同一条记录多个 $FILE_NAME:只产出一个条目,大小不翻倍。"""
        records = dict(self.records)
        records[26] = fx.make_mft_record(
            record_number=26,
            flags=0x0001,
            hard_link_count=2,
            attributes=[
                fx.attr_standard_information(),
                fx.attr_file_name(name="link-a.dll", parent=16, attr_id=1),
                fx.attr_file_name(name="link-b.dll", parent=20, attr_id=2),
                fx.attr_data_nonresident(allocated=2 * 1024 * 1024, real=2 * 1024 * 1024),
            ],
        )
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        entries = reader.read_entries()
        matches = [e for e in entries if e.record == 26]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].bytes, 2 * 1024 * 1024)
        self.assertEqual(matches[0].hard_links, 2)

    def test_dos_short_name_not_preferred(self):
        records = dict(self.records)
        records[27] = fx.make_mft_record(
            record_number=27,
            flags=0x0001,
            attributes=[
                fx.attr_standard_information(),
                fx.attr_file_name(name="PROGRA~1", parent=5, namespace=2, attr_id=1),
                fx.attr_file_name(name="Program Files", parent=5, namespace=1, attr_id=2),
                fx.attr_data_nonresident(allocated=1024, real=1024),
            ],
        )
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        entry = next(e for e in reader.read_entries() if e.record == 27)
        self.assertEqual(entry.name, "Program Files")

    def test_corrupt_record_does_not_abort_scan(self):
        records = dict(self.records)
        bad = bytearray(a_file(28, "corrupt.bin", 20, 1024))
        struct.pack_into("<H", bad, 510, 0xBEEF)  # 破坏 fixup
        records[28] = bytes(bad)
        records[29] = a_file(29, "after-corrupt.bin", 20, 2048)
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        names = {e.name for e in reader.read_entries()}
        self.assertIn("after-corrupt.bin", names)
        self.assertNotIn("corrupt.bin", names)
        self.assertEqual(reader.stats.fixup_failures, 1)

    def test_baad_record_skipped_without_error(self):
        records = dict(self.records)
        records[30] = fx.make_mft_record(
            record_number=30, attributes=[fx.attr_file_name(name="baad.bin")], magic=b"BAAD"
        )
        image, boot = build_image(records)
        reader = mft.MftReader(FakeVolume(image, boot))
        names = {e.name for e in reader.read_entries()}
        self.assertNotIn("baad.bin", names)

    def test_missing_mft_magic_raises(self):
        image, boot = build_image(self.records)
        broken = bytearray(image)
        mft_offset = MFT_CLUSTER * BYTES_PER_CLUSTER
        broken[mft_offset : mft_offset + 4] = b"XXXX"
        reader = mft.MftReader(FakeVolume(bytes(broken), boot))
        with self.assertRaises(NtfsError):
            reader.mft_runs()


class ResolvePathsTest(unittest.TestCase):
    def _dir(self, record, name, parent):
        return mft.FileEntry(record=record, parent=parent, name=name, is_dir=True)

    def test_orphaned_directory_dropped(self):
        entries = [self._dir(50, "Lost", 9999)]  # 父目录不存在
        paths, stats = mft.resolve_paths(entries)
        self.assertNotIn(50, paths)
        self.assertGreaterEqual(stats.orphaned, 1)

    def test_cycle_detected_and_dropped(self):
        entries = [self._dir(60, "A", 61), self._dir(61, "B", 60)]
        paths, stats = mft.resolve_paths(entries)
        self.assertNotIn(60, paths)
        self.assertNotIn(61, paths)
        self.assertGreaterEqual(stats.cycles, 1)

    def test_root_is_empty_string(self):
        entries = [self._dir(mft.ROOT_RECORD, ".", mft.ROOT_RECORD)]
        paths, _ = mft.resolve_paths(entries)
        self.assertEqual(paths[mft.ROOT_RECORD], "")

    def test_deep_chain_resolves(self):
        entries = [
            self._dir(mft.ROOT_RECORD, ".", mft.ROOT_RECORD),
            self._dir(100, "a", mft.ROOT_RECORD),
            self._dir(101, "b", 100),
            self._dir(102, "c", 101),
            self._dir(103, "d", 102),
        ]
        paths, _ = mft.resolve_paths(entries)
        self.assertEqual(paths[103], "a\\b\\c\\d")


if __name__ == "__main__":
    unittest.main()
