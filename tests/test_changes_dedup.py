"""「消失的东西」这张表里,一个文件只该占一行。

USN 日志记的是操作,不是文件。一次「建-写-关-删」的生命周期会留下好几条记录,
reason 位一路累加:0x100(创建)→ 0x102 → 0x103 → 0x80000103(关闭)→
0x80000200(关闭+删除)。每条有各自的 USN,所以 ON CONFLICT(drive, usn)
挡不住它们 —— 它们本来就不是重复行,是同一个文件的不同时刻。

本机实盘量到的样子:

    delete 事件 32,031 条,其中带路径的 450 条 → 去重后只有 405 条不同路径
    bc_09.db-journal 出现 115 次,某安装包临时目录 45 次,.git\\index.lock 21 次

10% 听起来不多,但它砸的位置很准。界面按 COALESCE(bytes,0) DESC 排,而同一个
文件的多条记录字节数一样,于是它们连着占据榜首 —— 实测第一名和第二名是同一个
bootstrap-aarch64.zip(32.2 MB)。用户唯一会细看的就是前几行。

去重的口径只能是路径,不能是文件名:名字重的是真·不同文件。实盘上 `Editor`
出现 786 次、`Assets` 465 次、`manifest.xml` 387 次 —— 那是几百个不同目录里的
同名文件,按名字合并会凭空捏出「这个文件被删了 786 次」的假话。所以路径为空的
行原样留着,一行都不合并。
"""

from __future__ import annotations

import time
import unittest

from strata.ntfs import usn as usn_mod
from strata.scan import changes as changes_mod
from strata.server import api
from strata.store import db

BACK = "\\"
LIFECYCLE = (0x100, 0x102, 0x103, 0x80000103, 0x80000200)


def ev(usn: int, *, at: float, path: str | None, size: int | None = None,
       kind: str = usn_mod.KIND_DELETE, reason: int = 0x80000200,
       is_dir: bool = False, name: str | None = None) -> db.UsnRow:
    """造一条 USN 记录。name 默认从 path 取末段,和真实数据一致。"""
    if name is None:
        name = path.rsplit(BACK, 1)[-1] if path else "?"
    return db.UsnRow(
        usn=usn, timestamp=at, reason=reason, kind=kind,
        is_dir=is_dir, name=name, path=path, bytes=size,
    )


class DeletedListCollapsesOneFileToOneRow(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = time.time()

    def add(self, rows: list[db.UsnRow]) -> None:
        db.insert_usn_events(self.conn, "D:", rows)
        self.conn.commit()

    def changes(self, **kw: str) -> list[dict]:
        params = {k: [v] for k, v in kw.items()}
        params.setdefault("drive", ["D:"])
        return api.get_changes(self.conn, params)["events"]

    def test_one_lifecycle_is_one_row(self) -> None:
        """一次「建-写-关-删」5 条记录,列表里只该有 1 行。"""
        p = "AI" + BACK + "build" + BACK + "bootstrap.zip"
        self.add([
            ev(1000 + i, at=self.now - 60 + i, path=p, size=32209189, reason=r)
            for i, r in enumerate(LIFECYCLE)
        ])
        got = self.changes()
        self.assertEqual(
            [e["path"] for e in got], [p],
            "同一个文件的多条 USN 记录没有合并 —— 界面上它会连着占好几行",
        )

    def test_collapsed_row_keeps_the_latest_time_and_the_known_size(self) -> None:
        """合并不能丢信息:时间取最后一次,字节数取查得到的那个。

        字节数是从历史快照反查来的,不是每条记录都有(USN 日志本身不记大小)。
        同一个文件的 5 条记录里可能只有 1 条补到了值,取 MAX 才不会把它丢成空。
        """
        p = "qq" + BACK + "profile_info.db-wal"
        self.add([
            ev(2001, at=self.now - 300, path=p, size=None),
            ev(2002, at=self.now - 200, path=p, size=3502032),
            ev(2003, at=self.now - 100, path=p, size=None),
        ])
        got = self.changes()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["bytes"], 3502032,
                         "三条里只有一条反查到了大小,合并后不该变成空")
        self.assertAlmostEqual(got[0]["at"], self.now - 100, places=3,
                               msg="时间该是最后一次,不是第一次")

    def test_rows_without_a_path_are_never_merged_by_name(self) -> None:
        """没解析出路径的行原样留着 —— 名字一样不代表是同一个文件。

        实盘上 `Editor` 出现 786 次,是几百个不同目录里的同名文件。按名字合并
        会说「Editor 被删了 786 次」,那是凭空捏的。
        """
        self.add([ev(3000 + i, at=self.now - i, path=None, name="Editor")
                  for i in range(4)])
        got = self.changes()
        self.assertEqual(len(got), 4,
                         "路径为空的行被按名字合并了 —— 这会把不同文件说成同一个")

    def test_a_path_and_a_pathless_row_sharing_a_name_stay_apart(self) -> None:
        """一条有路径、一条没有,名字相同也不能凑成一行。"""
        self.add([
            ev(4001, at=self.now - 50, path="proj" + BACK + "Editor", name="Editor"),
            ev(4002, at=self.now - 40, path=None, name="Editor"),
        ])
        self.assertEqual(len(self.changes()), 2)

    def test_distinct_paths_are_all_kept(self) -> None:
        """别把去重做成了「只剩一行」。"""
        self.add([ev(5000 + i, at=self.now - i, path="d" + BACK + f"f{i}.bin",
                     size=i * 1000) for i in range(6)])
        got = self.changes()
        self.assertEqual(len(got), 6)
        self.assertEqual(
            [e["bytes"] for e in got], [5000, 4000, 3000, 2000, 1000, 0],
            "还是该按字节数从大到小",
        )

    def test_limit_counts_files_not_records(self) -> None:
        """limit=3 该给 3 个不同文件,而不是 3 条挤在一个文件上的记录。

        没去重的时候,limit=200 可能只覆盖到几十个文件 —— 用户以为看到了 200 个,
        其实列表被少数几个高频临时文件占满了。
        """
        rows = []
        for f in range(5):
            p = "d" + BACK + f"f{f}.bin"
            for i, r in enumerate(LIFECYCLE):
                rows.append(ev(6000 + f * 10 + i, at=self.now - f,
                               path=p, size=(5 - f) * 1000, reason=r))
        self.add(rows)
        got = self.changes(limit="3")
        self.assertEqual(len(got), 3)
        self.assertEqual(len({e["path"] for e in got}), 3,
                         "limit 切的还是记录数,不是文件数")

    def test_how_many_records_collapsed_is_reported(self) -> None:
        """折叠掉的次数要说出来,不能默默吞掉。

        一个文件在 30 天里被删了 115 次,这件事本身有用 —— 它说明这是个反复重建
        的临时文件,不是「丢了个东西」。藏起来就等于把信息删了。
        """
        p = "d" + BACK + "bc_09.db-journal"
        self.add([ev(7000 + i, at=self.now - i, path=p, reason=r)
                  for i, r in enumerate(LIFECYCLE)])
        got = self.changes()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["count"], len(LIFECYCLE),
                         "合并了 5 条,得让界面知道是 5 条")

    def test_a_single_record_reports_count_one(self) -> None:
        self.add([ev(8001, at=self.now, path="d" + BACK + "only.txt", size=1)])
        self.assertEqual(self.changes()[0]["count"], 1)

    def test_other_kinds_dedup_too(self) -> None:
        """create 也一样:52,489 条里带路径的 3,011 条,同样有重复。"""
        p = "d" + BACK + "made.tmp"
        self.add([ev(9000 + i, at=self.now - i, path=p,
                     kind=usn_mod.KIND_CREATE, reason=0x100) for i in range(4)])
        got = self.changes(kind="create")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["count"], 4)

    def test_usn_reported_is_the_latest_one(self) -> None:
        """行的身份用最后一次的 USN —— 前端拿它当 key,得稳定且唯一。"""
        p = "d" + BACK + "x.bin"
        self.add([ev(u, at=self.now - 10, path=p) for u in (100, 300, 200)])
        self.assertEqual(self.changes()[0]["usn"], 300)


class ResolvedPathsComeFirst(unittest.TestCase):
    """解析出路径的排在前面。名额有限,别让查不到路径的把它占满。

    实盘上量到的:D: 盘去重后有 405 条带路径的删除记录,而界面一次放 200 行 ——
    其中 115 行给了没路径的条目。那种行上什么都没有:没路径、没大小(大小是按
    路径去历史快照反查的,没路径就必然反查不到)、右键菜单也定位不了任何东西。
    用户能用的只剩 85 行,另外 320 条带路径的被挤掉了。

    为什么会挤:排序是 COALESCE(bytes,0) DESC, timestamp DESC,而实盘上只有 4 行
    反查到了大小,剩下 196 行全靠时间排 —— 有路径的和没路径的就这么交替混在一起:

        PPPPPPPPPPPPPPPPPPPPPP..........PPPPPPPPPPPPPPP...
        .......P.P.PPPPPPPPPPPPPPP...........PPPP.PPPPPPPP
        PPPPPPPPPPPPPPPPPP.P..............................
        ..................................................

    加一档排序键:先按大小,再按「有没有路径」,最后按时间。这样 limit 切掉的
    是最没用的那些行。

    不删它们 —— 「有个叫这名字的东西被删了」也是信息,删掉等于替用户断言没发生过。
    只是往后排,再在界面上说清这行为什么只有名字。
    """

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = time.time()

    def test_pathless_rows_sort_after_resolved_ones(self) -> None:
        """没路径的时间更新也得排在后面。"""
        db.insert_usn_events(self.conn, "D:", [
            # 没路径的更新,原来会靠 timestamp DESC 排到前面
            ev(1, at=self.now - 1, path=None, name="classes"),
            ev(2, at=self.now - 2, path=None, name="binary"),
            ev(3, at=self.now - 900, path="proj" + BACK + "real.log"),
        ])
        self.conn.commit()
        ev_out = api.get_changes(self.conn, {"drive": ["D:"]})["events"]
        self.assertEqual(
            [bool(e["path"]) for e in ev_out], [True, False, False],
            "有路径的没排在前面 —— limit 一切,能用的行先被切掉",
        )

    def test_size_still_wins_over_having_a_path(self) -> None:
        """大小仍然是第一档:反查到 3 GB 的那行必须在最前面。"""
        db.insert_usn_events(self.conn, "D:", [
            ev(10, at=self.now, path=None, name="nameless"),
            ev(11, at=self.now, path="a" + BACK + "small.bin", size=10),
            ev(12, at=self.now, path="a" + BACK + "huge.bin", size=3_000_000_000),
        ])
        self.conn.commit()
        got = api.get_changes(self.conn, {"drive": ["D:"]})["events"]
        self.assertEqual([e["bytes"] for e in got], [3_000_000_000, 10, None])

    def test_limit_spends_its_budget_on_resolved_rows(self) -> None:
        """limit=3 的时候,3 个名额该给有路径的 —— 哪怕没路径的时间更新。"""
        rows = [ev(100 + i, at=self.now - i, path=None, name=f"n{i}")
                for i in range(5)]
        rows += [ev(200 + i, at=self.now - 500 - i, path="d" + BACK + f"f{i}.log")
                 for i in range(4)]
        db.insert_usn_events(self.conn, "D:", rows)
        self.conn.commit()
        got = api.get_changes(self.conn, {"drive": ["D:"], "limit": ["3"]})["events"]
        self.assertTrue(all(e["path"] for e in got),
                        f"名额被没路径的占了:{[e['path'] or e['name'] for e in got]}")

    def test_pathless_rows_are_still_there(self) -> None:
        """往后排,不是删掉。「有个叫这名字的东西被删了」也是信息。"""
        db.insert_usn_events(self.conn, "D:", [
            ev(300, at=self.now, path="d" + BACK + "a.log"),
            ev(301, at=self.now, path=None, name="gone"),
        ])
        self.conn.commit()
        got = api.get_changes(self.conn, {"drive": ["D:"]})["events"]
        self.assertEqual(len(got), 2)
        self.assertEqual(got[-1]["name"], "gone")


class DailyTopDeletedCollapsesToo(unittest.TestCase):
    """时间轴那一栏走的是另一条查询,同一个毛病要一起治。

    changes.usn_daily_summary 里的 top_deleted 用了一模一样的
    ORDER BY COALESCE(bytes,0) DESC —— top_n=5 的时候,5 格全被同一个
    文件占满是很容易的事。
    """

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.now = time.time() - 3600      # 往回挪一小时,躲开跨天边界

    def test_top_deleted_shows_five_different_files(self) -> None:
        rows = []
        for f in range(3):
            p = "d" + BACK + f"f{f}.bin"
            for i, r in enumerate(LIFECYCLE):
                rows.append(ev(100 + f * 10 + i, at=self.now - f,
                               path=p, size=(3 - f) * 1000, reason=r))
        db.insert_usn_events(self.conn, "D:", rows)
        self.conn.commit()

        days = changes_mod.usn_daily_summary(self.conn, "D:", days=2, top_n=5)
        top = [t for d in days for t in d.top_deleted]
        self.assertTrue(top, "造了删除事件,这天该有明细")
        paths = [t["path"] for t in top]
        self.assertEqual(len(paths), len(set(paths)),
                         f"同一个文件占了好几格:{paths}")

    def test_top_deleted_keeps_the_known_size(self) -> None:
        """反查到的大小挂在**较早**那条上,合并后照样得留住。

        顺序是刻意的。SQLite 允许在 GROUP BY 里裸取列,取到的是产生 MAX() 的那行 ——
        要是把有大小的记录放在最后,裸取 bytes 也能碰对(它正好是 MAX(timestamp)
        那行),这个测试就变成了永远通过。放在前面才真的在验 MAX(bytes)。
        """
        p = "d" + BACK + "big.zip"
        db.insert_usn_events(self.conn, "D:", [
            ev(201, at=self.now - 20, path=p, size=999000),
            ev(202, at=self.now - 10, path=p, size=None),
        ])
        self.conn.commit()
        days = changes_mod.usn_daily_summary(self.conn, "D:", days=2, top_n=5)
        top = [t for d in days for t in d.top_deleted]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["bytes"], 999000)

    def test_top_deleted_does_not_merge_pathless_rows(self) -> None:
        db.insert_usn_events(self.conn, "D:", [
            ev(300 + i, at=self.now - i, path=None, name="Assets")
            for i in range(3)
        ])
        self.conn.commit()
        days = changes_mod.usn_daily_summary(self.conn, "D:", days=2, top_n=5)
        top = [t for d in days for t in d.top_deleted]
        self.assertEqual(len(top), 3,
                         "按名字合并了 —— 不同目录里的同名文件会被说成一个")


if __name__ == "__main__":
    unittest.main()
