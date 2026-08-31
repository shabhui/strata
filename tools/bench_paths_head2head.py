"""同一台机器、同一个时刻,MFT 和 scandir 各跑一次完整扫描。

为什么需要这个:两条路的数字一直是从不同时间、不同缓存状态的运行里拼出来的,
拼出来的比较不能用来决定 config.PREFER_MFT。已知的坑:

    scandir 冷缓存 81.2~142s,热缓存 23.2~38s —— 2.7 倍,比任何代码优化都大
      (walker.py 模块开头)
    MFT 真盘单次方差也大 —— 同样代码量到 45.52 / 47.89 / 71.37 / 89.61
      (config.py 里那段账)

所以只有背靠背跑、并且交换顺序跑两轮,才说明得了问题。

⚠ 需要管理员权限(MFT 要直读裸卷)。
⚠ **会写真库**:每跑一次 scan_drive 就多一个快照。只增不删 —— scan_drive
   自己会把旧快照降级(demote_snapshot),那是它本来的行为,不是这个工具干的。
   一轮写 2 个快照,两轮 4 个。

    tools\\run_elevated.bat bench_paths_head2head.py C:

顺序是刻意交替的:第 1 轮 MFT 先跑(它吃亏,因为 scandir 会捡到它预热的
目录缓存),第 2 轮 scandir 先跑。只信两轮同向的结论。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.scan import snapshot as snap_mod  # noqa: E402
from strata.store import db  # noqa: E402


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def one(conn, drive: str, *, prefer_mft: bool) -> tuple[float, str, int, int]:
    """跑一次完整扫描,返回 (秒, 用了哪条路, 文件数, 目录数)。

    自己掐表而不是用 res.duration_ms:后者是 scan_drive 内部量的,不含
    connect 之后的那些收尾(降级、清理、可能的 VACUUM)。要比的是用户等的
    那段时间,所以从外面量。
    """
    t = time.perf_counter()
    res = snap_mod.scan_drive(conn, drive, prefer_mft=prefer_mft)
    secs = time.perf_counter() - t
    return secs, res.method, res.file_count, res.dir_count


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    if not is_admin():
        print("要管理员权限 —— MFT 那条路要直读裸卷。")
        print("用 tools\\run_elevated.bat bench_paths_head2head.py C:")
        return 2

    conn = db.connect()
    print(f"盘 {drive}   ⚠ 每跑一次多一个快照,一共会写 4 个\n")

    rows: list[tuple[int, str, float, str, int, int]] = []
    # 第 1 轮 MFT 先(它吃亏);第 2 轮 scandir 先。只信两轮同向的。
    for round_no, order in ((1, (True, False)), (2, (False, True))):
        print(f"--- 第 {round_no} 轮({'MFT' if order[0] else 'scandir'} 先跑)---")
        for prefer in order:
            want = "mft" if prefer else "scandir"
            secs, method, nf, nd = one(conn, drive, prefer_mft=prefer)
            flag = "" if method == want else f"  ⚠ 实际走了 {method}"
            print(f"  {want:<8} {secs:>7.1f}s   文件 {nf:>9,}  目录 {nd:>8,}{flag}")
            rows.append((round_no, want, secs, method, nf, nd))

    print("\n两轮对照:")
    for want in ("mft", "scandir"):
        got = [s for _, w, s, _, _, _ in rows if w == want]
        if len(got) == 2:
            print(f"  {want:<8} {got[0]:>7.1f}s / {got[1]:>7.1f}s   "
                  f"平均 {sum(got) / 2:>7.1f}s")

    mft = [s for _, w, s, _, _, _ in rows if w == "mft"]
    scan = [s for _, w, s, _, _, _ in rows if w == "scandir"]
    if len(mft) == 2 and len(scan) == 2:
        a, b = sum(mft) / 2, sum(scan) / 2
        faster = "MFT" if a < b else "scandir"
        print(f"\n平均看 {faster} 快 {max(a, b) / min(a, b):.2f}x")
        same_dir = (mft[0] < scan[0]) == (mft[1] < scan[1])
        print("两轮同向" if same_dir else
              "⚠ 两轮结论相反 —— 缓存在主导,这次比较不能用来定默认值")

    # 口径差异也一并报:MFT 算占盘大小、硬链接只算一次,scandir 算逻辑大小
    print("\n条目数差异(口径不同,不是谁错):")
    for round_no, want, _, method, nf, nd in rows:
        print(f"  第 {round_no} 轮 {want:<8} 文件 {nf:>9,}  目录 {nd:>8,}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
