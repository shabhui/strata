"""MFT 那条路不能把 NTFS 元文件算成用户数据。

背景(实测,不是推测):提权修好之后第一次真跑通 MFT,结果两个盘的
scanned_bytes 都比系统报的已用量多出整整一卷 ——

    C:  MFT 说 397.1 GB,系统说 169.2 GB,多出来的是整卷的 1.041 倍
    D:  MFT 说 1389.7 GB,系统说 637.8 GB,1.030 倍

零头是 $MFT 自己(C: 上 160 万条记录约 1.6 GB)。主因是 $BadClus:$Bad:
它是稀疏流,allocated_size 按定义就等于整卷容量,而 parse_data_size() 对
非常驻属性一律取 allocated_size —— 那一条记录就把整块盘报了一遍。

更糟的是这些字节还看不见:元文件全挂在盘根下,而 prune_tree() 跳过根节点
那一行(tree.py 里 `if node.path == "": continue`),所以总数里有、树里
查无此人。当时的现象是顶层 28 项加起来 190 GB,总数写着 397 GB。

FileEntry.is_metafile 字段一直存在(mft.py 里按 record < 16 赋值),
_mft_to_scan_entries() 的注释也写着「元文件目录也跳过」,但代码只跳了
ROOT_RECORD —— 这个过滤从来没被实现过。这个文件盯的就是它。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strata import config
from strata.ntfs import mft
from strata.scan import snapshot, tree

ROOT = mft.ROOT_RECORD          # 5
FIRST_USER = mft.FIRST_USER_RECORD  # 16
VOLUME_BYTES = 213_584_441_344  # 199 GiB,照本机 C: 的实际容量取


def _root():
    return mft.FileEntry(record=ROOT, parent=ROOT, name=".", is_dir=True)


def _badclus():
    """$BadClus:整卷容量报成一个文件,就是它把总数顶上去的。"""
    return mft.FileEntry(
        record=8,
        parent=ROOT,
        name="$BadClus",
        is_dir=False,
        bytes=VOLUME_BYTES,
        is_metafile=True,
        has_data=True,
    )


def _mft_self(size: int = 1_600_000_000):
    return mft.FileEntry(
        record=0,
        parent=ROOT,
        name="$MFT",
        is_dir=False,
        bytes=size,
        is_metafile=True,
        has_data=True,
    )


def _user_file(record: int = 100, name: str = "real.bin", size: int = 4096):
    return mft.FileEntry(
        record=record, parent=ROOT, name=name, is_dir=False, bytes=size, has_data=True
    )


def _total(entries: list[tree.ScanEntry]) -> int:
    _nodes, scanned, _files = tree.build_tree(entries)
    return scanned


class MetafilesExcludedTest(unittest.TestCase):
    def test_badclus_does_not_land_in_the_total(self):
        """核心那条:整卷容量不能算进 scanned_bytes。

        断言的是「总数只剩用户文件」,不是「总数小于某个数」—— 后者
        $BadClus 只被算一半也能过。
        """
        out, _orphan, _warn = snapshot._mft_to_scan_entries(
            [_root(), _badclus(), _user_file(size=4096)]
        )
        self.assertEqual(_total(out), 4096)

    def test_all_metafile_records_are_excluded(self):
        """不是只挡 $BadClus 一条,记录号 < 16 的文件都不算。"""
        entries = [_root()]
        for rec in range(FIRST_USER):
            if rec == ROOT:
                continue
            entries.append(
                mft.FileEntry(
                    record=rec,
                    parent=ROOT,
                    name=f"$meta{rec}",
                    is_dir=False,
                    bytes=10 * 2**30,
                    is_metafile=True,
                    has_data=True,
                )
            )
        entries.append(_user_file(size=777))
        out, _orphan, _warn = snapshot._mft_to_scan_entries(entries)
        self.assertEqual(_total(out), 777)
        names = {e.path for e in out}
        self.assertFalse(
            {n for n in names if n.startswith("$meta")},
            f"元文件漏进来了:{sorted(n for n in names if n.startswith('$meta'))}",
        )

    def test_user_files_at_drive_root_still_count(self):
        """别把「挂在盘根下」当成排除条件 —— pagefile.sys 就在盘根,是真实占用。"""
        page = _user_file(record=200, name="pagefile.sys", size=8 * 2**30)
        out, _orphan, _warn = snapshot._mft_to_scan_entries(
            [_root(), _badclus(), page]
        )
        self.assertEqual(_total(out), 8 * 2**30)
        self.assertIn("pagefile.sys", {e.path for e in out})

    def test_metafile_directories_are_kept(self):
        """$Extend(记录号 11)是目录,它下面 $RmMetadata 之类是真实占用。

        只过滤文件形态的元文件。整个记录段一刀切会让 $Extend 的子项失去父链,
        那些字节会变成「无法归属」,换一种方式丢掉。
        """
        extend = mft.FileEntry(
            record=11, parent=ROOT, name="$Extend", is_dir=True, is_metafile=True
        )
        child_dir = mft.FileEntry(
            record=1000, parent=11, name="$RmMetadata", is_dir=True
        )
        child_file = mft.FileEntry(
            record=1001, parent=1000, name="$TxfLog.blf", is_dir=False,
            bytes=64 * 2**20, has_data=True,
        )
        out, _orphan, _warn = snapshot._mft_to_scan_entries(
            [_root(), extend, child_dir, child_file]
        )
        paths = {e.path for e in out}
        self.assertIn("$Extend", paths)
        self.assertIn("$Extend\\$RmMetadata", paths)
        self.assertIn("$Extend\\$RmMetadata\\$TxfLog.blf", paths)
        self.assertEqual(_total(out), 64 * 2**20)


class MetafileWarningTest(unittest.TestCase):
    """排掉的量必须报出来。

    这个数很大(含一整卷),看的人一定会拿它跟资源管理器对。不说清它是什么,
    「为什么差这么多」就没法回答;而静悄悄扣掉几百 GB 比多算更难查。
    """

    def test_warning_names_the_amount_and_the_reason(self):
        _out, _orphan, warns = snapshot._mft_to_scan_entries(
            [_root(), _badclus(), _mft_self(), _user_file()]
        )
        hit = [w for w in warns if "元文件" in w]
        self.assertEqual(len(hit), 1, f"没有元文件警告:{warns}")
        text = hit[0]
        # 数字得是真实排掉的量,不是随便一个占位
        expected = (VOLUME_BYTES + 1_600_000_000) / 2**30
        self.assertIn(f"{expected:.2f} GiB", text)
        self.assertIn("$BadClus", text)

    def test_no_warning_when_there_are_no_metafiles(self):
        """没排掉东西就不要提 —— 每次都出现的警告等于没有警告。"""
        _out, _orphan, warns = snapshot._mft_to_scan_entries(
            [_root(), _user_file()]
        )
        self.assertFalse([w for w in warns if "元文件" in w], warns)


class PreferMftDefaultTest(unittest.TestCase):
    """默认不走 MFT。

    实测 MFT 在这份实现上比 scandir 慢 2.6 倍(C: 100.5s vs 38.1s,原因是
    read_entries 单线程逐条纯 Python 解析,而 scandir 那条路有线程池)。
    提权成功反而让扫描变慢,这个默认值得钉住,别哪天顺手翻回去。
    """

    def test_config_default_is_off(self):
        self.assertIs(config.PREFER_MFT, False)

    def test_none_means_read_from_config(self):
        """collect_entries 不传 prefer_mft 时要去问 config,不是自己写死 False。

        钉的是「跟着配置走」:把 config 改成 True 就必须去试 MFT。
        只断言默认走 scandir 的话,函数体里写死 False 也能过。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "a.bin").write_bytes(b"x" * 16)

        with mock.patch.object(config, "PREFER_MFT", True):
            with mock.patch.object(
                snapshot, "Volume", side_effect=snapshot.AccessDenied("拒绝")
            ) as vol:
                _e, method, _w, reason = snapshot.collect_entries(tmp.name)
        self.assertTrue(vol.called, "config.PREFER_MFT=True 时没去试 MFT")
        self.assertEqual(method, "scandir")
        self.assertIn("管理员权限", reason)

        with mock.patch.object(config, "PREFER_MFT", False):
            with mock.patch.object(snapshot, "Volume") as vol:
                _e, method, _w, reason = snapshot.collect_entries(tmp.name)
        self.assertFalse(vol.called, "config.PREFER_MFT=False 时仍然打开了卷")
        self.assertEqual(method, "scandir")
        self.assertIn("config.PREFER_MFT", reason)


if __name__ == "__main__":
    unittest.main()
