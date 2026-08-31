"""strata.bat 必须是纯 ASCII,而且不许用 chcp。

这条规矩是踩出来的,值得把过程记下来,免得有人觉得"加句中文提示怎么了"。

起点:文件存 UTF-8,中文 Windows 控制台默认代码页 936,于是启动器里每句中文都是
乱码 —— 偏偏乱的就是「提权失败或被取消」「找不到 Python」这几句,也就是出事时
唯一能告诉用户发生了什么的话。

第一次修:开头加 `chcp 65001`。这个修法把启动器彻底弄坏了。cmd 用**字节偏移**
记自己读到文件哪儿,中途换代码页,字节→字符的映射变了,偏移就错位,后面每个
多字节汉字都在加剧漂移。实际现象:

    'bat' 不是内部或外部命令   <- strata.bat 的尾巴
    'un' 不是内部或外部命令    <- :run 的尾巴
    Python 3.12.10 ... >>>     <- 那一行被读残,-m strata serve 丢了

复现时还看到 `'em'`(rem 的尾巴)。注意漂移要攒够多字节内容才出现,3 行汉字的
最小复现是绿的 —— 所以"我试了一下没问题"不能作为证据。

第二个被否掉的选项:文件存成 GBK。中文 Windows 好了,其他地区全乱,而仓库刚加了
英文界面和英文 README,方向正好相反。

所以最后是:整个文件纯 ASCII,提示用英文,不用 chcp。Python 起来之后的界面照旧
中文 —— Python 自己管得住编码。

这里钉三件事:没有生效的 chcp、一个非 ASCII 字节都没有、没有 BOM。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

BAT = Path(__file__).resolve().parents[1] / "strata.bat"


class LauncherEncodingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = BAT.read_bytes()
        self.text = self.raw.decode("ascii", errors="replace")
        self.lines = self.text.splitlines()

    def test_is_pure_ascii(self) -> None:
        """一个非 ASCII 字节都不许有,注释也算。

        注释里的汉字不会打印(rem 不输出),但它照样是要被 cmd 按代码页解码的字节;
        真正的理由是这条规矩必须简单到不用讨论 —— "全 ASCII" 谁都能验,
        "只有 echo 行不许有中文"迟早被绕过去。
        """
        try:
            self.raw.decode("ascii")
        except UnicodeDecodeError as exc:
            off = exc.start
            line = self.raw[:off].count(b"\n") + 1
            snippet = self.raw[max(0, off - 30):off + 30].decode("utf-8", "replace")
            self.fail(
                f"strata.bat 第 {line} 行(字节 {off})有非 ASCII:{snippet!r}\n"
                f"这个文件必须全 ASCII,理由见模块顶上的注释 —— "
                f"要加中文提示,加在 Python 那一侧"
            )

    def test_no_chcp(self) -> None:
        """不许有生效的 chcp。注释里提这个词可以(顶上就在解释为什么不能用)。"""
        offenders = [
            (i + 1, ln.strip())
            for i, ln in enumerate(self.lines)
            if re.search(r"\bchcp\b", ln, re.I) and not ln.strip().lower().startswith("rem")
        ]
        self.assertEqual(
            offenders,
            [],
            "strata.bat 里出现了生效的 chcp。中途换代码页会让 cmd 的字节偏移错位,"
            "启动器会读残行(实测打出 'bat'、'un',并起了个裸 Python REPL)。"
            f"位置:{offenders}",
        )

    def test_no_bom(self) -> None:
        """带 BOM 的话 cmd 会把它当成第一条命令的一部分,报个莫名其妙的错。"""
        self.assertFalse(
            self.raw.startswith(b"\xef\xbb\xbf"), "strata.bat 不该带 BOM"
        )

    def test_crlf_line_endings(self) -> None:
        """.bat 用 CRLF。裸 LF 在老 cmd 上会出怪问题,而且这文件是给 Windows 的。"""
        bare_lf = self.raw.count(b"\n") - self.raw.count(b"\r\n")
        self.assertEqual(bare_lf, 0, f"有 {bare_lf} 个裸 LF,.bat 应该全是 CRLF")

    def test_still_does_the_things_it_is_for(self) -> None:
        """别为了满足上面几条把功能删了。

        纯 ASCII 很容易靠"把文件清空"达成,所以这里钉住它该干的事还在。

        只看非 rem 行。第一版没排除注释,结果顶上那段解释里就写着
        `-m strata serve`,把真命令换成 echo TODO 测试照样绿 ——
        又是一条只会通过的检查。
        """
        code = "\n".join(
            ln for ln in self.lines if not ln.strip().lower().startswith("rem")
        )
        for what, pattern in (
            ("提权判断", r"net session"),
            ("提权", r"-Verb RunAs"),
            ("找 Python", r"where (?:py|python)"),
            ("起服务", r"-m strata serve"),
            ("切到脚本所在目录", r"cd /d"),
        ):
            with self.subTest(what):
                self.assertRegex(code, pattern, f"{what} 那一段不见了")


if __name__ == "__main__":
    unittest.main()
