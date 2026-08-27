"""把 USN 事件收进库里,并按天汇总。

USN 记录里没有文件大小 —— 只知道「这个文件被删了」,不知道它多大。
所以这一层能给出的是「哪些东西消失了、什么时候」,字节数要靠历史快照里的
同名文件反查,拿不到就留空。宁可空着,也不要编一个数字出来。

路径同理:USN 只给父目录的 MFT 记录号。有上一次扫描的目录表就能还原完整
路径,没有就只剩文件名。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import config
from ..ntfs import usn as usn_mod
from ..ntfs.volume import AccessDenied, NtfsError
from ..store import db


@dataclass(slots=True)
class ChangeStats:
    drive: str
    events_read: int = 0
    events_stored: int = 0
    resolved_paths: int = 0
    first_run: bool = False       # 第一次跑,没有连续性可谈
    journal_reset: bool = False   # 本来有游标,但断了 —— 中间那段历史丢了
    available: bool = True
    reason: str | None = None
    first_usn: int | None = None
    last_usn: int | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "drive": self.drive,
            "events_read": self.events_read,
            "events_stored": self.events_stored,
            "resolved_paths": self.resolved_paths,
            "first_run": self.first_run,
            "journal_reset": self.journal_reset,
            "available": self.available,
            "reason": self.reason,
            "first_usn": self.first_usn,
            "last_usn": self.last_usn,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class DaySummary:
    day: str
    created: int = 0
    deleted: int = 0
    renamed: int = 0
    written: int = 0
    deleted_bytes_known: int = 0
    deleted_bytes_unknown_files: int = 0
    top_deleted: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "created": self.created,
            "deleted": self.deleted,
            "renamed": self.renamed,
            "written": self.written,
            "deleted_bytes_known": self.deleted_bytes_known,
            "deleted_bytes_unknown_files": self.deleted_bytes_unknown_files,
            "top_deleted": self.top_deleted,
        }


# 只把这几类存库。write 事件量极大(一次编译几万条)而信息量低,
# 真正想知道「什么在涨」看快照差值更准。
STORED_KINDS = (usn_mod.KIND_CREATE, usn_mod.KIND_DELETE, usn_mod.KIND_RENAME_OLD)


def _compose_path(event: usn_mod.UsnEvent, dir_paths: dict[int, str] | None) -> str | None:
    """用父目录记录号拼出相对路径。父目录不认识就返回 None。"""
    if not dir_paths:
        return None
    parent = dir_paths.get(event.parent_reference)
    if parent is None:
        return None
    return f"{parent}\\{event.name}" if parent else event.name


def collect_usn(
    conn: sqlite3.Connection,
    drive: str,
    *,
    dir_paths: dict[int, str] | None = None,
    max_events: int = 200_000,
    kinds: tuple[str, ...] = STORED_KINDS,
) -> ChangeStats:
    """增量拉取 USN 日志。

    dir_paths 是上次 MFT 扫描得到的「记录号 → 目录路径」,用来还原完整路径。

    日志被重建过(journal_id 变了)就从头再读一遍:日志窗口里的记录还在,
    只是编号体系换了,继续用旧游标会读到错的位置。
    """
    started = time.perf_counter()
    stats = ChangeStats(drive=drive)

    cursor = db.get_usn_cursor(conn, drive)
    try:
        with usn_mod.UsnJournal(drive) as journal:
            info = journal.query()
            start_usn = info.lowest_valid_usn
            if cursor is None:
                # 第一次跑,从日志里现存的最早位置开始。这不是「历史丢了」,
                # 是「历史从这里开始」,界面上的说法不一样
                stats.first_run = True
            else:
                known_journal_id, next_usn = cursor
                if known_journal_id != info.journal_id:
                    # 日志被重建过,旧编号已经没有意义
                    stats.journal_reset = True
                elif next_usn < info.lowest_valid_usn:
                    # 游标滚出窗口了,中间这段历史找不回来 —— 必须让界面知道,
                    # 不能默默跳到窗口起点当作没事发生
                    stats.journal_reset = True
                else:
                    start_usn = next_usn

            stats.first_usn = start_usn
            rows: list[db.UsnRow] = []
            for event in journal.read_all(
                start_usn, journal_id=info.journal_id, max_events=max_events
            ):
                stats.events_read += 1
                kind = event.kind
                if kind not in kinds:
                    continue
                path = _compose_path(event, dir_paths)
                if path:
                    stats.resolved_paths += 1
                rows.append(
                    db.UsnRow(
                        usn=event.usn,
                        timestamp=event.timestamp or time.time(),
                        reason=event.reason,
                        kind=kind,
                        is_dir=event.is_dir,
                        name=event.name,
                        path=path,
                        bytes=None,
                    )
                )

            stats.last_usn = getattr(journal, "last_usn", start_usn)
            if rows:
                stats.events_stored = db.insert_usn_events(conn, drive, rows)
            db.set_usn_cursor(conn, drive, info.journal_id, stats.last_usn or 0)
            conn.commit()

    except usn_mod.JournalUnavailable as exc:
        stats.available = False
        stats.reason = str(exc)
    except AccessDenied as exc:
        stats.available = False
        stats.reason = str(exc)
    except (NtfsError, OSError) as exc:
        stats.available = False
        stats.reason = f"读取 USN 日志失败:{exc}"

    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    return stats


def enrich_deleted_sizes(
    conn: sqlite3.Connection, drive: str, *, limit: int = 5000
) -> int:
    """给删除事件补上大小 —— 从历史快照里按路径反查。

    只在路径唯一命中时才填,命中不到就保持 NULL。这是估算:快照是某个时刻的
    切片,文件在被删之前可能还长大过,那部分看不到。
    """
    rows = list(
        conn.execute(
            """
            SELECT id, path FROM usn_events
             WHERE drive = ? AND kind = 'delete'
               AND bytes IS NULL AND path IS NOT NULL AND is_dir = 0
             LIMIT ?
            """,
            (drive, limit),
        )
    )
    if not rows:
        return 0

    updates: list[tuple[int, int]] = []
    for row in rows:
        hit = conn.execute(
            """
            SELECT f.bytes FROM files f
              JOIN snapshots s ON s.id = f.snapshot_id
             WHERE s.drive = ? AND f.path = ?
             ORDER BY s.taken_at DESC LIMIT 1
            """,
            (drive, row["path"]),
        ).fetchone()
        if hit is not None:
            updates.append((int(hit["bytes"]), int(row["id"])))

    if updates:
        conn.executemany("UPDATE usn_events SET bytes = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def usn_daily_summary(
    conn: sqlite3.Connection, drive: str, *, days: int = 30, top_n: int = 5
) -> list[DaySummary]:
    """按天汇总变更事件,给时间轴的「消失了什么」那一栏用。"""
    cutoff = time.time() - days * 86400
    out: dict[str, DaySummary] = {}

    for row in conn.execute(
        """
        SELECT kind, timestamp, bytes, path, name, is_dir
          FROM usn_events
         WHERE drive = ? AND timestamp >= ?
         ORDER BY timestamp
        """,
        (drive, cutoff),
    ):
        # USN 的时间戳也是从 FILETIME 换算的,同样可能是坏值
        day = config.safe_day(row["timestamp"])
        if day is None:
            continue
        summary = out.get(day)
        if summary is None:
            summary = DaySummary(day=day)
            out[day] = summary

        kind = row["kind"]
        if kind == usn_mod.KIND_CREATE:
            summary.created += 1
        elif kind == usn_mod.KIND_DELETE:
            summary.deleted += 1
            size = row["bytes"]
            if size:
                summary.deleted_bytes_known += int(size)
            else:
                summary.deleted_bytes_unknown_files += 1
        elif kind in (usn_mod.KIND_RENAME_OLD, usn_mod.KIND_RENAME_NEW):
            summary.renamed += 1
        elif kind == usn_mod.KIND_WRITE:
            summary.written += 1

    # 每天挑几个最大的删除给界面展示
    for day, summary in out.items():
        if not summary.deleted:
            continue
        start = config.day_timestamp(day)
        if start is None:
            # 这一天算不出当地午夜就不查明细,而不是让它把整个结果带崩。
            # 走 HTTP 打不到这里:上面的 cutoff 只放过 days 天内的事件,而
            # days 被接口层夹在 3650。但 days 本身没有上界,函数对任何取值都
            # 该成立 —— UTC+8 上时间戳 0 的事件会被归到 '1970-01-01',那天的
            # 当地午夜在 epoch 之前,原来这行会抛 OverflowError。
            continue
        end = start + 86400 + 7200      # 多给两小时,夏令时不会漏掉边缘事件
        summary.top_deleted = [
            {
                "path": r["path"] or r["name"],
                "bytes": r["bytes"],
                "is_dir": bool(r["is_dir"]),
                "at": r["timestamp"],
            }
            for r in conn.execute(
                """
                SELECT path, name, bytes, is_dir, timestamp FROM usn_events
                 WHERE drive = ? AND kind = 'delete'
                   AND timestamp >= ? AND timestamp < ?
                 ORDER BY COALESCE(bytes, 0) DESC, timestamp DESC
                 LIMIT ?
                """,
                (drive, start, end, top_n),
            )
        ]

    return [out[day] for day in sorted(out)]


def usn_coverage(conn: sqlite3.Connection, drive: str) -> dict:
    """USN 数据覆盖到哪一天。界面要靠这个说清楚「这段历史是实测的」。"""
    row = conn.execute(
        """
        SELECT COUNT(*) n, MIN(timestamp) lo, MAX(timestamp) hi
          FROM usn_events WHERE drive = ?
        """,
        (drive,),
    ).fetchone()

    n = int(row["n"] or 0)
    if not n:
        return {"events": 0, "first_day": None, "last_day": None, "days": 0}

    first_day = config.safe_day(row["lo"])
    last_day = config.safe_day(row["hi"])
    if first_day is None or last_day is None:
        return {"events": n, "first_day": first_day, "last_day": last_day, "days": 0}
    span = (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days + 1
    return {
        "events": n,
        "first_day": first_day,
        "last_day": last_day,
        "days": span,
    }


def prune_usn_events(conn: sqlite3.Connection, drive: str, *, keep_days: int = 180) -> int:
    """删掉过期事件。日志本身会滚动,库里也没必要无限留。"""
    cutoff = (date.today() - timedelta(days=keep_days))
    cutoff_ts = time.mktime(
        (cutoff.year, cutoff.month, cutoff.day, 0, 0, 0, 0, 0, -1)
    )
    cur = conn.execute(
        "DELETE FROM usn_events WHERE drive = ? AND timestamp < ?", (drive, cutoff_ts)
    )
    conn.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
