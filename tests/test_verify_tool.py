"""验发布验证脚本自己。

为什么值得为一个开发脚本写测试:tools/verify_exe.py 是发布前的最后一道闸,
它瞎了的后果是把坏 exe 发出去。而它之前真的瞎过 ——

    if "警告:界面目录不存在" in out:   # 冻结后的 exe 写 GBK,这条永远不成立
        BAD
    else:
        OK "web/ 打进去了"             # 于是永远走这里

判据只有一个可能的结果(通过)时,它比没有检查更糟:没有检查你知道自己不知道。
这个文件盯两件事:编码解得对(反向判据才有可能成立),以及界面目录那条判据
真的会判失败。
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import verify_exe as V  # noqa: E402

MEI = r"C:\Users\sbhui\AppData\Local\Temp\_MEI123456"
DB_OK = r"C:\Users\sbhui\AppData\Local\Strata\strata.db"

DOCTOR = ("Strata 自检\n"
          f"界面文件:{MEI}\\web\n"
          f"数据库:{DB_OK}\n"
          "C: 快照 2 个\n")


class DecodeOutputTest(unittest.TestCase):
    """子进程写什么编码就按什么解。"""

    def test_gbk_comes_back_as_chinese(self):
        # 冻结后的 exe 在中文 Windows 上就是这么写的:不认 PYTHONIOENCODING,
        # 直接往管道写本地代码页。
        self.assertEqual(V.decode_output(DOCTOR.encode("gbk")), DOCTOR)

    def test_utf8_still_works(self):
        # 源码模式、英文机器、或者哪天 PyInstaller 认了那个变量,拿到的是 UTF-8。
        self.assertEqual(V.decode_output(DOCTOR.encode("utf-8")), DOCTOR)

    def test_utf8_wins_when_bytes_are_valid_utf8(self):
        # 顺序要紧:UTF-8 先试。一段中文同时是合法 GBK 和合法 UTF-8 的情况下,
        # 按 GBK 解会得到另一串汉字 —— 不报错,只是错。
        self.assertEqual(V.decode_output("界面文件".encode("utf-8")), "界面文件")

    def test_garbage_does_not_raise(self):
        # 解不动也不能炸:验证脚本挂在解码上,等于什么都没验。
        got = V.decode_output(b"\xff\xfe\x00\x81\x40")
        self.assertIsInstance(got, str)

    def test_empty_is_empty(self):
        self.assertEqual(V.decode_output(b""), "")


class WebDirCheckTest(unittest.TestCase):
    """界面目录那条判据:三种坏情况都得判失败。"""

    def setUp(self):
        self.real_run, self.real_log = V.run, V.log
        self.lines: list[str] = []
        V.log = self.lines.append
        self.addCleanup(self._restore)

    def _restore(self):
        V.run, V.log = self.real_run, self.real_log

    def check(self, out: str, code: int = 0) -> bool:
        V.run = lambda exe, *a, **kw: subprocess.CompletedProcess(
            ["fake"], code, out, "")
        return V.check_doctor(Path("fake.exe"))

    def said(self, needle: str) -> bool:
        return any(needle in ln for ln in self.lines)

    def test_bundled_web_dir_passes(self):
        self.assertTrue(self.check(DOCTOR))
        self.assertTrue(self.said("界面目录在解包目录里"))

    def test_web_dir_on_the_source_tree_fails(self):
        # bundle_dir() 在冻结环境里没认出自己被打包了。在开发机上一切正常,
        # 发到别人机器上全 404 —— 正是只有打包后才暴露的那类问题。
        out = DOCTOR.replace(f"{MEI}\\web", r"D:\proj\src\strata\web")
        self.assertFalse(self.check(out))
        self.assertTrue(self.said("没落在解包目录里"))

    def test_explicit_warning_fails(self):
        # 解对编码之后,这条反向判据才真的会成立 —— 它是本来那个 bug 的原点。
        out = DOCTOR.replace("数据库:",
                             "  警告:界面目录不存在,serve 会返回 404。\n数据库:")
        self.assertFalse(self.check(out))
        self.assertTrue(self.said("没打进去"))

    def test_missing_line_fails_loudly(self):
        # 格式变了要报错,不能默默当通过 —— 那就退回原来的毛病了。
        out = "\n".join(ln for ln in DOCTOR.splitlines()
                        if not ln.endswith("web")) + "\n"
        self.assertFalse(self.check(out))
        self.assertTrue(self.said("找不到界面目录那一行"))

    def test_mojibake_still_judges_the_path(self):
        # 中文烂了也得答对:判据认的是 ASCII 路径。数据库那条当初就是靠这个
        # 躲过一劫的。
        out = ("Strata \ufffd\ufffd\n"
               f"\ufffd\ufffd:{MEI}\\web\n"
               f"\ufffd\ufffd:{DB_OK}\n")
        self.assertTrue(self.check(out))


class DbPathCheckTest(unittest.TestCase):
    """数据库不能落在 _MEIPASS —— 那是临时目录,进程一退历史就没了。"""

    def setUp(self):
        self.real_run, self.real_log = V.run, V.log
        V.log = lambda m: None
        self.addCleanup(self._restore)

    def _restore(self):
        V.run, V.log = self.real_run, self.real_log

    def check(self, out: str) -> bool:
        V.run = lambda exe, *a, **kw: subprocess.CompletedProcess(
            ["fake"], 0, out, "")
        return V.check_doctor(Path("fake.exe"))

    def test_db_in_unpack_dir_fails(self):
        out = DOCTOR.replace(DB_OK, MEI + r"\strata.db")
        self.assertFalse(self.check(out))

    def test_db_missing_fails(self):
        out = "\n".join(ln for ln in DOCTOR.splitlines()
                        if not ln.endswith(".db")) + "\n"
        self.assertFalse(self.check(out))


class NonzeroExitTest(unittest.TestCase):
    """doctor 自己挂了就别去解读它的输出了。"""

    def setUp(self):
        self.real_run, self.real_log = V.run, V.log
        V.log = lambda m: None
        self.addCleanup(self._restore)

    def _restore(self):
        V.run, V.log = self.real_run, self.real_log

    def check(self, out: str, code: int, err: str = "") -> bool:
        V.run = lambda exe, *a, **kw: subprocess.CompletedProcess(
            ["fake"], code, out, err)
        return V.check_doctor(Path("fake.exe"))

    def test_good_report_but_nonzero_still_fails(self):
        # 关键情形:报告打得漂亮,然后在后面某一步炸了,进程非 0 退出。
        # 这时候下游每条判据都会通过 —— 只有返回码这一关能拦住。
        # 用 traceback 当输出测不出这一点:那种输出下游本来就过不了,
        # 把返回码检查删掉结果也不变,等于没测。
        self.assertFalse(self.check(DOCTOR, 1))

    def test_crash_output_fails(self):
        self.assertFalse(self.check("Traceback ...", 1,
                                    "ImportError: no module named store"))

    def test_zero_with_good_report_passes(self):
        # 对照组:同一份输出,返回码 0 就该通过 —— 否则上面那条只是恒假。
        self.assertTrue(self.check(DOCTOR, 0))


if __name__ == "__main__":
    unittest.main()
