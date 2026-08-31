"""MFT 条目 → ScanEntry 那一段,值多少秒。

要回答的问题:C: 走 MFT 全程 100.5 秒。已经用 prof_pipeline.py 量掉了收集
之后那五遍(8.8 秒),用算术排掉了写库(scandir 全程只要 38.8 秒,同样那五遍
同样那次写库都在里面)。剩下 80 秒以上落在 MFT 独有的两段:

    read_entries          读 + 解析,要提权,见 bench_mft_read.py
    _mft_to_scan_entries  resolve_paths + 161 万次建对象,就是这个工具量的

这一段不需要管理员权限:合成 FileEntry,不碰真盘。

    python tools/prof_mft_convert.py

规模照真实的 C: 来:MFT 约 161 万条记录,产出 1,079,118 个条目
(config.py:135)。合成路径的形状不可能和真盘完全一样,数字只用来判断量级。
"""

from __future__ import annotations

import gc
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import mft  # noqa: E402
from strata.scan import snapshot as snap_mod  # noqa: E402

N_RECORDS = 1_610_000        # C: 的 MFT 记录数
N_DIRS = 290_418             # 照快照 #9
DIR_NAMES = [
    "Windows", "Users", "AppData", "Local", "Roaming", "ProgramFiles",
    "node_modules", "src", "build", "dist", "cache", "Temp", "lib", "bin",
]


def synth(seed: int = 11) -> list[mft.FileEntry]:
    """造一批 FileEntry,父引用构成一棵真的树。

    目录先造,记录号从 ROOT_RECORD+1 开始连续分配,父引用指向已造出的目录 ——
    这样 resolve_paths 沿父链一定能走到根,不会退化成「全是孤立条目」那种
    比真实情况快得多的假象。
    """
    rnd = random.Random(seed)
    now = time.time()
    out: list[mft.FileEntry] = []

    # 根
    out.append(mft.FileEntry(record=mft.ROOT_RECORD, parent=mft.ROOT_RECORD,
                             name=".", is_dir=True))

    dir_records = [mft.ROOT_RECORD]
    rec = mft.ROOT_RECORD + 1
    for _ in range(N_DIRS):
        # 父在已有目录里随机挑,偏向靠后的 —— 真盘上树是逐层长出来的,
        # 均匀挑父会造出一棵又宽又浅的树,深度分布跟真盘差太远。
        i = int(len(dir_records) * (1 - rnd.random() ** 2.2))
        parent = dir_records[min(i, len(dir_records) - 1)]
        out.append(mft.FileEntry(
            record=rec, parent=parent,
            name=f"{rnd.choice(DIR_NAMES)}{rnd.randrange(9999)}",
            is_dir=True,
            created=now - rnd.randrange(86400 * 900),
            modified=now - rnd.randrange(86400 * 900),
        ))
        dir_records.append(rec)
        rec += 1

    n_files = N_RECORDS - len(out)
    for i in range(n_files):
        out.append(mft.FileEntry(
            record=rec, parent=dir_records[rnd.randrange(len(dir_records))],
            name=f"f{i}.dat", is_dir=False,
            bytes=int(2 ** rnd.uniform(6, 26)),
            logical_bytes=0,
            created=now - rnd.randrange(86400 * 900),
            modified=now - rnd.randrange(86400 * 900),
            has_data=True,
        ))
        rec += 1
    return out


def main() -> int:
    print(f"合成 {N_RECORDS:,} 条 MFT 记录({N_DIRS:,} 目录),照真实 C:")
    t = time.perf_counter()
    entries = synth()
    print(f"  造数据本身 {time.perf_counter() - t:.1f}s(不算在下面)\n")

    gc.collect()

    # resolve_paths 单独量一次 —— ntfs-reader 的实测说这一环可能比解析还贵
    t = time.perf_counter()
    paths, pstats = mft.resolve_paths(entries)
    resolve_s = time.perf_counter() - t

    gc.collect()

    # 再量整个 _mft_to_scan_entries(它内部会再跑一次 resolve_paths)
    dir_paths: dict[int, str] = {}
    t = time.perf_counter()
    out, orphan_bytes, warns = snap_mod._mft_to_scan_entries(
        entries, dir_paths=dir_paths
    )
    convert_s = time.perf_counter() - t

    print(f"{'段':<40} {'秒':>7}")
    print(f"{'resolve_paths(单独)':<40} {resolve_s:>7.2f}s   "
          f"{len(paths):,} 条路径,孤立 {pstats.orphaned:,},成环 {pstats.cycles:,}")
    print(f"{'_mft_to_scan_entries(含上面那次)':<40} {convert_s:>7.2f}s   "
          f"产出 {len(out):,} 条")
    print(f"{'  └ 减掉 resolve,剩下的建对象循环':<40} "
          f"{convert_s - resolve_s:>7.2f}s")

    print(f"\n对照 C: 走 MFT 全程 100.5s:")
    print(f"  收集后五遍(prof_pipeline 量的)         8.8s")
    print(f"  _mft_to_scan_entries(本工具)         {convert_s:>5.1f}s")
    print(f"  ────────────────────────────────────────────")
    print(f"  剩给 read_entries(读 + 解析)         {100.5 - 8.8 - convert_s:>5.1f}s")
    print(f"\n而 bench_mft_read.py 的前提说解析只值 12.7s。差额就是读盘 ——")
    print(f"1.5 GiB 的 MFT 配 FILE_FLAG_NO_BUFFERING(volume.py:186 默认开着)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
