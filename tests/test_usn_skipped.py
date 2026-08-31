"""不提权时,「消失了什么」不能悄悄消失。

实测到的毛病:库里 usn_events 和 usn_cursor 都是 0 行,而同一台机器上
直接读 USN 日志能读到 20 万条事件、其中 84,303 条该入库(tools/probe_usn.py
量的)。整条 USN 管道本身是通的 —— 断点在门口:

  _run_scan 里 `if with_usn and privileges.is_admin():`(app.py:96)
  不是管理员就整段跳过,不写 state、不写 payload、不写日志。

然后前端 renderChanges 看到 coverage.events == 0,把整个面板 hidden 掉,
注释写着「别摆一张空表让人以为什么都没删过」—— 意图是对的,可结果是
用户既不知道这个功能存在,也不知道为什么没有。三种情况长得一模一样:
没提权、提权了但日志是空的、还没扫过。

这里测两件事:
  1. 后端跳过时要在 payload 里留下原因(不是静默 return)
  2. 前端拿到「不是管理员 + 0 条事件」时要说出来,不能 hidden
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strata import privileges
from strata.scan import snapshot as snapshot_mod
from strata.server import app as server_app
from strata.store import db

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "src" / "strata" / "web" / "app.js"
NODE = shutil.which("node")


class UsnSkipLeavesATraceTest(unittest.TestCase):
    """不是管理员就跳过 USN,但要把「为什么」放进结果里。"""

    def setUp(self) -> None:
        # _run_scan 会把异常连 traceback 写进 config.log_path(),也就是用户真实的
        # %LOCALAPPDATA%\Strata\strata.log。把 LOCALAPPDATA 指到临时目录,
        # 免得测试噪音污染真日志 —— 上次排查就是被这种噪音误导过。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *, is_admin: bool) -> dict:
        class FakeResult:
            snapshot_id = 1
            method = "scandir"
            scanned_bytes = 0
            file_count = 0
            dir_count = 0
            duration_ms = 0
            fallback_reason = None
            # 这个字段是后加的,而 app.py 那边现在直接取属性、不再用
            # getattr(..., None) 兜底 —— 于是这个假货一缺字段就当场 AttributeError,
            # 这条测试立刻红。这就是去掉兜底想要的效果:真正的 ScanResult
            # 哪天丢了这个字段也会同样地炸,而不是悄悄把每条事件的路径存成 NULL
            # (那个 bug 藏了很久,就因为兜底值让它看起来在工作)。
            dir_paths: dict[int, str] = {}

        with mock.patch.object(snapshot_mod, "scan_drive",
                               lambda conn, drive, **kw: FakeResult()), \
             mock.patch.object(db, "connect", lambda *a, **k: mock.MagicMock()), \
             mock.patch.object(privileges, "is_admin", lambda: is_admin), \
             mock.patch("strata.scan.changes.collect_usn") as collect, \
             mock.patch("strata.scan.changes.enrich_deleted_sizes", lambda *a, **k: 0):
            collect.return_value = mock.MagicMock(
                as_dict=lambda: {"events_stored": 7, "available": True})
            server_app._scan_lock.acquire()
            server_app._run_scan("C:")
        return server_app.scan_state()["result"] or {}

    def test_admin_run_reports_usn(self):
        """先钉住正常那一半:是管理员时 payload 里有 usn。"""
        payload = self._run(is_admin=True)
        self.assertIn("usn", payload)
        self.assertEqual(payload["usn"]["events_stored"], 7)

    def test_non_admin_says_why_it_skipped(self):
        """不是管理员时,payload 里也必须有 usn,并且说明是权限问题。

        原来这里什么都不写 —— 前端只能看到「没有 usn 这个键」,
        和「读了但一条都没有」分不开。
        """
        payload = self._run(is_admin=False)
        self.assertIn("usn", payload,
                      "不提权时 payload 里连 usn 键都没有,前端无法区分"
                      "「没权限」和「日志是空的」")
        usn = payload["usn"]
        self.assertFalse(usn.get("available"),
                         "跳过了却报 available=True")
        self.assertTrue(usn.get("reason"),
                        "跳过了但没给原因,界面没话可说")
        self.assertIn("管理员", usn["reason"],
                      f"原因里没提管理员权限:{usn.get('reason')!r}")


HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');
const script = JSON.parse(process.env.STRATA_SCRIPT);

function grab(name) {
  const re = new RegExp('(?:async )?function ' + name + '\\([\\s\\S]*?\\n}', 'm');
  const m = src.match(re);
  if (!m) { process.stderr.write('在 app.js 里找不到 ' + name); process.exit(2); }
  return m[0];
}

// 极简假 DOM:只关心 hidden 和落进去的文字
const nodes = {};
function mkNode(id) {
  return {
    id, hidden: false, childNodes: [], textContent: '',
    appendChild(c) { this.childNodes.push(c); this.textContent += (c.textContent || ''); return c; },
    setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} },
  };
}
for (const id of ['deletedSection', 'usnCoverage', 'usnNotice', 'deletedBody',
                  'diffSection', 'diffRange', 'diffNet', 'diffNotice',
                  'grewBody', 'shrankBody']) {
  nodes[id] = mkNode(id);
}
function el(id) { return nodes[id] || null; }
function clear(n) { if (n) { n.childNodes = []; n.textContent = ''; } }
function tag(name, attrs, text) {
  const n = mkNode(null);
  n.textContent = text == null ? '' : String(text);
  return n;
}
const document = { createTextNode: (s) => ({ textContent: String(s) }) };
function fillRows(id, rows, empty) { nodes.deletedBody.textContent = rows.length ? 'rows' : String(empty); }
function fmtCount(n) { return String(n); }
function fmtBytes(n) { return String(n); }
function fmtTime(n) { return String(n); }
function pathCell(a) { return { text: String(a) }; }
// t() 回显 key,断言就查 key,不依赖译文
function t(key, vars) { return key + (vars ? '(' + JSON.stringify(vars) + ')' : ''); }

const S = { status: script.status || {}, changes: null };

// renderDiff 里用到的两个小工具
function fmtSigned(n) { return String(n); }

eval(grab('renderChanges'));
renderChanges(script.changes);

if (script.diff !== undefined) {
  eval(grab('renderDiff'));
  renderDiff(script.diff);
}

process.stdout.write(JSON.stringify({
  hidden: nodes.deletedSection.hidden,
  coverage: nodes.usnCoverage.textContent,
  notice: nodes.usnNotice.textContent,
  body: nodes.deletedBody.textContent,
  diff_hidden: nodes.diffSection.hidden,
  diff_notice: nodes.diffNotice.textContent,
}));
"""


def run_js(script: dict) -> dict:
    proc = subprocess.run(
        [NODE, "-e", HARNESS],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "STRATA_APP_JS": str(APP_JS),
             "STRATA_SCRIPT": json.dumps(script)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "没装 node,跳过前端检查")
class DeletedPanelExplainsItselfTest(unittest.TestCase):
    """0 条事件有三种原因,面板不能一律隐藏。"""

    def test_hidden_when_admin_and_genuinely_empty(self):
        """提权了、日志真没东西:藏起来是对的,保持原行为。"""
        got = run_js({
            "status": {"privileges": {"is_admin": True}},
            "changes": {"coverage": {"events": 0}, "events": []},
        })
        self.assertTrue(got["hidden"])

    def test_visible_when_not_admin(self):
        """没提权:必须露出来说清楚,否则用户不知道有这个功能。"""
        got = run_js({
            "status": {"privileges": {"is_admin": False}},
            "changes": {"coverage": {"events": 0}, "events": []},
        })
        self.assertFalse(got["hidden"],
                         "不是管理员却把整个面板藏了 —— 用户永远发现不了"
                         "「消失了什么」需要提权")
        text = got["coverage"] + got["notice"] + got["body"]
        self.assertIn("del.needAdmin", text,
                      f"没说要提权,面板上写的是:{text!r}")

    def test_diff_panel_says_it_needs_two_scans(self):
        """只扫过一次时,「最近两次扫描之间」也不能默默消失。

        后端在 api.py 里给了原因(「至少要两次扫描才能对比」),前端 renderDiff
        直接 hidden 掉整段,那句话永远不可能被看到 —— 写了却到不了用户眼前的
        文案和没写一样。而对第一次用的人来说这正是该说的:再扫一次就有了。
        """
        got = run_js({
            "status": {"privileges": {"is_admin": True}},
            "changes": {"coverage": {"events": 0}, "events": []},
            "diff": {"available": False,
                     "reason": "至少要两次扫描才能对比。第一次扫描后请等下一次快照。"},
        })
        self.assertFalse(got["diff_hidden"],
                         "只有一次快照就把整段藏了,那句原因永远到不了界面")
        # 断言查的是键,不是译文 —— 文案归 i18n.js,这里只管接线通不通
        self.assertIn("diff.needTwo", got["diff_notice"])

    def test_shows_events_when_present(self):
        """有数据时照旧显示,别把正常路径改坏。"""
        got = run_js({
            "status": {"privileges": {"is_admin": True}},
            "changes": {
                "coverage": {"events": 3, "first_day": "2026-08-01", "days": 30},
                "events": [{"kind": "delete", "path": "a\\b.txt", "bytes": 10, "at": 0}],
            },
        })
        self.assertFalse(got["hidden"])
        self.assertEqual(got["body"], "rows")


if __name__ == "__main__":
    unittest.main()
