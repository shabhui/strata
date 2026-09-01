"""把扁平的文件条目变成目录树汇总、日期分桶和文件明细。

MFT 和 scandir 两条路径都先归一成 ScanEntry,这里只认路径,不关心来源。

三个产出:
  build_tree()      目录汇总(含子树累计),按保留规则裁剪
  build_buckets()   按「文件创建日」分桶 —— 这是回溯时间轴的数据来源
  select_files()    大文件与近期文件明细
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .. import config
from ..store.db import BucketRow, DirRow, FileRow


@dataclass(slots=True)
class ScanEntry:
    """归一化后的一个文件/目录。path 不含盘符,反斜杠分隔,根目录为空串。"""

    path: str
    is_dir: bool
    bytes: int = 0
    created: float | None = None
    modified: float | None = None
    attributes: int = 0


@dataclass(slots=True)
class DirNode:
    """一个目录节点。

    direct_* 是本目录直属文件的贡献,subtree_* 是含所有后代的累计。
    两者分开存,汇总逻辑就只有「把子节点的 subtree 加到父节点的 subtree」
    这一条规则,不依赖循环顺序的巧合。
    """

    path: str
    depth: int
    direct_bytes: int = 0
    direct_files: int = 0
    subtree_bytes: int = 0
    subtree_files: int = 0
    subtree_dirs: int = 0
    newest_mtime: float | None = None
    newest_ctime: float | None = None


def parent_of(path: str) -> str | None:
    """父目录路径。顶层目录的父是根(空串),根本身返回 None。"""
    if path == "":
        return None
    idx = path.rfind("\\")
    return path[:idx] if idx > 0 else ""


def depth_of(path: str) -> int:
    """根为 0,顶层目录为 1。"""
    return 0 if path == "" else path.count("\\") + 1


def attribution_of(path: str, depth: int = config.ATTRIBUTION_DEPTH) -> str:
    """把路径截到前 depth 段,用于增长归因。

    3 段能区分到 `Users\\alice\\AppData` 和 `Program Files\\Steam\\steamapps`,
    比只看顶层目录有用得多。
    """
    # 别改成「两次 find + 一次切片」—— 试过,**慢 1.7 倍**(82 万条路径
    # 0.18s → 0.31s,同进程交错量三轮)。看着 split 要建列表再 join、显得比
    # 找两个下标浪费,但 split(maxsplit) 是一次 C 调用,而 find 循环每轮都是
    # 解释器在跑。数分配次数不等于量时间。
    if path == "":
        return ""
    parts = path.split("\\", depth)
    if len(parts) > depth:
        parts = parts[:depth]
    return "\\".join(parts)


def _newer(a: float | None, b: float | None, ceiling: float) -> float | None:
    """取较新的一个,顺手把不可信的滤掉。

    过滤放在这里而不是各个调用点:这是两个字段(mtime/ctime)、两轮聚合
    (文件→目录、目录→父目录)的唯一入口。聚合取最大值,所以一个坏值会
    被放大到它的每一级祖先 —— 真机上那个 2030 年就是这么从一个文件爬到
    `Program Files (x86)`(6.5 GB,必然出现在大目录表里)再爬到盘根的。

    后果不是崩,是**静默显示错的数**:`recently_grown` 按
    `newest_ctime >= cutoff` 筛,2030 永远满足,那个目录被永久钉在
    「最近写入」榜首;算出的 days_old 是 -1477.6,负数直接进了 API;
    前端 `ageText` 判 `d < 1` 就说「今天」。而同一个目录在大目录表里用
    `newest_mtime` 显示 2026-08-30 —— 两个面板自相矛盾。

    ceiling 是必传的,没有默认值。写成 `ceiling=None` 再在里面兜一个
    `time.time()` 会更好调用,但这个项目已经栽过两次「默认值让没接上的参数
    看起来在工作」(dir_paths 从来没接上、prune_usn_events 从来没被调用)。
    必传的话,漏接就是 TypeError,当场炸。

    返 None 而不是退一个凑合的数:None 在下游是「不知道」,列表显示未知、
    时间范围筛不到它。留个假数字下游会当真。
    """
    a = config.safe_ts(a)
    b = config.safe_ts(b)
    if a is not None and a > ceiling:
        a = None
    if b is not None and b > ceiling:
        b = None
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def build_tree(
    entries: list[ScanEntry], *, now: float | None = None
) -> tuple[dict[str, DirNode], int, int]:
    """汇总出目录树。返回 (路径 → 节点, 总字节, 文件数)。

    两步:先把每个文件计到直属目录,再按深度降序做一次自底向上累计。
    文件的父目录若没被显式列出(MFT 路径解析失败等)会补建,
    连同缺失的祖先一起 —— 否则这些字节会凭空消失。

    now 只给测试用。上界在这儿算一次然后传下去,不在 _newer 里每次取 ——
    一次全盘扫描要调它一百多万次,而这个界在一次扫描里本来就该是同一个。
    """
    ceiling = config.newest_ceiling(now)
    nodes: dict[str, DirNode] = {"": DirNode(path="", depth=0)}

    def ensure(path: str) -> DirNode:
        node = nodes.get(path)
        if node is not None:
            return node
        node = DirNode(path=path, depth=depth_of(path))
        nodes[path] = node
        parent = parent_of(path)
        while parent is not None and parent not in nodes:
            nodes[parent] = DirNode(path=parent, depth=depth_of(parent))
            parent = parent_of(parent)
        return node

    # 第一步:建节点 + 记直属贡献
    total_bytes = 0
    total_files = 0
    for e in entries:
        if e.is_dir:
            if e.path != "":
                node = ensure(e.path)
                node.newest_mtime = _newer(node.newest_mtime, e.modified, ceiling)
                node.newest_ctime = _newer(node.newest_ctime, e.created, ceiling)
            continue

        node = ensure(parent_of(e.path) or "")
        node.direct_bytes += e.bytes
        node.direct_files += 1
        node.newest_mtime = _newer(node.newest_mtime, e.modified, ceiling)
        node.newest_ctime = _newer(node.newest_ctime, e.created, ceiling)
        total_bytes += e.bytes
        total_files += 1

    # 第二步:自底向上累计。深度降序保证处理某节点时它的子节点已经算完。
    for node in sorted(nodes.values(), key=lambda n: n.depth, reverse=True):
        node.subtree_bytes += node.direct_bytes
        node.subtree_files += node.direct_files

        parent_path = parent_of(node.path)
        if parent_path is None:
            continue
        parent = nodes.get(parent_path)
        if parent is None:
            continue
        parent.subtree_bytes += node.subtree_bytes
        parent.subtree_files += node.subtree_files
        parent.subtree_dirs += node.subtree_dirs + 1  # +1 是 node 自己
        parent.newest_mtime = _newer(parent.newest_mtime, node.newest_mtime, ceiling)
        parent.newest_ctime = _newer(parent.newest_ctime, node.newest_ctime, ceiling)

    return nodes, total_bytes, total_files


def prune_tree(
    nodes: dict[str, DirNode],
    *,
    min_bytes: int = config.DIR_KEEP_MIN_BYTES,
    max_depth: int = config.DIR_KEEP_MAX_DEPTH,
) -> list[DirRow]:
    """按保留规则裁剪目录,被裁掉的合并进最近的保留祖先。

    保留条件:占用 >= min_bytes,或深度 <= max_depth。
    被裁掉的目录数与字节记在保留祖先的 folded_* 字段上,
    界面能显示「另有 1,240 个较小目录合计 3.2 GB」而不是假装它们不存在。
    """
    keep: dict[str, DirRow] = {}
    for node in nodes.values():
        # 根节点那一行无条件写出来,不参与保留判断。
        #
        # 原来直接 continue,盘根就没有行。后果不是少一行装饰:盘根下的文件
        # (C: 上就是 pagefile.sys 和 hiberfil.sys,一般是整块盘最大的两个)
        # 字节记在根节点的 direct_bytes 上,而这一行不落库 —— 这些字节进了
        # scanned_bytes,却在树里查无此人,总数和树永远差这么多。实测构造
        # 14 GB 盘根文件,树里写出的行 own_bytes 合计是 0。
        #
        # 顺带修掉 /api/tree?path= 返回 node: null。
        #
        # 加了这一行,底下所有按 depth 取目录的查询都得排掉 depth 0,不然
        # 「整块盘」会稳定霸占榜首 —— 见 diff.py / timeline.py / hotspots.py
        # 里的 depth BETWEEN 1 AND ? 和 depth >= 1。
        if node.path == "":
            keep[""] = DirRow(
                path="",
                depth=0,
                bytes=node.subtree_bytes,
                own_bytes=node.direct_bytes,
                files=node.subtree_files,
                dirs=node.subtree_dirs,
                newest_mtime=node.newest_mtime,
                newest_ctime=node.newest_ctime,
            )
            continue
        if node.subtree_bytes >= min_bytes or node.depth <= max_depth:
            keep[node.path] = DirRow(
                path=node.path,
                depth=node.depth,
                bytes=node.subtree_bytes,
                own_bytes=node.direct_bytes,
                files=node.subtree_files,
                dirs=node.subtree_dirs,
                newest_mtime=node.newest_mtime,
                newest_ctime=node.newest_ctime,
            )

    # 被裁掉的目录归给最近的保留祖先。
    # 只累加 direct_bytes:每个被裁节点各报自己的直属字节,
    # 合起来正好是这片被裁子树的总量,不会重复。
    # 根现在也在 keep 里,所以不再需要把它排除在外 —— 顶层目录被裁掉时
    # 正好折叠到根这一行上。三个判断都得跟着改:`""` 是 falsy,写成
    # `if ancestor` 的话被裁掉的顶层目录会折叠到「没有地方」,folded_bytes
    # 无声丢失。
    for node in nodes.values():
        if node.path in keep:
            continue
        ancestor = parent_of(node.path)
        while ancestor is not None and ancestor not in keep:
            ancestor = parent_of(ancestor)
        if ancestor is not None and ancestor in keep:
            row = keep[ancestor]
            row.folded_children += 1
            row.folded_bytes += node.direct_bytes

    return sorted(keep.values(), key=lambda r: (-r.bytes, r.path))


def build_buckets(
    entries: list[ScanEntry],
    *,
    min_bytes: int = config.BUCKET_MIN_BYTES,
    attribution_depth: int = config.ATTRIBUTION_DEPTH,
) -> list[BucketRow]:
    """按文件创建日 + 路径归因分桶。这是回溯时间轴的数据来源。

    低于 min_bytes 的桶合并成当天的「其他」(attribution 为空串),
    这样每天的总量仍然准确,只是细分粒度变粗。
    """
    raw: dict[tuple[str, str], tuple[int, int]] = {}
    # 记忆化的 safe_day。行为和 config.safe_day 一样,只是记住算过的日子 ——
    # 这一行调用要走 82 万次,而贵的是里面的 time.localtime()。2.3x,见 day_memo。
    safe_day = config.day_memo()

    for e in entries:
        if e.is_dir or e.bytes <= 0:
            continue
        stamp = e.created if e.created is not None else e.modified
        # 坏时间戳直接跳过。系统盘上 FILETIME 为 0 或写着未来年份的文件不少,
        # 让它们参与分桶只会把时间轴污染成 1601 年的一根巨柱
        day = safe_day(stamp)
        if day is None:
            continue
        attribution = attribution_of(e.path, attribution_depth)
        key = (day, attribution)
        prev = raw.get(key)
        if prev is None:
            raw[key] = (e.bytes, 1)
        else:
            raw[key] = (prev[0] + e.bytes, prev[1] + 1)

    rows: list[BucketRow] = []
    other: dict[str, tuple[int, int]] = {}
    for (day, attribution), (size, count) in raw.items():
        if size >= min_bytes:
            rows.append(BucketRow(day=day, attribution=attribution, bytes=size, files=count))
        else:
            prev = other.get(day)
            other[day] = (
                (size, count) if prev is None else (prev[0] + size, prev[1] + count)
            )

    for day, (size, count) in other.items():
        rows.append(BucketRow(day=day, attribution="", bytes=size, files=count))

    rows.sort(key=lambda r: (r.day, -r.bytes))
    return rows


def select_files(
    entries: list[ScanEntry],
    *,
    now: float | None = None,
    min_bytes: int = config.FILE_KEEP_MIN_BYTES,
    recent_days: int = config.FILE_RECENT_DAYS,
    recent_min_bytes: int = config.FILE_RECENT_MIN_BYTES,
    cap: int = config.FILE_ROW_CAP,
) -> list[FileRow]:
    """挑出值得逐条入库的文件:够大的,或够新的。

    近期文件放宽阈值,因为「这几天新增了什么」里 2 MB 的东西也有意义;
    而全盘范围内 2 MB 的文件有几十万个,不能全存。
    """
    now = time.time() if now is None else now
    recent_cutoff = now - recent_days * 86400
    picked: list[FileRow] = []

    for e in entries:
        if e.is_dir or e.bytes <= 0:
            continue
        stamp = e.created if e.created is not None else e.modified
        is_recent = stamp is not None and stamp >= recent_cutoff
        if e.bytes >= min_bytes or (is_recent and e.bytes >= recent_min_bytes):
            picked.append(
                FileRow(path=e.path, bytes=e.bytes, mtime=e.modified, ctime=e.created)
            )

    if len(picked) > cap:
        # 超上限时保大的,近期文件优先级体现在阈值上,这里只按大小截断
        picked.sort(key=lambda r: r.bytes, reverse=True)
        picked = picked[:cap]

    picked.sort(key=lambda r: (-r.bytes, r.path))
    return picked
