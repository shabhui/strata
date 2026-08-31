"""稀疏流的真实占用:allocated_size 靠不住,走一遍运行列表才准。

来路:WOF 修完之后 mft 那条路仍然虚报 +20.1%,probe_wof_shapes.py 排出来
最大的单个「文件」是 $UsnJrnl —— 39.83 GiB,比剩下的整个差额(34.08G)还大。
那是 USN 变更日志,一个环形缓冲:旧区间被释放掉,只有活动窗口真占盘。
它的数据在名为 $J 的备用流里,allocated_size 报的是整个逻辑区间。

这引出一个比「按流名认 WOF」更根本的修法:**带稀疏标记的流,不要相信
allocated_size,把运行列表走一遍,只把真正分配了簇的段加起来。**

如果这条对 $UsnJrnl:$J 和 WOF 的幻影流都成立,那就不用按名字认 WOF 了 ——
幻影流本身就是稀疏的,走运行列表自然得 0。一条通用规则替掉一个特例。

这个工具对几个已知文件,把每条 $DATA 的三个数并排打出来:

    allocated_size      属性头里写的
    运行列表里的实际段    只数非稀疏段(lcn 不为空)的簇
    GetCompressedFileSizeW   系统给的真实占盘,判据

⚠ 需要管理员权限。只读。

    tools\\run_elevated.bat probe_runlist_truth.py
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs.mft import MftReader  # noqa: E402
from strata.ntfs.runlist import decode_runlist  # noqa: E402
from strata.ntfs.volume import AccessDenied, Volume  # noqa: E402

MIB = 2**20
GIB = 2**30
INVALID_FILE_SIZE = 0xFFFFFFFF

# $UsnJrnl 没有普通路径能打开,靠记录号找;其余用路径拿记录号。
BY_PATH = [
    r"C:\Windows\System32\kernel32.dll",
    r"C:\Windows\notepad.exe",
    r"C:\hiberfil.sys",
    r"C:\pagefile.sys",
]
BY_NAME = ["$UsnJrnl"]        # 在 $Extend 下,扫 MFT 找名字


def real_disk_bytes(path: str) -> int | None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = k32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == INVALID_FILE_SIZE and ctypes.get_last_error() != 0:
        return None
    return (high.value << 32) | low


def record_number_of(path: str) -> int | None:
    from probe_wof import record_number_of as inner   # 复用,别写两遍

    try:
        return inner(path)[0]
    except OSError:
        return None


def runlist_real_bytes(buf: bytearray, attr, off: int, bpc: int) -> tuple[int, int, int]:
    """走运行列表,返回 (真实分配字节, 段数, 稀疏段数)。"""
    if not attr.non_resident:
        return attr.value_length, 0, 0
    start = off + attr.runlist_offset
    end = off + attr.length
    try:
        runs = decode_runlist(memoryview(buf)[start:end])
    except Exception:                                    # noqa: BLE001
        return -1, 0, 0
    real = 0
    sparse = 0
    for run in runs:
        if run.lcn is None:
            sparse += 1
            continue
        real += run.length * bpc
    return real, len(runs), sparse


def dump_record(vol: Volume, reader: MftReader, record: int, label: str,
                path: str | None) -> None:
    rec_size = reader.record_size
    bpc = vol.boot.bytes_per_cluster
    off = None
    seen = 0
    want = record * rec_size
    for run in reader.mft_runs():
        span = run.length * bpc
        if run.lcn is None:
            seen += span
            continue
        if seen + span > want:
            off = run.lcn * bpc + (want - seen)
            break
        seen += span
    if off is None:
        print(f"  定位不到记录 {record}")
        return
    raw = bytearray(vol.read(off, rec_size))
    if bytes(raw[0:4]) != A.MAGIC_FILE:
        print("  没有 FILE 标记")
        return
    A.apply_fixups(raw, 0, rec_size, vol.boot.bytes_per_sector)
    header = A.parse_record_header(raw)

    alloc_total = 0
    runlist_total = 0
    for attr, ao in A.iter_attributes(raw, header, 0, rec_size):
        if attr.type_code != A.ATTR_DATA:
            continue
        size = A.parse_data_size(raw, attr, ao)
        if size is None:
            continue
        name = A.attribute_name(raw, attr, ao)
        real, nruns, nsparse = runlist_real_bytes(raw, attr, ao, bpc)
        tag = f'"{name}"' if name else "(未命名)"
        marks = []
        if attr.sparse:
            marks.append("稀疏位")
        if attr.compressed:
            marks.append("压缩位")
        alloc_total += size.allocated
        if real >= 0:
            runlist_total += real
        print(f"    $DATA{tag:<22} allocated {size.allocated/MIB:>10,.2f}M  "
              f"运行列表 {real/MIB:>10,.2f}M  段 {nruns}(稀疏 {nsparse})"
              f"  {','.join(marks)}")

    print(f"    合计  allocated {alloc_total/MIB:>10,.2f}M   "
          f"运行列表 {runlist_total/MIB:>10,.2f}M")
    if path:
        actual = real_disk_bytes(path)
        if actual is not None:
            print(f"    系统报真实占盘 {actual/MIB:>10,.2f}M   ← 判据")
            for what, value in (("allocated", alloc_total),
                                ("运行列表", runlist_total)):
                mark = "✓ 对上" if abs(value - actual) <= 2 * bpc else "✗"
                print(f"      {what:<10} {mark}")


def find_by_name(vol: Volume, reader: MftReader, names: set[str]) -> dict[str, int]:
    """扫一遍 MFT,按名字找记录号。$UsnJrnl 这类没有普通路径。"""
    found: dict[str, int] = {}
    rec_size = reader.record_size
    bpc = vol.boot.bytes_per_cluster
    scratch = bytearray(8192 * rec_size)
    index = 0
    for run in reader.mft_runs():
        if run.lcn is None:
            index += (run.length * bpc) // rec_size
            continue
        run_bytes = run.length * bpc
        base = run.lcn * bpc
        done = 0
        while done < run_bytes and len(found) < len(names):
            take = min(len(scratch), run_bytes - done)
            take -= take % rec_size
            if take <= 0:
                break
            got = vol.read_into(base + done, take, scratch)
            if not got:
                break
            for i in range(got // rec_size):
                o = i * rec_size
                if bytes(scratch[o : o + 4]) != A.MAGIC_FILE:
                    continue
                try:
                    A.apply_fixups(scratch, o, rec_size, vol.boot.bytes_per_sector)
                    header = A.parse_record_header(scratch, o)
                except Exception:                        # noqa: BLE001
                    continue
                if not header.in_use or header.is_extension:
                    continue
                for attr, ao in A.iter_attributes(scratch, header, o, rec_size):
                    if attr.type_code != A.ATTR_FILE_NAME:
                        continue
                    info = A.parse_file_name(scratch, attr, ao)
                    if info is not None and info.name in names:
                        found.setdefault(info.name, index + i)
            index += got // rec_size
            done += got
    return found


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        vol = Volume("C:")
    except AccessDenied as exc:
        print(f"打不开裸卷:{exc}\n必须提权跑。")
        return 2

    with vol:
        reader = MftReader(vol)
        for path in BY_PATH:
            rec = record_number_of(path)
            print(f"\n{path}")
            if rec is None:
                print("  打不开(可能正在被系统独占)")
                continue
            print(f"  记录号 {rec:,}")
            dump_record(vol, reader, rec, path, path)

        if BY_NAME:
            print(f"\n扫 MFT 找 {', '.join(BY_NAME)} ...")
            hits = find_by_name(vol, reader, set(BY_NAME))
            for name in BY_NAME:
                rec = hits.get(name)
                print(f"\n{name}")
                if rec is None:
                    print("  没找到")
                    continue
                print(f"  记录号 {rec:,}")
                dump_record(vol, reader, rec, name, None)

    print("\n要看的是:稀疏流那几行,「运行列表」是否比 allocated 小很多,")
    print("以及有路径的那些里「运行列表」能不能对上系统报的真实占盘。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
