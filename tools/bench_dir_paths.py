"""量 dir_paths 这个出参在生产配置下要多花多少 —— 时间和内存都要。

调的是真 walker.walk_drive,不是照抄的复制品(tools/bench_walk.py 里那种
复制品会漂,我已经在那儿栽过一次:标签写着 v_current,其实是单线程,
算出个 2.2 倍的假余量)。这里不传 workers,吃 DEFAULT_WORKERS = 8,
跟 snapshot.py 调它的方式一模一样。

为什么单独量:取目录编号在 Windows 上不免费。大小和时间戳本来就在 scandir
返回的目录项里,文件编号不在 —— DirEntry.inode() 是另一次系统调用。
tools/bench_inode.py 在 System32 上单线程量到每个目录约 27 微秒,
按整盘 29 万个目录折算是 +7.8s。但那是单线程;这个调用是 I/O,会放开 GIL,
8 个线程理论上能盖掉大部分。理论不算数,所以在整盘上跑一遍。

顺序:先不开、再开、再不开、再开……交替,摊平缓存冷热的偏差。

用法:
    python tools/bench_dir_paths.py [盘] [轮数]
默认 C:,2 轮。不写库,只遍历。
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.scan import walker  # noqa: E402


def run(drive: str, *, want: bool, trace: bool = False) -> tuple[float, int, int, int]:
    """跑一遍。返回 (秒, 条目数, 目录表条数, 峰值字节;不 trace 时峰值为 0)。

    trace 默认关,而且**测时间时必须关**。tracemalloc 会拦每一次内存分配,
    而开 dir_paths 那条路本来就多分配(每个目录一个元组、一条 dict 项),
    于是它把要量的差值自己放大了一遍。我第一版两件事在同一次跑里量,
    得出 +47% —— 同时基线也从别处量到的 26.6s 涨到 42.3s,那就是 trace 的钱。
    时间和内存分两轮量,别让工具变成被测对象的一部分。
    """
    bag: dict[int, str] | None = {} if want else None
    peak = 0
    if trace:
        tracemalloc.start()
    t0 = time.perf_counter()
    entries, stats = walker.walk_drive(drive, dir_paths=bag)
    dt = time.perf_counter() - t0
    if trace:
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    n = len(entries)
    mapped = len(bag) if bag is not None else 0
    del entries, bag
    return dt, n, mapped, peak


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 2

    print(f"目标 {drive}  线程数 {walker.DEFAULT_WORKERS}(生产默认)")
    print("先跑一遍把目录缓存热起来,结果丢掉……", flush=True)
    run(drive, want=False)

    off: list[float] = []
    on: list[float] = []
    shape = (0, 0)
    for i in range(rounds):
        a, n1, _, _ = run(drive, want=False)
        b, n2, mapped, _ = run(drive, want=True)
        off.append(a)
        on.append(b)
        shape = (n2, mapped)
        print(f"  第 {i + 1} 轮:不开 {a:6.2f}s   开 {b:6.2f}s"
              f"   条目 {n1:,}/{n2:,}", flush=True)

    n, mapped = shape
    a_best, b_best = min(off), min(on)
    print()
    print(f"条目 {n:,} 个,目录表 {mapped:,} 条")
    print(f"不开 dir_paths   {a_best:6.2f}s")
    print(f"开 dir_paths     {b_best:6.2f}s")
    print(f"代价             {b_best - a_best:+6.2f}s "
          f"({(b_best / a_best - 1) * 100:+.1f}%)")
    if mapped:
        print(f"折算每个目录多 {(b_best - a_best) / mapped * 1e6:.2f} 微秒")

    print("\n=== 内存单独一轮(开 tracemalloc,时间不作数)===", flush=True)
    _t, _n, _m, pa = run(drive, want=False, trace=True)
    _t, _n, mapped2, pb = run(drive, want=True, trace=True)
    print(f"不开   峰值 {pa / 2**20:7.1f} MiB")
    print(f"开     峰值 {pb / 2**20:7.1f} MiB   "
          f"多 {(pb - pa) / 2**20:+.1f} MiB / {mapped2:,} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
