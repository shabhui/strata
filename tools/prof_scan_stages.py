"""一次完整扫描,每一段花多少秒。

为什么要这个:现在整次扫描 23.4 秒(tools/verify_wof_fix.py 量的),但之前
逐段的账是在缓冲区复用、页缓存开大**之前**量的,早就过期了。继续优化得先知道
23.4 秒现在摊在哪儿,不然只是凭印象改。

分段照 snapshot.scan_drive 的实际结构切:

    collect_entries     采集(MFT 直读 + 路径还原 + 转 ScanEntry)
    build_tree          按路径聚合成目录树
    prune_tree          裁剪成要入库的目录行
    build_buckets       每日归因桶
    select_files        挑要入库的单文件明细
    dir_count           数目录(一次全表扫)
    写库                四张表 + 收尾(降级、清理、可能的 VACUUM)

⚠ 需要管理员权限(MFT 直读裸卷)。
⚠ **不写真库**:写到临时文件里的一个新库,跑完删掉。所以不会产生快照、
   不会降级历史。代价是页缓存冷、文件系统缓存冷,写库那一段会比真实情况
   略慢 —— 报的时候会标出来。

    tools\\run_elevated.bat prof_scan_stages.py C:
"""

from __future__ import annotations

import ctypes
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs.volume import volume_space  # noqa: E402
from strata.scan import snapshot as snap_mod  # noqa: E402
from strata.scan import tree  # noqa: E402
from strata.store import db  # noqa: E402

GIB = 2**30


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


class Clock:
    """分段计时。名字和秒数按调用顺序留着,最后一起报。"""

    def __init__(self) -> None:
        self.marks: list[tuple[str, float]] = []
        self._t = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.marks.append((name, now - self._t))
        self._t = now

    def report(self) -> float:
        total = sum(s for _, s in self.marks)
        width = max(len(n) for n, _ in self.marks)
        print(f"\n{'段':<{width}}  {'秒':>7}  {'占比':>6}")
        for name, secs in self.marks:
            print(f"{name:<{width}}  {secs:>7.2f}  {secs / total * 100:>5.1f}%")
        print(f"{'合计':<{width}}  {total:>7.2f}")
        return total


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    if not is_admin():
        print("要管理员权限 —— MFT 那条路要直读裸卷。")
        print("用 tools\\run_elevated.bat prof_scan_stages.py C:")
        return 2

    total_bytes, free_bytes = volume_space(drive)
    print(f"盘 {drive}   容量 {total_bytes/GIB:,.1f}G   "
          f"已用 {(total_bytes-free_bytes)/GIB:,.1f}G")
    print("写到临时库,不碰真库。\n")

    clock = Clock()
    taken_at = time.time()

    dir_paths: dict[int, str] = {}
    entries, method, warnings, reason = snap_mod.collect_entries(
        drive, prefer_mft=True, dir_paths=dir_paths
    )
    clock.lap("collect_entries")
    if method != "mft":
        print(f"⚠ 没走 MFT,实际是 {method}:{reason}")

    nodes, scanned_bytes, file_count = tree.build_tree(entries)
    clock.lap("build_tree")
    dir_rows = tree.prune_tree(nodes)
    clock.lap("prune_tree")
    bucket_rows = tree.build_buckets(entries)
    clock.lap("build_buckets")
    file_rows = tree.select_files(entries, now=taken_at)
    clock.lap("select_files")
    dir_count = sum(1 for e in entries if e.is_dir)
    clock.lap("dir_count")

    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(str(Path(tmp) / "prof.db"))
        clock.lap("建库(冷)")
        snap = db.Snapshot(
            drive=drive, taken_at=taken_at, method=method,
            total_bytes=total_bytes, free_bytes=free_bytes,
            used_bytes=total_bytes - free_bytes,
            scanned_bytes=scanned_bytes, file_count=file_count,
            dir_count=dir_count, complete=False,
        )
        conn.execute("BEGIN")
        db.insert_snapshot(conn, snap)
        clock.lap("写 snapshot")
        db.insert_dirs(conn, snap.id, dir_rows)
        clock.lap("写 dirs")
        db.insert_files(conn, snap.id, file_rows)
        clock.lap("写 files")
        db.insert_buckets(conn, snap.id, bucket_rows)
        clock.lap("写 buckets")
        snap.complete = True
        db.update_snapshot_totals(conn, snap)
        conn.execute("COMMIT")
        clock.lap("提交")
        conn.close()

    total = clock.report()

    print(f"\n条目 {len(entries):,}   文件 {file_count:,}   目录 {dir_count:,}")
    print(f"入库行数:dirs {len(dir_rows):,}  files {len(file_rows):,}  "
          f"buckets {len(bucket_rows):,}")
    print(f"扫到 {scanned_bytes/GIB:,.2f}G")
    for w in warnings:
        print(f"警告:{w}")

    print(f"\n注:写库那几段是空库冷缓存,真实情况下库里已经有历史,"
          f"\n而且 scan_drive 还有收尾(降级旧快照、清理、可能的 VACUUM)没算进来。"
          f"\n真实整次扫描实测 23.4 秒,这里合计 {total:.1f} 秒。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
