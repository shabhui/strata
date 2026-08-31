"""WOF 压缩的文件,在 MFT 里长什么样。

背景:两条扫描路都把 C: 虚报 27 GB(+15.9%)。拿 GetCompressedFileSizeW
(它返回真实占盘字节)对最大的 378 个文件比过,342 个被高估。元凶不只是
标了 COMPRESSED 的那些:

    Sessions.xml    逻辑 137.9M  实际  17.8M  7.7x  有 COMPRESSED 位
    msedge.dll      逻辑 327.7M  实际 185.4M  1.8x  没有
    kernel32.dll    逻辑   0.8M  实际   0.4M  1.8x  没有
    notepad.exe     逻辑   0.3M  实际   0.2M  1.6x  没有

没有 COMPRESSED 位却压着,这是 Compact OS 的 WOF(Windows Overlay Filter)
压缩:真实数据搬进一条名为 WofCompressedData 的备用流,主数据流变成重解析点,
而整件事对普通 API 保持透明 —— 故意不设 COMPRESSED 位,免得老程序犯错。
这也解释了为什么前 378 个大文件只占那 27 GB 的 20%:系统里几十万个
二进制文件全被压着,差额摊得很薄。

修法取决于 MFT 里能读到什么,所以这个工具回答三个问题:

    1. 未命名 $DATA 的 allocated_size 报的是逻辑大小、还是 0(稀疏)
    2. 有没有 WofCompressedData 那条流,它的 allocated_size 是不是真实占用
    3. 重解析点标记是不是 IO_REPARSE_TAG_WOF(0x80000017)

要是 (2) 成立,修起来就是「认出 WOF 就改用备用流的大小」,几行的事,
而且不用为 115 万个文件各调一次系统 API(那个代价挡在门外)。
要是未命名流报的就是逻辑大小、备用流又读不到,那 MFT 这条路没法自己算准。

⚠ 需要管理员权限:直读裸卷。只读 —— 不写库、不改文件、不碰快照。

    tools\\run_elevated.bat probe_wof.py
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
from strata.ntfs.volume import AccessDenied, Volume  # noqa: E402

IO_REPARSE_TAG_WOF = 0x80000017
ATTR_REPARSE_POINT = 0xC0        # attributes.py 里没定义,只有这里用得到
MIB = 2**20

ATTR_NAMES = {
    0x10: "$STANDARD_INFORMATION",
    0x20: "$ATTRIBUTE_LIST",
    0x30: "$FILE_NAME",
    0x40: "$OBJECT_ID",
    0x50: "$SECURITY_DESCRIPTOR",
    0x60: "$VOLUME_NAME",
    0x70: "$VOLUME_INFORMATION",
    0x80: "$DATA",
    0x90: "$INDEX_ROOT",
    0xA0: "$INDEX_ALLOCATION",
    0xB0: "$BITMAP",
    0xC0: "$REPARSE_POINT",
    0x100: "$LOGGED_UTILITY_STREAM",
}


def attr_label(code: int) -> str:
    return ATTR_NAMES.get(code, f"0x{code:X}")

GENERIC_READ = 0
FILE_SHARE_ALL = 0x07
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE = ctypes.c_void_p(-1).value
INVALID_FILE_SIZE = 0xFFFFFFFF

TARGETS = [
    r"C:\Windows\System32\kernel32.dll",
    r"C:\Windows\notepad.exe",
    r"C:\Windows\explorer.exe",
    r"C:\Windows\servicing\Sessions\Sessions.xml",
]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def record_number_of(path: str) -> tuple[int, int]:
    """(MFT 记录号, 逻辑字节数)。

    文件索引的低 48 位就是 MFT 记录号,高 16 位是序列号 —— 掩掉。
    GENERIC_READ 传 0(只问元数据,不读内容),所以受保护的系统文件也开得了。
    OPEN_REPARSE_POINT 是关键:WOF 文件的主流是重解析点,不加这个标志会被
    过滤器接管、透明解压,那就问不到底下的实情了。
    """
    k32 = _k32()
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(
        path, GENERIC_READ, FILE_SHARE_ALL, None, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == INVALID_HANDLE:
        raise OSError(f"打不开:Windows 错误 {ctypes.get_last_error()}")
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(f"问不到文件信息:错误 {ctypes.get_last_error()}")
        index = (info.nFileIndexHigh << 32) | info.nFileIndexLow
        size = (info.nFileSizeHigh << 32) | info.nFileSizeLow
        return index & 0x0000FFFFFFFFFFFF, size
    finally:
        k32.CloseHandle(handle)


def real_disk_bytes(path: str) -> int | None:
    k32 = _k32()
    k32.GetCompressedFileSizeW.argtypes = [wintypes.LPCWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
    k32.GetCompressedFileSizeW.restype = wintypes.DWORD
    high = wintypes.DWORD(0)
    low = k32.GetCompressedFileSizeW(path, ctypes.byref(high))
    if low == INVALID_FILE_SIZE and ctypes.get_last_error() != 0:
        return None
    return (high.value << 32) | low


def record_offset(reader: MftReader, record: int) -> int | None:
    """记录号 → 盘上字节偏移。MFT 本身可能碎成多段,得按运行列表走。"""
    bpc = reader.boot.bytes_per_cluster
    want = record * reader.record_size
    seen = 0
    for run in reader.mft_runs():
        span = run.length * bpc
        if run.lcn is None:                     # 稀疏段,跳过但要计长度
            seen += span
            continue
        if seen + span > want:
            return run.lcn * bpc + (want - seen)
        seen += span
    return None


def read_record(vol: Volume, reader: MftReader, record: int) -> bytearray | None:
    off = record_offset(reader, record)
    if off is None:
        return None
    raw = bytearray(vol.read(off, reader.record_size))
    if bytes(raw[0:4]) != A.MAGIC_FILE:
        return None
    A.apply_fixups(raw, 0, reader.record_size, vol.boot.bytes_per_sector)
    return raw


def attribute_list_entries(raw: bytearray, attr, off: int) -> list[tuple[int, str, int, int]]:
    """解 $ATTRIBUTE_LIST,产出 (类型, 名字, 起始VCN, 所在记录号)。

    属性多到一条 MFT 记录装不下时,NTFS 把它们搬到扩展记录里,基记录留一张
    $ATTRIBUTE_LIST 当索引。这张表就是「未命名 $DATA 到底在哪条记录上」的答案。
    """
    out: list[tuple[int, str, int, int]] = []
    if attr.non_resident:            # 极少见,这里不追非常驻的属性列表
        return out
    base = off + attr.value_offset
    end = base + attr.value_length
    pos = base
    while pos + 0x1A <= end:
        code = int.from_bytes(bytes(raw[pos : pos + 4]), "little")
        length = int.from_bytes(bytes(raw[pos + 4 : pos + 6]), "little")
        if length < 0x1A or pos + length > end:
            break
        name_len = raw[pos + 6]
        name_off = raw[pos + 7]
        start_vcn = int.from_bytes(bytes(raw[pos + 8 : pos + 16]), "little")
        ref = int.from_bytes(bytes(raw[pos + 16 : pos + 24]), "little")
        name = ""
        if name_len:
            ns = pos + name_off
            name = bytes(raw[ns : ns + name_len * 2]).decode("utf-16-le", "replace")
        out.append((code, name, start_vcn, ref & 0x0000FFFFFFFFFFFF))
        pos += length
    return out


def dump(vol: Volume, reader: MftReader, path: str) -> None:
    try:
        record, logical = record_number_of(path)
    except OSError as exc:
        print(f"  取记录号失败:{exc}")
        return
    print(f"  MFT 记录号 {record:,}   逻辑大小 {logical / MIB:,.2f}M")

    raw = read_record(vol, reader, record)
    if raw is None:
        print(f"  定位不到记录 {record},或那个偏移上没有 FILE 标记")
        return

    unnamed_alloc = 0
    wof_alloc = 0
    extension_records: list[int] = []

    def scan(buf: bytearray, tag: str) -> None:
        nonlocal unnamed_alloc, wof_alloc
        header = A.parse_record_header(buf)
        for attr, off in A.iter_attributes(buf, header, 0, reader.record_size):
            code = attr.type_code
            if code == A.ATTR_DATA:
                name = A.attribute_name(buf, attr, off)
                size = A.parse_data_size(buf, attr, off)
                label = f'$DATA:"{name}"' if name else "$DATA(未命名)"
                if size is None:
                    print(f"  {tag}{label:<26} 后续片段(lowest_vcn≠0),不带大小")
                    continue
                marks = []
                if attr.compressed:
                    marks.append("压缩位")
                if attr.sparse:
                    marks.append("稀疏位")
                if size.resident:
                    marks.append("常驻")
                tail = ("  " + ",".join(marks)) if marks else ""
                print(f"  {tag}{label:<26} allocated {size.allocated / MIB:>9,.2f}M"
                      f"  real {size.real / MIB:>9,.2f}M{tail}")
                if name.lower() == "wofcompresseddata":
                    wof_alloc += size.allocated
                elif not name:
                    unnamed_alloc += size.allocated
            elif code == ATTR_REPARSE_POINT:
                base = off + attr.value_offset
                if attr.value_length >= 4:
                    t = int.from_bytes(bytes(buf[base : base + 4]), "little")
                    note = "  ← IO_REPARSE_TAG_WOF" if t == IO_REPARSE_TAG_WOF else ""
                    print(f"  {tag}$REPARSE_POINT 标记 0x{t:08X}{note}")
            elif code == A.ATTR_ATTRIBUTE_LIST:
                print(f"  {tag}$ATTRIBUTE_LIST —— 属性摊到别的记录上了:")
                for c, nm, vcn, ref in attribute_list_entries(buf, attr, off):
                    shown = f'"{nm}"' if nm else "无名"
                    here = "(本记录)" if ref == record else f"→ 记录 {ref:,}"
                    print(f"  {tag}  {attr_label(c):<24} {shown:<20} "
                          f"VCN {vcn:<6} {here}")
                    if ref != record and ref not in extension_records:
                        extension_records.append(ref)
            else:
                print(f"  {tag}{attr_label(code)}")

    scan(raw, "")

    for ext in extension_records:
        print(f"  --- 扩展记录 {ext:,} ---")
        ext_raw = read_record(vol, reader, ext)
        if ext_raw is None:
            print("      读不到")
            continue
        scan(ext_raw, "    ")

    actual = real_disk_bytes(path)
    if actual is None:
        print("  GetCompressedFileSizeW 取不到")
        return
    print(f"  真实占盘(GetCompressedFileSizeW)  {actual / MIB:>9,.2f}M")

    # _parse_record 现在的算法:未命名的 allocated,和所有备用流的 allocated 相加;
    # 跨记录时 _merge_pending 再取两者的较大值。两个数都打出来,好看清差在哪。
    summed = unnamed_alloc + wof_alloc
    print(f"  未命名 + 备用流(相加)           {summed / MIB:>9,.2f}M"
          f"{f'   ← 高估 {summed / actual:.1f}x' if actual and summed > actual else ''}")
    print(f"  只算 WofCompressedData            {wof_alloc / MIB:>9,.2f}M"
          f"{'   ← 等于真实占盘' if wof_alloc and abs(wof_alloc - actual) <= 65536 else ''}")


def main() -> int:
    try:
        vol = Volume("C:")
    except AccessDenied as exc:
        print(f"打不开裸卷:{exc}")
        print("这个工具必须用管理员权限跑:tools\\run_elevated.bat probe_wof.py")
        return 2

    with vol:
        reader = MftReader(vol)
        for path in TARGETS:
            print(f"\n{path}")
            dump(vol, reader, path)

    print("\n看三件事:")
    print("  1. 未命名 $DATA 的 allocated 是逻辑大小还是 0")
    print("  2. 有没有 WofCompressedData 流,它的 allocated 是否等于真实占盘")
    print("  3. 重解析点标记是不是 0x80000017")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
