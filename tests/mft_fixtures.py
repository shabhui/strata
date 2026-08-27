"""合成 MFT 记录与引导扇区,供解析测试使用。

按磁盘上的真实布局构造(含更新序列数组替换),
这样解析器必须做对 fixup 还原才能通过测试。
"""

from __future__ import annotations

import struct

FILETIME_EPOCH_DIFF = 11_644_473_600
FILETIME_TICKS = 10_000_000


def unix_to_filetime(ts: float) -> int:
    return int((ts + FILETIME_EPOCH_DIFF) * FILETIME_TICKS)


def pad8(data: bytes) -> bytes:
    """属性按 8 字节对齐。"""
    remainder = len(data) % 8
    return data + b"\x00" * (8 - remainder) if remainder else data


def make_boot_sector(
    *,
    bytes_per_sector: int = 512,
    sectors_per_cluster: int = 8,
    total_sectors: int = 1_000_000,
    mft_cluster: int = 786_432,
    mft_mirror_cluster: int = 2,
    clusters_per_mft_record: int = 0xF6,  # -10 → 2**10 = 1024 字节
    oem: bytes = b"NTFS    ",
    serial: int = 0x1122334455667788,
) -> bytes:
    buf = bytearray(512)
    buf[0:3] = b"\xeb\x52\x90"
    buf[3:11] = oem
    struct.pack_into("<H", buf, 11, bytes_per_sector)
    buf[13] = sectors_per_cluster
    struct.pack_into("<Q", buf, 40, total_sectors)
    struct.pack_into("<Q", buf, 48, mft_cluster)
    struct.pack_into("<Q", buf, 56, mft_mirror_cluster)
    buf[64] = clusters_per_mft_record
    struct.pack_into("<Q", buf, 72, serial)
    buf[510:512] = b"\x55\xaa"
    return bytes(buf)


def attr_standard_information(
    *,
    created: float = 1_700_000_000.0,
    modified: float = 1_700_500_000.0,
    mft_changed: float = 1_700_500_000.0,
    accessed: float = 1_700_600_000.0,
    attributes: int = 0x20,
) -> bytes:
    value = struct.pack(
        "<QQQQI",
        unix_to_filetime(created),
        unix_to_filetime(modified),
        unix_to_filetime(mft_changed),
        unix_to_filetime(accessed),
        attributes,
    )
    value = pad8(value)
    header = struct.pack(
        "<IIBBHHHIH2x",
        0x10,               # 类型
        24 + len(value),    # 总长
        0,                  # 常驻
        0,                  # 名字长度
        0,                  # 名字偏移
        0,                  # 标志
        0,                  # 属性 ID
        len(value),         # 值长度
        24,                 # 值偏移
    )
    return header + value


def attr_file_name(
    *,
    name: str,
    parent: int = 5,
    namespace: int = 3,
    allocated_hint: int = 0,
    real_hint: int = 0,
    attributes: int = 0x20,
    created: float = 1_700_000_000.0,
    modified: float = 1_700_500_000.0,
    attr_id: int = 1,
) -> bytes:
    encoded = name.encode("utf-16-le")
    value = struct.pack(
        "<QQQQQQQIIBB",
        parent,
        unix_to_filetime(created),
        unix_to_filetime(modified),
        unix_to_filetime(modified),
        unix_to_filetime(modified),
        allocated_hint,
        real_hint,
        attributes,
        0,                  # reparse
        len(name),
        namespace,
    ) + encoded
    value = pad8(value)
    header = struct.pack(
        "<IIBBHHHIH2x",
        0x30,
        24 + len(value),
        0,
        0,
        0,
        0,
        attr_id,
        len(value),
        24,
    )
    return header + value


def attr_data_resident(*, payload: bytes = b"hello", attr_id: int = 2, name: str = "") -> bytes:
    name_encoded = name.encode("utf-16-le")
    name_offset = 24 + len(name_encoded)
    # 值紧跟在名字后面,整体对齐
    prefix_len = name_offset
    pad = (8 - prefix_len % 8) % 8
    value_offset = prefix_len + pad
    value = payload
    total = value_offset + len(value)
    total += (8 - total % 8) % 8
    header = struct.pack(
        "<IIBBHHHIH2x",
        0x80,
        total,
        0,
        len(name),
        24 if name else 0,
        0,
        attr_id,
        len(value),
        value_offset,
    )
    body = name_encoded + b"\x00" * pad + value
    body += b"\x00" * (total - 24 - len(body))
    return header + body


def attr_data_nonresident(
    *,
    allocated: int,
    real: int,
    runlist: bytes = b"\x21\x10\x00\x01\x00",
    attr_id: int = 2,
    name: str = "",
    lowest_vcn: int = 0,
    highest_vcn: int | None = None,
    flags: int = 0,
) -> bytes:
    if highest_vcn is None:
        highest_vcn = max(0, (allocated // 4096) - 1)
    name_encoded = name.encode("utf-16-le")
    # 非常驻头固定 64 字节,名字紧跟其后,runlist 再跟在名字后面
    name_offset = 64 if name else 0
    runlist_offset = 64 + len(name_encoded)
    runlist_offset += (8 - runlist_offset % 8) % 8
    body = struct.pack(
        "<QQHHIQQQ",
        lowest_vcn,
        highest_vcn,
        runlist_offset,
        0,          # 压缩单元
        0,          # 填充
        allocated,
        real,
        real,       # initialized
    )
    header_fixed = struct.pack(
        "<IIBBHHH",
        0x80,
        0,          # 长度稍后回填
        1,          # 非常驻
        len(name),
        name_offset,
        flags,
        attr_id,
    )
    assembled = bytearray(header_fixed + body)
    assembled.extend(name_encoded)
    while len(assembled) < runlist_offset:
        assembled.append(0)
    assembled.extend(runlist)
    while len(assembled) % 8:
        assembled.append(0)
    struct.pack_into("<I", assembled, 4, len(assembled))
    return bytes(assembled)


def make_mft_record(
    *,
    record_number: int,
    attributes: list[bytes],
    flags: int = 0x0001,           # IN_USE
    record_size: int = 1024,
    sector_size: int = 512,
    sequence: int = 1,
    hard_link_count: int = 1,
    base_reference: int = 0,
    magic: bytes = b"FILE",
    usn: int = 0x1234,
    apply_fixups: bool = True,
) -> bytes:
    """按磁盘布局拼一条 MFT 记录,默认执行 USA 替换。"""
    usa_count = record_size // sector_size + 1
    usa_offset = 48
    attrs_offset = usa_offset + usa_count * 2
    attrs_offset += (8 - attrs_offset % 8) % 8

    body = b"".join(attributes) + b"\xff\xff\xff\xff\x00\x00\x00\x00"
    used_size = attrs_offset + len(body)
    if used_size > record_size:
        raise ValueError(f"合成记录超长: {used_size} > {record_size}")

    buf = bytearray(record_size)
    struct.pack_into(
        "<4sHHQHHHHIIQHHI",
        buf,
        0,
        magic,
        usa_offset,
        usa_count,
        0,                  # LSN
        sequence,
        hard_link_count,
        attrs_offset,
        flags,
        used_size,
        record_size,
        base_reference,     # 64 位文件引用
        99,                 # next attr id
        0,                  # 填充
        record_number,
    )
    buf[attrs_offset : attrs_offset + len(body)] = body

    if apply_fixups:
        # 每个扇区末尾两字节挪进 USA,原位写 USN
        struct.pack_into("<H", buf, usa_offset, usn)
        for i in range(usa_count - 1):
            tail = (i + 1) * sector_size - 2
            struct.pack_into("<H", buf, usa_offset + 2 + i * 2, struct.unpack_from("<H", buf, tail)[0])
            struct.pack_into("<H", buf, tail, usn)

    return bytes(buf)
