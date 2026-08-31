"""os.scandir 后备扫描器。

MFT 直读不可用时(非管理员、非 NTFS、卷解析失败)退回到这里。
慢一个数量级(实测本机 C: 冷缓存 142 秒、热缓存 38 秒),而且有两个固有缺陷:
  - 硬链接会被重复计数(WinSxS 会虚高十几 GB)
  - 拿到的是逻辑大小,不是占盘大小
所以只作后备。产出的条目结构与 MFT 路径一致,下游代码不用区分。

关于快慢,tools/bench_walk.py 在整块 C: 上量过四种写法(第二轮,缓存已热):

    只枚举(不 stat 不建对象)   29.8s
    枚举 + stat               31.2s
    现在这样(建 WalkEntry)    32.0s
    多线程 8 个               23.2s   1.38 倍

两件事:一是 `dir_entry.stat()` 在 Windows 上几乎免费 —— scandir 返回的目录项
里已经带着大小和时间,不会再去问一次文件系统(29.8 → 31.2 秒在噪声里);
省 stat 换不来速度。二是瓶颈在 I/O,而 os.scandir 会放开 GIL,所以多线程有用。
真正的大头是 OS 目录缓存冷热:同一份代码 81.2 秒 vs 29.8 秒,2.7 倍,
比任何代码层面的优化都大。用户抱怨的「太久」基本都是冷缓存那一次。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

# 重解析点标志:符号链接/联接点。不跟进,否则会重复计数甚至成环。
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# 默认线程数。8 是量出来的:再往上加,收益被目录锁和调度吃掉。
DEFAULT_WORKERS = 8


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


@dataclass(slots=True)
class _Level:
    """走完一层目录的产物。

    做成一个对象而不是回传一串位置参数:六个数按顺序传,加一个就要改三处,
    而且漏改不会报错、只会把数字对错位置。
    """

    rows: list[WalkEntry]
    subdirs: list[str]
    files: int = 0
    dirs: int = 0
    bytes_total: int = 0
    errors: int = 0
    reparse: int = 0


def _scan_one_dir(path: str, prefix_len: int) -> _Level:
    """走一层目录。

    只走一层、把子目录交回调度方 —— 不在这里递归。线程池里递归提交任务会死锁:
    池子满了之后,一个线程会等在自己提交的任务上,而没有线程能来跑它。

    这里**不取**目录的 NTFS 文件编号,虽然 USN 那边正需要它来还原路径。
    量过,太贵:DirEntry.inode() 在 Windows 上是按路径做 lstat,操作系统要从根
    逐段解析,而 C: 上净是 WinSxS、node_modules 这种深路径。整块 C: 上
    8 线程实测 68s → 165s(tools/bench_dir_paths.py)。
    改成读完日志再拿里面的父引用反查:2,572 个引用、0.23 秒、覆盖 87% 的事件,
    见 changes._resolve_by_id 和 docs/superpowers/plans/2026-08-30-usn-path-resolution.md。
    """
    rows: list[WalkEntry] = []
    subdirs: list[str] = []
    files = dirs = total_bytes = errors = 0
    reparse = 0

    try:
        it = os.scandir(path)
    except OSError:
        return _Level(rows, subdirs, errors=1)

    with it:
        while True:
            try:
                dir_entry = next(it)
            except StopIteration:
                break
            except OSError:
                errors += 1
                continue

            try:
                st = dir_entry.stat(follow_symlinks=False)
                attributes = getattr(st, "st_file_attributes", 0)
                is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                is_dir = dir_entry.is_dir(follow_symlinks=False)
            except OSError:
                errors += 1
                continue

            rel = dir_entry.path[prefix_len:]
            created = getattr(st, "st_birthtime", None) or st.st_ctime

            if is_dir:
                dirs += 1
                rows.append(
                    WalkEntry(
                        path=rel,
                        name=dir_entry.name,
                        is_dir=True,
                        bytes=0,
                        created=created,
                        modified=st.st_mtime,
                        attributes=attributes,
                    )
                )
                if is_reparse:
                    # 联接点/符号链接不跟进
                    reparse += 1
                else:
                    subdirs.append(dir_entry.path)
            else:
                size = 0 if is_reparse else st.st_size
                files += 1
                total_bytes += size
                rows.append(
                    WalkEntry(
                        path=rel,
                        name=dir_entry.name,
                        is_dir=False,
                        bytes=size,
                        created=created,
                        modified=st.st_mtime,
                        attributes=attributes,
                    )
                )
                if is_reparse:
                    reparse += 1

    return _Level(rows, subdirs, files, dirs, total_bytes, errors, reparse)


def walk_drive(
    drive: str,
    *,
    progress: Callable[[int], None] | None = None,
    progress_every: int = 20_000,
    workers: int = DEFAULT_WORKERS,
) -> tuple[list[WalkEntry], WalkStats]:
    """递归遍历整个盘。返回 (条目, 统计)。

    权限不足的目录直接跳过并计入 errors —— 一个 System Volume Information
    不该让整次扫描失败。

    workers 是并发读目录的线程数,1 表示退回单线程(测试用它跟多线程对结果)。
    整块 C: 上 8 个线程比 1 个快 1.38 倍,见模块开头。
    """
    root = drive.rstrip("\\") + "\\"
    stats = WalkStats()
    entries: list[WalkEntry] = []
    t0 = time.perf_counter()
    prefix_len = len(root)
    n_workers = max(1, int(workers))

    # 队列里放待走的目录。所有对 entries/stats 的写都在持锁时做 ——
    # list.append 本身是原子的,但 stats 的那几个 += 不是。
    # None 是收工信号,所以元素类型是 str | None
    pending: queue.Queue[str | None] = queue.Queue()
    pending.put(root)
    lock = threading.Lock()
    reported = 0            # 上次报进度时的条目数

    def consume(lv: _Level) -> None:
        """把一层的结果并进总账,并在跨过一个刻度时报一次进度。"""
        nonlocal reported
        with lock:
            entries.extend(lv.rows)
            stats.files += lv.files
            stats.dirs += lv.dirs
            stats.bytes_total += lv.bytes_total
            stats.errors += lv.errors
            stats.skipped_reparse += lv.reparse
            total = stats.files + stats.dirs
            # 进度用累计总数,不用各线程的局部数 —— 后者会让界面上的数字来回跳
            due = progress is not None and total - reported >= progress_every
            if due:
                reported = total
        for d in lv.subdirs:
            pending.put(d)
        if due:
            progress(total)          # 回调在锁外调,别让用户的函数拖住所有线程

    if n_workers == 1:
        while True:
            try:
                current = pending.get_nowait()
            except queue.Empty:
                break
            consume(_scan_one_dir(current, prefix_len))
    else:
        # 自己管线程,不用 ThreadPoolExecutor:任务是边走边长出来的,
        # 线程要在队列上等新活,而不是跑完一批就退出。
        #
        # 什么时候算走完,交给 Queue 自己记 —— 不要手写「队列空 + 没人在走」
        # 那种两个条件的判断。手写的版本有个很窄的窗口:A 刚取到最后一个目录、
        # 还没登记「我开始走了」,B 就看到队列空且没人在走,把所有线程停掉,
        # A 手上那棵子树整个丢掉 —— 不报错、不卡住,只是结果少几条。
        # 我拿 tools/stress_walk.py 跑 60 轮都没能复现出来,说明这种错靠测试
        # 兜不住,只能靠写法本身不给它机会。
        #
        # Queue 的 unfinished_tasks 在 put 时加、task_done 时减,而子目录是在
        # task_done 之前 put 进去的,所以「计数归零」严格意味着「没有活了,
        # 而且不会再长出新的活」。join() 等的就是它归零,判断只有一个条件,
        # 也不在我们手里。
        def worker() -> None:
            while True:
                current = pending.get()
                if current is None:            # 收工信号
                    pending.task_done()
                    return
                try:
                    consume(_scan_one_dir(current, prefix_len))
                finally:
                    pending.task_done()

        threads = [threading.Thread(target=worker, daemon=True,
                                    name=f"walk-{i}") for i in range(n_workers)]
        for th in threads:
            th.start()

        pending.join()                          # 等到没有未完成的目录
        for _ in threads:
            pending.put(None)                   # 让等在 get() 上的线程退出
        for th in threads:
            th.join()

    stats.duration_ms = int((time.perf_counter() - t0) * 1000)
    return entries, stats
