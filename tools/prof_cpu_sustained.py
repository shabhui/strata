"""这台机器持续满载会不会掉频 —— 判断前面那 5.8 倍是我的代码还是硬件。

prof_mft_perchunk.py 量到解析越跑越慢:前 5 块 9.1 µs/条,后 5 块 53.0 µs/条,
差 5.8 倍。已经排掉的:

    缺页    每块稳定 2,800 次,从头到尾不变 —— 不是换页
    长列表  不留条目也一样慢(44.4 µs/条)
    GC      gc.disable() 只省 5%

剩下唯一跟「已经跑了多久」相关的东西是 CPU 频率。睿频只能维持几秒,之后
掉回基频或被功耗墙压住。

这个工具跑一个**完全不分配内存**的纯整数循环:没有对象、没有 GC、没有缺页,
唯一的变量就是 CPU 快慢。它要是也掉 5~6 倍,那前面量到的就不是我的代码的问题。

    python tools/prof_cpu_sustained.py

不需要管理员权限。跑约 80 秒(和一次真实 MFT 解析同量级)。
"""

from __future__ import annotations

import time

SLICE_ITERS = 2_000_000      # 一片大约 0.1~0.5 秒
TARGET_SECS = 80.0


def burn(n: int) -> int:
    """纯整数运算,不分配任何对象。

    局部变量、小整数、算术 —— CPython 里这条路上不会新建堆对象
    (小整数有缓存),所以不会触发 GC,也不会有缺页。
    """
    x = 0
    for i in range(n):
        x = (x * 31 + i) & 0xFFFFFF
    return x


def main() -> int:
    print(f"纯整数循环,每片 {SLICE_ITERS:,} 次,总共约 {TARGET_SECS:.0f} 秒")
    print("不分配对象、不触发 GC、不缺页 —— 唯一的变量是 CPU 频率\n")
    print(f"{'片':>4} {'累计秒':>8} {'本片秒':>8} {'相对第一片':>11}")

    marks: list[float] = []
    t0 = time.perf_counter()
    i = 0
    while time.perf_counter() - t0 < TARGET_SECS:
        t = time.perf_counter()
        burn(SLICE_ITERS)
        secs = time.perf_counter() - t
        marks.append(secs)
        elapsed = time.perf_counter() - t0
        if i < 5 or i % 20 == 0:
            rel = secs / marks[0]
            print(f"{i:>4} {elapsed:>8.1f} {secs:>8.3f} {rel:>10.2f}x")
        i += 1

    first = sum(marks[:5]) / 5
    last = sum(marks[-5:]) / 5
    print(f"\n共 {len(marks)} 片,{time.perf_counter() - t0:.1f} 秒")
    print(f"前 5 片平均 {first * 1000:.0f} ms")
    print(f"后 5 片平均 {last * 1000:.0f} ms")
    print(f"末尾比开头慢 {last / first:.2f}x")

    print()
    if last / first > 1.5:
        print(f"→ 这台机器持续满载会掉 {last / first:.1f}x。")
        print("  解析那 5.8 倍里有一部分(或全部)是硬件,不是代码。")
        print("  以后量长任务必须分段看,只报总时间会把掉频算进代码头上。")
    else:
        print("→ 这台机器持续满载不掉频(只慢 "
              f"{last / first:.2f}x)。")
        print("  那解析那 5.8 倍是代码自己的问题,得继续往下查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
