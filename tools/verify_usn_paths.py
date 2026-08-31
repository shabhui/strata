"""端到端验:USN 事件的路径能不能还原,大小能不能跟着填上。

只读。真库先复制到临时目录,所有写都发生在副本上 —— 这个项目里的探针一律
这么办,免得一次跑砸把用户的历史快照弄坏。

## 为什么这么验

链路是:日志给父目录引用 → 反查成路径 → 拼上文件名存进 usn_events →
enrich_deleted_sizes 拿这个路径去 files 表里对大小 → 界面显示
「Downloads\\x.iso(4.2 GB)不见了」。

读日志那一段要管理员,但**反查那一段不要**(提示句柄用盘根目录,见
ntfs/fileid.py)。所以这里把日志换成假的,其余全用真货:

- 真的 64 位父目录引用 —— os.stat(父目录).st_ino,不是编的
- 真的 Win32 调用 —— OpenFileById + GetFinalPathNameByHandleW
- 真的 files 表 —— 从用户库里复制出来的,25,272 行

假的只有「日志里有哪些事件」。而那一段是本来就测透的部分(游标、去重、
分类、日志重建),不是这次改动碰的地方。

## 交叉校验

每条都有两个独立来源可以对:文件路径是从 files 表里读出来的(快照写的),
反查出来的路径是操作系统答的。两边对得上才算过。这比「跑完没报错」强 ——
后者连路径全空都算通过。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strata.ntfs import usn as usn_mod                  # noqa: E402
from strata.scan import changes                         # noqa: E402
from strata.store import db                             # noqa: E402

DRIVE = "C:"
WANT = 40                # 取多少个真文件来试


class FakeJournal:
    """只负责把事件吐出来。游标那套行为在 test_changes.py 里测过,这里不重复。"""

    events: list[usn_mod.UsnEvent] = []

    def __init__(self, drive: str) -> None:
        self.drive = drive

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self):
        return usn_mod.JournalInfo(
            journal_id=1, first_usn=0, next_usn=10_000_000,
            lowest_valid_usn=0, max_usn=1 << 60,
            max_size=32 << 20, allocation_delta=4 << 20,
        )

    def read_all(self, start_usn, *, journal_id=None, max_events=0, **kw):
        yield from self.events


def copy_db() -> str:
    src = os.path.join(os.environ["LOCALAPPDATA"], "Strata", "strata.db")
    dst = os.path.join(tempfile.gettempdir(), "strata_verify_usn.db")
    shutil.copy2(src, dst)
    return dst


def pick_real_files(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """从 files 表里挑还在盘上的文件。要真存在 —— 父目录得开得出来。"""
    rows = conn.execute(
        """
        SELECT f.path, f.bytes FROM files f
          JOIN snapshots s ON s.id = f.snapshot_id
         WHERE s.drive = ?
         ORDER BY s.taken_at DESC, f.bytes DESC
         LIMIT 4000
        """,
        (DRIVE,),
    ).fetchall()

    picked: list[tuple[str, int]] = []
    seen: set[str] = set()
    for r in rows:
        rel = r["path"]
        if rel in seen:
            continue
        full = os.path.join(DRIVE + "\\", rel)
        parent = os.path.dirname(full)
        if not os.path.isdir(parent):
            continue
        seen.add(rel)
        picked.append((rel, int(r["bytes"])))
        if len(picked) >= WANT:
            break
    return picked


def main() -> None:
    path = copy_db()
    conn = db.connect(path)

    picked = pick_real_files(conn)
    print(f"从真库里挑到 {len(picked)} 个还在盘上的文件\n")
    if not picked:
        print("× 一个都没挑到,没法验")
        return

    # 造事件:父目录引用取真的,文件名取真的,其余按删除事件填。
    events = []
    expected: dict[int, tuple[str, int]] = {}     # usn → (期望路径, 期望大小)
    base_usn = 9_000_000
    for i, (rel, size) in enumerate(picked):
        full = os.path.join(DRIVE + "\\", rel)
        parent = os.path.dirname(full)
        ref = os.stat(parent).st_ino               # 真的 64 位引用
        usn = base_usn + i
        events.append(
            usn_mod.UsnEvent(
                usn=usn,
                file_reference=usn + 500,
                parent_reference=ref & ((1 << 48) - 1),   # 掩过的那份
                timestamp=time.time() - 3600,             # 一小时前,别被清理掉
                reason=usn_mod.USN_REASON_FILE_DELETE,
                attributes=0x80,
                name=os.path.basename(rel),
                parent_reference_full=ref,                # 反查要用这份
            )
        )
        expected[usn] = (rel, size)

    FakeJournal.events = events
    original = changes.usn_mod.UsnJournal
    changes.usn_mod.UsnJournal = FakeJournal
    try:
        # 副本里已有 84,303 条历史事件,清掉免得跟这次的混在一起看不清。
        conn.execute("DELETE FROM usn_events")
        conn.execute("DELETE FROM usn_cursor")
        conn.commit()

        # dir_paths 传 None:模拟 scandir 扫描(日常那条路),第一条路给不了答案,
        # 全靠反查。这正是要验的情形。
        stats = changes.collect_usn(conn, DRIVE, dir_paths=None)
    finally:
        changes.usn_mod.UsnJournal = original

    filled = changes.enrich_deleted_sizes(conn, DRIVE)

    print(f"读到事件      {stats.events_read}")
    print(f"存下          {stats.events_stored}")
    print(f"拼出路径      {stats.resolved_paths}")
    print(f"反查命中/失败 {stats.lookups_ok} / {stats.lookups_failed}"
          f"(问了 {stats.lookups_ok + stats.lookups_failed} 个不同的父目录,"
          f"{len(picked)} 条事件 —— 差额是缓存省下的)")
    print(f"补上大小      {filled}")
    if stats.resolver_reason:
        print(f"反查没开起来:{stats.resolver_reason}")
    print()

    rows = {
        r["usn"]: r
        for r in conn.execute(
            "SELECT usn, name, path, bytes FROM usn_events WHERE drive = ?", (DRIVE,)
        )
    }
    exact = wrong = null = size_ok = 0
    print(f"{'期望路径':<62} 结果")
    print("-" * 110)
    shown = 0
    for usn, (rel, size) in sorted(expected.items()):
        row = rows.get(usn)
        got = row["path"] if row else None
        if got is None:
            null += 1
            mark = "× 路径空"
        elif got.upper() == rel.upper():
            exact += 1
            mark = "√"
            if row["bytes"] == size:
                size_ok += 1
                mark = f"√ 大小也对上了({size:,})"
            elif row["bytes"] is not None:
                mark = f"√ 路径对,大小 {row['bytes']:,} ≠ 快照 {size:,}"
            else:
                mark = "√ 路径对,大小没填上"
        else:
            wrong += 1
            mark = f"≠ {got}"
        if shown < 12 or mark.startswith(("×", "≠")):
            head = rel if len(rel) <= 60 else "..." + rel[-57:]
            print(f"{head:<62} {mark}")
            shown += 1

    print("-" * 110)
    total = len(expected)
    print(f"路径逐字一致 {exact}/{total},不一致 {wrong},空 {null};其中大小也对上 {size_ok}")

    if wrong:
        print("\n× 有路径还错了 —— 这是真问题:显示错的位置比不显示更糟。")
    elif null == 0:
        print("\n√ 整条链路通了:引用 → 路径 → 大小,每一步都对得上真数据。")
    else:
        # 空的那些逐个查是「没权限」还是「目录不在了」。这两件事结论不同:
        # 前者提权就好(而生产本来就是管理员),后者本质上问不出来。
        denied = gone = 0
        for usn, (rel, _size) in expected.items():
            if rows.get(usn, {})["path"] is not None:
                continue
            parent = os.path.dirname(os.path.join(DRIVE + "\\", rel))
            if not os.path.isdir(parent):
                gone += 1
                continue
            try:
                os.listdir(parent)
            except PermissionError:
                denied += 1
            except OSError:
                gone += 1
        print(f"\n√ 路径没有一个是错的,{exact}/{total} 逐字一致。")
        print(f"  空的 {null} 个:没权限 {denied},目录已不在 {gone}。")
        if denied:
            print("  没权限那些提权就能拿到 —— collect_usn 本来只在管理员下跑,"
                  "这里是非管理员验的,失败率偏高。")

    conn.close()
    print(f"\n(用的是副本 {path},真库没动)")


if __name__ == "__main__":
    main()
