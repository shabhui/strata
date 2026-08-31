"""只要后端说在扫,前端就必须有一个定时器在盯着它。

这条不变量原来只在「点了扫描按钮」这一条路上成立,setInterval 写在 startScan
里。于是有三种情况界面会永久停在「扫描中」,扫完了也不刷新:

  1. 刷新页面时正在扫。boot() 只 await pollScan() 一次,看到 running 就返回,
     没人建定时器。
  2. POST /api/scan 撞上 409(计划任务在扫,或者用户开了第二个标签页)。
     请求抛异常落进 catch,也没人建定时器。
  3. 上一次轮询自己失败清掉了定时器,而扫描其实还在跑。

三种表现一样:徽章一直转。用户说的「扫描出来还在显示扫描中」就是这个。

在 node 里跑真的 app.js 片段,setInterval/clearInterval/api 全用假的,
按脚本回放状态序列。不用浏览器 —— 这几个函数只碰 S 和定时器,不碰 DOM
(setScanState 在这里也是假的,它是渲染,单独的事)。
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

# 抠出轮询那几个函数,连同 startScan。抠不到就报错,不能悄悄跳过。
HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.env.STRATA_APP_JS, 'utf8');

function grab(name) {
  const re = new RegExp('(?:async )?function ' + name + '\\([\\s\\S]*?\\n}', 'm');
  const m = src.match(re);
  if (!m) { process.stderr.write('在 app.js 里找不到 ' + name); process.exit(2); }
  return m[0];
}

const SCRIPT = process.env.STRATA_SCRIPT;      // 状态序列,JSON
const script = JSON.parse(SCRIPT);

// ---- 假的运行时 ----
let now = 0;
let timers = new Map();
let nextTimerId = 1;
const log = [];

function setInterval(fn, ms) {
  const id = nextTimerId++;
  timers.set(id, { fn, ms });
  log.push({ ev: 'setInterval', ms });
  return id;
}
function clearInterval(id) {
  if (timers.has(id)) { timers.delete(id); log.push({ ev: 'clearInterval' }); }
}

let step = 0;
let postFails = script.postFails || null;

const S = { drive: 'C:', path: '', scanPoll: null, scan: null };

async function api(path) {
  if (path === '/api/scan/state') {
    const st = script.states[Math.min(step, script.states.length - 1)];
    step += 1;
    log.push({ ev: 'poll', running: !!st.running, phase: st.phase || null });
    if (st.throw) throw new Error(st.throw);
    return st;
  }
  throw new Error('没料到的请求 ' + path);
}

async function post(path, body) {
  log.push({ ev: 'post', path });
  if (postFails) throw new Error(postFails);
  return { ok: true };
}

function setScanState(state) { S.scan = state; log.push({ ev: 'render', running: !!(state && state.running) }); }
async function loadDrive(drive, opts) { log.push({ ev: 'loadDrive', keepPath: !!(opts && opts.keepPath) }); }

// ---- 被测代码 ----
// 上限也从源文件里取,别在测试里写死 —— 写死了就等于两处各有一个数,
// 改了源码这边不会红。
const lim = src.match(/const SCAN_POLL_MAX_ERRORS = (\d+);/);
if (!lim) { process.stderr.write('在 app.js 里找不到 SCAN_POLL_MAX_ERRORS'); process.exit(2); }
const SCAN_POLL_MAX_ERRORS = Number(lim[1]);

eval(grab('ensureScanPolling'));
eval(grab('stopScanPolling'));
eval(grab('pollScan'));
eval(grab('startScan'));

// ---- 回放 ----
async function tick() {
  // 把当前挂着的定时器各跑一次
  for (const { fn } of Array.from(timers.values())) await fn();
}

(async () => {
  if (script.start === 'boot') {
    await pollScan();                      // boot() 就是这么一句
  } else {
    await startScan();
  }
  for (let i = 0; i < (script.ticks || 0); i++) await tick();
  process.stdout.write(JSON.stringify({
    log,
    timersLeft: timers.size,
    scanPoll: S.scanPoll === null ? null : 'set',
  }));
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
        raise AssertionError(f"node 跑不起来轮询代码:\n{proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "没装 node,跳过扫描轮询检查")
class TestPollingInvariant(unittest.TestCase):

    def test_boot_while_scan_running_starts_polling(self):
        """刷新页面时正在扫 —— 这是第 1 种,原来这里不会建定时器。"""
        got = run({
            "start": "boot",
            "states": [{"running": True, "phase": "正在扫描"}],
            "ticks": 0,
        })
        self.assertEqual(got["scanPoll"], "set",
                         f"boot 时正在扫,却没建定时器。log={got['log']}")
        self.assertEqual(got["timersLeft"], 1)

    def test_boot_while_idle_does_not_start_polling(self):
        """没在扫就别留一个每 1.2 秒打一次后端的定时器。"""
        got = run({
            "start": "boot",
            "states": [{"running": False, "finished_at": 1}],
            "ticks": 0,
        })
        self.assertIsNone(got["scanPoll"])
        self.assertEqual(got["timersLeft"], 0)

    def test_post_conflict_still_starts_polling(self):
        """第 2 种:409。别人在扫,我们也得跟着刷。"""
        got = run({
            "start": "click",
            "postFails": "已经有一次扫描在进行中,请等它结束。",
            "states": [{"running": True, "phase": "正在扫描"}],
            "ticks": 0,
        })
        self.assertEqual(got["scanPoll"], "set",
                         f"POST 撞 409 之后没建定时器。log={got['log']}")

    def test_polling_stops_and_reloads_when_scan_finishes(self):
        """扫完:清掉定时器,并且重新取数据(不然表上还是旧快照)。"""
        got = run({
            "start": "click",
            "states": [
                {"running": True, "phase": "正在扫描"},
                {"running": True, "phase": "正在扫描"},
                {"running": False, "finished_at": 1, "result": {"method": "scandir"}},
            ],
            "ticks": 5,
        })
        evs = [e["ev"] for e in got["log"]]
        self.assertIn("loadDrive", evs, f"扫完没有重新取数据。log={got['log']}")
        self.assertEqual(got["timersLeft"], 0, "扫完了定时器还挂着")
        self.assertIsNone(got["scanPoll"])
        # keepPath:扫完不该把用户踢回根目录
        reload_ev = next(e for e in got["log"] if e["ev"] == "loadDrive")
        self.assertTrue(reload_ev["keepPath"], "扫完把用户踢回了根目录")

    def test_only_one_timer_ever(self):
        """连点扫描按钮、或者轮询里再看到 running,都不能叠出第二个定时器。"""
        got = run({
            "start": "click",
            "states": [{"running": True}, {"running": True}, {"running": True}],
            "ticks": 3,
        })
        starts = sum(1 for e in got["log"] if e["ev"] == "setInterval")
        self.assertEqual(starts, 1, f"建了 {starts} 个定时器。log={got['log']}")
        self.assertEqual(got["timersLeft"], 1)

    def test_transient_state_error_does_not_abandon_a_running_scan(self):
        """第 3 种:一次取状态失败,不该就此不管了。

        扫描在后台照样跑着,放弃轮询等于把界面永久钉在最后看到的样子。
        """
        got = run({
            "start": "click",
            "states": [
                {"running": True},
                {"throw": "NetworkError"},
                {"running": True},
                {"running": False, "finished_at": 1},
            ],
            "ticks": 6,
        })
        evs = [e["ev"] for e in got["log"]]
        self.assertIn("loadDrive", evs,
                      f"中间失败一次之后再没恢复,扫完也没刷新。log={got['log']}")

    def test_gives_up_after_repeated_failures(self):
        """一直失败(服务真的没了)就得停,不能无限打一个死掉的后端。"""
        got = run({
            "start": "click",
            "states": [{"throw": "ECONNREFUSED"}],
            "ticks": 30,
        })
        self.assertEqual(got["timersLeft"], 0,
                         "后端一直连不上,定时器还在打")
        polls = sum(1 for e in got["log"] if e["ev"] == "poll")
        self.assertLess(polls, 15, f"放弃前打了 {polls} 次,太多")

    def test_boot_while_idle_does_not_reload(self):
        """开页面没在扫的时候,pollScan 不该再取一遍数据。

        boot() 的顺序是:loadDrive() → loadSchedule() → pollScan()。
        最后那句是为了接住别处已经在跑的扫描(计划任务、命令行、另一个标签页)。
        可它在「没在扫」这条路上无条件又调了一次 loadDrive —— 于是每次开页面
        整份数据都抓两遍。浏览器网络面板上量到的:

            /api/status    ×3
            /api/timeline  ×2      /api/tree     ×2
            /api/hotspots  ×2      /api/diff     ×2
            /api/changes   ×2

        C: 那几个接口要查 16 万行 dirs,白跑一遍。而且两轮之间界面会闪一下。

        判据用现成的 S.scanPoll:boot 调进来时它是 null(还没建定时器),
        定时器那一路和 startScan 那一路都非 null。「刚才在轮询」正好等于
        「有一次扫描刚结束」,那才是该重新取数的时刻。
        """
        got = run({
            "start": "boot",
            "states": [{"running": False, "finished_at": 1}],
            "ticks": 0,
        })
        loads = [e for e in got["log"] if e["ev"] == "loadDrive"]
        self.assertEqual(
            loads, [],
            "boot 时没在扫,pollScan 却又取了一遍数据 —— boot 上一行刚取过。"
            f" log={got['log']}",
        )

    def test_finished_scan_still_reloads(self):
        """但扫描真的结束时必须刷 —— 这是上一条的另一半。

        少了这一条,把重载整个删掉也能让上面那条绿,而那样扫完表上还是旧快照,
        正是用户最早报的「扫描结果时不时就没了」那一类毛病。
        """
        got = run({
            "start": "click",
            "states": [
                {"running": True, "phase": "正在扫描"},
                {"running": False, "finished_at": 1},
            ],
            "ticks": 4,
        })
        loads = [e for e in got["log"] if e["ev"] == "loadDrive"]
        self.assertEqual(len(loads), 1,
                         f"扫完应该刷且只刷一次,实际 {len(loads)} 次。log={got['log']}")
        self.assertTrue(loads[0]["keepPath"], "扫完把用户踢回了根目录")

    def test_reload_happens_once_not_per_idle_poll(self):
        """扫完之后定时器停了,不会每轮再刷一次。

        这条防的是「用 S.scan 之类的状态当判据」那种改法:那样只要状态是
        idle 就刷,而定时器还在的那一轮会重复刷。
        """
        got = run({
            "start": "click",
            "states": [
                {"running": True},
                {"running": False, "finished_at": 1},
                {"running": False, "finished_at": 1},
                {"running": False, "finished_at": 1},
            ],
            "ticks": 8,
        })
        loads = [e for e in got["log"] if e["ev"] == "loadDrive"]
        self.assertEqual(len(loads), 1,
                         f"刷了 {len(loads)} 次,应该只有 1 次。log={got['log']}")


if __name__ == "__main__":
    unittest.main()
