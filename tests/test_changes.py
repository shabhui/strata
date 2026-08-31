"""USN 收集层测试。

真实卷读不了(要管理员权限),所以用假 journal 顶替 UsnJournal,
把游标推进、日志重建、路径还原这些逻辑单独钉死。
"""

from __future__ import annotations

import time
import unittest
from datetime import date, timedelta

from strata.ntfs import usn as usn_mod
from strata.scan import changes
from strata.store import db

MB = 1024 * 1024


def ts_days_ago(n: float) -> float:
    return time.time() - n * 86400


def day_str(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def make_event(
    *,
    usn: int,
    name: str,
    reason: int,
    parent: int = 5,
    parent_full: int = 0,
    timestamp: float | None = None,
    attributes: int = 0x80,
) -> usn_mod.UsnEvent:
    """parent 是掩过序列号的记录号(跟 MFT 侧对),parent_full 是原样 64 位。

    parent_full 默认 0 —— 0 不是有效引用,反查会直接短路不发系统调用。
    这个默认让原有的测试保持原样:它们测的是游标、去重、分类,跟反查无关。
    """
    return usn_mod.UsnEvent(
        usn=usn,
        file_reference=usn + 1000,
        parent_reference=parent,
        timestamp=timestamp if timestamp is not None else ts_days_ago(1),
        reason=reason,
        attributes=attributes,
        name=name,
        parent_reference_full=parent_full,
    )


class FakeJournal:
    """假的 UsnJournal。记录别人怎么调它,方便断言游标行为。"""

    instances: list["FakeJournal"] = []

    def __init__(self, drive: str) -> None:
        self.drive = drive
        self.last_usn = 0
        self.read_from: list[int] = []
        FakeJournal.instances.append(self)

    # 由测试逐个设置
    info = usn_mod.JournalInfo(
        journal_id=111,
        first_usn=0,
        next_usn=9999,
        lowest_valid_usn=0,
        max_usn=1 << 60,
        max_size=32 * MB,
        allocation_delta=4 * MB,
    )
    events: list[usn_mod.UsnEvent] = []
    raise_on_query: Exception | None = None

    def query(self) -> usn_mod.JournalInfo:
        if self.raise_on_query is not None:
            raise self.raise_on_query
        return self.info

    def read_all(self, start_usn, *, journal_id=None, max_events=0, **kw):
        self.read_from.append(start_usn)
        picked = [e for e in self.events if e.usn >= start_usn]
        for event in picked:
            yield event
        self.last_usn = (picked[-1].usn + 1) if picked else start_usn

    def __enter__(self) -> "FakeJournal":
        return self

    def __exit__(self, *exc) -> None:
        return None


class ChangesFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        FakeJournal.instances = []
        FakeJournal.events = []
        FakeJournal.raise_on_query = None
        FakeJournal.info = usn_mod.JournalInfo(
            journal_id=111,
            first_usn=0,
            next_usn=9999,
            lowest_valid_usn=0,
            max_usn=1 << 60,
            max_size=32 * MB,
            allocation_delta=4 * MB,
        )

    def patch_journal(self) -> None:
        original = changes.usn_mod.UsnJournal
        changes.usn_mod.UsnJournal = FakeJournal
        self.addCleanup(lambda: setattr(changes.usn_mod, "UsnJournal", original))

    def add_event_rows(self, rows: list[db.UsnRow], drive: str = "C:") -> None:
        db.insert_usn_events(self.conn, drive, rows)
        self.conn.commit()


class ComposePathTest(unittest.TestCase):
    def test_uses_parent_map(self) -> None:
        event = make_event(usn=1, name="big.iso", reason=usn_mod.USN_REASON_FILE_DELETE, parent=42)
        path = changes._compose_path(event, {42: r"Users\me\Downloads"})
        self.assertEqual(path, r"Users\me\Downloads\big.iso")

    def test_root_parent_gives_bare_name(self) -> None:
        event = make_event(usn=1, name="pagefile.sys", reason=0, parent=5)
        self.assertEqual(changes._compose_path(event, {5: ""}), "pagefile.sys")

    def test_unknown_parent_gives_none(self) -> None:
        event = make_event(usn=1, name="x", reason=0, parent=999)
        self.assertIsNone(changes._compose_path(event, {5: ""}))

    def test_no_map_gives_none(self) -> None:
        event = make_event(usn=1, name="x", reason=0)
        self.assertIsNone(changes._compose_path(event, None))
        self.assertIsNone(changes._compose_path(event, {}))


class CollectUsnTest(ChangesFixture):
    def test_stores_only_interesting_kinds(self) -> None:
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="new.bin", reason=usn_mod.USN_REASON_FILE_CREATE),
            make_event(usn=11, name="gone.bin", reason=usn_mod.USN_REASON_FILE_DELETE),
            make_event(usn=12, name="hot.log", reason=usn_mod.USN_REASON_DATA_EXTEND),
            make_event(usn=13, name="acl", reason=usn_mod.USN_REASON_SECURITY_CHANGE),
        ]
        stats = changes.collect_usn(self.conn, "C:")

        self.assertTrue(stats.available)
        self.assertEqual(stats.events_read, 4)
        self.assertEqual(stats.events_stored, 2)   # write 和 security 不入库

        kinds = {r["kind"] for r in self.conn.execute("SELECT kind FROM usn_events")}
        self.assertEqual(kinds, {"create", "delete"})

    def test_resolves_paths_when_dir_map_given(self) -> None:
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="a.iso", reason=usn_mod.USN_REASON_FILE_DELETE, parent=7),
            make_event(usn=11, name="b.iso", reason=usn_mod.USN_REASON_FILE_DELETE, parent=999),
        ]
        stats = changes.collect_usn(self.conn, "C:", dir_paths={7: r"Games\Steam"})

        self.assertEqual(stats.resolved_paths, 1)
        rows = {r["name"]: r["path"] for r in self.conn.execute("SELECT name, path FROM usn_events")}
        self.assertEqual(rows["a.iso"], r"Games\Steam\a.iso")
        self.assertIsNone(rows["b.iso"])

    def test_first_run_is_not_a_reset(self) -> None:
        """第一次跑没有历史可丢,不能报成「日志断了」。"""
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        stats = changes.collect_usn(self.conn, "C:")

        self.assertTrue(stats.first_run)
        self.assertFalse(stats.journal_reset)

    def test_second_run_is_neither(self) -> None:
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        changes.collect_usn(self.conn, "C:")
        stats = changes.collect_usn(self.conn, "C:")

        self.assertFalse(stats.first_run)
        self.assertFalse(stats.journal_reset)

    def test_cursor_saved_and_reused(self) -> None:
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        changes.collect_usn(self.conn, "C:")

        cursor = db.get_usn_cursor(self.conn, "C:")
        self.assertEqual(cursor, (111, 11))

        FakeJournal.events = [
            make_event(usn=11, name="b", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        changes.collect_usn(self.conn, "C:")
        # 第二次从上次结束的位置继续,不是从 0 重读
        self.assertEqual(FakeJournal.instances[1].read_from, [11])

    def test_journal_rebuild_resets_cursor(self) -> None:
        self.patch_journal()
        db.set_usn_cursor(self.conn, "C:", 111, 5000)
        self.conn.commit()

        FakeJournal.info = usn_mod.JournalInfo(
            journal_id=222,       # 换了日志
            first_usn=0,
            next_usn=100,
            lowest_valid_usn=0,
            max_usn=1 << 60,
            max_size=32 * MB,
            allocation_delta=4 * MB,
        )
        FakeJournal.events = [
            make_event(usn=1, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        stats = changes.collect_usn(self.conn, "C:")

        self.assertTrue(stats.journal_reset)
        self.assertFalse(stats.first_run)
        self.assertEqual(FakeJournal.instances[0].read_from, [0])
        self.assertEqual(db.get_usn_cursor(self.conn, "C:"), (222, 2))

    def test_cursor_rolled_out_of_window(self) -> None:
        """游标比日志最低有效值还旧,说明中间那段被覆盖了。"""
        self.patch_journal()
        db.set_usn_cursor(self.conn, "C:", 111, 100)
        self.conn.commit()

        FakeJournal.info = usn_mod.JournalInfo(
            journal_id=111,
            first_usn=8000,
            next_usn=9000,
            lowest_valid_usn=8000,
            max_usn=1 << 60,
            max_size=32 * MB,
            allocation_delta=4 * MB,
        )
        FakeJournal.events = [
            make_event(usn=8500, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        ]
        stats = changes.collect_usn(self.conn, "C:")

        self.assertTrue(stats.journal_reset)
        self.assertEqual(FakeJournal.instances[0].read_from, [8000])

    def test_journal_unavailable_is_reported_not_raised(self) -> None:
        self.patch_journal()
        FakeJournal.raise_on_query = usn_mod.JournalUnavailable("C: 没有启用 USN 日志。")
        stats = changes.collect_usn(self.conn, "C:")

        self.assertFalse(stats.available)
        self.assertIn("USN", stats.reason)
        self.assertEqual(stats.events_stored, 0)

    def test_access_denied_is_reported_not_raised(self) -> None:
        from strata.ntfs.volume import AccessDenied

        self.patch_journal()
        FakeJournal.raise_on_query = AccessDenied("需要管理员权限")
        stats = changes.collect_usn(self.conn, "C:")

        self.assertFalse(stats.available)
        self.assertIn("管理员", stats.reason)

    def test_duplicate_usn_not_inserted_twice(self) -> None:
        self.patch_journal()
        event = make_event(usn=10, name="a", reason=usn_mod.USN_REASON_FILE_CREATE)
        FakeJournal.events = [event]
        changes.collect_usn(self.conn, "C:")

        # 游标倒回去重读同一条
        db.set_usn_cursor(self.conn, "C:", 111, 0)
        self.conn.commit()
        changes.collect_usn(self.conn, "C:")

        n = self.conn.execute("SELECT COUNT(*) c FROM usn_events").fetchone()["c"]
        self.assertEqual(n, 1)


class EnrichTest(ChangesFixture):
    def _snapshot_with_file(self, path: str, size: int, taken_at: float) -> int:
        sid = db.insert_snapshot(
            self.conn,
            db.Snapshot(
                drive="C:",
                taken_at=taken_at,
                method="mft",
                total_bytes=100 * MB,
                free_bytes=10 * MB,
                used_bytes=50 * MB,
                scanned_bytes=50 * MB,
            ),
        )
        db.insert_files(self.conn, sid, [db.FileRow(path=path, bytes=size)])
        self.conn.commit()
        return sid

    def test_fills_size_from_snapshot(self) -> None:
        self._snapshot_with_file(r"Downloads\big.iso", 700 * MB, ts_days_ago(3))
        self.add_event_rows(
            [
                db.UsnRow(
                    usn=1,
                    timestamp=ts_days_ago(1),
                    reason=usn_mod.USN_REASON_FILE_DELETE,
                    kind="delete",
                    is_dir=False,
                    name="big.iso",
                    path=r"Downloads\big.iso",
                )
            ]
        )
        filled = changes.enrich_deleted_sizes(self.conn, "C:")

        self.assertEqual(filled, 1)
        row = self.conn.execute("SELECT bytes FROM usn_events").fetchone()
        self.assertEqual(row["bytes"], 700 * MB)

    def test_prefers_newest_snapshot(self) -> None:
        self._snapshot_with_file(r"a\x.bin", 100 * MB, ts_days_ago(9))
        self._snapshot_with_file(r"a\x.bin", 300 * MB, ts_days_ago(2))
        self.add_event_rows(
            [
                db.UsnRow(
                    usn=1,
                    timestamp=ts_days_ago(1),
                    reason=usn_mod.USN_REASON_FILE_DELETE,
                    kind="delete",
                    is_dir=False,
                    name="x.bin",
                    path=r"a\x.bin",
                )
            ]
        )
        changes.enrich_deleted_sizes(self.conn, "C:")
        self.assertEqual(
            self.conn.execute("SELECT bytes FROM usn_events").fetchone()["bytes"],
            300 * MB,
        )

    def test_unmatched_stays_null(self) -> None:
        """快照里没见过的文件宁可留空,也不要瞎猜一个大小。"""
        self._snapshot_with_file(r"a\known.bin", 100 * MB, ts_days_ago(2))
        self.add_event_rows(
            [
                db.UsnRow(
                    usn=1,
                    timestamp=ts_days_ago(1),
                    reason=usn_mod.USN_REASON_FILE_DELETE,
                    kind="delete",
                    is_dir=False,
                    name="ghost.bin",
                    path=r"a\ghost.bin",
                )
            ]
        )
        filled = changes.enrich_deleted_sizes(self.conn, "C:")

        self.assertEqual(filled, 0)
        self.assertIsNone(self.conn.execute("SELECT bytes FROM usn_events").fetchone()["bytes"])

    def test_ignores_creates_and_dirs(self) -> None:
        self._snapshot_with_file(r"a\x.bin", 100 * MB, ts_days_ago(2))
        self.add_event_rows(
            [
                db.UsnRow(
                    usn=1, timestamp=ts_days_ago(1), reason=0, kind="create",
                    is_dir=False, name="x.bin", path=r"a\x.bin",
                ),
                db.UsnRow(
                    usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                    is_dir=True, name="x.bin", path=r"a\x.bin",
                ),
            ]
        )
        self.assertEqual(changes.enrich_deleted_sizes(self.conn, "C:"), 0)


class DailySummaryTest(ChangesFixture):
    def test_counts_by_kind_per_day(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(1), reason=0, kind="create",
                          is_dir=False, name="a"),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="create",
                          is_dir=False, name="b"),
                db.UsnRow(usn=3, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="c", bytes=50 * MB),
                db.UsnRow(usn=4, timestamp=ts_days_ago(2), reason=0, kind="rename_old",
                          is_dir=False, name="d"),
            ]
        )
        summaries = {s.day: s for s in changes.usn_daily_summary(self.conn, "C:")}

        yesterday = summaries[day_str(-1)]
        self.assertEqual(yesterday.created, 2)
        self.assertEqual(yesterday.deleted, 1)
        self.assertEqual(yesterday.deleted_bytes_known, 50 * MB)
        self.assertEqual(summaries[day_str(-2)].renamed, 1)

    def test_unknown_sizes_counted_separately(self) -> None:
        """有多少删除算不出大小,必须如实说,不能混进已知字节里。"""
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="known", bytes=10 * MB),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="unknown1"),
                db.UsnRow(usn=3, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="unknown2"),
            ]
        )
        s = changes.usn_daily_summary(self.conn, "C:")[0]

        self.assertEqual(s.deleted, 3)
        self.assertEqual(s.deleted_bytes_known, 10 * MB)
        self.assertEqual(s.deleted_bytes_unknown_files, 2)

    def test_top_deleted_ranked(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="small", path="a\\small", bytes=MB),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="huge", path="a\\huge", bytes=900 * MB),
            ]
        )
        s = changes.usn_daily_summary(self.conn, "C:", top_n=2)[0]

        self.assertEqual([d["path"] for d in s.top_deleted], ["a\\huge", "a\\small"])

    def test_top_deleted_falls_back_to_name(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="orphan.bin", path=None, bytes=5 * MB),
            ]
        )
        s = changes.usn_daily_summary(self.conn, "C:")[0]
        self.assertEqual(s.top_deleted[0]["path"], "orphan.bin")

    def test_cutoff_excludes_old(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(100), reason=0, kind="delete",
                          is_dir=False, name="ancient"),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="recent"),
            ]
        )
        days = [s.day for s in changes.usn_daily_summary(self.conn, "C:", days=30)]

        self.assertEqual(days, [day_str(-1)])

    def test_sorted_ascending(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=i, timestamp=ts_days_ago(i), reason=0, kind="create",
                          is_dir=False, name=f"f{i}")
                for i in (1, 5, 3)
            ]
        )
        days = [s.day for s in changes.usn_daily_summary(self.conn, "C:")]
        self.assertEqual(days, sorted(days))

    def test_other_drive_excluded(self) -> None:
        self.add_event_rows(
            [db.UsnRow(usn=1, timestamp=ts_days_ago(1), reason=0, kind="delete",
                       is_dir=False, name="d-only")],
            drive="D:",
        )
        self.assertEqual(changes.usn_daily_summary(self.conn, "C:"), [])
        self.assertEqual(len(changes.usn_daily_summary(self.conn, "D:")), 1)

    def test_empty(self) -> None:
        self.assertEqual(changes.usn_daily_summary(self.conn, "C:"), [])

    def test_epoch_timestamp_filtered_by_cutoff(self) -> None:
        """默认窗口下,时间戳为 0 的事件在 SQL 那层就被 cutoff 挡掉了。"""
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=0.0, reason=0, kind="delete",
                          is_dir=False, name="epoch", bytes=MB),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="normal", bytes=2 * MB),
            ]
        )
        summaries = changes.usn_daily_summary(self.conn, "C:")

        self.assertEqual([s.day for s in summaries], [day_str(-1)])
        self.assertEqual(summaries[0].deleted_bytes_known, 2 * MB)

    def test_survives_unrepresentable_day(self) -> None:
        """窗口足够大时,算不出当地午夜的那天要跳过,不能连带整个结果一起崩。

        UTC+8 上 safe_day(0.0) 得到 '1970-01-01',而这一天的当地午夜在 epoch
        之前,mktime 表示不了。默认 30 天窗口下这条事件进不了循环(见上一个测例),
        HTTP 层也把 days 夹在 3650 —— 所以这不是接口上活着的崩溃。但 days 本身
        没有上界,这个函数对任何 days 都该成立:坏的那天只丢自己。
        """
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=0.0, reason=0, kind="delete",
                          is_dir=False, name="epoch", bytes=MB),
                db.UsnRow(usn=2, timestamp=ts_days_ago(1), reason=0, kind="delete",
                          is_dir=False, name="normal", bytes=2 * MB),
            ]
        )

        summaries = changes.usn_daily_summary(self.conn, "C:", days=3_000_000)

        by_day = {s.day: s for s in summaries}
        # 正常那天照常出现,明细也在
        self.assertEqual(by_day[day_str(-1)].deleted_bytes_known, 2 * MB)
        self.assertEqual(len(by_day[day_str(-1)].top_deleted), 1)
        # 1970 那天计数还在,只是没有明细可查
        self.assertEqual(by_day["1970-01-01"].deleted, 1)
        self.assertEqual(by_day["1970-01-01"].top_deleted, [])


class CoverageAndPruneTest(ChangesFixture):
    def test_coverage_empty(self) -> None:
        """一条事件都没有。available/reason 也得在,而且都是 None。

        None 是「不知道」:这个盘没扫过,既没读成也没读失败。默认成 False 会让
        一个没扫过的盘在界面上显示成故障。整个字典比,不比子集 —— 多出来的字段
        应该是有意加的,让它撞一次比悄悄放过好。
        """
        cov = changes.usn_coverage(self.conn, "C:")
        self.assertEqual(cov, {"events": 0, "first_day": None, "last_day": None,
                               "days": 0, "available": None, "reason": None})

    def test_coverage_span(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(6), reason=0, kind="create",
                          is_dir=False, name="a"),
                db.UsnRow(usn=2, timestamp=ts_days_ago(0), reason=0, kind="create",
                          is_dir=False, name="b"),
            ]
        )
        cov = changes.usn_coverage(self.conn, "C:")

        self.assertEqual(cov["events"], 2)
        self.assertEqual(cov["first_day"], day_str(-6))
        self.assertEqual(cov["last_day"], day_str(0))
        self.assertEqual(cov["days"], 7)

    def test_prune_removes_old_only(self) -> None:
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(300), reason=0, kind="create",
                          is_dir=False, name="old"),
                db.UsnRow(usn=2, timestamp=ts_days_ago(10), reason=0, kind="create",
                          is_dir=False, name="new"),
            ]
        )
        removed = changes.prune_usn_events(self.conn, "C:", keep_days=180)

        self.assertEqual(removed, 1)
        names = [r["name"] for r in self.conn.execute("SELECT name FROM usn_events")]
        self.assertEqual(names, ["new"])

    def test_prune_scoped_to_drive(self) -> None:
        old = db.UsnRow(usn=1, timestamp=ts_days_ago(300), reason=0, kind="create",
                        is_dir=False, name="old")
        self.add_event_rows([old], drive="C:")
        self.add_event_rows([old], drive="D:")

        changes.prune_usn_events(self.conn, "C:", keep_days=180)
        rows = list(self.conn.execute("SELECT drive FROM usn_events"))
        self.assertEqual([r["drive"] for r in rows], ["D:"])

    def test_collect_usn_actually_calls_prune(self) -> None:
        """上面两条测试直接调 prune,一直是绿的 —— 但生产从来没人调它。

        这是「测过但没接上」:函数写了、测试写了、默认 keep_days=180 也写了,
        而 grep 整个 src 只有定义、没有调用点,所以那个 180 天从来没生效过,
        usn_events 表无上限地长。跟 dir_paths 是同一类事故,而且两处都是
        测试给了假信心 —— 测试证明的是「函数能用」,不是「功能在跑」。
        这条测的是后者:清理必须挂在正常扫描流程上,不然它等于不存在。
        """
        self.add_event_rows(
            [
                db.UsnRow(usn=1, timestamp=ts_days_ago(400), reason=0, kind="create",
                          is_dir=False, name="ancient"),
            ]
        )
        # 日志读不出来也不影响清理 —— 这里没有真日志,collect_usn 会走
        # JournalUnavailable 那一支。清理照样得做:它跟能不能读日志无关。
        changes.collect_usn(self.conn, "C:")
        names = [r["name"] for r in self.conn.execute("SELECT name FROM usn_events")]
        self.assertNotIn("ancient", names, "collect_usn 没有清理过期事件")


class ResolverWiredIntoCollectTest(ChangesFixture):
    """反查有没有真的接在采集流程上。

    test_usn_path_resolve.py 测的是 DirPathResolver 本身能用,这里测的是
    它在 collect_usn 里被用上了 —— 两件事,而且这个项目里后者栽过两次
    (dir_paths 有字段没接、prune_usn_events 有函数没人调)。
    单元测试给的是「零件合格」的信心,装没装上得单独证。
    """

    def fake_opener(self, table):
        def opener(ref: int) -> str | None:
            return table.get(ref)

        return opener

    def test_resolver_fills_what_dir_paths_cannot(self) -> None:
        """dir_paths 认不出的父目录,反查得接上。

        这是这条改动的正题:scandir 那条路给不出 dir_paths(取编号太贵),
        所以日常扫描下第一条路是空的,全靠反查。
        """
        self.patch_journal()
        FakeJournal.events = [
            make_event(
                usn=10, name="big.iso", reason=usn_mod.USN_REASON_FILE_DELETE,
                parent=7, parent_full=0x0002_0000_0000_0007,
            )
        ]
        stats = changes.collect_usn(
            self.conn,
            "C:",
            dir_paths=None,           # scandir 扫的,没有这张表
            opener=self.fake_opener({0x0002_0000_0000_0007: r"\\?\C:\Games\Steam"}),
        )

        row = self.conn.execute("SELECT path FROM usn_events").fetchone()
        self.assertEqual(row["path"], r"Games\Steam\big.iso")
        self.assertEqual(stats.resolved_paths, 1)
        self.assertEqual(stats.lookups_ok, 1)

    def test_dir_paths_wins_and_skips_the_syscall(self) -> None:
        """能查字典就别发系统调用 —— 顺序反了就是拿 91 微秒换免费答案。"""
        self.patch_journal()
        FakeJournal.events = [
            make_event(
                usn=10, name="a.iso", reason=usn_mod.USN_REASON_FILE_DELETE,
                parent=7, parent_full=0x0002_0000_0000_0007,
            )
        ]
        called: list[int] = []

        def counting_opener(ref: int) -> str | None:
            called.append(ref)
            return r"\\?\C:\WRONG"

        stats = changes.collect_usn(
            self.conn, "C:", dir_paths={7: r"Games\Steam"}, opener=counting_opener
        )

        row = self.conn.execute("SELECT path FROM usn_events").fetchone()
        self.assertEqual(row["path"], r"Games\Steam\a.iso")
        self.assertEqual(called, [], "dir_paths 已经有答案了,不该再问系统")
        self.assertEqual(stats.lookups_ok, 0)

    def test_unresolvable_parent_leaves_path_null(self) -> None:
        """两条路都还不回来就留空。事件照存 —— 少个路径比少条记录好。"""
        self.patch_journal()
        FakeJournal.events = [
            make_event(
                usn=10, name="ghost.iso", reason=usn_mod.USN_REASON_FILE_DELETE,
                parent=7, parent_full=0x0002_0000_0000_0007,
            )
        ]
        stats = changes.collect_usn(
            self.conn, "C:", dir_paths=None, opener=self.fake_opener({})
        )

        row = self.conn.execute("SELECT name, path FROM usn_events").fetchone()
        self.assertEqual(row["name"], "ghost.iso")
        self.assertIsNone(row["path"])
        self.assertEqual(stats.lookups_failed, 1)
        self.assertEqual(stats.events_stored, 1)

    def test_opener_none_degrades_quietly(self) -> None:
        self.patch_journal()
        FakeJournal.events = [
            make_event(
                usn=10, name="x", reason=usn_mod.USN_REASON_FILE_DELETE,
                parent=7, parent_full=0x0002_0000_0000_0007,
            )
        ]
        stats = changes.collect_usn(self.conn, "C:", dir_paths=None, opener=None)

        self.assertEqual(stats.events_stored, 1)
        self.assertEqual(stats.lookups_ok, 0)
        self.assertIsNotNone(stats.resolver_reason)

    def test_default_actually_builds_a_real_opener(self) -> None:
        """默认值必须是「去开一个」,不能是 None。

        这条是整组里最要紧的一条。前两次事故都长一个样:参数在、文档写了、
        单元测试绿着,而生产上那个参数从来没拿到过真货 —— 因为默认值让
        「没接上」和「接上了但这次没结果」在外面看起来一模一样。
        所以这里不看签名的默认值写了什么,直接看它有没有真去构造。
        """
        self.patch_journal()
        FakeJournal.events = [
            make_event(usn=10, name="x", reason=usn_mod.USN_REASON_FILE_DELETE)
        ]
        built: list[str] = []
        original = changes.fileid.FileIdOpener

        class SpyOpener:
            def __init__(self, drive: str) -> None:
                built.append(drive)

            def path_of(self, ref: int) -> str | None:
                return None

            def close(self) -> None:
                pass

        changes.fileid.FileIdOpener = SpyOpener
        self.addCleanup(lambda: setattr(changes.fileid, "FileIdOpener", original))

        # 注意:不传 opener
        changes.collect_usn(self.conn, "C:", dir_paths=None)

        self.assertEqual(built, ["C:"], "默认没有去开 opener,反查等于没接上")


if __name__ == "__main__":
    unittest.main()
