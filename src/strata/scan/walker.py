"""os.scandir 后备扫描器。

MFT 直读不可用时(非管理员、非 NTFS、卷解析失败)退回到这里。
慢一个数量级(实测本机 C: 约 112 秒),而且有两个固有缺陷:
  - 硬链接会被重复计数(WinSxS 会虚高十几 GB)
  - 拿到的是逻辑大小,不是占盘大小
所以只作后备。产出的条目结构与 MFT 路径一致,下游代码不用区分。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

# 重解析点标志:符号链接/联接点。不跟进,否则会重复计数甚至成环。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(slots=True)
class WalkEntry:
    """一个文件或目录。path 不含盘符,用反斜杠。"""

    path: str
    name: str
    is_dir: bool
    bytes: int = 0
    created: float | None = None
    modified: float | None = None
    attributes: int = 0


@dataclass(slots=True)
class WalkStats:
    files: int = 0
    dirs: int = 0
    bytes_total: int = 0
    errors: int = 0
    skipped_reparse: int = 0
    duration_ms: int = 0


def walk_drive(
    drive: str,
    *,
    progress: Callable[[int], None] | None = None,
    progress_every: int = 20_000,
) -> tuple[list[WalkEntry], WalkStats]:
    """递归遍历整个盘。返回 (条目, 统计)。

    权限不足的目录直接跳过并计入 errors —— 一个 System Volume Information
    不该让整次扫描失败。
    """
    root = drive.rstrip("\\") + "\\"
    stats = WalkStats()
    entries: list[WalkEntry] = []
    t0 = time.perf_counter()
    prefix_len = len(root)

    # 显式栈,避免深目录树递归超限
    stack: list[str] = [root]

    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            stats.errors += 1
            continue

        with it:
            while True:
                try:
                    dir_entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    stats.errors += 1
                    continue

                try:
                    st = dir_entry.stat(follow_symlinks=False)
                    attributes = getattr(st, "st_file_attributes", 0)
                    is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = dir_entry.is_dir(follow_symlinks=False)
                except OSError:
                    stats.errors += 1
                    continue

                rel = dir_entry.path[prefix_len:]

                if is_dir:
                    stats.dirs += 1
                    entries.append(
                        WalkEntry(
                            path=rel,
                            name=dir_entry.name,
                            is_dir=True,
                            bytes=0,
                            created=getattr(st, "st_birthtime", None) or st.st_ctime,
                            modified=st.st_mtime,
                            attributes=attributes,
                        )
                    )
                    if is_reparse:
                        # 联接点/符号链接不跟进
                        stats.skipped_reparse += 1
                    else:
                        stack.append(dir_entry.path)
                else:
                    size = 0 if is_reparse else st.st_size
                    stats.files += 1
                    stats.bytes_total += size
                    entries.append(
                        WalkEntry(
                            path=rel,
                            name=dir_entry.name,
                            is_dir=False,
                            bytes=size,
                            created=getattr(st, "st_birthtime", None) or st.st_ctime,
                            modified=st.st_mtime,
                            attributes=attributes,
                        )
                    )
                    if is_reparse:
                        stats.skipped_reparse += 1

                if progress is not None:
                    total = stats.files + stats.dirs
                    if total % progress_every == 0:
                        progress(total)

    stats.duration_ms = int((time.perf_counter() - t0) * 1000)
    return entries, stats
