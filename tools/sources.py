"""算一个「打进 exe 里的源码」指纹。build_exe 写下来,verify_exe 比对。

要解决的问题:verify_exe 验的是 dist/Strata.exe 这个文件,但它没法知道这个
文件是从哪版代码打出来的。改了 src/ 忘了重新打包,再跑一遍验证,它照样报
「全部通过,可以发出去了」—— 报告的是真的,只是关于一个旧二进制。

这跟刚修掉的那条界面目录检查是同一类毛病:检查本身能过,但它过的不是你以为
的那件事。

为什么按内容算而不按 mtime:换个分支、重新克隆、git 碰一下文件,mtime 就变了,
而代码没变。那样会天天误报,而误报久了人就不看了 —— 一个被忽略的警告和没有
警告是一回事。内容变了才是真的要重打。

为什么归一化换行:仓库里是 worktree CRLF / index LF。.py 编成字节码之后
CRLF 和 LF 没有区别,web/ 那几个文件的换行也不影响功能。按原始字节算的话,
git 重写一遍换行就报「过期」,又是误报。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FINGERPRINT = ROOT / "dist" / "Strata.sources.json"

# 影响产物的东西,和 strata.spec 里 datas/入口那几项对齐:
#   entry.py        入口
#   src/strata/**   全部代码 + web/ 整个目录 + schema.sql
#   strata.manifest 嵌进 exe 的 manifest(提权、长路径)
#   strata.spec     打包配置本身(datas 漏一项就是运行到那儿才崩)
# 不含 build_exe.py / verify_exe.py:那是工具,不进产物。
PARTS = ("tools/entry.py", "tools/strata.manifest", "tools/strata.spec")
TREE = "src/strata"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def bundled_files() -> list[Path]:
    """打进 exe 的文件,排好序(顺序不定的话指纹就不稳)。"""
    found = [ROOT / p for p in PARTS]
    for path in sorted((ROOT / TREE).rglob("*")):
        if not path.is_file() or path.suffix in SKIP_SUFFIX:
            continue
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return [p for p in found if p.is_file()]


def digest_of(path: Path) -> str:
    raw = path.read_bytes()
    # 二进制文件(manifest 是 XML 文本,这里其实都是文本)照原样;
    # 文本统一成 \n 再算。
    try:
        raw = raw.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
    except UnicodeDecodeError:
        pass
    return hashlib.sha256(raw).hexdigest()[:16]


def fingerprint() -> dict[str, str]:
    """{相对路径: 短哈希}。缺文件也记一条,不然少一个文件看不出来。"""
    out: dict[str, str] = {}
    for path in bundled_files():
        rel = path.relative_to(ROOT).as_posix()
        out[rel] = digest_of(path)
    for rel in PARTS:
        out.setdefault(rel, "缺失")
    return out


def write(target: Path = FINGERPRINT) -> dict[str, str]:
    fp = fingerprint()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(fp, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    return fp


def compare(target: Path = FINGERPRINT) -> tuple[str, list[str]]:
    """跟记录下来的指纹比。返回 (结论, 具体哪些文件变了)。

    结论:
      same     一致,exe 就是这版代码打出来的
      stale    有文件变了 —— 得重新打包
      missing  没有指纹文件(上次打包时还没有这套机制,或者 dist/ 是手搓的)
    """
    if not target.exists():
        return "missing", []
    try:
        was = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "missing", [f"指纹文件读不动:{exc}"]

    now = fingerprint()
    diff = []
    for rel in sorted(set(was) | set(now)):
        before, after = was.get(rel), now.get(rel)
        if before == after:
            continue
        if before is None:
            diff.append(f"新增 {rel}")
        elif after is None:
            diff.append(f"删掉 {rel}")
        else:
            diff.append(f"改了 {rel}")
    return ("same" if not diff else "stale"), diff
