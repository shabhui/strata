"""tools/strata.spec 里那几份清单必须说的是真话。

来路:一次打包日志里有这么一行,而 exe 照样打出来了、照样能跑:

    ERROR: Hidden import 'strata.analysis.treemap' not found

那个模块**从来不存在**,源码里也没有任何地方 import 它。PyInstaller 找不到就
打一行 ERROR 继续走,退出码还是 0,所以这条假条目安安稳稳待了很久没人发现。
更要紧的是反面:真实存在的 analysis/diff.py、hotspots.py、paths.py 三个反倒
没列在 hiddenimports 里。

于是那份清单当时的实际状态是 —— 一条在保护不存在的东西,三条该保护的没保护,
而它看起来在保护十个。spec 里那句注释写着「少一个就是运行到那条命令才崩」,
清单本身却漏了三个:注释在说一件事,代码在做另一件事,中间没有任何东西拦着。

这就是项目里那句「只会通过的检查比没有检查更糟」的又一例:清单不报错,不等于
清单是对的。所以钉三条:

  1. hiddenimports 里每个名字都得真能 import
  2. datas 里每个源路径都得真存在(漏了要到打包出来点开页面才发现)
  3. src/strata/analysis/ 下的模块都得在 hiddenimports 里(那几个是
     __main__ 的 cmd_* 在函数体内 import 的,静态分析之外没有第二道保险)

不打包、不装 PyInstaller 也能跑 —— spec 是普通 Python 源码,用 AST 读它就行。
不 exec 它:里面有 Analysis()/EXE() 这些只在 PyInstaller 进程里才有的名字。
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tools" / "strata.spec"
SRC = ROOT / "src"


def _spec_kwarg(name: str) -> list:
    """从 spec 里取 Analysis(...) 的一个列表参数,按字面量求值。

    用 AST 而不是 exec:spec 在 PyInstaller 进程里才有 Analysis/PYZ/EXE 这些
    注入的全局名,直接 exec 会 NameError。而这几个参数都是字面量列表,
    literal_eval 够了 —— 顺带保证了这条测试读的是「写在那儿的东西」,
    不是「跑起来碰巧算成的东西」。
    """
    tree = ast.parse(SPEC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Analysis"):
            continue
        for kw in node.keywords:
            if kw.arg != name:
                continue
            if name == "datas":
                # datas 的元素是 (str(SRC / ...), "web") —— 不是字面量,
                # 取不了值。这里只要第二项(打包内的目标路径)是字面量,
                # 第一项交给 test_datas_sources_exist 用别的办法验。
                return kw.value.elts
            return ast.literal_eval(kw.value)
    raise AssertionError(f"strata.spec 里找不到 Analysis(..., {name}=...)")


class HiddenImportsAreRealTest(unittest.TestCase):
    def setUp(self):
        import sys

        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))

    def test_every_hidden_import_can_actually_be_imported(self):
        """清单里每个名字都得真能 import。

        这一条就是当初该抓住 strata.analysis.treemap 的那条。变异验证:往
        hiddenimports 里加一个 'strata.analysis.nope',这条测试立刻红。
        """
        names = _spec_kwarg("hiddenimports")
        self.assertGreater(len(names), 5, "清单短得可疑,是不是被清空了")
        for name in names:
            with self.subTest(name=name):
                try:
                    importlib.import_module(name)
                except ImportError as exc:
                    self.fail(
                        f"strata.spec 的 hiddenimports 里写着 {name!r},"
                        f"但它 import 不了({exc})。PyInstaller 遇到这种只会打"
                        f"一行 ERROR 然后继续,exe 照样打出来 —— 所以这里得拦住。"
                    )

    def test_every_analysis_module_is_listed(self):
        """analysis/ 下的模块都得在清单里 —— 漏的那三个就是这么漏的。

        为什么单挑 analysis:__main__ 里各个 cmd_* 是在函数体内 import 它们的,
        静态分析之外没有第二道保险,而这个目录还在长新模块。
        """
        listed = set(_spec_kwarg("hiddenimports"))
        on_disk = {
            f"strata.analysis.{p.stem}"
            for p in (SRC / "strata" / "analysis").glob("*.py")
            if p.stem != "__init__"
        }
        self.assertTrue(on_disk, "analysis 目录下一个模块都没找到,路径是不是变了")
        missing = sorted(on_disk - listed)
        self.assertEqual(
            missing, [],
            f"这些模块在 src/strata/analysis/ 里真实存在,却没写进 spec 的 "
            f"hiddenimports:{missing}。spec 那句注释说的是「不指望静态分析」,"
            f"漏了就等于指望了。",
        )


class DatasSourcesExistTest(unittest.TestCase):
    def test_every_bundled_path_is_on_disk(self):
        """datas 里每个源路径都得真存在。漏了要到打包出来点开页面才发现。

        spec 里 datas 的源是 str(SRC / "strata" / "web") 这种表达式,不是字面量,
        所以不能 literal_eval。这里把 SRC 绑成真值之后单独 eval 每一项的第一元素
        —— 只 eval 这一个表达式,不 exec 整个 spec。
        """
        env = {"SRC": SRC, "ROOT": ROOT, "str": str, "Path": Path}
        for elt in _spec_kwarg("datas"):
            self.assertIsInstance(
                elt, ast.Tuple, "datas 的元素应该是 (源, 目标) 二元组"
            )
            src_expr = ast.unparse(elt.elts[0])
            dest = ast.literal_eval(elt.elts[1])
            with self.subTest(dest=dest):
                path = Path(eval(src_expr, env))  # noqa: S307
                self.assertTrue(
                    path.exists(),
                    f"spec 要把 {path} 打进 exe 的 {dest!r},但这个路径不存在。"
                    f"PyInstaller 对缺失的 datas 只是警告,漏了要到运行时才崩。",
                )


if __name__ == "__main__":
    unittest.main()
