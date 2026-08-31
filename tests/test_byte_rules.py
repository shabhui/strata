"""换了字节口径之后,别把口径变化显示成硬盘变化。

WOF 那个修法(见 tests/test_wof_compression.py)让 mft 那条路对 C: 少报约
38.5 GiB —— 因为原来把 Compact OS 压过的文件按解压后的大小算了。修得对,
但它带来一个新问题,而且正好打在这个工具的立命之本上:

    修之前的快照   196.7G
    修之后的快照   约 158G

下一次扫描完,对比页会显示「减少 38 GB」。**硬盘上什么都没发生。**
一个专门回答「我的空间去哪了」的工具,如果把自己的口径调整说成用户删了
38 GB,那比不给数字更糟 —— 用户会去找那 38 GB,或者以为清理生效了。

已有的 mixedMethod 提示挡不住这一类:那条看的是 method 变了没
(mft ↔ scandir),而这里两边都是 mft,变的是同一条路的算法。

做法:在 meta 里记一个分界快照号 —— 它之前的快照按老口径算,之后的按新口径。
不加表列,因为库里没有 ALTER TABLE 那套迁移机制(schema.sql 是
CREATE TABLE IF NOT EXISTS,每次连库重跑一遍,加列不会自动生效),
为了一个布尔量引进迁移机制不值得。

分界只在第一次见到这个库时定,定成「当前最大快照号 + 1」:已经在库里的
一律算老口径,以后扫的算新口径。空库定成 1,于是全是新口径。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.analysis import diff  # noqa: E402
from strata.store import db  # noqa: E402

from .test_analysis import MB, AnalysisFixture, ts_at  # noqa: E402


class BoundaryRecordedTest(unittest.TestCase):
    """分界值怎么定、什么时候不许再动。"""

    def test_empty_database_starts_at_one(self):
        """空库:第一个快照就是新口径,分界是 1。"""
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertEqual(db.byte_rules_boundary(conn), 1)

    def test_existing_snapshots_are_all_old_rules(self):
        """已经在库里的快照按老口径算 —— 它们确实是老代码扫的。

        这条是这个机制的全部意义:升级之后第一次连库,必须把历史划到分界
        之前去。划错了就没有提示,那 38 GB 的假跌照样显示。
        """
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        for i in range(3):
            db.insert_snapshot(conn, _snap(taken_at=1_700_000_000.0 + i))
        # 模拟「带着老快照的库第一次被新代码打开」:先抹掉标记,再走一遍
        # connect() 会走的那一步。少了后面这句,读到的是「缺键」的兜底值,
        # 测的就不是这件事了。
        conn.execute("DELETE FROM meta WHERE key = ?", (db.BYTE_RULES_KEY,))
        db.ensure_byte_rules_boundary(conn)
        self.assertEqual(db.byte_rules_boundary(conn), 4)

    def test_boundary_does_not_move_on_later_connects(self):
        """定了就不许再动。每次连库重算的话,分界会一直追着最新快照跑,
        于是永远没有「跨口径」的一对,提示永远不出现 —— 又是一条永远通过的检查。
        """
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        first = db.byte_rules_boundary(conn)
        for i in range(3):
            db.insert_snapshot(conn, _snap(taken_at=1_700_000_000.0 + i))
        db.ensure_byte_rules_boundary(conn)          # 再走一遍,模拟重连
        self.assertEqual(db.byte_rules_boundary(conn), first)


def _snap(*, taken_at: float, method: str = "mft") -> db.Snapshot:
    return db.Snapshot(
        drive="C:",
        taken_at=taken_at,
        method=method,
        total_bytes=200 * 1024**3,
        free_bytes=30 * 1024**3,
        used_bytes=100 * MB,
        scanned_bytes=100 * MB,
        complete=True,
        note=None,
    )


class RulesChangedCaveatTest(AnalysisFixture):
    """跨口径对比要出提示,同口径不要出。"""

    def _pair(self, *, method: str = "mft"):
        a = self.add_snapshot(
            taken_at=ts_at(-1), scanned=196 * MB, dirs={"A": 100 * MB},
            files={"A\\x": 100 * MB}, method=method,
        )
        b = self.add_snapshot(
            taken_at=ts_at(0), scanned=158 * MB, dirs={"A": 62 * MB},
            files={"A\\x": 62 * MB}, method=method,
        )
        return a, b

    def _set_boundary(self, value: int) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (db.BYTE_RULES_KEY, str(value)),
        )

    def test_caveat_when_the_pair_straddles_the_boundary(self):
        a, b = self._pair()
        self._set_boundary(b)                 # a 在分界前,b 在分界上
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertIn("rulesChanged", [c["code"] for c in d.caveats])

    def test_no_caveat_when_both_are_new(self):
        a, b = self._pair()
        self._set_boundary(a)                 # 两个都在分界之后
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertNotIn("rulesChanged", [c["code"] for c in d.caveats])

    def test_no_caveat_when_both_are_old(self):
        a, b = self._pair()
        self._set_boundary(b + 1)             # 两个都在分界之前
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertNotIn("rulesChanged", [c["code"] for c in d.caveats])

    def test_no_caveat_for_a_scandir_pair(self):
        """口径只在 mft 那条路上变过。scandir 两边都是 st_size,没变。

        不加这个条件的话,所有跨越升级点的对比都会挂上这句话,包括根本
        不受影响的 scandir 对比 —— 到处都出现的提示等于没有提示。
        """
        a, b = self._pair(method="scandir")
        self._set_boundary(b)
        d = diff.diff_snapshots(self.conn, a, b)
        self.assertNotIn("rulesChanged", [c["code"] for c in d.caveats])

    def test_caveat_says_how_much_and_why(self):
        """提示得带上「哪个方向、大概多少」,否则用户没法判断该不该找那 38 GB。

        代号 + 参数,措辞在 web/i18n.js —— 后端不知道人在看哪种语言。
        """
        a, b = self._pair()
        self._set_boundary(b)
        d = diff.diff_snapshots(self.conn, a, b)
        note = next(c for c in d.caveats if c["code"] == "rulesChanged")
        self.assertIn("vars", note)
        self.assertEqual(note["vars"]["method"], "mft")


if __name__ == "__main__":
    unittest.main()
