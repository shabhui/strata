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


class DayTimestampTest(unittest.TestCase):
    """safe_day 的逆向:'YYYY-MM-DD' 还原成当地时间戳。

    原来两处调用点直接用 time.strptime,它是纯 Python 实现,每次都要查 locale
    再跑一次正则 —— 336 个日期 1.43 ms,换成切片取数字是 0.19 ms。更要紧的是
    strptime 配 mktime 会抛 OverflowError,而 UTC+8 上 safe_day(0.0) 正好产出
    '1970-01-01',当地午夜是 epoch 之前,mktime 表示不了,真把接口打崩过。
    """

    def test_matches_strptime_on_valid_dates(self):
        """合法日期上必须和旧实现逐个一致,不然是在悄悄改数据。"""
        checked = 0
        for year in (1971, 1999, 2024, 2026, 2099):
            for month in range(1, 13):
                for day in (1, 15, 28):
                    text = f"{year:04d}-{month:02d}-{day:02d}"
                    for hour in (0, 12):
                        stamp = f"{text} {hour:02d}:00:00"
                        want = time.mktime(
                            time.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                        )
                        with self.subTest(day=text, hour=hour):
                            self.assertEqual(config.day_timestamp(text, hour), want)
                        checked += 1
        self.assertGreater(checked, 300, "测例前提变了")

    def test_hour_offsets_from_midnight(self):
        base = config.day_timestamp("2026-08-20", 0)
        self.assertEqual(config.day_timestamp("2026-08-20", 12), base + 12 * 3600)

    def test_round_trips_with_safe_day(self):
        """safe_day 产出的每个字符串都必须能被还原回同一天。"""
        for ts in (1e9, 1.5e9, 1.7e9, 2e9, 4e9):
            day = config.safe_day(ts)
            back = config.day_timestamp(day, 12)
            self.assertIsNotNone(back, day)
            self.assertEqual(config.safe_day(back), day)

    def test_malformed_returns_none(self):
        for text in ("", "2026", "2026-08", "2026-13-01", "2026-02-30",
                     "2026-00-01", "2026-01-00", "2026-01-32", "2026/01/01",
                     "abcd-ef-gh", "2026-08-28x", " 2026-08-28", "2026-8-1"):
            with self.subTest(text=text):
                self.assertIsNone(config.day_timestamp(text))

    def test_rejects_what_int_would_tolerate(self):
        """int() 认的东西不一定是合法日期,长度和分隔符查过了还不够。

        int('+1') 是 1,int(' 999') 是 999 —— 少了纯数字这道校验,'2026-+1-01'
        会被悄悄当成 1 月 1 日。宁可返回 None,不能猜。
        """
        for text in ("2026-+1-01", "2026-01-+1", "2026- 1-01", "2026-01- 1",
                     "+999-01-01", " 999-01-01", "-999-01-01", "2026-01-1 "):
            with self.subTest(text=text):
                self.assertIsNone(config.day_timestamp(text))

    def test_unrepresentable_day_returns_none_not_overflow(self):
        """超出 mktime 表示范围的日期要返回 None,不能抛 OverflowError。

        这正是打崩 usn_daily_summary 的那条路径。
        """
        self.assertIsNone(config.day_timestamp("1601-01-01"))

    def test_never_raises(self):
        for value in ("", "x", None, 0, 1.5, b"2026-08-28", object(), [], {},
                      "9999-99-99", "1601-01-01", "1970-01-01"):
            with self.subTest(value=repr(value)):
                try:
                    config.day_timestamp(value)
                except Exception as exc:      # noqa: BLE001
                    self.fail(
                        f"day_timestamp({value!r}) 抛了 {type(exc).__name__}: {exc}"
                    )

    def test_repeated_calls_agree(self):
        # 这个函数带缓存,缓存不能改变结果
        first = config.day_timestamp("2026-08-20", 12)
        for _ in range(3):
            self.assertEqual(config.day_timestamp("2026-08-20", 12), first)


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
