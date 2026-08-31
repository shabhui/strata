"""连库时要把页缓存开大。

SQLite 默认 cache_size 是 2 MB(实测真库上 PRAGMA cache_size 返回 -2000,
负数以 KiB 计)。而 dirs 表是 WITHOUT ROWID、主键 (snapshot_id, path) 是文本,
整行存在按路径排序的 B 树里,另有两个索引 —— 每行要插三棵 B 树。prune_tree
产出的路径是乱序的,每行落在 B 树随机位置,2 MB 缓存装不下索引就反复换页。

实测(tools/bench_dbwrite_order.py,64,795 行 = 真实快照 #9 的 dirs 行数):

    乱序 + 默认 cache(2 MB)     1.94s   30.0 µs/行
    乱序 + cache 64 MiB         0.53s    8.2 µs/行   快 3.66x
    按主键排序 + 默认 cache       3.19s   49.2 µs/行   反而更慢
    按主键排序 + cache 64 MiB     3.04s   47.0 µs/行

排序反而慢:主键那棵树变成顺序追加了,但两个二级索引(bytes DESC、depth)
的插入顺序还是乱的,而排序本身又要花时间。所以只改 cache_size。

行数越多差距越大 —— 381,272 行时默认配置掉到 94.7 µs/行(prof_dbwrite.py)。
D: 盘有 203,997 个目录,比 C: 的 64,795 多两倍,受益更明显。

为什么放在 connect() 而不是 schema.sql:cache_size 不是持久设置,它是
每个连接各自的,写在 schema.sql 里只对建库那一次的连接生效,之后每次
连库都是默认值。这类「看着像设置了其实没生效」的写法,和永远通过的
检查是同一类问题。
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import unittest

from strata.store import db

# 至少要这么大(KiB)。写成下界而不是等值:以后想调大不该来改测试。
MIN_CACHE_KIB = 32 * 1024


class ConnectSetsABigEnoughCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="strata_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_fresh_database_gets_a_big_cache(self) -> None:
        conn = db.connect(self.tmp / "new.db")
        self.addCleanup(conn.close)
        got = int(conn.execute("PRAGMA cache_size").fetchone()[0])
        self.assertLess(
            got, 0,
            f"cache_size = {got},正数是「页数」,页大小一变含义就变了。"
            f"要用负数(以 KiB 计)才跟页大小无关",
        )
        self.assertGreaterEqual(
            -got, MIN_CACHE_KIB,
            f"cache_size 只有 {-got} KiB。默认 2 MB 装不下 dirs 的索引,"
            f"实测 64,795 行时 30.0 µs/行,开大到 64 MiB 是 8.2 µs/行",
        )

    def test_reopening_an_existing_database_also_gets_it(self) -> None:
        """这条是关键:cache_size 是连接级的,不跟着库文件走。

        写在 schema.sql 里的话,只有建库那一次的连接有大缓存,之后每次
        连库都退回 2 MB —— 而真实使用里几乎每次都是「打开已有的库」。
        """
        path = self.tmp / "again.db"
        first = db.connect(path)
        first.close()

        second = db.connect(path)
        self.addCleanup(second.close)
        got = int(second.execute("PRAGMA cache_size").fetchone()[0])
        self.assertGreaterEqual(
            -got, MIN_CACHE_KIB,
            f"重开已有的库只拿到 {-got} KiB —— 说明这个设置只在建库时生效,"
            f"而真实使用里几乎每次都是重开",
        )

    def test_in_memory_database_too(self) -> None:
        """测试大量用 :memory:,别让它们走一条和生产不同的路。"""
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        got = int(conn.execute("PRAGMA cache_size").fetchone()[0])
        self.assertGreaterEqual(-got, MIN_CACHE_KIB)


if __name__ == "__main__":
    unittest.main()
