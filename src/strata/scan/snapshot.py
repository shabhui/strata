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
    # {目录编号 → 相对路径},给 changes.collect_usn 还原删除文件的路径用。
    #
    # 这个字段以前不存在,而 server/app.py:99 和 __main__.py:81 两处都写着
    # `getattr(result, "dir_paths", None)` —— 于是两条路拿到的都恒等于 None,
    # 每条 USN 事件的 path 存成 NULL,enrich_deleted_sizes 又要求
    # `path IS NOT NULL` 才填大小,「消失了什么」面板就只剩裸文件名。
    # 库里 84,303 条事件全是 NULL,一条都没例外。
    #
    # 参数被文档写过、被 test_changes.py 拿 {7: "Games\\Steam"} 测过、
    # 就是从来没接上真货。这和「只会通过的检查」是同一类事:看着有,实际没有。
    dir_paths: dict[int, str] = field(default_factory=dict)

    def summary(self) -> str:
        gib = 2**30
        return (
            f"{self.drive} 用 {self.method} 扫描:{self.file_count:,} 文件 / "
            f"{self.dir_count:,} 目录 / {self.scanned_bytes / gib:,.1f} GiB,"
            f"耗时 {self.duration_ms / 1000:.1f}s"
        )


def _reparse_warning(count: int) -> list[str]:
    """联接点/符号链接的说明。两种扫描方法都要用,措辞得一样。

    这一条不是"出错了",是"这个数字为什么长这样":联接点自己算 0 字节,里面的
    东西算在目标路径上。不说的话,树图里 runtime-sandbox\\abdata 显示 0,资源
    管理器点进去却是 23 GB —— 看的人只能得出"这工具算不准"。

    跟进才是错的:D: 上那两个 23.35 GB 的联接点都指向已经数过的 Koikatu\\abdata,
    跟进就是把同一批字节数三遍,盘还会看起来比实际大 47 GB。
    """
    if not count:
        return []
    return [
        f"{count:,} 个联接点/符号链接没有跟进(显示为 0 字节,"
        f"真实体积算在目标路径上;跟进会重复计数)"
    ]


def _mft_to_scan_entries(
    entries: list[mft.FileEntry],
    *,
    dir_paths: dict[int, str] | None = None,
) -> tuple[list[tree.ScanEntry], int, list[str]]:
    """MFT 条目 → ScanEntry,顺带还原文件路径。

    父目录解析不出来的条目会被丢掉并计数 —— 这些字节确实无法归属,
    与其编一个假路径,不如如实报告有多少没归到位。

    dir_paths 是出参。这条路上它是白捡的:resolve_paths() 返回的就是
    {记录号 → 目录路径},本来算完就扔。而 MFT 记录号和 USN 的
    parent_reference 是同一个东西(usn.py:293 把序列号掩掉之后),
    所以直接对得上,不用像 scandir 那条路那样另花系统调用去取编号。
    """
    paths, pstats = mft.resolve_paths(entries)
    if dir_paths is not None:
        dir_paths.update(paths)
    out: list[tree.ScanEntry] = []
    orphaned_bytes = 0
    metafile_bytes = 0
    warnings: list[str] = []

    for e in entries:
        # NTFS 元文件($MFT、$LogFile、$BadClus……记录号 < 16)不计入。
        #
        # 不是嫌它们没用,是其中 $BadClus:$Bad 会把整块盘的容量报成一个文件:
        # 它是稀疏流,allocated_size 按定义就等于整卷容量。实测本机 C: 因此把
        # 169 GB 的盘说成用了 397 GB,D: 把 638 GB 说成 1390 GB —— 两个盘多出来的
        # 都正好是一整卷(1.041 和 1.030 倍,零头是 $MFT 自己)。
        #
        # 而且这些字节还看不见:元文件全挂在盘根下,prune_tree() 不写根节点
        # 那一行,所以总数里有、树里查无此人,总数和树永远差一卷。
        #
        # 目录形态的元文件($Extend,记录号 11)留着 —— 它下面 $RmMetadata 之类
        # 是真实占用,而且 resolve_paths() 已经在完整列表上跑完了,这里过滤不会
        # 让它的子项失去父链。
        if e.is_metafile and not e.is_dir:
            metafile_bytes += e.bytes
            continue

        if e.is_dir:
            # 根目录自身不作为条目
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
    # MFT 这边不存在"跟不跟进"的选择 —— 记录是平的,每个文件只有一条父链,
    # 联接点里的东西本来就只会落在目标路径下。但看的人面对的是同一个 0 字节
    # 目录,所以话也得说一样。
    warnings.extend(
        _reparse_warning(sum(1 for e in entries if e.is_reparse))
    )
    if orphaned_bytes:
        warnings.append(f"{orphaned_bytes / 2**30:.2f} GiB 无法归属到路径")
    # 排掉的量要说出来,不然「为什么和资源管理器差这么多」没法回答。
    # 这个数会很大(含一整卷的 $BadClus),说清它是什么,别让人以为是丢了数据。
    if metafile_bytes:
        warnings.append(
            f"NTFS 元文件未计入(共 {metafile_bytes / 2**30:.2f} GiB,"
            f"其中 $BadClus 是稀疏流,按定义等于整卷容量,不是真实占用)"
        )

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


def _post_scan_maintenance(conn: sqlite3.Connection) -> None:
    """扫描落库之后的收尾。必须在事务外调用。

    两件事都不影响数据正确性,只影响体积和速度,所以失败一律吞掉 —— 一次已经
    成功的扫描不该因为收尾出错而变成失败。

    1. 回收空间:降级和清理每次扫描都在删行,而 SQLite 删行只把页挂到 freelist,
       文件不会缩。一个用来省硬盘的工具自己泄漏硬盘最说不过去。
    2. 刷新统计信息:数据形状变了,规划器手里的旧行数会让它选错索引。实测根视图
       那条查询没统计信息时 115 ms,有统计信息 0.07 ms。

    VACUUM 和 ANALYZE 都不能出现在事务里。
    """
    for step in (db.maybe_vacuum, db.refresh_stats):
        try:
            step(conn)
        except sqlite3.Error:
            pass


def collect_entries(
    drive: str,
    *,
    prefer_mft: bool | None = None,
    progress: ProgressFn | None = None,
    dir_paths: dict[int, str] | None = None,
) -> tuple[list[tree.ScanEntry], str, list[str], str | None]:
    """采集条目。返回 (条目, 方法, 警告, 退化原因)。

    prefer_mft 传 None 表示用 config.PREFER_MFT(那边写了为什么默认是关的)。
    显式传 True/False 的照旧,测试和命令行都靠这个。

    dir_paths 是出参,只有 MFT 那一支会填 —— 那边 resolve_paths() 本来就算出
    {记录号 → 目录路径},算完扔掉,接上不花钱。scandir 那一支填不了
    (取编号太贵,见那边的注释),USN 靠 changes._resolve_by_id 兜。

    还是 4 元组:8 处调用和一处写死了 `return_value=(entries, "mft", [], None)`
    的 mock 都按 4 个解包,加第 5 个返回值会连 mock 一起炸。
    """
    warnings: list[str] = []
    if prefer_mft is None:
        prefer_mft = config.PREFER_MFT

    if prefer_mft:
        try:
            with Volume(drive) as vol:
                reader = mft.MftReader(vol)
                if progress is not None:
                    reader_progress = lambda n: progress("mft", n)  # noqa: E731
                else:
                    reader_progress = None
                raw = reader.read_entries(progress=reader_progress)
            entries, _orphan_bytes, warns = _mft_to_scan_entries(
                raw, dir_paths=dir_paths
            )
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
        reason = "按配置跳过 MFT,直接用目录遍历(见 config.PREFER_MFT)"

    # 退回 scandir。这条路填不了 dir_paths —— 遍历时逐个取目录编号太贵
    # (整块 C: 上 8 线程实测 68s → 165s),USN 那边改成读完日志再拿父引用
    # 反查,见 changes._resolve_by_id。
    raw_walk, wstats = walker.walk_drive(
        drive,
        progress=(lambda n: progress("scandir", n)) if progress else None,
    )
    if wstats.errors:
        warnings.append(f"{wstats.errors:,} 个目录/文件读取被拒(已跳过)")
    warnings.extend(_reparse_warning(wstats.skipped_reparse))
    warnings.append("用目录遍历代替 MFT:硬链接会重复计数,大小是逻辑大小而非占盘大小")
    return _walk_to_scan_entries(raw_walk), "scandir", warnings, reason


def scan_drive(
    conn: sqlite3.Connection,
    drive: str,
    *,
    prefer_mft: bool | None = None,
    progress: ProgressFn | None = None,
    now: float | None = None,
) -> ScanResult:
    """扫描一个盘并写入一个快照。prefer_mft=None 时用 config.PREFER_MFT。"""
    started = time.perf_counter()
    taken_at = time.time() if now is None else now
    total_bytes, free_bytes = volume_space(drive)
    used_bytes = total_bytes - free_bytes

    dir_paths: dict[int, str] = {}
    entries, method, warnings, fallback_reason = collect_entries(
        drive, prefer_mft=prefer_mft, progress=progress, dir_paths=dir_paths
    )

    nodes, scanned_bytes, file_count = tree.build_tree(entries)
    dir_rows = tree.prune_tree(nodes)
    bucket_rows = tree.build_buckets(entries)
    file_rows = tree.select_files(entries, now=taken_at)
    dir_count = sum(1 for e in entries if e.is_dir)

    # 退化原因也要落库,不能只放在返回值里。退回目录遍历意味着这份数据的口径
    # 变了(硬链接重复计数、算的是逻辑大小),而这个前提在快照活着的整段时间里
    # 都成立 —— 原来它只在扫描那一刻显示,刷新页面就没了,后来看这份数据的人
    # 不知道数字是怎么来的。note 已经是自由文本,也已经透给了前端。
    # 原因排在警告前面:先说为什么退,再说退了之后数字有什么变化。
    note_parts = ([fallback_reason] if fallback_reason else []) + warnings

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
        note="; ".join(note_parts) if note_parts else None,
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
    _post_scan_maintenance(conn)

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
        dir_paths=dir_paths,
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

    _post_scan_maintenance(conn)

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
        # scan_directory 走 scandir,拿不到目录编号,所以这里恒空。
        # 留着字段而不是省掉:字段不在的话 getattr 的默认值会恒生效,
        # 而那正是之前那个 bug —— 「压根没这个东西」和「有但这次是空的」
        # 在调用方眼里长得一样,后者是状态,前者是 bug。
    )
