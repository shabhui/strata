"""坏时间戳不能一路污染到盘根。

## 真机上量到的(tools/probe_bad_ts.py)

    × dirs.newest_ctime  160,235 行   越界 6   区间内但在未来 7
          min=-11416537060.313498  max=1915769744.0

    越界的(源头):
      ProgramData\\Package Cache\\{0154B4C2-...}   c=-11416537060  → 1608 年
    在未来的(源头):
      Program Files (x86)\\Internet Download Manager   2030-09-16

两批坏值的**性质不一样**,这是这组测试的重点:

- 负值那批**越界**(< TS_MIN),`config.safe_ts` 的范围检查挡得住。
- 2030 那批**没越界**。1915769744 离 TS_MAX(2100-01-01,4102444800)
  还差一大截,范围检查一路放行 —— 而它恰恰是界面上真正看得见的那个。

所以光靠 TS_MIN/TS_MAX 挡不住实际发生的那个 bug。「能不能表示」和
「可不可信」是两个界,`config.newest_ceiling` 管后者。

## 看得见的后果(修之前真实发生的)

    快照9  211402.3 MB  2030-09-16 (未来 1478 天)  (根)
    快照9    6881.1 MB  2030-09-16 (未来 1478 天)  Program Files (x86)

一个 31.8 MB 目录里的文件,把 211 GB 的盘根染成了 2030 年。

1. `recently_grown` 用 `newest_ctime >= cutoff` 筛,2030 永远满足 ——
   那个目录被永久钉在「最近写入」上,挤掉真正在长的目录。
2. `days_old` 算出 **-1477.6**,负的天数进了 API。
3. 前端 `ageText` 判 `d < 1` 就说「今天」,2030 年显示成「今天」。
4. 同一个 `Program Files (x86)`,大目录表用 `newest_mtime` 显示 2026-08-30,
   最近写入表用 `newest_ctime` 显示 2030-09-16。两个面板自相矛盾。

## 为什么在 _newer 里修

它是两个字段、两轮聚合(文件→目录、目录→父目录)的唯一入口。在这里挡掉,
坏值进不了库,下游不用每个读的地方各挡一次 —— 那种散落的防护迟早漏一处,
而漏掉的那处就是显示出错的那处。
"""

from __future__ import annotations

import time
import unittest

from strata import config
from strata.scan import tree

# 真机上那个值,原样搬过来。用它而不是随手造一个 TS_MAX+1:
# TS_MAX+1 会被范围检查挡住,测试绿了但证明不了实际那个 bug 修好了。
IDM_2030 = 1915769744.0                     # 2030-09-16
PACKAGE_CACHE_1608 = -11416537060.313498    # 1608 年


class ObservedValuesAreWhatWeTestWith(unittest.TestCase):
    """先把前提钉住:这两个真实值分别属于哪一类。

    这组存在的理由:如果哪天有人把 TS_MAX 调低到 2030 以下,下面那些
    「未来」测试就会因为范围检查而通过 —— 通过了,但不再测的是原来那件事。
    这里让那种改动直接把前提测红。
    """

    def test_the_2030_value_is_in_range_so_range_check_cannot_catch_it(self):
        self.assertIsNotNone(
            config.safe_ts(IDM_2030),
            "2030-09-16 在 TS_MIN..TS_MAX 之内,范围检查本来就挡不住它 —— "
            "这正是为什么需要 newest_ceiling",
        )
        self.assertEqual(
            time.strftime("%Y-%m-%d", time.localtime(IDM_2030)), "2030-09-16"
        )

    def test_the_negative_value_is_out_of_range(self):
        self.assertIsNone(config.safe_ts(PACKAGE_CACHE_1608))


class NewestCeiling(unittest.TestCase):
    def test_ceiling_sits_just_past_now(self):
        now = 1_700_000_000.0
        self.assertEqual(config.newest_ceiling(now), now + config.FUTURE_TOLERANCE)

    def test_ceiling_never_exceeds_representable_range(self):
        """钟走到 2100 附近时上界不能翻出可表示范围,否则 localtime 会抛。"""
        self.assertEqual(config.newest_ceiling(config.TS_MAX), config.TS_MAX)

    def test_tolerance_covers_clock_skew_but_not_the_bad_value(self):
        """容差要够宽装得下时区/夏令时错配,又要远远够不着那个坏值。"""
        self.assertGreaterEqual(
            config.FUTURE_TOLERANCE, 26 * 3600,
            "NAS 时区配错最多超前 26 小时,那是正常产物,不该丢",
        )
        now = time.time()
        self.assertGreater(
            IDM_2030 - now, 100 * config.FUTURE_TOLERANCE,
            "坏值比容差大两个数量级以上 —— 不存在挡不住又误伤的中间地带",
        )


class NewerRejectsGarbage(unittest.TestCase):
    """_newer 是唯一入口,守在这儿。"""

    def setUp(self):
        self.now = time.time()
        self.ceiling = config.newest_ceiling(self.now)

    def newer(self, a, b):
        return tree._newer(a, b, self.ceiling)

    def test_keeps_normal_values(self):
        older = self.now - 100
        self.assertEqual(self.newer(older, self.now), self.now)
        self.assertEqual(self.newer(self.now, older), self.now)

    def test_none_still_passes_through(self):
        self.assertEqual(self.newer(None, self.now), self.now)
        self.assertEqual(self.newer(self.now, None), self.now)
        self.assertIsNone(self.newer(None, None))

    def test_the_real_2030_value_loses(self):
        """整组里最要紧的一条:真机上那个值不能赢。"""
        self.assertEqual(self.newer(self.now, IDM_2030), self.now)
        self.assertEqual(self.newer(IDM_2030, self.now), self.now)

    def test_the_real_negative_value_loses(self):
        self.assertEqual(self.newer(self.now, PACKAGE_CACHE_1608), self.now)
        self.assertEqual(self.newer(PACKAGE_CACHE_1608, self.now), self.now)

    def test_out_of_range_future_also_loses(self):
        self.assertEqual(self.newer(self.now, config.TS_MAX + 86400), self.now)

    def test_mild_clock_skew_is_kept(self):
        """超前一小时的文件是跨机器拷贝的正常产物,留着。

        这条和上面几条一起画出界在哪。少了它,把 ceiling 收成「不许超过现在」
        也能全绿 —— 而那样会把正常文件的时间丢成「未知」。
        """
        skewed = self.now + 3600
        self.assertEqual(self.newer(self.now, skewed), skewed)

    def test_all_garbage_becomes_none(self):
        """两边都不可信就返回 None —— 「不知道」比「说个错的」好。

        None 在下游是有意义的:列表列显示未知,recently_grown 的
        `newest_ctime >= cutoff` 筛不到它。留个坏数字下游会当真。
        """
        self.assertIsNone(self.newer(PACKAGE_CACHE_1608, IDM_2030))

    def test_nan_is_rejected(self):
        """NaN 跟任何数比都是 False,不挡掉会让 a > b 静默走错分支。"""
        self.assertEqual(self.newer(self.now, float("nan")), self.now)
        self.assertEqual(self.newer(float("nan"), self.now), self.now)

    def test_ceiling_is_required(self):
        """漏传上界必须当场炸,不能兜一个默认值。

        这个项目已经栽过两次「默认值让没接上的参数看起来在工作」
        (dir_paths 从来没接上、prune_usn_events 从来没被调用),
        两次都是照样能跑、照样测试全绿、只是功能没在工作。
        """
        with self.assertRaises(TypeError):
            tree._newer(self.now, self.now)          # type: ignore[call-arg]


def entry(path: str, *, is_dir: bool = False, bytes_: int = 0,
          modified: float | None = None, created: float | None = None) -> tree.ScanEntry:
    return tree.ScanEntry(
        path=path, is_dir=is_dir, bytes=bytes_, modified=modified, created=created
    )


class PoisonDoesNotReachTheRoot(unittest.TestCase):
    """端到端:一个坏文件不能把整棵树染了。这是真机上发生过的事。"""

    def test_one_bad_file_does_not_poison_ancestors(self):
        """照着真机的形状搭:31.8 MB 的 IDM 目录,把整个盘根染成 2030。"""
        now = time.time()
        entries = [
            entry("", is_dir=True),
            entry("Program Files (x86)", is_dir=True),
            entry("Program Files (x86)\\Internet Download Manager", is_dir=True),
            entry("Program Files (x86)\\Internet Download Manager\\ok.dll",
                  bytes_=1_000_000, modified=now - 86400, created=now - 86400),
            # 坏文件:ctime 写着 2030-09-16。真机上就是这一个。
            entry("Program Files (x86)\\Internet Download Manager\\bad.dat",
                  bytes_=2_000_000, modified=now - 3600, created=IDM_2030),
        ]
        nodes, _b, _f = tree.build_tree(entries, now=now)

        ceiling = config.newest_ceiling(now)
        for path in ("", "Program Files (x86)",
                     "Program Files (x86)\\Internet Download Manager"):
            ctime = nodes[path].newest_ctime
            label = path or "(根)"
            self.assertIsNotNone(ctime, f"{label} 的 ctime 不该是 None,同级还有正常文件")
            self.assertLessEqual(
                ctime, ceiling,
                f"{label} 被 2030 那个值染了 —— 真机上盘根就是这么变成 2030 的",
            )

    def test_a_dir_whose_only_file_is_bad_reports_unknown(self):
        """整个目录只有坏时间戳时报「不知道」,不报一个假日期。"""
        entries = [
            entry("", is_dir=True),
            entry("Cache", is_dir=True),
            entry("Cache\\x.bin", bytes_=100,
                  modified=PACKAGE_CACHE_1608, created=PACKAGE_CACHE_1608),
        ]
        nodes, _b, _f = tree.build_tree(entries)
        self.assertIsNone(nodes["Cache"].newest_ctime)
        self.assertIsNone(nodes["Cache"].newest_mtime)

    def test_directory_own_timestamps_also_filtered(self):
        """目录条目自己的时间戳走的是另一条分支(build_tree 里 is_dir 那一支),
        也得挡 —— 不然坏值从目录本身进来,而不是从文件。"""
        entries = [
            entry("", is_dir=True),
            entry("Weird", is_dir=True, modified=IDM_2030, created=IDM_2030),
        ]
        nodes, _b, _f = tree.build_tree(entries)
        self.assertIsNone(nodes["Weird"].newest_mtime)
        self.assertIsNone(nodes["Weird"].newest_ctime)

    def test_normal_tree_still_aggregates_upward(self):
        """过滤不能顺手把正常的聚合弄坏 —— 最深的那个时间要一路传到根。"""
        now = time.time()
        entries = [
            entry("", is_dir=True),
            entry("a", is_dir=True),
            entry("a\\b", is_dir=True),
            entry("a\\b\\old.txt", bytes_=10, modified=now - 8640000, created=now - 8640000),
            entry("a\\b\\new.txt", bytes_=10, modified=now - 60, created=now - 60),
        ]
        nodes, _b, _f = tree.build_tree(entries, now=now)
        for path in ("", "a", "a\\b"):
            self.assertAlmostEqual(nodes[path].newest_mtime, now - 60, places=3)


if __name__ == "__main__":
    unittest.main()
