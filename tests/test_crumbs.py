"""面包屑发出去的路径,必须是库认的那种路径。

schema.sql 写着:`目录汇总。path 用反斜杠、不含盘符、根目录为空串。`
后端 get_dir() 就按这个口径查表。面包屑上每一级都是一个「跳到这里」的按钮,
它给 /api/tree 的 path 只要不合口径,那一级就查不到东西 —— 而且不会报错:
get_tree() 返回 node=None、children=[],前端拿到的是一次成功的空响应,
画出来是一张空图,表头还留着上一层的数字。

这条测试就盯这一件事:crumbTrail(rel) 吐出来的每个 path 都得能原样喂给后端。

用 node 跑真的 app.js,不用 Python 重写一遍路径拼接 —— 重写一遍就等于把
同一个 bug 抄两份,两边都错的时候测试照样绿。没装 node 就跳过,这是零依赖
项目,不能因为缺个开发工具让测试红。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "strata" / "web" / "app.js"
SCHEMA = ROOT / "src" / "strata" / "store" / "schema.sql"

NODE = shutil.which("node")

# app.js 是给浏览器用的普通脚本,没有导出。这里把 crumbTrail 那一段单独抠出来
# 在 node 里跑 —— 抠的是源文件里的真代码,不是复制品:正则没匹配上就直接失败,
# 函数改了名或者被删了,这条测试会报「找不到」而不是悄悄跳过。
PROBE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');

const m = src.match(/function crumbTrail\([\s\S]*?\n}/);
if (!m) {
  process.stderr.write('在 app.js 里找不到 function crumbTrail');
  process.exit(2);
}
eval(m[0]);

const cases = [
  '',
  'Users',
  'Users\\alice',
  'Users\\alice\\Downloads',
  'Program Files\\Common Files\\microsoft shared',
];
const out = {};
for (const c of cases) out[c] = crumbTrail(c);
process.stdout.write(JSON.stringify(out));
"""


def trails() -> dict[str, list[dict]]:
    proc = subprocess.run(
        [NODE, "-e", PROBE],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ, "STRATA_APP_JS": str(APP_JS)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来 crumbTrail:\n{proc.stderr}")
    return json.loads(proc.stdout)


class TestPathContractIsWritten(unittest.TestCase):
    """先确认口径还在 schema 里写着 —— 下面所有断言都以它为准。"""

    def test_schema_states_the_contract(self):
        text = SCHEMA.read_text(encoding="utf-8")
        self.assertIn("不含盘符", text, "schema.sql 里的路径口径不见了,先确认口径有没有变")
        self.assertIn("根目录为空串", text)


@unittest.skipIf(NODE is None, "没装 node,跳过面包屑路径检查")
class TestCrumbTrail(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.trails = trails()

    def test_root_is_empty_string(self):
        """根目录那一级发空串,不是 `C:\\`。"""
        trail = self.trails[""]
        self.assertEqual(len(trail), 1, f"根目录应该只有一级,得到 {trail}")
        self.assertEqual(trail[0]["path"], "")

    def test_no_drive_letter_anywhere(self):
        for rel, trail in self.trails.items():
            for i, crumb in enumerate(trail):
                self.assertNotRegex(
                    crumb["path"], r"^[A-Za-z]:",
                    f"rel={rel!r} 第 {i} 级带了盘符:{crumb['path']!r}。"
                    f"schema.sql:31 说 path 不含盘符",
                )

    def test_no_trailing_separator(self):
        """尾部反斜杠是这次的病根:父级发的是 `Users\\`,查不到。"""
        for rel, trail in self.trails.items():
            for i, crumb in enumerate(trail):
                p = crumb["path"]
                self.assertFalse(
                    p.endswith("\\"),
                    f"rel={rel!r} 第 {i} 级尾部带反斜杠:{p!r}",
                )

    def test_each_level_is_a_prefix_of_the_current_path(self):
        """每一级都得是当前路径的真前缀,顺序从浅到深。"""
        for rel, trail in self.trails.items():
            paths = [c["path"] for c in trail]
            self.assertEqual(paths[0], "", f"rel={rel!r} 第一级必须是根")
            self.assertEqual(paths[-1], rel, f"rel={rel!r} 最后一级必须是它自己,得到 {paths[-1]!r}")
            for shallow, deep in zip(paths, paths[1:]):
                if shallow == "":
                    continue
                self.assertTrue(
                    deep.startswith(shallow + "\\"),
                    f"rel={rel!r}:{deep!r} 不是 {shallow!r} 的子路径",
                )

    def test_level_count_matches_depth(self):
        """N 段路径出 N+1 级(多的那个是根)。"""
        for rel, trail in self.trails.items():
            depth = len([p for p in rel.split("\\") if p])
            self.assertEqual(
                len(trail), depth + 1,
                f"rel={rel!r} 深度 {depth},应该 {depth + 1} 级,得到 {len(trail)}",
            )

    def test_only_last_level_is_current(self):
        """只有最深那一级是「当前位置」,其余都可点。"""
        for rel, trail in self.trails.items():
            flags = [bool(c["current"]) for c in trail]
            self.assertEqual(
                flags, [False] * (len(trail) - 1) + [True],
                f"rel={rel!r} 的 current 标记不对:{flags}",
            )

    def test_labels_survive_spaces(self):
        """带空格的目录名不能被拆开。"""
        trail = self.trails["Program Files\\Common Files\\microsoft shared"]
        self.assertEqual(
            [c["label"] for c in trail][1:],
            ["Program Files", "Common Files", "microsoft shared"],
        )

    def test_tolerates_a_full_path_being_passed_in(self):
        """万一有人喂进来带盘符的路径,也得归一化掉,不能原样传给后端。

        这是防回归:S.path 的来源不止一处(点方块、右键菜单、面包屑自己),
        以后再多一个来源时,这里兜住。
        """
        proc = subprocess.run(
            [NODE, "-e", PROBE.replace(
                "const cases = [",
                "const cases = ['C:\\\\Users\\\\alice', 'C:\\\\Users\\\\', 'C:\\\\', ",
            )],
            capture_output=True, encoding="utf-8", errors="replace",
            env={**os.environ, "STRATA_APP_JS": str(APP_JS)},
        )
        if proc.returncode != 0:
            raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
        got = json.loads(proc.stdout)

        self.assertEqual([c["path"] for c in got["C:\\Users\\alice"]],
                         ["", "Users", "Users\\alice"])
        self.assertEqual([c["path"] for c in got["C:\\Users\\"]], ["", "Users"])
        self.assertEqual([c["path"] for c in got["C:\\"]], [""])


if __name__ == "__main__":
    unittest.main()
