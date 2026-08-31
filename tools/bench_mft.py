"""对真实卷跑一次 MFT 解析,核对结果并计时。需要管理员权限。

用法(管理员 PowerShell/CMD):
    python tools\\bench_mft.py C: D:

它不写数据库、不改动任何东西,只读、算、打印。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata.ntfs import mft
from strata.ntfs.volume import AccessDenied, NtfsError, Volume, volume_space


def gib(n: int) -> str:
    return f"{n / 2**30:,.2f} GiB"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def bench(drive: str) -> None:
    print(f"\n{'=' * 62}\n{drive} \n{'=' * 62}")
    total, free = volume_space(drive)
    used = total - free
    print(f"系统报告   总 {gib(total)}  可用 {gib(free)}  已用 {gib(used)}")

    t0 = time.perf_counter()
    with Volume(drive) as vol:
        boot = vol.boot
        print(
            f"卷几何     扇区 {boot.bytes_per_sector}  簇 {boot.bytes_per_cluster}  "
            f"MFT 记录 {boot.bytes_per_mft_record}  卷内扇区 {boot.total_sectors:,}"
        )
        reader = mft.MftReader(vol)
        runs = reader.mft_runs()
        mft_clusters = sum(r.length for r in runs if not r.sparse)
        print(
            f"MFT        {len(runs)} 个运行,{mft_clusters:,} 簇 "
            f"= {gib(mft_clusters * boot.bytes_per_cluster)},"
            f"约 {mft_clusters * boot.bytes_per_cluster // boot.bytes_per_mft_record:,} 条记录"
        )

        t_scan = time.perf_counter()
        last = [0.0]

        def progress(n: int) -> None:
            now = time.perf_counter()
            if now - last[0] > 1.0:
                last[0] = now
                print(f"  ... 已解析 {n:,} 条记录", end="\r", flush=True)

        entries = reader.read_entries(progress=progress)
        scan_s = time.perf_counter() - t_scan
        print(" " * 40, end="\r")

    st = reader.stats
    print(
        f"解析       {st.records_seen:,} 条有 FILE 标记 / {st.records_in_use:,} 在用  "
        f"耗时 {scan_s:.2f}s  ({st.records_seen / max(scan_s, 1e-6):,.0f} 条/秒)"
    )
    print(f"条目       文件 {st.files:,}  目录 {st.dirs:,}  扩展记录 {st.extension_records:,}")
    print(
        f"异常       fixup 失败 {st.fixup_failures:,}  解析失败 {st.parse_failures:,}  "
        f"无名字 {st.unnamed:,}"
    )

    t_paths = time.perf_counter()
    paths, pstats = mft.resolve_paths(entries)
    paths_s = time.perf_counter() - t_paths
    print(
        f"路径       {len(paths):,} 个目录路径  耗时 {paths_s:.2f}s  "
        f"孤立 {pstats.orphaned:,}  成环 {pstats.cycles:,}"
    )

    # 口径要和真实扫描一致:snapshot._mft_to_scan_entries 不计文件形态的元文件
    # (记录号 < 16)。不过滤的话这里永远报「差异偏大」—— 因为 $BadClus:$Bad 是
    # 稀疏流,allocated_size 按定义等于整卷容量,一条就把总数顶上去一整卷。
    # 实测本机 C: 不过滤是 397.19 GiB(系统已用 169.63 GiB,+134%),过滤后才对得上。
    # 一条永远会响的警告和永远通过的检查一样没用,而且它会把人往错的方向带。
    scanned = sum(e.bytes for e in entries if not e.is_dir and not e.is_metafile)
    meta = st.bytes_total - scanned
    delta = scanned - used
    pct = (delta / used * 100) if used else 0
    print(f"\n合计       MFT 累计 {gib(scanned)}   系统已用 {gib(used)}")
    print(f"           (另有元文件 {gib(meta)} 未计入,其中 $BadClus:$Bad 占一整卷)")
    print(f"差异       {gib(delta)}  ({pct:+.2f}%)")
    if abs(pct) < 3:
        print("           ✓ 在预期范围内(差异来自 MFT 自身、$LogFile、快照等)")
    else:
        print("           ⚠ 差异偏大,需要排查")

    # 抽查最大的十个文件,肉眼核对是否合理
    files = [e for e in entries if not e.is_dir and not e.is_metafile]
    files.sort(key=lambda e: e.bytes, reverse=True)
    print("\n最大的文件:")
    for e in files[:10]:
        parent = paths.get(e.parent)
        full = f"{drive}\\{parent}\\{e.name}" if parent else f"{drive}\\?\\{e.name}"
        print(f"  {e.bytes / 2**30:9,.2f} GiB  {full}")

    print(f"\n总耗时     {time.perf_counter() - t0:.2f}s")


def main() -> int:
    if not is_admin():
        print("需要管理员权限。请在管理员终端里运行:")
        print(r"    python tools\bench_mft.py C: D:")
        return 1

    drives = sys.argv[1:] or ["C:", "D:"]
    for drive in drives:
        try:
            bench(drive)
        except AccessDenied as exc:
            print(f"{drive} 拒绝访问: {exc}")
        except NtfsError as exc:
            print(f"{drive} NTFS 错误: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
