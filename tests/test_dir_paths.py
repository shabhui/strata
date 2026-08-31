"""USN 事件要能还原出路径,靠的是 {目录编号 → 相对路径} 这张表。

背景,也是这批测试存在的理由:`collect_usn(conn, drive, dir_paths=...)` 这个
参数从一开始就在,changes.py 里有 `_compose_path` 用它,tests/test_changes.py
拿手写的 `{7: "Games\\Steam"}` 测过它 —— 但**代码库里没有任何地方产出过真值**。
两个调用点(server/app.py、__main__.py)写的都是
`getattr(result, "dir_paths", None)`,而 ScanResult 上根本没这个字段,
所以两条扫描路径拿到的都恒等于 None。

后果不是报错,是静默降级:每条 USN 事件的 path 存成 NULL,
而 enrich_deleted_sizes 要求 `path IS NOT NULL` 才填大小 —— 于是「消失了什么」
面板只能显示裸文件名,没有目录、没有大小。实测库里 84,303 条事件全是 NULL。
一个参数被文档写过、被单测测过、还是从来没通过 —— 这跟「只会通过的检查」
是同一类东西:看着有,实际没有。

所以这里测的不是「dir_paths 能不能用」,是「它到底有没有被填上」。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan import snapshot, walker  # noqa: E402
from strata.store import db  # noqa: E402

MASK48 = 0x0000FFFFFFFFFFFF


class WalkerDoesNotTakeInodes(unittest.TestCase):
    """scandir 那条路**故意不**收目录编号 —— 这条锁的是「别把它加回来」。

    加回来的诱惑很直接:USN 正需要这张表,而遍历时顺手取一下看着很自然。
    量过,不行:DirEntry.inode() 在 Windows 上是按路径做 lstat,操作系统要从根
    逐段解析,C: 上净是 WinSxS、node_modules 这种深路径。整块 C: 上 8 线程
    68s → 165s(tools/bench_dir_paths.py),而用户第一位的诉求就是扫描快。
    换成读完日志拿父引用反查:0.23 秒、覆盖 87% 的事件,少 4 个百分点,
    便宜两个数量级。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "docs" / "draft").mkdir(parents=True)
        (self.root / "docs" / "report.txt").write_bytes(b"x" * 16)

    def tearDown(self):
        self.tmp.cleanup()

    def test_walk_drive_has_no_dir_paths_param(self):
        with self.assertRaises(TypeError):
            walker.walk_drive(str(self.root), dir_paths={})

    def test_walk_still_works(self):
        entries, stats = walker.walk_drive(str(self.root))
        self.assertGreater(stats.dirs, 0)
        self.assertEqual(stats.files, 1)


class ScanResultHasTheField(unittest.TestCase):
    """两个调用点都在 getattr(result, "dir_paths", None) —— 字段得真存在。

    以前不存在,所以 getattr 的默认值恒生效,两条路拿到的都是 None。
    现在字段在了:MFT 那一支填(resolve_paths 白送),scandir 那一支是空 dict
    (填不起),USN 靠 changes 里的反查兜。空 dict 和 None 的区别在于
    「有这个东西但这次没内容」和「压根没这个东西」—— 后者是 bug,前者是状态。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "a.bin").write_bytes(b"y" * 32)
        self.dbfile = self.root / "t.db"
        self.conn = db.connect(str(self.dbfile))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_field_exists_and_is_a_dict(self):
        result = snapshot.scan_directory(self.conn, str(self.root), label="T:")
        self.assertIsInstance(
            getattr(result, "dir_paths", None), dict,
            "ScanResult 上没有 dir_paths,两个调用点的 getattr 白写"
        )

    def test_collect_entries_return_shape_unchanged(self):
        """还是 4 元组。

        8 处调用和一处 mock(test_walker_snapshot.py:358 写死了
        `return_value=(entries, "mft", [], None)`)都按 4 个解包。
        表走出参而不是加第 5 个返回值,就是为了这个。
        """
        out = snapshot.collect_entries(str(self.root), prefer_mft=False)
        self.assertEqual(len(out), 4)

    def test_scandir_branch_leaves_it_empty(self):
        """scandir 这一支应当是空的 —— 而且这是设计,不是遗漏。

        写这条是因为「空」和「坏」长得一样。没有这条测试,下一个人看到
        scandir 扫完 dir_paths 是空的,会以为是 bug 又把 inode 收集加回来。
        """
        got: dict[int, str] = {}
        snapshot.collect_entries(str(self.root), prefer_mft=False, dir_paths=got)
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
