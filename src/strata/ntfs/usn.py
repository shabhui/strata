"""USN 变更日志 —— 时间戳看不到的东西,这里能看到。

回溯层靠文件的创建时间倒推历史,所以有个天生的盲区:8 月 3 日下了 50 GB、
8 月 9 日删掉,今天扫描时那 50 GB 已经不在盘上,8 月 3 日就显示成 0。

USN 日志记录的是操作而不是状态,删除、重命名、创建都留痕,正好补上这个洞。
NTFS 默认就开着这个日志,容量有限(通常 32 MB 左右)会滚动覆盖,
所以能回溯多久取决于盘上的写入活动 —— 几天到几周不等,拿不到就退回时间戳。

设计上把解析和系统调用分开:parse_usn_buffer 是纯函数,喂字节就能测;
UsnJournal 负责 DeviceIoControl 那一层。
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator

from .attributes import filetime_to_unix
from .volume import (
    ERROR_ACCESS_DENIED,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    GENERIC_READ,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    AccessDenied,
    NtfsError,
    _kernel32,
)

# ---- 控制码 ----
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

# ---- Windows 错误码 ----
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_ENTRY_DELETED = 1181
ERROR_INVALID_FUNCTION = 1
ERROR_NOT_SUPPORTED = 50
ERROR_HANDLE_EOF = 38

# ---- 变更原因位 ----
USN_REASON_DATA_OVERWRITE = 0x00000001
USN_REASON_DATA_EXTEND = 0x00000002
USN_REASON_DATA_TRUNCATION = 0x00000004
USN_REASON_NAMED_DATA_OVERWRITE = 0x00000010
USN_REASON_NAMED_DATA_EXTEND = 0x00000020
USN_REASON_NAMED_DATA_TRUNCATION = 0x00000040
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_EA_CHANGE = 0x00000400
USN_REASON_SECURITY_CHANGE = 0x00000800
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_INDEXABLE_CHANGE = 0x00004000
USN_REASON_BASIC_INFO_CHANGE = 0x00008000
USN_REASON_HARD_LINK_CHANGE = 0x00010000
USN_REASON_COMPRESSION_CHANGE = 0x00020000
USN_REASON_ENCRYPTION_CHANGE = 0x00040000
USN_REASON_OBJECT_ID_CHANGE = 0x00080000
USN_REASON_REPARSE_POINT_CHANGE = 0x00100000
USN_REASON_STREAM_CHANGE = 0x00200000
USN_REASON_TRANSACTED_CHANGE = 0x00400000
USN_REASON_INTEGRITY_CHANGE = 0x00800000
USN_REASON_CLOSE = 0x80000000

FILE_ATTRIBUTE_DIRECTORY = 0x00000010

# 只关心影响占用的操作。所有位都收会把日志塞满 SECURITY_CHANGE 这种噪音,
# 对「什么在吃空间」毫无帮助。
REASON_MASK_SPACE = (
    USN_REASON_DATA_EXTEND
    | USN_REASON_DATA_TRUNCATION
    | USN_REASON_DATA_OVERWRITE
    | USN_REASON_NAMED_DATA_EXTEND
    | USN_REASON_NAMED_DATA_TRUNCATION
    | USN_REASON_FILE_CREATE
    | USN_REASON_FILE_DELETE
    | USN_REASON_RENAME_OLD_NAME
    | USN_REASON_RENAME_NEW_NAME
    | USN_REASON_CLOSE
)

# 事件分类,和 db.UsnRow.kind 对应
KIND_CREATE = "create"
KIND_DELETE = "delete"
KIND_RENAME_OLD = "rename_old"
KIND_RENAME_NEW = "rename_new"
KIND_WRITE = "write"
KIND_OTHER = "other"

_JOURNAL_DATA = struct.Struct("<QqqqqQQ")   # V0:7 个 64 位字段
_READ_DATA_V0 = struct.Struct("<qIIQQQ")    # StartUsn Reason ReturnOnlyOnClose Timeout BytesToWaitFor JournalId

# USN_RECORD_V2 头:0x3C 字节
_RECORD_V2 = struct.Struct("<IHHQQqqIIIIHH")
_V2_HEADER_LEN = 0x3C
# USN_RECORD_V3 头:0x4C 字节,文件引用变成 128 位
_V3_HEADER_LEN = 0x4C

_REASON_LABELS: tuple[tuple[int, str], ...] = (
    (USN_REASON_FILE_CREATE, "新建"),
    (USN_REASON_FILE_DELETE, "删除"),
    (USN_REASON_RENAME_OLD_NAME, "改名前"),
    (USN_REASON_RENAME_NEW_NAME, "改名后"),
    (USN_REASON_DATA_EXTEND, "写入变大"),
    (USN_REASON_DATA_TRUNCATION, "截断变小"),
    (USN_REASON_DATA_OVERWRITE, "覆写"),
    (USN_REASON_NAMED_DATA_EXTEND, "附加流变大"),
    (USN_REASON_NAMED_DATA_TRUNCATION, "附加流变小"),
    (USN_REASON_HARD_LINK_CHANGE, "硬链接变化"),
    (USN_REASON_STREAM_CHANGE, "数据流变化"),
    (USN_REASON_BASIC_INFO_CHANGE, "属性变化"),
    (USN_REASON_SECURITY_CHANGE, "权限变化"),
    (USN_REASON_CLOSE, "关闭"),
)


class JournalUnavailable(NtfsError):
    """日志没开、被删、或者游标已经滚出窗口。调用方应当退回时间戳方案。"""


@dataclass(slots=True)
class JournalInfo:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int
    max_size: int
    allocation_delta: int


@dataclass(slots=True)
class UsnEvent:
    usn: int
    file_reference: int          # 掩过的,只有 MFT 记录号
    parent_reference: int        # 同上,拿来跟 mft.resolve_paths 的键对
    timestamp: float | None
    reason: int
    attributes: int
    name: str
    # 原样的 64 位引用:高 16 位是序列号,低 48 位是记录号。
    #
    # 为什么两个都留:记录号那份是为了跟 MFT 侧对齐(resolve_paths 的键就是
    # 不带序列号的记录号),而 OpenFileById 只认完整的 —— 实测掩掉序列号
    # 一个都开不了,4/4 返回错误 87(真盘上用一次性脚本量的,脚本没留仓库)。
    # 序列号是日志里本来就有的信息,掩掉就找不回来了,所以两份都带着,
    # 用哪份由用的人决定。
    file_reference_full: int = 0
    parent_reference_full: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def kind(self) -> str:
        return classify_reason(self.reason)

    @property
    def reason_text(self) -> str:
        return describe_reason(self.reason)


def classify_reason(reason: int) -> str:
    """把原因位归成一类。

    顺序有讲究:一条记录常常同时带 CREATE 和 DATA_EXTEND,
    对「空间去哪了」来说建立/删除比写入更值得展示。
    """
    if reason & USN_REASON_FILE_DELETE:
        return KIND_DELETE
    if reason & USN_REASON_FILE_CREATE:
        return KIND_CREATE
    if reason & USN_REASON_RENAME_OLD_NAME:
        return KIND_RENAME_OLD
    if reason & USN_REASON_RENAME_NEW_NAME:
        return KIND_RENAME_NEW
    if reason & (
        USN_REASON_DATA_EXTEND
        | USN_REASON_DATA_TRUNCATION
        | USN_REASON_DATA_OVERWRITE
        | USN_REASON_NAMED_DATA_EXTEND
        | USN_REASON_NAMED_DATA_TRUNCATION
    ):
        return KIND_WRITE
    return KIND_OTHER


def describe_reason(reason: int) -> str:
    hits = [label for bit, label in _REASON_LABELS if reason & bit]
    return "+".join(hits) if hits else f"0x{reason:08X}"


def parse_journal_info(data: bytes) -> JournalInfo:
    """解析 USN_JOURNAL_DATA。V0/V1/V2 前 56 字节布局相同,只取这部分。"""
    if len(data) < _JOURNAL_DATA.size:
        raise JournalUnavailable(
            f"日志信息只有 {len(data)} 字节,至少需要 {_JOURNAL_DATA.size}"
        )
    (
        journal_id,
        first_usn,
        next_usn,
        lowest_valid,
        max_usn,
        max_size,
        alloc_delta,
    ) = _JOURNAL_DATA.unpack_from(data, 0)
    return JournalInfo(
        journal_id=journal_id,
        first_usn=first_usn,
        next_usn=next_usn,
        lowest_valid_usn=lowest_valid,
        max_usn=max_usn,
        max_size=max_size,
        allocation_delta=alloc_delta,
    )


def parse_usn_buffer(buf: bytes) -> tuple[int, list[UsnEvent]]:
    """解析 FSCTL_READ_USN_JOURNAL 的输出。

    前 8 字节是下一次该从哪个 USN 继续,后面是变长记录。
    返回 (next_usn, 事件列表)。

    遇到不认识的版本或长度不合理的记录就停下来,不抛异常 —— 日志是滚动缓冲,
    截断在正常范围内,已经解出来的部分仍然有效。
    """
    if len(buf) < 8:
        return 0, []
    next_usn = struct.unpack_from("<q", buf, 0)[0]

    events: list[UsnEvent] = []
    offset = 8
    limit = len(buf)
    while offset + 8 <= limit:
        record_length, major, minor = struct.unpack_from("<IHH", buf, offset)
        if record_length == 0:
            break
        if record_length < 8 or offset + record_length > limit:
            break

        event = _parse_record(buf, offset, record_length, major)
        if event is not None:
            events.append(event)
        offset += record_length

    return next_usn, events


def _parse_record(
    buf: bytes, offset: int, record_length: int, major: int
) -> UsnEvent | None:
    if major == 2:
        if record_length < _V2_HEADER_LEN:
            return None
        (
            _len,
            _major,
            _minor,
            file_ref,
            parent_ref,
            usn,
            timestamp,
            reason,
            _source,
            _security,
            attributes,
            name_len,
            name_off,
        ) = _RECORD_V2.unpack_from(buf, offset)
    elif major == 3:
        if record_length < _V3_HEADER_LEN:
            return None
        # FILE_ID_128 的低 64 位就是 MFT 记录号,取低位即可
        file_ref = struct.unpack_from("<Q", buf, offset + 0x08)[0]
        parent_ref = struct.unpack_from("<Q", buf, offset + 0x18)[0]
        usn, timestamp, reason, _source, _security, attributes, name_len, name_off = (
            struct.unpack_from("<qqIIIIHH", buf, offset + 0x28)
        )
    else:
        # V4 只在范围跟踪时出现,没有文件名,对我们没用
        return None

    name = ""
    if name_len and name_off:
        start = offset + name_off
        end = start + name_len
        if end <= len(buf) and name_off + name_len <= record_length:
            name = buf[start:end].decode("utf-16-le", errors="replace")

    return UsnEvent(
        usn=usn,
        file_reference=file_ref & 0x0000FFFFFFFFFFFF,   # 去掉高 16 位的序列号
        parent_reference=parent_ref & 0x0000FFFFFFFFFFFF,
        timestamp=filetime_to_unix(timestamp),
        reason=reason,
        attributes=attributes,
        name=name,
        file_reference_full=file_ref,
        parent_reference_full=parent_ref,
    )


class UsnJournal:
    """卷的 USN 日志读取器。

    需要管理员权限,和直读 MFT 是同一个门槛。用法::

        with UsnJournal("C:") as j:
            info = j.query()
            next_usn, events = j.read(start_usn=0)
    """

    def __init__(self, drive: str) -> None:
        self.drive = drive.rstrip("\\").rstrip(":") + ":"
        self._k32 = _kernel32()
        self._handle: int | None = None
        self.last_usn = 0
        self._open()

    def _open(self) -> None:
        path = f"\\\\.\\{self.drive}"
        handle = self._k32.CreateFileW(
            path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise AccessDenied(
                    f"打开 {path} 被拒绝(错误 5)。读取 USN 日志需要管理员权限。"
                )
            raise NtfsError(f"打开 {path} 失败,Windows 错误 {err}")
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "UsnJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ioctl(self, code: int, in_buf: bytes | None, out_size: int) -> bytes:
        out = ctypes.create_string_buffer(out_size)
        returned = wintypes.DWORD(0)
        in_ptr = None
        in_len = 0
        if in_buf:
            in_ptr = ctypes.create_string_buffer(in_buf, len(in_buf))
            in_len = len(in_buf)

        ok = self._k32.DeviceIoControl(
            self._handle,
            code,
            in_ptr,
            in_len,
            out,
            out_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise _translate_error(err, self.drive)
        return out.raw[: returned.value]

    def query(self) -> JournalInfo:
        """读日志元信息。日志没开时抛 JournalUnavailable。"""
        data = self._ioctl(FSCTL_QUERY_USN_JOURNAL, None, 128)
        return parse_journal_info(data)

    def read(
        self,
        start_usn: int,
        *,
        journal_id: int | None = None,
        reason_mask: int = REASON_MASK_SPACE,
        buffer_size: int = 1 << 20,
    ) -> tuple[int, list[UsnEvent]]:
        """读一批变更。返回 (下一个 USN, 事件列表)。

        journal_id 传上次记下的值,日志被重建过就会报错 —— 这正是我们要的:
        宁可知道游标失效,也不要把新旧日志的记录混在一起。
        """
        if journal_id is None:
            journal_id = self.query().journal_id

        payload = _READ_DATA_V0.pack(start_usn, reason_mask, 0, 0, 0, journal_id)
        data = self._ioctl(FSCTL_READ_USN_JOURNAL, payload, buffer_size)
        return parse_usn_buffer(data)

    def read_all(
        self,
        start_usn: int,
        *,
        journal_id: int | None = None,
        reason_mask: int = REASON_MASK_SPACE,
        buffer_size: int = 1 << 20,
        max_events: int = 500_000,
    ) -> Iterator[UsnEvent]:
        """从 start_usn 一直读到日志末尾。

        max_events 是保险:活跃的盘一次能吐几十万条,不设上限会读到内存吃紧。
        """
        if journal_id is None:
            journal_id = self.query().journal_id

        cursor = start_usn
        yielded = 0
        while True:
            next_usn, events = self.read(
                cursor,
                journal_id=journal_id,
                reason_mask=reason_mask,
                buffer_size=buffer_size,
            )
            if not events:
                self.last_usn = next_usn or cursor
                return
            for event in events:
                yield event
                yielded += 1
                if yielded >= max_events:
                    self.last_usn = event.usn
                    return
            if next_usn <= cursor:
                # 没有推进说明到底了,再读也是同一批
                self.last_usn = next_usn or cursor
                return
            cursor = next_usn


def _translate_error(err: int, drive: str) -> NtfsError:
    if err == ERROR_ACCESS_DENIED:
        return AccessDenied(f"读取 {drive} 的 USN 日志被拒绝,需要管理员权限。")
    if err == ERROR_JOURNAL_NOT_ACTIVE:
        return JournalUnavailable(f"{drive} 没有启用 USN 日志。")
    if err == ERROR_JOURNAL_DELETE_IN_PROGRESS:
        return JournalUnavailable(f"{drive} 的 USN 日志正在删除中。")
    if err == ERROR_JOURNAL_ENTRY_DELETED:
        return JournalUnavailable(
            f"{drive} 的 USN 游标已经滚出日志窗口,这段历史拿不回来了。"
        )
    if err in (ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED):
        return JournalUnavailable(f"{drive} 的文件系统不支持 USN 日志(可能不是 NTFS)。")
    if err == ERROR_HANDLE_EOF:
        return JournalUnavailable(f"{drive} 的 USN 日志已读到末尾。")
    return NtfsError(f"读取 {drive} 的 USN 日志失败,Windows 错误 {err}")
