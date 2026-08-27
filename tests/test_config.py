"""config 里的时间戳保护。

真机扫描第一次就死在这上面:磁盘上的时间戳不一定是合法值,
而 Windows 的 time.localtime() 碰到越界值会抛 OSError。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from strata import config


class SafeDayTest(unittest.TestCase):
    def test_normal_timestamp(self):
        ts = time.mktime(time.strptime("2026-08-20 12:00:00", "%Y-%m-%d %H:%M:%S"))
        self.assertEqual(config.safe_day(ts), "2026-08-20")

    def test_epoch_is_valid(self):
        self.assertEqual(config.safe_day(0), "1970-01-01")

    def test_none(self):
        self.assertIsNone(config.safe_day(None))

    def test_negative_rejected(self):
        # Windows 上负时间戳会抛 OSError,不能放过去
        self.assertIsNone(config.safe_day(-1))
        self.assertIsNone(config.safe_day(-11644473600.0))   # FILETIME 0 → 1601

    def test_far_future_rejected(self):
        self.assertIsNone(config.safe_day(config.TS_MAX + 1))
        self.assertIsNone(config.safe_day(1e18))

    def test_nan_rejected(self):
        self.assertIsNone(config.safe_day(float("nan")))

    def test_non_numeric_rejected(self):
        self.assertIsNone(config.safe_day("2026-08-20"))
        self.assertIsNone(config.safe_day(object()))

    def test_boundaries_inclusive(self):
        self.assertIsNotNone(config.safe_day(config.TS_MIN))
        self.assertIsNotNone(config.safe_day(config.TS_MAX))

    def test_never_raises(self):
        """这个函数的全部意义就是不抛异常 —— 扫描不能被一个坏文件带崩。"""
        for value in [None, 0, -1, 1, 1e300, -1e300, float("inf"), float("-inf"),
                      float("nan"), "x", b"x", object(), [], {}]:
            with self.subTest(value=repr(value)):
                try:
                    config.safe_day(value)
                except Exception as exc:      # noqa: BLE001
                    self.fail(f"safe_day({value!r}) 抛了 {type(exc).__name__}: {exc}")


class LegacyMigrationTest(unittest.TestCase):
    """改名(TimeClear -> Strata)之后老数据要能自己搬过来。

    快照是这个工具唯一不可再生的东西:回溯层可以重扫重建,实测层不行 ——
    它记的是「那一天硬盘长什么样」,过去了就没了。搬错一次就永久少一段历史,
    所以这几条都得钉住。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="strata-mig-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = pathlib.Path(self.tmp)
        self.old = self.root / "TimeClear"
        self.new = self.root / "Strata"

    def _seed_legacy(self, db=b"OLD-DB", log="老日志\n"):
        self.old.mkdir(parents=True, exist_ok=True)
        (self.old / "timeclear.db").write_bytes(db)
        (self.old / "timeclear.log").write_text(log, encoding="utf-8")

    def test_moves_db_and_log_and_renames_them(self):
        self._seed_legacy()
        config._migrate_legacy_dir(self.old, _mk(self.new))
        self.assertEqual((self.new / "strata.db").read_bytes(), b"OLD-DB")
        self.assertEqual((self.new / "strata.log").read_text(encoding="utf-8"), "老日志\n")

    def test_legacy_files_are_left_in_place(self):
        """不删老文件。万一搬错了,原件还得在。"""
        self._seed_legacy()
        config._migrate_legacy_dir(self.old, _mk(self.new))
        self.assertTrue((self.old / "timeclear.db").is_file(), "老库被删了")

    def test_leaves_a_note(self):
        self._seed_legacy()
        config._migrate_legacy_dir(self.old, _mk(self.new))
        note = (self.new / "MIGRATED.txt")
        self.assertTrue(note.is_file())
        self.assertIn("strata.db", note.read_text(encoding="utf-8"))

    def test_never_overwrites_existing_data(self):
        """新库已经有内容时绝不能被老库盖掉 —— 那才是真的丢数据。"""
        self._seed_legacy()
        _mk(self.new)
        (self.new / "strata.db").write_bytes(b"NEW-DB-ALREADY-HERE")
        config._migrate_legacy_dir(self.old, self.new)
        self.assertEqual((self.new / "strata.db").read_bytes(), b"NEW-DB-ALREADY-HERE")

    def test_no_legacy_dir_is_a_no_op(self):
        """全新安装:老目录压根不存在,不能因此报错。"""
        config._migrate_legacy_dir(self.old, _mk(self.new))
        self.assertFalse((self.new / "strata.db").exists())
        self.assertFalse((self.new / "MIGRATED.txt").exists())

    def test_empty_legacy_dir_leaves_no_note(self):
        """老目录在但是空的:没搬任何东西,就不要留一张说搬过的条子。"""
        self.old.mkdir(parents=True, exist_ok=True)
        config._migrate_legacy_dir(self.old, _mk(self.new))
        self.assertFalse((self.new / "MIGRATED.txt").exists())

    def test_same_dir_is_a_no_op(self):
        """老新同一个目录时直接返回,别把文件拷给自己。"""
        self._seed_legacy()
        config._migrate_legacy_dir(self.old, self.old)
        self.assertFalse((self.old / "MIGRATED.txt").exists())

    def test_unreadable_legacy_does_not_raise(self):
        """搬不动(占用/权限/磁盘满)时要静静放过 —— 空库也能跑,
        在这儿抛异常会把整个程序拦在启动之前。"""
        self._seed_legacy()
        with mock.patch("shutil.copy2", side_effect=OSError("被占用")):
            config._migrate_legacy_dir(self.old, _mk(self.new))   # 不抛就算过
        self.assertFalse((self.new / "strata.db").exists())

    def test_data_dir_only_migrates_on_first_run(self):
        """data_dir() 只在新目录还不存在时才去搬。

        每次启动都搬一遍的话,用户在新库里扫出来的东西会被老库反复盖掉。
        """
        self._seed_legacy()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            first = config.data_dir()
            self.assertEqual((first / "strata.db").read_bytes(), b"OLD-DB")
            # 模拟用户又扫了一次,新库内容变了
            (first / "strata.db").write_bytes(b"SCANNED-AFTER-MIGRATION")
            second = config.data_dir()
        self.assertEqual((second / "strata.db").read_bytes(), b"SCANNED-AFTER-MIGRATION",
                         "第二次启动又把老库盖回去了")


def _mk(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


if __name__ == "__main__":
    unittest.main()
