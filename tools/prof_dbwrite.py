"""写库那一段值多少秒。

scandir 那条路全程 38.8 秒(库里快照 #9,C: 1,175,141 条)。已经量掉的:

    收集后那五遍   8.8s   prof_pipeline.py

剩下的是 walk_drive 加写库。这个工具量写库,剩下的就归 walk_drive。

不需要管理员权限:合成条目,写到临时目录的新库,跑完删掉。**不碰真库。**

    python tools/prof_dbwrite.py

⚠ 这个工具报的数偏高,别直接当真实写库时间用。原因是合成条目的形状和真盘
差得远 —— 同样 88 万文件 + 29 万目录,产出的行数是:

                本工具      真实快照 #9(C:)
    dirs        381,272     64,795       ← 多 5.9 倍
    files        60,000      9,260       ← 撞到 FILE_ROW_CAP 上限
    age_buckets 218,083      5,749       ← 多 38 倍

差在分布:合成文件大小取 2**uniform(6,26)(64 B ~ 64 MB),太多都过了
FILE_KEEP_MIN_BYTES;合成目录的字节也太均匀,prune_tree 该折叠的没折叠掉。
真盘上绝大多数文件很小、绝大多数目录很浅很空。

所以拿它看**单行成本**(µs/行),别看总秒数。按单行成本折算到真实行数,
真实写库约 4.8 秒;而这个工具会报 45 秒。第一次看这份输出的时候我就是
按总秒数推的,推出「写库比整次扫描还长」这种不可能的结论。
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

from strata.scan import tree  # noqa: E402
from strata.store import db  # noqa: E402

# 照真实快照 #9(C:)
N_FILES = 884_723
N_DIRS = 290_418
WORDS = ["Windows", "Users", "AppData", "Local", "Roaming", "ProgramFiles",
         "node_modules", "src", "build", "dist", "cache", "Temp", "lib", "bin"]


def synth(seed: int = 7) -> list[tree.ScanEntry]:
    rnd = random.Random(seed)
    now = time.time()
    dirs: list[str] = []
    for _ in range(N_DIRS):
        depth = min(22, max(1, int(rnd.gauss(5.5, 2.6))))
        parts = [rnd.choice(WORDS) for _ in range(depth)]
        parts[-1] = f"{parts[-1]}{rnd.randrange(9999)}"
        dirs.append("\\".join(parts))

    out: list[tree.ScanEntry] = []
    for p in dirs:
        out.append(tree.ScanEntry(path=p, is_dir=True, bytes=0,
                                  modified=now - rnd.randrange(86400 * 900),
                                  created=now - rnd.randrange(86400 * 900)))
    for i in range(N_FILES):
        d = dirs[rnd.randrange(len(dirs))]
        out.append(tree.ScanEntry(path=f"{d}\\f{i}.dat", is_dir=False,
                                  bytes=int(2 ** rnd.uniform(6, 26)),
                                  modified=now - rnd.randrange(86400 * 900),
                                  created=now - rnd.randrange(86400 * 900)))
    return out


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="strata_prof_"))
    try:
        print(f"临时库 {tmp}(跑完删)")
        print(f"合成 {N_FILES:,} 文件 + {N_DIRS:,} 目录\n")
        entries = synth()

        taken_at = time.time()
        nodes, scanned, files = tree.build_tree(entries)
        dir_rows = tree.prune_tree(nodes)
        bucket_rows = tree.build_buckets(entries)
        file_rows = tree.select_files(entries, now=taken_at)
        print(f"待写:dirs {len(dir_rows):,} 行,files {len(file_rows):,} 行,"
              f"buckets {len(bucket_rows):,} 行\n")

        conn = db.connect(tmp / "s.db")
        snap = db.Snapshot(
            drive="X:", taken_at=taken_at, method="scandir",
            total_bytes=200 * 2**30, free_bytes=30 * 2**30,
            used_bytes=170 * 2**30, scanned_bytes=scanned,
            file_count=files, dir_count=N_DIRS, complete=False, note=None,
        )
        gc.collect()

        marks: list[tuple[str, float, int]] = []
        t_all = time.perf_counter()
        conn.execute("BEGIN")

        t = time.perf_counter()
        db.insert_snapshot(conn, snap)
        marks.append(("insert_snapshot", time.perf_counter() - t, 1))

        t = time.perf_counter()
        db.insert_dirs(conn, snap.id, dir_rows)
        marks.append((f"insert_dirs({len(dir_rows):,} 行)",
                      time.perf_counter() - t, len(dir_rows)))

        t = time.perf_counter()
        db.insert_files(conn, snap.id, file_rows)
        marks.append((f"insert_files({len(file_rows):,} 行)",
                      time.perf_counter() - t, len(file_rows)))

        t = time.perf_counter()
        db.insert_buckets(conn, snap.id, bucket_rows)
        marks.append((f"insert_buckets({len(bucket_rows):,} 行)",
                      time.perf_counter() - t, len(bucket_rows)))

        snap.complete = True
        snap.duration_ms = 0
        t = time.perf_counter()
        db.update_snapshot_totals(conn, snap)
        marks.append(("update_totals", time.perf_counter() - t, 1))

        t = time.perf_counter()
        conn.commit()
        marks.append(("commit", time.perf_counter() - t, 0))
        total = time.perf_counter() - t_all

        print(f"{'段':<34} {'秒':>7}   占写库   {'µs/行':>9}")
        for name, secs, n in marks:
            per = f"{secs / n * 1e6:.1f}" if n else "-"
            print(f"{name:<34} {secs:>7.2f}s  {secs / total * 100:>5.1f}%  {per:>9}")
        print(f"{'合计':<34} {total:>7.2f}s")

        size = (tmp / "s.db").stat().st_size
        print(f"\n库文件 {size / 2**20:,.1f} MiB")
        conn.close()

        # 按单行成本折算到真实行数。直接用上面那个总秒数会高估 9 倍 ——
        # 合成数据产出的行数比真盘多得多,原因见模块开头。
        real = {"dirs": 64_795, "files": 9_260, "age_buckets": 5_749}
        per_row = {name.split("(")[0]: (secs / n if n else 0.0)
                   for name, secs, n in marks}
        est = (per_row.get("insert_dirs", 0) * real["dirs"]
               + per_row.get("insert_files", 0) * real["files"]
               + per_row.get("insert_buckets", 0) * real["age_buckets"]
               + next(s for nm, s, _ in marks if nm == "commit"))
        print(f"\n按单行成本折算到真实快照 #9 的行数"
              f"(dirs {real['dirs']:,} / files {real['files']:,} / "
              f"buckets {real['age_buckets']:,}):")
        print(f"  写库约 {est:.1f}s   ← 用这个,别用上面那个 {total:.1f}s")
        print(f"\nscandir 那条路全程 38.8s(快照 #9)的账:")
        print(f"  写库(折算)        {est:>5.1f}s")
        print(f"  收集后五遍          8.8s")
        print(f"  ────────────────────────")
        print(f"  剩给 walk_drive   {38.8 - est - 8.8:>5.1f}s")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
