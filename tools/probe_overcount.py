"""两条扫描路都把 C: 虚报了 27 GB,查是谁贡献的。

现象(库里的快照):

    #19 mft       196.7G  系统已用 169.7G  +15.9%
    #9  scandir   196.9G  系统已用 170.4G  +15.6%
    #3  scandir   169.5G  系统已用 166.4G   +1.9%   ← 更早的快照是准的
    #2  scandir   170.2G  系统已用 168.5G   +1.0%

两条路偏差一样大,说明不是某条路的口径问题,是有些文件本身被高估了。
更早的快照准,说明是后来盘上多了什么东西。

判据用 GetCompressedFileSizeW:它返回**实际占用的磁盘字节**,稀疏文件和
压缩文件都算准。拿它和库里记的字节数比,差得最多的就是元凶。

    python tools/probe_overcount.py

只读:读库、对文件调 API 问大小。不写任何东西,不碰文件内容。
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata import config  # noqa: E402

GIB = 2**30
INVALID_FILE_SIZE = 0xFFFFFFFF


def real_disk_bytes(path: str) -> int | None:
    """这个文件实际占多少磁盘字节。取不到返回 None。

    GetCompressedFileSizeW 是唯一直接回答这个问题的 API:稀疏文件只算
    真正分配的簇,压缩文件算压缩后的。st_size 和 MFT 的 allocated_size
    都可能远大于它。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = k32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == INVALID_FILE_SIZE:
        err = ctypes.get_last_error()
        if err != 0:
            return None
    return (high.value << 32) | low


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    conn = sqlite3.connect(str(config.db_path()))
    conn.row_factory = sqlite3.Row

    snaps = conn.execute(
        """SELECT id, method, scanned_bytes, used_bytes
             FROM snapshots WHERE drive = ? ORDER BY id DESC LIMIT 4""",
        (drive,),
    ).fetchall()
    print(f"盘 {drive} 的近期快照:")
    for s in snaps:
        d = (s["scanned_bytes"] - s["used_bytes"]) / s["used_bytes"] * 100
        print(f"  #{s['id']:<3} {s['method']:<8} 扫到 {s['scanned_bytes']/GIB:>7,.1f}G  "
              f"系统 {s['used_bytes']/GIB:>7,.1f}G  {d:>+6.1f}%")

    # 挑最近那个有 files 明细的快照
    target = None
    for s in snaps:
        n = conn.execute("SELECT COUNT(*) c FROM files WHERE snapshot_id = ?",
                         (s["id"],)).fetchone()["c"]
        if n:
            target = (s, n)
            break
    if target is None:
        print("\n近期快照都没有 files 明细(旧快照会被降级删掉),没法逐文件比。")
        return 1
    snap, n_files = target
    print(f"\n拿快照 #{snap['id']}({snap['method']})的 {n_files:,} 条文件明细来比\n")

    rows = conn.execute(
        """SELECT path, bytes FROM files
            WHERE snapshot_id = ? ORDER BY bytes DESC LIMIT 400""",
        (snap["id"],),
    ).fetchall()

    root = drive + "\\"
    over: list[tuple[int, int, int, str]] = []   # (差, 记的, 实际, 路径)
    missing = 0
    checked = 0
    for r in rows:
        full = root + r["path"]
        if not os.path.exists(full):
            missing += 1
            continue
        actual = real_disk_bytes(full)
        if actual is None:
            missing += 1
            continue
        checked += 1
        diff = r["bytes"] - actual
        if diff > 0:
            over.append((diff, r["bytes"], actual, r["path"]))

    over.sort(reverse=True)
    print(f"查了 {checked} 个最大的文件,{missing} 个已经不在了或取不到大小")
    print(f"其中 {len(over)} 个被高估,合计高估 {sum(d for d, *_ in over)/GIB:,.2f} GiB\n")

    if over:
        print(f"{'高估':>10} {'库里记的':>10} {'实际占盘':>10}  路径")
        for diff, recorded, actual, path in over[:15]:
            print(f"{diff/GIB:>9,.2f}G {recorded/GIB:>9,.2f}G {actual/GIB:>9,.2f}G  "
                  f"{path[:72]}")

    total_over = sum(d for d, *_ in over)
    gap = snap["scanned_bytes"] - snap["used_bytes"]
    print(f"\n这 {checked} 个文件贡献的高估   {total_over/GIB:>7,.2f} GiB")
    print(f"整个快照的偏差              {gap/GIB:>7,.2f} GiB")
    if gap:
        print(f"→ 前 {checked} 个大文件解释了其中 {total_over/gap*100:.0f}%")
        if total_over / gap < 0.5:
            print("  剩下的在别处 —— 可能是大量小文件,或者 files 表只存了")
            print("  够大/够新的那些(见 config.FILE_KEEP_MIN_BYTES),看不到全貌。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
