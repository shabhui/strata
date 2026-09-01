"""分桶热路径上换的两处写法,结果必须和原来一个字不差。

来路:build_buckets 在整次扫描里占 1.99 秒(tools/prof_scan_stages.py),而它对
每个文件调一次 safe_day、一次 attribution_of —— 本机 82 万个文件。单独量这两个:

    safe_day,82 万次
      time.strftime("%Y-%m-%d", localtime(v))    1.61s   现在这样
      f"{tm.tm_year:04d}-..."(仍走 localtime)    0.79s   2.04x

    strftime 那一半是白花的:它要查 locale、跑格式串解析,而我们的格式是固定的
    三段数字。config.day_timestamp 早就因为同一个理由不用 strptime 了
    (它的注释里写着「日期格式是我们自己写进库的固定格式,不需要通用解析器」),
    反方向却还在用 strftime —— 这次把它对上。

    再加一层记忆化(config.day_memo)                0.65s   1.45x

    记忆化按「实际算出来的当地午夜」存窗口,不按固定粒度切 —— 理由和窗口宽度
    取 22 小时的理由都在 config.day_memo 的文档里。1.45x 是连 safe_ts 一起算的
    端到端倍数;只比裸格式化那一段能到 2.3x,但调用方拿不到那个数。

  **一开始报错了两个数**,记下来免得再犯:第一次量出 strftime 1.61s、f-string
  0.79s(2.04x),是在机器上跑着一个 qemu 虚拟机(3906 秒 CPU)的时候单独跑
  一遍量的。改成同进程交错三轮之后是 0.97 / 0.89 —— 只有 1.09x。绝对值和倍数
  都错了。跨进程、不配对的计时在有负载的机器上不能用。

    attribution_of:试过换成「两次 find + 一次切片」,**慢 1.7 倍**,已经改回来。

    0.18s → 0.31s。看着 split 要建列表再 join、比找两个下标浪费,但
    split(maxsplit) 是一次 C 调用,find 循环每轮都是解释器在跑。这个函数以前
    一条测试都没有,所以覆盖留下来了(段数不够 depth、正好等于、多于、空串、
    结尾反斜杠、连续反斜杠),只是不再和参照实现比 —— 实现已经就是参照实现,
    比了是同义反复。

safe_day 那部分的测试拿**原来的实现**当参照物(照抄在下面,不是调 config 里的
函数),因为实现确实变了(strftime → f-string),两边不一样才比得出东西。
"""

from __future__ import annotations

import time
import unittest

from strata import config
from strata.scan import tree

def reference_day(ts: float | None) -> str | None:
    """safe_day 换写法之前的实现,照抄。"""
    value = config.safe_ts(ts)
    if value is None:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return None


def reference_attribution(path: str, depth: int = config.ATTRIBUTION_DEPTH) -> str:
    """attribution_of 换写法之前的实现,照抄。"""
    if path == "":
        return ""
    parts = path.split("\\", depth)
    if len(parts) > depth:
        parts = parts[:depth]
    return "\\".join(parts)


class SafeDayMatchesStrftimeTest(unittest.TestCase):
    """整个可用区间扫一遍,一天都不能差。"""

    def test_sweep_across_the_whole_range(self):
        # 步长取一个不整的数,免得每次都落在同一个时刻(比如正好午夜)
        step = 9 * 86400 + 3607
        ts = config.TS_MIN
        checked = 0
        while ts < config.TS_MAX:
            self.assertEqual(config.safe_day(ts), reference_day(ts), f"ts={ts}")
            ts += step
            checked += 1
        self.assertGreater(checked, 5000, "扫的点太少,覆盖不到全区间")

    def test_boundaries_and_midnights(self):
        """午夜前后一秒是最容易出错的地方。"""
        interesting = [config.TS_MIN, config.TS_MAX, config.TS_MAX - 1]
        day = 86400
        for base in (0, 946_684_800, 1_700_000_000, 4_000_000_000):
            for delta in (-1, 0, 1, day - 1, day, day + 1):
                value = base + delta
                if config.TS_MIN <= value <= config.TS_MAX:
                    interesting.append(value)
        for ts in interesting:
            self.assertEqual(config.safe_day(ts), reference_day(ts), f"ts={ts}")

    def test_dst_transitions_if_this_zone_has_them(self):
        """本机时区有夏令时的话,切换前后也得一致。

        中国没有夏令时,所以这条在本机上等于「随便再扫几个点」—— 不是白写:
        在有夏令时的机器上跑同一套测试时它才是主角,而 CI 换台机器就可能有。
        """
        for year_start in range(2000, 2030):
            # 三月和十一月的第二个周日附近,北半球夏令时切换常在那儿
            for month_day in ((3, 8), (3, 15), (11, 1), (11, 8)):
                tm = (year_start, month_day[0], month_day[1], 1, 30, 0, 0, 1, -1)
                try:
                    ts = time.mktime(tm)
                except (OverflowError, ValueError):
                    continue
                if config.TS_MIN <= ts <= config.TS_MAX:
                    self.assertEqual(config.safe_day(ts), reference_day(ts), f"ts={ts}")

    def test_bad_values_still_none(self):
        """坏值那套行为一条都不能变 —— test_config.py 也在盯,这里再钉一遍
        是因为换写法最容易顺手把 try/except 的范围改窄。"""
        for bad in (None, -1, -11_644_473_600.0, config.TS_MAX + 1, 1e18,
                    float("nan"), "2026-08-20", object()):
            self.assertIsNone(config.safe_day(bad), f"{bad!r}")
            self.assertEqual(config.safe_day(bad), reference_day(bad), f"{bad!r}")


class DayMemoMatchesSafeDayTest(unittest.TestCase):
    """记忆化的那个必须和 safe_day 给出同一个答案,一个都不能差。

    下面这些用例做过变异验证,结果值得写下来 —— 因为**有两处它们抓不到**:

      ✓ 窗口放到 25 小时(跨进下一天)          抓到
      ✗ 窗口右端点写成 <=                     抓不到
      ✗ 当地午夜起点偏一秒                     抓不到(其实不是 bug,见下)
      ✗ 缓存键粗一百倍                         抓不到(不是 bug)
      ✗ 让缓存永不命中                         抓不到(不是 bug)

    后三条抓不到是**对的**:键变粗只是窗口列表变长,答案照样对;起点偏一秒
    变成缓存不中、重算一次,答案也对;永不命中只是退化成直算。这些是性能变异,
    不是正确性变异,靠比对答案的测试本来就抓不到。

    第二条「窗口右端点写成 <=」是真的会算错 —— 但只在夏令时把一天缩短的时候。
    中国没有夏令时,而 Windows 上没有 time.tzset(),没法在进程里假造一个有
    夏令时的时区。所以这一处**没有测试兜着**,靠的是 config.day_memo 文档里
    那段推理(窗口 22 小时、右端用 `<`)。写在这儿是为了别让人以为「全绿 =
    夏令时也验过了」。
    """

    def test_sweep_with_one_shared_memo(self):
        """同一个 memo 连着问一大片时间戳 —— 命中路径和未命中路径都要走到。"""
        memo = config.day_memo()
        step = 9 * 86400 + 3607
        ts = config.TS_MIN
        while ts < config.TS_MAX:
            self.assertEqual(memo(ts), config.safe_day(ts), f"ts={ts}")
            ts += step

    def test_same_day_asked_many_times(self):
        """同一天问一百次,答案不能变 —— 这条走的全是命中路径。"""
        memo = config.day_memo()
        base = 1_700_000_000.0
        want = config.safe_day(base)
        for i in range(100):
            self.assertEqual(memo(base + i * 37.5), config.safe_day(base + i * 37.5))
        self.assertEqual(memo(base), want)

    def test_interleaved_days_do_not_poison_each_other(self):
        """来回跳着问不同的天。单条「上次那天」的缓存会在这里露馅。"""
        memo = config.day_memo()
        days = [1_700_000_000.0 + d * 86400 for d in range(60)]
        for _ in range(5):
            for ts in days:
                self.assertEqual(memo(ts), config.safe_day(ts), f"ts={ts}")
            for ts in reversed(days):
                self.assertEqual(memo(ts), config.safe_day(ts), f"ts={ts}")

    def test_around_local_midnight(self):
        """当地午夜前后各一秒 —— 窗口边界就在这儿,错一秒就跨天。"""
        memo = config.day_memo()
        for base in (1_700_000_000.0, 946_684_800.0, 4_000_000_000.0):
            midnight = config.day_timestamp(config.safe_day(base) or "", 0)
            if midnight is None:
                continue
            for delta in (-2, -1, -0.5, 0, 0.5, 1, 2, 86398, 86399, 86400, 86401):
                ts = midnight + delta
                if config.TS_MIN <= ts <= config.TS_MAX:
                    self.assertEqual(memo(ts), config.safe_day(ts),
                                     f"当地午夜{delta:+} → {ts}")

    def test_bad_values_behave_the_same(self):
        memo = config.day_memo()
        for bad in (None, -1, -11_644_473_600.0, config.TS_MAX + 1, 1e18,
                    float("nan"), "2026-08-20", object()):
            self.assertIsNone(memo(bad), f"{bad!r}")

    def test_two_memos_do_not_share_state(self):
        """每次 day_memo() 都是新的缓存 —— 不是模块级的。

        钉这一条是因为「顺手改成模块级 lru_cache」是个很自然的手滑,而那样
        缓存会跨整个进程活着,机器改了时区就开始给旧答案。
        """
        a, b = config.day_memo(), config.day_memo()
        self.assertIsNot(a, b)
        ts = 1_700_000_000.0
        self.assertEqual(a(ts), b(ts))


class AttributionBehaviourTest(unittest.TestCase):
    """这个函数原来一条测试都没有。换写法的尝试被实测否掉了,行为照旧,
    但覆盖留下来 —— 它本来就该有。

    注意这里**没有**「和参照实现比」那种用例:实现已经和参照实现一模一样了,
    比了等于同义反复,而同义反复的断言永远绿。所以钉的是写死的期望值和一条
    不变量(结果必须是原路径的前缀)。
    """

    PATHS = (
        "",
        "Users",
        "Users\\alice",
        "Users\\alice\\AppData",
        "Users\\alice\\AppData\\Local",
        "Users\\alice\\AppData\\Local\\Temp\\deep\\deeper\\x.tmp",
        "Program Files\\Steam\\steamapps\\common\\Game\\bin\\game.exe",
        "a\\b\\c",
        "a\\b\\c\\d",
        "a\\b\\",
        "a\\\\b",
        "a\\\\b\\\\c\\\\d",
        "\\leading",
        "单个中文目录\\子目录\\孙目录\\文件.txt",
    )

    def test_the_cases_that_matter_spelled_out(self):
        """写死期望值,不跟任何参照实现比。"""
        self.assertEqual(tree.attribution_of("", 3), "")
        self.assertEqual(tree.attribution_of("Users", 3), "Users")
        self.assertEqual(tree.attribution_of("Users\\alice", 3), "Users\\alice")
        self.assertEqual(
            tree.attribution_of("Users\\alice\\AppData", 3), "Users\\alice\\AppData"
        )
        self.assertEqual(
            tree.attribution_of("Users\\alice\\AppData\\Local\\Temp", 3),
            "Users\\alice\\AppData",
        )
        self.assertEqual(tree.attribution_of("a\\b\\c\\d", 1), "a")
        self.assertEqual(tree.attribution_of("a\\b\\c\\d", 2), "a\\b")

    def test_result_is_a_prefix_of_the_path(self):
        """归因必须是原路径的前缀 —— 否则会归到一个不存在的目录上去。"""
        for depth in (1, 2, 3, 4):
            for path in self.PATHS:
                got = tree.attribution_of(path, depth)
                self.assertTrue(
                    path.startswith(got), f"depth={depth} path={path!r} got={got!r}"
                )


if __name__ == "__main__":
    unittest.main()
