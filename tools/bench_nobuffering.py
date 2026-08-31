"""FILE_FLAG_NO_BUFFERING 到底让顺序读慢多少。

要回答的问题:C: 走 MFT 全程 100.5 秒,已经量掉/排掉了除读盘以外的每一段 ——
收集后五遍 8.8s(prof_pipeline.py),条目转换 11.6s(prof_mft_convert.py),
写库靠 scandir 全程 38.8s 反推。剩下 80 秒落在 read_entries,而解析只值
12.7s。也就是 1.5 GiB 的 MFT 读了约 67 秒 = 23 MB/s。顺序读不该这么慢。

嫌疑:volume.py:184-186 同时设了两个标志

    FILE_FLAG_SEQUENTIAL_SCAN   给缓存管理器的预读提示
    FILE_FLAG_NO_BUFFERING      绕开缓存管理器

后者让前者失效 —— 提示给了一个被绕开的组件。结果是没有预读,8 MiB 一次
同步读,一次只有一个请求在飞。

这个工具不需要管理员权限:拿普通大文件按 volume.py 一模一样的模式读
(create_string_buffer + ReadFile,8 MiB 一块),A/B 那个标志。

    python tools/bench_nobuffering.py

公平性:同一个文件切两半,一半用一种标志。两半冷热条件一样。第二轮对调 ——
缓存要是污染了结论,对调之后数字会反过来,那就不能信。只读,不写任何文件。
"""

from __future__ import annotations

import ctypes
import glob
import os
import sys
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

MIB = 1024 * 1024
CHUNK = 8 * MIB          # 和 mft.py 的 CHUNK_RECORDS * 1024 一样


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    k.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    k.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def read_span(path: str, start: int, length: int, *, no_buffering: bool) -> tuple[float, int]:
    """按 volume.py 的模式读一段。返回 (秒, 字节数)。

    偏移和长度都按 4096 对齐 —— NO_BUFFERING 要求扇区对齐,不对齐 ReadFile
    直接失败(错误 87)。volume.py:246-252 也是这么对齐的。
    """
    k = _k32()
    flags = FILE_FLAG_SEQUENTIAL_SCAN
    if no_buffering:
        flags |= FILE_FLAG_NO_BUFFERING
    h = k.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                      None, OPEN_EXISTING, flags, None)
    if h is None or h == INVALID_HANDLE_VALUE:
        raise OSError(f"打开失败,错误 {ctypes.get_last_error()}:{path}")

    start = (start // 4096) * 4096
    length = (length // 4096) * 4096
    try:
        pos = ctypes.c_longlong(0)
        if not k.SetFilePointerEx(h, ctypes.c_longlong(start), ctypes.byref(pos), 0):
            raise OSError(f"定位失败,错误 {ctypes.get_last_error()}")

        t = time.perf_counter()
        got = 0
        while got < length:
            want = min(CHUNK, length - got)
            buf = ctypes.create_string_buffer(want)
            n = wintypes.DWORD(0)
            if not k.ReadFile(h, buf, want, ctypes.byref(n), None):
                raise OSError(f"读取失败,错误 {ctypes.get_last_error()}")
            if n.value == 0:
                break
            _ = buf.raw[: n.value]        # volume.py 也做了这次拷贝
            got += n.value
        return time.perf_counter() - t, got
    finally:
        k.CloseHandle(h)


def biggest_file() -> str:
    cands: list[tuple[int, str]] = []
    for pat in (r"C:\Windows\Installer\*.msi", r"C:\Windows\System32\*.dll"):
        for f in glob.glob(pat):
            try:
                cands.append((os.path.getsize(f), f))
            except OSError:
                pass
    if not cands:
        raise SystemExit("找不到够大的测试文件")
    cands.sort(reverse=True)
    return cands[0][1]


def line(label: str, secs: float, nbytes: int) -> None:
    mbs = (nbytes / MIB / secs) if secs > 0 else 0.0
    print(f"  {label:<38} {secs:>6.2f}s  {nbytes / MIB:>7.1f} MiB  {mbs:>8.1f} MB/s")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else biggest_file()
    size = os.path.getsize(path)
    half = (size // 2 // 4096) * 4096
    print(f"文件 {path}")
    print(f"     {size / MIB:.1f} MiB,切两半各 {half / MIB:.1f} MiB,{CHUNK // MIB} MiB 一块读\n")

    rates: dict[str, list[float]] = {"nobuf": [], "buf": []}

    # 第 1 轮:前半 NO_BUFFERING,后半走缓存。两半都是冷的。
    print("--- 第 1 轮(前半 NO_BUFFERING,后半走缓存)---")
    s, n = read_span(path, 0, half, no_buffering=True)
    line("NO_BUFFERING(现在的设置)", s, n)
    rates["nobuf"].append(n / MIB / s)
    s, n = read_span(path, half, half, no_buffering=False)
    line("走 OS 缓存", s, n)
    rates["buf"].append(n / MIB / s)

    # 第 2 轮:对调。这两半现在的缓存状态跟第 1 轮相反 ——
    # 结论要是被缓存左右,这一轮会反过来。
    print("\n--- 第 2 轮(对调:前半走缓存,后半 NO_BUFFERING)---")
    s, n = read_span(path, half, half, no_buffering=True)
    line("NO_BUFFERING", s, n)
    rates["nobuf"].append(n / MIB / s)
    s, n = read_span(path, 0, half, no_buffering=False)
    line("走 OS 缓存", s, n)
    rates["buf"].append(n / MIB / s)

    nb = sum(rates["nobuf"]) / 2
    bf = sum(rates["buf"]) / 2
    print(f"\n两轮平均  NO_BUFFERING {nb:,.0f} MB/s   走缓存 {bf:,.0f} MB/s")
    if nb > 0:
        print(f"          关掉它快 {bf / nb:.1f}x")
    print(f"两轮各自  NO_BUFFERING {rates['nobuf'][0]:,.0f} / {rates['nobuf'][1]:,.0f}"
          f"   走缓存 {rates['buf'][0]:,.0f} / {rates['buf'][1]:,.0f}")
    print("(两轮同向才算数;反向说明是缓存在起作用,不是标志)")

    mft_mib = 1536.0
    print(f"\n换算到 C: 的 $MFT(约 {mft_mib:.0f} MiB):")
    print(f"  NO_BUFFERING  {mft_mib / nb:>6.1f}s")
    print(f"  走缓存        {mft_mib / bf:>6.1f}s")
    print(f"实测 read_entries 那 80 秒里,解析占 12.7s,剩 67s 是读 —— 对得上哪个?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
