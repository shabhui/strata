"""每日增减时间轴 —— 合并回溯层与实测层。

两个数据来源,精度不同,界面上必须区分开:

回溯层(basis='retro'):把现存文件按创建日分桶。
    优点:刚装好就有几个月的历史。
    含义:那天写入、并且到扫描时还活着的字节数 —— 是现有内容按创建日的分解,
          加起来正好等于扫到的总量(本机实测 100.0%)。
    它不是当天的净增减,方向两边都会偏:
      · 偏小:8 月 3 日下载又在 8 月 9 日删掉的 50 GB,今天看不到,那天显示 0。
      · 偏大:那天删掉的是更早创建的文件,回溯完全看不见,于是净减的日子
              也只会显示成正数。真实数据上 2026-08-28 回溯说 +8.35 GB,
              实测说 -0.74 GB。
    求和之后是净增的**上界**而不是下界,推导见 timeline_summary。
    所以回溯值只能回答「现在盘上的东西是什么时候写的」,不能回答「那天涨了多少」。

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

from ..store.db import SEP
from .paths import has_ancestor_in, is_ancestor_of

# 旧快照会被降级成粗粒度,只保留浅层目录。
# 对比时统一用这个深度,避免「某天目录消失了」其实只是被降级裁掉。
COMPARE_DEPTH = 3

# 一个子目录占到父目录增减的这个比例,就认为这件事说的是它,报它而不报父目录。
# 0.9 是权衡:真实数据里 OneDrive 占 Program Files 减少量的 97%,报父目录等于
# 让人自己再找一遍;而定得再低就会把「好几个子目录一起长」说成只是其中一个。
DOMINANT_RATIO = 0.9


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


def _depth(path: str) -> int:
    return path.count(SEP)


def _collapse_chains(
    items: list[Contributor], ratio: float = DOMINANT_RATIO
) -> list[Contributor]:
    """把祖先—后代链折成一行,并且尽量报到具体的那一层。

    快照差是逐层累加的:360Safe 长了 48 MB,它每一层祖先都跟着长 48 MB。
    真实数据上前五名里出现过 Program Files (x86)、...\\360、...\\360\\360Safe
    三行同一件事,而界面只显示四行 —— 一件事吃掉了三格。

    分两步:

    一、每条链只留一个代表。按字节从大到小取,字节相同时先取更深的;取过的
        那条链上的其他节点全部跳过(祖先和后代都算)。父目录的字节必然不小于
        任何单个子目录,所以「增长分散在很多子目录」时选中的是父目录 —— 报
        「Users 长了 200 MB」,而不是五个 1 MB 的碎片。

    二、代表往下钻。只要某个直接子目录占了它 ratio 以上,就换成那个子目录,
        一直钻到分岔为止。这一步才让结论能动手:真实数据里 Program Files
        减了 137.8 MB,其中 133.8 MB 是 Microsoft OneDrive —— 报父目录等于
        让人自己再找一遍。分散的情况钻不动(最大的子目录也不够 ratio),
        自然还是报父目录。

    钻下去之后字节数会变小(父目录里别处的增减不算在子目录头上),所以最后
    要重新排一次序:调用方直接切前几行,顺序必须是准的。

    只在分隔符处切,所以 Windows\\Temp 取走之后 Windows\\Temp2 不受影响。
    """
    if not items:
        return []

    by_path = {c.path: c.bytes for c in items}
    children: dict[str, list[str]] = {}
    for c in items:
        cut = c.path.rfind(SEP)
        if cut != -1:
            children.setdefault(c.path[:cut], []).append(c.path)

    # 深的优先只在字节相同时起作用,把「整条链一个来源」的平局定死,
    # 不依赖第二步 —— 两个机制各自成立,调 ratio 不会动到它
    order = sorted(items, key=lambda c: (c.bytes, _depth(c.path)), reverse=True)
    kept: list[Contributor] = []
    spoken_for: set[str] = set()
    for cand in order:
        if has_ancestor_in(cand.path, spoken_for):
            continue                        # 链上更大的那个已经报过了
        # 反方向也要查:先取了父目录,它的后代就不该再占一格
        if any(is_ancestor_of(cand.path, p) for p in spoken_for):
            continue
        # 记的是选中时的原始路径:整条链从此都算说过了,钻到哪一层不影响这件事
        spoken_for.add(cand.path)
        kept.append(_drill_to_dominant(cand, by_path, children, ratio))

    kept.sort(key=lambda c: c.bytes, reverse=True)
    return kept


def _drill_to_dominant(
    top: Contributor,
    by_path: dict[str, int],
    children: dict[str, list[str]],
    ratio: float,
) -> Contributor:
    """沿着「占大头的那个子目录」往下走,返回走到的那一层。"""
    path, size = top.path, top.bytes
    while True:
        kids = children.get(path)
        if not kids:
            return Contributor(path=path, bytes=size)
        best = max(kids, key=lambda p: by_path[p])
        if by_path[best] < size * ratio:
            return Contributor(path=path, bytes=size)   # 分岔了,这一层就是结论
        path, size = best, by_path[best]


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

        # 折叠再截断,顺序不能反 —— 先截会让前几格全被同一条链占满
        change.contributors = _collapse_chains(grew)[:top_n]
        change.shrinkers = _collapse_chains(shrank)[:top_n]

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
    """时间轴的汇总数字,给界面顶部用。两层分开报,绝不相加。

    以前这里有一个 net = sum(所有天的 net),界面把它当「N 天净变化」的大字。
    那个数没有含义:回溯天的 net 是「现在盘上、创建于那天的字节数」,实测天的
    net 是「两次扫描的差」,量纲不同。

    真实数据上的证据(C 盘,2026-08-28 两种口径都覆盖到):
        回溯说 +8.35 GB,实测说 -0.74 GB —— 差 9 GB,方向还相反。

    加总之后错得更明确。回溯值求和等于「现存字节里创建日落在窗口内的部分」,
    也就是 扫到的总量 - 窗口之前创建且还活着的字节。而窗口之前创建还活着的
    那些,是窗口开始时就已经在盘上的字节的子集,所以:

        Σ回溯 ≥ 现在的用量 - 窗口开始时的用量 = 这段时间真实的净增

    是净增的**上界**,不是下界 —— 只有一个字节都没删过时才取等。本机上界面
    报「91 天净变化 +93 GB」,推出 91 天前只用了 85.6 GB;可盘上「91 天以上」
    的文件现在就还有 81.3 GB,而那只是活下来的部分。

    所以 retro_bytes 只报「这段时间写入并且还留在盘上多少」,net/added/removed
    三个数只统计实测的日子。字段名也换掉了,避免旧名字被当成同一个意思继续用。
    """
    measured = [c for c in changes if c.basis == "measured"]
    return {
        "days": len(changes),
        "measured_days": len(measured),
        "retro_days": len(changes) - len(measured),
        # 实测层:两次扫描的真实差,这三个数才是「变化」
        "measured_added": sum(c.added for c in measured),
        "measured_removed": sum(c.removed for c in measured),
        "measured_net": sum(c.net for c in measured),
        # 回溯层:现在盘上、创建日落在这段时间里的字节数。不是增量
        "retro_bytes": sum(c.added for c in changes if c.basis != "measured"),
        "busiest_day": max(changes, key=lambda c: c.added).day if changes else None,
        "first_measured_day": measured[0].day if measured else None,
    }
