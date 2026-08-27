"""两个快照的对比:什么涨了、什么缩了、什么出现了、什么消失了。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

GREW = "grew"
SHRANK = "shrank"
APPEARED = "appeared"
VANISHED = "vanished"


@dataclass(slots=True)
class Delta:
    path: str
    before: int
    after: int
    kind: str

    @property
    def delta(self) -> int:
        return self.after - self.before

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
            "kind": self.kind,
        }


@dataclass(slots=True)
class SnapshotDiff:
    drive: str
    before_id: int
    after_id: int
    before_at: float
    after_at: float
    before_bytes: int
    after_bytes: int
    dir_deltas: list[Delta] = field(default_factory=list)
    file_deltas: list[Delta] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    @property
    def net(self) -> int:
        return self.after_bytes - self.before_bytes

    @property
    def grew(self) -> list[Delta]:
        return [d for d in self.dir_deltas if d.delta > 0]

    @property
    def shrank(self) -> list[Delta]:
        return [d for d in self.dir_deltas if d.delta < 0]

    def as_dict(self, limit: int = 40) -> dict:
        return {
            "drive": self.drive,
            "before_id": self.before_id,
            "after_id": self.after_id,
            "before_at": self.before_at,
            "after_at": self.after_at,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "net": self.net,
            "grew": [d.as_dict() for d in self.grew[:limit]],
            "shrank": [d.as_dict() for d in self.shrank[:limit]],
            "files": [d.as_dict() for d in self.file_deltas[:limit]],
            "caveats": self.caveats,
        }


def _dirs(conn: sqlite3.Connection, snapshot_id: int, max_depth: int) -> dict[str, int]:
    return {
        r["path"]: r["bytes"]
        for r in conn.execute(
            "SELECT path, bytes FROM dirs WHERE snapshot_id = ? AND depth <= ?",
            (snapshot_id, max_depth),
        )
    }


def _files(conn: sqlite3.Connection, snapshot_id: int) -> dict[str, int]:
    return {
        r["path"]: r["bytes"]
        for r in conn.execute(
            "SELECT path, bytes FROM files WHERE snapshot_id = ?", (snapshot_id,)
        )
    }


def _classify(before: int | None, after: int | None) -> str:
    if before is None:
        return APPEARED
    if after is None:
        return VANISHED
    return GREW if (after or 0) > before else SHRANK


def _deltas(before: dict[str, int], after: dict[str, int], *, min_bytes: int) -> list[Delta]:
    out: list[Delta] = []
    for path in set(before) | set(after):
        b = before.get(path)
        a = after.get(path)
        change = (a or 0) - (b or 0)
        if abs(change) < min_bytes:
            continue
        out.append(
            Delta(path=path, before=b or 0, after=a or 0, kind=_classify(b, a))
        )
    out.sort(key=lambda d: abs(d.delta), reverse=True)
    return out


def diff_snapshots(
    conn: sqlite3.Connection,
    before_id: int,
    after_id: int,
    *,
    max_depth: int = 4,
    min_bytes: int = 1024 * 1024,
) -> SnapshotDiff:
    """对比两个快照。

    深度限制是必要的:旧快照被降级后只留浅层目录,
    比较深层路径会把「被裁掉」误报成「被删掉」。
    """
    from ..store import db

    before = db.get_snapshot(conn, before_id)
    after = db.get_snapshot(conn, after_id)
    if before is None or after is None:
        raise ValueError(f"快照不存在: {before_id} 或 {after_id}")
    if before["drive"] != after["drive"]:
        raise ValueError(
            f"不能跨盘对比: {before['drive']} 与 {after['drive']}"
        )
    if float(before["taken_at"]) > float(after["taken_at"]):
        before, after = after, before
        before_id, after_id = after_id, before_id

    caveats: list[str] = []
    demoted = [
        row["id"]
        for row in (before, after)
        if "[已降级]" in (row["note"] or "")
    ]
    if demoted:
        caveats.append(
            f"参与对比的快照中有 {len(demoted)} 个已降级为粗粒度,"
            f"深度超过 3 层且小于 64 MB 的目录不参与比较"
        )
    if before["method"] != after["method"]:
        caveats.append(
            f"两次扫描方式不同({before['method']} vs {after['method']}),"
            f"硬链接与占盘大小的算法有差异,增减值仅供参考"
        )

    dir_deltas = _deltas(
        _dirs(conn, before_id, max_depth),
        _dirs(conn, after_id, max_depth),
        min_bytes=min_bytes,
    )

    before_files = _files(conn, before_id)
    after_files = _files(conn, after_id)
    if not before_files and not after_files:
        file_deltas: list[Delta] = []
        caveats.append("两个快照都没有文件明细(已降级),只能对比目录")
    elif not before_files or not after_files:
        file_deltas = []
        caveats.append("其中一个快照的文件明细已被降级清除,跳过文件级对比")
    else:
        file_deltas = _deltas(before_files, after_files, min_bytes=min_bytes)
        caveats.append(
            "文件明细只收录超过阈值的文件,"
            "「新出现」也可能是原本就在、只是这次才超过阈值"
        )

    return SnapshotDiff(
        drive=before["drive"],
        before_id=before_id,
        after_id=after_id,
        before_at=float(before["taken_at"]),
        after_at=float(after["taken_at"]),
        before_bytes=int(before["scanned_bytes"]),
        after_bytes=int(after["scanned_bytes"]),
        dir_deltas=dir_deltas,
        file_deltas=file_deltas,
        caveats=caveats,
    )


def diff_latest(
    conn: sqlite3.Connection, drive: str, *, back: int = 1, **kwargs
) -> SnapshotDiff | None:
    """把最新快照与往前数第 back 个快照对比。快照不够时返回 None。"""
    rows = list(
        conn.execute(
            """
            SELECT id FROM snapshots
             WHERE drive = ? AND complete = 1
             ORDER BY taken_at DESC LIMIT ?
            """,
            (drive, back + 1),
        )
    )
    if len(rows) < back + 1:
        return None
    return diff_snapshots(conn, int(rows[back]["id"]), int(rows[0]["id"]), **kwargs)
