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


def safe_day(ts: float | None) -> str | None:
    """时间戳转 YYYY-MM-DD;超出可用范围或转换失败时返回 None。

    宁可丢掉一个坏时间戳,也不能让它把整次扫描带崩 —— 这种文件在系统盘上
    很常见(FILETIME 0、未来时间、坏值)。
    """
    if ts is None:
        return None
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return None
    if value != value or value < TS_MIN or value > TS_MAX:   # NaN 也在这里挡掉
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
