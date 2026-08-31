"""库里对 WOF 文件记的是哪个数 —— 逻辑大小还是真实占盘。

为什么要单独问一次:probe_wof.py 已经证明 MFT 里读得到真实占盘
(Sessions.xml 的 WofCompressedData 流 = 17.85M = GetCompressedFileSizeW),
而且 _parse_record 算的是 alloc + named_alloc,也就是 17.85M。
但 probe_overcount.py 看到库里记着 0.13 GiB(≈137.9M,逻辑大小)。

算得对、存进去的却不对,这中间一定还有一层。这个工具把同一个路径在
各个快照里的数字排出来,顺便看两条路(mft / scandir)记的是否一样 ——
一样就说明不是 MFT 特有的问题,不一样就能定位到哪条路丢了精度。

只读:用 mode=ro 打开真库,不写、不建表、不删快照。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata import config  # noqa: E402

MIB = 2**20
GIB = 2**30

TARGETS = [
    r"Windows\servicing\Sessions\Sessions.xml",
    r"Windows\explorer.exe",
    r"Windows\System32\kernel32.dll",
    r"Windows\System32\msedge.dll",
]


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    uri = "file:" + config.db_path().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    print(f"{drive} 的近期快照:")
    snaps = conn.execute(
        """SELECT id, method, scanned_bytes, used_bytes FROM snapshots
            WHERE drive = ? ORDER BY id DESC LIMIT 10""",
        (drive,),
    ).fetchall()
    for s in snaps:
        n = conn.execute(
            "SELECT COUNT(*) c FROM files WHERE snapshot_id = ?", (s["id"],)
        ).fetchone()["c"]
        gap = ((s["scanned_bytes"] - s["used_bytes"]) / s["used_bytes"] * 100
               if s["used_bytes"] else 0.0)
        print(f"  #{s['id']:<3} {s['method']:<8} 扫到 {s['scanned_bytes']/GIB:>7,.1f}G "
              f"系统 {s['used_bytes']/GIB:>7,.1f}G {gap:>+6.1f}%  明细 {n:,} 行")

    for t in TARGETS:
        print(f"\n{t}")
        rows = conn.execute(
            """SELECT f.snapshot_id, f.bytes, s.method
                 FROM files f JOIN snapshots s ON s.id = f.snapshot_id
                WHERE f.path = ? AND s.drive = ?
                ORDER BY f.snapshot_id DESC LIMIT 8""",
            (t, drive),
        ).fetchall()
        if not rows:
            print("  不在 files 表里(小于 FILE_KEEP_MIN_BYTES,或那个快照被降级了)")
            continue
        for r in rows:
            print(f"  #{r['snapshot_id']:<3} {r['method']:<8} {r['bytes']/MIB:>10,.2f}M")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
