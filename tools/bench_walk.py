"""量 scandir 遍历的时间花在哪:I/O、stat、还是建对象。

只读盘,不写库、不删东西。跑一次约几分钟。

  python tools/bench_walk.py C:

四个变体跑同一棵树,比的是彼此的比值 —— 绝对值受 OS 目录缓存影响很大
(实测同一块盘冷缓存 142 秒、热缓存 38 秒),单看一个数说明不了问题。
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(slots=True)
class E:
    path: str
    name: str
    is_dir: bool
    bytes: int = 0
    created: float | None = None
    modified: float | None = None
    attributes: int = 0


def v_enum_only(root: str) -> int:
    """只枚举:scandir + is_dir。不 stat、不建对象。"""
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for de in it:
                n += 1
                try:
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                except OSError:
                    pass
    return n


def v_enum_stat(root: str) -> int:
    """枚举 + stat。看 stat 在 Windows 上是不是真的免费。"""
    n = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for de in it:
                n += 1
                try:
                    st = de.stat(follow_symlinks=False)
                    _ = st.st_size, st.st_mtime, st.st_file_attributes
                    if de.is_dir(follow_symlinks=False):
                        stack.append(de.path)
                except OSError:
                    pass
    return n


def v_single_thread(root: str) -> int:
    """walker.py 的单个工作线程在干的活,逐字照搬包括那两个 getattr。

    注意这**不是**生产在跑的东西:snapshot.py 调 walk_drive 时不传 workers,
    吃的是 DEFAULT_WORKERS = 8。这一行只是单线程基线,拿它当「现在的性能」
    会算出一个 2.2x 的假余量 —— 那个余量早就吃掉了。
    """
    out: list[E] = []
    prefix = len(root)
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=False)
                    attributes = getattr(st, "st_file_attributes", 0)
                    is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                rel = de.path[prefix:]
                if is_dir:
                    out.append(E(rel, de.name, True, 0,
                                 getattr(st, "st_birthtime", None) or st.st_ctime,
                                 st.st_mtime, attributes))
                    if not is_reparse:
                        stack.append(de.path)
                else:
                    out.append(E(rel, de.name, False, 0 if is_reparse else st.st_size,
                                 getattr(st, "st_birthtime", None) or st.st_ctime,
                                 st.st_mtime, attributes))
    return len(out)


def v_threaded(root: str, workers: int = 8) -> int:
    """多线程枚举。os.scandir 会放开 GIL,所以线程对 I/O 有用。

    每个任务只处理一层目录,把子目录交回池子 —— 不递归提交,避免线程等线程
    (池子满了之后,一个线程等自己提交的任务会死锁)。
    """
    out: list[E] = []
    prefix = len(root)

    def one_level(path: str) -> tuple[list[E], list[str]]:
        rows: list[E] = []
        subdirs: list[str] = []
        try:
            it = os.scandir(path)
        except OSError:
            return rows, subdirs
        with it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=False)
                    attributes = st.st_file_attributes
                    is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                rel = de.path[prefix:]
                if is_dir:
                    rows.append(E(rel, de.name, True, 0, st.st_ctime, st.st_mtime, attributes))
                    if not is_reparse:
                        subdirs.append(de.path)
                else:
                    rows.append(E(rel, de.name, False, 0 if is_reparse else st.st_size,
                                  st.st_ctime, st.st_mtime, attributes))
        return rows, subdirs

    pending = [root]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending:
            batch, pending = pending, []
            for rows, subs in pool.map(one_level, batch):
                out.extend(rows)
                pending.extend(subs)
    return len(out)


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    root = drive + "\\"
    workers = int(os.environ.get("WALK_WORKERS", "8"))

    baseline = f"生产(多线程 {workers} 个)"
    variants = [
        ("只枚举(不 stat 不建对象)", lambda: v_enum_only(root)),
        ("枚举 + stat", lambda: v_enum_stat(root)),
        ("单线程基线(非生产)", lambda: v_single_thread(root)),
        (baseline, lambda: v_threaded(root, workers)),
    ]

    print(f"盘 {root}  线程数 {workers}")
    print("注意:缓存越跑越热,靠后的变体占便宜。要比就跑两轮看第二轮。")
    print("基准是多线程那一行 —— 生产就是这么跑的(snapshot.py 不传 workers,"
          "吃 DEFAULT_WORKERS=8)。拿单线程当基准会算出一个已经吃掉的余量。\n")

    results = []
    for round_no in (1, 2):
        print(f"--- 第 {round_no} 轮 ---")
        for label, fn in variants:
            t = time.perf_counter()
            n = fn()
            dt = time.perf_counter() - t
            print(f"  {label:<26} {dt:>6.1f}s  条目 {n:,}")
            if round_no == 2:
                results.append((label, dt, n))

    base = next((d for lab, d, _ in results if lab == baseline), None)
    if base:
        print(f"\n第二轮相对「{baseline}」:")
        for label, dt, _ in results:
            print(f"  {label:<26} {base / dt:>5.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
