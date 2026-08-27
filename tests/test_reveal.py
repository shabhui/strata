"""在资源管理器里定位:路径校验与命令行拼装。

这个模块拿浏览器传进来的字符串去启动进程,所以测试盯的是两件事:
拼出来的命令行到底是什么,以及各种越界写法有没有被拦住。

runner 全程注入,不真的弹窗。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from strata import reveal


class Recorder:
    """记下被要求执行的命令行。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(list(argv))
        return 0


class NormalizeDriveTest(unittest.TestCase):
    def test_accepts_the_usual_spellings(self) -> None:
        for raw in ("c", "C", "c:", "C:", "c:\\", "C:\\", " c: "):
            with self.subTest(raw=raw):
                self.assertEqual(reveal.normalize_drive(raw), "C:")

    def test_rejects_junk(self) -> None:
        for raw in ("", "   ", "1", "..", "CD", "\\\\server\\share", "%"):
            with self.subTest(raw=raw):
                with self.assertRaises(reveal.RevealError):
                    reveal.normalize_drive(raw)


class ResolveTargetTest(unittest.TestCase):
    def test_joins_relative_path_onto_drive(self) -> None:
        got = reveal.resolve_target("C:", "Users\\me\\Downloads")
        self.assertEqual(got, Path("C:\\Users\\me\\Downloads"))

    def test_empty_path_is_the_drive_root(self) -> None:
        self.assertEqual(reveal.resolve_target("C:", ""), Path("C:\\"))

    def test_strips_wrapping_quotes(self) -> None:
        self.assertEqual(
            reveal.resolve_target("C:", '"Users\\me"'), Path("C:\\Users\\me")
        )

    def test_refuses_absolute_and_unc(self) -> None:
        """带盘符或 UNC 的写法一律不收 —— 只接受盘内相对路径。"""
        for rel in (
            "C:\\Windows",
            "D:\\Secrets",
            "\\\\server\\share\\x",
            "//server/share/x",
            "shell:startup",
            "C:Windows",
        ):
            with self.subTest(rel=rel):
                with self.assertRaises(reveal.RevealError):
                    reveal.resolve_target("C:", rel)

    def test_climbing_past_the_root_lands_back_on_the_root(self) -> None:
        """盘根上的 .. 会被 Windows 夹住,爬不出去。

        `Path("C:\\\\") / "..\\\\..\\\\Windows"` 解析出来是 `C:\\Windows` ——
        根目录的父目录还是根目录。所以这里不该抛异常:没有越界,
        钳位本身就是我们要的结果。真正能跑到别处的写法是冒号(D:\\x)
        和 UNC(\\\\server),那两种在上面那条用例里已经挡掉了。

        这条用例守的是不变量 —— 结果必须还在盘内 —— 而不是某一种实现。
        """
        for rel in (
            "..\\..\\..\\Windows",
            "Users\\..\\..\\..\\..\\x",
            "..",
            "..\\",
            "../../../../etc/passwd",
            "Users\\..\\..\\Windows\\System32",
        ):
            with self.subTest(rel=rel):
                got = reveal.resolve_target("C:", rel)
                self.assertTrue(
                    got.is_relative_to(Path("C:\\")),
                    f"{rel} 解析成了 {got},跑出 C: 了",
                )

    def test_climbing_inside_the_drive_is_fine(self) -> None:
        """没跑出盘符的 .. 不用管,解析完还在盘里就行。"""
        got = reveal.resolve_target("C:", "Users\\me\\..\\you")
        self.assertEqual(got, Path("C:\\Users\\you"))

    def test_refuses_nul(self) -> None:
        with self.assertRaises(reveal.RevealError):
            reveal.resolve_target("C:", "Users\\a\0b")


@unittest.skipUnless(sys.platform == "win32", "只在 Windows 上有意义")
class RevealTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tc-reveal-"))
        self.addCleanup(self._cleanup)
        self.drive = self.tmp.drive                      # 例如 'C:'
        self.rel = str(self.tmp.relative_to(self.tmp.anchor))

    def _cleanup(self) -> None:
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.tmp.rmdir()

    def test_directory_opens_directly(self) -> None:
        rec = Recorder()
        out = reveal.reveal(self.drive, self.rel, runner=rec)

        self.assertEqual(out["kind"], "dir")
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0], ["explorer.exe", str(self.tmp)])

    def test_file_is_selected_not_launched(self) -> None:
        """关键安全属性:对着文件不能「打开」它,只能在父目录里选中。

        os.startfile 或者 explorer 直接给文件路径,对 .exe / .bat 就是运行。
        """
        bait = self.tmp / "看起来像陷阱.bat"
        bait.write_text("@echo 不该被执行\n", encoding="utf-8")
        rec = Recorder()

        out = reveal.reveal(self.drive, str(bait.relative_to(bait.anchor)), runner=rec)

        self.assertEqual(out["kind"], "file")
        self.assertEqual(rec.calls[0][0], "explorer.exe")
        self.assertEqual(rec.calls[0][1], f"/select,{bait}")
        # 参数是一个整体,没有被拆开,也没有经过 shell
        self.assertEqual(len(rec.calls[0]), 2)

    def test_missing_path_is_404_and_launches_nothing(self) -> None:
        rec = Recorder()
        gone = str((self.tmp / "早就没了").relative_to(self.tmp.anchor))

        with self.assertRaises(reveal.RevealError) as caught:
            reveal.reveal(self.drive, gone, runner=rec)

        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(rec.calls, [])

    def test_path_with_spaces_and_chinese_stays_one_argument(self) -> None:
        """空格和中文不该把参数拆开 —— 拼字符串就会踩这个坑。"""
        weird = self.tmp / "有 空格 的 目录"
        weird.mkdir()
        rec = Recorder()

        reveal.reveal(self.drive, str(weird.relative_to(weird.anchor)), runner=rec)

        self.assertEqual(rec.calls[0], ["explorer.exe", str(weird)])

    def test_runner_failure_becomes_500(self) -> None:
        def boom(argv: list[str]) -> int:
            raise OSError("找不到 explorer")

        with self.assertRaises(reveal.RevealError) as caught:
            reveal.reveal(self.drive, self.rel, runner=boom)
        self.assertEqual(caught.exception.status, 500)

    def test_out_of_bounds_shapes_never_reach_the_runner(self) -> None:
        """越界的写法要在校验阶段就被挡住,不能进到启动进程那一步。

        校验和执行是两段代码,分别测过不代表接起来还对 —— 万一哪天
        改成先起进程再校验,单测 resolve_target 的用例照样是绿的。
        """
        for rel in (
            "D:\\Secrets",                # 冒号:换盘
            "\\\\server\\share\\x",       # UNC:换机器
            "shell:startup",              # shell 协议
            "Users\\a\0b",                # NUL 截断
        ):
            with self.subTest(rel=rel):
                rec = Recorder()
                with self.assertRaises(reveal.RevealError):
                    reveal.reveal(self.drive, rel, runner=rec)
                self.assertEqual(rec.calls, [])

    def test_climbing_past_the_root_opens_the_root_not_something_else(self) -> None:
        """一串 .. 被钳回盘根,交给资源管理器的还是盘根。

        这里不抛异常是对的(见 ResolveTargetTest),但要盯住交出去的
        参数:必须是这个盘的根,不是别的盘、别的目录。
        """
        rec = Recorder()
        reveal.reveal(self.drive, "..\\..\\..\\..", runner=rec)
        self.assertEqual(rec.calls, [["explorer.exe", self.drive + "\\"]])


if __name__ == "__main__":
    unittest.main()
