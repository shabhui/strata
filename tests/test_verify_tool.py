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

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import verify_exe as V  # noqa: E402

MEI = r"C:\Users\alice\AppData\Local\Temp\_MEI123456"
DB_OK = r"C:\Users\alice\AppData\Local\Strata\strata.db"

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


class SourceFingerprintTest(unittest.TestCase):
    """源码指纹:回答「这个 exe 是哪版代码打出来的」。

    不加这个的话,整套验证能全过而 exe 是三天前的 —— 报告没说假话,只是说的
    不是你以为的那个二进制。
    """

    def setUp(self):
        import sources
        self.S = sources
        self.tmp = Path(tempfile.mkdtemp(prefix="tc-fp-")) / "fp.json"
        self.addCleanup(shutil.rmtree, self.tmp.parent, ignore_errors=True)

    def test_covers_everything_the_spec_bundles(self):
        # 漏了哪一类文件,那类文件改了就不会被发现。
        rels = set(self.S.fingerprint())
        self.assertIn("tools/entry.py", rels)               # 入口
        self.assertIn("tools/strata.spec", rels)            # datas 漏一项就崩
        self.assertIn("tools/strata.manifest", rels)        # 提权靠它
        self.assertIn("src/strata/web/app.js", rels)        # datas 带的
        self.assertIn("src/strata/store/schema.sql", rels)  # datas 带的
        self.assertIn("src/strata/__main__.py", rels)

    def test_no_pyc_in_the_fingerprint(self):
        # .pyc 会跟着解释器版本变,进指纹就会无缘无故报过期。
        self.assertFalse([r for r in self.S.fingerprint()
                          if r.endswith((".pyc", ".pyo")) or "__pycache__" in r])

    def test_no_fingerprint_file_is_missing_not_stale(self):
        # 分清「验不了」和「验过了不对」。当成 stale 会让人白重打一次包。
        self.assertEqual(self.S.compare(self.tmp)[0], "missing")

    def test_unchanged_tree_is_same(self):
        self.S.write(self.tmp)
        self.assertEqual(self.S.compare(self.tmp)[0], "same")

    def test_changed_content_is_stale(self):
        self.S.write(self.tmp)
        was = json.loads(self.tmp.read_text(encoding="utf-8"))
        was["src/strata/web/app.js"] = "0" * 16
        self.tmp.write_text(json.dumps(was), encoding="utf-8")
        verdict, diff = self.S.compare(self.tmp)
        self.assertEqual(verdict, "stale")
        self.assertIn("改了 src/strata/web/app.js", diff)

    def test_deleted_file_is_stale(self):
        self.S.write(self.tmp)
        was = json.loads(self.tmp.read_text(encoding="utf-8"))
        was["src/strata/nonexistent.py"] = "abc"
        self.tmp.write_text(json.dumps(was), encoding="utf-8")
        verdict, diff = self.S.compare(self.tmp)
        self.assertEqual(verdict, "stale")
        self.assertIn("删掉 src/strata/nonexistent.py", diff)

    def test_added_file_is_stale(self):
        self.S.write(self.tmp)
        was = json.loads(self.tmp.read_text(encoding="utf-8"))
        was.pop("src/strata/__main__.py")
        self.tmp.write_text(json.dumps(was), encoding="utf-8")
        verdict, diff = self.S.compare(self.tmp)
        self.assertEqual(verdict, "stale")
        self.assertIn("新增 src/strata/__main__.py", diff)

    def test_line_endings_do_not_count(self):
        # 仓库是 worktree CRLF / index LF。按原始字节算的话,git 重写一遍换行
        # 就报过期 —— 而误报久了人就不看警告了。
        src = "def f():\r\n    return 1\r\n"
        a = Path(self.tmp.parent, "a.py"); a.write_text(src, newline="")
        b = Path(self.tmp.parent, "b.py"); b.write_text(src.replace("\r\n", "\n"), newline="")
        self.assertEqual(self.S.digest_of(a), self.S.digest_of(b))

    def test_content_change_does_count(self):
        # 对照组:上面那条不能是「什么都算一样」。
        a = Path(self.tmp.parent, "c.py"); a.write_text("return 1\n", newline="")
        b = Path(self.tmp.parent, "d.py"); b.write_text("return 2\n", newline="")
        self.assertNotEqual(self.S.digest_of(a), self.S.digest_of(b))

    def test_unreadable_fingerprint_is_missing_not_crash(self):
        # 文件坏了要退回「验不了」,不能让验证脚本自己炸在这儿。
        self.tmp.parent.mkdir(parents=True, exist_ok=True)
        self.tmp.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(self.S.compare(self.tmp)[0], "missing")


class CaveatTest(unittest.TestCase):
    """「没验上」不能被末尾那句「可以发出去了」盖过去。

    警告埋在几十行输出中间等于没有 —— 人只看最后一行。
    """

    def setUp(self):
        self.real = list(V.caveats)
        V.caveats.clear()
        self.addCleanup(lambda: (V.caveats.clear(), V.caveats.extend(self.real)))

    def test_missing_fingerprint_records_a_caveat(self):
        import sources
        real_compare, real_log = sources.compare, V.log
        V.log = lambda m: None
        sources.compare = lambda *a, **kw: ("missing", [])
        try:
            if not V.RELEASE_EXE.exists():
                self.skipTest("没有 dist/Strata.exe")
            self.assertTrue(V.check_fresh())      # 不算失败
            self.assertTrue(V.caveats)            # 但也不算验过了
        finally:
            sources.compare, V.log = real_compare, real_log

    def test_match_records_nothing(self):
        import sources
        real_compare, real_log = sources.compare, V.log
        V.log = lambda m: None
        sources.compare = lambda *a, **kw: ("same", [])
        try:
            if not V.RELEASE_EXE.exists():
                self.skipTest("没有 dist/Strata.exe")
            self.assertTrue(V.check_fresh())
            self.assertFalse(V.caveats)
        finally:
            sources.compare, V.log = real_compare, real_log

    def test_stale_is_a_failure_not_a_caveat(self):
        # 过期是明确的失败,不是「没验上」—— 别降级成一条提示。
        import sources
        real_compare, real_log = sources.compare, V.log
        V.log = lambda m: None
        sources.compare = lambda *a, **kw: ("stale", ["改了 src/strata/x.py"])
        try:
            if not V.RELEASE_EXE.exists():
                self.skipTest("没有 dist/Strata.exe")
            self.assertFalse(V.check_fresh())
            self.assertFalse(V.caveats)
        finally:
            sources.compare, V.log = real_compare, real_log


if __name__ == "__main__":
    unittest.main()
