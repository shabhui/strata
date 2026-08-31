"""吃空间的大户、最近长胖的目录、可清理的候选。

「可清理」只做识别和标注,不删任何东西 —— 判断由人来做。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from .. import config
from .paths import has_ancestor_in

# 可清理候选:路径片段 → (文案代号, 安全等级)
#
# 这里只出代号,不出中文。标签和建议在 web/i18n.js 里按代号取 —— 代号是
# clean.rule.<code>.label / .advice。
#
# 为什么后端不直接给中文:这两段字是整个界面上最长的一片文字,而且要跟着
# 界面语言变。中文写在这儿的话,英文界面上「可以清的」整张表还是中文,
# 而它旁边的表头、按钮全是英文 —— 一屏里两种语言,最难看的那种。
# 后端不知道人在看哪种语言(而且切语言时不会重新请求),所以判断留在后端,
# 措辞交给前端。
#
# 安全等级: 'safe' 删了只会丢缓存;'review' 可能有用,要看一眼;
#           'careful' 删了可能影响功能
CLEANUP_RULES: tuple[tuple[str, str, str], ...] = (
    (r"windows\installer", "winInstaller", "careful"),
    (r"windows\winsxs", "winSxs", "careful"),
    (r"windows\softwaredistribution\download", "winUpdate", "safe"),
    (r"windows\temp", "winTemp", "safe"),
    (r"$recycle.bin", "recycleBin", "review"),
    (r"appdata\local\temp", "userTemp", "safe"),
    (r"appdata\local\packages", "storeApps", "review"),
    (r"appdata\local\microsoft\windows\explorer", "thumbnails", "safe"),
    (r"appdata\local\pip\cache", "pipCache", "safe"),
    (r"appdata\local\npm-cache", "npmCache", "safe"),
    (r"appdata\roaming\npm-cache", "npmCache", "safe"),
    (r"appdata\local\yarn\cache", "yarnCache", "safe"),
    (r".cache", "toolCache", "safe"),
    (r".gradle\caches", "gradleCache", "safe"),
    (r".m2\repository", "mavenRepo", "review"),
    (r".nuget\packages", "nugetCache", "safe"),
    (r"node_modules", "nodeModules", "review"),
    (r"__pycache__", "pycache", "safe"),
    (r"\target\debug", "rustDebug", "safe"),
    (r"\target\release", "rustRelease", "safe"),
    (r"steamapps\downloading", "steamPartial", "safe"),
    (r"steamapps\shadercache", "steamShaders", "safe"),
    (r"steamapps\workshop", "steamWorkshop", "review"),
    (r"nvidia corporation\downloader", "nvidiaDownload", "safe"),
    (r"crashdumps", "crashDumps", "safe"),
    (r"windows\minidump", "miniDumps", "safe"),
    (r"hiberfil.sys", "hiberfil", "careful"),
    (r"pagefile.sys", "pagefile", "careful"),
    (r"swapfile.sys", "swapfile", "careful"),
)


@dataclass(slots=True)
class Hotspot:
    path: str
    bytes: int
    files: int
    newest: float | None = None
    rule: str | None = None      # 命中的清理规则代号,见 CLEANUP_RULES
    safety: str | None = None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "files": self.files,
            "newest": self.newest,
            "rule": self.rule,
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


def classify_path(path: str) -> tuple[str, str] | None:
    """路径命中清理规则时返回 (文案代号, 安全等级)。"""
    lowered = path.lower()
    for needle, code, safety in CLEANUP_RULES:
        if needle in lowered:
            return code, safety
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
             WHERE snapshot_id = ? AND depth BETWEEN 1 AND ?
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
                rule=hint[0] if hint else None,
                safety=hint[1] if hint else None,
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
                rule=hint[0] if hint else None,
                safety=hint[1] if hint else None,
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

    区间**两头都有界**。原来只有下界,于是写在未来的目录永远满足条件,被
    永久钉在榜首,把真正在长的目录挤下去。真机上出过这一行:

        6.41 GB  写入=2030-09-16  -1477.6 天前  Program Files (x86)

    单边的区间条件不是「少了道防护」,是条件本身不完整 —— 「最近」按定义
    就是 [cutoff, 现在] 这一段,写成只有下界等于说「从那天起,一直到永远」。

    上界带容差(config.newest_ceiling),不掐在 now 上:跨机器拷来的文件常带
    对面的钟,超前几小时是正常产物,掐死了会把真的刚写过的目录整个抹掉。
    """
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    ceiling = config.newest_ceiling(now)
    rows = conn.execute(
        """
        SELECT path, bytes, newest_ctime
          FROM dirs
         WHERE snapshot_id = ? AND depth >= 1
           AND newest_ctime BETWEEN ? AND ? AND bytes >= ?
         ORDER BY bytes DESC LIMIT ?
        """,
        (snapshot_id, cutoff, ceiling, min_bytes, limit * 4),
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
                # 不让它是负数。上面的上界带了容差,所以钟稍微超前的目录
                # 会算出一个小负值 —— 那不是「未来写入」,是拷贝带来的偏差,
                # 报 0 天(刚写过)比报 -0.04 天诚实。
                days_old=max(0.0, (now - newest) / 86400) if newest else None,
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
        -- depth >= 1 排掉盘根那一行。这一处是第二道:classify_path("") 必然
        -- 返回 None(规则是子串匹配,空串命中不了任何 needle),所以根在下面
        -- 那一步本来就会被挡掉 —— 删掉这个守卫测试也是绿的,变异验证过。
        -- 留着是因为另外四处都这么写,而且规则匹配方式以后可能会变。
        SELECT path, bytes, files, newest_mtime FROM dirs
         WHERE snapshot_id = ? AND depth >= 1 AND bytes >= ?
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
            rule=hint[0],
            safety=hint[1],
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
            rule=hint[0],
            safety=hint[1],
        )

    out = sorted(found.values(), key=lambda h: h.bytes, reverse=True)
    return out[:limit]


def age_profile(conn: sqlite3.Connection, snapshot_id: int, *, now: float | None = None) -> list[dict]:
    """按年龄段汇总占用,给树图的年龄图例用。"""
    now = time.time() if now is None else now
    # 只有 key 和上界。文案不在这儿 —— 界面上那几个字是 web/app.js 的
    # AGE_BANDS 按 key 出的(它还要配色),后端再给一份中文标签没人读,
    # 只会让下一个人以为改这里就能改界面。
    bands = (
        ("today", 1),
        ("week", 7),
        ("month", 30),
        ("quarter", 90),
        ("year", 365),
        ("older", None),
    )

    rows = list(
        conn.execute(
            "SELECT day, SUM(bytes) b, SUM(files) f FROM age_buckets "
            "WHERE snapshot_id = ? GROUP BY day",
            (snapshot_id,),
        )
    )

    totals = {key: [0, 0] for key, _ in bands}
    for row in rows:
        # 取当天正午,而不是午夜:落在分界上的那天不会因为几小时的偏差被算到
        # 相邻的年龄段里。这里一次请求要转上千个日期,strptime 太慢,见 config。
        ts = config.day_timestamp(row["day"], 12)
        if ts is None:
            continue
        age_days = (now - ts) / 86400
        for key, limit in bands:
            if limit is None or age_days <= limit:
                totals[key][0] += int(row["b"] or 0)
                totals[key][1] += int(row["f"] or 0)
                break

    return [
        {"key": key, "max_days": limit,
         "bytes": totals[key][0], "files": totals[key][1]}
        for key, limit in bands
    ]
