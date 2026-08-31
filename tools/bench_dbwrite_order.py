"""写 dirs 表:乱序插 vs 按主键排序插,差多少。

prof_dbwrite.py 量到 insert_dirs 是 94.7 µs/行 —— SQLite 在一个事务里
批量插入不该这么慢。原因在结构上(schema.sql:30-48):

    dirs 是 WITHOUT ROWID 表,主键 (snapshot_id, path) 是文本
    → 整行存在按路径排序的 B 树里
    另有 idx_dirs_snap_bytes 和 idx_dirs_snap_depth 两个索引
    → 每行要插三棵 B 树

而 prune_tree 产出的路径是乱序的,每行都落在 B 树的随机位置:页分裂、
缓存未命中。默认 cache_size 只有 2 MB,而索引有几十 MB。

两个候选:插入前按主键排序,和加大 cache_size。这个工具 A/B 四种组合。

不需要管理员权限。每个变体一个新的临时库,跑完删掉。**不碰真库。**

    python tools/bench_dbwrite_order.py

行数照真实快照 #9(C:)来:64,795 行。合成路径的形状影响 B 树深度,
所以看比值不看绝对值。
"""

from __future__ import annotations

import gc
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.store import db  # noqa: E402

N_ROWS = 64_795          # 真实快照 #9 的 dirs 行数
WORDS = ["Windows", "Users", "AppData", "Local", "Roaming", "ProgramFiles",
         "node_modules", "src", "build", "dist", "cache", "Temp", "lib", "bin"]


def synth_rows(seed: int = 5) -> list[db.DirRow]:
    rnd = random.Random(seed)
    now = time.time()
    seen: set[str] = set()
    rows: list[db.DirRow] = []
    while len(rows) < N_ROWS:
        depth = min(22, max(1, int(rnd.gauss(5.5, 2.6))))
        parts = [rnd.choice(WORDS) for _ in range(depth)]
        parts[-1] = f"{parts[-1]}{rnd.randrange(999999)}"
        p = "\\".join(parts)
        if p in seen:
            continue
        seen.add(p)
        rows.append(db.DirRow(
            path=p, depth=depth, bytes=int(2 ** rnd.uniform(10, 32)),
            own_bytes=int(2 ** rnd.uniform(10, 28)),
            files=rnd.randrange(10000), dirs=rnd.randrange(500),
            newest_mtime=now - rnd.randrange(86400 * 900),
            newest_ctime=now - rnd.randrange(86400 * 900),
            folded_children=rnd.randrange(50), folded_bytes=rnd.randrange(2**30),
        ))
    return rows


def run(rows: list[db.DirRow], *, sort: bool, cache_mib: int) -> tuple[float, int]:
    """写一次,返回 (秒, 写进去几行)。每次都是新库。"""
    tmp = Path(tempfile.mkdtemp(prefix="strata_dbo_"))
    try:
        conn = db.connect(tmp / "s.db")
        if cache_mib:
            # 负数 = 以 KiB 为单位,正数 = 页数。用负数才跟页大小无关。
            conn.execute(f"PRAGMA cache_size = -{cache_mib * 1024}")
        snap = db.Snapshot(
            drive="X:", taken_at=time.time(), method="scandir",
            total_bytes=1, free_bytes=1, used_bytes=1, scanned_bytes=1,
            file_count=1, dir_count=len(rows), complete=False, note=None,
        )
        payload = sorted(rows, key=lambda r: r.path) if sort else rows
        gc.collect()
        conn.execute("BEGIN")
        db.insert_snapshot(conn, snap)
        t = time.perf_counter()
        db.insert_dirs(conn, snap.id, payload)
        conn.commit()
        secs = time.perf_counter() - t
        n = conn.execute(
            "SELECT COUNT(*) c FROM dirs WHERE snapshot_id = ?", (snap.id,)
        ).fetchone()["c"]
        conn.close()
        return secs, n
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print(f"合成 {N_ROWS:,} 行 dirs(照真实快照 #9 的 C:)")
    rows = synth_rows()
    print(f"造数据完毕,开始 A/B\n")

    print(f"{'变体':<34} {'秒':>7} {'µs/行':>9} {'行数':>9}")
    results: dict[str, float] = {}
    for sort in (False, True):
        for cache in (0, 64):
            label = ("按主键排序" if sort else "乱序") + \
                    (f" + cache {cache} MiB" if cache else " + 默认 cache")
            secs, n = run(rows, sort=sort, cache_mib=cache)
            results[label] = secs
            print(f"{label:<34} {secs:>7.2f} {secs / n * 1e6:>9.1f} {n:>9,}")
            assert n == N_ROWS, f"写进去 {n} 行,该是 {N_ROWS} —— 排序把行弄丢了"

    base = results["乱序 + 默认 cache"]
    print(f"\n以「乱序 + 默认 cache」为基准:")
    for label, secs in results.items():
        if label == "乱序 + 默认 cache":
            continue
        print(f"  {label:<32} 快 {base / secs:.2f}x")

    best = min(results.items(), key=lambda kv: kv[1])
    print(f"\n最快:{best[0]}({best[1]:.2f}s)")
    print(f"真实 insert_dirs 现在约 {N_ROWS * 94.7 / 1e6:.1f}s"
          f"(94.7 µs/行 × {N_ROWS:,} 行),换成最快那个约 {best[1]:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
