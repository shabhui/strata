"""「消失的东西」那张表里的行,不能对用户撒谎。

跑的是 app.js 里真的 renderChanges —— 用正则把函数原文抠出来在 node 里执行,
DOM 那几个帮手换成桩,然后看它到底往表里塞了什么。不在 Python 里重写一遍行
逻辑:重写就是把同一个 bug 抄两份,两边都错的时候测试照样绿。

盯三件事:

1. 反查不出路径的行(只有文件名)不能标成可定位。右键菜单把那一格的文字当
   盘内相对路径,拼出来是「D:\\problems-report.html」并且印在菜单顶上 ——
   那个位置这文件从来没待过。就算点下去,reveal 也会说「这个路径已经不在了
   (可能在上次扫描之后被删了)」,而真相是从来不知道它在哪。

2. 这种行得看得出来跟真路径不一样。实盘上 problems-report.html 出现 5 次、
   classes 5 次 —— 不同目录里的不同文件,界面上却一模一样,像是列表坏了。
   (不能按名字合并:那 5 个真是 5 个文件。)

3. 后端把同一个文件的多条 USN 记录合成一行之后,折叠掉的次数要显示出来。
   一个临时文件被删 115 次跟「丢了个东西」是两回事。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "strata" / "web" / "app.js"

NODE = shutil.which("node")

PROBE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');

function grab(name) {
  const re = new RegExp('function ' + name + '\\([\\s\\S]*?\\n}');
  const m = src.match(re);
  if (!m) { process.stderr.write('app.js 里找不到 function ' + name); process.exit(2); }
  return m[0];
}

// 真代码:行怎么拼的在这两个函数里
eval(grab('renderChanges'));
eval(grab('pathCell'));

// ---- 桩。只做到够 renderChanges 跑完,不模拟浏览器 ----
const S = { changes: null,
            status: { privileges: { is_admin: process.env.STRATA_IS_ADMIN === '1' } } };
let captured = null;
let noticeText = '';

const fillRows = (id, rows, empty) => { captured = { id, rows, empty }; };
const el = (id) => ({
  id, hidden: false,
  appendChild(n) { if (id === 'usnNotice') noticeText += (n.__text || ''); },
});
const clear = () => {};
// 够用的节点:能挂子节点,并把文字一路往上累加,这样 notice 那段拼出来的
// 整句话能一次读到。childNodes 也留着 —— renderChanges 拿它判断要不要挂上去。
function node(name, attrs, text) {
  return {
    name, attrs, __text: text || '', childNodes: [],
    appendChild(n) { this.childNodes.push(n); this.__text += (n.__text || ''); return n; },
  };
}
const tag = (name, attrs, text) => node(name, attrs, text);
const document = { createTextNode: (s) => node('#text', null, s) };
// 文案换成 key 本身,断言就不跟具体措辞绑死;带参数的把参数也带上
const t = (key, vars) => vars ? key + '(' + JSON.stringify(vars) + ')' : key;
const fmtCount = (n) => String(n);
const fmtBytes = (n) => n + 'B';
const fmtTime = () => 'T';

renderChanges(JSON.parse(process.env.STRATA_PAYLOAD));

process.stdout.write(JSON.stringify({
  rows: (captured && captured.rows) || null,
  empty: captured && captured.empty,
  notice: noticeText,
}));
"""


def render(events: list[dict], *, coverage: dict | None = None,
           is_admin: bool = True) -> dict:
    payload = {
        "coverage": coverage or {"events": len(events), "first_day": "2026-01-01", "days": 30},
        "events": events,
    }
    env = dict(os.environ)
    env["STRATA_APP_JS"] = str(APP_JS)
    env["STRATA_PAYLOAD"] = json.dumps(payload)
    env["STRATA_IS_ADMIN"] = "1" if is_admin else "0"
    proc = subprocess.run(
        [NODE, "-e", PROBE], capture_output=True, text=True, encoding="utf-8", env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来(码 {proc.returncode}):{proc.stderr}")
    return json.loads(proc.stdout)


def ev(**kw) -> dict:
    row = {"kind": "delete", "at": 1.0, "is_dir": False, "usn": 1,
           "name": "x", "path": None, "bytes": None, "count": 1}
    row.update(kw)
    return row


@unittest.skipIf(NODE is None, "没装 node,跳过(零依赖项目,不能因为缺开发工具让测试红)")
class DeletedRowsDoNotLie(unittest.TestCase):
    def cells(self, events: list[dict]) -> list[list[dict]]:
        out = render(events)
        self.assertIsNotNone(out["rows"], "renderChanges 没往 deletedBody 塞任何行")
        return out["rows"]

    def test_a_real_path_is_locatable(self) -> None:
        """有路径的行照旧:标成文件,右键能定位。"""
        (cell, *_), = self.cells([ev(name="a.log", path="proj\\a.log", bytes=10)])
        self.assertEqual(cell["attrs"]["data-file"], "1")
        self.assertNotIn("data-noloc", cell["attrs"])

    def test_a_name_only_row_is_not_marked_locatable(self) -> None:
        """只有名字的行不能标成可定位 —— 否则菜单会印出一个假位置。"""
        (cell, *_), = self.cells([ev(name="problems-report.html", path=None)])
        self.assertNotIn(
            "data-file", cell["attrs"],
            "标成了可定位。右键菜单会把这个名字当盘内相对路径,顶上印出 "
            "「D:\\problems-report.html」—— 这文件从来没在那儿",
        )
        self.assertEqual(
            cell["attrs"].get("data-noloc"), "1",
            "没打 data-noloc,bindCtxMenu 就没法跳过这一行",
        )

    def test_a_name_only_row_looks_different(self) -> None:
        """看得出来跟真路径不一样,还得说清为什么。

        实盘上 problems-report.html 出现 5 次、classes 5 次,是不同目录里的不同
        文件。界面上一模一样的话,看着像列表坏了。
        """
        (cell, *_), = self.cells([ev(name="classes", path=None)])
        self.assertIn("dim", cell["attrs"]["class"], "没弱化,看着跟真路径一样")
        self.assertIn("del.noPathRow", cell["title"], "悬停上去没有解释")

    def test_the_note_counts_name_only_rows(self) -> None:
        """整段说明里要报出有多少条是这种行。"""
        out = render([ev(name="a", path=None), ev(name="b", path=None),
                      ev(name="c", path="d\\c", bytes=5)])
        self.assertIn("del.noPathNote", out["notice"])
        self.assertIn('"n":"2"', out["notice"], f"数错了:{out['notice']}")

    def test_no_note_when_every_row_has_a_path(self) -> None:
        """全都有路径的时候别摆一句「另有 0 条」。"""
        out = render([ev(name="c", path="d\\c", bytes=5)])
        self.assertNotIn("del.noPathNote", out["notice"])

    def test_collapsed_count_is_shown(self) -> None:
        """后端合并了几条要显示出来 —— 删 115 次跟丢一个东西不是一回事。"""
        cells = self.cells([ev(name="j", path="d\\bc_09.db-journal", count=115)])
        (path_cell, _size, time_cell) = cells[0]
        self.assertIn("del.times", time_cell["text"])
        self.assertIn('"n":"115"', time_cell["text"])
        self.assertIn("del.timesNote", path_cell["title"])

    def test_a_single_record_shows_no_count(self) -> None:
        """只有一条的时候别挂个「×1」,那是噪声。"""
        cells = self.cells([ev(name="a", path="d\\a.log", count=1)])
        self.assertNotIn("del.times", cells[0][2]["text"])

    def test_old_backend_without_count_is_fine(self) -> None:
        """字段缺失也不能印出「×undefined」。"""
        row = ev(name="a", path="d\\a.log")
        del row["count"]
        cells = self.cells([row])
        self.assertNotIn("undefined", cells[0][2]["text"])
        self.assertNotIn("del.times", cells[0][2]["text"])

    def test_name_only_row_still_shows_its_name(self) -> None:
        """往后排、不给菜单,但不能藏起来 —— 「有个叫这名字的东西被删了」也是信息。"""
        (cell, *_), = self.cells([ev(name="results.bin", path=None)])
        self.assertEqual(cell["text"], "results.bin")


@unittest.skipIf(NODE is None, "没装 node,跳过")
class ZeroEventsSaysWhichKindOfZero(unittest.TestCase):
    """一条事件都没有,有三种原因,界面不能都显示成同一个样子。

        没提权              → 说要提权(这条早就修了)
        提了权但读失败      → 说为什么失败。日志可以关,不少机器默认就是关的
        提了权、读成了、真空 → 照旧藏起来,别摆一张空表让人以为什么都没删过

    第二条以前跟第三条走同一条路:整段藏掉。用户看到的跟「什么都没删过」一模
    一样,而真相是「我没看成」。后端现在把原因存下来了(usn_status 表),
    这一组盯着前端有没有把它显示出来。
    """

    def test_failed_read_is_explained_not_hidden(self) -> None:
        out = render([], coverage={"events": 0, "available": False,
                                   "reason": "USN 日志没有启用"})
        self.assertIn(
            "del.unavailable", out["notice"],
            "读失败了却没解释 —— 面板藏起来,看着跟「什么都没删过」一样",
        )
        self.assertIn("USN 日志没有启用", out["notice"],
                      "没把后端给的具体原因带出来")

    def test_failed_read_shows_a_short_empty_row(self) -> None:
        """表格空态要短,长解释在 notice 里,别两处都写整段。"""
        out = render([], coverage={"events": 0, "available": False, "reason": "没权限"})
        self.assertIsNotNone(out["rows"], "读失败时表格没被填成空态")
        self.assertEqual(out["rows"], [])
        self.assertIn("del.unavailableRow", out["empty"] or "")

    def test_clean_read_with_nothing_deleted_stays_hidden(self) -> None:
        """读成了、确实一条没删:照旧藏。别摆空表让人以为什么都没删过。"""
        out = render([], coverage={"events": 0, "available": True, "reason": None})
        self.assertNotIn("del.unavailable", out["notice"],
                         "真的空却报了故障 —— 这是在编事实")

    def test_never_scanned_does_not_claim_a_failure(self) -> None:
        """没扫过的盘 available 是 null,不能显示成故障。"""
        out = render([], coverage={"events": 0, "available": None, "reason": None})
        self.assertNotIn("del.unavailable", out["notice"],
                         "没扫过被说成了读取失败")

    def test_no_admin_still_wins(self) -> None:
        """没提权的时候还是先说提权 —— 那是用户能动手解决的那一个。

        不提权时后端整段跳过 USN,连 usn_status 都不会写,所以这两个条件会同时
        成立。先说提权:重新以管理员身份启动就能解决,而「日志没开」的解释在
        这种情况下是误导。
        """
        out = render([], coverage={"events": 0, "available": False, "reason": "需要管理员权限"},
                     is_admin=False)
        self.assertIn("del.needAdmin", out["notice"])
        self.assertNotIn("del.unavailable", out["notice"])


@unittest.skipIf(NODE is None, "没装 node,跳过")
class ContextMenuSkipsNameOnlyRows(unittest.TestCase):
    """右键处理器必须认 data-noloc。

    上面那组保证了行上有这个标记,这组保证有人看它 —— 少了任何一半,假位置
    照样会印出来。
    """

    def test_handler_bails_on_noloc(self) -> None:
        src = APP_JS.read_text(encoding="utf-8")
        # 认 td.path 那个,不是树图那个 —— app.js 里有两个 contextmenu 监听器,
        # 按 'contextmenu' 找会先撞上树图画布那个,那条路上没有表格行。
        i = src.find("td.path")
        self.assertNotEqual(i, -1, "app.js 里找不到表格行的右键监听器(td.path)")
        body = src[i:i + 900]
        self.assertIn(
            "noloc", body,
            "右键处理器没检查 data-noloc —— 行上打了标记但没人看,"
            "菜单照样会印出「D:\\那个文件名」这种从来不存在的位置",
        )
        self.assertLess(
            body.find("noloc"), body.find("showCtx"),
            "检查得在 showCtx 之前 —— 菜单弹出来之后再判断就晚了",
        )


if __name__ == "__main__":
    unittest.main()
