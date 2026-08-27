"""对真实临时目录测试 scandir 扫描器与快照编排,不需要提权。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan import snapshot, walker  # noqa: E402
from strata.store import db  # noqa: E402

KB = 1024


def make_tree(root: Path) -> dict[str, int]:
    """建一棵已知大小的目录树,返回 相对路径 → 字节。"""
    layout = {
        "docs/report.txt": 3 * KB,
        "docs/draft/notes.md": 1 * KB,
        "media/video.bin": 40 * KB,
        "media/audio.bin": 12 * KB,
        "empty_dir/": 0,
        "top.log": 5 * KB,
    }
    written: dict[str, int] = {}
    for rel, size in layout.items():
        target = root / rel
        if rel.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\0" * size)
        written[rel.replace("/", "\\")] = size
    return written


class WalkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expected = make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_every_file(self):
        entries, stats = walker.walk_drive(str(self.root))
        found = {e.path for e in entries if not e.is_dir}
        self.assertEqual(found, set(self.expected))
        self.assertEqual(stats.files, len(self.expected))

    def test_sizes_match(self):
        entries, stats = walker.walk_drive(str(self.root))
        sizes = {e.path: e.bytes for e in entries if not e.is_dir}
        self.assertEqual(sizes, self.expected)
        self.assertEqual(stats.bytes_total, sum(self.expected.values()))

    def test_counts_directories(self):
        entries, stats = walker.walk_drive(str(self.root))
        dirs = {e.path for e in entries if e.is_dir}
        self.assertEqual(dirs, {"docs", "docs\\draft", "media", "empty_dir"})
        self.assertEqual(stats.dirs, 4)

    def test_paths_are_relative_with_backslashes(self):
        entries, _ = walker.walk_drive(str(self.root))
        for e in entries:
            self.assertFalse(e.path.startswith(str(self.root)), e.path)
            self.assertNotIn("/", e.path)

    def test_timestamps_present(self):
        entries, _ = walker.walk_drive(str(self.root))
        for e in entries:
            self.assertIsNotNone(e.modified, e.path)
            self.assertIsNotNone(e.created, e.path)

    def test_missing_root_reports_error_not_crash(self):
        entries, stats = walker.walk_drive(str(self.root / "nope"))
        self.assertEqual(entries, [])
        self.assertEqual(stats.errors, 1)

    def test_duration_recorded(self):
        _, stats = walker.walk_drive(str(self.root))
        self.assertGreaterEqual(stats.duration_ms, 0)


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expected = make_tree(self.root)
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_scan_directory_writes_complete_snapshot(self):
        result = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        self.assertIsNotNone(result.snapshot_id)
        self.assertEqual(result.file_count, len(self.expected))
        self.assertEqual(result.scanned_bytes, sum(self.expected.values()))

        row = db.get_snapshot(self.conn, result.snapshot_id)
        self.assertEqual(row["complete"], 1)
        self.assertEqual(row["drive"], "TEST")
        self.assertEqual(row["scanned_bytes"], sum(self.expected.values()))

    def test_scan_refreshes_planner_stats(self):
        """扫描完必须刷新统计信息,否则规划器会拿旧行数选错索引。

        库里有没有 sqlite_stat1,决定根视图是 0.07 ms 还是 115 ms。
        """
        before = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0]
        self.assertEqual(before, 0, "测例前提变了:新库不该已经有统计信息")

        snapshot.scan_directory(self.conn, str(self.root), label="TEST")

        self.assertGreater(
            self.conn.execute(
                "SELECT count(*) FROM sqlite_stat1 WHERE tbl='dirs'"
            ).fetchone()[0],
            0,
        )

    def test_scan_survives_broken_maintenance(self):
        """收尾失败不能把一次已经成功的扫描变成失败。"""
        import sqlite3 as _sqlite3

        def boom(_conn):
            raise _sqlite3.OperationalError("database is locked")

        original = db.refresh_stats
        db.refresh_stats = boom
        try:
            result = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        finally:
            db.refresh_stats = original

        self.assertIsNotNone(result.snapshot_id)
        self.assertEqual(db.get_snapshot(self.conn, result.snapshot_id)["complete"], 1)

    def test_dirs_table_has_correct_rollups(self):
        result = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        rows = {
            r["path"]: r
            for r in self.conn.execute(
                "SELECT * FROM dirs WHERE snapshot_id=?", (result.snapshot_id,)
            )
        }
        self.assertEqual(rows["docs"]["bytes"], 4 * KB)      # report + draft/notes
        self.assertEqual(rows["docs\\draft"]["bytes"], 1 * KB)
        self.assertEqual(rows["media"]["bytes"], 52 * KB)
        self.assertEqual(rows["empty_dir"]["bytes"], 0)

    def test_files_table_populated(self):
        result = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        rows = {
            r["path"]: r["bytes"]
            for r in self.conn.execute(
                "SELECT path, bytes FROM files WHERE snapshot_id=?", (result.snapshot_id,)
            )
        }
        self.assertEqual(rows, self.expected)

    def test_buckets_cover_all_bytes(self):
        result = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        total = self.conn.execute(
            "SELECT SUM(bytes) s FROM age_buckets WHERE snapshot_id=?", (result.snapshot_id,)
        ).fetchone()["s"]
        self.assertEqual(total, sum(self.expected.values()))

    def test_second_scan_reflects_added_file(self):
        first = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        (self.root / "media" / "new.bin").write_bytes(b"\0" * (20 * KB))
        second = snapshot.scan_directory(self.conn, str(self.root), label="TEST")

        self.assertEqual(second.scanned_bytes - first.scanned_bytes, 20 * KB)
        self.assertEqual(second.file_count - first.file_count, 1)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)

    def test_second_scan_reflects_deleted_file(self):
        first = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        (self.root / "media" / "video.bin").unlink()
        second = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        self.assertEqual(first.scanned_bytes - second.scanned_bytes, 40 * KB)

    def test_latest_snapshot_is_the_newest(self):
        snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        time.sleep(0.01)
        second = snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        self.assertEqual(db.latest_snapshot(self.conn, "TEST")["id"], second.snapshot_id)

    def test_failed_scan_leaves_no_partial_snapshot(self):
        """写入过程出错必须整体回滚,不能留下半截快照。"""
        original = db.insert_buckets

        def boom(*args, **kwargs):
            raise RuntimeError("模拟写入失败")

        db.insert_buckets = boom
        try:
            with self.assertRaises(RuntimeError):
                snapshot.scan_directory(self.conn, str(self.root), label="TEST")
        finally:
            db.insert_buckets = original

        count = self.conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"]
        self.assertEqual(count, 0)
        dirs = self.conn.execute("SELECT COUNT(*) c FROM dirs").fetchone()["c"]
        self.assertEqual(dirs, 0)


class CollectEntriesFallbackTest(unittest.TestCase):
    """非管理员时 collect_entries 必须优雅退化,而不是抛异常。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_tree(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_prefer_mft_false_uses_scandir(self):
        entries, method, warnings, reason = snapshot.collect_entries(
            str(self.root), prefer_mft=False
        )
        self.assertEqual(method, "scandir")
        self.assertGreater(len(entries), 0)
        self.assertEqual(reason, "调用方指定跳过 MFT")
        self.assertTrue(any("硬链接" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
