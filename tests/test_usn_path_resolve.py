"""按文件编号反查目录路径 —— USN 事件的路径就靠它还原。

为什么是这个设计,而不是遍历时顺手记编号:后者量过,整块 C: 上 8 线程
68s → 165s(DirEntry.inode() 在 Windows 上是按路径做 lstat,深路径特别贵)。
这个反过来做 —— 先读日志、收齐用到的父引用,再只对那几千个引用问路径。
实测 2,572 个引用 0.23 秒,覆盖 87.1% 的事件。
详见 docs/superpowers/plans/2026-08-30-usn-path-resolution.md。

真正调 Win32 的部分没法在临时目录上测(OpenFileById 要卷句柄和管理员权限),
所以这里测的是它周围的逻辑:去重、缓存、失败计数、路径清洗、以及
「拿不到就老实留空,别编」。Win32 那一层靠 tools/probe_openbyid.py 在真盘上验。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan import changes  # noqa: E402


class StripPrefixTest(unittest.TestCase):
    """GetFinalPathNameByHandleW 返回的是 \\\\?\\C:\\Windows 这种形式,要洗干净。"""

    def test_strips_dos_device_prefix_and_drive(self):
        self.assertEqual(changes._clean_final_path(r"\\?\C:\Windows", "C:"), "Windows")

    def test_root_becomes_empty_string(self):
        """盘根要变成空串,跟 mft.resolve_paths 的口径一致(它给根用 "")。

        不一致的话,根下面的文件会拼出 "\\x.iso" 这种带前导反斜杠的路径,
        而库里存的是不带的 —— enrich_deleted_sizes 按路径反查大小就永远查不中。
        """
        self.assertEqual(changes._clean_final_path(r"\\?\C:\\", "C:"), "")
        self.assertEqual(changes._clean_final_path(r"\\?\C:", "C:"), "")

    def test_nested_path(self):
        self.assertEqual(
            changes._clean_final_path(r"\\?\C:\Users\me\Downloads", "C:"),
            r"Users\me\Downloads",
        )

    def test_other_drive_is_rejected(self):
        """解析出来的路径不在本盘上,就不能用。

        联接点可以指向别的卷:C: 上的一个目录,解析出来是 D:\\something。
        照着拼会得出一条本盘根本不存在的路径,而且看的人无从分辨。
        宁可留空 —— 「不知道」比「说错」好。
        """
        self.assertIsNone(changes._clean_final_path(r"\\?\D:\Games", "C:"))

    def test_unc_is_rejected(self):
        """UNC 形式(\\\\?\\UNC\\server\\share)不是本盘路径,同上。"""
        self.assertIsNone(changes._clean_final_path(r"\\?\UNC\srv\share\x", "C:"))

    def test_garbage_is_rejected(self):
        self.assertIsNone(changes._clean_final_path("", "C:"))
        self.assertIsNone(changes._clean_final_path("Windows", "C:"))


class ResolverBookkeepingTest(unittest.TestCase):
    """去重、缓存、计数。用一个假的 opener 替掉 Win32 那一层。"""

    def setUp(self):
        self.calls: list[int] = []

    def fake_opener(self, table):
        """table 是 {完整引用 → 返回的原始路径 或 None}。"""
        def opener(ref: int) -> str | None:
            self.calls.append(ref)
            return table.get(ref)
        return opener

    def test_each_ref_asked_once(self):
        """同一个引用只问一次。

        实测 200,000 条事件只涉及 2,572 个不同父目录 —— 平均一个目录 78 条事件。
        不去重就是 78 倍的 Win32 调用,这个功能之所以便宜全靠这一点。
        """
        table = {0x11: r"\\?\C:\Downloads"}
        r = changes.DirPathResolver("C:", opener=self.fake_opener(table))
        for _ in range(50):
            r.resolve(0x11)
        self.assertEqual(self.calls, [0x11])

    def test_failures_are_cached_too(self):
        """失败也要缓存。

        实测 2,572 个里有 1,266 个开不了(目录已删,或 MFT 槽位被回收后
        序列号变了)—— 占一半。这些如果不缓存,每条事件都会重试一次,
        而失败的调用一样要花时间。
        """
        r = changes.DirPathResolver("C:", opener=self.fake_opener({}))
        for _ in range(30):
            r.resolve(0x22)
        self.assertEqual(self.calls, [0x22])

    def test_counts_hits_and_misses(self):
        table = {0x11: r"\\?\C:\A", 0x22: r"\\?\C:\B"}
        r = changes.DirPathResolver("C:", opener=self.fake_opener(table))
        r.resolve(0x11)
        r.resolve(0x22)
        r.resolve(0x33)
        r.resolve(0x33)          # 重复的不该重复计数
        self.assertEqual(r.hits, 2)
        self.assertEqual(r.misses, 1)

    def test_zero_ref_is_not_asked(self):
        """引用是 0 就别问了。

        0 不是合法的 MFT 引用(实测直接返回错误 87)。这类值来自解析不完整的
        记录,问一次纯浪费。
        """
        r = changes.DirPathResolver("C:", opener=self.fake_opener({}))
        self.assertIsNone(r.resolve(0))
        self.assertEqual(self.calls, [])

    def test_junction_paths_are_counted(self):
        """走联接点解析出来的路径要单独计数。

        GetFinalPathNameByHandleW 返回重解析后的规范路径,可能挑联接点那一侧:
        C:\\Users\\me\\AppData\\Local 会解析成
        ...\\AppData\\Local\\Packages\\Claude_xxx\\LocalCache\\Local。
        两条路径都合法、指向同一个目录,但显示出来会让人以为文件在别处。
        实测 1,296 个成功的里有 116 个这样(9%)。

        路径照用(它是对的),但数量要能报出来 —— 不然就没法判断
        「用户说路径看着不对」是不是这个原因。
        """
        table = {
            0x11: r"\\?\C:\Users\me\AppData\Local\Packages\App_x\LocalCache\Local",
            0x22: r"\\?\C:\Windows",
        }
        r = changes.DirPathResolver("C:", opener=self.fake_opener(table))
        got = r.resolve(0x11)
        r.resolve(0x22)
        self.assertEqual(
            got, r"Users\me\AppData\Local\Packages\App_x\LocalCache\Local"
        )
        self.assertEqual(r.via_reparse, 1)

    def test_opener_blowing_up_does_not_propagate(self):
        """opener 抛异常不能把整次采集带走。

        路径还原是锦上添花 —— 事件本身(名字、时间、类型)已经存下来了。
        为了补路径把整次采集搞挂,是拿主要功能换次要功能。
        """
        def boom(ref: int) -> str | None:
            raise OSError("卷句柄没了")

        r = changes.DirPathResolver("C:", opener=boom)
        self.assertIsNone(r.resolve(0x11))
        self.assertEqual(r.misses, 1)

    def test_no_opener_means_everything_misses(self):
        """开不出卷句柄时(比如没提权)整个解析器要能空转。

        不是抛异常,是每次都返回 None —— 调用方的代码路径不该分叉。
        """
        r = changes.DirPathResolver("C:", opener=None)
        self.assertIsNone(r.resolve(0x11))
        self.assertEqual(r.hits, 0)


if __name__ == "__main__":
    unittest.main()
