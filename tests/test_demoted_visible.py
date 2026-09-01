"""「明细已精简」这件事,得有一处定义、一处判断,并且能从接口问出来。

降级(db.demote_snapshot)会清空 files 整张表、删掉深处的小目录行,然后在
note 里留一个 '[已降级]' 标记。这个中文串曾经被内联在四个地方:这个 db.py 里
三处(拼接、两处 NOT LIKE),analysis/diff.py 里一处。抄四遍的代价不是难看,
是漏改 —— 少改一处,那一处就永远判 False,不报错、不抛异常,只是界面上少说
一句话。所以标记和判断各只留一份。

顺带说一件**没做**的事,免得下一个人重做一遍:

原本想修的是树图的空白提示。降级过的快照钻到深处会得到一张空图,正中间写着
「这里还没有数据,先扫一次」—— 两半都不对:数据有过,是明细被精简了;而
「先扫一次」是反向指路,扫一次会产生新快照并把这个降得更狠。

但这个状态**现在的界面到不了**。树图只由 enterPath() 加载,它给 /api/tree
不传 snapshot,所以永远画最新快照;而 demote_previous_snapshots 从不降级最新
那个。也就是说换了文案也永远不会被显示 —— 一条永远不会响的提示和一条永远
通过的检查一样没用。真正缺的是「让树图指向某个旧快照」这个入口
(/api/tree 的 snapshot 参数后端早就实现了,前端没有用它)。

所以这里只测能真正生效的两层:
  1. db 层:标记有唯一定义(DEMOTED_MARK)和唯一判断(is_demoted)
  2. api 层:快照载荷带 demoted 字段,降级过就是 True
"""

from __future__ import annotations

import unittest
from pathlib import Path

from strata.server import api
from strata.store import db

ROOT = Path(__file__).resolve().parents[1]
GB = 1024**3


def make_snapshot(conn, *, drive: str = "C:", taken_at: float = 1000.0) -> int:
    snap = db.Snapshot(
        drive=drive, taken_at=taken_at, method="mft",
        total_bytes=200 * GB, free_bytes=37 * GB, used_bytes=163 * GB,
        scanned_bytes=163 * GB, complete=True,
    )
    return db.insert_snapshot(conn, snap)


class MarkerHasOneDefinitionTest(unittest.TestCase):
    """'[已降级]' 这个串只能有一处定义、一处判断。

    钉这一条是因为它已经被抄了四遍(db.py 三处 + diff.py 一处)。抄第五遍的
    代价不是难看,是「改措辞时漏掉一处」—— 而漏掉的那处会安静地永远判 False,
    不报错。
    """

    def test_marker_constant_exists(self):
        self.assertTrue(hasattr(db, "DEMOTED_MARK"), "db 里没有 DEMOTED_MARK")
        self.assertIn("降级", db.DEMOTED_MARK)

    def test_is_demoted_recognises_the_mark(self):
        self.assertTrue(db.is_demoted(f"扫描完成 {db.DEMOTED_MARK}"))
        self.assertTrue(db.is_demoted(db.DEMOTED_MARK))

    def test_is_demoted_says_no_for_ordinary_notes(self):
        self.assertFalse(db.is_demoted(None))
        self.assertFalse(db.is_demoted(""))
        self.assertFalse(db.is_demoted("扫描完成,9 个目录读取被拒"))

    def test_demote_snapshot_sets_it(self):
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        sid = make_snapshot(conn)
        row = db.get_snapshot(conn, sid)
        self.assertFalse(db.is_demoted(row["note"]), "还没降级就带标记了")

        db.demote_snapshot(conn, sid)
        row = db.get_snapshot(conn, sid)
        self.assertTrue(db.is_demoted(row["note"]),
                        f"降级之后 note 里没有标记:{row['note']!r}")

    def test_demoting_twice_does_not_double_the_mark(self):
        """标记只加一次 —— 加两遍虽然不影响判断,但 note 会越来越长。"""
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        sid = make_snapshot(conn)
        db.demote_snapshot(conn, sid)
        db.demote_snapshot(conn, sid)
        note = db.get_snapshot(conn, sid)["note"] or ""
        self.assertEqual(note.count(db.DEMOTED_MARK), 1, note)


class ApiExposesTheFlagTest(unittest.TestCase):
    """前端要的是布尔量,不是让它去解析中文 note。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.sid = make_snapshot(self.conn)

    def test_payload_has_demoted_false_by_default(self):
        row = db.get_snapshot(self.conn, self.sid)
        payload = api._snapshot_payload(row)
        self.assertIn("demoted", payload, "快照载荷里没有 demoted 字段")
        self.assertIs(payload["demoted"], False)

    def test_payload_has_demoted_true_after_demotion(self):
        db.demote_snapshot(self.conn, self.sid)
        row = db.get_snapshot(self.conn, self.sid)
        self.assertIs(api._snapshot_payload(row)["demoted"], True)

    def test_it_is_a_real_bool_not_a_string(self):
        """JSON 里必须是 true/false。给个非空字符串前端也会当真,
        但 `if (snap.demoted)` 对 '' 和 '[已降级]' 的判断就全靠 note 内容了。"""
        db.demote_snapshot(self.conn, self.sid)
        row = db.get_snapshot(self.conn, self.sid)
        self.assertIsInstance(api._snapshot_payload(row)["demoted"], bool)

    def test_tree_endpoint_carries_it(self):
        """/api/tree 的响应里要带上 —— 树图就是从这个响应拿数据的。"""
        db.demote_snapshot(self.conn, self.sid)
        out = api.get_tree(self.conn, {"drive": ["C:"], "snapshot": [str(self.sid)]})
        self.assertIsNotNone(out["snapshot"], "响应里没有 snapshot")
        self.assertIs(out["snapshot"]["demoted"], True)



if __name__ == "__main__":
    unittest.main()
