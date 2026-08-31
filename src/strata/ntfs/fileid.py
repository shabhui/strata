"""按 NTFS 文件编号直接打开对象,问出它的路径。

给 USN 事件还原路径用。日志里带的是父目录的文件引用(64 位:高 16 位序列号 +
低 48 位 MFT 记录号),不是路径。要显示「Downloads\\x.iso 被删了」而不是光秃秃
一个「x.iso」,就得把引用翻成路径。

为什么走这条路而不是遍历时顺手记编号:后者量过,太贵。DirEntry.inode() 在
Windows 上是按**路径**做 lstat,操作系统要从根逐段解析,而 C: 上净是 WinSxS、
node_modules 这种深路径 —— 整块盘 8 线程实测 68s → 165s。
反过来做就便宜得多:一次日志读到 20 万条事件,只涉及 2,572 个不同的父目录,
逐个问一遍 0.23 秒(tools/probe_openbyid.py 量的)。少两个数量级。

两个实测出来的硬约束,都写在这儿免得再踩:

1. **序列号不能掩掉。** OpenFileById 只认完整 64 位引用。把高 16 位掩了之后
   4/4 全失败,错误码 87(ERROR_INVALID_PARAMETER)。所以 usn.py 现在
   两份都带:parent_reference 是掩过的(跟 MFT 记录号对齐),
   parent_reference_full 是原样的,这里用后者。

2. **两种失败,含义完全不同。**

   - 错误 87(ERROR_INVALID_PARAMETER):目录已经删了、MFT 槽位被回收后序列号
     变了、或者引用是脏数据。这三种分不开,而且都没救 —— 目录不在了,
     再多权限也问不出路径。日志窗口很长,期间大量临时目录建了又删,所以
     这类占大头:2,572 个日志引用里 1,266 个如此(约一半)。
   - 错误 5(ERROR_ACCESS_DENIED):目录还在,ACL 不让开。OpenFileById 绕过
     路径遍历检查,但仍要过对象自身的 ACL,所以 C:\\Windows\\LiveKernelReports
     这种受保护目录非管理员开不了(os.listdir 同样被拒)。这类提权就能解决,
     而 collect_usn 只在管理员下跑,生产上基本遇不到。

   区分这两个的实际意义:非管理员下量出来的失败率偏高,不能拿去估生产表现。
   40 个真文件、36 个不同父目录,非管理员下只 1 个失败,就是错误 5 那种
   (tools/verify_usn_paths.py)。

**不要管理员权限。** 提示句柄用的是盘根目录 C:\\,不是卷设备 \\\\.\\C: ——
后者要提权,前者普通用户就开得了,而 OpenFileById 只要求「该卷上的任意文件」,
根目录满足。这条是量出来的,不是推的:非管理员下 7 个真目录 7 个全还回来,
6 个路径逐字一致,不一致的那个是穿了联接点(tools/probe_roothint.py)。
差别有实际意义 —— 这条链路因此能在没提权的情况下测,不用每次改都去点 UAC。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
# 目录必须用这个标志才能拿到句柄,否则 CreateFileW/OpenFileById 直接失败。
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

FileIdType = 0                 # FILE_ID_DESCRIPTOR.Type:用 64 位 NTFS 引用
VOLUME_NAME_DOS = 0x0          # 要 \\?\C:\... 形式,不要 NT 设备名

_INVALID = ctypes.c_void_p(-1).value


class FILE_ID_DESCRIPTOR(ctypes.Structure):
    """OpenFileById 的入参。

    真正的定义里 FileId 是个 union:64 位的 LARGE_INTEGER(NTFS)或 128 位的
    GUID/FILE_ID_128(ReFS)。只用前者,但结构体大小必须按最大的那支算 ——
    dwSize 对不上的话 API 直接回 ERROR_INVALID_PARAMETER,而那个错误码
    跟「文件不存在」是同一个,排查起来会绕远。所以后面补 8 字节。
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("FileId", ctypes.c_longlong),
        ("_pad", ctypes.c_byte * 8),
    ]


class FileIdOpener:
    """握着一个卷句柄,按引用问路径。

    用法::

        with FileIdOpener("C:") as op:
            path = op.path_of(0x00030000000f757c)    # -> r"\\\\?\\C:\\Windows"

    开不出句柄(盘不在、盘没挂上)时构造会抛 OSError —— 调用方该把这当成
    「这次没有这个能力」,而不是错误。
    """

    def __init__(self, drive: str) -> None:
        if sys.platform != "win32":       # pragma: no cover - 仅 Windows
            raise OSError("按文件编号打开只在 Windows 上可用")
        self.drive = drive.rstrip("\\").rstrip(":").upper() + ":"
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()
        # 提示句柄 = 盘根目录。开目录必须给 FILE_FLAG_BACKUP_SEMANTICS,
        # 否则 CreateFileW 对目录一律失败(错误 5)。
        # 三个共享位都要给:根目录一直有别的进程在用,少一个就开不上。
        h = self._k32.CreateFileW(
            f"{self.drive}\\",
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if h is None or h == _INVALID:
            raise OSError(ctypes.get_last_error(), f"开不了 {self.drive}\\ 的句柄")
        self._handle = h

    def _bind(self) -> None:
        k = self._k32
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.OpenFileById.restype = wintypes.HANDLE
        k.OpenFileById.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(FILE_ID_DESCRIPTOR), wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        k.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

    def path_of(self, file_reference: int) -> str | None:
        """按完整 64 位引用问路径。拿不到返回 None。

        返回的是 \\\\?\\C:\\Windows 这种形式,而且是**重解析之后**的规范路径 ——
        如果目录能通过联接点访问到,可能给出联接点那一侧。实测
        C:\\Users\\me\\AppData\\Local 会返回
        ...\\AppData\\Local\\Packages\\Claude_xxx\\LocalCache\\Local。
        两条都合法、指向同一个目录,但显示出来会让人以为文件在别处。
        清洗和判断留给调用方(changes._clean_final_path)。
        """
        if not file_reference:
            return None

        desc = FILE_ID_DESCRIPTOR()
        desc.dwSize = ctypes.sizeof(FILE_ID_DESCRIPTOR)
        desc.Type = FileIdType
        # ctypes 的 c_longlong 是有符号的,而引用的最高位可能是 1
        # (序列号大的时候)。不折成负数会 OverflowError。
        desc.FileId = file_reference - (1 << 64) if file_reference >> 63 else file_reference

        h = self._k32.OpenFileById(
            self._handle, ctypes.byref(desc), GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None, FILE_FLAG_BACKUP_SEMANTICS,
        )
        if h is None or h == _INVALID:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = self._k32.GetFinalPathNameByHandleW(h, buf, 1024, VOLUME_NAME_DOS)
            # n 为 0 是失败;n >= 1024 表示缓冲不够(路径超过 1023 字符,
            # 极少见)—— 两种都当拿不到,不重试。多要一次缓冲的收益
            # 抵不上多一轮系统调用。
            if n == 0 or n >= 1024:
                return None
            return buf.value
        finally:
            self._k32.CloseHandle(h)

    def close(self) -> None:
        h = getattr(self, "_handle", None)
        if h:
            self._k32.CloseHandle(h)
            self._handle = None

    def __enter__(self) -> "FileIdOpener":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
