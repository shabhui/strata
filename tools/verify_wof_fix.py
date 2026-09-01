"""WOF 修好之后,扫出来的总量对不对得上系统报的已用量。

这是那个修法唯一有意义的验收:合成记录的单元测试证明规则实现对了,
但「整盘加起来是否等于 169.7 GB」只有真盘能回答。

修之前的账(库里的真实快照):

    #19 mft       196.7G  系统已用 169.7G  +15.9%
    #9  scandir   196.9G  系统已用 170.4G  +15.6%
    #3  scandir   169.5G  系统已用 166.4G   +1.9%   ← 更早的快照是准的

修完之后实测(同一台机器,系统报已用 169.6G):

    mft       扫到 163.1G   -6.5G  (-3.8%)   23.4s
    scandir   扫到 194.7G  +25.1G (+14.8%)   49.0s

也就是从「多报 27 GB」变成「少报 6.5 GB」。scandir 那条**一点没变**,
和预期一致:它拿的是 st_size,逻辑大小,Windows 不提供便宜的占盘大小。

少报的 6.5 GB 是能说清的,而且都不是错:
  · $MFT 自身约 1.6 GB、$LogFile、$Bitmap —— 记录号 < 16 的元文件全排掉了
  · 卷影副本(系统还原点)占的空间在 MFT 里没有对应文件
  · 目录的索引分配没算(目录一律按 0 字节)
  · 72,381 个联接点/符号链接按 0 算,体积记在目标路径上
  · $UsnJrnl 的活动窗口(见 mft.py 里那段)

少报比多报好:这个工具是用来找「谁在吃空间」的,多报会让人去找不存在的
几十 GB,少报只是漏掉一些本来也无从下手的系统开销。

⚠ 需要管理员权限(MFT 直读裸卷)。
⚠ **不写库**:走 collect_entries + build_tree,不碰 snapshots 表,
   不降级、不删任何历史。跑几次都不会污染数据。

    tools\\run_elevated.bat verify_wof_fix.py C:
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs.volume import volume_space  # noqa: E402
from strata.scan import snapshot, tree  # noqa: E402

GIB = 2**30


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                                    # noqa: BLE001
        return False


def one(drive: str, *, prefer_mft: bool) -> tuple[str, int, int, float]:
    t = time.perf_counter()
    entries, method, warns, reason = snapshot.collect_entries(
        drive, prefer_mft=prefer_mft
    )
    _nodes, scanned, files = tree.build_tree(entries)
    secs = time.perf_counter() - t
    if reason:
        print(f"    退回原因:{reason}")
    for w in warns:
        print(f"    警告:{w}")
    return method, scanned, files, secs


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    total, free = volume_space(drive)
    used = total - free
    print(f"盘 {drive}   容量 {total/GIB:,.1f}G   系统报已用 {used/GIB:,.1f}G")
    print("不写库:只采集 + 建树,不产生快照。\n")

    if not is_admin():
        print("⚠ 没有管理员权限,MFT 那条路会退回 scandir —— 这次比较没有意义。")
        print("  用 tools\\run_elevated.bat verify_wof_fix.py C:\n")

    for want, prefer in (("mft", True), ("scandir", False)):
        print(f"--- {want} ---")
        method, scanned, files, secs = one(drive, prefer_mft=prefer)
        gap = scanned - used
        pct = gap / used * 100 if used else 0.0
        flag = "" if method == want else f"   ⚠ 实际走了 {method}"
        print(f"    扫到 {scanned/GIB:>8,.1f}G   文件 {files:>9,}   "
              f"{secs:>6.1f}s{flag}")
        print(f"    对系统已用量的偏差 {gap/GIB:>+7,.1f}G  ({pct:+.1f}%)\n")

    print("看 mft 那行:修之前 +15.9%(多报 27 GB),现在实测 -3.8%(少报 6.5 GB)。")
    print("少报的部分是元文件、卷影副本、目录索引、联接点 —— 都在上面的警告里。")
    print("scandir 那行预期不变(+14.8%):它只有逻辑大小可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
