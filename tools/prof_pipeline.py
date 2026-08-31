"""收集条目之后那五遍,各自值多少秒。

要回答的问题:C: 走 MFT 全程 100.5 秒,而解析只值约 12.7 秒。剩下的时间
在哪?嫌疑之一是 scan_drive 在收集完之后对着上百万条又走了五遍:

    build_tree / prune_tree / build_buckets / select_files
    再加 snapshot.py:297 那句 sum(1 for e in entries if e.is_dir)

先用算术排掉一半:scandir 那条路跑的是同样这五遍,而它全程只要 38.8 秒
(库里快照 #9,C: 1,175,141 条)。五遍要是值 76 秒,scandir 不可能 38.8 秒
跑完。这个工具把上界钉死 —— 量出具体秒数,才好说 76 秒到底该往哪找。

不需要管理员权限:合成条目,不碰真盘、不写库。规模和形状照真实快照来
(#9 C: 884,723 文件 / 290,418 目录,最深 22 层)。

    python tools/prof_pipeline.py

合成数据的路径分布不可能和真盘完全一样,所以这里的数字只用来判断量级
(「几秒」还是「几十秒」),不当精确值用。
"""

from __future__ import annotations

import gc
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.scan import tree  # noqa: E402

# 照真实快照 #9(C:)来
N_FILES = 884_723
N_DIRS = 290_418
MAX_DEPTH = 22

WORDS = [
    "Windows", "Users", "AppData", "Local", "Roaming", "Program Files",
    "node_modules", "src", "build", "dist", "cache", "Temp", "lib", "bin",
    "include", "share", "docs", "test", "assets", "data", "logs", "config",
]


def synth(seed: int = 7) -> list[tree.ScanEntry]:
    """造一批形状接近真盘的条目。

    目录深度按真盘的样子偏向中层(3-8),少量很深的 —— node_modules 那种。
    文件挂在随机目录下,大小取对数分布(绝大多数小文件、少数很大)。
    """
    rnd = random.Random(seed)
    now = time.time()

    dirs: list[str] = []
    for _ in range(N_DIRS):
        depth = min(MAX_DEPTH, max(1, int(rnd.gauss(5.5, 2.6))))
        parts = [rnd.choice(WORDS) for _ in range(depth)]
        # 加个编号,避免全都撞成同一批路径 —— 真盘上同名目录在不同父下
        parts[-1] = f"{parts[-1]}{rnd.randrange(9999)}"
        dirs.append("\\".join(parts))

    out: list[tree.ScanEntry] = []
    for p in dirs:
        out.append(tree.ScanEntry(
            path=p, is_dir=True, bytes=0,
            modified=now - rnd.randrange(86400 * 900),
            created=now - rnd.randrange(86400 * 900),
        ))
    for i in range(N_FILES):
        d = dirs[rnd.randrange(len(dirs))]
        out.append(tree.ScanEntry(
            path=f"{d}\\f{i}.dat", is_dir=False,
            bytes=int(2 ** rnd.uniform(6, 26)),
            modified=now - rnd.randrange(86400 * 900),
            created=now - rnd.randrange(86400 * 900),
        ))
    rnd.shuffle(out)          # 真盘上目录和文件是交替出现的
    return out


def main() -> int:
    print(f"合成 {N_FILES:,} 文件 + {N_DIRS:,} 目录(照快照 #9 的 C:)")
    t = time.perf_counter()
    entries = synth()
    print(f"  造数据本身 {time.perf_counter() - t:.1f}s(不算在下面)\n")

    gc.collect()
    taken_at = time.time()
    marks: list[tuple[str, float]] = []

    t = time.perf_counter()
    nodes, scanned, files = tree.build_tree(entries)
    marks.append(("build_tree", time.perf_counter() - t))

    t = time.perf_counter()
    dir_rows = tree.prune_tree(nodes)
    marks.append(("prune_tree", time.perf_counter() - t))

    t = time.perf_counter()
    bucket_rows = tree.build_buckets(entries)
    marks.append(("build_buckets", time.perf_counter() - t))

    t = time.perf_counter()
    file_rows = tree.select_files(entries, now=taken_at)
    marks.append(("select_files", time.perf_counter() - t))

    t = time.perf_counter()
    dir_count = sum(1 for e in entries if e.is_dir)
    marks.append(("数目录那句(snapshot.py:297)", time.perf_counter() - t))

    total = sum(s for _, s in marks)
    print(f"{'段':<32} {'秒':>7}   占五遍")
    for name, secs in marks:
        print(f"{name:<32} {secs:>7.2f}s  {secs / total * 100:>5.1f}%")
    print(f"{'合计':<32} {total:>7.2f}s")
    print(f"\n产出  节点 {len(nodes):,}  dirs {len(dir_rows):,}  "
          f"files {len(file_rows):,}  buckets {len(bucket_rows):,}")
    print(f"      {scanned / 2**30:,.1f} GiB  {files:,} 文件  {dir_count:,} 目录")

    print("\n对照:C: 走 MFT 全程 100.5s,走 scandir 全程 38.8s(快照 #9)。")
    print(f"这五遍量到 {total:.1f}s —— 两条路都要走这五遍,所以 MFT 那 100.5s 里")
    print(f"属于 MFT 自己的是 100.5 - {total:.1f} - 写库 ≈ 剩下的大头。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
