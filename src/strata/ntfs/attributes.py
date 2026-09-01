"""MFT 记录内的属性解析。

一条 MFT 记录 = 固定头 + 更新序列数组 + 一串变长属性。
我们只关心三个属性:
  $STANDARD_INFORMATION (0x10) 时间戳
  $FILE_NAME            (0x30) 名字 + 父目录引用
  $DATA                 (0x80) 大小
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ---- 属性类型 ----
ATTR_STANDARD_INFORMATION = 0x10
ATTR_ATTRIBUTE_LIST = 0x20
ATTR_FILE_NAME = 0x30
ATTR_OBJECT_ID = 0x40
ATTR_DATA = 0x80
ATTR_INDEX_ROOT = 0x90
ATTR_INDEX_ALLOCATION = 0xA0
ATTR_END = 0xFFFFFFFF

# ---- 记录头标志 ----
MFT_RECORD_IN_USE = 0x0001
MFT_RECORD_IS_DIRECTORY = 0x0002

# ---- 文件属性标志(来自 $STANDARD_INFORMATION / $FILE_NAME) ----
FILE_ATTR_READONLY = 0x00000001
FILE_ATTR_HIDDEN = 0x00000002
FILE_ATTR_SYSTEM = 0x00000004
FILE_ATTR_DIRECTORY = 0x00000010
FILE_ATTR_ARCHIVE = 0x00000020
FILE_ATTR_TEMPORARY = 0x00000100
FILE_ATTR_SPARSE = 0x00000200
FILE_ATTR_REPARSE_POINT = 0x00000400
FILE_ATTR_COMPRESSED = 0x00000800
FILE_ATTR_OFFLINE = 0x00001000
FILE_ATTR_NOT_INDEXED = 0x00002000
FILE_ATTR_ENCRYPTED = 0x00004000

# ---- 文件名命名空间 ----
NAMESPACE_POSIX = 0
NAMESPACE_WIN32 = 1
NAMESPACE_DOS = 2
NAMESPACE_WIN32_DOS = 3

# 名字优选顺序:Win32&DOS > Win32 > POSIX > 纯 DOS 短名
_NAMESPACE_RANK = {
    NAMESPACE_WIN32_DOS: 3,
    NAMESPACE_WIN32: 2,
    NAMESPACE_POSIX: 1,
    NAMESPACE_DOS: 0,
}

MAGIC_FILE = b"FILE"
MAGIC_BAAD = b"BAAD"

# FILETIME 是 1601-01-01 起的 100 纳秒数
_FILETIME_EPOCH_DIFF = 11_644_473_600
_FILETIME_TICKS = 10_000_000

# 合理时间范围:1990 到 2100。超出的当成脏数据丢掉。
_TIME_MIN = 631_152_000.0
_TIME_MAX = 4_102_444_800.0

# 预编译,解析热路径上省下大量开销
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
# 属性头开头的类型码 + 长度。一次取两个,省掉热路径上的第二次 unpack_from。
_U32_PAIR = struct.Struct("<II")
_U64 = struct.Struct("<Q")
# 记录头布局:magic(0x00) usa_offset(0x04) usa_count(0x06) lsn(0x08)
# sequence(0x10) hard_links(0x12) attrs_offset(0x14) flags(0x16)
# used(0x18) allocated(0x1C) base_ref(0x20,64 位) next_attr_id(0x28)
# 填充(0x2A) record_number(0x2C) —— 共 0x30 字节
_REC_HEADER = struct.Struct("<4sHHQHHHHIIQHHI")
_ATTR_COMMON = struct.Struct("<IIBBHHH")
# 公共头偏移 8 起的那五个字段。类型码和长度在它前面单独取 —— 见
# iter_attributes:不要的属性只需要长度就能跳过,不该为它们解满七个字段。
_ATTR_TAIL = struct.Struct("<BBHHH")
_ATTR_RESIDENT = struct.Struct("<IH")
_ATTR_NONRESIDENT = struct.Struct("<QQHHIQQQ")
_STD_INFO = struct.Struct("<QQQQI")
_FILE_NAME = struct.Struct("<QQQQQQQIIBB")


def filetime_to_unix(ft: int) -> float | None:
    """FILETIME → Unix 秒。0 或明显不合理的值返回 None。"""
    if ft == 0:
        return None
    ts = ft / _FILETIME_TICKS - _FILETIME_EPOCH_DIFF
    if ts < _TIME_MIN or ts > _TIME_MAX:
        return None
    return ts


@dataclass(slots=True)
class RecordHeader:
    magic: bytes
    usa_offset: int
    usa_count: int
    lsn: int
    sequence: int
    hard_link_count: int
    attrs_offset: int
    flags: int
    used_size: int
    allocated_size: int
    base_reference: int
    next_attr_id: int
    record_number: int

    @property
    def in_use(self) -> bool:
        return bool(self.flags & MFT_RECORD_IN_USE)

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & MFT_RECORD_IS_DIRECTORY)

    @property
    def is_extension(self) -> bool:
        """扩展记录:数据属于 base_reference 指向的基记录。"""
        return (self.base_reference & 0x0000FFFFFFFFFFFF) != 0

    @property
    def base_record_number(self) -> int:
        return self.base_reference & 0x0000FFFFFFFFFFFF


def parse_record_header(buf: bytes | bytearray | memoryview, offset: int = 0) -> RecordHeader:
    (
        magic,
        usa_offset,
        usa_count,
        lsn,
        sequence,
        hard_link_count,
        attrs_offset,
        flags,
        used_size,
        allocated_size,
        base_reference,
        next_attr_id,
        _pad,
        record_number,
    ) = _REC_HEADER.unpack_from(buf, offset)
    return RecordHeader(
        magic=magic,
        usa_offset=usa_offset,
        usa_count=usa_count,
        lsn=lsn,
        sequence=sequence,
        hard_link_count=hard_link_count,
        attrs_offset=attrs_offset,
        flags=flags,
        used_size=used_size,
        allocated_size=allocated_size,
        base_reference=base_reference,
        next_attr_id=next_attr_id,
        record_number=record_number,
    )


class FixupError(Exception):
    """更新序列数组校验失败,记录不可信。"""


def apply_fixups(buf: bytearray, offset: int, record_size: int, sector_size: int = 512) -> None:
    """就地还原更新序列数组做的替换。

    NTFS 把每个扇区最后两字节换成了 USN,原值存在数组里。
    不还原就解析会读到错误的字节 —— 这一步不是可选的。
    """
    usa_offset = _U16.unpack_from(buf, offset + 4)[0]
    usa_count = _U16.unpack_from(buf, offset + 6)[0]
    if usa_count == 0:
        return
    if usa_offset + usa_count * 2 > record_size:
        raise FixupError(f"更新序列数组越界: offset={usa_offset} count={usa_count}")

    base = offset + usa_offset
    # 按整数比,不切片。每个扇区原来要 bytes(buf[t:t+2]) 一次,1024 字节的记录
    # 两个扇区,120 万条记录就是几百万个两字节对象 —— 纯粹为了比两个字节。
    usn_lo = buf[base]
    usn_hi = buf[base + 1]
    entries = usa_count - 1
    if entries * sector_size > record_size:
        raise FixupError(f"更新序列条目数 {entries} 超过记录能容纳的扇区数")

    for i in range(entries):
        tail = offset + (i + 1) * sector_size - 2
        if buf[tail] != usn_lo or buf[tail + 1] != usn_hi:
            raise FixupError(f"扇区 {i} 的 USN 不匹配,记录已损坏")
        src = base + 2 + i * 2
        buf[tail] = buf[src]
        buf[tail + 1] = buf[src + 1]


@dataclass(slots=True)
class AttributeHeader:
    type_code: int
    length: int
    non_resident: bool
    name_length: int
    name_offset: int
    flags: int
    attr_id: int
    # 常驻
    value_length: int = 0
    value_offset: int = 0
    # 非常驻
    lowest_vcn: int = 0
    highest_vcn: int = 0
    runlist_offset: int = 0
    compression_unit: int = 0
    allocated_size: int = 0
    real_size: int = 0
    initialized_size: int = 0

    @property
    def compressed(self) -> bool:
        return bool(self.flags & 0x0001)

    @property
    def sparse(self) -> bool:
        return bool(self.flags & 0x8000)


def iter_attributes(
    buf: bytes | bytearray | memoryview,
    header: RecordHeader,
    offset: int,
    record_size: int,
    wanted: frozenset[int] | set[int] | None = None,
):
    """依次产出 (属性头, 属性起始偏移)。遇到不合理的长度就停,不抛异常。

    wanted 给一个类型码集合时,只有集合里的类型会产出 —— 别的连 AttributeHeader
    都不建,按 length 跳过就算了。这是为热路径准备的:_parse_record 只认三种
    类型码,而目录身上还挂着 $INDEX_ROOT / $INDEX_ALLOCATION / $BITMAP,
    120 万条记录乘四五条属性,白建的是五六百万个 16 字段的对象。

    **默认不过滤**,而且空集合是「什么都不要」不是「不过滤」(所以判的是
    `is None` 而不是真值)—— 探针工具要把每条属性都打出来,把默认改了它们
    就少东西;而调用方用集合运算算 wanted 时,算出空集是很自然的事。

    长度校验在过滤**之前**:一条属性的 length 不可信,它后面所有属性的位置
    就都不可信了,跟这条要不要没关系。
    """
    pos = offset + header.attrs_offset
    limit = offset + min(header.used_size or record_size, record_size)

    while pos + 8 <= limit:
        type_code, length = _U32_PAIR.unpack_from(buf, pos)
        if type_code == ATTR_END:
            return
        if pos + _ATTR_COMMON.size > limit:
            return

        # 长度必须推进且不越界,否则视为损坏
        if length < _ATTR_COMMON.size or pos + length > limit:
            return

        if wanted is not None and type_code not in wanted:
            pos += length
            continue

        (
            non_resident,
            name_length,
            name_offset,
            flags,
            attr_id,
        ) = _ATTR_TAIL.unpack_from(buf, pos + 8)

        attr = AttributeHeader(
            type_code,
            length,
            bool(non_resident),
            name_length,
            name_offset,
            flags,
            attr_id,
        )

        if non_resident:
            if pos + 16 + _ATTR_NONRESIDENT.size <= limit:
                (
                    attr.lowest_vcn,
                    attr.highest_vcn,
                    attr.runlist_offset,
                    attr.compression_unit,
                    _pad,
                    attr.allocated_size,
                    attr.real_size,
                    attr.initialized_size,
                ) = _ATTR_NONRESIDENT.unpack_from(buf, pos + 16)
        else:
            if pos + 16 + _ATTR_RESIDENT.size <= limit:
                attr.value_length, attr.value_offset = _ATTR_RESIDENT.unpack_from(buf, pos + 16)

        yield attr, pos
        pos += length


@dataclass(slots=True)
class StandardInfo:
    """$STANDARD_INFORMATION 的四个时间和属性位。

    时间**存原始 FILETIME**,取的时候才换算 —— created 之类是 property。
    理由是量出来的:解析一条记录要换算 6 次时间(这里 4 次 + $FILE_NAME 2 次),
    而用得上的只有 2 次。accessed 和 mft_changed 全仓库没有一处读,
    $FILE_NAME 那两个只在这里缺失时兜底(而这条属性每条记录都有)。
    120 万条记录上就是几百万次白算。

    **不删** accessed / mft_changed:它们确实在记录里,删了以后就再也问不出来,
    而现在这样留着一分钱不花。字段顺序和 _STD_INFO 的解包顺序一致,所以下面
    可以直接摊开构造。
    """

    created_ft: int
    modified_ft: int
    mft_changed_ft: int
    accessed_ft: int
    attributes: int

    @property
    def created(self) -> float | None:
        return filetime_to_unix(self.created_ft)

    @property
    def modified(self) -> float | None:
        return filetime_to_unix(self.modified_ft)

    @property
    def mft_changed(self) -> float | None:
        return filetime_to_unix(self.mft_changed_ft)

    @property
    def accessed(self) -> float | None:
        return filetime_to_unix(self.accessed_ft)


def parse_standard_information(
    buf: bytes | bytearray | memoryview, attr: AttributeHeader, attr_offset: int
) -> StandardInfo | None:
    if attr.non_resident or attr.value_length < _STD_INFO.size:
        return None
    return StandardInfo(*_STD_INFO.unpack_from(buf, attr_offset + attr.value_offset))


@dataclass(slots=True)
class FileNameInfo:
    parent: int
    name: str
    namespace: int
    allocated_hint: int
    real_hint: int
    attributes: int
    # 同样存原始 FILETIME,见 StandardInfo 的说明。默认 0 和以前的默认 None
    # 是同一个意思 —— filetime_to_unix(0) 返回 None。
    created_ft: int = 0
    modified_ft: int = 0

    @property
    def rank(self) -> int:
        return _NAMESPACE_RANK.get(self.namespace, -1)

    @property
    def created(self) -> float | None:
        return filetime_to_unix(self.created_ft)

    @property
    def modified(self) -> float | None:
        return filetime_to_unix(self.modified_ft)


def parse_file_name(
    buf: bytes | bytearray | memoryview, attr: AttributeHeader, attr_offset: int
) -> FileNameInfo | None:
    if attr.non_resident or attr.value_length < _FILE_NAME.size:
        return None
    base = attr_offset + attr.value_offset
    (
        parent_ref,
        created,
        modified,
        _mft_changed,
        _accessed,
        allocated_hint,
        real_hint,
        attributes,
        _reparse,
        name_length,
        namespace,
    ) = _FILE_NAME.unpack_from(buf, base)

    name_start = base + _FILE_NAME.size
    name_bytes = bytes(buf[name_start : name_start + name_length * 2])
    if len(name_bytes) < name_length * 2:
        return None
    try:
        name = name_bytes.decode("utf-16-le")
    except UnicodeDecodeError:
        name = name_bytes.decode("utf-16-le", errors="replace")

    return FileNameInfo(
        parent=parent_ref & 0x0000FFFFFFFFFFFF,
        name=name,
        namespace=namespace,
        allocated_hint=allocated_hint,
        real_hint=real_hint,
        attributes=attributes,
        created_ft=created,
        modified_ft=modified,
    )


def attribute_name(
    buf: bytes | bytearray | memoryview, attr: AttributeHeader, attr_offset: int
) -> str:
    """属性名(用于区分主数据流和备用数据流)。无名返回空串。"""
    if attr.name_length == 0:
        return ""
    start = attr_offset + attr.name_offset
    raw = bytes(buf[start : start + attr.name_length * 2])
    return raw.decode("utf-16-le", errors="replace")


@dataclass(slots=True)
class DataSize:
    """一条 $DATA 流贡献的大小。"""

    allocated: int
    real: int
    resident: bool
    named: bool


def parse_data_size(
    buf: bytes | bytearray | memoryview, attr: AttributeHeader, attr_offset: int
) -> DataSize | None:
    """算出这条 $DATA 属性占多少盘。

    常驻数据存在 MFT 记录里,不占额外簇 —— 但报 0 会让用户困惑,
    所以用逻辑大小。非常驻用 allocated_size,它已经把压缩和稀疏算进去了。
    只取第一个片段(lowest_vcn == 0),后续片段的 allocated_size 字段没意义。
    """
    named = attr.name_length > 0
    if attr.non_resident:
        if attr.lowest_vcn != 0:
            return None
        return DataSize(
            allocated=attr.allocated_size,
            real=attr.real_size,
            resident=False,
            named=named,
        )
    return DataSize(
        allocated=attr.value_length,
        real=attr.value_length,
        resident=True,
        named=named,
    )
