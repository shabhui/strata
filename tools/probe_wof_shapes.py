"""真盘上,WOF 文件的幻影流和真实流分别落在哪条记录里。

为什么需要这个:按 WOF 规则改完之后,mft 那条路的总量反而从 +15.9% 涨到
+19.5%(tools/verify_wof_fix.py)。说明两件事同时发生了,一件减一件加,
加的那件更大 —— 但具体是哪种记录形状在加,靠读代码猜不出来。

这个工具不走 MftReader 的合并逻辑,自己从裸卷把每条记录的 $DATA 拆开记下来,
再按基记录号归并,统计每个文件属于哪种形状:

    A  未命名流和 WofCompressedData 在同一条记录
    B  基记录没有未命名流,两者都在扩展记录里        ← kernel32.dll 实测是这种
    C  WofCompressedData 在基记录,幻影在扩展记录     ← Sessions.xml 实测是这种
    D  幻影在基记录,WofCompressedData 在扩展记录     ← max() 会让幻影赢的那种
    E  没有 WofCompressedData(普通文件)

每种形状报三个数:按「未命名 + 备用流」算多少、按「WOF 就只算备用流」算多少、
差多少。这样能直接看出剩下的超算集中在哪一种,以及正确的总量应该是多少。

⚠ 需要管理员权限。只读,不写库、不改文件。

    tools\\run_elevated.bat probe_wof_shapes.py C:
"""

from __future__ import annotations

import ctypes
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from strata.ntfs import attributes as A  # noqa: E402
from strata.ntfs.mft import WOF_STREAM, WOF_STREAM_CHARS, MftReader  # noqa: E402
from strata.ntfs.volume import AccessDenied, Volume, volume_space  # noqa: E402

GIB = 2**30
FIRST_USER = 16


class Parts:
    """一个文件(按基记录号归并)的各部分。"""

    __slots__ = ("base_unnamed", "base_named", "base_wof",
                 "ext_unnamed", "ext_named", "ext_wof", "is_dir", "seen_base",
                 "name", "attrs", "links", "sparse")

    def __init__(self) -> None:
        self.base_unnamed = 0
        self.base_named = 0
        self.base_wof = False
        self.ext_unnamed = 0
        self.ext_named = 0
        self.ext_wof = False
        self.is_dir = False
        self.seen_base = False
        self.name = ""
        self.attrs = 0
        self.links = 1
        self.sparse = False

    @property
    def wof(self) -> bool:
        return self.base_wof or self.ext_wof

    @property
    def summed(self) -> int:
        """老规则:未命名 + 备用流,全都加起来。"""
        return self.base_unnamed + self.base_named + self.ext_unnamed + self.ext_named

    @property
    def wof_rule(self) -> int:
        """新规则:是 WOF 就只算备用流。"""
        if self.wof:
            return self.base_named + self.ext_named
        return self.summed

    def shape(self) -> str:
        if not self.wof:
            return "E 普通文件(无 WofCompressedData)"
        if self.base_wof and self.base_unnamed:
            return "A 幻影与真实流同在基记录"
        if self.ext_wof and self.ext_unnamed and not self.base_unnamed:
            return "B 两者都在扩展记录"
        if self.base_wof and self.ext_unnamed:
            return "C 真实流在基记录,幻影在扩展记录"
        if self.ext_wof and self.base_unnamed:
            return "D 幻影在基记录,真实流在扩展记录"
        return "F 其它(只有备用流,没有未命名流)"


def main() -> int:
    drive = (sys.argv[1] if len(sys.argv) > 1 else "C:").rstrip("\\")
    try:
        vol = Volume(drive)
    except AccessDenied as exc:
        print(f"打不开裸卷:{exc}\n必须用管理员权限跑。")
        return 2

    files: dict[int, Parts] = defaultdict(Parts)
    rec_size = 0
    with vol:
        reader = MftReader(vol)
        rec_size = reader.record_size
        bpc = vol.boot.bytes_per_cluster
        scratch = bytearray(8192 * rec_size)
        index = 0

        for run in reader.mft_runs():
            if run.lcn is None:
                index += (run.length * bpc) // rec_size
                continue
            run_bytes = run.length * bpc
            base_off = run.lcn * bpc
            done = 0
            while done < run_bytes:
                want = min(len(scratch), run_bytes - done)
                want -= want % rec_size
                if want <= 0:
                    break
                got = vol.read_into(base_off + done, want, scratch)
                if not got:
                    break
                for i in range(got // rec_size):
                    off = i * rec_size
                    if bytes(scratch[off : off + 4]) != A.MAGIC_FILE:
                        continue
                    try:
                        A.apply_fixups(scratch, off, rec_size,
                                       vol.boot.bytes_per_sector)
                        header = A.parse_record_header(scratch, off)
                    except Exception:                        # noqa: BLE001
                        continue
                    if not header.in_use:
                        continue
                    number = index + i
                    if number < FIRST_USER:
                        continue

                    unnamed = named = 0
                    wof = sparse = False
                    best = None
                    std = None
                    for attr, ao in A.iter_attributes(scratch, header, off,
                                                      rec_size):
                        code = attr.type_code
                        if code == A.ATTR_STANDARD_INFORMATION:
                            std = A.parse_standard_information(scratch, attr, ao)
                            continue
                        if code == A.ATTR_FILE_NAME:
                            info = A.parse_file_name(scratch, attr, ao)
                            if info is not None and (best is None
                                                     or info.rank > best.rank):
                                best = info
                            continue
                        if code != A.ATTR_DATA:
                            continue
                        size = A.parse_data_size(scratch, attr, ao)
                        if size is None:
                            continue
                        if size.named:
                            named += size.allocated
                            if attr.name_length == WOF_STREAM_CHARS:
                                s = ao + attr.name_offset
                                nm = bytes(scratch[s : s + WOF_STREAM_CHARS * 2])
                                if nm.decode("utf-16-le", "replace").lower() == WOF_STREAM:
                                    wof = True
                        else:
                            unnamed += size.allocated
                            if attr.sparse:
                                sparse = True

                    if header.is_extension:
                        p = files[header.base_record_number]
                        p.ext_unnamed += unnamed
                        p.ext_named += named
                        p.ext_wof = p.ext_wof or wof
                        p.sparse = p.sparse or sparse
                    else:
                        p = files[number]
                        p.seen_base = True
                        p.is_dir = header.is_directory
                        p.base_unnamed += unnamed
                        p.base_named += named
                        p.base_wof = p.base_wof or wof
                        p.sparse = p.sparse or sparse
                        p.links = header.hard_link_count
                        if best is not None:
                            p.name = best.name
                        if std is not None:
                            p.attrs = std.attributes

                index += got // rec_size
                done += got

    total, free = volume_space(drive)
    used = total - free

    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])  # 个数,老,新
    for p in files.values():
        if p.is_dir:
            continue
        b = buckets[p.shape()]
        b[0] += 1
        b[1] += p.summed
        b[2] += p.wof_rule

    print(f"盘 {drive}   系统报已用 {used / GIB:,.2f} GiB")
    print(f"记录大小 {rec_size},归并出 {len(files):,} 个文件\n")
    print(f"{'形状':<34}{'个数':>10}{'老规则':>12}{'新规则':>12}{'差':>11}")
    old_all = new_all = 0
    for shape in sorted(buckets):
        n, old, new = buckets[shape]
        old_all += old
        new_all += new
        print(f"{shape:<34}{n:>10,}{old / GIB:>11,.2f}G{new / GIB:>11,.2f}G"
              f"{(old - new) / GIB:>10,.2f}G")
    print(f"\n{'合计':<34}{'':>10}{old_all / GIB:>11,.2f}G{new_all / GIB:>11,.2f}G"
          f"{(old_all - new_all) / GIB:>10,.2f}G")
    for label, value in (("老规则", old_all), ("新规则", new_all)):
        print(f"  {label}对系统已用量:{(value - used) / GIB:>+8,.2f}G  "
              f"({(value - used) / used * 100:+.1f}%)")

    # 只见到扩展记录、没见到基记录的:这些字节归属不明。可能是基记录是目录、
    # 是元文件(记录号 < 16,上面跳过了),或者引用本身是脏数据。
    orphan = [p for p in files.values() if not p.seen_base]
    orphan_bytes = sum(p.wof_rule for p in orphan)
    print(f"\n只见到扩展记录的:{len(orphan):,} 个,{orphan_bytes / GIB:,.2f}G")

    # 剩下的超算在谁身上 —— 按新规则排前 25 名,连属性一起看
    ranked = sorted(
        (p for p in files.values() if not p.is_dir and p.seen_base),
        key=lambda p: p.wof_rule, reverse=True,
    )[:25]
    print(f"\n新规则下最大的 25 个文件:")
    print(f"{'新规则':>10}{'老规则':>10}{'链接':>5}  {'标记':<16}名字")
    for p in ranked:
        marks = []
        if p.sparse:
            marks.append("稀疏")
        if p.attrs & A.FILE_ATTR_COMPRESSED:
            marks.append("压缩位")
        if p.attrs & A.FILE_ATTR_REPARSE_POINT:
            marks.append("重解析")
        if p.wof:
            marks.append("WOF")
        print(f"{p.wof_rule / GIB:>9,.2f}G{p.summed / GIB:>9,.2f}G"
              f"{p.links:>5}  {','.join(marks):<16}{p.name[:44]}")

    # 稀疏文件单独结账:它们的 allocated 本该已经是真实占用,如果这一类
    # 贡献很大,说明这个假设在这台机器上不成立。
    sparse_files = [p for p in files.values()
                    if p.sparse and not p.is_dir and not p.wof]
    print(f"\n非 WOF 的稀疏文件:{len(sparse_files):,} 个,"
          f"{sum(p.wof_rule for p in sparse_files) / GIB:,.2f}G")

    # 硬链接:MFT 按记录去重,所以这里每个文件只算一次。报一下有多少多链接
    # 文件,好跟 scandir 的重复计数对照。
    multi = [p for p in files.values()
             if p.links > 1 and not p.is_dir and p.seen_base]
    print(f"多硬链接文件:{len(multi):,} 个,"
          f"{sum(p.wof_rule for p in multi) / GIB:,.2f}G "
          f"(scandir 会把它们按链接数重复算)")

    print("\n注:这里不含目录,也不含记录号 < 16 的元文件,所以和扫描总量")
    print("差一点是正常的(扫描还会算联接点为 0、跳过无父链的孤儿)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
