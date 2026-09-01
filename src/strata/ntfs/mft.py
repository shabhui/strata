"""流式解析整个 $MFT,产出文件条目并还原完整路径。

流程:
  1. 读 MFT 记录 0(即 $MFT 自身),从它的 $DATA 属性拿到整个 MFT 的运行列表
  2. 按运行列表大块顺序读,逐条解析记录
  3. 每条记录取名字 + 父引用 + 大小 + 时间戳
  4. 沿父引用链还原完整路径(带记忆化,防环)

比目录遍历快一个数量级,而且按记录号去重 —— 硬链接只算一次,
不会像目录遍历那样把 WinSxS 的链接重复累加。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import attributes as A
from .runlist import Run, decode_runlist, iter_extents
from .volume import NtfsError, Volume

# 元文件占据前 16 条记录($MFT / $LogFile / $Bitmap 等)
FIRST_USER_RECORD = 16
ROOT_RECORD = 5

# 一次读多少条记录。1024 字节/条时约 8 MB 一批。
CHUNK_RECORDS = 8192

# Compact OS(WOF)压缩把真实数据搬进这条备用流,主数据流退化成重解析点,
# 而且**故意不设 FILE_ATTRIBUTE_COMPRESSED 位** —— 为了对老程序透明。
# 所以只能靠流名认。实测本机 kernel32.dll / notepad.exe / explorer.exe 全是
# 这个形状,逻辑大小是真实占盘的 2.7~2.8 倍;整盘因此虚报约 27 GB(+15.9%)。
# 名字按 UTF-16-LE 存,17 个字符。长度先挡一道,绝大多数备用流一比就出局。
WOF_STREAM = "wofcompresseddata"
WOF_STREAM_CHARS = len(WOF_STREAM)

#: 下面那个 for 循环认得的类型码,一个不多。传给 iter_attributes 让它别为
#: 别的类型建属性头 —— 目录身上挂着 $INDEX_ROOT / $INDEX_ALLOCATION / $BITMAP,
#: 每条都建一个 16 字段的对象再丢掉。加类型码时**这里和循环里要一起加**:
#: 只加循环不加这里,那个分支就永远进不去(而且不报错,只是数字变小)。
_WANTED_ATTRS = frozenset(
    (A.ATTR_STANDARD_INFORMATION, A.ATTR_FILE_NAME, A.ATTR_DATA)
)


@dataclass(slots=True)
class FileEntry:
    """一条 MFT 记录归纳出的信息。"""

    record: int
    parent: int
    name: str
    is_dir: bool
    bytes: int = 0            # 占盘大小(分配量)
    logical_bytes: int = 0    # 逻辑大小
    created: float | None = None
    modified: float | None = None
    attributes: int = 0
    hard_links: int = 1
    is_metafile: bool = False
    has_data: bool = False
    # 见到 WofCompressedData 这条流就置位。必须记在条目上而不是就地判断:
    # 幻影流和真实流可能分在不同的 MFT 记录里,得等所有记录读完才能定论。
    # 详见 _apply_pending 和 tests/test_wof_compression.py。
    wof: bool = False

    @property
    def is_reparse(self) -> bool:
        return bool(self.attributes & A.FILE_ATTR_REPARSE_POINT)


@dataclass(slots=True)
class MftStats:
    """一次解析的统计,用于诊断和界面显示。"""

    records_seen: int = 0
    records_in_use: int = 0
    files: int = 0
    dirs: int = 0
    extension_records: int = 0
    fixup_failures: int = 0
    parse_failures: int = 0
    unnamed: int = 0
    bytes_total: int = 0
    duration_ms: int = 0
    mft_bytes: int = 0
    orphaned: int = 0
    cycles: int = 0


class MftReader:
    """从一个 NTFS 卷读取所有文件条目。"""

    def __init__(self, volume: Volume) -> None:
        self.vol = volume
        self.boot = volume.boot
        self.record_size = self.boot.bytes_per_mft_record
        self.sector_size = self.boot.bytes_per_sector
        self.stats = MftStats()
        self._runs: list[Run] | None = None

    # ---- MFT 自身的运行列表 ----

    def mft_runs(self) -> list[Run]:
        """解析记录 0 拿到 $MFT 的数据运行。"""
        if self._runs is not None:
            return self._runs

        raw = bytearray(self.vol.read(self.boot.mft_offset, self.record_size))
        if len(raw) < self.record_size:
            raise NtfsError("读取 MFT 记录 0 时数据不足")
        if bytes(raw[0:4]) != A.MAGIC_FILE:
            raise NtfsError(f"MFT 记录 0 缺少 FILE 标记(读到 {bytes(raw[0:4])!r})")

        A.apply_fixups(raw, 0, self.record_size, self.sector_size)
        header = A.parse_record_header(raw)

        for attr, off in A.iter_attributes(raw, header, 0, self.record_size):
            if attr.type_code != A.ATTR_DATA or attr.name_length != 0:
                continue
            if not attr.non_resident:
                raise NtfsError("$MFT 的 $DATA 意外为常驻属性")
            start = off + attr.runlist_offset
            end = off + attr.length
            runs = decode_runlist(memoryview(raw)[start:end])
            if not runs:
                raise NtfsError("$MFT 的运行列表为空")
            self._runs = runs
            self.stats.mft_bytes = attr.allocated_size
            return runs

        raise NtfsError("$MFT 记录里没有找到未命名的 $DATA 属性")

    # ---- 记录解析 ----

    def _parse_record(self, buf: bytearray, offset: int, expect_record: int) -> tuple[FileEntry | None, tuple[int, int, int] | None]:
        """解析一条记录。

        返回 (条目, 扩展记录的大小贡献)。两者都可能为 None。
        扩展贡献形如 (基记录号, 分配大小, 逻辑大小)。
        """
        # startswith 而不是切片比较:切一次就多一个 bytes 对象,
        # 而这行每条记录都要走一遍(本机 161 万次)
        if not buf.startswith(A.MAGIC_FILE, offset):
            # BAAD 或全零(未分配)都属正常,不算错误
            return None, None

        self.stats.records_seen += 1

        # 先解头、先看在用位,**再**做 fixup —— 空闲记录不值得还原 USA
        # (本机 161 万条里 40 万条是空闲的)。
        #
        # 这么排是可以证明的,不是「试了没出事」:USA 替换动的是每个扇区最后
        # 两字节,第一个坑在 sector_size - 2;扇区最小 512,所以第一个坑在 510,
        # 而记录头一共 48 字节(A._REC_HEADER.size),整个头都在坑下面。
        # tests/test_parse_fast_path.py 把这个算术钉住了 —— 记录头哪天跨过去,
        # 那里会先响。
        try:
            header = A.parse_record_header(buf, offset)
        except Exception:
            self.stats.parse_failures += 1
            return None, None

        if not header.in_use:
            return None, None
        self.stats.records_in_use += 1

        try:
            A.apply_fixups(buf, offset, self.record_size, self.sector_size)
        except A.FixupError:
            self.stats.fixup_failures += 1
            return None, None

        record_number = header.record_number
        # 有些卷的记录号字段不可靠,以序号位置为准
        if record_number != expect_record:
            record_number = expect_record

        best_name: A.FileNameInfo | None = None
        std: A.StandardInfo | None = None
        alloc = 0
        logical = 0
        named_alloc = 0
        has_data = False
        wof = False

        for attr, attr_off in A.iter_attributes(
            buf, header, offset, self.record_size, _WANTED_ATTRS
        ):
            code = attr.type_code
            if code == A.ATTR_STANDARD_INFORMATION:
                std = A.parse_standard_information(buf, attr, attr_off)
            elif code == A.ATTR_FILE_NAME:
                info = A.parse_file_name(buf, attr, attr_off)
                if info is not None and (best_name is None or info.rank > best_name.rank):
                    best_name = info
            elif code == A.ATTR_DATA:
                size = A.parse_data_size(buf, attr, attr_off)
                if size is None:
                    continue
                has_data = True
                if size.named:
                    # 备用数据流也占盘,单独累加
                    named_alloc += size.allocated
                    # 长度先挡:名字不是 17 个字符的直接不用取出来比
                    if attr.name_length == WOF_STREAM_CHARS:
                        start = attr_off + attr.name_offset
                        raw_name = bytes(buf[start : start + WOF_STREAM_CHARS * 2])
                        if raw_name.decode("utf-16-le", "replace").lower() == WOF_STREAM:
                            wof = True
                else:
                    alloc = size.allocated
                    logical = size.real

        if header.is_extension:
            self.stats.extension_records += 1
            # 未命名和备用流分开报,WOF 的判断留给 _apply_pending ——
            # 幻影流和真实流可能落在不同记录里,这条记录看不到全貌。
            #
            # 条件里**故意不带** named_alloc。曾经加过,想的是「只有备用流的
            # 扩展记录也该报,否则那些字节凭空消失」。加完实测总量反而从
            # +15.9% 涨到 +19.5%(tools/verify_wof_fix.py):被放进来的最大一条是
            # $UsnJrnl 的 $J 流,39.83 GiB(tools/probe_wof_shapes.py 排出来的)。
            # 那是 USN 变更日志,一个环形缓冲 —— 旧区间早释放了,allocated_size
            # 报的是整个逻辑区间,真实占用只有活动窗口那几十 MB。
            #
            # 所以这里维持原样。这不是「$J 该被丢掉」的理由,只是「按
            # allocated_size 把它算进来更错」。
            #
            # 真要算准得走运行列表只数非稀疏段。这条规则**已经在真盘上验过一半**
            # (tools/probe_runlist_truth.py,提权跑):
            #
            #   kernel32.dll  扩展记录 729,364
            #     $DATA(未命名)              allocated 0.81M  运行列表 0.00M  1 段全稀疏
            #     $DATA:"WofCompressedData"  allocated 0.45M  运行列表 0.45M
            #     系统报真实占盘 0.45M  → 运行列表 ✓ 对上,allocated ✗ 多 0.81M
            #
            # 幻影流是**一整段稀疏**,所以走运行列表自然得 0 —— 不用按流名认
            # WOF,规则自己就能得出正确答案。这比现在这套按名字匹配更根本。
            #
            # 没验的那一半是 $UsnJrnl 的 $J:它的基记录 77,516 里只有一条很小的
            # $Max,$J 在哪条扩展记录上还没找到(那个工具按 base_reference 反查,
            # 对 notepad.exe 也没找到,说明反查这条路本身还有问题)。
            # 换成运行列表规则要动的是所有文件的字节口径,只验证了一半就上
            # 不合算 —— 而且对总量的影响很小:WOF 那部分两种规则得出同一个数,
            # 差别只在 probe_wof_shapes.py 里那 4 个「幻影在基记录」的文件(1.13G)。
            if has_data and (alloc or logical):
                return None, (
                    header.base_record_number, alloc, named_alloc, logical, wof
                )
            return None, None

        if best_name is None:
            self.stats.unnamed += 1
            return None, None

        is_dir = header.is_directory
        # WOF 压缩:未命名流的 allocated 报的是**逻辑大小**(而且带稀疏位,
        # 跟「稀疏 = 分配得少」的直觉正好相反),真实字节全在备用流里。
        # 所以只算备用流之和 —— 之和而不是只算 WofCompressedData,因为一个
        # 文件可以既被 WOF 压着、又带 Zone.Identifier 那样真占盘的流。
        total_alloc = 0 if is_dir else (named_alloc if wof else alloc + named_alloc)
        total_logical = 0 if is_dir else logical

        # 目录本身不占数据空间,索引开销忽略不计
        entry = FileEntry(
            record=record_number,
            parent=best_name.parent,
            name=best_name.name,
            is_dir=is_dir,
            bytes=total_alloc,
            logical_bytes=total_logical,
            created=(std.created if std else None) or best_name.created,
            modified=(std.modified if std else None) or best_name.modified,
            attributes=(std.attributes if std else best_name.attributes),
            hard_links=header.hard_link_count,
            is_metafile=record_number < FIRST_USER_RECORD,
            has_data=has_data,
            wof=wof,
        )

        if is_dir:
            self.stats.dirs += 1
        else:
            self.stats.files += 1
            self.stats.bytes_total += total_alloc

        return entry, None

    # ---- 主循环 ----

    def read_entries(self, progress: Callable[[int], None] | None = None) -> list[FileEntry]:
        """解析整个 MFT,返回条目列表(按记录号索引的稀疏结构会在 tree 层构建)。"""
        t0 = time.perf_counter()
        runs = self.mft_runs()
        bpc = self.boot.bytes_per_cluster
        rec_size = self.record_size

        entries: list[FileEntry] = []
        # 基记录缺 $DATA 时,用扩展记录的大小补上
        pending: dict[int, tuple[int, int]] = {}

        record_index = 0
        chunk_bytes = CHUNK_RECORDS * rec_size

        # 整趟就这一块。原来每块 `bytearray(raw)` 新分配 8 MiB,而这个循环
        # 同时在往 entries 里攒上百万个条目 —— 这两件事同时成立时解析速度会
        # 从 9 µs/条掉到 53 µs/条(前 5 块 74ms、后 5 块 440ms,越跑越慢)。
        # 复用之后 161 万条从 72.6 秒降到 15.5 秒。
        #
        # 单独任何一个条件都不会触发:只新分配不留条目稳定在 68→74ms,
        # 只留条目不新分配稳定在 75→76ms。四个变体的对照在
        # tools/prof_mft_buffer.py,排掉掉频/换页/GC 的过程在
        # tests/test_mft_buffer_reuse.py 的开头。
        scratch = bytearray(chunk_bytes)

        for run in runs:
            if run.sparse or run.lcn is None:
                # 稀疏段不含记录,但要推进记录号
                record_index += (run.length * bpc) // rec_size
                continue

            run_bytes = run.length * bpc
            base_offset = run.lcn * bpc
            consumed = 0

            while consumed < run_bytes:
                want = min(chunk_bytes, run_bytes - consumed)
                try:
                    got = self.vol.read_into(base_offset + consumed, want, scratch)
                except NtfsError:
                    # 坏块:跳过这一批,继续往下
                    self.stats.parse_failures += 1
                    record_index += want // rec_size
                    consumed += want
                    continue

                if not got:
                    break
                # 只解这次真读到的部分。缓冲区是复用的,got 之后是上一块的
                # 残留 —— 最后一块几乎必然读不满,照 len(scratch) 解会把残留
                # 当记录,解出一堆重复记录号。
                count = got // rec_size

                for i in range(count):
                    entry, ext = self._parse_record(scratch, i * rec_size, record_index + i)
                    if entry is not None:
                        entries.append(entry)
                    elif ext is not None:
                        base, unnamed, named, l, w = ext
                        prev = pending.get(base)
                        if prev is None:
                            pending[base] = (unnamed, named, l, w)
                        else:
                            pending[base] = (
                                prev[0] + unnamed,
                                prev[1] + named,
                                prev[2] + l,
                                prev[3] or w,
                            )

                record_index += count
                consumed += got

                if progress is not None:
                    progress(record_index)

                if got < want:
                    break

        self._apply_pending(entries, pending)
        self.stats.duration_ms = int((time.perf_counter() - t0) * 1000)
        return entries

    def _apply_pending(
        self,
        entries: list[FileEntry],
        pending: dict[int, tuple[int, int, int, bool]],
    ) -> None:
        """把扩展记录里的大小合并到对应基记录。

        高度碎片化的大文件($DATA 被挤到扩展记录)靠这一步才拿到正确大小。

        WOF 压缩的文件必须在这里定论,不能在单条记录里判:幻影的未命名流和
        真实的 WofCompressedData 流可能分在不同记录上。实测 Sessions.xml 就是
        这样 —— 基记录带真实流 17.85M,扩展记录带幻影流 137.88M。原来这里对
        两者取 max(),于是幻影赢了,库里记的就是 137.88M(逻辑大小)。
        所以 pending 存的是拆开的 (未命名, 备用流, 逻辑, 是否WOF),
        等两边的 wof 标记合并之后再决定算哪个。
        """
        if not pending:
            return
        by_record = {e.record: e for e in entries}
        for record, (unnamed, named, logical, wof) in pending.items():
            entry = by_record.get(record)
            if entry is None or entry.is_dir:
                continue
            if wof:
                entry.wof = True
            alloc = named if entry.wof else unnamed + named
            # 基记录已有大小时取较大值,避免重复累加同一条流
            if entry.bytes == 0:
                entry.bytes = alloc
                self.stats.bytes_total += alloc
            elif alloc > entry.bytes:
                self.stats.bytes_total += alloc - entry.bytes
                entry.bytes = alloc
            # 逻辑大小单独取,不跟着 alloc 的分支走 —— WOF 文件的 alloc 会
            # 被判成 0(幻影不算),但那条幻影流的 real_size 恰恰是唯一能
            # 拿到的真实逻辑大小,丢了界面上「文件多大」就成 0 了。
            if logical > entry.logical_bytes:
                entry.logical_bytes = logical


def resolve_paths(
    entries: list[FileEntry],
    *,
    on_orphan: str = "skip",
) -> tuple[dict[int, str], MftStats]:
    """沿父引用链还原每个目录的完整路径(不含盘符)。

    只给目录建路径表,文件路径由「父目录路径 + 名字」现拼,省内存。
    返回 (记录号 → 目录路径, 统计)。
    """
    stats = MftStats()
    dirs: dict[int, FileEntry] = {e.record: e for e in entries if e.is_dir}
    cache: dict[int, str] = {ROOT_RECORD: ""}

    def resolve(record: int) -> str | None:
        if record in cache:
            return cache[record]
        chain: list[int] = []
        cur = record
        seen: set[int] = set()

        while True:
            if cur in cache:
                base = cache[cur]
                break
            if cur in seen:
                # 父链成环,整条链判为孤立
                stats.cycles += 1
                for r in chain:
                    cache[r] = None  # type: ignore[assignment]
                return None
            seen.add(cur)
            entry = dirs.get(cur)
            if entry is None:
                stats.orphaned += 1
                for r in chain:
                    cache[r] = None  # type: ignore[assignment]
                cache[cur] = None  # type: ignore[assignment]
                return None
            chain.append(cur)
            cur = entry.parent

        # 自底向上填缓存
        for r in reversed(chain):
            entry = dirs[r]
            base = f"{base}\\{entry.name}" if base else entry.name
            cache[r] = base
        return cache.get(record)

    for record in list(dirs):
        resolve(record)

    return {k: v for k, v in cache.items() if v is not None}, stats
