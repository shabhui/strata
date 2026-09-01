"""API 处理函数。

每个函数拿连接和参数、返回可直接 JSON 化的字典,不碰 HTTP。
这样 app.py 只管路由和序列化,api 这层能单独测。
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Callable

from .. import config, privileges
from ..analysis import diff as diff_mod
from ..analysis import hotspots, timeline
from ..ntfs.volume import volume_space
from ..scan import changes as changes_mod
from ..store import db


class ApiError(Exception):
    """带 HTTP 状态码的错误。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# `C:` / `C:\` 开头。库里的路径不含盘符,进来的路径要把它掐掉。
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:\\?")


def _drive_param(params: dict[str, list[str]], conn: sqlite3.Connection) -> str:
    raw = (params.get("drive") or [""])[0].strip()
    if not raw:
        known = db.known_drives(conn)
        if known:
            return known[0]
        return config.DEFAULT_DRIVES[0]

    drive = raw.rstrip("\\").rstrip(":").upper() + ":"
    if len(drive) != 2 or not drive[0].isalpha():
        raise ApiError(f"盘符不合法:{raw}")
    return drive


def _int_param(params: dict[str, list[str]], name: str, default: int, *,
               lo: int = 1, hi: int = 100_000) -> int:
    raw = (params.get(name) or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(f"{name} 需要是整数,收到 {raw!r}")
    return max(lo, min(hi, value))


def _snapshot_payload(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "drive": row["drive"],
        "taken_at": row["taken_at"],
        "method": row["method"],
        "total_bytes": row["total_bytes"],
        "free_bytes": row["free_bytes"],
        "used_bytes": row["used_bytes"],
        "scanned_bytes": row["scanned_bytes"],
        "file_count": row["file_count"],
        "dir_count": row["dir_count"],
        "duration_ms": row["duration_ms"],
        "complete": bool(row["complete"]),
        "note": row["note"],
        # 明细精简过没有。note 里那个中文标记不该让前端去解析 —— 判断只在
        # db.is_demoted() 一处,这里出一个布尔量。降级过的快照钻不下去,
        # 树图得据此换一句话说,不能说「还没有数据,先扫一次」。
        "demoted": db.is_demoted(row["note"]),
    }


# ---- 端点 --------------------------------------------------------------------


def get_status(conn: sqlite3.Connection, params: dict) -> dict:
    """总览:每个盘的现状 + 是否已有快照。界面开场就靠这个。"""
    known = db.known_drives(conn)
    drives = list(dict.fromkeys([*known, *config.DEFAULT_DRIVES]))

    out = []
    for drive in drives:
        latest = db.latest_snapshot(conn, drive)
        live_total = live_free = None
        try:
            live_total, live_free = volume_space(drive)
        except OSError:
            pass      # 盘不在了(移动硬盘拔掉),照样把历史数据显示出来

        out.append(
            {
                "drive": drive,
                "live_total_bytes": live_total,
                "live_free_bytes": live_free,
                "live_used_bytes": (live_total - live_free)
                if (live_total is not None and live_free is not None)
                else None,
                "present": live_total is not None,
                "latest_snapshot": _snapshot_payload(latest),
                "snapshot_count": len(db.list_snapshots(conn, drive, limit=10_000)),
                "usn": changes_mod.usn_coverage(conn, drive),
            }
        )

    return {
        "drives": out,
        "privileges": privileges.privilege_state().as_dict(),
        "db_path": str(config.db_path()),
        "db_bytes": db.db_size_bytes(),
        "server_time": time.time(),
    }


def get_timeline(conn: sqlite3.Connection, params: dict) -> dict:
    drive = _drive_param(params, conn)
    days = _int_param(params, "days", 90, lo=1, hi=3650)
    top_n = _int_param(params, "top", 5, lo=1, hi=50)

    changes = timeline.build_timeline(conn, drive, days=days, top_n=top_n)
    summary = timeline.timeline_summary(changes)
    usn_days = {
        s.day: s.as_dict()
        for s in changes_mod.usn_daily_summary(conn, drive, days=days)
    }

    return {
        "drive": drive,
        "days": [
            {
                "day": c.day,
                "added": c.added,
                "removed": c.removed,
                "net": c.net,
                "files_added": c.files_added,
                "basis": c.basis,
                "contributors": [
                    {"path": x.path, "bytes": x.bytes} for x in c.contributors
                ],
                "shrinkers": [
                    {"path": x.path, "bytes": x.bytes} for x in c.shrinkers
                ],
                "usn": usn_days.get(c.day),
            }
            for c in changes
        ],
        "summary": summary,
        "usn_coverage": changes_mod.usn_coverage(conn, drive),
        # 界面必须把这两层的差别讲清楚,不能让人以为回溯值是实测的
        "notes": {
            "retro": "回溯:那天写入、现在还留在盘上的量。删掉的东西看不见,"
                     "所以只增不减,不等于那天的净变化。",
            "measured": "实测:两次扫描的差值,含删除,是真实净增减。",
        },
    }


def _rel_path_param(params: dict, name: str = "path") -> str:
    """取一个盘内相对路径,归一化成库里的口径。

    schema.sql:31 —— 反斜杠、不含盘符、根目录空串。前端曾经把 `C:\\Users\\`
    这种带盘符带尾巴的路径发过来,这里只 strip('\\') 掐不掉盘符,
    get_dir() 于是查不到,整层显示成空的。归一化放在参数入口而不是各个
    查询里:进了这道门之后,底下所有代码都能假定路径是干净的。
    """
    raw = (params.get(name) or [""])[0].replace("/", "\\")
    return _DRIVE_PREFIX.sub("", raw).strip("\\")


def get_tree(conn: sqlite3.Connection, params: dict) -> dict:
    """某个目录的直接子项,给树图和逐层下钻用。"""
    drive = _drive_param(params, conn)
    path = _rel_path_param(params)
    limit = _int_param(params, "limit", 200, lo=1, hi=2000)

    snapshot_id = _int_param(params, "snapshot", 0, lo=0, hi=1 << 40)
    if snapshot_id:
        snap = db.get_snapshot(conn, snapshot_id)
        if snap is None:
            raise ApiError(f"快照 {snapshot_id} 不存在", status=404)
    else:
        snap = db.latest_snapshot(conn, drive)
        if snap is None:
            return {"drive": drive, "path": path, "children": [], "snapshot": None}
        snapshot_id = int(snap["id"])

    node = db.get_dir(conn, snapshot_id, path)
    children = db.children_of(conn, snapshot_id, path, limit=limit)

    return {
        "drive": drive,
        "path": path,
        "snapshot": _snapshot_payload(snap),
        "node": {
            "path": path,
            "bytes": node["bytes"] if node else None,
            # 本目录直属文件的字节。没有它,前端就只能拿子目录之和当本层总量,
            # 直属文件那部分在树图上是个洞 —— 盘根最明显:pagefile.sys 和
            # hiberfil.sys 都直挂在根下,真机上加起来常有 20 GB。
            "own_bytes": node["own_bytes"] if node else None,
            "files": node["files"] if node else None,
            "dirs": node["dirs"] if node else None,
            "newest_mtime": node["newest_mtime"] if node else None,
            "newest_ctime": node["newest_ctime"] if node else None,
            "folded_children": node["folded_children"] if node else 0,
            "folded_bytes": node["folded_bytes"] if node else 0,
        }
        if node
        else None,
        "children": [
            {
                "path": r["path"],
                "name": r["path"].rsplit("\\", 1)[-1] if r["path"] else drive,
                "bytes": r["bytes"],
                "own_bytes": r["own_bytes"],
                "files": r["files"],
                "dirs": r["dirs"],
                "newest_mtime": r["newest_mtime"],
                "newest_ctime": r["newest_ctime"],
                "folded_children": r["folded_children"],
                "folded_bytes": r["folded_bytes"],
            }
            for r in children
        ],
    }


def get_hotspots(conn: sqlite3.Connection, params: dict) -> dict:
    drive = _drive_param(params, conn)
    limit = _int_param(params, "limit", 30, lo=1, hi=200)
    snap = db.latest_snapshot(conn, drive)
    if snap is None:
        return {"drive": drive, "snapshot": None, "dirs": [], "files": [],
                "recent": [], "cleanup": [], "age_profile": []}

    sid = int(snap["id"])
    return {
        "drive": drive,
        "snapshot": _snapshot_payload(snap),
        "dirs": [h.as_dict() for h in hotspots.biggest_dirs(conn, sid, limit=limit)],
        "files": [h.as_dict() for h in hotspots.biggest_files(conn, sid, limit=limit)],
        "recent": [g.as_dict() for g in hotspots.recently_grown(conn, sid, limit=limit)],
        "cleanup": [h.as_dict() for h in hotspots.cleanup_candidates(conn, sid, limit=limit)],
        "age_profile": hotspots.age_profile(conn, sid),
    }


def get_diff(conn: sqlite3.Connection, params: dict) -> dict:
    drive = _drive_param(params, conn)
    limit = _int_param(params, "limit", 40, lo=1, hi=500)

    before = _int_param(params, "before", 0, lo=0, hi=1 << 40)
    after = _int_param(params, "after", 0, lo=0, hi=1 << 40)
    if before and after:
        result = diff_mod.diff_snapshots(conn, before, after)
    else:
        back = _int_param(params, "back", 1, lo=1, hi=1000)
        result = diff_mod.diff_latest(conn, drive, back=back)

    if result is None:
        return {
            "drive": drive,
            "available": False,
            "reason": "至少要两次扫描才能对比。第一次扫描后请等下一次快照。",
        }
    payload = result.as_dict(limit=limit)
    payload["available"] = True
    return payload


def get_snapshots(conn: sqlite3.Connection, params: dict) -> dict:
    drive = _drive_param(params, conn)
    limit = _int_param(params, "limit", 60, lo=1, hi=2000)
    return {
        "drive": drive,
        "snapshots": [
            _snapshot_payload(r) for r in db.list_snapshots(conn, drive, limit=limit)
        ],
    }


def get_changes(conn: sqlite3.Connection, params: dict) -> dict:
    """USN 变更明细。这里是唯一能看到「删了什么」的地方。"""
    drive = _drive_param(params, conn)
    days = _int_param(params, "days", 30, lo=1, hi=3650)
    limit = _int_param(params, "limit", 200, lo=1, hi=5000)
    kind = (params.get("kind") or ["delete"])[0].strip() or "delete"

    cutoff = time.time() - days * 86400
    # 一个文件一行。USN 记的是操作不是文件:一次「建-写-关-删」留下 5 条记录,
    # reason 位一路累加(0x100 → 0x102 → 0x103 → 0x80000103 → 0x80000200),
    # 每条有各自的 USN,所以入库时的 ON CONFLICT(drive, usn) 挡不住 ——
    # 它们不是重复行,是同一个文件的不同时刻。
    #
    # 不合并的后果很集中:排序按字节数,而同一个文件的几条记录字节数一样,
    # 于是它们连着占榜首。实盘上第一名和第二名就是同一个 32.2 MB 的压缩包。
    #
    # 只能按路径合并,不能按文件名。实盘上 `Editor` 出现 786 次、`Assets` 465 次,
    # 那是几百个不同目录里的同名文件 —— 按名字合并等于凭空说「它被删了 786 次」。
    # 所以路径为空的行按 usn 各自成组,一行都不并。
    #
    # bytes 取 MAX 而不是随便挑一条:USN 日志不记大小,这个值是从历史快照反查的,
    # 几条记录里可能只有一条补到了,挑错了就白丢。
    #
    # kind / is_dir / name 直接裸取:kind 被 WHERE 定死,path 是分组键,而 name
    # 是路径的末段、is_dir 是同一个文件的属性 —— 组内本来就只有一个值。
    #
    # 排序里夹了一档「有没有路径」。没路径的那种行上什么都没有:没路径、没大小
    # (大小是按路径去历史快照反查的,没路径必然反查不到)、右键也定位不了 ——
    # 可它照样占一个名额。实盘上量到:库里有 405 条带路径的删除记录,界面一次放
    # 200 行,其中 115 行给了这种空行,用户能用的只剩 85 行。
    #
    # 之所以会混进来,是因为实盘上只有 4 行反查到了大小,剩下全靠 timestamp 排,
    # 有路径的和没路径的就交替着来。加这一档,limit 切掉的就是最没用的那些。
    # 不删它们:「有个叫这名字的东西被删了」也是信息,删掉等于替用户断言没发生过。
    rows = conn.execute(
        """
        SELECT MAX(usn) usn, MAX(timestamp) timestamp, kind, is_dir, name, path,
               MAX(bytes) bytes, COUNT(*) n
          FROM usn_events
         WHERE drive = ? AND timestamp >= ? AND kind = ?
         GROUP BY path, CASE WHEN path IS NULL OR path = '' THEN usn ELSE 0 END
         ORDER BY COALESCE(MAX(bytes), 0) DESC,
                  (path IS NOT NULL AND path <> '') DESC,
                  MAX(timestamp) DESC
         LIMIT ?
        """,
        (drive, cutoff, kind, limit),
    )
    return {
        "drive": drive,
        "kind": kind,
        "coverage": changes_mod.usn_coverage(conn, drive),
        "events": [
            {
                "usn": r["usn"],
                "at": r["timestamp"],
                "kind": r["kind"],
                "is_dir": bool(r["is_dir"]),
                "name": r["name"],
                "path": r["path"],
                "bytes": r["bytes"],
                # 折叠掉了几条。>1 说明这文件在窗口里反复出现,界面标出来 ——
                # 「同一个临时文件被删了 115 次」本身是有用的信息,不能默默吞掉。
                "count": r["n"],
            }
            for r in rows
        ],
        # 这里原来带一条中文说明。它是一句固定的话,没有后端才知道的参数,
        # 所以现在由前端自己出(web/i18n.js 的 del.sizeNote)—— 从后端送
        # 中文过来的话,英文界面上这一句会是唯一的中文。
    }


ROUTES: dict[str, Callable[[sqlite3.Connection, dict], Any]] = {
    "/api/status": get_status,
    "/api/timeline": get_timeline,
    "/api/tree": get_tree,
    "/api/hotspots": get_hotspots,
    "/api/diff": get_diff,
    "/api/snapshots": get_snapshots,
    "/api/changes": get_changes,
}
