"""对真实临时目录测试 scandir 扫描器与快照编排,不需要提权。"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs import mft  # noqa: E402
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


class FallbackReasonPersistedTest(unittest.TestCase):
    """退化原因要跟着快照留在库里,不能只在扫描那一刻显示。

    退回目录遍历意味着这次的数字口径变了(硬链接重复计数、算的是逻辑大小),
    这个前提在快照活着的整段时间里都成立。原来它只出现在扫描返回值里,
    刷一下页面就没了 —— 之后看这份数据的人不知道数字是怎么来的。
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_tree(self.root)

    def _note(self):
        """取最后一条快照的 note。

        走的是 scan_drive 而不是 scan_directory —— 只有前者会调 collect_entries,
        也就只有前者有退化原因可落。scan_drive 没有 label 参数,drive 列存的就是
        传进去的那个路径,所以按临时目录查。
        """
        return self.conn.execute(
            "SELECT note FROM snapshots WHERE drive = ? ORDER BY id DESC LIMIT 1",
            (str(self.root),),
        ).fetchone()["note"]

    def test_reason_lands_in_snapshot_note(self):
        snapshot.scan_drive(self.conn, str(self.root), prefer_mft=False)
        note = self._note()
        self.assertIn("跳过 MFT", note)

    def test_reason_comes_before_the_warnings(self):
        """原因排在最前面。后面那串是它的后果,先说因。"""
        snapshot.scan_drive(self.conn, str(self.root), prefer_mft=False)
        note = self._note()
        self.assertLess(note.index("跳过 MFT"), note.index("硬链接"))

    def test_no_reason_leaves_note_alone(self):
        """MFT 走通的时候不该凭空多出一段话。"""
        entries, _m, _w, _r = snapshot.collect_entries(str(self.root), prefer_mft=False)
        with mock.patch.object(
            snapshot, "collect_entries", return_value=(entries, "mft", [], None)
        ):
            snapshot.scan_drive(self.conn, str(self.root))
        self.assertIsNone(self._note())


def make_junction(link: Path, target: Path) -> bool:
    """建一个目录联接点。建不出来就返回 False,让用它的测试跳过。

    用 mklink /J 而不是 os.symlink:目录联接点普通用户就能建,符号链接要么提权
    要么开开发者模式 —— 这个测试文件的前提是"不需要提权"。
    """
    try:
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return False
    return done.returncode == 0 and os.path.isjunction(link)


class ReparseReportedTest(unittest.TestCase):
    """联接点被跳过这件事,得让看的人知道。

    背景是 D: 上真实踩到的:CharaStudio 那棵树里两个 23.35 GB 的联接点都指向
    已经数过的 Koikatu\\abdata。不跟进是对的(跟进要把同一批字节数三遍,盘会
    看起来大出 47 GB),但树图里那个目录显示 0 字节,资源管理器点进去是 23 GB
    —— 之前屏幕上没有任何一句话解释这个差,只能让人以为工具算不准。
    """

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sizes = make_tree(self.root)
        if not make_junction(self.root / "shortcut", self.root / "sub"):
            self.skipTest("这台机器建不了目录联接点")

    def test_walker_does_not_follow_it(self):
        """先确认前提:跳过了,而且计了数。不然下面测的都是空的。"""
        _entries, stats = walker.walk_drive(str(self.root))
        self.assertEqual(stats.skipped_reparse, 1)
        self.assertEqual(stats.bytes_total, sum(self.sizes.values()))

    def test_the_junction_itself_shows_zero(self):
        """联接点自己算 0 —— 这正是屏幕上那个让人困惑的 0,得有话解释它。"""
        entries, _stats = walker.walk_drive(str(self.root))
        link = next(e for e in entries if e.name == "shortcut")
        self.assertTrue(link.is_dir)
        self.assertEqual(link.bytes, 0)

    def test_it_lands_in_the_snapshot_note(self):
        """落进 note,而不是只在扫描返回值里晃一下。

        note 是常驻在基线数字下面那一行的,描述的是"这份数据是什么口径"。
        联接点这件事在快照活着的整段时间里都成立,所以归它。
        """
        snapshot.scan_drive(self.conn, str(self.root), prefer_mft=False)
        note = self.conn.execute(
            "SELECT note FROM snapshots WHERE drive = ? ORDER BY id DESC LIMIT 1",
            (str(self.root),),
        ).fetchone()["note"]
        self.assertIn("联接点", note)
        self.assertIn("1 个", note)

    def test_says_where_the_bytes_actually_went(self):
        """光说"跳过了"没用 —— 得说清那 23 GB 算在哪了,不然还是像丢了。"""
        _e, _m, warnings, _r = snapshot.collect_entries(
            str(self.root), prefer_mft=False
        )
        line = next(w for w in warnings if "联接点" in w)
        self.assertIn("0 字节", line)
        self.assertIn("目标路径", line)

    def test_counts_them_all(self):
        """两个联接点要说 2 个。写死"有联接点"的话这里看不出来。"""
        self.assertTrue(make_junction(self.root / "shortcut2", self.root / "sub"))
        _e, _m, warnings, _r = snapshot.collect_entries(
            str(self.root), prefer_mft=False
        )
        self.assertTrue(any("2 个联接点" in w for w in warnings))


class MftReparseSameWordingTest(unittest.TestCase):
    """MFT 那条路也要说同一句话。

    这里没法造真联接点来跑 MFT —— 直读卷要提权。但两条路给出的说明必须一字不差:
    看的人不知道自己走的是哪条,同一个 0 字节目录换个方法换套说法,只会更糊。
    所以直接喂 FileEntry,查两边过的是同一个措辞函数。
    """

    ROOT = 5      # mft.ROOT_RECORD,父链的终点

    def _entries(self, junctions: int):
        """一个根目录 + 若干联接点目录 + 一个普通文件。"""
        out = [
            mft.FileEntry(record=self.ROOT, parent=self.ROOT, name=".", is_dir=True),
            mft.FileEntry(
                record=100, parent=self.ROOT, name="real.bin", is_dir=False, bytes=4096
            ),
        ]
        for i in range(junctions):
            out.append(
                mft.FileEntry(
                    record=200 + i,
                    parent=self.ROOT,
                    name=f"link{i}",
                    is_dir=True,
                    attributes=A.FILE_ATTR_DIRECTORY | A.FILE_ATTR_REPARSE_POINT,
                )
            )
        return out

    def test_wording_matches_the_scandir_path(self):
        _out, _orphan, warnings = snapshot._mft_to_scan_entries(self._entries(2))
        self.assertEqual(
            [w for w in warnings if "联接点" in w],
            snapshot._reparse_warning(2),
        )

    def test_counts_only_the_reparse_ones(self):
        """普通文件和根目录不能被算进去。"""
        _out, _orphan, warnings = snapshot._mft_to_scan_entries(self._entries(3))
        self.assertTrue(any("3 个联接点" in w for w in warnings))

    def test_silent_when_there_are_none(self):
        _out, _orphan, warnings = snapshot._mft_to_scan_entries(self._entries(0))
        self.assertFalse([w for w in warnings if "联接点" in w])


class NoReparseNoLineTest(unittest.TestCase):
    """没有联接点的时候不能凭空多出一句话。

    note 那一行是给人看的,每多一句都在挤掉别的。没发生的事不该占位置。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_tree(self.root)

    def test_clean_tree_has_no_reparse_line(self):
        _e, _m, warnings, _r = snapshot.collect_entries(
            str(self.root), prefer_mft=False
        )
        self.assertFalse([w for w in warnings if "联接点" in w])


if __name__ == "__main__":
    unittest.main()
