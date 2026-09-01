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


class ToolsIndexIsHonestTest(unittest.TestCase):
    """tools/README.md 那份索引,和目录里的实际内容必须对得上。

    和上面几条同一个主题:写下来的清单会漂移,而漂移不报错。这一条有具体来路
    —— 源码注释里曾指名 5 个 tools 脚本(probe_openbyid、probe_usn、probe_rst、
    probe_roothint、probe_bad_ts),`git log` 显示它们**从来没被提交过**。当时
    是拿一次性脚本量完数就删了,注释留下了指向,读者照注释去 tools/ 找会一无所获。

    这和 hiddenimports 里那条不存在的 strata.analysis.treemap 是同一个毛病:
    引用看起来在提供证据,实际指向空气,而且没有任何东西会报错。
    """

    TOOLS = ROOT / "tools"

    def _listed(self) -> set:
        import re

        doc = (self.TOOLS / "README.md").read_text(encoding="utf-8")
        return set(re.findall(r"`([A-Za-z_0-9]+\.(?:py|bat|spec|manifest))`", doc))

    def _on_disk(self) -> set:
        return {
            p.name
            for p in self.TOOLS.glob("*")
            if p.is_file() and p.name != "README.md"
        }

    def test_index_has_no_phantom_entries(self):
        """索引里的每个文件名都得真在 tools/ 下。"""
        phantom = sorted(self._listed() - self._on_disk())
        self.assertEqual(
            phantom, [],
            f"tools/README.md 提到了这些文件,但 tools/ 下没有:{phantom}。"
            f"指向空气的引用比没有引用更糟 —— 读者会去找。",
        )

    def test_every_tool_is_in_the_index(self):
        """tools/ 下的每个文件都得在索引里 —— 新加脚本别忘了写一行。"""
        missing = sorted(self._on_disk() - self._listed())
        self.assertEqual(
            missing, [],
            f"这些文件在 tools/ 下,但 tools/README.md 没提:{missing}。"
            f"索引漏一个,那个脚本就等于没人知道它是干什么的。",
        )


class NoDanglingToolReferencesTest(unittest.TestCase):
    """源码、测试、文档里写 tools/xxx.py 的地方,那个文件必须真存在。

    这一条抓的正是上面 docstring 里说的 5 处。变异验证:把任意一处注释改回
    `tools/probe_usn.py`,这条立刻红。
    """

    PATTERN = r"tools/([A-Za-z_0-9]+\.(?:py|bat|spec|manifest))"

    # 这个文件自己不扫:上面几段 docstring 为了讲清问题,引用了几个**故意不存在**
    # 的脚本名当例子。扫自己会把说明文字当成真引用 —— 第一次跑就撞上了。
    #
    # docs/superpowers/ 也不扫:那是 .gitignore 里的本地计划文档,不进仓库,
    # 里面记的是当时那几个一次性脚本,属于历史记录而不是给读者的指路牌。
    SKIP = ("__pycache__", "superpowers")

    def test_all_cited_tools_exist(self):
        import re

        on_disk = {p.name for p in (ROOT / "tools").glob("*") if p.is_file()}
        searched = 0
        dangling = []
        for folder in ("src", "tests", "docs"):
            for path in (ROOT / folder).rglob("*"):
                if path.suffix not in (".py", ".md", ".js", ".sql"):
                    continue
                if any(part in self.SKIP for part in path.parts):
                    continue
                if path.resolve() == Path(__file__).resolve():
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                searched += 1
                for name in re.findall(self.PATTERN, text):
                    if name not in on_disk:
                        rel = path.relative_to(ROOT).as_posix()
                        dangling.append(f"{rel} 引用了 tools/{name}")
        self.assertGreater(searched, 50, "扫到的文件太少,路径是不是变了")
        self.assertEqual(
            dangling, [],
            "这些地方引用的 tools 脚本不存在 —— 注释在指向空气:\n  "
            + "\n  ".join(dangling),
        )


if __name__ == "__main__":
    unittest.main()
