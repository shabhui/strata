"""分析层测试:时间轴的回溯/实测分界、快照对比、热点识别。

快照直接手写进内存库,不经过扫描 —— 这样可以精确控制日期和字节数,
把边界条件钉死。
"""

from __future__ import annotations

import time
import unittest
from datetime import date, datetime, timedelta

from strata.analysis import diff, hotspots, timeline
from strata.store import db


def day_str(offset_days: int, *, base: float | None = None) -> str:
    """相对今天的日期字符串。offset 为负表示过去。"""
    base = time.time() if base is None else base
    return (date.fromtimestamp(base) + timedelta(days=offset_days)).isoformat()


def ts_at(offset_days: int, hour: int = 12) -> float:
    """相对今天 offset 天、当地 hour 点的时间戳。"""
    d = date.today() + timedelta(days=offset_days)
    return datetime(d.year, d.month, d.day, hour, 0, 0).timestamp()


class AnalysisFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def add_snapshot(
        self,
        *,
        drive: str = "C:",
        taken_at: float,
        scanned: int,
        dirs: dict[str, int] | None = None,
        files: dict[str, int] | None = None,
        buckets: list[tuple[str, str, int, int]] | None = None,
        method: str = "mft",
        note: str | None = None,
        complete: bool = True,
        newest_ctime: float | None = None,
    ) -> int:
        snap = db.Snapshot(
            drive=drive,
            taken_at=taken_at,
            method=method,
            total_bytes=200 * 1024**3,
            free_bytes=30 * 1024**3,
            used_bytes=scanned,
            scanned_bytes=scanned,
            complete=complete,
            note=note,
        )
        sid = db.insert_snapshot(self.conn, snap)

        dir_rows = []
        for path, size in (dirs or {}).items():
            depth = 0 if not path else path.count("\\") + 1
            dir_rows.append(
                db.DirRow(
                    path=path,
                    depth=depth,
                    bytes=size,
                    own_bytes=size,
                    files=1,
                    dirs=0,
                    newest_mtime=taken_at,
                    newest_ctime=newest_ctime if newest_ctime is not None else taken_at,
                )
            )
        if dir_rows:
            db.insert_dirs(self.conn, sid, dir_rows)

        file_rows = [
            db.FileRow(path=p, bytes=s, mtime=taken_at, ctime=taken_at)
            for p, s in (files or {}).items()
        ]
        if file_rows:
            db.insert_files(self.conn, sid, file_rows)

        bucket_rows = [
            db.BucketRow(day=d, attribution=a, bytes=b, files=f)
            for d, a, b, f in (buckets or [])
        ]
        if bucket_rows:
            db.insert_buckets(self.conn, sid, bucket_rows)

        self.conn.commit()
        return sid


class FillGapsTest(unittest.TestCase):
    def test_fills_missing_days(self) -> None:
        days = [
            timeline.DayChange(day="2026-03-01", added=100),
            timeline.DayChange(day="2026-03-05", added=200),
        ]
        out = timeline._fill_gaps(days)
        self.assertEqual(
            [d.day for d in out],
            ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"],
        )
        self.assertEqual([d.added for d in out], [100, 0, 0, 0, 200])

    def test_no_duplicate_or_skipped_days_across_dst(self) -> None:
        """跨夏令时切换时每个日历日恰好出现一次。

        给时间戳加 86400 秒会在这里出错:回拨那天有 25 小时,
        午夜加一天只到 23 点,还是同一天。
        """
        for start, end in (
            ("2026-03-06", "2026-03-12"),   # 北美春季前拨
            ("2026-10-28", "2026-11-05"),   # 北美秋季回拨
            ("2026-03-27", "2026-04-02"),   # 欧洲切换
        ):
            with self.subTest(start=start):
                days = [
                    timeline.DayChange(day=start),
                    timeline.DayChange(day=end),
                ]
                out = timeline._fill_gaps(days)
                labels = [d.day for d in out]
                self.assertEqual(len(labels), len(set(labels)), f"有重复日期: {labels}")
                expected = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                self.assertEqual(len(labels), expected)
                self.assertEqual(labels, sorted(labels))

    def test_single_day(self) -> None:
        out = timeline._fill_gaps([timeline.DayChange(day="2026-05-09", added=7)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].added, 7)

    def test_empty(self) -> None:
        self.assertEqual(timeline._fill_gaps([]), [])

    def test_filled_days_are_zero_and_retro(self) -> None:
        out = timeline._fill_gaps(
            [
                timeline.DayChange(day="2026-01-01", added=5, basis="measured"),
                timeline.DayChange(day="2026-01-03", added=5, basis="measured"),
            ]
        )
        self.assertEqual(out[1].added, 0)
        self.assertEqual(out[1].net, 0)
        self.assertEqual(out[1].basis, "retro")


class RetroLayerTest(AnalysisFixture):
    def test_buckets_become_days(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[
                (day_str(-3), "Users\\me\\Downloads", 500, 2),
                (day_str(-3), "Program Files\\Steam", 300, 1),
                (day_str(-1), "Users\\me\\Videos", 200, 1),
            ],
        )
        days = timeline._retro_days(self.conn, sid, top_n=5)

        self.assertEqual(days[day_str(-3)].added, 800)
        self.assertEqual(days[day_str(-3)].net, 800)
        self.assertEqual(days[day_str(-3)].files_added, 3)
        self.assertEqual(days[day_str(-3)].basis, "retro")
        self.assertEqual(days[day_str(-1)].added, 200)

    def test_contributors_ranked_and_capped(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[(day_str(-2), f"dir{i}", (10 - i) * 100, 1) for i in range(6)],
        )
        days = timeline._retro_days(self.conn, sid, top_n=3)
        contributors = days[day_str(-2)].contributors

        self.assertEqual(len(contributors), 3)
        self.assertEqual(contributors[0].path, "dir0")
        self.assertEqual([c.bytes for c in contributors], [1000, 900, 800])

    def test_other_bucket_counts_but_is_not_a_contributor(self) -> None:
        """空归因是「其他」桶,金额要算,但不能当成一个目录展示。"""
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[
                (day_str(-1), "Users\\me", 700, 1),
                (day_str(-1), "", 300, 40),
            ],
        )
        days = timeline._retro_days(self.conn, sid, top_n=5)

        self.assertEqual(days[day_str(-1)].added, 1000)
        self.assertEqual(days[day_str(-1)].files_added, 41)
        self.assertEqual([c.path for c in days[day_str(-1)].contributors], ["Users\\me"])


class MeasuredLayerTest(AnalysisFixture):
    def test_single_snapshot_has_no_measured_days(self) -> None:
        self.add_snapshot(taken_at=ts_at(-1), scanned=1000)
        days, first = timeline._measured_days(self.conn, "C:", top_n=5)

        self.assertEqual(days, {})
        self.assertAlmostEqual(first, ts_at(-1), places=3)

    def test_net_comes_from_scanned_bytes(self) -> None:
        self.add_snapshot(
            taken_at=ts_at(-2), scanned=1000, dirs={"Users": 600, "Games": 400}
        )
        self.add_snapshot(
            taken_at=ts_at(-1), scanned=1500, dirs={"Users": 900, "Games": 600}
        )
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        change = days[day_str(-1)]
        self.assertEqual(change.net, 500)
        self.assertEqual(change.added, 500)
        self.assertEqual(change.removed, 0)
        self.assertEqual(change.basis, "measured")

    def test_deletion_shows_as_removed(self) -> None:
        self.add_snapshot(
            taken_at=ts_at(-2), scanned=1000, dirs={"Users": 600, "Games": 400}
        )
        self.add_snapshot(taken_at=ts_at(-1), scanned=650, dirs={"Users": 650})
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        change = days[day_str(-1)]
        self.assertEqual(change.net, -350)
        self.assertEqual(change.added, 50)
        self.assertEqual(change.removed, 400)
        self.assertEqual([s.path for s in change.shrinkers], ["Games"])
        self.assertEqual(change.shrinkers[0].bytes, 400)

    def test_nested_dirs_not_double_counted_in_totals(self) -> None:
        """added 只累加顶层,否则父子目录会重复计算。"""
        self.add_snapshot(
            taken_at=ts_at(-2),
            scanned=1000,
            dirs={"Users": 1000, "Users\\me": 800, "Users\\me\\Downloads": 500},
        )
        self.add_snapshot(
            taken_at=ts_at(-1),
            scanned=1600,
            dirs={"Users": 1600, "Users\\me": 1400, "Users\\me\\Downloads": 1100},
        )
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        change = days[day_str(-1)]
        self.assertEqual(change.added, 600)   # 只有 Users,不是 600+600+600
        self.assertEqual(change.net, 600)
        # 但归因里仍然列出深层目录,方便定位
        paths = {c.path for c in change.contributors}
        self.assertIn("Users\\me\\Downloads", paths)

    def test_two_snapshots_same_day_merge(self) -> None:
        """一天多个快照时,净增减累加,归因也要合并而不是被覆盖。"""
        self.add_snapshot(taken_at=ts_at(-1, hour=9), scanned=1000, dirs={"A": 1000})
        self.add_snapshot(
            taken_at=ts_at(-1, hour=13), scanned=1300, dirs={"A": 1000, "B": 300}
        )
        self.add_snapshot(
            taken_at=ts_at(-1, hour=20), scanned=1700, dirs={"A": 1400, "B": 300}
        )
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        self.assertEqual(len(days), 1)
        change = days[day_str(-1)]
        self.assertEqual(change.net, 700)
        self.assertEqual(change.added, 700)
        paths = {c.path: c.bytes for c in change.contributors}
        self.assertEqual(paths, {"A": 400, "B": 300})

    def test_incomplete_snapshots_ignored(self) -> None:
        self.add_snapshot(taken_at=ts_at(-2), scanned=1000, dirs={"A": 1000})
        self.add_snapshot(taken_at=ts_at(-1), scanned=99999, complete=False)
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        self.assertEqual(days, {})

    def test_other_drive_ignored(self) -> None:
        self.add_snapshot(drive="C:", taken_at=ts_at(-2), scanned=1000)
        self.add_snapshot(drive="D:", taken_at=ts_at(-1), scanned=5000)
        days, first = timeline._measured_days(self.conn, "C:", top_n=5)

        self.assertEqual(days, {})
        self.assertAlmostEqual(first, ts_at(-2), places=3)


class BuildTimelineTest(AnalysisFixture):
    def test_no_snapshot_gives_empty(self) -> None:
        self.assertEqual(timeline.build_timeline(self.conn, "C:"), [])

    def test_first_snapshot_day_is_retro_and_totals_reconcile(self) -> None:
        """第一个快照当天归回溯,之后的日子归实测,加起来不重不漏。

        两层数的是不相交的文件:回溯数的是第一次扫描时已经在盘上的,
        实测数的是那次扫描之后才变的。所以分界日留着回溯值不会重复计算 ——
        总和应该正好等于最新快照扫到的字节数。
        """
        self.add_snapshot(
            taken_at=ts_at(-4),
            scanned=1000,
            dirs={"A": 1000},
            buckets=[(day_str(-6), "A", 100, 1), (day_str(-4), "A", 900, 5)],
        )
        self.add_snapshot(
            taken_at=ts_at(-2), scanned=1400, dirs={"A": 1400},
            buckets=[(day_str(-6), "A", 100, 1), (day_str(-4), "A", 900, 5)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        by_day = {c.day: c for c in changes}

        self.assertEqual(by_day[day_str(-6)].basis, "retro")
        self.assertEqual(by_day[day_str(-6)].added, 100)
        self.assertEqual(by_day[day_str(-4)].basis, "retro")
        self.assertEqual(by_day[day_str(-4)].added, 900)
        self.assertEqual(by_day[day_str(-2)].basis, "measured")
        self.assertEqual(by_day[day_str(-2)].net, 400)
        # 100 + 900 + 400 == 最新快照的 scanned_bytes
        self.assertEqual(timeline.timeline_summary(changes)["net"], 1400)

    def test_two_snapshots_in_one_day_stay_retro(self) -> None:
        """同一天扫两次跨不过任何一天,那天仍归回溯。

        两次扫描相隔几小时,差值测的是这几小时,不是这一天。把它当成
        整天报会把当天少算一大截 —— 回溯值(当天创建的文件)更接近这一天。
        """
        self.add_snapshot(taken_at=ts_at(-3, hour=9), scanned=1000, dirs={"A": 1000})
        self.add_snapshot(
            taken_at=ts_at(-3, hour=22),
            scanned=1200,
            dirs={"A": 1200},
            buckets=[(day_str(-3), "A", 99999, 3)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        same_day = [c for c in changes if c.day == day_str(-3)]

        self.assertEqual(len(same_day), 1)
        self.assertEqual(same_day[0].basis, "retro")
        self.assertEqual(same_day[0].added, 99999)
        # 一天都还没测满,别声称已经进入实测
        self.assertEqual(timeline.timeline_summary(changes)["measured_days"], 0)

    def test_measured_takes_over_once_a_day_is_crossed(self) -> None:
        """跨天的那一对接管当天,同一天里的第二次扫描累加进去而不是被丢掉。"""
        self.add_snapshot(taken_at=ts_at(-2, hour=9), scanned=1000, dirs={"A": 1000})
        self.add_snapshot(taken_at=ts_at(-1, hour=9), scanned=1200, dirs={"A": 1200})
        self.add_snapshot(
            taken_at=ts_at(-1, hour=20),
            scanned=1500,
            dirs={"A": 1500},
            buckets=[(day_str(-1), "A", 99999, 3)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        by_day = {c.day: c for c in changes}

        self.assertEqual(by_day[day_str(-1)].basis, "measured")
        self.assertEqual(by_day[day_str(-1)].net, 500)   # 200 + 300,两对都算

    def test_retro_kept_when_only_one_snapshot(self) -> None:
        """刚装好的场景:一个快照,回溯层必须仍然给出历史。"""
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[
                (day_str(-5), "Users\\me\\Downloads", 400, 2),
                (day_str(-2), "Games", 600, 1),
            ],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        by_day = {c.day: c for c in changes}

        self.assertEqual(by_day[day_str(-5)].added, 400)
        self.assertEqual(by_day[day_str(-2)].added, 600)
        self.assertTrue(all(c.basis == "retro" for c in changes))

    def test_snapshot_day_kept_when_only_one_snapshot(self) -> None:
        """真机上撞出来的:装好当天扫一次,那天却是空白。

        旧规则把「第一个快照当天及之后」全划给实测层,可只有一个快照时
        实测层是空的 —— 6 GB、5 万多个文件直接掉进缝里。而刚装好那天
        正是最想看的一天。
        """
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[(day_str(-1), "A", 400, 2), (day_str(0), "B", 600, 3)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        by_day = {c.day: c for c in changes}

        self.assertIn(day_str(0), by_day)
        self.assertEqual(by_day[day_str(0)].added, 600)
        self.assertEqual(by_day[day_str(0)].files_added, 3)
        self.assertEqual(by_day[day_str(0)].basis, "retro")

    def test_days_between_snapshots_go_to_measured(self) -> None:
        """快照隔了几天时,中间那几天的增减已并进后一次差值,回溯不能再报。"""
        self.add_snapshot(taken_at=ts_at(-4), scanned=1000, dirs={"A": 1000})
        self.add_snapshot(
            taken_at=ts_at(-1),
            scanned=1500,
            dirs={"A": 1500},
            buckets=[
                (day_str(-9), "A", 700, 4),   # 快照之前,回溯照常
                # 中间两天在最新快照里当然有创建记录,但那笔账实测层已经算过
                (day_str(-3), "A", 200, 1),
                (day_str(-2), "A", 300, 1),
            ],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        by_day = {c.day: c for c in changes}

        self.assertEqual(by_day[day_str(-3)].added, 0)
        self.assertEqual(by_day[day_str(-2)].added, 0)
        self.assertEqual(by_day[day_str(-9)].added, 700)
        self.assertEqual(by_day[day_str(-1)].basis, "measured")
        self.assertEqual(by_day[day_str(-1)].net, 500)
        # 中间那两天不再重复计上:总量是 700+500,不是 700+500+200+300
        self.assertEqual(timeline.timeline_summary(changes)["net"], 1200)

    def test_future_dated_buckets_do_not_stretch_the_axis(self) -> None:
        """真机上撞出来的:C 盘里有个文件创建时间写着 2030 年。

        补零会一路补到那天,90 天的真实数据被挤成看不见的细线。
        扫描之后的日子我们没有任何观测,不该出现在轴上。
        """
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[
                (day_str(-2), "A", 500, 1),
                (day_str(0), "B", 500, 1),
                (day_str(1200), "weird", 4096, 1),
            ],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=90)
        days = [c.day for c in changes]

        self.assertEqual(days[-1], day_str(0))
        self.assertNotIn(day_str(1200), days)
        self.assertLessEqual(len(changes), 91)

    def test_cutoff_respected(self) -> None:
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[(day_str(-100), "old", 500, 1), (day_str(-3), "new", 500, 1)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)
        days = [c.day for c in changes]

        self.assertNotIn(day_str(-100), days)
        self.assertIn(day_str(-3), days)

    def test_gaps_filled_between_first_and_last(self) -> None:
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[(day_str(-5), "a", 100, 1), (day_str(-1), "b", 100, 1)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30)

        self.assertEqual([c.day for c in changes], [day_str(-d) for d in (5, 4, 3, 2, 1)])
        self.assertEqual([c.added for c in changes], [100, 0, 0, 0, 100])

    def test_fill_gaps_off(self) -> None:
        self.add_snapshot(
            taken_at=ts_at(0),
            scanned=1000,
            buckets=[(day_str(-5), "a", 100, 1), (day_str(-1), "b", 100, 1)],
        )
        changes = timeline.build_timeline(self.conn, "C:", days=30, fill_gaps=False)
        self.assertEqual([c.day for c in changes], [day_str(-5), day_str(-1)])


class TimelineSummaryTest(unittest.TestCase):
    def test_totals_and_boundaries(self) -> None:
        changes = [
            timeline.DayChange(day="2026-04-01", added=100, net=100, basis="retro"),
            timeline.DayChange(day="2026-04-02", added=900, net=900, basis="retro"),
            timeline.DayChange(
                day="2026-04-03", added=300, removed=50, net=250, basis="measured"
            ),
            timeline.DayChange(
                day="2026-04-04", added=10, removed=500, net=-490, basis="measured"
            ),
        ]
        s = timeline.timeline_summary(changes)

        self.assertEqual(s["days"], 4)
        self.assertEqual(s["total_added"], 1310)
        self.assertEqual(s["total_removed"], 550)
        self.assertEqual(s["net"], 760)
        self.assertEqual(s["measured_days"], 2)
        self.assertEqual(s["retro_days"], 2)
        self.assertEqual(s["busiest_day"], "2026-04-02")
        self.assertEqual(s["first_measured_day"], "2026-04-03")

    def test_empty(self) -> None:
        s = timeline.timeline_summary([])
        self.assertEqual(s["days"], 0)
        self.assertIsNone(s["busiest_day"])
        self.assertIsNone(s["first_measured_day"])

    def test_no_measured_days(self) -> None:
        s = timeline.timeline_summary([timeline.DayChange(day="2026-04-01", added=1)])
        self.assertEqual(s["measured_days"], 0)
        self.assertIsNone(s["first_measured_day"])


MB = 1024 * 1024


class DiffTest(AnalysisFixture):
    def test_classification(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1),
            scanned=100 * MB,
            dirs={"Grow": 10 * MB, "Shrink": 20 * MB, "Gone": 30 * MB},
            files={"Grow\\big.bin": 10 * MB, "Gone\\x.iso": 30 * MB},
        )
        b = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=140 * MB,
            dirs={"Grow": 60 * MB, "Shrink": 5 * MB, "New": 40 * MB},
            files={"Grow\\big.bin": 60 * MB, "New\\y.iso": 40 * MB},
        )
        d = diff.diff_snapshots(self.conn, a, b)
        kinds = {x.path: x.kind for x in d.dir_deltas}

        self.assertEqual(kinds["Grow"], diff.GREW)
        self.assertEqual(kinds["Shrink"], diff.SHRANK)
        self.assertEqual(kinds["Gone"], diff.VANISHED)
        self.assertEqual(kinds["New"], diff.APPEARED)
        self.assertEqual(d.net, 40 * MB)

    def test_sorted_by_magnitude(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={"Small": 1 * MB, "Big": 1 * MB}
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"Small": 4 * MB, "Big": 90 * MB}
        )
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertEqual([x.path for x in d.dir_deltas], ["Big", "Small"])

    def test_min_bytes_filters_noise(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={"Noise": 10 * MB, "Real": 10 * MB}
        )
        b = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=100 * MB,
            dirs={"Noise": 10 * MB + 4096, "Real": 50 * MB},
        )
        d = diff.diff_snapshots(self.conn, a, b, min_bytes=MB)
        self.assertEqual([x.path for x in d.dir_deltas], ["Real"])

    def test_depth_limit(self) -> None:
        deep = "A\\B\\C\\D\\E\\deep"
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB, deep: 10 * MB})
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 90 * MB, deep: 90 * MB})
        d = diff.diff_snapshots(self.conn, a, b, max_depth=4)

        self.assertIn("A", [x.path for x in d.dir_deltas])
        self.assertNotIn(deep, [x.path for x in d.dir_deltas])

    def test_auto_swap_when_passed_newest_first(self) -> None:
        old = self.add_snapshot(taken_at=ts_at(-2), scanned=100 * MB, dirs={"A": 10 * MB})
        new = self.add_snapshot(taken_at=ts_at(0), scanned=150 * MB, dirs={"A": 60 * MB})
        d = diff.diff_snapshots(self.conn, new, old)

        self.assertEqual(d.before_id, old)
        self.assertEqual(d.after_id, new)
        self.assertEqual(d.net, 50 * MB)
        self.assertEqual(d.dir_deltas[0].kind, diff.GREW)

    def test_cross_drive_rejected(self) -> None:
        c = self.add_snapshot(drive="C:", taken_at=ts_at(-1), scanned=100 * MB)
        d_ = self.add_snapshot(drive="D:", taken_at=ts_at(0), scanned=100 * MB)
        with self.assertRaises(ValueError) as ctx:
            diff.diff_snapshots(self.conn, c, d_)
        self.assertIn("跨盘", str(ctx.exception))

    def test_missing_snapshot_rejected(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB)
        with self.assertRaises(ValueError):
            diff.diff_snapshots(self.conn, a, 99999)

    def test_demoted_caveat(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB},
            files={"A\\x": 10 * MB}, note="[已降级]",
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB}, files={"A\\x": 20 * MB}
        )
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertTrue(any("降级" in c for c in d.caveats))

    def test_mixed_method_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, method="mft")
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, method="scandir")
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertTrue(any("扫描方式不同" in c for c in d.caveats))

    def test_no_file_rows_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB})
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB})
        d = diff.diff_snapshots(self.conn, a, b)

        self.assertEqual(d.file_deltas, [])
        self.assertTrue(any("只能对比目录" in c for c in d.caveats))

    def test_one_side_missing_files_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB})
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB}, files={"A\\x": 20 * MB}
        )
        d = diff.diff_snapshots(self.conn, a, b)

        self.assertEqual(d.file_deltas, [])
        self.assertTrue(any("跳过文件级对比" in c for c in d.caveats))

    def test_threshold_caveat_when_both_have_files(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, files={"a": 10 * MB})
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, files={"a": 30 * MB})
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertTrue(any("新出现" in c for c in d.caveats))

    def test_grew_shrank_helpers(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={"Up": 10 * MB, "Down": 50 * MB}
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"Up": 50 * MB, "Down": 10 * MB}
        )
        d = diff.diff_snapshots(self.conn, a, b)

        self.assertEqual([x.path for x in d.grew], ["Up"])
        self.assertEqual([x.path for x in d.shrank], ["Down"])

    def test_as_dict_limits(self) -> None:
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={f"d{i}": MB for i in range(50)}
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={f"d{i}": 40 * MB for i in range(50)}
        )
        d = diff.diff_snapshots(self.conn, a, b)
        payload = d.as_dict(limit=5)

        self.assertEqual(len(d.grew), 50)
        self.assertEqual(len(payload["grew"]), 5)
        self.assertEqual(payload["shrank"], [])
        self.assertEqual(payload["net"], d.net)
        self.assertEqual(payload["grew"][0]["delta"], 39 * MB)

    def test_diff_latest(self) -> None:
        self.add_snapshot(taken_at=ts_at(-2), scanned=100 * MB, dirs={"A": 10 * MB})
        self.add_snapshot(taken_at=ts_at(-1), scanned=120 * MB, dirs={"A": 30 * MB})
        self.add_snapshot(taken_at=ts_at(0), scanned=200 * MB, dirs={"A": 110 * MB})

        d1 = diff.diff_latest(self.conn, "C:")
        self.assertEqual(d1.net, 80 * MB)

        d2 = diff.diff_latest(self.conn, "C:", back=2)
        self.assertEqual(d2.net, 100 * MB)

    def test_diff_latest_needs_two(self) -> None:
        self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB)
        self.assertIsNone(diff.diff_latest(self.conn, "C:"))
        self.assertIsNone(diff.diff_latest(self.conn, "D:"))


class HotspotsTest(AnalysisFixture):
    def test_biggest_dirs_excludes_descendants(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=100 * MB,
            dirs={
                "Users": 90 * MB,
                "Users\\me": 88 * MB,
                "Users\\me\\Downloads": 80 * MB,
                "Games": 50 * MB,
            },
        )
        top = hotspots.biggest_dirs(self.conn, sid, limit=10)
        paths = [h.path for h in top]

        self.assertEqual(paths, ["Users", "Games"])

    def test_biggest_dirs_respects_depth(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=100 * MB,
            dirs={"A\\B\\C\\D\\E\\F\\G": 99 * MB, "Z": 10 * MB},
        )
        top = hotspots.biggest_dirs(self.conn, sid, limit=10, max_depth=3)
        self.assertEqual([h.path for h in top], ["Z"])

    def test_biggest_files(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=100 * MB,
            files={"a.iso": 30 * MB, "b.bin": 90 * MB, "c.txt": MB},
        )
        top = hotspots.biggest_files(self.conn, sid, limit=2)
        self.assertEqual([h.path for h in top], ["b.bin", "a.iso"])

    def test_classify_path_hits_and_misses(self) -> None:
        hit = hotspots.classify_path(r"Users\me\AppData\Local\Temp")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[2], "safe")

        careful = hotspots.classify_path(r"Windows\WinSxS")
        self.assertEqual(careful[2], "careful")

        self.assertIsNone(hotspots.classify_path(r"Users\me\Documents\thesis"))

    def test_classify_is_case_insensitive(self) -> None:
        self.assertIsNotNone(hotspots.classify_path(r"WINDOWS\TEMP"))
        self.assertIsNotNone(hotspots.classify_path(r"windows\temp"))

    def test_cleanup_candidates_labels_and_threshold(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=500 * MB,
            dirs={
                r"Windows\Temp": 200 * MB,
                r"Users\me\Documents": 300 * MB,
                r"Users\me\AppData\Local\pip\cache": 5 * MB,
            },
            files={"pagefile.sys": 400 * MB},
        )
        found = hotspots.cleanup_candidates(self.conn, sid, min_bytes=32 * MB)
        by_path = {h.path: h for h in found}

        self.assertIn(r"Windows\Temp", by_path)
        self.assertIn("pagefile.sys", by_path)
        self.assertNotIn(r"Users\me\Documents", by_path)
        # 低于阈值的不上榜,免得列表被小东西塞满
        self.assertNotIn(r"Users\me\AppData\Local\pip\cache", by_path)
        self.assertEqual(by_path[r"Windows\Temp"].safety, "safe")
        self.assertEqual(by_path["pagefile.sys"].safety, "careful")

    def test_cleanup_candidates_sorted_desc(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=500 * MB,
            dirs={r"Windows\Temp": 100 * MB, r"Users\me\AppData\Local\Temp": 300 * MB},
        )
        found = hotspots.cleanup_candidates(self.conn, sid, min_bytes=MB)
        self.assertEqual(
            [h.path for h in found],
            [r"Users\me\AppData\Local\Temp", r"Windows\Temp"],
        )

    def test_cleanup_skips_nested_same_rule(self) -> None:
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=500 * MB,
            dirs={r"Windows\Temp": 100 * MB, r"Windows\Temp\sub": 90 * MB},
        )
        found = hotspots.cleanup_candidates(self.conn, sid, min_bytes=MB)
        self.assertEqual([h.path for h in found], [r"Windows\Temp"])

    def test_cleanup_skips_deeply_nested(self) -> None:
        """压制要认所有层级的祖先,不只是直接父目录。

        判断"祖先是否已收录"是沿路径向上查集合做的,只走一层就会把孙子目录
        重复算进来 —— 界面上同一份空间会被列两次。
        """
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=900 * MB,
            dirs={
                r"Windows\Temp": 300 * MB,
                r"Windows\Temp\a\b\c": 200 * MB,       # 隔了三层
                r"Windows\Temp\a\b\c\d\e": 100 * MB,   # 隔了五层
            },
        )
        found = hotspots.cleanup_candidates(self.conn, sid, min_bytes=MB)
        self.assertEqual([h.path for h in found], [r"Windows\Temp"])

    def test_cleanup_sibling_with_shared_prefix_not_suppressed(self) -> None:
        """名字以已收录目录开头、但不是它后代的目录,不能被压掉。

        压制只在分隔符边界上成立:Windows\\Temp 收录之后,Windows\\Temp2 是
        兄弟不是后代。少了分隔符这一步就会把它一起吞掉。
        """
        sid = self.add_snapshot(
            taken_at=ts_at(0),
            scanned=500 * MB,
            dirs={r"Windows\Temp": 300 * MB, r"Windows\Temp2": 200 * MB},
        )
        found = hotspots.cleanup_candidates(self.conn, sid, min_bytes=MB)
        self.assertEqual(
            sorted(h.path for h in found), [r"Windows\Temp", r"Windows\Temp2"]
        )

    def test_recently_grown_filters_by_age(self) -> None:
        now = ts_at(0, hour=18)
        sid = self.add_snapshot(
            taken_at=now, scanned=500 * MB, dirs={"Fresh": 200 * MB}, newest_ctime=ts_at(-2)
        )
        # 同一快照里再插一个很旧的大目录,验证它不会被算成「最近增长」
        db.insert_dirs(
            self.conn,
            sid,
            [
                db.DirRow(
                    path="Stale",
                    depth=1,
                    bytes=300 * MB,
                    own_bytes=300 * MB,
                    files=1,
                    dirs=0,
                    newest_mtime=ts_at(-200),
                    newest_ctime=ts_at(-200),
                )
            ],
        )
        self.conn.commit()

        grown = hotspots.recently_grown(self.conn, sid, days=14, min_bytes=MB, now=now)
        self.assertEqual([g.path for g in grown], ["Fresh"])
        self.assertAlmostEqual(grown[0].days_old, 2, delta=0.5)

    def test_recently_grown_excludes_descendants(self) -> None:
        now = ts_at(0, hour=18)
        sid = self.add_snapshot(
            taken_at=now,
            scanned=500 * MB,
            dirs={"Games": 200 * MB, "Games\\Steam": 190 * MB},
            newest_ctime=ts_at(-1),
        )
        grown = hotspots.recently_grown(self.conn, sid, days=14, min_bytes=MB, now=now)
        self.assertEqual([g.path for g in grown], ["Games"])

    def test_age_profile_bands(self) -> None:
        now = time.time()
        sid = self.add_snapshot(
            taken_at=now,
            scanned=100 * MB,
            buckets=[
                (day_str(0), "today", 10 * MB, 1),
                (day_str(-3), "week", 20 * MB, 2),
                (day_str(-20), "month", 30 * MB, 3),
                (day_str(-500), "old", 40 * MB, 4),
            ],
        )
        profile = {b["key"]: b for b in hotspots.age_profile(self.conn, sid, now=now)}

        self.assertEqual(profile["today"]["bytes"], 10 * MB)
        self.assertEqual(profile["week"]["bytes"], 20 * MB)
        self.assertEqual(profile["month"]["bytes"], 30 * MB)
        self.assertEqual(profile["older"]["bytes"], 40 * MB)
        total = sum(b["bytes"] for b in profile.values())
        self.assertEqual(total, 100 * MB)

    def test_age_profile_dates_anchor_at_noon(self) -> None:
        """日期按当天正午折算年龄,不是午夜。

        分桶只精确到天,折算成时间戳时必须选一个代表时刻。取正午的话,误差最多
        半天且两边对称;取午夜就等于把每个日期都算老了不到一天,正好卡在分界上的
        那天会被推进更旧的一档。这里让 now 落在正午,7 天前那天的年龄恰好是 7.0,
        属于「一周内」;若改用午夜就变成 7.5 天,掉进「一月内」。
        """
        # now 固定在某天正午,不依赖跑测试的时刻
        anchor = date.today()
        now = datetime(anchor.year, anchor.month, anchor.day, 12, 0, 0).timestamp()
        sid = self.add_snapshot(
            taken_at=now,
            scanned=50 * MB,
            buckets=[(day_str(-7, base=now), "edge", 50 * MB, 5)],
        )

        profile = {b["key"]: b for b in hotspots.age_profile(self.conn, sid, now=now)}

        self.assertEqual(profile["week"]["bytes"], 50 * MB)
        self.assertEqual(profile["month"]["bytes"], 0)

    def test_age_profile_keys_stable(self) -> None:
        sid = self.add_snapshot(taken_at=time.time(), scanned=0)
        profile = hotspots.age_profile(self.conn, sid)
        self.assertEqual(
            [b["key"] for b in profile],
            ["today", "week", "month", "quarter", "year", "older"],
        )
        self.assertTrue(all(b["bytes"] == 0 for b in profile))


if __name__ == "__main__":
    unittest.main()
