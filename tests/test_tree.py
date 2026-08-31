import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan.tree import (  # noqa: E402
    ScanEntry,
    attribution_of,
    build_buckets,
    build_tree,
    depth_of,
    parent_of,
    prune_tree,
    select_files,
)

MB = 1024 * 1024
GB = 1024 * MB


class PathHelpersTest(unittest.TestCase):
    def test_parent_of(self):
        self.assertIsNone(parent_of(""))
        self.assertEqual(parent_of("Users"), "")
        self.assertEqual(parent_of("Users\\alice"), "Users")
        self.assertEqual(parent_of("Users\\alice\\Downloads"), "Users\\alice")

    def test_depth_of(self):
        self.assertEqual(depth_of(""), 0)
        self.assertEqual(depth_of("Users"), 1)
        self.assertEqual(depth_of("Users\\alice"), 2)
        self.assertEqual(depth_of("a\\b\\c\\d"), 4)

    def test_attribution_truncates_to_depth(self):
        self.assertEqual(attribution_of("Users\\alice\\AppData\\Local\\Temp\\x"), "Users\\alice\\AppData")
        self.assertEqual(attribution_of("Users"), "Users")
        self.assertEqual(attribution_of("Users\\alice"), "Users\\alice")
        self.assertEqual(attribution_of(""), "")
        self.assertEqual(attribution_of("a\\b\\c\\d\\e", depth=2), "a\\b")


class BuildTreeTest(unittest.TestCase):
    def setUp(self):
        # \Users\alice\Downloads\a.iso   100 MB
        # \Users\alice\Downloads\b.zip    50 MB
        # \Users\alice\note.txt            1 MB
        # \Games\game.pak                200 MB
        self.entries = [
            ScanEntry("Users", is_dir=True),
            ScanEntry("Users\\alice", is_dir=True),
            ScanEntry("Users\\alice\\Downloads", is_dir=True),
            ScanEntry("Games", is_dir=True),
            ScanEntry("Users\\alice\\Downloads\\a.iso", is_dir=False, bytes=100 * MB),
            ScanEntry("Users\\alice\\Downloads\\b.zip", is_dir=False, bytes=50 * MB),
            ScanEntry("Users\\alice\\note.txt", is_dir=False, bytes=1 * MB),
            ScanEntry("Games\\game.pak", is_dir=False, bytes=200 * MB),
        ]

    def test_totals(self):
        nodes, total_bytes, total_files = build_tree(self.entries)
        self.assertEqual(total_bytes, 351 * MB)
        self.assertEqual(total_files, 4)

    def test_subtree_rollup(self):
        nodes, _, _ = build_tree(self.entries)
        self.assertEqual(nodes["Users\\alice\\Downloads"].subtree_bytes, 150 * MB)
        self.assertEqual(nodes["Users\\alice"].subtree_bytes, 151 * MB)
        self.assertEqual(nodes["Users"].subtree_bytes, 151 * MB)
        self.assertEqual(nodes["Games"].subtree_bytes, 200 * MB)
        self.assertEqual(nodes[""].subtree_bytes, 351 * MB)

    def test_direct_vs_subtree_bytes(self):
        nodes, _, _ = build_tree(self.entries)
        alice = nodes["Users\\alice"]
        self.assertEqual(alice.direct_bytes, 1 * MB)      # 只有 note.txt
        self.assertEqual(alice.subtree_bytes, 151 * MB)   # 含 Downloads

    def test_file_counts_roll_up_once(self):
        nodes, _, _ = build_tree(self.entries)
        self.assertEqual(nodes["Users\\alice\\Downloads"].subtree_files, 2)
        self.assertEqual(nodes["Users\\alice"].subtree_files, 3)
        self.assertEqual(nodes["Users"].subtree_files, 3)
        self.assertEqual(nodes[""].subtree_files, 4)

    def test_dir_counts(self):
        nodes, _, _ = build_tree(self.entries)
        # Users 子树含 alice 和 Downloads
        self.assertEqual(nodes["Users"].subtree_dirs, 2)
        self.assertEqual(nodes["Users\\alice"].subtree_dirs, 1)
        self.assertEqual(nodes["Users\\alice\\Downloads"].subtree_dirs, 0)
        # 根含全部 4 个目录
        self.assertEqual(nodes[""].subtree_dirs, 4)

    def test_missing_parent_directories_are_created(self):
        """目录条目缺失时不能丢字节。"""
        entries = [ScanEntry("a\\b\\c\\orphan.bin", is_dir=False, bytes=10 * MB)]
        nodes, total, _ = build_tree(entries)
        self.assertEqual(total, 10 * MB)
        self.assertIn("a", nodes)
        self.assertIn("a\\b", nodes)
        self.assertIn("a\\b\\c", nodes)
        self.assertEqual(nodes["a"].subtree_bytes, 10 * MB)
        self.assertEqual(nodes[""].subtree_bytes, 10 * MB)

    def test_root_level_file(self):
        entries = [ScanEntry("pagefile.sys", is_dir=False, bytes=8 * GB)]
        nodes, total, _ = build_tree(entries)
        self.assertEqual(total, 8 * GB)
        self.assertEqual(nodes[""].direct_bytes, 8 * GB)

    def test_newest_timestamps_propagate_up(self):
        entries = [
            ScanEntry("a", is_dir=True, modified=1000.0, created=900.0),
            ScanEntry("a\\b", is_dir=True, modified=1100.0, created=950.0),
            ScanEntry("a\\b\\new.bin", is_dir=False, bytes=MB, modified=5000.0, created=4000.0),
            ScanEntry("a\\old.bin", is_dir=False, bytes=MB, modified=2000.0, created=1500.0),
        ]
        nodes, _, _ = build_tree(entries)
        self.assertEqual(nodes["a"].newest_mtime, 5000.0)
        self.assertEqual(nodes["a"].newest_ctime, 4000.0)
        self.assertEqual(nodes["a\\b"].newest_mtime, 5000.0)

    def test_empty_input(self):
        nodes, total, files = build_tree([])
        self.assertEqual((total, files), (0, 0))
        self.assertEqual(set(nodes), {""})


class PruneTreeTest(unittest.TestCase):
    def test_keeps_shallow_and_large(self):
        entries = [
            ScanEntry("small_top", is_dir=True),
            ScanEntry("small_top\\tiny.txt", is_dir=False, bytes=1024),
            ScanEntry("a\\b\\c\\d\\e\\huge.iso", is_dir=False, bytes=500 * MB),
            ScanEntry("a\\b\\c\\d\\e\\f\\tiny2.txt", is_dir=False, bytes=2048),
        ]
        nodes, _, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=4 * MB, max_depth=2)
        kept = {r.path for r in rows}

        self.assertIn("small_top", kept)          # 浅
        self.assertIn("a\\b\\c\\d\\e", kept)      # 大
        self.assertNotIn("a\\b\\c\\d\\e\\f", kept)  # 深且小

    def test_folded_bytes_accounted_on_nearest_kept_ancestor(self):
        entries = [
            ScanEntry("keep\\x.bin", is_dir=False, bytes=100 * MB),
            ScanEntry("keep\\drop1\\a.txt", is_dir=False, bytes=1000),
            ScanEntry("keep\\drop1\\drop2\\b.txt", is_dir=False, bytes=2000),
        ]
        nodes, _, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=4 * MB, max_depth=1)
        by_path = {r.path: r for r in rows}

        self.assertIn("keep", by_path)
        self.assertNotIn("keep\\drop1", by_path)
        row = by_path["keep"]
        self.assertEqual(row.folded_children, 2)
        self.assertEqual(row.folded_bytes, 3000)  # 1000 + 2000,不重复

    def test_pruned_rows_never_lose_subtree_total(self):
        """保留行的 bytes 仍是完整子树总量,裁剪只影响明细粒度。"""
        entries = [
            ScanEntry("top\\deep\\deeper\\x.bin", is_dir=False, bytes=7 * MB),
        ]
        nodes, total, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=100 * GB, max_depth=1)
        top = next(r for r in rows if r.path == "top")
        self.assertEqual(top.bytes, total)

    def test_sorted_by_size_descending(self):
        entries = [
            ScanEntry("a\\x", is_dir=False, bytes=10 * MB),
            ScanEntry("b\\y", is_dir=False, bytes=50 * MB),
            ScanEntry("c\\z", is_dir=False, bytes=30 * MB),
        ]
        nodes, _, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=0, max_depth=10)
        # 根那一行是整块盘,必然排最前;这里盯的是它后面的顺序
        self.assertEqual(rows[0].path, "")
        top3 = [r.path for r in rows[1:4]]
        self.assertEqual(top3, ["b", "c", "a"])


class RootRowTest(unittest.TestCase):
    """盘根那一行必须落库。

    原来 prune_tree 直接 `if node.path == "": continue`,盘根就没有行。
    后果不是少一行装饰:盘根下的文件字节记在根节点的 direct_bytes 上,这一行
    不写出去,这些字节就进了 scanned_bytes 而在树里查无此人 —— 总数和树永远
    差这么多,而且差的正好是 C: 上最大的两个文件(pagefile.sys、hiberfil.sys)。

    这条在两条采集路径上都成立:scandir 给盘根文件的 path 是裸文件名,
    MFT 那边 resolve_paths 把根记录映射成空串,结果一样。
    """

    def test_root_row_exists(self):
        nodes, _, _ = build_tree([ScanEntry("a\\x", is_dir=False, bytes=MB)])
        rows = prune_tree(nodes, min_bytes=0, max_depth=10)
        self.assertIn("", {r.path for r in rows})

    def test_root_level_file_bytes_are_reachable(self):
        """核心那条:总数里的每个字节都要能在某一行的 own_bytes 里找到。"""
        entries = [
            ScanEntry("Windows", is_dir=True),
            ScanEntry("Windows\\a.dll", is_dir=False, bytes=1000),
            ScanEntry("pagefile.sys", is_dir=False, bytes=8 * GB),
            ScanEntry("hiberfil.sys", is_dir=False, bytes=6 * GB),
        ]
        nodes, scanned, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=0, max_depth=10)
        self.assertEqual(sum(r.own_bytes for r in rows), scanned)

        root = next(r for r in rows if r.path == "")
        self.assertEqual(root.own_bytes, 14 * GB)
        self.assertEqual(root.bytes, scanned)
        self.assertEqual(root.depth, 0)

    def test_root_row_survives_aggressive_pruning(self):
        """裁剪阈值再狠也不能把根裁掉 —— 它不参与保留判断。"""
        entries = [ScanEntry("deep\\deeper\\x.bin", is_dir=False, bytes=1024)]
        nodes, scanned, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=100 * GB, max_depth=0)
        self.assertEqual([r.path for r in rows], [""])
        # 被裁掉的顶层目录要折叠到根上,不能凭空消失
        root = rows[0]
        self.assertEqual(root.bytes, scanned)
        self.assertEqual(root.folded_bytes, 1024)
        self.assertEqual(root.folded_children, 2)

    def test_folded_bytes_never_vanish(self):
        """把每一行的 own_bytes 和 folded_bytes 加起来,应当等于总数。

        钉的是 prune_tree 第二个循环里那三个 `""` 判断。`""` 是 falsy,
        写成 `if ancestor` 的话被裁掉的顶层目录会折叠到「没有地方」。
        """
        entries = [
            ScanEntry("big\\x.bin", is_dir=False, bytes=500 * MB),
            ScanEntry("tiny1\\a.txt", is_dir=False, bytes=1000),
            ScanEntry("tiny2\\b.txt", is_dir=False, bytes=2000),
            ScanEntry("root.bin", is_dir=False, bytes=3000),
        ]
        nodes, scanned, _ = build_tree(entries)
        rows = prune_tree(nodes, min_bytes=4 * MB, max_depth=0)
        accounted = sum(r.own_bytes + r.folded_bytes for r in rows)
        self.assertEqual(accounted, scanned)


class BucketsTest(unittest.TestCase):
    def _ts(self, day: str) -> float:
        return time.mktime(time.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def test_groups_by_creation_day_and_attribution(self):
        entries = [
            ScanEntry("Games\\Steam\\steamapps\\a.pak", is_dir=False,
                      bytes=100 * MB, created=self._ts("2026-08-20")),
            ScanEntry("Games\\Steam\\steamapps\\b.pak", is_dir=False,
                      bytes=50 * MB, created=self._ts("2026-08-20")),
            ScanEntry("Users\\alice\\Downloads\\c.zip", is_dir=False,
                      bytes=80 * MB, created=self._ts("2026-08-21")),
        ]
        rows = build_buckets(entries, min_bytes=MB)
        by_key = {(r.day, r.attribution): r for r in rows}

        steam = by_key[("2026-08-20", "Games\\Steam\\steamapps")]
        self.assertEqual(steam.bytes, 150 * MB)
        self.assertEqual(steam.files, 2)

        dl = by_key[("2026-08-21", "Users\\alice\\Downloads")]
        self.assertEqual(dl.bytes, 80 * MB)

    def test_small_buckets_merge_into_other_preserving_day_total(self):
        entries = [
            ScanEntry(f"dir{i}\\x\\y\\f.bin", is_dir=False, bytes=100 * 1024,
                      created=self._ts("2026-08-20"))
            for i in range(20)
        ]
        rows = build_buckets(entries, min_bytes=MB)
        self.assertEqual([r.attribution for r in rows], [""])
        self.assertEqual(rows[0].bytes, 20 * 100 * 1024)
        self.assertEqual(rows[0].files, 20)

    def test_falls_back_to_mtime_when_no_ctime(self):
        entries = [
            ScanEntry("a\\x.bin", is_dir=False, bytes=10 * MB,
                      created=None, modified=self._ts("2026-08-19")),
        ]
        rows = build_buckets(entries, min_bytes=MB)
        self.assertEqual(rows[0].day, "2026-08-19")

    def test_entries_without_any_timestamp_skipped(self):
        entries = [ScanEntry("a\\x.bin", is_dir=False, bytes=10 * MB)]
        self.assertEqual(build_buckets(entries), [])

    def test_broken_timestamps_skipped_without_killing_the_scan(self):
        """真机上撞出来的:C 盘里有文件带着 localtime() 拒绝转换的时间戳。

        一个坏文件曾经让整次全盘扫描在最后一步抛 OSError 挂掉。坏值必须丢掉,
        同一批里的好文件还得照常入桶。
        """
        bad = [
            -1.0,                    # 负数,Windows 上 localtime 直接抛 OSError
            -11644473600.0,          # FILETIME 0 → 1601 年
            1e18,                    # 溢出
            float("nan"),
            4102444801.0,            # 2100 年之后,当坏值处理
        ]
        entries = [
            ScanEntry(f"bad\\f{i}.bin", is_dir=False, bytes=10 * MB, created=ts)
            for i, ts in enumerate(bad)
        ]
        entries.append(
            ScanEntry("good\\ok.bin", is_dir=False, bytes=7 * MB,
                      created=self._ts("2026-08-20"))
        )

        rows = build_buckets(entries, min_bytes=MB)
        self.assertEqual([(r.day, r.bytes) for r in rows], [("2026-08-20", 7 * MB)])

    def test_zero_timestamp_is_usable(self):
        """1970-01-01 是合法的,别把它跟坏值一起丢掉。"""
        entries = [ScanEntry("a\\x.bin", is_dir=False, bytes=10 * MB, created=0.0)]
        rows = build_buckets(entries, min_bytes=MB)
        self.assertEqual([r.day for r in rows], ["1970-01-01"])

    def test_directories_and_zero_byte_files_excluded(self):
        entries = [
            ScanEntry("a", is_dir=True, created=self._ts("2026-08-20")),
            ScanEntry("a\\empty.txt", is_dir=False, bytes=0, created=self._ts("2026-08-20")),
        ]
        self.assertEqual(build_buckets(entries), [])

    def test_day_totals_are_conserved(self):
        """无论怎么合并,某天的总字节必须等于当天所有文件之和。"""
        day = self._ts("2026-08-20")
        entries = [
            ScanEntry("big\\a\\b\\x.bin", is_dir=False, bytes=500 * MB, created=day),
            *[ScanEntry(f"s{i}\\a\\b\\y.bin", is_dir=False, bytes=1024, created=day)
              for i in range(50)],
        ]
        rows = build_buckets(entries, min_bytes=MB)
        expected = 500 * MB + 50 * 1024
        self.assertEqual(sum(r.bytes for r in rows), expected)
        self.assertEqual(sum(r.files for r in rows), 51)


class SelectFilesTest(unittest.TestCase):
    def test_keeps_large_files(self):
        entries = [
            ScanEntry("a\\big.iso", is_dir=False, bytes=100 * MB, created=1000.0),
            ScanEntry("a\\small.txt", is_dir=False, bytes=1024, created=1000.0),
        ]
        rows = select_files(entries, now=2_000_000_000.0, min_bytes=8 * MB)
        self.assertEqual([r.path for r in rows], ["a\\big.iso"])

    def test_keeps_recent_medium_files(self):
        now = 2_000_000_000.0
        entries = [
            ScanEntry("a\\recent.log", is_dir=False, bytes=2 * MB, created=now - 86400),
            ScanEntry("a\\old.log", is_dir=False, bytes=2 * MB, created=now - 200 * 86400),
        ]
        rows = select_files(
            entries, now=now, min_bytes=8 * MB, recent_days=45, recent_min_bytes=MB
        )
        self.assertEqual([r.path for r in rows], ["a\\recent.log"])

    def test_cap_keeps_biggest(self):
        entries = [
            ScanEntry(f"a\\f{i}.bin", is_dir=False, bytes=(i + 1) * MB, created=1000.0)
            for i in range(100)
        ]
        rows = select_files(entries, now=2_000_000_000.0, min_bytes=0, cap=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0].bytes, 100 * MB)
        self.assertEqual(rows[-1].bytes, 96 * MB)

    def test_directories_excluded(self):
        entries = [ScanEntry("a", is_dir=True, bytes=999 * GB, created=1000.0)]
        self.assertEqual(select_files(entries, now=2_000_000_000.0, min_bytes=0), [])

    def test_sorted_descending(self):
        entries = [
            ScanEntry("a\\m.bin", is_dir=False, bytes=50 * MB, created=1000.0),
            ScanEntry("a\\l.bin", is_dir=False, bytes=90 * MB, created=1000.0),
            ScanEntry("a\\s.bin", is_dir=False, bytes=10 * MB, created=1000.0),
        ]
        rows = select_files(entries, now=2_000_000_000.0, min_bytes=MB)
        self.assertEqual([r.bytes for r in rows], [90 * MB, 50 * MB, 10 * MB])


if __name__ == "__main__":
    unittest.main()
