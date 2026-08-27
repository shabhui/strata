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
    if path == "":
        return ""
    parts = path.split("\\", depth)
    if len(parts) > depth:
        parts = parts[:depth]
    return "\\".join(parts)


def _newer(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b


def build_tree(entries: list[ScanEntry]) -> tuple[dict[str, DirNode], int, int]:
    """汇总出目录树。返回 (路径 → 节点, 总字节, 文件数)。

    两步:先把每个文件计到直属目录,再按深度降序做一次自底向上累计。
    文件的父目录若没被显式列出(MFT 路径解析失败等)会补建,
    连同缺失的祖先一起 —— 否则这些字节会凭空消失。
    """
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
                node.newest_mtime = _newer(node.newest_mtime, e.modified)
                node.newest_ctime = _newer(node.newest_ctime, e.created)
            continue

        node = ensure(parent_of(e.path) or "")
        node.direct_bytes += e.bytes
        node.direct_files += 1
        node.newest_mtime = _newer(node.newest_mtime, e.modified)
        node.newest_ctime = _newer(node.newest_ctime, e.created)
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
        parent.newest_mtime = _newer(parent.newest_mtime, node.newest_mtime)
        parent.newest_ctime = _newer(parent.newest_ctime, node.newest_ctime)

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
        if node.path == "":
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
    for node in nodes.values():
        if node.path == "" or node.path in keep:
            continue
        ancestor = parent_of(node.path)
        while ancestor is not None and ancestor != "" and ancestor not in keep:
            ancestor = parent_of(ancestor)
        if ancestor and ancestor in keep:
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

    for e in entries:
        if e.is_dir or e.bytes <= 0:
            continue
        stamp = e.created if e.created is not None else e.modified
        # 坏时间戳直接跳过。系统盘上 FILETIME 为 0 或写着未来年份的文件不少,
        # 让它们参与分桶只会把时间轴污染成 1601 年的一根巨柱
        day = config.safe_day(stamp)
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
