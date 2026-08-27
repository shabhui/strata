"""每日增减时间轴 —— 合并回溯层与实测层。

两个数据来源,精度不同,界面上必须区分开:

回溯层(basis='retro'):把现存文件按创建日分桶。
    优点:刚装好就有几个月的历史。
    缺点:只看得到「还在盘上的东西」。8 月 3 日下载又在 8 月 9 日删掉的
          50 GB,今天扫描时完全看不到 —— 那天会显示成 0。
    所以回溯层是「净新增的下界」,不是当天真实写入量。

实测层(basis='measured'):相邻两个快照对比。
    优点:含删除,是真实净增减。
    缺点:只能从装了这个工具那天开始。

分工按天来定:一对相邻快照覆盖到的日期用实测,其余日期用回溯。
第一个快照当天没有差值可算,所以仍归回溯 —— 刚装好那天也看得见增长。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

# 旧快照会被降级成粗粒度,只保留浅层目录。
# 对比时统一用这个深度,避免「某天目录消失了」其实只是被降级裁掉。
COMPARE_DEPTH = 3


@dataclass(slots=True)
class Contributor:
    path: str
    bytes: int

    def as_dict(self) -> dict:
        return {"path": self.path, "bytes": self.bytes}


@dataclass(slots=True)
class DayChange:
    day: str
    added: int = 0
    removed: int = 0
    net: int = 0
    files_added: int = 0
    basis: str = "retro"          # 'retro' | 'measured'
    contributors: list[Contributor] = field(default_factory=list)
    shrinkers: list[Contributor] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "added": self.added,
            "removed": self.removed,
            "net": self.net,
            "files_added": self.files_added,
            "basis": self.basis,
            "contributors": [c.as_dict() for c in self.contributors],
            "shrinkers": [c.as_dict() for c in self.shrinkers],
        }


def _day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _dir_bytes_at(
    conn: sqlite3.Connection, snapshot_id: int, depth: int = COMPARE_DEPTH
) -> dict[str, int]:
    return {
        r["path"]: r["bytes"]
        for r in conn.execute(
            "SELECT path, bytes FROM dirs WHERE snapshot_id = ? AND depth <= ?",
            (snapshot_id, depth),
        )
    }


def _retro_days(
    conn: sqlite3.Connection, snapshot_id: int, top_n: int
) -> dict[str, DayChange]:
    """从 age_buckets 读出按创建日的新增。"""
    days: dict[str, DayChange] = {}
    for row in conn.execute(
        """
        SELECT day, attribution, bytes, files
          FROM age_buckets
         WHERE snapshot_id = ?
         ORDER BY day, bytes DESC
        """,
        (snapshot_id,),
    ):
        day = row["day"]
        change = days.get(day)
        if change is None:
            change = DayChange(day=day, basis="retro")
            days[day] = change
        change.added += row["bytes"]
        change.net += row["bytes"]
        change.files_added += row["files"]
        if len(change.contributors) < top_n and row["attribution"]:
            change.contributors.append(
                Contributor(path=row["attribution"], bytes=row["bytes"])
            )
    return days


def _measured_days(
    conn: sqlite3.Connection, drive: str, top_n: int
) -> tuple[dict[str, DayChange], float | None]:
    """相邻快照两两对比,得到实测净增减。

    返回 (日期 → 变化, 第一个快照的时间戳)。
    """
    snaps = list(
        conn.execute(
            """
            SELECT id, taken_at, scanned_bytes
              FROM snapshots
             WHERE drive = ? AND complete = 1
             ORDER BY taken_at
            """,
            (drive,),
        )
    )
    if len(snaps) < 2:
        return {}, (float(snaps[0]["taken_at"]) if snaps else None)

    days: dict[str, DayChange] = {}
    # 一天里可能有多个快照(计划任务加手动扫描),同一天的增减要累加到
    # 一起再排名,否则后一对快照会把前一对的归因覆盖掉,而字节数已经加进去了
    per_day_delta: dict[str, dict[str, int]] = {}

    for prev, cur in zip(snaps, snaps[1:]):
        day = _day_of(float(cur["taken_at"]))
        change = days.get(day)
        if change is None:
            change = DayChange(day=day, basis="measured")
            days[day] = change
            per_day_delta[day] = {}

        # 净增减取快照总量之差 —— 这是权威值
        change.net += int(cur["scanned_bytes"]) - int(prev["scanned_bytes"])

        before = _dir_bytes_at(conn, int(prev["id"]))
        after = _dir_bytes_at(conn, int(cur["id"]))

        bucket = per_day_delta[day]
        for path in set(before) | set(after):
            delta = after.get(path, 0) - before.get(path, 0)
            if delta:
                bucket[path] = bucket.get(path, 0) + delta

    for day, change in days.items():
        bucket = per_day_delta[day]
        grew = [Contributor(path=p, bytes=d) for p, d in bucket.items() if d > 0]
        shrank = [Contributor(path=p, bytes=-d) for p, d in bucket.items() if d < 0]

        # 只统计顶层增减,避免父子目录重复累加
        change.added = sum(c.bytes for c in grew if "\\" not in c.path)
        change.removed = sum(c.bytes for c in shrank if "\\" not in c.path)

        grew.sort(key=lambda c: c.bytes, reverse=True)
        shrank.sort(key=lambda c: c.bytes, reverse=True)
        change.contributors = grew[:top_n]
        change.shrinkers = shrank[:top_n]

    return days, float(snaps[0]["taken_at"])


def _spanned_days(conn: sqlite3.Connection, drive: str) -> set[str]:
    """被快照对跨到的日期 —— 这些天归实测层,回溯层要让开。

    一对快照覆盖「前一次那天之后」到「后一次那天」,下界不含、上界含。
    中间跳过的天也算覆盖:那几天的增减已经并进后一次快照的差值里了。

    下界不含,是因为快照拍在某天的某个时刻,它测不到那天在它之前的部分。
    于是同一天里的两个快照跨不过任何一天(区间是空的),那天仍归回溯 ——
    相隔半小时的两次扫描测出来的是半小时,不是一天,当成一天报会把
    当天少算一大截。这种情况只有装好当天才会单独出现:平时有计划任务
    跨天的那一对,同一天的差值会累加进去,不会丢。

    只有一个快照时返回空集合:它是基线,不是差值,一天也没测过。
    """
    snaps = [
        float(r["taken_at"])
        for r in conn.execute(
            "SELECT taken_at FROM snapshots"
            "  WHERE drive = ? AND complete = 1 ORDER BY taken_at",
            (drive,),
        )
    ]
    spanned: set[str] = set()
    step = timedelta(days=1)
    for prev, cur in zip(snaps, snaps[1:]):
        day = date.fromisoformat(_day_of(prev)) + step      # 下界不含
        end = date.fromisoformat(_day_of(cur))
        while day <= end:
            spanned.add(day.isoformat())
            day += step
    return spanned


def _fill_gaps(days: list[DayChange]) -> list[DayChange]:
    """把日期序列补齐,缺的天填 0,时间轴才不会把间隔画错。

    用 date 做加法而不是给时间戳加 86400 —— 夏令时的日子长 23 或 25 小时,
    加秒数会让某一天出现两次或直接被跳过。
    """
    if not days:
        return []
    index = {d.day: d for d in days}
    cur = date.fromisoformat(days[0].day)
    end = date.fromisoformat(days[-1].day)

    out: list[DayChange] = []
    step = timedelta(days=1)
    while cur <= end:
        key = cur.isoformat()
        out.append(index.get(key) or DayChange(day=key, basis="retro"))
        cur += step
    return out


def build_timeline(
    conn: sqlite3.Connection,
    drive: str,
    *,
    days: int = 90,
    top_n: int = 5,
    fill_gaps: bool = True,
) -> list[DayChange]:
    """构造某盘最近 days 天的每日增减。

    第一个快照之前的日期用回溯值,之后用实测值。
    """
    from ..store import db

    latest = db.latest_snapshot(conn, drive)
    if latest is None:
        return []

    retro = _retro_days(conn, int(latest["id"]), top_n)
    measured, _ = _measured_days(conn, drive, top_n)

    combined: dict[str, DayChange] = {}
    # 回溯层负责所有没被快照对覆盖的日期。注意这包括第一个快照当天:
    # 那天还没有任何差值可算,把它划给实测层只会让它变成空白 ——
    # 而刚装好那天恰恰是最想看到的一天。
    spanned = _spanned_days(conn, drive)
    for day, change in retro.items():
        if day not in spanned:
            combined[day] = change
    # 实测层只接管真正跨到的日期。没跨到的那天(同一天里扫了两次)差值
    # 只覆盖一天中的一小段,当成整天报会少算 —— 那天让回溯来说。
    for day, change in measured.items():
        if day in spanned:
            combined[day] = change

    # 上界卡在最后一次扫描那天。文件的创建时间可以是未来(装包写错、
    # 时钟跑偏),那种桶会把时间轴一路补零补到几年后,真实的那几十天
    # 被挤成看不见的细线。我们对扫描之后的日子没有任何观测。
    horizon = _day_of(float(latest["taken_at"]))
    cutoff = _day_of(time.time() - days * 86400)
    selected = [c for day, c in combined.items() if cutoff <= day <= horizon]
    selected.sort(key=lambda c: c.day)

    return _fill_gaps(selected) if fill_gaps else selected


def timeline_summary(changes: list[DayChange]) -> dict:
    """时间轴的汇总数字,给界面顶部用。"""
    measured = [c for c in changes if c.basis == "measured"]
    return {
        "days": len(changes),
        "total_added": sum(c.added for c in changes),
        "total_removed": sum(c.removed for c in changes),
        "net": sum(c.net for c in changes),
        "measured_days": len(measured),
        "retro_days": len(changes) - len(measured),
        "busiest_day": max(changes, key=lambda c: c.added).day if changes else None,
        "first_measured_day": measured[0].day if measured else None,
    }
