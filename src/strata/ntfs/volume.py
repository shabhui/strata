"""打开 NTFS 卷做原始读取,并解析引导扇区。

用 ctypes 直接调 CreateFileW,因为原始卷设备需要显式的共享模式,
而且所有读取必须扇区对齐 —— Python 的缓冲 IO 会发出非对齐读而被系统拒绝。
"""

from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_ACCESS_DENIED = 5


class NtfsError(Exception):
    """卷打开失败、不是 NTFS、或读取出错。"""


class AccessDenied(NtfsError):
    """需要管理员权限。"""


def _kernel32():
    if sys.platform != "win32":  # pragma: no cover - 仅 Windows 可用
        raise NtfsError("原始卷读取只在 Windows 上可用")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CreateFileW.restype = wintypes.HANDLE

    k32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    k32.ReadFile.restype = wintypes.BOOL

    k32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    k32.SetFilePointerEx.restype = wintypes.BOOL

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    k32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    k32.DeviceIoControl.restype = wintypes.BOOL
    return k32


@dataclass(slots=True, frozen=True)
class BootSector:
    """NTFS 引导扇区里我们需要的字段。"""

    bytes_per_sector: int
    sectors_per_cluster: int
    total_sectors: int
    mft_cluster: int
    mft_mirror_cluster: int
    bytes_per_mft_record: int
    serial: int

    @property
    def bytes_per_cluster(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def mft_offset(self) -> int:
        return self.mft_cluster * self.bytes_per_cluster

    @property
    def volume_bytes(self) -> int:
        return self.total_sectors * self.bytes_per_sector


def _decode_signed_power(raw: int) -> int:
    """NTFS 用「负数表示 2 的幂」编码某些字段。

    0x00-0x80 直接是数值;大于 0x80 时按有符号解释,结果是 2**(-value)。
    """
    if raw <= 0x80:
        return raw
    return 1 << (256 - raw)


def parse_boot_sector(data: bytes) -> BootSector:
    """从引导扇区(至少 512 字节)解析出几何参数。"""
    if len(data) < 512:
        raise NtfsError(f"引导扇区太短: {len(data)} 字节")
    if data[3:11] != b"NTFS    ":
        raise NtfsError("不是 NTFS 卷(引导扇区缺少 NTFS 标记)")

    bytes_per_sector = struct.unpack_from("<H", data, 11)[0]
    if bytes_per_sector == 0 or bytes_per_sector % 512 != 0:
        raise NtfsError(f"引导扇区里的每扇区字节数不合理: {bytes_per_sector}")

    raw_spc = data[13]
    sectors_per_cluster = _decode_signed_power(raw_spc)
    if sectors_per_cluster == 0:
        raise NtfsError("每簇扇区数为 0")

    total_sectors = struct.unpack_from("<Q", data, 40)[0]
    mft_cluster = struct.unpack_from("<Q", data, 48)[0]
    mft_mirror_cluster = struct.unpack_from("<Q", data, 56)[0]

    raw_mft_rec = data[64]
    if raw_mft_rec <= 0x80:
        # 单位是簇
        bytes_per_mft_record = raw_mft_rec * bytes_per_sector * sectors_per_cluster
    else:
        # 单位是字节,按 2 的幂编码
        bytes_per_mft_record = 1 << (256 - raw_mft_rec)
    if bytes_per_mft_record == 0:
        bytes_per_mft_record = 1024

    serial = struct.unpack_from("<Q", data, 72)[0]

    return BootSector(
        bytes_per_sector=bytes_per_sector,
        sectors_per_cluster=sectors_per_cluster,
        total_sectors=total_sectors,
        mft_cluster=mft_cluster,
        mft_mirror_cluster=mft_mirror_cluster,
        bytes_per_mft_record=bytes_per_mft_record,
        serial=serial,
    )


class Volume:
    """原始卷句柄,提供扇区对齐读取。

    用法::

        with Volume("C:") as vol:
            boot = vol.boot
            data = vol.read(boot.mft_offset, 4096)
    """

    def __init__(self, drive: str, *, no_buffering: bool = True) -> None:
        self.drive = drive.rstrip("\\").rstrip(":") + ":"
        self._k32 = _kernel32()
        self._handle: int | None = None
        self._pos = 0
        self._no_buffering = no_buffering
        self._open()
        self.boot = self._read_boot()

    # ---- 生命周期 ----
    def _open(self) -> None:
        path = f"\\\\.\\{self.drive}"
        flags = FILE_FLAG_SEQUENTIAL_SCAN
        if self._no_buffering:
            flags |= FILE_FLAG_NO_BUFFERING
        handle = self._k32.CreateFileW(
            path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            None,
            OPEN_EXISTING,
            flags,
            None,
        )
        if handle is None or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise AccessDenied(
                    f"打开 {path} 被拒绝(错误 5)。直读 MFT 需要管理员权限。"
                )
            raise NtfsError(f"打开 {path} 失败,Windows 错误 {err}")
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "Volume":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 读取 ----
    def _seek(self, offset: int) -> None:
        new_pos = ctypes.c_longlong(0)
        ok = self._k32.SetFilePointerEx(
            self._handle, ctypes.c_longlong(offset), ctypes.byref(new_pos), 0
        )
        if not ok:
            raise NtfsError(
                f"定位到偏移 {offset} 失败,Windows 错误 {ctypes.get_last_error()}"
            )
        self._pos = new_pos.value

    def _read_raw(self, length: int) -> bytes:
        buf = ctypes.create_string_buffer(length)
        read = wintypes.DWORD(0)
        ok = self._k32.ReadFile(self._handle, buf, length, ctypes.byref(read), None)
        if not ok:
            raise NtfsError(f"读取 {length} 字节失败,Windows 错误 {ctypes.get_last_error()}")
        self._pos += read.value
        return buf.raw[: read.value]

    def _read_boot(self) -> BootSector:
        self._seek(0)
        # 先用 4096 读,足够覆盖任意扇区大小且本身对齐
        return parse_boot_sector(self._read_raw(4096))

    def read(self, offset: int, length: int) -> bytes:
        """从任意偏移读任意长度,内部自动做扇区对齐。"""
        if length <= 0:
            return b""
        sector = self.boot.bytes_per_sector
        start = (offset // sector) * sector
        end = ((offset + length + sector - 1) // sector) * sector
        self._seek(start)
        raw = self._read_raw(end - start)
        head = offset - start
        return raw[head : head + length]

    def read_into(self, offset: int, length: int, buf: bytearray) -> int:
        """读进调用方给的缓冲区,返回实际读到的字节数。

        跟 read() 的区别是不新分配。read() 一次调用要过四遍内存:
        create_string_buffer 分配并填零、buf.raw[:n] 拷一份、
        raw[head:head+length] 再切一份,调用方通常还要 bytearray() 一遍。
        整个 MFT 走下来是 8 MiB × 4 × 196 块 ≈ 6.3 GB 的搬运。

        而且量出来的代价远超搬运本身:每块新分配 8 MiB **同时**手里攥着
        上百万个条目时,解析速度会从 9 µs/条掉到 53 µs/条(前 5 块 74ms、
        后 5 块 440ms)。两个条件缺一个都不会发生 —— 只新分配不留条目稳定,
        只留条目不新分配也稳定。tools/prof_mft_buffer.py 有四个变体的对照。

        偏移和长度必须按扇区对齐。read() 会替调用方兜这件事(对齐到扇区再
        切出中间那段),但那个切片就是要省掉的拷贝之一。MFT 那条路本来就是
        按簇读的,而簇是扇区的整数倍,所以对齐是白得的 —— 不对齐直接报错,
        而不是悄悄退回慢路子。
        """
        if length <= 0:
            return 0
        sector = self.boot.bytes_per_sector
        if offset % sector or length % sector:
            raise NtfsError(
                f"read_into 要求扇区对齐:offset={offset} length={length} "
                f"扇区={sector}。不对齐的读用 read()。"
            )
        if len(buf) < length:
            raise NtfsError(f"缓冲区只有 {len(buf)} 字节,装不下 {length}")

        self._seek(offset)
        # from_buffer 拿的是 buf 内存的视图,不是副本 —— 这是不新分配的关键。
        # 必须在函数里保住这个引用直到 ReadFile 返回。
        view = (ctypes.c_char * len(buf)).from_buffer(buf)
        read = wintypes.DWORD(0)
        ok = self._k32.ReadFile(self._handle, view, length, ctypes.byref(read), None)
        if not ok:
            raise NtfsError(
                f"读取 {length} 字节失败,Windows 错误 {ctypes.get_last_error()}"
            )
        self._pos += read.value
        return read.value

    def read_clusters(self, lcn: int, count: int) -> bytes:
        bpc = self.boot.bytes_per_cluster
        return self.read(lcn * bpc, count * bpc)

    # ---- 供 USN 使用 ----
    @property
    def handle(self) -> int:
        if self._handle is None:
            raise NtfsError("卷句柄已关闭")
        return self._handle

    @property
    def kernel32(self):
        return self._k32


def volume_space(drive: str) -> tuple[int, int]:
    """返回 (总字节, 可用字节)。用 shutil,不需要提权。"""
    import shutil

    usage = shutil.disk_usage(drive + "\\" if not drive.endswith("\\") else drive)
    return usage.total, usage.free
