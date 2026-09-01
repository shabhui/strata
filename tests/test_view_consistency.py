"""一屏之内的东西必须来自同一次请求。

两个独立的毛病,表现都是「扫描结果时不时就没了」:

1. 表头不跟着走。`treemapTotals`(「本层 163 GB · 28 项」)是在
   renderHotspots() 里写的,而 enterPath() 只调 drawTreemap() —— 进目录之后
   画布换了、表头没换。实测点进一层再返回,画布是空的而表头还写着上一层的
   163 GB,读起来就是「数据还在,只是图没了」。

2. 没有并发护栏。loadDrive() 起六个请求,回来直接往 S 上写。
   pollScan() 扫完会调它,用户这时候点了别的盘/进了别的目录,两次 loadDrive
   的 .then 交错,后回来的写完就被先回来的覆盖 —— 而且 S.drive 已经是新的了,
   于是新盘的界面上挂着旧盘的树。

这里测的是「谁调了谁」和「过期的响应会不会被丢掉」,不是像素。
用 node 跑真的 app.js 片段,DOM 和 api 都是假的。
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

NODE = shutil.which("node")

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

const log = [];
const S = {
  drive: 'C:', drives: [], path: '', tree: null, timeline: null, hotspots: null,
  status: null, scan: null, fade: new Map(), tlView: null, tlAnimated: false,
  loadSeq: 0,
};

// 假的渲染函数:只记谁被叫了,以及叫的时候 S.tree 是什么
function mark(name) {
  return function () {
    log.push({ ev: name, treeTag: S.tree ? S.tree.tag : null, path: S.path });
  };
}
const renderDriveTabs = mark('renderDriveTabs');
const renderBaseline = mark('renderBaseline');
const renderTimeline = mark('renderTimeline');
const renderLegend = mark('renderLegend');
const renderCrumbs = mark('renderCrumbs');
const drawTreemap = mark('drawTreemap');
const renderHotspots = mark('renderHotspots');
const renderTreemapTotals = mark('renderTreemapTotals');
const renderDiff = mark('renderDiff');
const renderChanges = mark('renderChanges');
const setScanState = mark('setScanState');
function banner(spec) { log.push({ ev: 'banner', text: (spec && spec.text) || null }); }

// 假 api:按 script.delays 里给的毫秒数延迟,让响应可以乱序回来
function later(ms, value) {
  return new Promise((res) => setTimeout(() => res(value), ms));
}
/* 延迟可以按 `端点` 或 `端点@盘` 给。必须能按盘分别设 —— 不然先发的请求
 * 总是先回来,竞态根本不会发生,测试就是白过。要制造「旧响应后到」,
 * 得让旧那一批比新那一批更慢。 */
async function api(path, params) {
  const key = path.replace('/api/', '');
  const drive = params && params.drive;
  const p = params && params.path !== undefined && params.path !== null ? params.path : '';
  const d = script.delays || {};
  const delay = d[key + '@' + drive + '|' + p] ?? d[key + '@' + drive] ?? d[key] ?? 0;
  log.push({ ev: 'request', key, drive, path: p, delay });
  return later(delay, {
    tag: drive + '|' + p,
    drive, children: [], days: [], dirs: [], files: [],
  });
}

eval(grab('newLoadToken'));
eval(grab('isCurrentLoad'));
eval(grab('enterPath'));
eval(grab('loadDrive'));

(async () => {
  const plan = script.plan;
  const running = [];
  for (const act of plan) {
    if (act.op === 'enterPath') running.push(enterPath(act.path));
    else if (act.op === 'loadDrive') running.push(loadDrive(act.drive, act.opts || null));
    else if (act.op === 'wait') await later(act.ms);
  }
  await Promise.all(running);
  process.stdout.write(JSON.stringify({ log, finalTree: S.tree && S.tree.tag, drive: S.drive, path: S.path }));
})();
"""


def run(script: dict) -> dict:
    payload = dict(script)
    proc = subprocess.run(
        [NODE, "-e", HARNESS],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "STRATA_APP_JS": str(APP_JS),
             "STRATA_SCRIPT": json.dumps(payload)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "没装 node,跳过视图一致性检查")
class TestHeaderFollowsCanvas(unittest.TestCase):
    """进目录之后,表头和画布必须一起换。"""

    def test_enter_path_repaints_the_totals_header(self):
        got = run({"plan": [{"op": "enterPath", "path": "Users"}]})
        evs = [e["ev"] for e in got["log"]]
        self.assertIn("drawTreemap", evs)
        self.assertIn(
            "renderTreemapTotals", evs,
            "enterPath 没重画那行「本层 x GB · n 项」,"
            f"画布换了表头还是上一层的数字。log={evs}",
        )

    def test_header_sees_the_new_tree_not_the_old_one(self):
        """重画的时候 S.tree 必须已经是新的,不然照样画出旧数字。"""
        got = run({"plan": [{"op": "enterPath", "path": "Users"}]})
        hot = [e for e in got["log"] if e["ev"] == "renderTreemapTotals"]
        self.assertTrue(hot, "根本没重画表头")
        self.assertEqual(
            hot[-1]["treeTag"], "C:|Users",
            f"重画表头时手上的树还是旧的:{hot[-1]['treeTag']}",
        )


@unittest.skipIf(NODE is None, "没装 node,跳过视图一致性检查")
class TestStaleResponsesAreDropped(unittest.TestCase):
    """晚发出、早回来的响应不能覆盖新的。"""

    def test_drive_switch_wins_over_inflight_scan_reload(self):
        """扫完自动刷新(C:)撞上用户切到 D: —— 最后手上必须是 D: 的树。

        这是「结果时不时就没了」最常见的一种:扫描结束那一刻正好在操作。
        C: 那一批故意设得比 D: 慢,让旧响应后到 —— 不这么设,先发的总是先回来,
        竞态不会发生,这条测试就是白过。
        """
        got = run({
            "delays": {"tree@C:": 80, "timeline@C:": 80, "tree@D:": 5,
                       "timeline@D:": 5, "status": 2, "hotspots": 2,
                       "diff": 2, "changes": 2},
            "plan": [
                {"op": "loadDrive", "drive": "C:", "opts": {"keepPath": True}},
                {"op": "wait", "ms": 10},
                {"op": "loadDrive", "drive": "D:"},
            ],
        })
        self.assertEqual(got["drive"], "D:")
        self.assertEqual(
            got["finalTree"], "D:|",
            f"切到 D: 之后手上是 {got['finalTree']} 的树 —— 旧请求把新数据覆盖了",
        )

    def test_enter_path_wins_over_inflight_drive_reload(self):
        """用户进目录时,后台的整盘刷新不能把他拽回根目录。

        整盘刷新那次(path='')设成慢的,进目录那次是快的。
        """
        got = run({
            "delays": {"tree@C:|": 80, "timeline": 80, "status": 2,
                       "hotspots": 2, "diff": 2, "changes": 2,
                       "tree@C:|Users\\alice": 5},
            "plan": [
                {"op": "loadDrive", "drive": "C:", "opts": {"keepPath": True}},
                {"op": "wait", "ms": 5},
                {"op": "enterPath", "path": "Users\\alice"},
            ],
        })
        self.assertEqual(got["path"], "Users\\alice")
        self.assertEqual(
            got["finalTree"], "C:|Users\\alice",
            f"手上是 {got['finalTree']},用户进的目录被整盘刷新覆盖了",
        )

    def test_two_enter_paths_last_one_wins(self):
        """连着点两层,先点的那层慢,不能盖住后点的。"""
        got = run({
            "delays": {"tree@C:|Windows": 60, "tree@C:|Users": 5},
            "plan": [
                {"op": "enterPath", "path": "Windows"},
                {"op": "wait", "ms": 5},
                {"op": "enterPath", "path": "Users"},
            ],
        })
        self.assertEqual(got["finalTree"], "C:|Users",
                         f"手上是 {got['finalTree']},应该是后点的 Users")


if __name__ == "__main__":
    unittest.main()
