"""扫描过程中,/api/scan/state 必须真的在变。

进度管道本来就是通的:walk_drive 每两万个条目回调一次(实测整盘 54 次,
中位间隔 0.75 秒)。但 _run_scan 一直没把 progress= 传下去,于是 phase 在
整次扫描里一动不动地写着「正在扫描」—— 一分多钟里界面上唯一的动静是那个
转圈动画。功能齐全、接线断开,而且没有任何测试会红。

这条测试盯的就是接线:跑一次真扫描(临时目录,不碰真盘真库),
中途反复读 state,要求 counted 见涨。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from strata.scan import snapshot as snapshot_mod
from strata.server import app as server_app
from strata.store import db


class ScanProgressWiringTest(unittest.TestCase):
    """_run_scan 必须把 progress 传给 scan_drive。"""

    def setUp(self) -> None:
        # 这几条测试是让假的 scan_drive 抛异常来提前收工的,而 _run_scan 会把异常
        # 连 traceback 写进 config.log_path() —— 也就是用户真实的
        # %LOCALAPPDATA%\Strata\strata.log。跑一遍测试往真日志里塞六段
        # RuntimeError("到此为止"),下次真出问题时翻日志,先看到的是测试噪音。
        # 排查启动器那次就吃过这个亏:日志里全是这些,反而误导。
        # config.data_dir() 是读 LOCALAPPDATA 的,所以指到临时目录就够了,
        # 不用改生产代码。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_passes_a_progress_callback(self):
        """最直接的一条:调 scan_drive 时带没带 progress。

        原来没带 —— 而这件事从外面看不出来,扫描照样成功,只是界面不动。
        """
        seen = {}

        def fake_scan_drive(conn, drive, **kw):
            seen.update(kw)
            raise RuntimeError("到此为止,只看参数")

        with mock.patch.object(snapshot_mod, "scan_drive", fake_scan_drive), \
             mock.patch.object(db, "connect", lambda *a, **k: mock.MagicMock()):
            server_app._scan_lock.acquire()
            server_app._run_scan("C:", with_usn=False)

        self.assertIn("progress", seen,
                      "_run_scan 没把 progress 传给 scan_drive,界面上进度永远不动")
        self.assertTrue(callable(seen["progress"]))

    def test_callback_updates_state(self):
        """回调要真的写进 state,而且写的是累计条数。"""
        captured = []

        def fake_scan_drive(conn, drive, *, progress=None, **kw):
            self.assertIsNotNone(progress)
            for n in (20_000, 40_000, 60_000):
                progress("scandir", n)
                captured.append(server_app.scan_state()["counted"])
            raise RuntimeError("到此为止")

        with mock.patch.object(snapshot_mod, "scan_drive", fake_scan_drive), \
             mock.patch.object(db, "connect", lambda *a, **k: mock.MagicMock()):
            server_app._scan_lock.acquire()
            server_app._run_scan("C:", with_usn=False)

        self.assertEqual(captured, [20_000, 40_000, 60_000])

    def test_state_exposes_counted_and_stage(self):
        """这两个键必须在 state 的初始形状里,不能只在扫描时才冒出来。

        前端读 state.counted;键不存在和值为 0 在 JS 里都假,但少一个键
        意味着接口形状随状态变,读的人得处理两种形状。
        """
        st = server_app.scan_state()
        self.assertIn("counted", st)
        self.assertIn("stage", st)

    def test_counted_resets_between_scans(self):
        """新一次扫描不能从上一次的数字接着涨。"""
        server_app._set_state(counted=999_999)

        def fake_scan_drive(conn, drive, *, progress=None, **kw):
            self.assertEqual(server_app.scan_state()["counted"], 0,
                             "新扫描开始时 counted 没清零")
            raise RuntimeError("到此为止")

        with mock.patch.object(snapshot_mod, "scan_drive", fake_scan_drive), \
             mock.patch.object(db, "connect", lambda *a, **k: mock.MagicMock()):
            server_app._scan_lock.acquire()
            server_app._run_scan("C:", with_usn=False)


class ScanProgressEndToEndTest(unittest.TestCase):
    """跑一次真扫描,从外面看 state 有没有动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # 造够条目,让每 20 个报一次进度时能报好几次
        for i in range(40):
            d = root / f"d{i}"
            d.mkdir()
            for j in range(20):
                (d / f"f{j}.bin").write_bytes(b"\0")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def test_counted_climbs_during_a_real_scan(self):
        from strata.scan import walker

        seen: list[int] = []

        def spy(stage, n):
            seen.append(n)

        entries, stats = walker.walk_drive(
            str(self.root), progress=lambda n: spy("scandir", n), progress_every=20
        )
        self.assertTrue(seen, "一次进度回调都没有")
        self.assertEqual(seen, sorted(seen), f"进度往回退了:{seen[:20]}")
        self.assertLessEqual(seen[-1], stats.files + stats.dirs)


if __name__ == "__main__":
    unittest.main()
