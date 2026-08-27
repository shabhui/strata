"""扫描编排:MFT 优先,失败退 scandir,结果写入数据库。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import config
from ..ntfs import mft
from ..ntfs.volume import AccessDenied, NtfsError, Volume, volume_space
from ..store import db
from . import tree, walker

ProgressFn = Callable[[str, int], None]


@dataclass(slots=True)
class ScanResult:
    drive: str
    method: str
    snapshot_id: int | None
    total_bytes: int
    free_bytes: int
    used_bytes: int
    scanned_bytes: int
    file_count: int
    dir_count: int
    duration_ms: int
    dir_rows: int = 0
    file_rows: int = 0
    bucket_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    fallback_reason: str | None = None

    def summary(self) -> str:
        gib = 2**30
        return (
            f"{self.drive} 用 {self.method} 扫描:{self.file_count:,} 文件 / "
            f"{self.dir_count:,} 目录 / {self.scanned_bytes / gib:,.1f} GiB,"
            f"耗时 {self.duration_ms / 1000:.1f}s"
        )


def _mft_to_scan_entries(
    entries: list[mft.FileEntry],
) -> tuple[list[tree.ScanEntry], int, list[str]]:
    """MFT 条目 → ScanEntry,顺带还原文件路径。

    父目录解析不出来的条目会被丢掉并计数 —— 这些字节确实无法归属,
    与其编一个假路径,不如如实报告有多少没归到位。
    """
    paths, pstats = mft.resolve_paths(entries)
    out: list[tree.ScanEntry] = []
    orphaned_bytes = 0
    warnings: list[str] = []

    for e in entries:
        if e.is_dir:
            # 根目录自身不作为条目;元文件目录也跳过
            if e.record == mft.ROOT_RECORD:
                continue
            path = paths.get(e.record)
            if path is None or path == "":
                orphaned_bytes += e.bytes
                continue
            out.append(
                tree.ScanEntry(
                    path=path,
                    is_dir=True,
                    bytes=0,
                    created=e.created,
                    modified=e.modified,
                    attributes=e.attributes,
                )
            )
        else:
            parent_path = paths.get(e.parent)
            if parent_path is None:
                orphaned_bytes += e.bytes
                continue
            path = f"{parent_path}\\{e.name}" if parent_path else e.name
            out.append(
                tree.ScanEntry(
                    path=path,
                    is_dir=False,
                    bytes=e.bytes,
                    created=e.created,
                    modified=e.modified,
                    attributes=e.attributes,
                )
            )

    if pstats.orphaned or pstats.cycles:
        warnings.append(
            f"{pstats.orphaned:,} 个目录父链断裂,{pstats.cycles:,} 个成环"
        )
    if orphaned_bytes:
        warnings.append(f"{orphaned_bytes / 2**30:.2f} GiB 无法归属到路径")

    return out, orphaned_bytes, warnings


def _walk_to_scan_entries(entries: list[walker.WalkEntry]) -> list[tree.ScanEntry]:
    return [
        tree.ScanEntry(
            path=e.path,
            is_dir=e.is_dir,
            bytes=e.bytes,
            created=e.created,
            modified=e.modified,
            attributes=e.attributes,
        )
        for e in entries
    ]


def collect_entries(
    drive: str,
    *,
    prefer_mft: bool = True,
    progress: ProgressFn | None = None,
) -> tuple[list[tree.ScanEntry], str, list[str], str | None]:
    """采集条目。返回 (条目, 方法, 警告, 退化原因)。"""
    warnings: list[str] = []

    if prefer_mft:
        try:
            with Volume(drive) as vol:
                reader = mft.MftReader(vol)
                if progress is not None:
                    reader_progress = lambda n: progress("mft", n)  # noqa: E731
                else:
                    reader_progress = None
                raw = reader.read_entries(progress=reader_progress)
            entries, _orphan_bytes, warns = _mft_to_scan_entries(raw)
            warnings.extend(warns)
            st = reader.stats
            if st.fixup_failures or st.parse_failures:
                warnings.append(
                    f"{st.fixup_failures:,} 条记录 fixup 校验失败,"
                    f"{st.parse_failures:,} 条解析失败(已跳过)"
                )
            return entries, "mft", warnings, None
        except AccessDenied as exc:
            reason = f"没有管理员权限,无法直读 MFT({exc})"
        except NtfsError as exc:
            reason = f"MFT 读取失败:{exc}"
        except OSError as exc:
            reason = f"打开卷失败:{exc}"
    else:
        reason = "调用方指定跳过 MFT"

    # 退回 scandir
    raw_walk, wstats = walker.walk_drive(
        drive, progress=(lambda n: progress("scandir", n)) if progress else None
    )
    if wstats.errors:
        warnings.append(f"{wstats.errors:,} 个目录/文件读取被拒(已跳过)")
    warnings.append("用目录遍历代替 MFT:硬链接会重复计数,大小是逻辑大小而非占盘大小")
    return _walk_to_scan_entries(raw_walk), "scandir", warnings, reason


def scan_drive(
    conn: sqlite3.Connection,
    drive: str,
    *,
    prefer_mft: bool = True,
    progress: ProgressFn | None = None,
    now: float | None = None,
) -> ScanResult:
    """扫描一个盘并写入一个快照。"""
    started = time.perf_counter()
    taken_at = time.time() if now is None else now
    total_bytes, free_bytes = volume_space(drive)
    used_bytes = total_bytes - free_bytes

    entries, method, warnings, fallback_reason = collect_entries(
        drive, prefer_mft=prefer_mft, progress=progress
    )

    nodes, scanned_bytes, file_count = tree.build_tree(entries)
    dir_rows = tree.prune_tree(nodes)
    bucket_rows = tree.build_buckets(entries)
    file_rows = tree.select_files(entries, now=taken_at)
    dir_count = sum(1 for e in entries if e.is_dir)

    snap = db.Snapshot(
        drive=drive,
        taken_at=taken_at,
        method=method,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        used_bytes=used_bytes,
        scanned_bytes=scanned_bytes,
        file_count=file_count,
        dir_count=dir_count,
        complete=False,
        note="; ".join(warnings) if warnings else None,
    )

    conn.execute("BEGIN")
    try:
        db.insert_snapshot(conn, snap)
        db.insert_dirs(conn, snap.id, dir_rows)
        db.insert_files(conn, snap.id, file_rows)
        db.insert_buckets(conn, snap.id, bucket_rows)

        snap.complete = True
        snap.duration_ms = int((time.perf_counter() - started) * 1000)
        db.update_snapshot_totals(conn, snap)

        # 年龄分布只对最新快照有意义;旧快照降级成粗粒度
        db.prune_old_buckets(conn, drive, keep_snapshot_id=snap.id)
        db.demote_previous_snapshots(conn, drive, keep_snapshot_id=snap.id)
        db.prune_snapshots(conn, drive)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # 降级和清理每次扫描都在删行，而 SQLite 删行只把页挂到 freelist，文件不会缩。
    # 一个用来省硬盘的工具自己泄漏硬盘最说不过去。
    # 必须在 COMMIT 之后：VACUUM 不能出现在事务里。
    try:
        db.maybe_vacuum(conn)
    except sqlite3.Error:
        # 回收失败不该让一次已经成功的扫描变成失败。
        pass

    return ScanResult(
        drive=drive,
        method=method,
        snapshot_id=snap.id,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        used_bytes=used_bytes,
        scanned_bytes=scanned_bytes,
        file_count=file_count,
        dir_count=dir_count,
        duration_ms=snap.duration_ms,
        dir_rows=len(dir_rows),
        file_rows=len(file_rows),
        bucket_rows=len(bucket_rows),
        warnings=warnings,
        fallback_reason=fallback_reason,
    )


def scan_directory(
    conn: sqlite3.Connection,
    root: str,
    *,
    label: str | None = None,
    now: float | None = None,
) -> ScanResult:
    """扫描任意目录(测试用,也可以给用户扫单个文件夹)。

    走 scandir 路径,不需要提权。label 用作快照的 drive 字段。
    """
    started = time.perf_counter()
    taken_at = time.time() if now is None else now
    drive_label = label or root

    raw_walk, wstats = walker.walk_drive(root)
    entries = _walk_to_scan_entries(raw_walk)

    nodes, scanned_bytes, file_count = tree.build_tree(entries)
    dir_rows = tree.prune_tree(nodes, min_bytes=0, max_depth=99)
    bucket_rows = tree.build_buckets(entries, min_bytes=0)
    file_rows = tree.select_files(entries, now=taken_at, min_bytes=0, recent_min_bytes=0)

    snap = db.Snapshot(
        drive=drive_label,
        taken_at=taken_at,
        method="scandir",
        total_bytes=scanned_bytes,
        free_bytes=0,
        used_bytes=scanned_bytes,
        scanned_bytes=scanned_bytes,
        file_count=file_count,
        dir_count=wstats.dirs,
        complete=False,
    )

    conn.execute("BEGIN")
    try:
        db.insert_snapshot(conn, snap)
        db.insert_dirs(conn, snap.id, dir_rows)
        db.insert_files(conn, snap.id, file_rows)
        db.insert_buckets(conn, snap.id, bucket_rows)
        snap.complete = True
        snap.duration_ms = int((time.perf_counter() - started) * 1000)
        db.update_snapshot_totals(conn, snap)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return ScanResult(
        drive=drive_label,
        method="scandir",
        snapshot_id=snap.id,
        total_bytes=scanned_bytes,
        free_bytes=0,
        used_bytes=scanned_bytes,
        scanned_bytes=scanned_bytes,
        file_count=file_count,
        dir_count=wstats.dirs,
        duration_ms=snap.duration_ms,
        dir_rows=len(dir_rows),
        file_rows=len(file_rows),
        bucket_rows=len(bucket_rows),
    )
