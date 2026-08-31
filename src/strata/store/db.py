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
    conn.execute(f"PRAGMA cache_size = -{config.SQLITE_CACHE_KIB}")
    _ensure_schema(conn)
    ensure_byte_rules_boundary(conn)   # 一次性,只在第一次见到这个库时定
    repair_timestamps(conn)      # 一次性,靠 meta 里的标记跳过。见函数说明。
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


# 字节口径分界:这个号及以后的快照按新口径算,之前的按老口径。
#
# 为什么需要它:WOF 那个修法让 mft 那条路对 C: 少报约 38.5 GiB(原来把
# Compact OS 压过的文件按解压后的大小算了,见 ntfs/mft.py 的 WOF_STREAM)。
# 修得对,但升级后第一次扫完,对比页会显示「减少 38 GB」——**硬盘上什么都
# 没发生**。一个专门回答「空间去哪了」的工具把自己的口径调整说成用户删了
# 38 GB,比不给数字更糟:用户会去找那 38 GB,或者以为清理生效了。
#
# 已有的 mixedMethod 提示挡不住:那条看的是 method 变了没(mft ↔ scandir),
# 这里两边都是 mft,变的是同一条路的算法。
#
# 不加表列是因为库里没有迁移机制 —— schema.sql 全是 CREATE TABLE IF NOT
# EXISTS、每次连库重跑一遍,加列对已存在的表不生效。为一个布尔量引进
# ALTER TABLE 那套不值得,meta 表本来就是干这个的(TS_REPAIR_KEY 同理)。
BYTE_RULES_KEY = "byte_rules_wof_v1"


def ensure_byte_rules_boundary(conn: sqlite3.Connection) -> None:
    """第一次见到这个库时,把分界定在「当前最大快照号 + 1」。

    已经在库里的快照一律算老口径 —— 它们确实是老代码扫出来的。空库定成 1,
    于是往后全是新口径,不会有跨口径的对比。

    只在缺这个键时写。每次连库重算的话分界会一直追着最新快照跑,于是永远
    凑不出跨口径的一对,那句提示永远不出现 —— 又是一条永远通过的检查。

    「只写一次」有两道保险:开头这个提前返回,和 SQL 里的 ON CONFLICT DO
    NOTHING。两道都能单独兜住(变异测过:各去掉一道,行为不变;两道都去掉,
    test_boundary_does_not_move_on_later_connects 才红)。别把任何一道当死代码
    删掉 —— 上面 _ensure_schema 用的是 DO UPDATE,照那个样子改这里正是会出错
    的那种编辑。提前返回顺带省掉每次连库那次 MAX(id)。
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (BYTE_RULES_KEY,)
    ).fetchone()
    if row is not None:
        return
    largest = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM snapshots").fetchone()
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (BYTE_RULES_KEY, str(int(largest["m"]) + 1)),
    )


def byte_rules_boundary(conn: sqlite3.Connection) -> int:
    """分界快照号。缺失或坏值时按 1 处理(等于「全是新口径」,不出提示)。"""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (BYTE_RULES_KEY,)
    ).fetchone()
    if row is None:
        return 1
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 1


# 洗过坏时间戳的标记。见 repair_timestamps()。
TS_REPAIR_KEY = "ts_repair_v1"


def repair_timestamps(conn: sqlite3.Connection, *, force: bool = False) -> int:
    """把已经入库的、不可信的时间戳洗成 NULL。返回洗掉几个。

    ## 为什么需要它

    `scan/tree.py` 的 `_newer` 挡住的是以后扫出来的。库里已经躺着的洗不到 ——
    本机上是三个快照:

        快照2 / 快照3 / 快照9   Program Files (x86)\\...\\Internet Download Manager
        快照9  211402.3 MB  2030-09-16  (根)    ← 快照 9 是 C: 当前最新那个

    一个 31.8 MB 目录里的文件,把 211 GB 的盘根染成了 2030 年。MAX 聚合会
    把一个坏值放大到它的每一级祖先。快照要在库里留 120 天,不洗就得等用户
    重扫一遍 C: 才能看到对的数。

    ## 上界用每个快照自己的 taken_at

    扫描记的是「那一刻硬盘长什么样」,所以任何文件的写入时间都不可能晚于
    这次扫描本身。这比拿「现在」当界更紧也更对:一个 2026 年的坏值放在
    2026 年的快照里该洗掉,而拿今天当界就会放过它。

    容差和写入侧共用 `config.newest_ceiling`,两边同一个界 —— 不然洗完再扫
    一次,同一个值一边留一边不留。

    ## 洗成 NULL 而不是改成别的值

    NULL 在下游是「不知道」:列表显示未知,`newest_ctime >= cutoff` 筛不到它。
    退一个凑合的数下游会当真。

    ## 只跑一次

    写入侧已经堵住,之后不会再产生新的坏值,所以每次启动都扫 160k 行是
    「永远只会通过的检查」。洗完在 meta 里记个标记,下次直接跳过。
    force 只给测试和手工重跑用。
    """
    if not force:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (TS_REPAIR_KEY,)
        ).fetchone()
        if row is not None:
            return 0

    # (表, 列) —— 只洗聚合出来的和明细里的时间戳。
    #
    # usn_events.timestamp 不在这儿:它是日志事件自己的时间,不是聚合值,
    # 不会被 MAX 放大;本机上量到 84,303 行全在范围内、没有一个在未来。
    # 加一条永远不会命中的 UPDATE,就是这个项目最反对的那种检查。
    targets = (
        ("dirs", "newest_mtime"),
        ("dirs", "newest_ctime"),
        ("files", "mtime"),
        ("files", "ctime"),
    )

    fixed = 0
    try:
        for table, col in targets:
            for snap_id, taken_at in conn.execute(
                "SELECT id, taken_at FROM snapshots"
            ).fetchall():
                ceiling = config.newest_ceiling(float(taken_at))
                cur = conn.execute(
                    f"""UPDATE {table} SET {col} = NULL
                         WHERE snapshot_id = ?
                           AND {col} IS NOT NULL
                           AND ({col} > ? OR {col} < ?)""",
                    (snap_id, ceiling, config.TS_MIN),
                )
                fixed += cur.rowcount or 0
    except sqlite3.Error:
        # 洗不动就算了。历史是这个工具唯一不可再生的东西,为了修一个显示问题
        # 而拦住启动是本末倒置 —— 大不了那几行继续显示错的日期。
        return 0

    try:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (TS_REPAIR_KEY, str(fixed)),
        )
    except sqlite3.Error:
        pass
    return fixed


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


def set_usn_status(
    conn: sqlite3.Connection, drive: str, *, available: bool, reason: str | None
) -> None:
    """记下这次读日志成没成。成了就把上次的失败原因清掉。

    读成功时必须显式写 reason=NULL —— 不然上次的失败原因会一直挂着,界面上
    就会对着一栏有数据的表说「读不了,因为没权限」。
    """
    conn.execute(
        """
        INSERT INTO usn_status (drive, available, reason, checked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(drive) DO UPDATE
            SET available = excluded.available,
                reason = excluded.reason,
                checked_at = excluded.checked_at
        """,
        (drive, 1 if available else 0, reason, time.time()),
    )


def get_usn_status(conn: sqlite3.Connection, drive: str) -> dict | None:
    """上次读日志的结果。没扫过这个盘就返回 None。

    None 和 available=False 是两回事:前者是「不知道」,后者是「试过,失败了」。
    把没扫过的盘显示成故障是在编事实。
    """
    row = conn.execute(
        "SELECT available, reason, checked_at FROM usn_status WHERE drive = ?",
        (drive,),
    ).fetchone()
    if row is None:
        return None
    return {
        "available": bool(row["available"]),
        "reason": row["reason"],
        "checked_at": float(row["checked_at"]),
    }


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
