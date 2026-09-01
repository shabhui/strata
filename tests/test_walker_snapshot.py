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


class WalkerWorkersTest(unittest.TestCase):
    """多线程遍历必须和单线程给出同一份结果。

    走多线程是为了快(实测整块 C: 从 32.0 秒到 23.2 秒,1.38 倍;
    瓶颈是 I/O,os.scandir 会放开 GIL)。但快没有意义,如果数出来的东西不一样 ——
    所以这里比的是两种模式的输出集合完全相等,而不是「多线程也能跑」。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expected = make_tree(self.root)
        # 再挖深一点,让活分得开:一层的树看不出并发问题
        for i in range(6):
            d = self.root / f"br{i}" / "mid" / "leaf"
            d.mkdir(parents=True)
            (d / f"f{i}.bin").write_bytes(b"\0" * (i + 1) * KB)
            self.expected[f"br{i}\\mid\\leaf\\f{i}.bin"] = (i + 1) * KB

    def tearDown(self):
        self.tmp.cleanup()

    def _norm(self, entries):
        return sorted((e.path, e.is_dir, e.bytes) for e in entries)

    def test_same_entries_as_serial(self):
        serial, s1 = walker.walk_drive(str(self.root), workers=1)
        threaded, s2 = walker.walk_drive(str(self.root), workers=8)
        self.assertEqual(self._norm(serial), self._norm(threaded))
        self.assertEqual((s1.files, s1.dirs, s1.bytes_total),
                         (s2.files, s2.dirs, s2.bytes_total))

    def test_no_duplicates(self):
        """一个目录被两个线程各走一遍,大小就会翻倍 —— 这是并发最容易出的错。"""
        entries, _ = walker.walk_drive(str(self.root), workers=8)
        paths = [e.path for e in entries]
        dupes = {p for p in paths if paths.count(p) > 1}
        self.assertEqual(dupes, set(), f"这些路径被数了多次:{sorted(dupes)[:5]}")

    def test_all_files_found_under_concurrency(self):
        entries, stats = walker.walk_drive(str(self.root), workers=8)
        found = {e.path for e in entries if not e.is_dir}
        self.assertEqual(found, set(self.expected))
        self.assertEqual(stats.bytes_total, sum(self.expected.values()))

    def test_worker_count_is_clamped(self):
        """0 或负数不能让它一个目录都不走。"""
        for w in (0, -1):
            with self.subTest(workers=w):
                entries, stats = walker.walk_drive(str(self.root), workers=w)
                self.assertEqual(stats.files, len(self.expected))

    def test_progress_is_reported_and_monotonic(self):
        """进度必须只增不减 —— 多线程下如果各线程报自己的局部计数,就会来回跳。

        界面上那一行数字往回退,比不显示更让人以为出错了。
        """
        seen: list[int] = []
        walker.walk_drive(str(self.root), workers=8,
                          progress=seen.append, progress_every=1)
        self.assertTrue(seen, "一次回调都没有")
        self.assertEqual(seen, sorted(seen), f"进度往回退了:{seen}")

    def test_errors_counted_not_raised(self):
        """并发下权限错误也得只计数、不炸整次扫描。"""
        entries, stats = walker.walk_drive(str(self.root / "nope"), workers=8)
        self.assertEqual(entries, [])
        self.assertEqual(stats.errors, 1)


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
        self.assertEqual(
            reason, "按配置跳过 MFT,直接用目录遍历(见 config.PREFER_MFT)"
        )
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


class ReparseCountIncludesSkippedEntriesTest(unittest.TestCase):
    """联接点计数的口径:数的是「见到几个」,**包括被主循环跳过的那些**。

    这条口径以前一条测试都没有,而它不是显而易见的 —— 所以钉下来。
    上面 MftReparseSameWordingTest 那几条只走了「联接点被正常输出」这一条路。

    ---- 顺带记一次没做成的优化,免得下一个人再走一遍 ----

    这个计数是**第二遍全表扫**(`sum(1 for e in entries if e.is_reparse)`),
    112 万条走两遍,而主循环本来就在遍历同一个列表。看着是白捡的。
    **量下来只值 0.011 秒**(tools/bench_reparse_pass.py,同进程交错三轮,
    1.25x),占整次扫描 19.13 秒的 **0.1%**。所以没改。

    (改之前估的是 0.15 秒,错了 14 倍。那个估值是拿 cProfile 下的 0.86 秒
    除一个猜的 profiler 系数得来的 —— cProfile 对 property 调用的放大远超
    那个系数。**profiler 的绝对值不能拿来倒推真实耗时**,只能看占比。)

    净差额本来就小,因为 property 调用两边次数一样:老写法在第二遍调,合进去
    是在主循环里调。省掉的只是 112 万次 genexpr 迭代。

    而合进去之后,正确性从「一个表达式」变成「必须写在四处 continue 之前」——
    文件形态的元文件、根目录、父链断的目录、父链断的文件。写在后面数字就变小,
    不报错,只是警告里少几个,没人看得出来。为 0.1% 换一个靠语句位置维持的
    不变量,不划算。和 attribution_of 那次是同一个判断
    (见 tests/test_bucket_fast_path.py)。

    下面这六条就是当时为那个改动写的。改动撤了,它们留着 —— 口径本来就该有
    覆盖,而且真有人再去合的时候,这几条会当场咬住。实测:把计数放到四处
    continue 之后,六条里红五条。
    """

    ROOT = 5                              # mft.ROOT_RECORD
    DIR_LINK = A.FILE_ATTR_DIRECTORY | A.FILE_ATTR_REPARSE_POINT
    FILE_LINK = A.FILE_ATTR_ARCHIVE | A.FILE_ATTR_REPARSE_POINT

    def _root(self):
        return mft.FileEntry(record=self.ROOT, parent=self.ROOT, name=".", is_dir=True)

    def count_in(self, entries) -> int:
        """从警告里把数字抠出来。没有那句话就是 0。"""
        _out, _orphan, warnings = snapshot._mft_to_scan_entries(entries)
        for w in warnings:
            if "联接点" in w:
                return int(w.split(" ", 1)[0].replace(",", ""))
        return 0

    def test_metafile_reparse_is_counted(self):
        """记录号 < 16 的文件被排掉(是 $BadClus 那类),但它带的标志要算。"""
        entries = [
            self._root(),
            mft.FileEntry(
                record=9, parent=self.ROOT, name="$Secure", is_dir=False,
                bytes=8192, attributes=self.FILE_LINK, is_metafile=True,
            ),
        ]
        self.assertEqual(self.count_in(entries), 1)

    def test_root_itself_counted_if_flagged(self):
        """根目录不作为条目输出,但真带上标志也得数 —— 口径是「见到几个」。"""
        entries = [
            mft.FileEntry(
                record=self.ROOT, parent=self.ROOT, name=".", is_dir=True,
                attributes=self.DIR_LINK,
            ),
        ]
        self.assertEqual(self.count_in(entries), 1)

    def test_orphaned_directory_reparse_is_counted(self):
        """父链断了的联接点目录 —— 路径解不出来,但它确实在盘上。"""
        entries = [
            self._root(),
            mft.FileEntry(
                record=300, parent=99999, name="lost_link", is_dir=True,
                attributes=self.DIR_LINK,
            ),
        ]
        self.assertEqual(self.count_in(entries), 1)

    def test_orphaned_file_reparse_is_counted(self):
        entries = [
            self._root(),
            mft.FileEntry(
                record=301, parent=99999, name="lost.lnk", is_dir=False,
                bytes=1024, attributes=self.FILE_LINK,
            ),
        ]
        self.assertEqual(self.count_in(entries), 1)

    def test_all_four_skip_paths_at_once(self):
        """四条跳过的路各来一个,再加一个正常输出的 —— 一共五个。

        这条是总账。上面四条各自只有一个,数漏一个和数漏全部在数字上分不开;
        这里五个里如果只数到一个,说明计数被放在了所有 continue 的后面。
        """
        entries = [
            self._root(),
            mft.FileEntry(
                record=9, parent=self.ROOT, name="$Secure", is_dir=False,
                bytes=8192, attributes=self.FILE_LINK, is_metafile=True,
            ),
            mft.FileEntry(
                record=300, parent=99999, name="lost_link", is_dir=True,
                attributes=self.DIR_LINK,
            ),
            mft.FileEntry(
                record=301, parent=99999, name="lost.lnk", is_dir=False,
                bytes=1024, attributes=self.FILE_LINK,
            ),
            mft.FileEntry(
                record=400, parent=self.ROOT, name="good_link", is_dir=True,
                attributes=self.DIR_LINK,
            ),
        ]
        # 根目录那条不带标志,所以是 4 个;换成带标志的根就是 5 个
        self.assertEqual(self.count_in(entries), 4)
        entries[0] = mft.FileEntry(
            record=self.ROOT, parent=self.ROOT, name=".", is_dir=True,
            attributes=self.DIR_LINK,
        )
        self.assertEqual(self.count_in(entries), 5)

    def test_non_reparse_entries_never_counted(self):
        """反过来也要钉:普通条目不能被算进去。

        没这一条的话,把计数写成「每条都加一」也能让上面全绿。
        """
        entries = [
            self._root(),
            mft.FileEntry(record=100, parent=self.ROOT, name="a.bin",
                          is_dir=False, bytes=4096),
            mft.FileEntry(record=101, parent=self.ROOT, name="sub", is_dir=True),
            mft.FileEntry(record=9, parent=self.ROOT, name="$Secure",
                          is_dir=False, bytes=8192, is_metafile=True),
            mft.FileEntry(record=302, parent=99999, name="orphan.bin",
                          is_dir=False, bytes=512),
        ]
        self.assertEqual(self.count_in(entries), 0)


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
