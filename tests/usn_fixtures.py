"""合成 USN 记录,按磁盘上的真实布局摆放。

和 mft_fixtures 一个思路:字节怎么排是照着结构定义来的,
解析器必须真的按偏移读才能通过,不是对着我的理解自说自话。
"""

from __future__ import annotations

import struct

from strata.ntfs.usn import (
    USN_REASON_FILE_CREATE,
    _V2_HEADER_LEN,
    _V3_HEADER_LEN,
)

UNIX_EPOCH_AS_FILETIME = 116_444_736_000_000_000


def unix_to_filetime(ts: float) -> int:
    return int(ts * 10_000_000) + UNIX_EPOCH_AS_FILETIME


def usn_record_v2(
    *,
    usn: int,
    name: str,
    reason: int = USN_REASON_FILE_CREATE,
    file_ref: int = 100,
    parent_ref: int = 5,
    timestamp: float = 1_700_000_000.0,
    attributes: int = 0x80,
    source_info: int = 0,
    security_id: int = 0,
    pad_to: int | None = None,
) -> bytes:
    """USN_RECORD_V2:0x3C 字节头 + UTF-16 文件名,整体 8 字节对齐。"""
    name_bytes = name.encode("utf-16-le")
    length = _V2_HEADER_LEN + len(name_bytes)
    length = (length + 7) & ~7          # 记录长度按 8 对齐
    if pad_to is not None:
        length = pad_to

    head = struct.pack(
        "<IHHQQqqIIIIHH",
        length,
        2,                  # MajorVersion
        0,                  # MinorVersion
        file_ref,
        parent_ref,
        usn,
        unix_to_filetime(timestamp),
        reason,
        source_info,
        security_id,
        attributes,
        len(name_bytes),
        _V2_HEADER_LEN,     # FileNameOffset
    )
    body = head + name_bytes
    return body + b"\x00" * (length - len(body))


def usn_record_v3(
    *,
    usn: int,
    name: str,
    reason: int = USN_REASON_FILE_CREATE,
    file_ref_low: int = 200,
    parent_ref_low: int = 5,
    timestamp: float = 1_700_000_000.0,
    attributes: int = 0x80,
) -> bytes:
    """USN_RECORD_V3:文件引用是 128 位,低 64 位才是 MFT 记录号。"""
    name_bytes = name.encode("utf-16-le")
    length = _V3_HEADER_LEN + len(name_bytes)
    length = (length + 7) & ~7

    out = bytearray(length)
    struct.pack_into("<IHH", out, 0, length, 3, 0)
    # FILE_ID_128:低 8 字节记录号,高 8 字节这里填 0
    struct.pack_into("<QQ", out, 0x08, file_ref_low, 0)
    struct.pack_into("<QQ", out, 0x18, parent_ref_low, 0)
    struct.pack_into(
        "<qqIIIIHH",
        out,
        0x28,
        usn,
        unix_to_filetime(timestamp),
        reason,
        0,
        0,
        attributes,
        len(name_bytes),
        _V3_HEADER_LEN,
    )
    out[_V3_HEADER_LEN : _V3_HEADER_LEN + len(name_bytes)] = name_bytes
    return bytes(out)


def usn_record_v4(*, usn: int, length: int = 40) -> bytes:
    """V4 没有文件名,只在范围跟踪时出现。解析器应当跳过。"""
    out = bytearray(length)
    struct.pack_into("<IHH", out, 0, length, 4, 0)
    struct.pack_into("<QQ", out, 0x08, 300, 0)
    struct.pack_into("<q", out, 0x18, usn)
    return bytes(out)


def usn_buffer(next_usn: int, records: list[bytes]) -> bytes:
    """FSCTL_READ_USN_JOURNAL 的输出:8 字节 next USN + 记录序列。"""
    return struct.pack("<q", next_usn) + b"".join(records)


def journal_data(
    *,
    journal_id: int = 0x01D9_ABCD_1234_5678,
    first_usn: int = 4096,
    next_usn: int = 1_000_000,
    lowest_valid_usn: int = 4096,
    max_usn: int = 0x7FFF_FFFF_FFFF_0000,
    max_size: int = 32 * 1024 * 1024,
    allocation_delta: int = 4 * 1024 * 1024,
    version: int = 0,
) -> bytes:
    """USN_JOURNAL_DATA。V1 在后面多两个版本字段,前 56 字节一致。"""
    data = struct.pack(
        "<QqqqqQQ",
        journal_id,
        first_usn,
        next_usn,
        lowest_valid_usn,
        max_usn,
        max_size,
        allocation_delta,
    )
    if version >= 1:
        data += struct.pack("<HH", 2, 3)      # Min/MaxSupportedMajorVersion
    if version >= 2:
        data += struct.pack("<HHII", 0, 0, 0, 0)
    return data
