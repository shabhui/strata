"""吃空间的大户、最近长胖的目录、可清理的候选。

「可清理」只做识别和标注,不删任何东西 —— 判断由人来做。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .. import config
from .paths import has_ancestor_in

# 可清理候选:路径片段 → (标签, 说明, 安全等级)
# 安全等级: 'safe' 删了只会丢缓存;'review' 可能有用,要看一眼;
#           'careful' 删了可能影响功能
CLEANUP_RULES: tuple[tuple[str, str, str, str], ...] = (
    (r"windows\installer", "Windows 安装缓存", "卸载和修复程序时用到,删了某些软件无法卸载", "careful"),
    (r"windows\winsxs", "组件存储", "系统组件的唯一副本,不能手删,只能用 DISM 清理", "careful"),
    (r"windows\softwaredistribution\download", "更新下载缓存", "已安装的更新包,可以删", "safe"),
    (r"windows\temp", "系统临时文件", "可以删", "safe"),
    (r"$recycle.bin", "回收站", "确认里面没有要恢复的东西再清空", "review"),
    (r"appdata\local\temp", "用户临时文件", "可以删,占用往往不小", "safe"),
    (r"appdata\local\packages", "应用商店应用数据", "含应用的缓存和存档,逐个看", "review"),
    (r"appdata\local\microsoft\windows\explorer", "缩略图缓存", "可以删,会自动重建", "safe"),
    (r"appdata\local\pip\cache", "pip 缓存", "可以删,重装包时会重新下载", "safe"),
    (r"appdata\local\npm-cache", "npm 缓存", "可以删", "safe"),
    (r"appdata\roaming\npm-cache", "npm 缓存", "可以删", "safe"),
    (r"appdata\local\yarn\cache", "yarn 缓存", "可以删", "safe"),
    (r".cache", "工具缓存目录", "多数可以删,会自动重建", "safe"),
    (r".gradle\caches", "Gradle 缓存", "可以删,下次构建重新下载", "safe"),
    (r".m2\repository", "Maven 本地仓库", "可以删,下次构建重新下载", "review"),
    (r".nuget\packages", "NuGet 缓存", "可以删", "safe"),
    (r"node_modules", "Node 依赖", "项目可重装依赖,长期不动的项目值得清", "review"),
    (r"__pycache__", "Python 字节码缓存", "可以删,会自动重建", "safe"),
    (r"\target\debug", "Rust 调试产物", "可以删,重新编译即可", "safe"),
    (r"\target\release", "Rust 发布产物", "可以删,重新编译即可", "safe"),
    (r"steamapps\downloading", "Steam 下载暂存", "中断的下载残留,可以删", "safe"),
    (r"steamapps\shadercache", "Steam 着色器缓存", "可以删,会自动重建", "safe"),
    (r"steamapps\workshop", "Steam 创意工坊", "订阅的模组内容,按需取舍", "review"),
    (r"nvidia corporation\downloader", "NVIDIA 驱动下载缓存", "可以删", "safe"),
    (r"crashdumps", "崩溃转储", "排查过就可以删", "safe"),
    (r"windows\minidump", "蓝屏小转储", "排查过就可以删", "safe"),
    (r"hiberfil.sys", "休眠文件", "关掉休眠可释放,等同于内存大小", "careful"),
    (r"pagefile.sys", "虚拟内存页面文件", "由系统管理,不要手删", "careful"),
    (r"swapfile.sys", "交换文件", "由系统管理,不要手删", "careful"),
)


@dataclass(slots=True)
class Hotspot:
    path: str
    bytes: int
    files: int
    newest: float | None = None
    label: str | None = None
    advice: str | None = None
    safety: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "files": self.files,
            "newest": self.newest,
            "label": self.label,
            "advice": self.advice,
            "safety": self.safety,
        }


@dataclass(slots=True)
class GrowthSpot:
    path: str
    bytes: int
    newest: float | None
    days_old: float | None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "newest": self.newest,
            "days_old": self.days_old,
        }


def classify_path(path: str) -> tuple[str, str, str] | None:
    """路径命中清理规则时返回 (标签, 说明, 安全等级)。"""
    lowered = path.lower()
    for needle, label, advice, safety in CLEANUP_RULES:
        if needle in lowered:
            return label, advice, safety
    return None


def biggest_dirs(
    conn: sqlite3.Connection, snapshot_id: int, *, limit: int = 30, max_depth: int = 6
) -> list[Hotspot]:
    """占用最大的目录。

    按深度限制并排除祖先,否则榜首永远是 `Users` 套 `Users\\alice` 套下去,
    看不出真正的大户在哪。
    """
    rows = list(
        conn.execute(
            """
            SELECT path, bytes, files, newest_mtime
              FROM dirs
             WHERE snapshot_id = ? AND depth <= ?
             ORDER BY bytes DESC
             LIMIT ?
            """,
            (snapshot_id, max_depth, limit * 6),
        )
    )

    picked: list[Hotspot] = []
    for row in rows:
        path = row["path"]
        # 已入选某个祖先时跳过后代,避免同一份空间报两次
        if any(path.startswith(p.path + "\\") for p in picked):
            continue
        hint = classify_path(path)
        picked.append(
            Hotspot(
                path=path,
                bytes=int(row["bytes"]),
                files=int(row["files"]),
                newest=row["newest_mtime"],
                label=hint[0] if hint else None,
                advice=hint[1] if hint else None,
                safety=hint[2] if hint else None,
            )
        )
        if len(picked) >= limit:
            break
    return picked


def biggest_files(
    conn: sqlite3.Connection, snapshot_id: int, *, limit: int = 40
) -> list[Hotspot]:
    rows = conn.execute(
        """
        SELECT path, bytes, mtime FROM files
         WHERE snapshot_id = ?
         ORDER BY bytes DESC LIMIT ?
        """,
        (snapshot_id, limit),
    )
    out: list[Hotspot] = []
    for row in rows:
        hint = classify_path(row["path"])
        out.append(
            Hotspot(
                path=row["path"],
                bytes=int(row["bytes"]),
                files=1,
                newest=row["mtime"],
                label=hint[0] if hint else None,
                advice=hint[1] if hint else None,
                safety=hint[2] if hint else None,
            )
        )
    return out


def recently_grown(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    days: int = 14,
    limit: int = 25,
    min_bytes: int = 64 * 1024 * 1024,
    now: float | None = None,
) -> list[GrowthSpot]:
    """最近有新文件写入的目录。

    用 newest_ctime 判断,所以这是「最近被写入过的大目录」,
    不是「最近增长了多少」—— 后者需要两个快照才能算。
    """
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    rows = conn.execute(
        """
        SELECT path, bytes, newest_ctime
          FROM dirs
         WHERE snapshot_id = ? AND newest_ctime >= ? AND bytes >= ?
         ORDER BY bytes DESC LIMIT ?
        """,
        (snapshot_id, cutoff, min_bytes, limit * 4),
    )

    picked: list[GrowthSpot] = []
    for row in rows:
        path = row["path"]
        if any(path.startswith(p.path + "\\") for p in picked):
            continue
        newest = row["newest_ctime"]
        picked.append(
            GrowthSpot(
                path=path,
                bytes=int(row["bytes"]),
                newest=newest,
                days_old=((now - newest) / 86400) if newest else None,
            )
        )
        if len(picked) >= limit:
            break
    return picked


def cleanup_candidates(
    conn: sqlite3.Connection,
    snapshot_id: int,
    *,
    limit: int = 40,
    min_bytes: int = 32 * 1024 * 1024,
) -> list[Hotspot]:
    """命中清理规则且占用可观的目录与文件。

    只识别,不删除。安全等级和说明一起给出,由人决定。
    """
    found: dict[str, Hotspot] = {}

    for row in conn.execute(
        """
        SELECT path, bytes, files, newest_mtime FROM dirs
         WHERE snapshot_id = ? AND bytes >= ?
         ORDER BY bytes DESC
        """,
        (snapshot_id, min_bytes),
    ):
        path = row["path"]
        hint = classify_path(path)
        if hint is None:
            continue
        # 命中同一规则的父目录已收录时跳过后代
        if has_ancestor_in(path, found):
            continue
        found[path] = Hotspot(
            path=path,
            bytes=int(row["bytes"]),
            files=int(row["files"]),
            newest=row["newest_mtime"],
            label=hint[0],
            advice=hint[1],
            safety=hint[2],
        )

    for row in conn.execute(
        """
        SELECT path, bytes, mtime FROM files
         WHERE snapshot_id = ? AND bytes >= ?
         ORDER BY bytes DESC
        """,
        (snapshot_id, min_bytes),
    ):
        path = row["path"]
        hint = classify_path(path)
        if hint is None or path in found:
            continue
        if has_ancestor_in(path, found):
            continue
        found[path] = Hotspot(
            path=path,
            bytes=int(row["bytes"]),
            files=1,
            newest=row["mtime"],
            label=hint[0],
            advice=hint[1],
            safety=hint[2],
        )

    out = sorted(found.values(), key=lambda h: h.bytes, reverse=True)
    return out[:limit]


def age_profile(conn: sqlite3.Connection, snapshot_id: int, *, now: float | None = None) -> list[dict]:
    """按年龄段汇总占用,给树图的年龄图例用。"""
    now = time.time() if now is None else now
    bands = (
        ("today", 1, "今天"),
        ("week", 7, "本周"),
        ("month", 30, "本月"),
        ("quarter", 90, "三个月内"),
        ("year", 365, "一年内"),
        ("older", None, "更早"),
    )

    rows = list(
        conn.execute(
            "SELECT day, SUM(bytes) b, SUM(files) f FROM age_buckets "
            "WHERE snapshot_id = ? GROUP BY day",
            (snapshot_id,),
        )
    )

    totals = {key: [0, 0] for key, _, _ in bands}
    for row in rows:
        # 取当天正午,而不是午夜:落在分界上的那天不会因为几小时的偏差被算到
        # 相邻的年龄段里。这里一次请求要转上千个日期,strptime 太慢,见 config。
        ts = config.day_timestamp(row["day"], 12)
        if ts is None:
            continue
        age_days = (now - ts) / 86400
        for key, limit, _label in bands:
            if limit is None or age_days <= limit:
                totals[key][0] += int(row["b"] or 0)
                totals[key][1] += int(row["f"] or 0)
                break

    return [
        {"key": key, "label": label, "max_days": limit,
         "bytes": totals[key][0], "files": totals[key][1]}
        for key, limit, label in bands
    ]
