"""路径、端口与扫描规则的集中配置。"""

from __future__ import annotations

import os
import shutil
import sys
import time as _time
from datetime import date as _date
from functools import lru_cache as _lru_cache
from pathlib import Path

APP_NAME = "Strata"

# 服务只绑本机回环,不监听外部接口。
HOST = "127.0.0.1"
PORT = 8731

# 默认关注的盘。界面上可改,这里只是首次运行的默认值。
DEFAULT_DRIVES = ("C:", "D:")


# 改名前叫这个。只为了把老数据搬过来,见 _migrate_legacy_dir()。
_LEGACY_APP_NAME = "TimeClear"
_LEGACY_FILES = {"timeclear.db": "strata.db", "timeclear.log": "strata.log"}


def data_dir() -> Path:
    """数据库与日志的存放目录:%LOCALAPPDATA%\\Strata"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    root = Path(base)
    path = root / APP_NAME
    fresh = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if fresh:
        _migrate_legacy_dir(root / _LEGACY_APP_NAME, path)
    return path


def _migrate_legacy_dir(old: Path, new: Path) -> None:
    """把改名前的数据库搬到新目录。

    项目从 TimeClear 改名成 Strata,数据目录和文件名跟着变了。历史是这个
    工具唯一不可再生的东西 —— 快照记录的是「那一天硬盘长什么样」,删掉之后
    再扫一次也回不来(回溯层能重建,实测层不能)。所以搬,不是让用户重扫。

    搬完不删老目录,只留在原地。万一搬错了,原件还在。
    """
    if not old.is_dir() or old == new:
        return
    moved = []
    for legacy, current in _LEGACY_FILES.items():
        src, dst = old / legacy, new / current
        if not src.is_file() or dst.exists():
            continue
        try:
            # copy2 而不是 move:老文件留着当后备,反正下次启动就不看了
            shutil.copy2(src, dst)
            moved.append(current)
        except OSError:
            # 搬不动就算了(占用、权限、磁盘满)。空库照样能跑,
            # 只是历史得重扫 —— 比在这儿抛异常拦住启动要好。
            pass
    if moved:
        try:
            (new / "MIGRATED.txt").write_text(
                f"从 {old} 迁移而来:{', '.join(moved)}\n"
                f"老目录没有删,确认没问题之后可以自己删掉。\n",
                encoding="utf-8")
        except OSError:
            pass


def db_path() -> Path:
    return data_dir() / "strata.db"


def log_path() -> Path:
    return data_dir() / "strata.log"


def frozen() -> bool:
    """是不是打包成单文件 exe 在跑。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_dir() -> Path:
    """随程序一起分发的只读文件在哪。

    打包成 exe 之后,这些文件被解到临时目录 sys._MEIPASS 里,不在源码树旁边。
    显式读 _MEIPASS,而不是靠 __file__ 碰巧也能指对 —— 后者取决于
    PyInstaller 怎么处理冻结模块的 __file__,换个打包模式就可能不成立。

    只读。要写的东西一律放 data_dir():_MEIPASS 是临时目录,进程一退就没了。
    """
    if frozen():
        return Path(sys._MEIPASS)                  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def web_dir() -> Path:
    """界面文件(index.html / app.js / app.css)所在目录。"""
    return bundle_dir() / "web"


def schema_path() -> Path:
    """建库用的 schema.sql。

    源码模式在 store/ 下;打包之后被放到 _MEIPASS 根上的 store/ 里。
    """
    return bundle_dir() / "store" / "schema.sql"


# ---- 扫描规则 ----------------------------------------------------------------

# 这些目录不计入「可清理」建议,但仍然统计大小。
SYSTEM_DIRS = frozenset(
    {
        "windows",
        "$recycle.bin",
        "system volume information",
        "$windows.~ws",
        "$windows.~bt",
        "recovery",
        "perflogs",
    }
)

# 默认走不走 MFT 直读。
#
# 提权成功之后第一次真跑通 MFT,实测结果和设计意图相反,所以默认关掉:
#
#   本机 C:(1,079,118 个条目)
#     scandir 热缓存   38.1s   28,718 条/秒
#     scandir 冷缓存  119.1s    9,027 条/秒
#     mft            100.5s   10,743 条/秒   之后 USN 再 29.2s
#
#   慢的原因是 mft.read_entries() 是单线程循环,每条记录调一次纯 Python 的
#   _parse_record;而 walker.py 的 scandir 那条路有线程池。瓶颈在 CPU 不在
#   I/O,所以 MFT「顺序读元数据所以快」这个前提在这份实现上不成立。
#
#   后来把这 100.5 秒逐段量了一遍(工具都在 tools/,不写库):
#
#     解析 161 万条记录   67.4s   bench_parse_record.py(合成记录,上限)
#     收集后那五遍         8.8s   prof_pipeline.py(scandir 也要走这五遍)
#     条目 → ScanEntry    11.6s   prof_mft_convert.py
#     路径还原             1.1s   同上 —— 记忆化够好,不是瓶颈
#     读 1.5 GiB 的 MFT    1.3s   bench_nobuffering.py
#
#   读盘只占 1.3 秒,FILE_FLAG_NO_BUFFERING 无罪(两种读法都 583~1699 MB/s)。
#   解析里还藏着一个跟规模相关的退化:read_entries 每块新分配 8 MiB
#   缓冲区,同时手里攥着上百万个条目 —— 这两件事同时成立时,解析从
#   9 µs/条掉到 53 µs/条(前 5 块 74ms、后 5 块 440ms)。单独任何一个都没事。
#   排掉的其他嫌疑:硬件掉频(满载 80 秒只慢 1.04x)、内存换页(缺页数每块
#   恒定)、GC(gc.disable() 只省 5%)。改成整趟复用一块之后,合成数据上
#   161 万条从 72.6 秒降到 15.5 秒(prof_mft_buffer.py,四个变体各独立进程)。
#
#   真盘上 read_entries + 路径还原从约 80 秒降到 46~49 秒。为什么不是 4.68 倍:
#   真盘记录比合成的复杂得多(161 万条里 78,445 条扩展记录、8,312 条无名字),
#   而且退化只去掉了一半 —— 每块新分配没了,但「建 161 万个对象」还在。
#   同一个进程里连跑两次 C: 是 45.52s 然后 89.61s,第二轮堆已经被撑大。
#   生产环境每次扫描是新进程,看到的是第一轮那个数。
#
#   真盘单次测量方差很大(同样代码量到 45.52 / 47.89 / 71.37 / 89.61),
#   所以上面报的是区间。要精确对比只能同进程连跑、或者取多次最小值。
#
# 口径上 MFT 本该更准(硬链接只算一次、算占盘大小而非逻辑大小),但实测
# scanned_bytes 比系统报的已用量多出整整一卷:C: 397.1 GB vs 169.2 GB,
# D: 1389.7 GB vs 637.8 GB,两个盘的差额分别是整卷的 1.041 和 1.030 倍。
# 原因是 NTFS 元文件(记录号 < 16)全挂在盘根下,其中 $BadClus:$Bad 是一条
# 稀疏流,allocated_size 按定义就等于整卷容量。这些字节进了总数,又因为
# prune_tree() 不写根节点那一行,在树里查无此人 —— 于是总数和树永远差一卷。
# 见 scan/snapshot.py 里 _mft_to_scan_entries() 的元文件过滤。
#
# 两件事叠起来:MFT 现在既慢 2.6 倍又把总量说成两倍多。留着它当默认值,
# 等于让提权成功反过来把体验变差。想单独用 MFT 的照样可以传 prefer_mft=True。
#
# 缓冲区那个修复之后重新算:MFT 那条路约 46~49s(read_entries + 路径)
# 加 11.6s(转 ScanEntry)加 8.8s(五遍)加写库,大约 70~75s;scandir 热缓存
# 全程 38.8s(库里快照 #9)。还是 scandir 快,所以默认值不动。
#
# 总量那条已经修了(snapshot._mft_to_scan_entries 过滤文件形态的元文件),
# 实测 397.19 → 196.67 GiB,对系统已用 169.63 GiB 是 +15.94%。还是超出 3%
# 的预期,剩下那 27 GiB 是另一件事(硬链接、卷影副本或压缩/稀疏口径),
# 没查清之前不能说 MFT 的总量可信了。
PREFER_MFT = False

# 归因深度:把每个文件的增长归到路径前 N 段。
# 3 段能区分到 `Users\alice\AppData` 和 `Program Files\Steam\steamapps`,
# 比只看顶层目录有用得多。
ATTRIBUTION_DEPTH = 3

# 目录树保留规则:占用达到阈值的目录全深度保留,
# 阈值以下但深度较浅的也保留,其余合并进父目录的「已折叠」计数。
# 实测本机 C: 267k 目录 / D: 142k 目录,4 MB 阈值下每盘约 4 万行,数据库压力可控。
DIR_KEEP_MIN_BYTES = 4 * 1024 * 1024  # 4 MB
DIR_KEEP_MAX_DEPTH = 4

# 单文件明细保留规则:大于此值的文件逐个入库(用于「谁在吃空间」明细)。
FILE_KEEP_MIN_BYTES = 8 * 1024 * 1024  # 8 MB
# 近期文件即使不大也保留,用于「这几天新增了什么」。
FILE_RECENT_DAYS = 45
FILE_RECENT_MIN_BYTES = 1024 * 1024  # 1 MB

# 每个快照最多保留多少条单文件明细,防止数据库失控。
FILE_ROW_CAP = 60_000

# 每日归因桶的最小入库字节,低于此值合并成「其他」。
BUCKET_MIN_BYTES = 2 * 1024 * 1024  # 2 MB

# ---- 历史快照降级 ----
# 只有最新快照需要全量细节(树图要能钻取)。更早的快照降级成粗粒度,
# 只留够画时间轴和做对比的部分,否则 120 个快照会把数据库撑到几百 MB。
DEMOTE_DIR_MIN_BYTES = 64 * 1024 * 1024  # 64 MB
DEMOTE_DIR_MAX_DEPTH = 3


# ---- 时间戳 ----
# 真实磁盘上有的文件带着离谱的时间戳:FILETIME 为 0 会算到 1601 年,
# 有些安装包会写未来的年份,还有些干脆是坏值。Windows 上 time.localtime()
# 碰到这些会直接抛 OSError,一个坏文件就能让整次扫描失败。
TS_MIN = 0.0                      # 1970-01-01
TS_MAX = 4102444800.0             # 2100-01-01

# 「最新写入时间」允许比现在晚多少。
#
# TS_MIN/TS_MAX 管的是**能不能表示**(localtime 会不会抛),这个管的是
# **可不可信**。两件事,界不一样,混用会漏 —— 本机上就漏了:
#
#   Program Files (x86)\Internet Download Manager 里有个文件写着 2030-09-16。
#   1915769744 这个值离 TS_MAX(2100)还差得远,范围检查一路放行,
#   于是它被 MAX 聚合放大到每一级祖先,最后连**盘根**的 newest_ctime
#   都是 2030 年。界面上 6.5 GB 的 `Program Files (x86)` 被永久钉在
#   「最近写入」榜首,算出的 days_old 是 -1477.6(负数进了 API),
#   前端见 d < 1 就显示「今天」。
#
# 为什么留两天的余量而不是「一秒都不许超过现在」:
#   跨机器拷来的文件常带对面的钟(几分钟到几小时)、夏令时算错是 1 小时、
#   时区当成 UTC 是 8 小时、NAS 时区配错最多 26 小时。这些是正常产物,
#   掐死了会把真实的最新时间丢成「未知」。而那个坏值超前 1478 天 ——
#   离容差还有三个数量级,不存在挡不住又误伤的中间地带。
FUTURE_TOLERANCE = 2 * 86400


def newest_ceiling(now: float | None = None) -> float:
    """聚合「最新写入时间」时的上界:现在 + 容差,且不超过可表示范围。"""
    current = _time.time() if now is None else now
    return min(TS_MAX, current + FUTURE_TOLERANCE)


def safe_ts(ts: float | None) -> float | None:
    """越界、NaN、非数字的时间戳一律当「不知道」,原样返回可信的值。

    这是范围判断的唯一定义,safe_day 和 scan/tree.py 的聚合都走它 ——
    两处各写一遍的话,改了一处就会出现「分桶丢掉了但聚合留着」这种
    互相矛盾的状态,而那正是本机上量到的现象:

        dirs.newest_ctime  7 行在未来(2030-09-16,源头是 IDM 写的一个文件)
                           6 行是负值(算出来 1608 年,ProgramData\\Package Cache)

    聚合取最大值,于是那一个文件把它每一级祖先都染成 2030 年 —— 包括盘根。
    分桶那边早就在挡了,聚合这边没挡,同一个目录在两张表里显示两个日期。

    NaN 要单独判(`value != value`):它跟任何数比较都是 False,靠
    `< TS_MIN or > TS_MAX` 挡不住,而后面 `a > b` 那种比较会静默走错分支。
    """
    if ts is None:
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value != value or value < TS_MIN or value > TS_MAX:   # NaN 也在这里挡掉
        return None
    return value


def safe_day(ts: float | None) -> str | None:
    """时间戳转 YYYY-MM-DD;超出可用范围或转换失败时返回 None。

    宁可丢掉一个坏时间戳,也不能让它把整次扫描带崩 —— 这种文件在系统盘上
    很常见(FILETIME 0、未来时间、坏值)。
    """
    value = safe_ts(ts)
    if value is None:
        return None
    try:
        return _time.strftime("%Y-%m-%d", _time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return None


def day_timestamp(day: str, hour: int = 0) -> float | None:
    """safe_day 的逆向:'YYYY-MM-DD' 加上小时,还原成当地时间戳。

    不用 time.strptime。它是纯 Python 实现,每次调用都要查一遍 locale 再跑一次
    正则;聚合几百天的接口一次请求要调上千次,实测 336 个日期 1.43 ms,切片取
    数字是 0.19 ms。日期格式是我们自己写进库的固定格式,不需要通用解析器。

    校验交给 datetime.date —— 月末和闰年的规则不该在这里重写一遍。mktime 拿
    tm_isdst=-1,和 strptime 解析无时区格式后的行为一致,夏令时语义不变。

    返回 None 而不抛异常,和 safe_day 一样:坏日期只该丢掉自己。None 不是纯
    假想 —— UTC+8 上 safe_day(0.0) 得到 '1970-01-01',而这一天的当地午夜在
    epoch 之前,mktime 表示不了,strptime + mktime 那套会抛 OverflowError。
    """
    # 类型守卫必须在缓存外面:lru_cache 会先对参数取哈希,传进来一个 list
    # 就在函数体执行之前抛 TypeError,守不住"绝不抛异常"这条。
    if not isinstance(day, str) or not isinstance(hour, int):
        return None
    return _day_timestamp(day, hour)


@_lru_cache(maxsize=4096)
def _day_timestamp(day: str, hour: int) -> float | None:
    """day_timestamp 的缓存内核。同一天在一次请求里会被问上很多次。"""
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None
    try:
        year, month, dom = int(day[0:4]), int(day[5:7]), int(day[8:10])
    except ValueError:                       # 非数字,含 int() 容忍的空白和正负号
        return None
    if not (day[0:4].isdigit() and day[5:7].isdigit() and day[8:10].isdigit()):
        return None
    try:
        _date(year, month, dom)              # 让它挡掉 2 月 30 日这类
        return _time.mktime((year, month, dom, hour, 0, 0, 0, 1, -1))
    except (OSError, OverflowError, ValueError):
        return None
