"""分析层测试:时间轴的回溯/实测分界、快照对比、热点识别。

快照直接手写进内存库,不经过扫描 —— 这样可以精确控制日期和字节数,
把边界条件钉死。
"""

from __future__ import annotations

import time
import unittest
import unittest.mock
from datetime import date, datetime, timedelta

from strata import config
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


class CollapseChainsTest(unittest.TestCase):
    """一次增减只该占一行。

    快照差是按目录逐层算的,所以 360Safe 长了 48 MB,它的每一层祖先都跟着
    长 48 MB。原来直接按字节排序取前五,真实数据上出现过 Program Files (x86)、
    ...\\360、...\\360\\360Safe 三行同一件事 —— 界面只显示四行,一件事吃掉三格。
    """

    def test_keeps_the_deepest_of_a_pass_through_chain(self) -> None:
        """整条链只有一个来源时,报最深的那个 —— 它才指出是谁干的。"""
        items = [
            timeline.Contributor("Program Files", 48_000_000),
            timeline.Contributor("Program Files\\360", 48_000_000),
            timeline.Contributor("Program Files\\360\\360Safe", 48_000_000),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Program Files\\360\\360Safe"])

    def test_keeps_the_parent_when_growth_is_spread(self) -> None:
        """长的东西分散在很多子目录里时,父目录才是那件事。"""
        items = [
            timeline.Contributor("Users", 200_000_000),
            timeline.Contributor("Users\\a", 1_000_000),
            timeline.Contributor("Users\\b", 1_000_000),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Users"])

    def test_drills_into_a_dominant_child(self) -> None:
        """子目录占了绝大部分时报子目录,报父目录等于让人自己再找一遍。

        真实数据:Program Files 减了 137.8 MB,其中 133.8 MB 是 OneDrive。
        """
        items = [
            timeline.Contributor("Program Files", 137_800_000),
            timeline.Contributor("Program Files\\Microsoft OneDrive", 133_800_000),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Program Files\\Microsoft OneDrive"])
        self.assertEqual(out[0].bytes, 133_800_000)

    def test_does_not_drill_when_the_child_is_not_dominant(self) -> None:
        """最大的子目录只占一半 —— 说明是好几处一起在长,该报父目录。"""
        items = [
            timeline.Contributor("Users", 100_000_000),
            timeline.Contributor("Users\\a", 50_000_000),
            timeline.Contributor("Users\\b", 45_000_000),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Users"])

    def test_drills_through_several_levels(self) -> None:
        """一路都不分岔就一路钻到底。"""
        items = [
            timeline.Contributor("A", 1000),
            timeline.Contributor("A\\b", 1000),
            timeline.Contributor("A\\b\\c", 990),
            timeline.Contributor("A\\b\\c\\d", 950),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["A\\b\\c\\d"])

    def test_drill_stops_at_the_fork(self) -> None:
        """钻到分岔就停,不能跳过分岔继续往下。"""
        items = [
            timeline.Contributor("A", 1000),
            timeline.Contributor("A\\b", 1000),
            timeline.Contributor("A\\b\\x", 500),
            timeline.Contributor("A\\b\\y", 500),
            timeline.Contributor("A\\b\\x\\deep", 500),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["A\\b"])

    def test_ratio_boundary_is_inclusive(self) -> None:
        """恰好等于阈值算占大头 —— 边界要定死,不然改比例会悄悄换结论。"""
        items = [
            timeline.Contributor("A", 1000),
            timeline.Contributor("A\\b", 900),
        ]
        self.assertEqual(
            [c.path for c in timeline._collapse_chains(items, ratio=0.9)], ["A\\b"]
        )
        items[1] = timeline.Contributor("A\\b", 899)
        self.assertEqual(
            [c.path for c in timeline._collapse_chains(items, ratio=0.9)], ["A"]
        )

    def test_unrelated_paths_all_survive(self) -> None:
        """互不为祖先的目录一个都不能少 —— 这才是需要那几格的地方。"""
        items = [
            timeline.Contributor("Users\\a", 30),
            timeline.Contributor("Windows\\b", 20),
            timeline.Contributor("Program Files\\c", 10),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual(len(out), 3)

    def test_shared_prefix_is_not_a_chain(self) -> None:
        """Temp 和 Temp2 是兄弟,不是父子。只在分隔符处切才不会认错。"""
        items = [
            timeline.Contributor("Windows\\Temp", 50),
            timeline.Contributor("Windows\\Temp2", 40),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Windows\\Temp", "Windows\\Temp2"])

    def test_output_stays_sorted_by_bytes(self) -> None:
        """折叠之后顺序还得是从大到小,界面直接切前几行。"""
        items = [
            timeline.Contributor("b", 10),
            timeline.Contributor("a", 30),
            timeline.Contributor("c", 20),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.bytes for c in out], [30, 20, 10])

    def test_resorts_after_drilling_shrinks_a_value(self) -> None:
        """钻下去会让字节变小,可能就排到别人后面了 —— 必须重排。

        A 选中时 1000,钻到 A\\b 之后只剩 910,比 B 的 950 小。不重排的话
        界面切前一行会切到 910 那个,而 950 才是最大的那件事。
        """
        items = [
            timeline.Contributor("A", 1000),
            timeline.Contributor("A\\b", 910),
            timeline.Contributor("B", 950),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([(c.path, c.bytes) for c in out], [("B", 950), ("A\\b", 910)])

    def test_drills_into_the_biggest_child_not_the_smallest(self) -> None:
        """分岔不明显时也要认准占大头的那个。

        A\\big 占了 A 的 95%,A\\small 只占 4%。往小的那边看会得出「没有哪个
        子目录占大头」的结论,于是停在 A —— 把能指名的结论丢了。
        """
        items = [
            timeline.Contributor("A", 1000),
            timeline.Contributor("A\\big", 950),
            timeline.Contributor("A\\small", 40),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["A\\big"])

    def test_deeper_wins_when_a_middle_level_is_missing(self) -> None:
        """链上缺了一层时,靠的是「同字节先取深的」那条平局规则。

        比较用的目录集合是裁过的(深度上限、小目录合并),中间某层可能不在里面。
        这时候从浅的那头钻不下去 —— children 里 A 的孩子是 A\\b,不是 A\\b\\c ——
        所以必须一开始就选中最深的那个。
        """
        items = [
            timeline.Contributor("A", 500),
            timeline.Contributor("A\\b\\c", 500),      # A\b 这一层不在集合里
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["A\\b\\c"])

    def test_shared_prefix_not_suppressed_in_either_order(self) -> None:
        """Temp2 先被收录时,Temp 也不能被当成它的祖先误杀。

        判断祖先必须要求紧跟分隔符。少了那一步,"Windows\\Temp2" 会以
        "Windows\\Temp" 开头,于是后来的 Temp 被当成「已经报过了」丢掉。
        """
        items = [
            timeline.Contributor("Windows\\Temp2", 50),   # 先被选中
            timeline.Contributor("Windows\\Temp", 40),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["Windows\\Temp2", "Windows\\Temp"])

    def test_two_separate_chains_each_report_once(self) -> None:
        items = [
            timeline.Contributor("A", 100),
            timeline.Contributor("A\\x", 100),
            timeline.Contributor("B", 50),
            timeline.Contributor("B\\y", 50),
        ]
        out = timeline._collapse_chains(items)
        self.assertEqual([c.path for c in out], ["A\\x", "B\\y"])

    def test_empty_input(self) -> None:
        self.assertEqual(timeline._collapse_chains([]), [])


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

    def test_a_chain_takes_one_slot_in_the_real_aggregation(self) -> None:
        """整条祖先链只占归因列表的一格 —— 折叠要接在真正跑的那条路上。

        单测 _collapse_chains 证明不了这件事:上面那个测试里三层各涨 600,
        不折叠也一样通过(它只查 Downloads 在不在里面)。这里查的是「几行」,
        没接上折叠的话会是 3 行同一件事,把界面前几格挤满。
        """
        self.add_snapshot(
            taken_at=ts_at(-2),
            scanned=1000,
            dirs={"Users": 1000, "Users\\me": 800, "Users\\me\\Downloads": 500,
                  "Games": 0},
        )
        self.add_snapshot(
            taken_at=ts_at(-1),
            scanned=1700,
            dirs={"Users": 1600, "Users\\me": 1400, "Users\\me\\Downloads": 1100,
                  "Games": 100},
        )
        days, _ = timeline._measured_days(self.conn, "C:", top_n=5)

        got = [(c.path, c.bytes) for c in days[day_str(-1)].contributors]
        self.assertEqual(got, [("Users\\me\\Downloads", 600), ("Games", 100)])

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
        # 100 + 900 + 400 == 最新快照的 scanned_bytes。这里把两种口径的量加在
        # 一起,只是为了验「没有哪天被数两遍」;界面上它们是分开报的。
        s = timeline.timeline_summary(changes)
        self.assertEqual(s["retro_bytes"] + s["measured_net"], 1400)

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
        # 中间那两天不再重复计上:总量是 700+500,不是 700+500+200+300。
        # 同上,两种口径相加只用来验没重复,不是界面上的数。
        s = timeline.timeline_summary(changes)
        self.assertEqual(s["retro_bytes"] + s["measured_net"], 1200)

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
        self.assertEqual(s["measured_days"], 2)
        self.assertEqual(s["retro_days"], 2)
        self.assertEqual(s["busiest_day"], "2026-04-02")
        self.assertEqual(s["first_measured_day"], "2026-04-03")
        # 实测的三个数只统计实测那两天
        self.assertEqual(s["measured_added"], 310)
        self.assertEqual(s["measured_removed"], 550)
        self.assertEqual(s["measured_net"], -240)
        # 回溯的量单独放,不参与任何净值
        self.assertEqual(s["retro_bytes"], 1000)

    def test_does_not_report_a_combined_net(self) -> None:
        """两层不能加在一起报成一个净变化。

        回溯值是「现在盘上、创建于那天的字节数」,和实测的净增减不是同一种量:
        真实数据上 2026-08-28 回溯说 +8.35 GB,实测说 -0.74 GB,差 9 GB。
        加起来的那个数没有任何含义,而它以前就是界面顶部的大字。
        """
        changes = [
            timeline.DayChange(day="2026-04-01", added=100, net=100, basis="retro"),
            timeline.DayChange(day="2026-04-02", added=10, removed=500, net=-490,
                               basis="measured"),
        ]
        s = timeline.timeline_summary(changes)
        for gone in ("net", "total_added", "total_removed"):
            self.assertNotIn(gone, s, f"{gone} 混了两种口径,不该再出现")

    def test_retro_bytes_ignores_measured_days(self) -> None:
        changes = [
            timeline.DayChange(day="2026-04-01", added=700, net=700, basis="retro"),
            timeline.DayChange(day="2026-04-02", added=9999, net=9999,
                               basis="measured"),
        ]
        self.assertEqual(timeline.timeline_summary(changes)["retro_bytes"], 700)

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
        self.assertIn("demoted", [c["code"] for c in d.caveats])

    def test_demoted_caveat_numbers_follow_config(self) -> None:
        """「深度超过 3 层、小于 64 MB」这两个数字必须跟着 config 变。

        原来它们是手写在中文句子里的。改了 config.DEMOTE_* 之后,界面上那句话
        就是一句错话 —— 而且没有任何测试会红:文案对不对没人验,降级行为本身
        用的是 config,两边各说各话也照样通过。

        这里把 config 改掉再看输出跟不跟,而不是拿输出和 config 比。比相等
        测不出这件事:写死的 3 恰好就等于现在的 config 值,断言照样通过,
        改回写死的也照样绿 —— 这个变异体真的逃过一次。
        """
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB},
            files={"A\\x": 10 * MB}, note="[已降级]",
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB}, files={"A\\x": 20 * MB}
        )

        with unittest.mock.patch.object(config, "DEMOTE_DIR_MAX_DEPTH", 9), \
             unittest.mock.patch.object(config, "DEMOTE_DIR_MIN_BYTES", 7 * MB):
            d = diff.diff_snapshots(self.conn, a, b)

        note = next(c for c in d.caveats if c["code"] == "demoted")
        self.assertEqual(note["vars"]["depth"], 9)
        self.assertEqual(note["vars"]["bytes"], 7 * MB)
        self.assertEqual(note["vars"]["n"], 1)

    def test_mixed_method_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, method="mft")
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, method="scandir")
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertIn("mixedMethod", [c["code"] for c in d.caveats])

    def test_no_file_rows_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB})
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB})
        d = diff.diff_snapshots(self.conn, a, b)

        self.assertEqual(d.file_deltas, [])
        self.assertIn("noFilesEitherSide", [c["code"] for c in d.caveats])

    def test_one_side_missing_files_caveat(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, dirs={"A": 10 * MB})
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=100 * MB, dirs={"A": 20 * MB}, files={"A\\x": 20 * MB}
        )
        d = diff.diff_snapshots(self.conn, a, b)

        self.assertEqual(d.file_deltas, [])
        self.assertIn("noFilesOneSide", [c["code"] for c in d.caveats])

    def test_threshold_caveat_when_both_have_files(self) -> None:
        a = self.add_snapshot(taken_at=ts_at(-1), scanned=100 * MB, files={"a": 10 * MB})
        b = self.add_snapshot(taken_at=ts_at(0), scanned=100 * MB, files={"a": 30 * MB})
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertIn("fileThreshold", [c["code"] for c in d.caveats])

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
        self.assertEqual(hit, ("userTemp", "safe"))

        careful = hotspots.classify_path(r"Windows\WinSxS")
        self.assertEqual(careful, ("winSxs", "careful"))

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
