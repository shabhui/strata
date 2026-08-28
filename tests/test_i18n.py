"""界面文案的完整性。

这个文件不测行为,测的是「有没有话可说」。三件事:

1. 每个键两种语言都不空。i18n.js 把两种语言并排放在一个键里就是为了让缺失
   看得见,但看得见不等于有人会看 —— 加一条中文忘了加英文,英文界面上那处
   直接漏出中文,而代码能跑、测试全绿。

2. 后端给的每个代号都有对应的文案。hotspots.py 的 CLEANUP_RULES 出的是代号,
   措辞在 i18n.js;加规则时漏了文案,那一行在界面上只会显示一个 camelCase
   的代号,像个 bug。diff.py 的口径说明同理。

3. 反过来也查:i18n.js 里有文案、后端却没有这个代号,说明规则删了文案没删,
   或者代号拼错了。拼错的那半边永远取不到,和第 2 条是同一个故障的两面。

为什么用 node:i18n.js 是给浏览器用的,拿 Python 解析 JS 字面量只会解出一个
似是而非的结果(模板串、函数值、字符串拼接都得自己实现)。直接让 node 加载它,
问的就是浏览器会看到的那份数据。没有 node 就跳过 —— 这是零依赖项目,不能因为
缺一个开发工具就让测试红。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from strata.analysis.hotspots import CLEANUP_RULES

ROOT = Path(__file__).resolve().parents[1]
I18N_JS = ROOT / "src" / "strata" / "web" / "i18n.js"
APP_JS = ROOT / "src" / "strata" / "web" / "app.js"
DIFF_PY = ROOT / "src" / "strata" / "analysis" / "diff.py"

NODE = shutil.which("node")

# 在 node 里加载 i18n.js,把每个键在两种语言下的取值倒出来。
#
# i18n.js 结尾要挂 window.I18N,所以先造一个最小的 window/navigator/document。
# 造得刚好够它跑起来:localStorage 抛异常(它自己有 try),navigator.language
# 给 en-US(让 detect() 有确定结果),document 只需要 querySelectorAll 返回空。
PROBE = r"""
const fs = require('fs');
// 走环境变量,不走 argv:node -e 下 argv 的位置和普通脚本不一样,
// 而且 Windows 上的路径带反斜杠,拼进命令行还要再操心一层转义。
const path = process.env.STRATA_I18N_JS;

const store = {
  getItem() { throw new Error('no localStorage'); },
  setItem() { throw new Error('no localStorage'); },
};
const noNodes = { forEach() {} };
const doc = {
  querySelectorAll() { return noNodes; },
  documentElement: {},
  get title() { return this._t; },
  set title(v) { this._t = v; },
};
const win = { localStorage: store, console: { warn() {} } };
global.window = win;
global.navigator = { language: 'en-US' };
global.document = doc;

new Function('window', 'navigator', 'document',
             fs.readFileSync(path, 'utf8'))(win, navigator, doc);

const I18N = win.I18N;
const out = {};

/* 原始表:直接看 raw(key) 的两半,不经过 t()。
 *
 * t() 在英文缺失时会退回中文(界面上不留空是对的),所以「英文那半是空的」
 * 经过 t() 之后看到的是一句中文 —— 空值检查在 t() 的输出上永远查不到英文
 * 缺失。要查这件事只能看原始表。 */
out.raw = {};
for (const key of I18N.keys()) {
  const pair = I18N.raw(key);
  out.raw[key] = [0, 1].map((i) => {
    const v = pair[i];
    if (v === undefined || v === null) return { kind: 'missing' };
    if (typeof v === 'function') return { kind: 'fn' };
    return { kind: 'str', text: String(v) };
  });
}
const NUMERIC = ['n', 'days', 'depth', 'count'];

/* 数字字段一律给 num,其余给占位串。
 *
 * 参数用 Proxy,不用一份写死的字段表 —— 写死的话,漏一个字段名就会拼出
 * "undefined",看起来像文案的毛病,其实是测试自己没给够。Proxy 让文案自己
 * 说它要什么,顺便把它问过的字段名记下来。 */
function makeProbe(num, asked) {
  return new Proxy({}, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive || prop === 'toString') return undefined;
      asked.push(String(prop));
      return NUMERIC.includes(String(prop)) ? num : '‹' + String(prop) + '›';
    },
    has() { return true; },
  });
}

function sweep(num) {
  const seen = {};
  const needs = {};
  for (const key of I18N.keys()) {
    const asked = [];
    try {
      seen[key] = String(I18N.t(key, makeProbe(num, asked)));
    } catch (err) {
      seen[key] = '__THREW__ ' + err.message;
    }
    needs[key] = asked;
  }
  return { seen, needs };
}

for (const lang of ['zh', 'en']) {
  I18N.set(lang);
  const many = sweep(2);
  out[lang + ':needs'] = many.needs;
  out[lang] = many.seen;
  // 数量为 1 的那一遍:英文单复数只在 n===1 时才走另一支,给 2 永远测不到。
  out[lang + ':one'] = sweep(1).seen;
}
process.stdout.write(JSON.stringify(out));
"""


def load_strings() -> dict[str, dict[str, str]]:
    proc = subprocess.run(
        [NODE, "-e", PROBE],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ, "STRATA_I18N_JS": str(I18N_JS)},
    )
    if proc.returncode != 0:
        raise AssertionError(f"node 跑不起来 i18n.js:\n{proc.stderr}")
    return json.loads(proc.stdout)


@unittest.skipIf(NODE is None, "没装 node,跳过前端文案检查")
class StringTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.strings = load_strings()

    def test_both_languages_present(self) -> None:
        """每个键两边都得有话。

        看的是原始表,不是 t() 的输出。t() 在英文缺失时会退回中文 —— 那对界面
        是对的(不留空),但对这条检查是致命的:经过 t() 之后,英文缺失和英文
        正常长得一模一样,这条检查就只剩下在验中文那半边。
        """
        names = {0: "中文", 1: "英文"}
        missing = []
        for key, pair in self.strings["raw"].items():
            for i, half in enumerate(pair):
                if half["kind"] == "missing":
                    missing.append(f"{key} 少了{names[i]}")
                elif half["kind"] == "str" and not half["text"].strip():
                    missing.append(f"{key} 的{names[i]}是空的")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_nothing_threw(self) -> None:
        """带参数的文案要能求值。函数里写错字段名,界面上会印出 undefined。"""
        threw = [
            f"{lang} {key}: {value}"
            for lang in ("zh", "en")
            for key, value in self.strings[lang].items()
            if value.startswith("__THREW__")
        ]
        self.assertEqual(threw, [], "\n".join(threw))

    def test_no_undefined_leaked_into_text(self) -> None:
        """文案里不该出现 undefined。

        模板串里写错字段名不会抛,会安静地拼进去一个 "undefined" —— 页面上
        就是「有 undefined 个快照已降级」。上面那条测不出这个。
        """
        bad = [
            f"{lang} {key}: {value}"
            for lang in ("zh", "en")
            for key, value in self.strings[lang].items()
            if "undefined" in value or "NaN" in value
        ]
        self.assertEqual(bad, [], "\n".join(bad))

    def test_both_languages_ask_for_the_same_fields(self) -> None:
        """同一个键的两种语言要用同一组参数。

        中文写 v.n、英文手滑写成 v.count,英文那句就印出 "undefined" —— 而
        中文一切正常,所以在中文界面上测不出来。上面那条 undefined 检查也测不出:
        它用的 Proxy 对任何字段都给值,两边各要什么都能拼出话来。能发现的只有
        「两边要的不一样」这件事本身。
        """
        zh_needs = self.strings["zh:needs"]
        en_needs = self.strings["en:needs"]
        mismatched = []
        for key in zh_needs:
            a, b = set(zh_needs[key]), set(en_needs.get(key, []))
            if a != b:
                mismatched.append(f"{key}: 中文要 {sorted(a)},英文要 {sorted(b)}")
        self.assertEqual(mismatched, [], "\n".join(mismatched))

    def test_english_singular_when_count_is_one(self) -> None:
        """数量是 1 的时候英文不能用复数。

        原来的检查只给 n=2,而单复数分支只有 n===1 才走另一边 —— 于是
        「1 days measured」「1 files」这种一路绿灯,是切到英文界面上一眼看见的。
        所以这里专门再跑一遍 n=1。

        顺带能抓住类型写错:grid.fileCount 原来判的是 v.n === '1'(跟字符串比),
        传进来的是数字,永远不相等,1 个文件也印成「1 files」。
        """
        # 「1 后面紧跟一个 s 结尾的词」。ss 结尾的词(less、progress)不算,
        # 那不是复数。
        bad_plural = re.compile(r"\b1 ([a-z]+[^s\W])s\b")
        leaked = []
        for key, value in self.strings["en:one"].items():
            if value.startswith("__THREW__"):
                continue          # test_nothing_threw 管这个
            hit = bad_plural.search(value)
            if hit:
                leaked.append(f"{key}: 「{hit.group(0)}」 应该是单数 —— {value}")
        self.assertEqual(leaked, [], "\n".join(leaked))

    def test_nothing_threw_at_one(self) -> None:
        """n=1 那一遍也要能求值,别只在 n=2 时不炸。"""
        threw = [
            f"{lang} {key}: {value}"
            for lang in ("zh", "en")
            for key, value in self.strings[lang + ":one"].items()
            if value.startswith("__THREW__") or "undefined" in value
        ]
        self.assertEqual(threw, [], "\n".join(threw))

    def test_english_has_no_chinese(self) -> None:
        """英文那半边不该有汉字。

        i18n.js 的 t() 在英文缺失时会退回中文(不留空是对的),于是「忘了翻」
        在界面上表现为一句中文,而不是一处空白。上面那条空值检查抓不到它。
        """
        han = re.compile(r"[\u4e00-\u9fff]")
        leaked = [
            f"{key}: {value}"
            for key, value in self.strings["en"].items()
            # \u8bed\u8a00\u6309\u94ae\u4e0a\u5199\u7684\u662f\u300c\u53e6\u4e00\u79cd\u8bed\u8a00\u300d,\u82f1\u6587\u754c\u9762\u4e0a\u5c31\u8be5\u662f\u300c\u4e2d\u6587\u300d\u3002
            # \u53ea\u653e\u8fc7\u8fd9\u4e24\u6761,\u4e0d\u653e\u8fc7\u6574\u4e2a nav. \u2014\u2014 \u767d\u540d\u5355\u5f00\u5927\u4e86,\u4ee5\u540e\u771f\u6f0f\u4e86\u4e5f\u4e0d\u54cd\u3002
            if key not in ("nav.lang", "nav.langTitle") and han.search(value)
        ]
        self.assertEqual(leaked, [], "\n".join(leaked))


def tag_text_args(src: str) -> list[tuple[int, str]]:
    """找出 app.js 里 tag(名字, 属性, 文字) 的第三个参数,原样返回。

    返回 [(行号, 那段源码)]。

    为什么按参数位置找,不直接全文搜英文字符串:app.js 里的英文字符串大半是
    CSS 类名('num dim')、标签名、属性值,搜全文得靠白名单排掉它们,而白名单
    会随着新类名一直长 —— 长到没人维护,检查就废了。tag() 的第三个参数是
    「印到屏幕上的字」,位置本身就是判据。

    括号和引号是自己数的:属性那一项是 { class: 'x' } 这样的对象字面量,里面
    有逗号,按逗号 split 会切错。
    """
    out: list[tuple[int, str]] = []
    for m in re.finditer(r"\btag\(", src):
        i = m.end()
        depth = 0
        args: list[str] = []
        start = i
        quote = ""
        while i < len(src):
            ch = src[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if ch == quote:
                    quote = ""
            elif ch in "'\"`":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:                       # tag( 的收尾括号
                    args.append(src[start:i])
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(src[start:i])
                start = i + 1
            i += 1
        if len(args) >= 3:
            out.append((src[:m.start()].count("\n") + 1, " ".join(args[2].split())))
    return out


def string_literals(code: str) -> list[str]:
    """把一段 JS 里的字符串字面量挨个取出来。

    为什么不用正则配对引号:正则会把前一个字面量的收尾引号和后一个的开头引号
    配成一对,于是把两个字面量之间的代码当成字面量。`t('set.paused')) +
    bits.join(' · ')` 里就会「取出」一段 `) ) + bits.join(`,里面有字母有空格,
    看起来像句英文 —— 这条检查第一版就是这么误报的。引号得顺着扫,不能配对。
    """
    out: list[str] = []
    i = 0
    while i < len(code):
        ch = code[i]
        if ch in "'\"":
            j = i + 1
            buf = []
            while j < len(code):
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == ch:
                    break
                buf.append(code[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
            continue
        i += 1
    return out


# t('some.key', ...) 里的那个键。键本身是英文,但它不是给人看的字。
TRANSLATE_KEY = re.compile(r"\bt\(\s*(['\"])[^'\"]*\1")
# 有字母、而且带空格或大写的字面量才算「给人读的话」。
# 'label' 这种全小写单词是代码里的标记,' · ' 这种没字母的是分隔符,都不算。
LOOKS_LIKE_PROSE = re.compile(r"(?=.*[A-Za-z])(?=.*(?:[ ]|[A-Z]))", re.S)


class NoHardcodedUITextTest(unittest.TestCase):
    """app.js 里不该有写死的、给人读的文字。

    这条是补上一个真实的漏:baseline 那行的 'as of ' 一直写死在 app.js 里,
    中文界面上就那么挂着一句英文。上面那些检查全都查的是 i18n.js —— 一句
    从没进过字符串表的话,它们一个都碰不到。

    不需要 node,纯读源码。
    """

    def test_tag_text_goes_through_translate(self) -> None:
        """tag() 第三个参数里不该有写死的话。

        先把 t('键') 里的键抠掉 —— 键是英文,但不是给人看的字,不抠掉的话
        每一处正确的调用都会误报。抠完之后剩下的字面量,只要有字母又带空格或
        大写,就是漏出来的原文。

        判据不是「整个参数是个字符串字面量」:漏掉的那句是
        `snap ? 'as of ' : ''`,写在三元表达式里 —— 按「整个参数」判,它一开始
        就不在检查范围内,这条测试第一版就是这么写的,把真的漏放过去了。
        """
        bad = []
        for line, arg in tag_text_args(APP_JS.read_text(encoding="utf-8")):
            stripped = TRANSLATE_KEY.sub("t(", arg)
            for lit in string_literals(stripped):
                if LOOKS_LIKE_PROSE.match(lit):
                    bad.append(f"app.js:{line} 写死了「{lit}」,应该走 t() —— {arg}")
        self.assertEqual(bad, [], "\n".join(bad))


@unittest.skipIf(NODE is None, "没装 node,跳过前端文案检查")
class BackendCodeCoverageTest(unittest.TestCase):
    """后端出的代号和前端的文案要一一对上。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.keys = set(load_strings()["zh"])

    def test_every_cleanup_rule_has_text(self) -> None:
        missing = []
        for _needle, code, _safety in CLEANUP_RULES:
            for part in ("label", "advice"):
                key = f"clean.rule.{code}.{part}"
                if key not in self.keys:
                    missing.append(key)
        self.assertEqual(missing, [], "CLEANUP_RULES 有代号没文案:" + "\n".join(missing))

    def test_no_orphan_cleanup_text(self) -> None:
        """有文案、后端没这个代号 —— 规则删了文案没删,或者代号拼错了。"""
        codes = {code for _n, code, _s in CLEANUP_RULES}
        orphans = sorted(
            key for key in self.keys
            if key.startswith("clean.rule.") and key.split(".")[2] not in codes
        )
        self.assertEqual(orphans, [], "文案没有对应的规则:" + "\n".join(orphans))

    def test_every_diff_caveat_has_text(self) -> None:
        """diff.py 里 {"code": "xxx"} 的每个代号都得有文案。

        用正则扫源码而不是跑一遍 diff:那几条口径说明各自要很特定的快照组合
        才会触发(降级、方式不同、一边没文件明细……),跑一遍只能覆盖到其中
        一两条,剩下的漏了照样绿。
        """
        src = DIFF_PY.read_text(encoding="utf-8")
        codes = set(re.findall(r'"code":\s*"(\w+)"', src))
        self.assertTrue(codes, "没在 diff.py 里找到任何代号,正则该改了")
        missing = sorted(f"diff.caveat.{c}" for c in codes
                         if f"diff.caveat.{c}" not in self.keys)
        self.assertEqual(missing, [], "diff.py 有代号没文案:" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
