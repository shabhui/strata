"""读不了变更日志的时候,得说得出为什么。

「这个盘没删过东西」和「这个盘我没看成」在界面上长得一模一样 —— 都是空的。
这两件事差别很大,而现在没法分辨:失败原因在 collect_usn 里被 except 捕住、
打一行日志就扔了,库里什么都不留。

三种失败,全都真实存在:

    JournalUnavailable   日志没开。NTFS 上 USN 日志是可以关的,而且不少机器
                         默认就是关的 —— 这是最常见的一种。
    AccessDenied         没提权。读 $Extend\\$UsnJrnl 要卷句柄。
    NtfsError / OSError   别的读取错误。

不提权那条路已经在界面上说清了(del.needAdmin)。剩下这几条没有 —— 提了权
但日志没开的话,面板整段藏掉,用户看到的跟「什么都没删过」一样。这跟之前修掉的
那个「不提权时整段藏掉」是同一类毛病:把「没看成」显示成「没有」。

存到单独一张表,不是往 usn_cursor 上加列:这个项目没有迁移机制,_ensure_schema
每次连库都重跑一遍 schema.sql,而 CREATE TABLE IF NOT EXISTS 对已存在的表直接
跳过 —— 加列在本机那个 92.8 MB 的老库上永远不会生效。新表会建。
"""

from __future__ import annotations

import sqlite3
import time
import unittest

from strata.scan import changes as changes_mod
from strata.store import db


class UsnStatusRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_no_status_recorded_yet(self) -> None:
        """从来没扫过的盘:没有记录,而不是编一个「可用」出来。"""
        self.assertIsNone(db.get_usn_status(self.conn, "Z:"))

    def test_records_a_failure_with_its_reason(self) -> None:
        db.set_usn_status(self.conn, "D:", available=False, reason="USN 日志没有启用")
        got = db.get_usn_status(self.conn, "D:")
        self.assertIsNotNone(got)
        self.assertFalse(got["available"])
        self.assertEqual(got["reason"], "USN 日志没有启用")
        self.assertAlmostEqual(got["checked_at"], time.time(), delta=30)

    def test_success_clears_the_old_reason(self) -> None:
        """这次读成了,上次的失败原因必须消失,不能一直挂着。"""
        db.set_usn_status(self.conn, "D:", available=False, reason="没权限")
        db.set_usn_status(self.conn, "D:", available=True, reason=None)
        got = db.get_usn_status(self.conn, "D:")
        self.assertTrue(got["available"])
        self.assertIsNone(got["reason"], "读成功了还挂着上次的失败原因")

    def test_each_drive_is_separate(self) -> None:
        """C: 读失败不能让 D: 也显示成失败。"""
        db.set_usn_status(self.conn, "C:", available=False, reason="没权限")
        db.set_usn_status(self.conn, "D:", available=True, reason=None)
        self.assertFalse(db.get_usn_status(self.conn, "C:")["available"])
        self.assertTrue(db.get_usn_status(self.conn, "D:")["available"])

    def test_drive_is_the_key_not_a_growing_log(self) -> None:
        """每个盘一行,反复扫不能攒出一堆历史。"""
        for i in range(5):
            db.set_usn_status(self.conn, "D:", available=False, reason=f"第 {i} 次")
        n = self.conn.execute("SELECT COUNT(*) n FROM usn_status").fetchone()["n"]
        self.assertEqual(n, 1)
        self.assertEqual(db.get_usn_status(self.conn, "D:")["reason"], "第 4 次")


class CoverageCarriesTheReason(unittest.TestCase):
    """usn_coverage 是界面判断「这一栏该显示什么」的唯一依据,原因得从这儿出。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_zero_events_with_a_recorded_failure_says_why(self) -> None:
        db.set_usn_status(self.conn, "D:", available=False, reason="USN 日志没有启用")
        cov = changes_mod.usn_coverage(self.conn, "D:")
        self.assertEqual(cov["events"], 0)
        self.assertFalse(cov["available"])
        self.assertEqual(cov["reason"], "USN 日志没有启用")

    def test_zero_events_after_a_clean_read_is_just_empty(self) -> None:
        """读成功了、确实一条没有:那就是真的空,不是故障。"""
        db.set_usn_status(self.conn, "D:", available=True, reason=None)
        cov = changes_mod.usn_coverage(self.conn, "D:")
        self.assertEqual(cov["events"], 0)
        self.assertTrue(cov["available"])
        self.assertIsNone(cov["reason"])

    def test_never_scanned_is_not_reported_as_broken(self) -> None:
        """没扫过 ≠ 坏了。没有记录的时候 available 得是 None(不知道),
        不能默认成 False —— 那会让一个没扫过的盘显示成故障。
        """
        cov = changes_mod.usn_coverage(self.conn, "Z:")
        self.assertEqual(cov["events"], 0)
        self.assertIsNone(cov["available"])
        self.assertIsNone(cov["reason"])

    def test_events_present_still_reports_availability(self) -> None:
        """有数据的时候这两个字段也得在,前端不用分情况取。"""
        db.insert_usn_events(self.conn, "D:", [
            db.UsnRow(usn=1, timestamp=time.time(), reason=0x80000200,
                      kind="delete", is_dir=False, name="a", path="d\\a"),
        ])
        db.set_usn_status(self.conn, "D:", available=True, reason=None)
        self.conn.commit()
        cov = changes_mod.usn_coverage(self.conn, "D:")
        self.assertEqual(cov["events"], 1)
        self.assertTrue(cov["available"])
        self.assertIn("reason", cov)


class CollectUsnRecordsWhatHappened(unittest.TestCase):
    """collect_usn 三条失败路径都要落库,成功那条要清掉。

    用假的 volume 层制造失败 —— 不去动真日志:真日志在这台机器上是好的,
    测不出失败路径,而「测不出来所以不测」就是这个项目最反对的那种检查。
    """

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def _run_with(self, exc: Exception, conn: sqlite3.Connection | None = None) -> None:
        """让 collect_usn 在打开日志的时候抛出指定异常。

        换掉的是 changes.py 自己引到的那个名字(usn_mod.UsnJournal),不是
        volume 层 —— 换在这儿才盖住 collect_usn 真正走的那条路。
        """
        from strata.ntfs import usn as usn_mod

        def boom(*a, **k):
            raise exc

        real = usn_mod.UsnJournal
        usn_mod.UsnJournal = boom
        try:
            changes_mod.collect_usn(conn if conn is not None else self.conn, "D:")
        finally:
            usn_mod.UsnJournal = real

    def test_journal_disabled_is_recorded(self) -> None:
        from strata.ntfs import usn as usn_mod

        self._run_with(usn_mod.JournalUnavailable("USN 日志没有启用"))
        got = db.get_usn_status(self.conn, "D:")
        self.assertIsNotNone(got, "日志没开这条路没落库 —— 界面上就说不出为什么是空的")
        self.assertFalse(got["available"])
        self.assertIn("启用", got["reason"])

    def test_access_denied_is_recorded(self) -> None:
        from strata.ntfs.volume import AccessDenied

        self._run_with(AccessDenied("需要管理员权限"))
        got = db.get_usn_status(self.conn, "D:")
        self.assertIsNotNone(got)
        self.assertFalse(got["available"])

    def test_os_error_is_recorded(self) -> None:
        self._run_with(OSError("设备没准备好"))
        got = db.get_usn_status(self.conn, "D:")
        self.assertIsNotNone(got)
        self.assertFalse(got["available"])
        self.assertIn("设备没准备好", got["reason"])

    def test_a_later_success_clears_the_stale_reason(self) -> None:
        """先失败、后成功:那条失败原因必须被这次成功清掉。

        不清的话,界面会对着一栏有数据的表挂一条横幅说「读不了这个盘的变更日志,
        原因:需要管理员权限」—— 提权重启之后第一次扫描就是这个场面,而且会一直
        挂着。这条盯的是 collect_usn 里成功那一路有没有写状态;db 层那条
        test_success_clears_the_old_reason 只证明了函数会清,不证明有人调它。
        """
        from strata.ntfs import usn as usn_mod
        from strata.ntfs.volume import AccessDenied

        self._run_with(AccessDenied("需要管理员权限"))
        self.assertFalse(db.get_usn_status(self.conn, "D:")["available"])

        # 换一个「读得通、但一条事件都没有」的假日志。空日志是合法结果:
        # 从上次游标之后确实可能什么都没发生。
        class OkJournal:
            journal_id = 7
            lowest_valid_usn = 0
            last_usn = 99

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def query(self):
                return self

            def read_all(self, *a, **k):
                return iter(())

        real = usn_mod.UsnJournal
        usn_mod.UsnJournal = lambda *a, **k: OkJournal()
        try:
            changes_mod.collect_usn(self.conn, "D:")
        finally:
            usn_mod.UsnJournal = real

        got = db.get_usn_status(self.conn, "D:")
        self.assertTrue(got["available"], "读成功了,状态还是失败")
        self.assertIsNone(
            got["reason"],
            "读成功了却还挂着上次的失败原因 —— 界面会对着有数据的表说读不了",
        )

    def test_the_reason_survives_a_reconnect(self) -> None:
        """原因得真写进库,不是挂在内存对象上 —— 下次开界面还得看得见。"""
        import pathlib
        import shutil
        import tempfile

        from strata.ntfs import usn as usn_mod

        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "s.db"
        conn = db.connect(path)
        try:
            self._run_with(usn_mod.JournalUnavailable("USN 日志没有启用"), conn)
        finally:
            conn.close()

        again = db.connect(path)
        self.addCleanup(again.close)
        got = db.get_usn_status(again, "D:")
        self.assertIsNotNone(got, "重开库之后原因没了 —— 说明只存在内存里")
        self.assertFalse(got["available"])


if __name__ == "__main__":
    unittest.main()
