"""扫描按钮的样式契约。

起因是个真 bug:app.js 往「扫描本盘」按钮上加的类是 .pulse,而 .pulse 当初是
按「一个 7px 的圆点」写的 —— width/height/border-radius:50%/background。全局
box-sizing:border-box 下,width:7px 会被 padding(15+15)和边框顶到 32px、
height:7px 顶到 16px,内容盒压成 0;50% 圆角碰上非正方形是椭圆;按钮文字
36px 宽从这个 32px 的盒子里溢出来,再叠上 0.8↔1.15 的缩放一直跳。
整个扫描过程里按钮就是一颗抽动的椭圆点。

所以这里盯两件事:

1. 类名是 app.js 和 app.css 之间的口头约定,改一边不改另一边,动画会无声地
   不再生效 —— 没有报错,没有测试红,只是没了。
2. 加在按钮上的类不准碰几何。碰了就是上面那个 bug 重演。

类名从 app.js 里抓出来,不写死:写死的话改名之后这个测试照样绿,而它本来就是
为了在改名时变红才存在的。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "strata" / "web"

# 会把按钮压变形的属性。padding/font-size 之类改了只是难看,不至于把盒子拆掉,
# 所以不管;这几个是真能把按钮变成一个点的。
GEOMETRY = ("width", "height", "border-radius", "transform", "aspect-ratio")


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _scan_button_class() -> str:
    """app.js 给扫描按钮加的那个类。"""
    js = _read("app.js")
    m = re.search(
        r"btn\.classList\.toggle\(\s*['\"]([\w-]+)['\"]\s*,\s*running\s*\)", js
    )
    assert m, "app.js 里找不到 btn.classList.toggle(<类名>, running)"
    return m.group(1)


def _strip_comments(css: str) -> str:
    """去掉 /* */。不去的话注释会被当成选择器的一部分粘上来。"""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _rule_body(css: str, selector: str) -> str | None:
    """取某个选择器的规则体。找不到返回 None。

    只认平铺的规则,@keyframes 那种嵌套的由调用方自己捞。
    """
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _strip_comments(css)):
        # 选择器可能跨行(前面还连着上一条规则的空白),只取最后一段
        heads = [h.strip() for h in m.group(1).replace("\n", ",").split(",")]
        if selector in heads:
            return m.group(2)
    return None


class ScanButtonStyleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.css = _read("app.css")
        self.cls = _scan_button_class()

    def test_css_defines_the_class_js_applies(self) -> None:
        # 两边对不上,扫描时按钮就一点变化都没有
        self.assertRegex(
            self.css,
            r"\.(?:btn\.)?" + re.escape(self.cls) + r"\b",
            f"app.js 加的是 .{self.cls},但 app.css 里没有这个类的规则",
        )

    def test_class_does_not_resize_the_button(self) -> None:
        body = _rule_body(self.css, f".btn.{self.cls}") or _rule_body(
            self.css, f".{self.cls}"
        )
        self.assertIsNotNone(body, f"找不到 .{self.cls} 的规则体")
        for prop in GEOMETRY:
            with self.subTest(prop=prop):
                self.assertNotRegex(
                    body,
                    rf"(?:^|;)\s*{re.escape(prop)}\s*:",
                    f".{self.cls} 设了 {prop} —— 这是加在按钮上的类,"
                    f"改尺寸会把按钮本身压变形(当年 .pulse 就是这样)",
                )

    def test_keyframes_do_not_resize_the_button(self) -> None:
        """关键帧里也不许有 transform/尺寸 —— 那是同一个 bug 换了个地方写。"""
        body = _rule_body(self.css, f".btn.{self.cls}") or _rule_body(
            self.css, f".{self.cls}"
        )
        name = re.search(r"animation:\s*([\w-]+)", body or "")
        if not name:  # 没动画也行,不动就不会变形
            self.skipTest(f".{self.cls} 没有 animation")
        block = re.search(
            r"@keyframes\s+" + re.escape(name.group(1)) + r"\s*\{(.*?\n\})",
            _strip_comments(self.css),
            re.S,
        )
        self.assertIsNotNone(block, f"引用了 {name.group(1)} 但没有对应的 @keyframes")
        for prop in GEOMETRY:
            with self.subTest(prop=prop):
                self.assertNotRegex(
                    block.group(1),
                    rf"(?:^|[;{{])\s*{re.escape(prop)}\s*:",
                    f"@keyframes {name.group(1)} 里设了 {prop},按钮会跟着变形",
                )

    def test_does_not_animate_opacity_on_a_disabled_button(self) -> None:
        """按钮此时是 disabled,.btn:disabled 已经把 opacity 压到 0.4。

        再动一层 opacity,两个乘起来字就看不清了。
        """
        self.assertRegex(
            self.css,
            r"\.btn:disabled\s*\{[^}]*opacity",
            "前提变了:.btn:disabled 不再设 opacity,这条测试要重新想",
        )
        body = _rule_body(self.css, f".btn.{self.cls}") or _rule_body(
            self.css, f".{self.cls}"
        )
        self.assertNotRegex(
            body or "",
            r"(?:^|;)\s*opacity\s*:",
            f".{self.cls} 不该再动 opacity,:disabled 已经压过一次了",
        )

    def test_reduced_motion_still_leaves_a_visible_state(self) -> None:
        """prefers-reduced-motion 会把 animation 整个干掉。

        所以静态那一层必须自己就说明「在忙」,不能只靠关键帧。
        """
        self.assertRegex(
            self.css,
            r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{[^}]*animation:\s*none",
            "前提变了:全局不再关动画,这条测试要重新想",
        )
        body = _rule_body(self.css, f".btn.{self.cls}") or _rule_body(
            self.css, f".{self.cls}"
        )
        static = [
            ln for ln in (body or "").split(";")
            if ln.strip() and not ln.strip().startswith("animation")
        ]
        self.assertTrue(
            static,
            f".{self.cls} 只有 animation:动画一关,扫描时按钮跟平时一模一样",
        )


if __name__ == "__main__":
    unittest.main()
