"""树图铺满的面积必须等于它自己写的总数。

背景:prune_tree 现在会写出盘根那一行(在那之前盘根下的文件在树里查无此人),
于是 treeTotal() 拿到的 node.bytes 是真的本层总量。但树图的格子只有子目录,
盖不住两部分:

  own_bytes     直接放在本目录下的文件。盘根上就是 pagefile.sys 和
                hiberfil.sys,真机上加起来常有 20 GB。
  folded_bytes  扫描时因为太小被裁掉的子目录。后端一直在给这个字段,
                前端从来没画过。

不补这一块,表头写着「本层 163 GB」而画布上的格子加起来只有 143 GB —— 数字
自相矛盾,而且矛盾的方向是「有 20 GB 你看不见」,正是这个工具要回答的问题。

用 node 跑真的 app.js 片段。没有 node 就跳过 —— 零依赖项目,不能因为缺一个
开发工具就让测试红。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from strata.server import api
from strata.store import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "strata" / "web" / "app.js"

NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');
const tree = JSON.parse(process.env.STRATA_TREE);

function grab(name) {
  const re = new RegExp('(?:async )?function ' + name + '\\([\\s\\S]*?\\n}', 'm');
  const m = src.match(re);
  if (!m) { process.stderr.write('在 app.js 里找不到 ' + name); process.exit(2); }
  return m[0];
}

const S = { tree: tree, ageFilter: null, fade: new Map() };

// 文案只要能取到就行,这个文件不测措辞(那是 test_i18n.py 的事)
function t(key, vars) { return vars ? key + ':' + JSON.stringify(vars) : key; }
function fmtCount(n) { return String(n); }
function ageBand(ctime) { return { key: 'unknown', color: '#888' }; }

eval(grab('treeItems'));
eval(grab('treeTotal'));

const items = treeItems();
process.stdout.write(JSON.stringify({
  total: treeTotal(),
  // 面积看 value,不看 bytes。squarify() 铺格子只读 value(app.js:286),
  // bytes 只进提示框 —— 拿 bytes 求和等于在验提示框的文字,画出来的图可以
  // 是另一回事。这条测试第一版就是这么写的,把 value 写成一半的错放过去了。
  covered: items.reduce((s, it) => s + it.value, 0),
  items: items.map((it) => ({
    name: it.name, path: it.path, bytes: it.bytes, value: it.value,
    isDir: !!it.isDir, synthetic: !!it.synthetic,
  })),
}));
"""


def run(tree: dict) -> dict:
    proc = subprocess.run(
        [NODE, "-e", HARNESS],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "STRATA_APP_JS": str(APP_JS),
             "STRATA_TREE": json.dumps(tree)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
    return json.loads(proc.stdout)


def tree_payload(*, node_bytes: int, own: int, folded: int = 0,
                 folded_children: int = 0, children: list | None = None) -> dict:
    """照 api.get_tree 的形状造一份。"""
    return {
        "drive": "C:",
        "path": "",
        "node": {
            "path": "",
            "bytes": node_bytes,
            "own_bytes": own,
            "files": 3,
            "dirs": 2,
            "newest_mtime": None,
            "newest_ctime": None,
            "folded_children": folded_children,
            "folded_bytes": folded,
        },
        "children": children or [],
    }


def child(path: str, size: int, *, dirs: int = 1, files: int = 1) -> dict:
    return {
        "path": path, "name": path.rsplit("\\", 1)[-1], "bytes": size,
        "own_bytes": size, "files": files, "dirs": dirs,
        "newest_mtime": None, "newest_ctime": None,
        "folded_children": 0, "folded_bytes": 0,
    }


GB = 1024**3


class ApiGivesTheNumbersTheTreemapNeedsTest(unittest.TestCase):
    """上面那块面积要靠 node.own_bytes 和 node.folded_bytes 算出来。

    这两个字段是前后端之间的约定,而 get_tree 的 node 是手写的字典 —— 少写一行
    就是 undefined,前端 `n.own_bytes || 0` 会安静地当 0,树图退回原来那个洞,
    不报错不抛异常。所以这里盯着接口的形状,不只盯前端。

    不需要 node,纯 Python。
    """

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        snap = db.Snapshot(
            drive="C:", taken_at=1000.0, method="scandir",
            total_bytes=200 * GB, free_bytes=37 * GB, used_bytes=163 * GB,
            scanned_bytes=163 * GB, complete=True,
        )
        self.sid = db.insert_snapshot(self.conn, snap)
        db.insert_dirs(self.conn, self.sid, [
            # 盘根:163 GB 里有 20 GB 是直挂根下的文件(pagefile/hiberfil),
            # 10 GB 是被裁掉的小目录
            db.DirRow(path="", depth=0, bytes=163 * GB, own_bytes=20 * GB,
                      files=3, dirs=2, newest_mtime=1000.0, newest_ctime=1000.0,
                      folded_children=1240, folded_bytes=10 * GB),
            db.DirRow(path="Windows", depth=1, bytes=100 * GB, own_bytes=100 * GB,
                      files=9, dirs=0, newest_mtime=1000.0, newest_ctime=1000.0),
            db.DirRow(path="Users", depth=1, bytes=33 * GB, own_bytes=33 * GB,
                      files=9, dirs=0, newest_mtime=1000.0, newest_ctime=1000.0),
        ])
        self.conn.commit()

    def node(self, path: str = "") -> dict:
        got = api.get_tree(self.conn, {"drive": ["C:"], "path": [path]})
        self.assertIsNotNone(got["node"], f"{path!r} 查不到")
        return got["node"]

    def test_own_bytes_is_in_the_payload(self) -> None:
        self.assertIn("own_bytes", self.node(),
                      "get_tree 不给 own_bytes,树图算不出本层文件那一块")

    def test_own_bytes_is_the_real_number(self) -> None:
        self.assertEqual(self.node()["own_bytes"], 20 * GB)

    def test_folded_bytes_is_in_the_payload(self) -> None:
        self.assertIn("folded_bytes", self.node())
        self.assertEqual(self.node()["folded_bytes"], 10 * GB)

    def test_the_three_numbers_reconcile(self) -> None:
        """本层总量 = 子目录之和 + 直属文件 + 被折叠的。

        这是树图能铺满的前提。对不上就不是前端的问题了。
        """
        got = api.get_tree(self.conn, {"drive": ["C:"], "path": [""]})
        kids = sum(c["bytes"] for c in got["children"])
        n = got["node"]
        self.assertEqual(kids + n["own_bytes"] + n["folded_bytes"], n["bytes"])

    def test_root_is_not_among_its_own_children(self) -> None:
        """盘根那一行的 depth 是 0,不能被 children_of('') 捞回来。

        捞回来的话树图上会出现一块和整层一样大的格子,点进去还是自己。
        """
        got = api.get_tree(self.conn, {"drive": ["C:"], "path": [""]})
        self.assertNotIn("", [c["path"] for c in got["children"]])


@unittest.skipIf(NODE is None, "没装 node,跳过树图覆盖检查")
class TreemapCoversItsOwnTotalTest(unittest.TestCase):
    def assertAreaMatchesLabel(self, got: dict) -> None:
        """每一格的面积和它写的字节数要一致。

        value 决定画多大,bytes 决定提示框里写多少 —— 两个字段,同一个数。
        分开写就有分开错的可能,而错开之后图上是「这块看着 10 GB,鼠标一悬
        说 20 GB」,比少画一块更难发现。
        """
        for it in got["items"]:
            self.assertEqual(
                it["value"], it["bytes"],
                f"「{it['name']}」画的面积按 {it['value']:,},标的是 {it['bytes']:,}")

    def test_root_level_files_get_a_block(self):
        """盘根:pagefile.sys + hiberfil.sys 直挂根下,必须有一块地方。"""
        got = run(tree_payload(
            node_bytes=163 * GB, own=20 * GB,
            children=[child("Windows", 100 * GB), child("Users", 43 * GB)],
        ))
        self.assertEqual(got["covered"], got["total"],
                         f"树图少画了 {got['total'] - got['covered']:,} 字节")
        self.assertAreaMatchesLabel(got)
        synth = [it for it in got["items"] if it["synthetic"]]
        self.assertEqual(len(synth), 1)
        self.assertEqual(synth[0]["bytes"], 20 * GB)

    def test_folded_dirs_get_a_block_too(self):
        """被裁掉的小目录也算在这一块里 —— 后端一直在给,以前没人画。"""
        got = run(tree_payload(
            node_bytes=110 * GB, own=0, folded=10 * GB, folded_children=1240,
            children=[child("Windows", 100 * GB)],
        ))
        self.assertEqual(got["covered"], got["total"])
        self.assertAreaMatchesLabel(got)
        synth = [it for it in got["items"] if it["synthetic"]]
        self.assertEqual(synth[0]["bytes"], 10 * GB)

    def test_own_and_folded_share_one_block(self):
        got = run(tree_payload(
            node_bytes=130 * GB, own=20 * GB, folded=10 * GB, folded_children=7,
            children=[child("Windows", 100 * GB)],
        ))
        self.assertEqual(got["covered"], got["total"])
        self.assertAreaMatchesLabel(got)
        self.assertEqual(
            [it["bytes"] for it in got["items"] if it["synthetic"]], [30 * GB])

    def test_no_block_when_nothing_is_left_over(self):
        """子目录就把本层占满时不要凭空多一块 —— 那会把总数说大。"""
        got = run(tree_payload(
            node_bytes=100 * GB, own=0, folded=0,
            children=[child("Windows", 100 * GB)],
        ))
        self.assertEqual(got["covered"], got["total"])
        self.assertEqual([it for it in got["items"] if it["synthetic"]], [])

    def test_block_is_not_clickable(self):
        """没有真实路径,所以不能是目录 —— 不然点它会请求一个不存在的路径。"""
        got = run(tree_payload(
            node_bytes=120 * GB, own=20 * GB,
            children=[child("Windows", 100 * GB)],
        ))
        synth = next(it for it in got["items"] if it["synthetic"])
        self.assertFalse(synth["isDir"], "这一块被当成了可进入的目录")

    def test_block_path_is_not_null(self):
        """路径不能是 null。

        tmHover 初值就是 null,而画的时候按 `tmHover === it.path` 判高亮 ——
        用 null 的话这一块在鼠标还没进画布时就自带白框。
        """
        got = run(tree_payload(
            node_bytes=120 * GB, own=20 * GB,
            children=[child("Windows", 100 * GB)],
        ))
        synth = next(it for it in got["items"] if it["synthetic"])
        self.assertIsNotNone(synth["path"])

    def test_missing_node_does_not_invent_a_block(self):
        """还没有 node 的时候(旧快照、空盘)不能凭空造格子。"""
        payload = tree_payload(node_bytes=0, own=0)
        payload["node"] = None
        got = run(payload)
        self.assertEqual(got["items"], [])


CELL_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');
const m = src.match(/function cellTarget\([\s\S]*?\n}/);
if (!m) { process.stderr.write('在 app.js 里找不到 function cellTarget'); process.exit(2); }
eval(m[0]);

const cases = {
  'null':      null,
  'synthetic': { item: { path: '\x00rest', isDir: false, synthetic: true } },
  'dir':       { item: { path: 'Windows', isDir: true } },
  'file':      { item: { path: 'pagefile.sys', isDir: false } },
};
const out = {};
for (const [k, v] of Object.entries(cases)) {
  const it = cellTarget(v);
  out[k] = it === null ? null : { path: it.path, isDir: !!it.isDir };
}
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipIf(NODE is None, "没装 node,跳过树图覆盖检查")
class SyntheticCellIsNotClickableTest(unittest.TestCase):
    """补出来的那一块不能被当成盘上的东西。

    点击和右键两个处理器都从 cellTarget() 取目标。哨兵路径 '\\x00rest' 要是
    漏到右键菜单里,「在资源管理器中显示」就会拿它去请求后端 —— 后端不收含 NUL
    的路径(reveal.py:56,test_reveal.py:102 盯着),所以这不是安全边界,
    而是「别让用户点出一个必然失败的菜单项」。
    """

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [NODE, "-e", CELL_HARNESS],
            capture_output=True, encoding="utf-8", errors="replace",
            env={**os.environ, "STRATA_APP_JS": str(APP_JS)},
        )
        if proc.returncode != 0:
            raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
        cls.got = json.loads(proc.stdout)

    def test_synthetic_cell_has_no_target(self):
        self.assertIsNone(self.got["synthetic"],
                          "合成的格子被当成了盘上的东西")

    def test_blank_space_has_no_target(self):
        self.assertIsNone(self.got["null"])

    def test_real_dir_passes_through(self):
        self.assertEqual(self.got["dir"], {"path": "Windows", "isDir": True})

    def test_real_file_passes_through(self):
        """文件也要过 —— 右键菜单对文件是有用的,不能一起挡掉。"""
        self.assertEqual(self.got["file"],
                         {"path": "pagefile.sys", "isDir": False})


class BothHandlersGoThroughCellTargetTest(unittest.TestCase):
    """两个处理器都得走 cellTarget,不能有一个绕过去。

    上面那些测的是 cellTarget 本身。它对不对,和处理器有没有用它,是两件事 ——
    处理器写成 `hitTest(...)` 直接取 .item,cellTarget 再正确也白搭,而上面
    四条照样全绿。事件监听器绑在 canvas 上,grab() 抠不出来,所以这条读源码。

    不需要 node。
    """

    def test_click_and_contextmenu_both_call_it(self) -> None:
        src = APP_JS.read_text(encoding="utf-8")
        for event in ("click", "contextmenu"):
            m = re.search(
                r"addEventListener\('" + event + r"', \(ev\) => \{([\s\S]*?)\n  \}\);",
                src)
            self.assertIsNotNone(m, f"找不到 {event} 的处理器,正则该改了")
            body = m.group(1)
            self.assertIn("cellTarget(", body,
                          f"{event} 没走 cellTarget,合成的格子会漏过去")
            self.assertNotIn(
                ".item", body,
                f"{event} 里还在直接读 cell.item,那就绕过了 cellTarget")


if __name__ == "__main__":
    unittest.main()
