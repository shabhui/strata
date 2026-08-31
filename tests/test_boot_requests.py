"""开一次页面,每个接口只该请求一次。

boot() 里有一句 `S.status = await api('/api/status')`,它必须先拿到:挑哪个盘
(找 snapshot_count > 0 的那个)、要不要弹「当前不是管理员」,都得靠它。
紧接着 boot 调 loadDrive(),而 loadDrive 的并发请求里又有一个 /api/status。
于是开一次页面,这个接口请求两次。

不是「多发一个包」那么轻。get_status 对每个盘都要:latest_snapshot 一次查询、
volume_space 一次 Win32 取容量(盘不在的时候还得等它失败)、
list_snapshots(limit=10000) 整个拉出来数个数、usn_coverage 一次聚合;
外加一次 db_size_bytes。乘以盘数,整套白跑一遍。

修法是让调用方说一声「我手上这份是刚取的」,而不是把 loadDrive 里那一路删掉 ——
换盘和扫完刷新都必须重取:snapshot_count 变了,基线那行不跟着走就一直写着
「尚无快照」。所以这里两头都测:开页面不许重复取,那两条路必须取。

用 node 跑真的 app.js 片段(boot 和 loadDrive 都是原文),DOM、I18N、api 全是假的。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from collections import Counter
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
  drive: null, drives: [], path: '', tree: null, timeline: null, hotspots: null,
  status: null, scan: null, schedule: null, diff: null, changes: null,
  fade: new Map(), tlView: null, tlAnimated: false, loadSeq: 0, scanPoll: null,
};

/* 假 status:C: 没快照,D: 有三个。boot 要能挑中 D: —— 顺手证明它挑盘用的
 * 确实是自己那次请求的结果,而不是 loadDrive 那一路顺带填上的。 */
const STATUS = {
  drives: [
    { drive: 'C:', snapshot_count: 0, present: true },
    { drive: 'D:', snapshot_count: 3, present: true },
  ],
  privileges: { is_admin: true },
  db_path: 'X:\\strata.db',
  db_bytes: 1,
  server_time: 0,
};

async function api(path, params) {
  const key = path.replace('/api/', '');
  log.push({ ev: 'request', key, drive: (params && params.drive) || null });
  if (key === 'status') return JSON.parse(JSON.stringify(STATUS));
  return { drive: params && params.drive, children: [], days: [], dirs: [], files: [] };
}

// 渲染和绑定全是空的:这里数的是请求,不是画了什么
const NOOP = ['renderDriveTabs', 'renderBaseline', 'renderTimeline', 'renderLegend',
  'renderCrumbs', 'drawTreemap', 'renderHotspots', 'renderTreemapTotals',
  'renderDiff', 'renderChanges', 'setScanState', 'bindTreemap', 'bindCtxMenu',
  'bindTimelineZoom', 'bindVisibilityRepaint', 'paintSettingsInfo', 'paintSchedule',
  'repaintAll', 'startScan', 'toggleSchedule'];
for (const name of NOOP) globalThis[name] = function () { log.push({ ev: name }); };

function banner(spec, vars) {
  log.push({ ev: 'banner', key: typeof spec === 'string' ? spec : null });
}
function el(id) {
  return { hidden: false, addEventListener() {}, textContent: '', classList: { add() {}, remove() {}, toggle() {} } };
}
const I18N = { lang: 'zh', apply() {}, onChange() {}, set() {} };
const document = { addEventListener() {} };
async function loadSchedule() { log.push({ ev: 'loadSchedule' }); }
async function pollScan() { log.push({ ev: 'pollScan' }); return false; }

eval(grab('newLoadToken'));
eval(grab('isCurrentLoad'));
eval(grab('loadDrive'));
eval(grab('boot'));

(async () => {
  if (script.op === 'boot') await boot();
  else await loadDrive(script.drive, script.opts || null);
  process.stdout.write(JSON.stringify({ log, drive: S.drive, drives: S.drives,
                                        status: !!S.status }));
})();
"""


def run(script: dict) -> dict:
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


def counts(got: dict) -> Counter:
    return Counter(e["key"] for e in got["log"] if e["ev"] == "request")


@unittest.skipIf(NODE is None, "没装 node,跳过开场请求检查")
class TestBootAsksOnce(unittest.TestCase):
    """开一次页面,每个接口一次。"""

    def test_status_is_not_fetched_twice(self):
        """boot 自己取过 status 之后,loadDrive 不该再取一遍。

        实测浏览器网络面板上是 /api/status ×2(修掉 pollScan 那个重载之前是 ×3)。
        get_status 不便宜:每个盘 latest_snapshot + volume_space(Win32)+
        list_snapshots(limit=10000) 数个数 + usn_coverage,再加一次 db_size_bytes。
        """
        got = run({"op": "boot"})
        c = counts(got)
        self.assertEqual(
            c["status"], 1,
            f"/api/status 请求了 {c['status']} 次。boot 和 loadDrive 各取了一遍,"
            f"整套按盘数的查询白跑。log={[e for e in got['log'] if e['ev'] == 'request']}",
        )

    def test_every_endpoint_exactly_once(self):
        """顺带把其余五个也钉住,别让下次谁再加一路重复的。"""
        c = counts(run({"op": "boot"}))
        for key in ("status", "timeline", "tree", "hotspots", "diff", "changes"):
            self.assertEqual(c[key], 1, f"/api/{key} 请求了 {c[key]} 次,应该是 1 次")

    def test_boot_still_has_the_drive_list(self):
        """少取一次不能把盘列表弄丢 —— 基线那行和盘标签全靠它。"""
        got = run({"op": "boot"})
        self.assertTrue(got["status"], "S.status 是空的")
        self.assertEqual([d["drive"] for d in got["drives"]], ["C:", "D:"])
        self.assertEqual(
            got["drive"], "D:",
            "boot 应该挑有快照的那个盘(D:),挑成了 " + str(got["drive"]),
        )


@unittest.skipIf(NODE is None, "没装 node,跳过开场请求检查")
class TestRefreshStillFetches(unittest.TestCase):
    """另外两条路必须重取 status,不然基线那行会一直停在旧数字上。

    这两条是防「把 loadDrive 里那一路直接删掉」的 —— 删掉之后上面三条一样绿。
    """

    def test_drive_switch_refetches(self):
        """换盘:selectDrive 不带 opts 进来,得重取。"""
        c = counts(run({"op": "loadDrive", "drive": "D:"}))
        self.assertEqual(c["status"], 1, "换盘没重取 status,盘标签上的数字不会动")

    def test_post_scan_reload_refetches(self):
        """扫完刷新:pollScan 带 {keepPath:true} 进来,更得重取。

        snapshot_count 刚变了。这条也顺手挡住一种偷懒的修法:「有 opts 就跳过
        status」—— 那样扫完基线那行会一直写着「尚无快照」,而下面的时间轴已经是
        新数据了,同一屏上两半自相矛盾。
        """
        c = counts(run({"op": "loadDrive", "drive": "C:", "opts": {"keepPath": True}}))
        self.assertEqual(
            c["status"], 1,
            "扫完刷新没重取 status —— 基线那行会停在扫描前的数字上",
        )


if __name__ == "__main__":
    unittest.main()
