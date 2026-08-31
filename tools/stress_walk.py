"""反复对多线程遍历和单线程遍历的结果,专抓「少走了一棵子树」。

结束条件那段是并发代码里最容易错的地方:队列空和「没人在走」必须一起判断,
取活和登记也必须是一步。错了不会报错、不会卡住 —— 只是结果少几条,
跑一次测试很可能是绿的。所以这里跑很多次,而且故意把树做宽做深。

  python tools/stress_walk.py [轮数]

只在临时目录里建树,跑完自己删。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan import walker  # noqa: E402


def build(root: Path, breadth: int = 12, depth: int = 4) -> int:
    """建一棵宽 breadth、深 depth 的树,每层放两个文件。"""
    n = 0
    def rec(d: Path, level: int) -> None:
        nonlocal n
        if level == 0:
            return
        for i in range(breadth if level == depth else 3):
            sub = d / f"d{level}_{i}"
            sub.mkdir(exist_ok=True)
            for j in range(2):
                (sub / f"f{j}.bin").write_bytes(b"\0" * (j + 1))
                n += 1
            rec(sub, level - 1)
    rec(root, depth)
    return n


def norm(entries):
    return sorted((e.path, e.is_dir, e.bytes) for e in entries)


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    tmp = Path(tempfile.mkdtemp(prefix="strata_stress_"))
    try:
        n_files = build(tmp)
        base, bstats = walker.walk_drive(str(tmp), workers=1)
        want = norm(base)
        print(f"树:{n_files:,} 个文件,{bstats.dirs:,} 个目录")
        print(f"单线程基准:{len(want):,} 条\n")

        bad = 0
        for r in range(1, rounds + 1):
            got, gstats = walker.walk_drive(str(tmp), workers=8)
            g = norm(got)
            if g != want or (gstats.files, gstats.dirs) != (bstats.files, bstats.dirs):
                bad += 1
                missing = set(want) - set(g)
                extra = set(g) - set(want)
                print(f"  第 {r} 轮不一致:{len(g):,} 条(应 {len(want):,})"
                      f"  少 {len(missing)}  多 {len(extra)}")
                if missing:
                    print(f"    少的例子:{sorted(missing)[:3]}")
            elif r % 10 == 0:
                print(f"  第 {r} 轮 ok")

        print()
        if bad:
            print(f"失败:{rounds} 轮里 {bad} 轮结果不一致")
            return 1
        print(f"{rounds} 轮全部与单线程一致")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
