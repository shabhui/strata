"""把已经入库的坏时间戳一次性洗掉。

## 为什么光修写入侧不够

`tree._newer` 挡住的是**以后**扫出来的。库里已经有三个快照带着坏值:

    快照2 / 快照3 / 快照9   Program Files (x86)\\...\\Internet Download Manager
    快照9  211402.3 MB  2030-09-16  (根)     ← 快照 9 就是 C: 当前最新的那个

也就是说界面现在打开就是错的,而这三个快照要在库里留 120 天。不洗的话得
等用户重扫一遍 C:(半分钟以上)才能看到对的数。

## 上界取每个快照自己的 taken_at,不是「现在」

扫描记下的是「那一刻硬盘长什么样」,所以**任何文件的写入时间都不可能晚于
这次扫描本身**。用 taken_at + 容差当界,比用 now 更紧也更对:一个 2026 年
的坏值放到 2026 年的快照里该挡掉,而拿今天当界就会放过它。

## 只跑一次

洗完往 meta 里记一个标记。每次启动都扫一遍 160k 行没意义 —— 写入侧已经堵住,
之后不会再有新的坏值,那种检查是「永远只会通过的检查」。
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from strata import config
from strata.store import db

IDM_2030 = 1915769744.0                     # 真机上那个值
PACKAGE_CACHE_1608 = -11416537060.313498


class RepairFixture(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        # connect() 已经洗过一遍了 —— 那时库还是空的,于是标记以 0 记下,
        # 之后就再也不跑。真机上的顺序不一样(先有数据,后有这个函数),
        # 所以这里把标记清掉,让每条测试从「还没洗过」开始。
        #
        # 顺带说明一件事:新库上 connect() 就把标记写死了,这是对的 ——
        # 新库没有历史行要洗,再去扫 160k 行是白扫。
        self.conn.execute("DELETE FROM meta WHERE key = ?", (db.TS_REPAIR_KEY,))
        self.taken_at = time.time() - 3600
        self.snap_id = db.insert_snapshot(self.conn, db.Snapshot(
            drive="C:", taken_at=self.taken_at, method="scandir",
            total_bytes=1000, free_bytes=500, used_bytes=500,
            scanned_bytes=500, file_count=1, dir_count=1, duration_ms=1,
        ))

    def tearDown(self):
        self.conn.close()

    def add_dir(self, path, *, mtime=None, ctime=None, bytes_=1024):
        db.insert_dirs(self.conn, self.snap_id, [db.DirRow(
            path=path, depth=path.count("\\") + 1 if path else 0,
            bytes=bytes_, own_bytes=bytes_, files=1, dirs=0,
            newest_mtime=mtime, newest_ctime=ctime,
        )])

    def add_file(self, path, *, mtime=None, ctime=None, bytes_=1024):
        db.insert_files(self.conn, self.snap_id, [db.FileRow(
            path=path, bytes=bytes_, mtime=mtime, ctime=ctime,
        )])

    def dir_row(self, path):
        return self.conn.execute(
            "SELECT newest_mtime m, newest_ctime c FROM dirs "
            "WHERE snapshot_id = ? AND path = ?", (self.snap_id, path)).fetchone()


class RepairNullsUntrustworthy(RepairFixture):
    def test_future_value_becomes_null(self):
        """真机上那个 2030 —— 洗成 NULL,也就是「不知道」。"""
        self.add_dir("Program Files (x86)", ctime=IDM_2030, mtime=self.taken_at - 60)
        n = db.repair_timestamps(self.conn)
        row = self.dir_row("Program Files (x86)")
        self.assertIsNone(row["c"])
        self.assertAlmostEqual(row["m"], self.taken_at - 60, places=3)
        self.assertGreaterEqual(n, 1)

    def test_negative_value_becomes_null(self):
        self.add_dir("ProgramData\\Package Cache\\{x}", ctime=PACKAGE_CACHE_1608)
        db.repair_timestamps(self.conn)
        self.assertIsNone(self.dir_row("ProgramData\\Package Cache\\{x}")["c"])

    def test_root_row_is_repaired_too(self):
        """真机上被染的正是盘根那一行,path 是空串。

        别的地方好几处查询都写着 depth >= 1 把根排掉,照抄过来就会漏掉
        唯一真正出问题的那一行。
        """
        self.add_dir("", ctime=IDM_2030)
        db.repair_timestamps(self.conn)
        self.assertIsNone(self.dir_row("")["c"])

    def test_files_table_is_repaired(self):
        self.add_file("Program Files (x86)\\IDM\\bad.dat", ctime=IDM_2030,
                      mtime=self.taken_at - 60)
        db.repair_timestamps(self.conn)
        row = self.conn.execute(
            "SELECT mtime m, ctime c FROM files WHERE snapshot_id = ?",
            (self.snap_id,)).fetchone()
        self.assertIsNone(row["c"])
        self.assertAlmostEqual(row["m"], self.taken_at - 60, places=3)

    def test_good_values_survive(self):
        good_m, good_c = self.taken_at - 86400, self.taken_at - 172800
        self.add_dir("Windows", mtime=good_m, ctime=good_c)
        self.add_file("Windows\\x.dll", mtime=good_m, ctime=good_c)
        db.repair_timestamps(self.conn)
        row = self.dir_row("Windows")
        self.assertAlmostEqual(row["m"], good_m, places=3)
        self.assertAlmostEqual(row["c"], good_c, places=3)

    def test_mild_skew_within_tolerance_survives(self):
        """比 taken_at 早一点点(容差内)的留着 —— 跨机器拷贝的正常产物。"""
        skewed = self.taken_at + 3600
        self.add_dir("Copied", ctime=skewed)
        db.repair_timestamps(self.conn)
        self.assertAlmostEqual(self.dir_row("Copied")["c"], skewed, places=3)

    def test_bound_is_per_snapshot_not_now(self):
        """上界跟着各自的快照走。

        同一个值放进两个快照:老快照里它在未来(该洗),新快照里它在过去
        (该留)。用「现在」当统一上界的话两个都留下来,这条就红。
        """
        old_taken = time.time() - 90 * 86400
        old_id = db.insert_snapshot(self.conn, db.Snapshot(
            drive="D:", taken_at=old_taken, method="scandir",
            total_bytes=1, free_bytes=1, used_bytes=0, scanned_bytes=0,
            file_count=0, dir_count=0, duration_ms=1,
        ))
        # 这个值在 90 天前的快照里是未来,在一小时前的快照里是过去
        between = time.time() - 10 * 86400
        db.insert_dirs(self.conn, old_id, [db.DirRow(
            path="X", depth=1, bytes=1, own_bytes=1, files=0, dirs=0,
            newest_ctime=between)])
        self.add_dir("X", ctime=between)

        db.repair_timestamps(self.conn)

        old = self.conn.execute(
            "SELECT newest_ctime c FROM dirs WHERE snapshot_id = ? AND path='X'",
            (old_id,)).fetchone()
        self.assertIsNone(old["c"], "90 天前的快照不该记着 10 天前才发生的写入")
        self.assertAlmostEqual(self.dir_row("X")["c"], between, places=3,
                               msg="一小时前的快照里这个值是正常的,不该洗掉")


class RepairRunsOnce(RepairFixture):
    def test_marker_is_written(self):
        self.add_dir("A", ctime=IDM_2030)
        db.repair_timestamps(self.conn)
        marker = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (db.TS_REPAIR_KEY,)).fetchone()
        self.assertIsNotNone(marker)

    def test_second_call_is_a_no_op(self):
        self.add_dir("A", ctime=IDM_2030)
        first = db.repair_timestamps(self.conn)
        second = db.repair_timestamps(self.conn)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(second, 0, "标记在就不该再扫一遍")

    def test_force_ignores_the_marker(self):
        self.add_dir("A", ctime=IDM_2030)
        db.repair_timestamps(self.conn)
        self.add_dir("B", ctime=IDM_2030)
        self.assertEqual(db.repair_timestamps(self.conn, force=True), 1)


class ConnectRepairsAutomatically(unittest.TestCase):
    """用真文件,不用 :memory: —— 要测的正是「关掉再打开」这件事。

    内存库一关就没了,两次 connect(":memory:") 是两个不相干的空库,
    那样只能验到「函数被调用过」,验不到「已经入库的坏值被洗掉了」。
    而后者才是这个功能存在的理由。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "strata.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_reopening_cleans_legacy_rows(self):
        taken_at = time.time() - 60
        conn = db.connect(self.path)
        snap_id = db.insert_snapshot(conn, db.Snapshot(
            drive="C:", taken_at=taken_at, method="scandir",
            total_bytes=1, free_bytes=1, used_bytes=0, scanned_bytes=0,
            file_count=0, dir_count=0, duration_ms=1))
        # 照真机的样子:盘根那一行带着 2030
        db.insert_dirs(conn, snap_id, [db.DirRow(
            path="", depth=0, bytes=1, own_bytes=1, files=0, dirs=0,
            newest_ctime=IDM_2030)])
        # 新库上 connect() 已经把标记写过一次(那时还没数据),清掉才能模拟
        # 真机的时间顺序:数据先在库里,洗数据的代码后来才有。
        conn.execute("DELETE FROM meta WHERE key = ?", (db.TS_REPAIR_KEY,))
        conn.close()

        conn2 = db.connect(self.path)
        try:
            row = conn2.execute(
                "SELECT newest_ctime c FROM dirs WHERE path = ''").fetchone()
            self.assertIsNone(
                row["c"], "重新开库没有洗掉盘根那个 2030 —— connect() 没接上")
        finally:
            conn2.close()

    def test_third_open_does_not_rescan(self):
        """标记留在文件里,下次开库不再扫。"""
        conn = db.connect(self.path)
        conn.close()
        conn2 = db.connect(self.path)
        try:
            marker = conn2.execute(
                "SELECT value FROM meta WHERE key = ?",
                (db.TS_REPAIR_KEY,)).fetchone()
            self.assertIsNotNone(marker)
        finally:
            conn2.close()

    def test_repair_failure_does_not_block_startup(self):
        """洗不动也得能开库。历史是这个工具唯一不可再生的东西,
        为了洗一个显示问题而拦住启动是本末倒置。"""
        conn = db.connect(self.path)
        conn.execute("DROP TABLE dirs")
        # 表没了,UPDATE 必然抛 —— 但不该冒出来
        self.assertEqual(db.repair_timestamps(conn, force=True), 0)
        conn.close()

    def test_repair_failure_does_not_block_startup(self):
        """洗不动也得能开库。历史是这个工具唯一不可再生的东西,
        为了洗一个显示问题而拦住启动是本末倒置。"""
        conn = db.connect(":memory:")
        conn.execute("DROP TABLE dirs")
        # 表没了,UPDATE 必然抛 —— 但不该冒出来
        self.assertEqual(db.repair_timestamps(conn, force=True), 0)
        conn.close()


class RepairMatchesTheWriteSideBound(unittest.TestCase):
    """洗数据和写数据必须用同一个界,不然洗完再扫一遍又不一致。"""

    def test_same_ceiling_helper(self):
        taken_at = time.time() - 3600
        ceiling = config.newest_ceiling(taken_at)
        self.assertEqual(ceiling, taken_at + config.FUTURE_TOLERANCE)
        # 写入侧:build_tree 用同一个 helper
        from strata.scan import tree
        self.assertIsNone(tree._newer(IDM_2030, None, ceiling))


if __name__ == "__main__":
    unittest.main()
