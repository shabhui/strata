"""SQLite 层:建库、写快照、读快照。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .. import config

# 打包成 exe 之后 schema.sql 不在这个文件旁边,统一走 config 里那个函数
SCHEMA_PATH = config.schema_path()
SCHEMA_VERSION = "1"

# 路径分隔符和它紧邻的下一个码位。取子目录时用作范围上界:
# 只有这两个码位相邻,[parent+SEP, parent+SEP_NEXT) 才恰好圈住 parent 的后代。
SEP = "\\"          # chr(92)
SEP_NEXT = "]"      # chr(93)


@dataclass(slots=True)
class Snapshot:
    """一次扫描的元数据。id 在写入后回填。"""

    drive: str
    taken_at: float
    method: str
    total_bytes: int
    free_bytes: int
    used_bytes: int
    scanned_bytes: int = 0
    file_count: int = 0
    dir_count: int = 0
    duration_ms: int = 0
    complete: bool = True
    note: str | None = None
    id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "drive": self.drive,
            "taken_at": self.taken_at,
            "method": self.method,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "used_bytes": self.used_bytes,
            "scanned_bytes": self.scanned_bytes,
            "file_count": self.file_count,
            "dir_count": self.dir_count,
            "duration_ms": self.duration_ms,
            "complete": bool(self.complete),
            "note": self.note,
        }


@dataclass(slots=True)
class DirRow:
    path: str
    depth: int
    bytes: int
    own_bytes: int
    files: int
    dirs: int
    newest_mtime: float | None = None
    newest_ctime: float | None = None
    folded_children: int = 0
    folded_bytes: int = 0


@dataclass(slots=True)
class FileRow:
    path: str
    bytes: int
    mtime: float | None = None
    ctime: float | None = None


@dataclass(slots=True)
class BucketRow:
    day: str
    attribution: str
    bytes: int
    files: int


@dataclass(slots=True)
class UsnRow:
    usn: int
    timestamp: float
    reason: int
    kind: str
    is_dir: bool
    name: str
    path: str | None = None
    bytes: int | None = None


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """打开(必要时创建)数据库并确保结构就位。"""
    target = Path(path) if path is not None else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


# ---- 写入 --------------------------------------------------------------------


def insert_snapshot(conn: sqlite3.Connection, snap: Snapshot) -> int:
    cur = conn.execute(
        """
        INSERT INTO snapshots (drive, taken_at, method, total_bytes, free_bytes,
                               used_bytes, scanned_bytes, file_count, dir_count,
                               duration_ms, complete, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.drive,
            snap.taken_at,
            snap.method,
            snap.total_bytes,
            snap.free_bytes,
            snap.used_bytes,
            snap.scanned_bytes,
            snap.file_count,
            snap.dir_count,
            snap.duration_ms,
            1 if snap.complete else 0,
            snap.note,
        ),
    )
    snap.id = int(cur.lastrowid)
    return snap.id


def update_snapshot_totals(conn: sqlite3.Connection, snap: Snapshot) -> None:
    conn.execute(
        """
        UPDATE snapshots
           SET scanned_bytes = ?, file_count = ?, dir_count = ?,
               duration_ms = ?, complete = ?, note = ?
         WHERE id = ?
        """,
        (
            snap.scanned_bytes,
            snap.file_count,
            snap.dir_count,
            snap.duration_ms,
            1 if snap.complete else 0,
            snap.note,
            snap.id,
        ),
    )


def insert_dirs(conn: sqlite3.Connection, snapshot_id: int, rows: Iterable[DirRow]) -> int:
    def gen() -> Iterator[tuple]:
        for r in rows:
            yield (
                snapshot_id,
                r.path,
                r.depth,
                r.bytes,
                r.own_bytes,
                r.files,
                r.dirs,
                r.newest_mtime,
                r.newest_ctime,
                r.folded_children,
                r.folded_bytes,
            )

    cur = conn.executemany(
        """
        INSERT INTO dirs (snapshot_id, path, depth, bytes, own_bytes, files, dirs,
                          newest_mtime, newest_ctime, folded_children, folded_bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id, path) DO NOTHING
        """,
        gen(),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def insert_files(conn: sqlite3.Connection, snapshot_id: int, rows: Iterable[FileRow]) -> int:
    def gen() -> Iterator[tuple]:
        for r in rows:
            yield (snapshot_id, r.path, r.bytes, r.mtime, r.ctime)

    cur = conn.executemany(
        """
        INSERT INTO files (snapshot_id, path, bytes, mtime, ctime)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id, path) DO NOTHING
        """,
        gen(),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def insert_buckets(conn: sqlite3.Connection, snapshot_id: int, rows: Iterable[BucketRow]) -> int:
    def gen() -> Iterator[tuple]:
        for r in rows:
            yield (snapshot_id, r.day, r.attribution, r.bytes, r.files)

    cur = conn.executemany(
        """
        INSERT INTO age_buckets (snapshot_id, day, attribution, bytes, files)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id, day, attribution) DO UPDATE
            SET bytes = bytes + excluded.bytes,
                files = files + excluded.files
        """,
        gen(),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def insert_usn_events(conn: sqlite3.Connection, drive: str, rows: Iterable[UsnRow]) -> int:
    def gen() -> Iterator[tuple]:
        for r in rows:
            yield (
                drive,
                r.usn,
                r.timestamp,
                r.reason,
                r.kind,
                1 if r.is_dir else 0,
                r.name,
                r.path,
                r.bytes,
            )

    cur = conn.executemany(
        """
        INSERT INTO usn_events (drive, usn, timestamp, reason, kind, is_dir, name, path, bytes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive, usn) DO NOTHING
        """,
        gen(),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def set_usn_cursor(conn: sqlite3.Connection, drive: str, journal_id: int, next_usn: int) -> None:
    conn.execute(
        """
        INSERT INTO usn_cursor (drive, journal_id, next_usn, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(drive) DO UPDATE
            SET journal_id = excluded.journal_id,
                next_usn = excluded.next_usn,
                updated_at = excluded.updated_at
        """,
        (drive, journal_id, next_usn, time.time()),
    )


def get_usn_cursor(conn: sqlite3.Connection, drive: str) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT journal_id, next_usn FROM usn_cursor WHERE drive = ?", (drive,)
    ).fetchone()
    return (int(row["journal_id"]), int(row["next_usn"])) if row else None


# ---- 读取 --------------------------------------------------------------------


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()


def latest_snapshot(conn: sqlite3.Connection, drive: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM snapshots
         WHERE drive = ? AND complete = 1
         ORDER BY taken_at DESC LIMIT 1
        """,
        (drive,),
    ).fetchone()


def list_snapshots(
    conn: sqlite3.Connection, drive: str | None = None, limit: int = 400
) -> list[sqlite3.Row]:
    if drive:
        return list(
            conn.execute(
                """
                SELECT * FROM snapshots
                 WHERE drive = ? AND complete = 1
                 ORDER BY taken_at DESC LIMIT ?
                """,
                (drive, limit),
            )
        )
    return list(
        conn.execute(
            "SELECT * FROM snapshots WHERE complete = 1 ORDER BY taken_at DESC LIMIT ?",
            (limit,),
        )
    )


def known_drives(conn: sqlite3.Connection) -> list[str]:
    return [r["drive"] for r in conn.execute("SELECT DISTINCT drive FROM snapshots ORDER BY drive")]


def children_of(
    conn: sqlite3.Connection, snapshot_id: int, parent: str, limit: int = 400
) -> list[sqlite3.Row]:
    """取某目录的直接子目录,按占用降序。parent='' 表示盘根。"""
    depth = 1 if parent == "" else parent.count("\\") + 2
    if parent == "":
        return list(
            conn.execute(
                """
                SELECT * FROM dirs
                 WHERE snapshot_id = ? AND depth = 1
                 ORDER BY bytes DESC LIMIT ?
                """,
                (snapshot_id, limit),
            )
        )
    # 用范围而不是 LIKE:dirs 是 WITHOUT ROWID,二级索引会把主键列附在后面,
    # 所以 idx_dirs_snap_depth 实际上就是 (snapshot_id, depth, path) —— 范围
    # 谓词能直接 seek 到这棵子树。带 ESCAPE 的 LIKE 用不上这个,只能逐行取出来
    # 试匹配再排序,实测 22 ms 对 0.03 ms。
    #
    # 上界取分隔符的下一个码位:落在 [parent+SEP, parent+SEP_NEXT) 里的路径,
    # 紧跟 parent 的那个字符只可能是分隔符本身,所以圈中的正好是 parent 的后代,
    # depth 再把它收窄到直接子目录。顺带也不用再转义 LIKE 元字符了。
    return list(
        conn.execute(
            """
            SELECT * FROM dirs
             WHERE snapshot_id = ? AND depth = ?
               AND path >= ? AND path < ?
             ORDER BY bytes DESC LIMIT ?
            """,
            (snapshot_id, depth, parent + SEP, parent + SEP_NEXT, limit),
        )
    )


def get_dir(conn: sqlite3.Connection, snapshot_id: int, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM dirs WHERE snapshot_id = ? AND path = ?", (snapshot_id, path)
    ).fetchone()


def refresh_stats(conn: sqlite3.Connection) -> None:
    """刷新查询规划器的统计信息。

    没有 sqlite_stat1,规划器只能用内置的粗略估计去猜每个条件能筛掉多少行,
    而它在这个库上猜得很偏。最典型的是根视图:`depth = 1` 只有 28 行,规划器
    却认定按 bytes 索引扫更划算,于是为了凑满 LIMIT 200 把六万条索引全走穿 ——
    115 ms。有统计信息之后同一条查询 0.07 ms。目录热点那条 `depth <= 6` 同理,
    38.6 ms 降到 0.3 ms。

    ANALYZE 在这个规模的库上 42 ms,不额外占空间,所以每次扫描完跟着跑一次,
    让统计信息跟着数据走。必须在事务外调用。
    """
    conn.execute("ANALYZE")


def prune_old_buckets(conn: sqlite3.Connection, drive: str, keep_snapshot_id: int) -> None:
    """年龄分布只对最新快照有意义,旧的删掉省空间。"""
    conn.execute(
        """
        DELETE FROM age_buckets
         WHERE snapshot_id IN (
               SELECT id FROM snapshots WHERE drive = ? AND id <> ?
         )
        """,
        (drive, keep_snapshot_id),
    )


def demote_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> tuple[int, int]:
    """把旧快照降级成粗粒度,只留够画时间轴和做对比的行。

    最新快照要支持树图钻取,必须全量;旧快照没这个需求。
    返回 (删掉的目录行数, 删掉的文件行数)。
    """
    cur = conn.execute(
        """
        DELETE FROM dirs
         WHERE snapshot_id = ? AND depth > ? AND bytes < ?
        """,
        (snapshot_id, config.DEMOTE_DIR_MAX_DEPTH, config.DEMOTE_DIR_MIN_BYTES),
    )
    dirs_gone = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    cur = conn.execute("DELETE FROM files WHERE snapshot_id = ?", (snapshot_id,))
    files_gone = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    conn.execute(
        "UPDATE snapshots SET note = COALESCE(note || ' ', '') || '[已降级]' "
        "WHERE id = ? AND COALESCE(note, '') NOT LIKE '%[已降级]%'",
        (snapshot_id,),
    )
    return dirs_gone, files_gone


def demote_previous_snapshots(conn: sqlite3.Connection, drive: str, keep_snapshot_id: int) -> int:
    """降级某盘除 keep_snapshot_id 以外所有尚未降级的快照,返回处理的快照数。"""
    rows = list(
        conn.execute(
            """
            SELECT id FROM snapshots
             WHERE drive = ? AND id <> ? AND COALESCE(note, '') NOT LIKE '%[已降级]%'
            """,
            (drive, keep_snapshot_id),
        )
    )
    for row in rows:
        demote_snapshot(conn, int(row["id"]))
    return len(rows)


def prune_snapshots(conn: sqlite3.Connection, drive: str, keep_daily: int = 120) -> int:
    """保留最近 keep_daily 天里每天最后一个快照,更早的每月留一个。

    返回删除的快照数。
    """
    rows = list(
        conn.execute(
            "SELECT id, taken_at FROM snapshots WHERE drive = ? ORDER BY taken_at DESC",
            (drive,),
        )
    )
    if not rows:
        return 0

    cutoff = time.time() - keep_daily * 86400
    keep: set[int] = set()
    seen_day: set[str] = set()
    seen_month: set[str] = set()
    for row in rows:
        ts = float(row["taken_at"])
        stamp = time.localtime(ts)
        day = time.strftime("%Y-%m-%d", stamp)
        month = time.strftime("%Y-%m", stamp)
        if ts >= cutoff:
            if day not in seen_day:
                seen_day.add(day)
                keep.add(int(row["id"]))
        elif month not in seen_month:
            seen_month.add(month)
            keep.add(int(row["id"]))

    doomed = [int(r["id"]) for r in rows if int(r["id"]) not in keep]
    if doomed:
        conn.executemany("DELETE FROM snapshots WHERE id = ?", [(i,) for i in doomed])
    return len(doomed)


def vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")
    # WAL 模式下 VACUUM 只是把重建写进 WAL，不 checkpoint 的话主库一字节不会缩。
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def wasted_bytes(conn: sqlite3.Connection) -> tuple[int, int]:
    """返回 (freelist 字节, 库文件字节)。

    SQLite 删行只把页挂到 freelist,文件大小一分不降。降级和清理每次扫描
    都在删行,所以这个数只会往上走,除非 VACUUM。
    """
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    return free * page_size, page_count * page_size


def maybe_vacuum(
    conn: sqlite3.Connection,
    *,
    min_waste_bytes: int = 8 * 1024 * 1024,
    min_waste_ratio: float = 0.2,
) -> bool:
    """浪费得够多才 VACUUM,返回是否真的做了。

    VACUUM 要整库重写(实测 26 MB 用 0.4 秒),每次扫描都做是白费 I/O;
    一直不做,文件会稳定停在实际数据的两倍上下。所以设个阈值:既浪费超过
    8 MB、又占到文件两成以上才动手。

    必须在事务外调用 —— VACUUM 不能出现在事务里。
    """
    waste, total = wasted_bytes(conn)
    if waste < min_waste_bytes or not total:
        return False
    if waste / total < min_waste_ratio:
        return False
    vacuum(conn)
    return True


def db_size_bytes(path: Path | str | None = None) -> int:
    target = Path(path) if path is not None else config.db_path()
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(target) + suffix)
        if candidate.exists():
            total += candidate.stat().st_size
    return total
