"""解码 NTFS 数据运行(data runs / mapping pairs)。

每个运行以一个头字节开头:低 4 位是「长度」字段的字节数,
高 4 位是「LCN 偏移」字段的字节数。偏移是相对上一个运行的有符号增量。
高 4 位为 0 表示稀疏运行(不占实际簇)。头字节为 0 表示结束。
"""

from __future__ import annotations

from typing import NamedTuple


class Run(NamedTuple):
    """一段连续簇。lcn 为 None 表示稀疏(未分配)。"""

    vcn: int
    lcn: int | None
    length: int

    @property
    def sparse(self) -> bool:
        return self.lcn is None


def decode_runlist(data: bytes | memoryview, *, max_runs: int = 1_000_000) -> list[Run]:
    """把 mapping pairs 字节串解成运行列表。

    遇到结构不合理时抛 ValueError —— 宁可让调用方退回慢速扫描,
    也不要返回半截数据把大小算错。
    """
    runs: list[Run] = []
    pos = 0
    vcn = 0
    lcn = 0
    size = len(data)

    while pos < size:
        header = data[pos]
        if header == 0:
            break
        pos += 1
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F

        if len_size == 0:
            raise ValueError(f"运行长度字段为 0(头字节 {header:#04x},偏移 {pos - 1})")
        if pos + len_size + off_size > size:
            raise ValueError("运行数据在读完字段前就结束了")

        length = int.from_bytes(data[pos : pos + len_size], "little", signed=False)
        pos += len_size

        if off_size == 0:
            # 稀疏运行:占逻辑空间但不占簇
            runs.append(Run(vcn=vcn, lcn=None, length=length))
        else:
            delta = int.from_bytes(data[pos : pos + off_size], "little", signed=True)
            pos += off_size
            lcn += delta
            if lcn < 0:
                raise ValueError(f"运行解出负的 LCN: {lcn}")
            runs.append(Run(vcn=vcn, lcn=lcn, length=length))

        vcn += length
        if len(runs) > max_runs:
            raise ValueError(f"运行数超过上限 {max_runs},数据可能损坏")

    return runs


def total_clusters(runs: list[Run], *, include_sparse: bool = False) -> int:
    """运行列表覆盖的簇数。默认不算稀疏段(它们不占实际空间)。"""
    if include_sparse:
        return sum(r.length for r in runs)
    return sum(r.length for r in runs if not r.sparse)


def iter_extents(runs: list[Run], bytes_per_cluster: int):
    """产出 (字节偏移, 字节长度),跳过稀疏段。用于顺序读取一条流。"""
    for run in runs:
        if run.sparse or run.lcn is None:
            continue
        yield run.lcn * bytes_per_cluster, run.length * bytes_per_cluster
