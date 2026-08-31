"""整趟扫描只用一块缓冲区。

这一条是性能修复的守卫。原来 read_entries 每读一块就 `bytearray(raw)` 新分配
8 MiB(mft.py:254),而它同时手里攥着越来越长的 entries 列表。实测这两件事
**同时成立**才会出问题,单独任何一个都没事:

    每块新分配 + 留住条目    72.6s   前 5 块 74ms,后 5 块 440ms   慢 6.0x
    每块新分配 + 不留条目    40.4s   前 5 块 68ms,后 5 块  74ms   稳定
    复用一块   + 留住条目    15.5s   前 5 块 75ms,后 5 块  76ms   稳定
    复用一块   + 不留条目    15.1s                                稳定

(tools/prof_mft_buffer.py,161 万条合成记录,每个变体独立进程)

而 read_entries 恰好两个条件都满足。161 万条从 72.6 秒降到 15.5 秒,
快 4.68 倍 —— 这是 C: 那 100 秒里最大的一块。

排查过程排掉的其他嫌疑,留个记录省得以后重走:

    硬件掉频    纯整数循环满载 80 秒只慢 1.04x(prof_cpu_sustained.py)
    内存换页    缺页数每块稳定 2,800 次,从头到尾不变
    GC          gc.disable() 只省 5%
    读盘慢      1.5 GiB 的 MFT 只要 1.3 秒,NO_BUFFERING 无罪
                (bench_nobuffering.py:两种读法都 583~1699 MB/s)
    路径还原    resolve_paths 只值 1.1 秒(prof_mft_convert.py)

为什么盯「同一个对象」而不是盯耗时:耗时断言在别人机器上会飘,而且慢下来
是渐进的 —— 小样本上看不出来,真机上才炸。盯对象身份是这条修复的因,
测得准也测得稳。
"""

from __future__ import annotations

import struct
import unittest

from strata.ntfs import mft
from strata.ntfs.volume import BootSector

from . import mft_fixtures as fx

BYTES_PER_SECTOR = 512
BYTES_PER_CLUSTER = 4096
REC = 1024
MFT_CLUSTER = 100


def boot() -> BootSector:
    return BootSector(
        bytes_per_sector=BYTES_PER_SECTOR,
        sectors_per_cluster=BYTES_PER_CLUSTER // BYTES_PER_SECTOR,
        total_sectors=1_000_000,
        mft_cluster=MFT_CLUSTER,
        mft_mirror_cluster=2,
        bytes_per_mft_record=REC,
        serial=0x1122334455667788,
    )


class RecordingVolume:
    """记下每次读用的是哪个缓冲区对象。

    read 和 read_into 分开计数。read 只该在开头被调一次(mft_runs 读记录 0
    拿运行列表),之后整个热循环必须走 read_into —— 要是热循环还在用 read,
    plain_reads 会涨,下面那条断言就红。
    """

    def __init__(self, image: bytes) -> None:
        self.image = image
        self.boot = boot()
        self.buffers: list[int] = []      # read_into 每次拿到的缓冲区 id()
        self.sizes: list[int] = []        # 每次读的长度
        self.plain_reads = 0              # 走老 read() 的次数

    def read(self, offset: int, length: int) -> bytes:
        self.plain_reads += 1
        return self.image[offset : offset + length]

    def read_into(self, offset: int, length: int, buf: bytearray) -> int:
        self.buffers.append(id(buf))
        self.sizes.append(length)
        chunk = self.image[offset : offset + length]
        buf[: len(chunk)] = chunk
        return len(chunk)


def mft_self_record(n_clusters: int) -> bytes:
    """记录 0:$MFT 自身,$DATA 指向 MFT 占的簇。

    长度字段用两字节(头字节 0x22):这个用例要跨 3 个读块,合成的 MFT 有
    4,000 多簇,一字节装不下 —— 掩成一字节的话只有前 255 簇会被读,
    热循环就只跑一块,那两条跨块的断言会变成永远通过。
    """
    runlist = bytes([0x22]) + struct.pack("<H", n_clusters) + struct.pack("<h", MFT_CLUSTER)
    return fx.make_mft_record(
        record_number=0,
        flags=0x0001,
        attributes=[
            fx.attr_standard_information(),
            fx.attr_file_name(parent=5, name="$MFT"),
            fx.attr_data_nonresident(
                runlist=runlist,
                allocated=n_clusters * BYTES_PER_CLUSTER,
                real=n_clusters * BYTES_PER_CLUSTER,
            ),
        ],
        record_size=REC,
        sector_size=BYTES_PER_SECTOR,
    )


def build_image(n_files: int) -> tuple[bytes, int]:
    """造一张够大的 MFT 镜像,横跨多个读块。返回 (镜像, 文件数)。

    读块是 CHUNK_RECORDS 条一批,所以要跨块就得超过那个数 —— 只有跨块
    才测得到「第二块还用不用同一个缓冲区」。
    """
    records = [mft_self_record(0)]      # 占位,长度算出来再补
    # 记录 1..4 空着(元文件),5 是根目录
    for i in range(1, 5):
        records.append(b"\x00" * REC)
    records.append(fx.make_mft_record(
        record_number=5, flags=0x0003,
        attributes=[fx.attr_standard_information(),
                    fx.attr_file_name(parent=5, name=".")],
        record_size=REC, sector_size=BYTES_PER_SECTOR,
    ))
    for i in range(n_files):
        rec = 6 + i
        records.append(fx.make_mft_record(
            record_number=rec, flags=0x0001,
            attributes=[
                fx.attr_standard_information(),
                fx.attr_file_name(parent=5, name=f"f{rec}.dat"),
                fx.attr_data_nonresident(
                    runlist=bytes([0x11, 1, 200]),
                    allocated=4096, real=1000,
                ),
            ],
            record_size=REC, sector_size=BYTES_PER_SECTOR,
        ))

    total = len(records) * REC
    n_clusters = -(-total // BYTES_PER_CLUSTER)
    records[0] = mft_self_record(n_clusters)

    image = bytearray(MFT_CLUSTER * BYTES_PER_CLUSTER)
    image += b"".join(records)
    image += b"\x00" * (n_clusters * BYTES_PER_CLUSTER - total)
    return bytes(image), n_files


class OneBufferForTheWholeScan(unittest.TestCase):
    def setUp(self) -> None:
        # 跨 3 个读块:CHUNK_RECORDS * 2 + 一点零头
        self.n_files = mft.CHUNK_RECORDS * 2 + 100
        image, _ = build_image(self.n_files)
        self.vol = RecordingVolume(image)
        self.reader = mft.MftReader(self.vol)

    def test_reads_more_than_one_chunk(self) -> None:
        """先确认这个用例真的跨了块 —— 不跨块的话下面那条永远通过。"""
        self.reader.read_entries()
        self.assertGreater(
            len(self.vol.buffers), 1,
            "只读了一块,测不出「第二块还用不用同一块内存」",
        )

    def test_the_hot_loop_does_not_use_plain_read(self) -> None:
        """热循环必须走 read_into。

        read() 每次都新建一个 bytes,再 bytearray() 一遍 —— 那正是要修掉的
        分配模式。只允许开头 mft_runs() 读记录 0 那一次。
        """
        self.reader.read_entries()
        self.assertLessEqual(
            self.vol.plain_reads, 1,
            f"热循环调了 {self.vol.plain_reads} 次 read() —— 每次都新分配,"
            f"复用缓冲区就白做了",
        )

    def test_every_read_uses_the_same_buffer(self) -> None:
        """整趟只有一个缓冲区对象。

        这是 4.68 倍那个修复的因。每块新分配的话,这里会看到一串不同的 id。
        """
        self.reader.read_entries()
        unique = set(self.vol.buffers)
        self.assertEqual(
            len(unique), 1,
            f"{len(self.vol.buffers)} 次读用了 {len(unique)} 个不同的缓冲区 —— "
            f"每块新分配 8 MiB,配上越来越长的 entries 列表,实测慢 4.68 倍",
        )

    def test_entries_are_still_correct(self) -> None:
        """复用缓冲区不能改变结果。

        复用最容易犯的错是没清干净:上一块的尾巴留在缓冲区里,这一块读得
        比缓冲区短的时候(最后一块必然如此),就会把上一块的残留当记录解。
        """
        entries = self.reader.read_entries()
        # $MFT 自身也是一条文件记录,它本来就该在结果里 —— 这里只数造出来的那些
        files = [e for e in entries if not e.is_dir and e.name != "$MFT"]
        self.assertEqual(
            len(files), self.n_files,
            f"该有 {self.n_files} 个文件,拿到 {len(files)} 个 —— "
            f"复用缓冲区把结果改了",
        )
        names = {e.name for e in files}
        self.assertIn("f6.dat", names)
        self.assertIn(f"f{6 + self.n_files - 1}.dat", names)
        self.assertEqual(len(names), self.n_files, "有重复名字,说明残留被当成了记录")

    def test_no_phantom_records_from_leftover_bytes(self) -> None:
        """最后一块通常读不满,残留区不能产出条目。

        照 len(scratch) 解而不是照实际读到的字节数解,残留会被当成记录 ——
        实测会多出 8,084 条 fixup 失败(残留大多在 fixup 校验那一步就被挡掉,
        所以先炸的是这个计数,不是重复记录号)。两个都断言:失败计数盯的是
        「有没有拿残留去解」,记录号盯的是「万一解出来了会不会进结果」。
        """
        entries = self.reader.read_entries()
        self.assertEqual(
            self.reader.stats.fixup_failures, 0,
            "有 fixup 失败 —— 缓冲区残留被当成记录送去解析了",
        )
        self.assertEqual(self.reader.stats.parse_failures, 0)
        records = [e.record for e in entries]
        self.assertEqual(len(records), len(set(records)),
                         "有重复记录号 —— 缓冲区残留被解析成了额外条目")


class ReadIntoContract(unittest.TestCase):
    """Volume.read_into 的契约。

    真的 Volume 要管理员权限、要一块真盘,这里测不了。能测的是不依赖句柄的
    那部分:参数校验。这两条走的是真实现的真代码路径 —— 检查发生在
    self._seek() 之前,所以不需要打开任何东西。

    为什么值得测:read_into 不像 read() 那样替调用方兜对齐(那次切片正是
    要省掉的拷贝)。不对齐必须报错而不是悄悄读错位置 —— 读错位置的话
    解析出来的是垃圾,而 fixup 校验会把它当「盘坏了」报告,查起来会一路
    往错的方向走。
    """

    def _unopened(self) -> object:
        """造一个没打开句柄的 Volume,只用来跑参数校验那几行。

        不走 __init__:那会真的去 CreateFileW 打开裸卷。
        """
        from strata.ntfs.volume import BootSector, Volume

        v = Volume.__new__(Volume)
        v.boot = BootSector(
            bytes_per_sector=512, sectors_per_cluster=8, total_sectors=1000,
            mft_cluster=4, mft_mirror_cluster=2, bytes_per_mft_record=1024,
            serial=1,
        )
        return v

    def test_volume_has_read_into(self) -> None:
        from strata.ntfs.volume import Volume

        self.assertTrue(
            hasattr(Volume, "read_into"),
            "Volume 没有 read_into —— read_entries 就没法复用缓冲区",
        )

    def test_unaligned_offset_is_refused(self) -> None:
        from strata.ntfs.volume import NtfsError

        v = self._unopened()
        with self.assertRaises(NtfsError) as cm:
            v.read_into(100, 512, bytearray(4096))     # 100 不是 512 的倍数
        self.assertIn("对齐", str(cm.exception))

    def test_unaligned_length_is_refused(self) -> None:
        from strata.ntfs.volume import NtfsError

        v = self._unopened()
        with self.assertRaises(NtfsError):
            v.read_into(0, 700, bytearray(4096))       # 700 不是 512 的倍数

    def test_buffer_too_small_is_refused(self) -> None:
        """缓冲区装不下的话必须报错。

        不报的话 ReadFile 会照 length 往一块更小的内存里写 —— 那是越界写,
        后果不是异常而是堆被踩坏,可能过很久才在别处炸。
        """
        from strata.ntfs.volume import NtfsError

        v = self._unopened()
        with self.assertRaises(NtfsError) as cm:
            v.read_into(0, 8192, bytearray(4096))
        self.assertIn("装不下", str(cm.exception))

    def test_zero_length_reads_nothing(self) -> None:
        v = self._unopened()
        self.assertEqual(v.read_into(0, 0, bytearray(16)), 0)


if __name__ == "__main__":
    unittest.main()
