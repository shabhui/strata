"""量一件事:在 scandir 遍历里顺手取目录的 st_ino,要多付多少时间。

为什么值得单独量:Windows 上 `dir_entry.stat(follow_symlinks=False)` 几乎免费,
因为大小和时间戳本来就在目录枚举返回的数据里(见 walker.py 开头那组数)。
但**文件索引不在那份数据里** —— CPython 的 DirEntry.inode() 在 Windows 上会
另外走一次 LSTAT。整块 C: 有 29 万个目录,如果每个多一次开句柄,
就可能把关掉 MFT 换来的 26.6 秒又吃回去。

所以这里不是量绝对速度,是量**比值**:同一棵树,只 stat vs stat + inode()。

单线程跑,故意的。生产是 8 线程(walker.DEFAULT_WORKERS),多线程会把 I/O
等待互相盖掉,所以单线程量出来的比值是这笔开销的上界 —— 单线程都不痛,
生产就更不痛。反过来不成立,别用多线程的数去下结论。

用法:
    python tools/bench_inode.py [目标目录] [轮数]
默认 C:\\Windows,2 轮。交替跑 A/B,摊平缓存的偏差。
"""

from __future__ import annotations

import os
import sys
import time

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MASK48 = 0x0000FFFFFFFFFFFF


def walk(root: str, *, take_inode: bool) -> tuple[int, int, int]:
    """走一遍。返回 (目录数, 文件数, 取到的 inode 数)。

    形状照 walker._scan_one_dir 抄:每个条目一次 stat、算 is_reparse、
    联接点不跟进。唯一的变量是 take_inode。
    """
    dirs = files = inos = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            continue
        with it:
            while True:
                try:
                    de = next(it)
                except StopIteration:
                    break
                except OSError:
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    is_reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir:
                    files += 1
                    continue
                dirs += 1
                if is_reparse:
                    continue
                if take_inode:
                    try:
                        ino = de.inode() & MASK48
                    except OSError:
                        ino = 0
                    if ino:
                        inos += 1
                stack.append(de.path)
    return dirs, files, inos


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else r"C:\Windows"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    print(f"目标:{root}")
    print("先跑一遍热缓存(结果丢掉)……", flush=True)
    walk(root, take_inode=False)

    a_times: list[float] = []
    b_times: list[float] = []
    shape = None
    for i in range(rounds):
        t0 = time.perf_counter()
        d1, f1, _ = walk(root, take_inode=False)
        a = time.perf_counter() - t0

        t0 = time.perf_counter()
        d2, f2, inos = walk(root, take_inode=True)
        b = time.perf_counter() - t0

        a_times.append(a)
        b_times.append(b)
        shape = (d2, f2, inos)
        print(f"  第 {i + 1} 轮:只 stat {a:6.2f}s   stat+inode {b:6.2f}s", flush=True)
        if (d1, f1) != (d2, f2):
            print(f"  ! 两次走到的条目数不一样:{d1},{f1} vs {d2},{f2}")

    dirs, files, inos = shape or (0, 0, 0)
    a_best = min(a_times)
    b_best = min(b_times)
    print()
    print(f"目录 {dirs:,} 个,文件 {files:,} 个,取到 inode {inos:,} 个")
    print(f"只 stat      {a_best:6.2f}s")
    print(f"stat+inode   {b_best:6.2f}s   慢 {b_best - a_best:+.2f}s "
          f"({(b_best / a_best - 1) * 100:+.1f}%)")
    if dirs:
        per = (b_best - a_best) / dirs * 1e6
        print(f"折算每个目录多 {per:.2f} 微秒")
        print(f"按整块 C: 的 289,833 个目录估:多 {per * 289833 / 1e6:.1f}s(单线程)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
