import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.store import db  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def _snap(self, drive="C:", taken_at=None, method="mft"):
        snap = db.Snapshot(
            drive=drive,
            taken_at=taken_at if taken_at is not None else time.time(),
            method=method,
            total_bytes=2_000_000_000_000,
            free_bytes=30_000_000_000,
            used_bytes=1_970_000_000_000,
        )
        db.insert_snapshot(self.conn, snap)
        return snap

    def test_schema_version_recorded(self):
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        self.assertEqual(row["value"], db.SCHEMA_VERSION)

    def test_insert_and_read_snapshot(self):
        snap = self._snap()
        self.assertIsNotNone(snap.id)
        got = db.get_snapshot(self.conn, snap.id)
        self.assertEqual(got["drive"], "C:")
        self.assertEqual(got["method"], "mft")

    def test_latest_snapshot_picks_newest_complete(self):
        old = self._snap(taken_at=1000.0)
        new = self._snap(taken_at=2000.0)
        self.assertEqual(db.latest_snapshot(self.conn, "C:")["id"], new.id)
        # 未完成的快照不参与
        pending = db.Snapshot(
            drive="C:", taken_at=3000.0, method="mft",
            total_bytes=1, free_bytes=1, used_bytes=0, complete=False,
        )
        db.insert_snapshot(self.conn, pending)
        self.assertEqual(db.latest_snapshot(self.conn, "C:")["id"], new.id)
        self.assertNotEqual(old.id, new.id)

    def test_dirs_roundtrip_and_children(self):
        snap = self._snap()
        rows = [
            db.DirRow("Users", 1, 500, 10, 5, 2),
            db.DirRow("Windows", 1, 900, 20, 8, 3),
            db.DirRow("Users\\alice", 2, 400, 30, 4, 1),
            db.DirRow("Users\\public", 2, 100, 40, 1, 0),
            db.DirRow("Users\\alice\\Downloads", 3, 300, 300, 3, 0),
        ]
        db.insert_dirs(self.conn, snap.id, rows)

        top = db.children_of(self.conn, snap.id, "")
        self.assertEqual([r["path"] for r in top], ["Windows", "Users"])

        kids = db.children_of(self.conn, snap.id, "Users")
        self.assertEqual([r["path"] for r in kids], ["Users\\alice", "Users\\public"])

        deep = db.children_of(self.conn, snap.id, "Users\\alice")
        self.assertEqual([r["path"] for r in deep], ["Users\\alice\\Downloads"])

    def test_children_of_does_not_leak_across_siblings(self):
        """LIKE 前缀必须精确到目录分隔符,'Users' 不能匹配到 'UsersData'。"""
        snap = self._snap()
        db.insert_dirs(
            self.conn,
            snap.id,
            [
                db.DirRow("Users", 1, 10, 10, 1, 0),
                db.DirRow("UsersData", 1, 20, 20, 1, 0),
                db.DirRow("Users\\a", 2, 5, 5, 1, 0),
                db.DirRow("UsersData\\b", 2, 7, 7, 1, 0),
            ],
        )
        kids = db.children_of(self.conn, snap.id, "Users")
        self.assertEqual([r["path"] for r in kids], ["Users\\a"])

    def test_children_of_escapes_like_metacharacters(self):
        snap = self._snap()
        db.insert_dirs(
            self.conn,
            snap.id,
            [
                db.DirRow("50%_off", 1, 10, 10, 1, 0),
                db.DirRow("50%_off\\real", 2, 8, 8, 1, 0),
                db.DirRow("50XYoff\\fake", 2, 9, 9, 1, 0),
            ],
        )
        kids = db.children_of(self.conn, snap.id, "50%_off")
        self.assertEqual([r["path"] for r in kids], ["50%_off\\real"])

    def test_files_roundtrip(self):
        snap = self._snap()
        db.insert_files(
            self.conn,
            snap.id,
            [
                db.FileRow("Users\\a.iso", 5_000_000_000, 100.0, 90.0),
                db.FileRow("Users\\b.zip", 1_000_000_000, 200.0, 190.0),
            ],
        )
        rows = list(
            self.conn.execute(
                "SELECT * FROM files WHERE snapshot_id=? ORDER BY bytes DESC", (snap.id,)
            )
        )
        self.assertEqual([r["path"] for r in rows], ["Users\\a.iso", "Users\\b.zip"])

    def test_buckets_accumulate_on_conflict(self):
        snap = self._snap()
        db.insert_buckets(self.conn, snap.id, [db.BucketRow("2026-08-01", "Users", 100, 2)])
        db.insert_buckets(self.conn, snap.id, [db.BucketRow("2026-08-01", "Users", 50, 1)])
        row = self.conn.execute(
            "SELECT bytes, files FROM age_buckets WHERE snapshot_id=? AND day=? AND attribution=?",
            (snap.id, "2026-08-01", "Users"),
        ).fetchone()
        self.assertEqual((row["bytes"], row["files"]), (150, 3))

    def test_usn_events_dedupe_by_usn(self):
        db.insert_usn_events(
            self.conn,
            "C:",
            [
                db.UsnRow(usn=10, timestamp=1.0, reason=0x200, kind="delete", is_dir=False, name="x"),
                db.UsnRow(usn=10, timestamp=1.0, reason=0x200, kind="delete", is_dir=False, name="x"),
                db.UsnRow(usn=11, timestamp=2.0, reason=0x100, kind="create", is_dir=False, name="y"),
            ],
        )
        n = self.conn.execute("SELECT COUNT(*) c FROM usn_events").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_usn_cursor_roundtrip(self):
        self.assertIsNone(db.get_usn_cursor(self.conn, "C:"))
        db.set_usn_cursor(self.conn, "C:", 12345, 999)
        self.assertEqual(db.get_usn_cursor(self.conn, "C:"), (12345, 999))
        db.set_usn_cursor(self.conn, "C:", 12345, 1500)
        self.assertEqual(db.get_usn_cursor(self.conn, "C:"), (12345, 1500))

    def test_cascade_delete_removes_children(self):
        snap = self._snap()
        db.insert_dirs(self.conn, snap.id, [db.DirRow("Users", 1, 1, 1, 1, 0)])
        db.insert_files(self.conn, snap.id, [db.FileRow("Users\\a", 1)])
        db.insert_buckets(self.conn, snap.id, [db.BucketRow("2026-01-01", "Users", 1, 1)])
        self.conn.execute("DELETE FROM snapshots WHERE id=?", (snap.id,))
        for table in ("dirs", "files", "age_buckets"):
            n = self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            self.assertEqual(n, 0, table)

    def test_prune_snapshots_keeps_one_per_day_then_per_month(self):
        now = time.time()
        ids_today = [self._snap(taken_at=now - i * 600).id for i in range(4)]
        # 半年前的三个快照落在同一个月
        old_base = now - 200 * 86400
        ids_old = [self._snap(taken_at=old_base - i * 86400).id for i in range(3)]

        removed = db.prune_snapshots(self.conn, "C:", keep_daily=120)
        self.assertGreater(removed, 0)

        left = {int(r["id"]) for r in self.conn.execute("SELECT id FROM snapshots")}
        # 今天只留一个
        self.assertEqual(len(left & set(ids_today)), 1)
        # 老月份至多留一个(三个快照可能跨月,所以 <=2)
        self.assertLessEqual(len(left & set(ids_old)), 2)
        self.assertGreaterEqual(len(left & set(ids_old)), 1)

    def test_demote_snapshot_keeps_coarse_rows_only(self):
        from strata import config

        snap = self._snap()
        big = config.DEMOTE_DIR_MIN_BYTES + 1
        small = 1024
        db.insert_dirs(
            self.conn,
            snap.id,
            [
                db.DirRow("shallow", 1, small, small, 1, 0),          # 浅,留
                db.DirRow("a\\b\\c\\deep", 4, small, small, 1, 0),    # 深且小,删
                db.DirRow("a\\b\\c\\huge", 4, big, big, 1, 0),        # 深但大,留
            ],
        )
        db.insert_files(self.conn, snap.id, [db.FileRow("a\\x.iso", big)])

        dirs_gone, files_gone = db.demote_snapshot(self.conn, snap.id)
        self.assertEqual((dirs_gone, files_gone), (1, 1))

        left = {r["path"] for r in self.conn.execute(
            "SELECT path FROM dirs WHERE snapshot_id=?", (snap.id,))}
        self.assertEqual(left, {"shallow", "a\\b\\c\\huge"})
        self.assertIn("[已降级]", db.get_snapshot(self.conn, snap.id)["note"])

    def test_demote_previous_leaves_newest_intact(self):
        old = self._snap(taken_at=1000.0)
        new = self._snap(taken_at=2000.0)
        for sid in (old.id, new.id):
            db.insert_files(self.conn, sid, [db.FileRow("x.iso", 999)])

        n = db.demote_previous_snapshots(self.conn, "C:", keep_snapshot_id=new.id)
        self.assertEqual(n, 1)

        remaining = {
            r["snapshot_id"]
            for r in self.conn.execute("SELECT DISTINCT snapshot_id FROM files")
        }
        self.assertEqual(remaining, {new.id})

        # 再跑一次不应重复处理(已打降级标记)
        self.assertEqual(db.demote_previous_snapshots(self.conn, "C:", new.id), 0)

    def test_prune_old_buckets_keeps_only_target(self):
        a = self._snap(taken_at=1000.0)
        b = self._snap(taken_at=2000.0)
        db.insert_buckets(self.conn, a.id, [db.BucketRow("2026-01-01", "X", 1, 1)])
        db.insert_buckets(self.conn, b.id, [db.BucketRow("2026-01-01", "X", 1, 1)])
        db.prune_old_buckets(self.conn, "C:", keep_snapshot_id=b.id)
        rows = list(self.conn.execute("SELECT DISTINCT snapshot_id FROM age_buckets"))
        self.assertEqual([r["snapshot_id"] for r in rows], [b.id])


class ReclaimTest(unittest.TestCase):
    """删过行之后,库文件要真的还给操作系统。

    这个工具是用来省磁盘的,自己泄漏磁盘最说不过去。SQLite 删行只把页
    挂到 freelist,文件大小一分不降,而降级/清理每次扫描都在删行。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.conn = db.connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _fill_then_delete(self, rows: int = 4000) -> None:
        """撑大库再删空,制造一堆 freelist 页。"""
        snap = db.Snapshot(
            drive="C:", taken_at=1000.0, method="mft",
            total_bytes=1, free_bytes=1, used_bytes=0,
        )
        db.insert_snapshot(self.conn, snap)
        db.insert_dirs(self.conn, snap.id, [
            db.DirRow(f"Users\\alice\\{'deep' * 12}\\{i}", 3, i, i, 1, 0)
            for i in range(rows)
        ])
        self.conn.execute("DELETE FROM dirs")

    def test_maybe_vacuum_shrinks_file_after_deletes(self):
        self._fill_then_delete()
        before = self.path.stat().st_size
        # 阈值显式传,别让这条测试依赖生产默认值
        self.assertTrue(db.maybe_vacuum(self.conn, min_waste_bytes=0))
        self.assertLess(self.path.stat().st_size, before)

    def test_maybe_vacuum_declines_when_waste_is_small(self):
        # 占比很高（~94%）但绝对值小（~1.2 MB）：只有绝对下限能拦住它。
        # 用空库测是测不出来的，两道门都会拦。
        self._fill_then_delete(rows=4000)
        waste, total = db.wasted_bytes(self.conn)
        self.assertGreater(waste / total, 0.2, "测例前提变了")
        self.assertLess(waste, 8 * 1024 * 1024, "测例前提变了")
        self.assertFalse(db.maybe_vacuum(self.conn))

    def test_maybe_vacuum_respects_ratio_floor(self):
        # 浪费绝对值够大但占比很小时，也不值得动
        self._fill_then_delete()
        self.assertFalse(
            db.maybe_vacuum(self.conn, min_waste_bytes=0, min_waste_ratio=1.5)
        )


if __name__ == "__main__":
    unittest.main()
