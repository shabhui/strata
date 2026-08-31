"""时间轴的坐标系:一份几何,窄窗口下字还得能读。

## 字缩到 3 像素

viewBox 死写成 `0 0 1000 260`,配 `preserveAspectRatio: none`,整张图靠 CSS
缩放填满容器。于是缩放系数是 宿主宽/1000,图里所有东西都跟着缩 —— 不只是字:
3px 的窗口指示轨、刻度线、柱子间距,全都按同一个系数变小。

浏览器实测(同一份数据,只改窗口宽):

    窗口 1265px → 宿主 1209px → 系数 1.21 → 日期标签 12 px 高
    窗口  820px → 宿主  749px → 系数 0.75 → 日期标签 8.7 px 高
    窗口  375px → 宿主  319px → 系数 0.32 → 日期标签 3.3 px 高,宽 9.5 px

3.3 像素高的「06-02」是看不清的,而且整张图只剩 83px 高(260 × 0.32)。
app.css 里有 `@media (max-width: 940px)` 把版面收成单列 —— 窄屏是当初特意
支持过的,不是「没人会那么用」。

## 三份几何

修法只能是让 viewBox 跟着实测像素宽走(1 单位 = 1 CSS 像素),这样字按自己的
font-size 渲染。可这么一改,另外两处抄写的常量就从「隐患」变成「当场就错」:

    renderTimeline   W = 1000, M = {left: 64, right: 16, ...}
    dayAt            M_LEFT = 64, M_RIGHT = 16, W = 1000
    拖动平移          r.width * ((1000 - 64 - 16) / 1000) / span   ← 字面量

后两处是「鼠标落在哪一天」和「拖过去多少天」。W 变成活的之后它们还按 1000 算,
点击会落到错的日子上、拖动速度和图对不上。就算不改 viewBox,这三份也是一改
一处另两处静默错位。

所以这里测两件事:几何只有一份(结构上不许再出现抄写的字面量),以及那两个
纯函数在窄宽度下给出的结果是能读的。
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

SRC = APP_JS.read_text(encoding="utf-8")


def fn_body(name: str) -> str:
    """抠出一个顶层函数的源码。和 node 侧 grab() 用的是同一个正则。"""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\([\s\S]*?\n}", SRC)
    if m is None:
        raise AssertionError(f"在 app.js 里找不到 {name}")
    return m.group(0)


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
/* 取一个顶层 const 的值,挂到 globalThis 上。
 *
 * 不能直接 eval 那行原文:eval 出来的 const 是块级的,循环体里定义完就没了,
 * 后面 eval 的函数看不见它(function 声明会提升,const 不会)。
 * 文件是 CRLF,别用 ';\n' 收尾 —— 中间还有个 \r。 */
function loadConst(name) {
  const re = new RegExp('const ' + name + ' = ([\\s\\S]*?);\\r?\\n', 'm');
  const m = src.match(re);
  if (!m) { process.stderr.write('在 app.js 里找不到 const ' + name); process.exit(2); }
  globalThis[name] = eval('(' + m[1] + ')');
}

const I18N = { lang: script.lang || 'zh' };

for (const name of ['TL_M', 'TL_H', 'TL_MIN_W']) loadConst(name);
eval(grab('tlGeom'));
eval(grab('tlLabelStride'));
eval(grab('tlDayAt'));
eval(grab('tlBarCenter'));

const out = {};
for (const w of script.widths) {
  const g = tlGeom(w);
  out[w] = {
    W: g.W, plotW: g.plotW, plotH: g.plotH, H: g.H,
    left: g.M.left, right: g.M.right,
    stride: tlLabelStride(g.plotW, script.days, I18N.lang),
  };

  /* 往返:第 i 天柱子的中心 → 屏幕像素 → 反推下标,必须回到 i。
   *
   * viewBox 单位换回屏幕像素要乘 hostPx/W —— 正常宽度下这是 1,
   * 宿主被夹到 TL_MIN_W 以下时不是。 */
  if (script.roundTrip) {
    const [from, to] = script.roundTrip;
    const n = to - from;
    const bad = [];
    for (let i = 0; i < n; i++) {
      const vx = tlBarCenter(i, g, n);
      const px = vx * (w / g.W);
      const got = tlDayAt(px, w, from, to);
      if (got !== from + i) bad.push({ i, want: from + i, got });
    }
    out[w].roundTripBad = bad;
  }
}
process.stdout.write(JSON.stringify(out));
"""


def geom(widths, days=90, lang="zh", round_trip=None) -> dict:
    proc = subprocess.run(
        [NODE, "-e", HARNESS],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "STRATA_APP_JS": str(APP_JS),
             "STRATA_SCRIPT": json.dumps({"widths": list(widths), "days": days,
                                          "lang": lang, "roundTrip": round_trip})},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来:\n{proc.stderr}")
    return {int(k): v for k, v in json.loads(proc.stdout).items()}


class TestGeometryHasOneDefinition(unittest.TestCase):
    """几何只有一份。这几条不用 node,直接读源码。"""

    def offenders(self, name: str, pattern: str) -> list[str]:
        """函数里命中 pattern 的行。

        报行不报整个函数体:bindTimelineZoom 有一百多行,失败信息里糊一整份
        源码等于没有信息 —— 得让人一眼看到是哪一行又抄了常量。
        注释行不算,说明文字里出现「1000」是在讲历史,不是代码。
        """
        out = []
        for line in fn_body(name).splitlines():
            bare = line.strip()
            if bare.startswith(("*", "/*", "//")):
                continue
            if re.search(pattern, line):
                out.append(bare)
        return out

    def test_pointer_math_does_not_hardcode_the_width(self):
        """dayAt 不许自己写 1000 —— viewBox 宽变成活的之后它会算错日子。"""
        hits = self.offenders("bindTimelineZoom", r"\b1000\b|M_LEFT|M_RIGHT")
        self.assertEqual(
            hits, [],
            "指针换算里还抄着几何常量,viewBox 宽跟着窗口走之后点击会落到错的"
            "日子上。这些行该从 tlGeom() 拿:\n  " + "\n  ".join(hits),
        )

    def test_drag_math_does_not_hardcode_the_margins(self):
        """拖动平移里那串字面量 ((1000 - 64 - 16) / 1000) 得没了。"""
        hits = self.offenders("bindTimelineZoom", r"\b64\b|\b16\b|1000 - 64")
        self.assertEqual(
            hits, [],
            "拖动/点击换算里还有留白的字面量,这是第三份抄写的几何:\n  "
            + "\n  ".join(hits),
        )

    def test_margins_are_defined_once(self):
        """TL_M 是唯一的留白定义,renderTimeline 不许自己再造一个。"""
        self.assertIn("const TL_M", SRC, "没有 TL_M —— 留白还散在各处")
        body = fn_body("renderTimeline")
        self.assertNotRegex(
            body, r"const M = \{",
            "renderTimeline 又自己定义了一遍 M,应该用 tlGeom() 返回的那份。",
        )


@unittest.skipIf(NODE is None, "没装 node,跳过时间轴几何检查")
class TestOneUnitIsOnePixel(unittest.TestCase):
    """viewBox 宽跟着实测像素走,字才能按 font-size 渲染。"""

    def test_viewbox_width_follows_the_host(self):
        """三个实测过的宽度:viewBox 宽必须等于宿主像素宽。"""
        g = geom([1209, 749, 319])
        for w in (1209, 749):
            self.assertEqual(
                g[w]["W"], w,
                f"宿主 {w}px 时 viewBox 宽是 {g[w]['W']} —— "
                f"缩放系数 {g[w]['W'] and w / g[w]['W']:.2f},字会跟着变形",
            )

    def test_height_does_not_shrink_with_width(self):
        """高度是死的 260。原来它跟着宽度缩:319px 宽时整张图只剩 83px 高。"""
        g = geom([1209, 749, 319])
        for w in (1209, 749, 319):
            self.assertEqual(
                g[w]["H"], 260,
                f"宿主 {w}px 时图高 {g[w]['H']} —— 窄窗口下图被压成一条",
            )

    def test_very_narrow_host_is_clamped(self):
        """宿主窄到没法画的时候,viewBox 不跟着塌到 0。

        绘图区宽度必须是正的:负宽度的 rect 在 SVG 里什么都不画,
        整张图会变成空白而不是「小」。
        """
        g = geom([0, 40, 120])
        for w in (0, 40, 120):
            self.assertGreater(
                g[w]["plotW"], 0,
                f"宿主 {w}px 时绘图区宽 {g[w]['plotW']} —— 图会整片空白",
            )

    def test_plot_width_is_host_minus_margins(self):
        """正常宽度下,绘图区 = 宿主宽 - 左右留白。没有隐藏的缩放。"""
        g = geom([1209])
        self.assertEqual(
            g[1209]["plotW"],
            1209 - g[1209]["left"] - g[1209]["right"],
        )


class TestUnknownWidthIsNotNarrowWidth(unittest.TestCase):
    """量不到宽度 ≠ 宽度很窄。这两件事界不一样,混用会画出错的图。

    第一次改完在浏览器里量到的:viewBox 是 `0 0 240 260`,而宿主 CSS 宽 749px。
    240 是 TL_MIN_W —— 也就是渲染那一刻量到 0,退到了「最窄」那一档。
    于是 SVG 被放大 749/240 ≈ 3.1 倍,日期标签渲染成 36px 高、91px 宽。
    方向和原来的 bug 相反,病根是同一个:在错的时机取尺寸,还把「不知道」
    当成了一个具体的数。

    量到 0 的正常原因是页面在后台标签页里加载、布局还没算 —— 这时候正确的
    做法是**先不画**,等 ResizeObserver 拿到真尺寸再来一次(drawTreemap 就是
    这么做的,注释也写了)。把它当成 240 画出去,是拿一个凑合的数当真。
    """

    def test_render_measures_the_shell_not_the_svg(self):
        """量的必须是外壳那个 div,不是 svg 自己。

        svg 的高度由 viewBox 宽高比推出来,而 viewBox 正是这个函数要设的 ——
        量自己就是自己算自己。外壳是普通 div,宽度只由 CSS 决定,不受影响。
        drawTreemap 量的也是 `canvas.parentElement`。
        """
        body = fn_body("renderTimeline")
        self.assertNotRegex(
            body, r"tlGeom\(\s*host\.clientWidth",
            "renderTimeline 量的是 svg 自己(host.clientWidth)。"
            "svg 的尺寸取决于将要设置的 viewBox,得量外壳。",
        )
        self.assertRegex(
            body, r"shell[\s\S]{0,40}clientWidth|clientWidth[\s\S]{0,40}shell",
            "没看到从外壳取宽度",
        )

    def test_unmeasurable_width_skips_the_chart(self):
        """量到 0 就不画图,而不是按最窄档画。

        `if (W < 2) return` 之类的护栏必须在,并且在设 viewBox 之前。
        """
        body = fn_body("renderTimeline")
        idx_guard = body.find("hostW < 2")
        self.assertNotEqual(
            idx_guard, -1,
            "renderTimeline 里没有「量不到就先不画」的护栏 —— "
            "后台标签页里加载会把 viewBox 钉在 TL_MIN_W 上",
        )
        idx_viewbox = body.find("setAttribute('viewBox'")
        self.assertNotEqual(idx_viewbox, -1, "找不到设 viewBox 的地方")
        self.assertLess(
            idx_guard, idx_viewbox,
            "护栏在设 viewBox 之后才判,已经把错的尺寸写进去了",
        )

    @unittest.skipIf(NODE is None, "没装 node")
    def test_clamp_still_applies_to_genuinely_narrow_hosts(self):
        """真的很窄(40px)还是要夹住 —— 护栏别把这一档也顺手去掉了。"""
        g = geom([40])
        self.assertGreater(g[40]["plotW"], 0)
        self.assertEqual(
            g[40]["W"], 240,
            f"40px 宽的宿主算出 viewBox 宽 {g[40]['W']},绘图区会是负的",
        )


class TestResizeSchedulingSurvivesHiddenTabs(unittest.TestCase):
    """页面隐藏时布局回调全停,而两张图都指望「首绘量到 0,回调再叫一次」。

    浏览器里实测了两件事,冻结的层次比一开始以为的高一层:

        document.hidden = true
        requestAnimationFrame(cb)          →  600ms 后没跑;setTimeout(cb, 50) 跑了
        new ResizeObserver(cb).observe(d)  →  一次都没派发
        d.style.width = '400px'            →  还是没派发(afterObserve/afterResize 都是 0)

    第二条才是根子:ResizeObserver 的派发挂在渲染步骤上,页面隐藏时那一整步不跑,
    所以连首次观测都不会来 —— 换成 setTimeout 也救不了,因为回调压根没被调用。
    实测后果:外壳已经量到 1029px,时间轴的 viewBox 还是 null,树图的画布位图停在
    默认的 300 —— 一个像素都没画。

    这带来两条要求,别把它们混成一件事:

    1. 隐藏期间没人重画,所以必须有东西在「页面重新可见」时补一刀。RO 按理说会
       自己补(它比的是「当前尺寸 vs 上次上报的尺寸」,是状态比较不是队列,所以
       隐藏期间的变化不会丢),但这条路在这儿没法验证 —— 没法把预览面板显示出来。
       而这两张图原来根本不依赖它:以前 viewBox 是死的,整张图靠 CSS 缩放,窗口
       一变浏览器自己就重新拉一遍,一行 JS 都不用。是「按像素画」这个改动引入的
       依赖,那就自己挂 visibilitychange,不赌规范。

    2. RO 回调里那层合并不能用 rAF。有个窄但真实的竞态:RO 在可见时派发 → 它把
       当前尺寸记成「已上报」→ 重画排进 rAF → 用户这一刻切走标签页 → rAF 冻结,
       图没画;切回来时尺寸相对「上次上报」没变,RO 不会再派发,于是没人再叫它。
       setTimeout 不冻结,这个洞就没了。
    """

    def _closure(self, fn_name: str) -> str:
        return fn_body(fn_name)

    def test_timeline_observer_does_not_use_raf(self):
        body = self._closure("bindTimelineZoom")
        ro = body[body.find("new ResizeObserver"):]
        self.assertNotEqual(ro, "", "bindTimelineZoom 里没有 ResizeObserver")
        self.assertNotIn(
            "requestAnimationFrame", ro,
            "时间轴的 ResizeObserver 把重画排在 rAF 上 —— "
            "后台标签页里加载的页面,时间轴会一直是空的",
        )
        self.assertIn("setTimeout", ro, "没看到用 setTimeout 合并连续的 resize")

    def test_treemap_observer_does_not_use_raf(self):
        """树图同一个毛病。本机上实测:后台面板里加载,画布位图停在默认的 300,
        CSS 宽 1207 —— 一个像素都没画。"""
        body = self._closure("bindTreemap")
        ro = body[body.find("new ResizeObserver"):]
        self.assertNotEqual(ro, "", "bindTreemap 里没有 ResizeObserver")
        self.assertNotIn(
            "requestAnimationFrame", ro,
            "树图的 ResizeObserver 把重画排在 rAF 上 —— "
            "后台标签页里加载的页面,树图会一直是空的",
        )
        self.assertIn("setTimeout", ro, "没看到用 setTimeout 合并连续的 resize")

    def test_coalescing_is_still_there(self):
        """别为了去掉 rAF 把合并也去掉:拖窗口边缘一秒几十次,
        每次重建 90 根柱子 + 命中区 + 标签会卡。"""
        for fn in ("bindTimelineZoom", "bindTreemap"):
            body = self._closure(fn)
            ro = body[body.find("new ResizeObserver"):]
            self.assertRegex(
                ro, r"if \(pending\w*\) return",
                f"{fn} 的 ResizeObserver 没有合并护栏,连续 resize 会逐次重画",
            )

    def test_page_becoming_visible_repaints_both_charts(self):
        """隐藏期间 RO 一次都不派发,所以可见时必须自己补一刀。

        没有这一刀,后台标签页里加载的页面切过来就是两张空图 —— 而且不是「刷新
        一下就好」:RO 不会再派发(尺寸没变),用户得手动缩一下窗口才出来。
        """
        self.assertIn(
            "visibilitychange", SRC,
            "没有 visibilitychange 监听 —— 页面在后台加载完再切过来,"
            "时间轴和树图都会一直空着",
        )
        m = re.search(
            r"addEventListener\('visibilitychange'[\s\S]{0,600}?\n  \}\);", SRC)
        self.assertIsNotNone(m, "找到了 visibilitychange 这个词,但没找到监听器本体")
        body = m.group(0)
        for call in ("renderTimeline()", "drawTreemap("):
            self.assertIn(
                call, body,
                f"visibilitychange 里没有 {call} —— 两张图都得补,"
                f"它们量到 0 的原因是同一个",
            )
        self.assertIn(
            "document.hidden", body,
            "没判断方向:变成隐藏时也跑一遍是白跑(那会儿量不到尺寸)",
        )


@unittest.skipIf(NODE is None, "没装 node,跳过时间轴几何检查")
class TestLabelsFitTheWidth(unittest.TestCase):
    """标签间隔按放得下几个算,不是死写 12 个。"""

    def test_narrow_host_gets_fewer_labels(self):
        """319px 宽放不下 12 个日期。

        原来 stride 是 `ceil(days/12)`,跟宽度无关 —— 注释里写的理由是
        「viewBox 是死的 1000,窄窗口时字和间距一起缩,相对关系不变」。
        那个理由在 viewBox 跟着宽度走之后就不成立了:字不缩了,间距缩了,
        于是 12 个标签会叠在一起。
        """
        g = geom([1209, 319], days=90)
        self.assertGreater(
            g[319]["stride"], g[1209]["stride"],
            f"窄窗口的间隔({g[319]['stride']})没比宽窗口({g[1209]['stride']})大 —— "
            "标签会互相压住",
        )

    def test_labels_never_overlap(self):
        """按算出来的间隔画,相邻标签的中心距不能小于标签宽。"""
        for days in (7, 30, 90, 365):
            g = geom([1209, 749, 480, 319], days=days)
            for w, info in g.items():
                slot = info["plotW"] / days
                gap = info["stride"] * slot
                self.assertGreaterEqual(
                    gap + 0.01, 38,
                    f"宿主 {w}px、{days} 天:相邻标签中心距 {gap:.1f}px,"
                    f"标签本身要 38px —— 会叠在一起",
                )

    def test_wide_host_does_not_get_a_wall_of_labels(self):
        """宽窗口也不能把 90 个日期全标上,那是另一种看不清。"""
        g = geom([1209], days=90)
        count = len(range(0, 90, g[1209]["stride"]))
        self.assertLessEqual(count, 14, f"宽窗口标了 {count} 个日期,太密")
        self.assertGreaterEqual(count, 6, f"宽窗口只标了 {count} 个日期,太疏")

    def test_english_needs_wider_spacing(self):
        """英文标签更宽("Aug 25" 比 "08-25" 宽),间隔得跟着大。

        这条是防「把宽度算进去之后顺手把语言那一档删掉」。
        """
        zh = geom([480], days=90, lang="zh")[480]["stride"]
        en = geom([480], days=90, lang="en")[480]["stride"]
        self.assertGreaterEqual(
            en, zh,
            f"英文间隔({en})比中文({zh})还小 —— 「Aug 25 Aug 27」会贴在一起",
        )


@unittest.skipIf(NODE is None, "没装 node,跳过时间轴几何检查")
class TestClickLandsOnTheRightDay(unittest.TestCase):
    """画柱子和反推日子必须用同一份几何。

    这是三份抄写真正威胁到的东西。两边不一致的时候不会有任何报错,
    只是「点这根柱子,弹出来的是隔壁那天」—— 而且偏移量随窗口宽度变,
    宽屏下可能刚好不偏,窄屏下偏一两天。
    """

    def test_bar_center_maps_back_to_its_own_day(self):
        """全景(90 天):每根柱子的中心都要能反推回它自己。"""
        g = geom([1209, 749, 480, 319], days=90, round_trip=[0, 90])
        for w, info in g.items():
            self.assertEqual(
                info["roundTripBad"], [],
                f"宿主 {w}px:这些天的柱子中心反推错了 —— "
                f"点上去会选中别的日子:{info['roundTripBad'][:5]}",
            )

    def test_round_trip_holds_when_zoomed(self):
        """缩放后(第 30..44 天这一段)也要成立。

        缩放走的是另一条路:from/to 不再是 0..总数,slot 变大。
        """
        g = geom([1209, 319], days=15, round_trip=[30, 45])
        for w, info in g.items():
            self.assertEqual(
                info["roundTripBad"], [],
                f"宿主 {w}px、缩放到 15 天:反推错了 {info['roundTripBad'][:5]}",
            )

    def test_round_trip_holds_at_the_clamped_width(self):
        """宿主被夹到 TL_MIN_W 以下时,屏幕像素和 viewBox 单位之间有缩放系数。

        这条专门守那个系数:忘了换算的话,窄到 120px 时点击会整体偏。
        """
        g = geom([120, 60], days=30, round_trip=[0, 30])
        for w, info in g.items():
            self.assertEqual(
                info["roundTripBad"], [],
                f"宿主 {w}px(已夹到 {info['W']}):反推错了 {info['roundTripBad'][:5]}",
            )

    def test_single_day_window_does_not_divide_by_zero(self):
        """只剩一天的时候别炸。tlZoom 的下限是 TL_MIN_DAYS,但 clampView
        在总数很小时可能给出 1 —— 边界值走一遍。"""
        g = geom([749], days=1, round_trip=[0, 1])
        self.assertEqual(g[749]["roundTripBad"], [])


if __name__ == "__main__":
    unittest.main()

