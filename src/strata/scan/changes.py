"""把 USN 事件收进库里,并按天汇总。

USN 记录里没有文件大小 —— 只知道「这个文件被删了」,不知道它多大。
所以这一层能给出的是「哪些东西消失了、什么时候」,字节数要靠历史快照里的
同名文件反查,拿不到就留空。宁可空着,也不要编一个数字出来。

路径同理:USN 只给父目录的 MFT 记录号。有上一次扫描的目录表就能还原完整
路径,没有就只剩文件名。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import config
from ..ntfs import fileid
from ..ntfs import usn as usn_mod
from ..ntfs.volume import AccessDenied, NtfsError
from ..store import db


@dataclass(slots=True)
class ChangeStats:
    drive: str
    events_read: int = 0
    events_stored: int = 0
    resolved_paths: int = 0
    first_run: bool = False       # 第一次跑,没有连续性可谈
    journal_reset: bool = False   # 本来有游标,但断了 —— 中间那段历史丢了
    available: bool = True
    reason: str | None = None
    first_usn: int | None = None
    last_usn: int | None = None
    duration_ms: int = 0
    pruned: int = 0               # 这次清掉的过期事件数
    # 反查父目录的账。resolved_paths 是「拼出路径的事件数」,这两个是
    # 「问过多少个不同的目录」—— 分开记是因为一个目录管着几十条事件,
    # 两个数量级不同,混在一起看不出反查到底起了多少作用。
    lookups_ok: int = 0
    lookups_failed: int = 0
    resolver_reason: str | None = None   # 反查没开起来的原因,开起来了是 None

    def as_dict(self) -> dict:
        return {
            "drive": self.drive,
            "events_read": self.events_read,
            "events_stored": self.events_stored,
            "resolved_paths": self.resolved_paths,
            "first_run": self.first_run,
            "journal_reset": self.journal_reset,
            "available": self.available,
            "reason": self.reason,
            "first_usn": self.first_usn,
            "last_usn": self.last_usn,
            "duration_ms": self.duration_ms,
            "pruned": self.pruned,
            "lookups_ok": self.lookups_ok,
            "lookups_failed": self.lookups_failed,
            "resolver_reason": self.resolver_reason,
        }


@dataclass(slots=True)
class DaySummary:
    day: str
    created: int = 0
    deleted: int = 0
    renamed: int = 0
    written: int = 0
    deleted_bytes_known: int = 0
    deleted_bytes_unknown_files: int = 0
    top_deleted: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "day": self.day,
            "created": self.created,
            "deleted": self.deleted,
            "renamed": self.renamed,
            "written": self.written,
            "deleted_bytes_known": self.deleted_bytes_known,
            "deleted_bytes_unknown_files": self.deleted_bytes_unknown_files,
            "top_deleted": self.top_deleted,
        }


# 只把这几类存库。write 事件量极大(一次编译几万条)而信息量低,
# 真正想知道「什么在涨」看快照差值更准。
STORED_KINDS = (usn_mod.KIND_CREATE, usn_mod.KIND_DELETE, usn_mod.KIND_RENAME_OLD)


def _clean_final_path(raw: str, drive: str) -> str | None:
    r"""把 GetFinalPathNameByHandleW 给的 \\?\C:\Windows 削成 Windows。

    对不上就返回 None。宁可留空 —— 面板上「不知道路径」显示成裸文件名,
    而「说错路径」会让人去错的地方找,后者更糟。

    对不上的三种情况:
    - 别的盘。日志是按卷读的,理论上不该出现;真出现了说明前提错了,不猜。
    - UNC(\\?\UNC\srv\share\...)。网络路径没有本地盘符,拼不出相对路径。
    - 不带 \\?\ 前缀。API 只会给这个前缀,没有就是调用方传错了东西。

    根目录削成空串,和 mft.resolve_paths 的约定一致(那边 cache 的初值就是
    {ROOT_RECORD: ""})。这条必须一致:根下的文件如果拼成 "\\x.iso",
    enrich_deleted_sizes 拿 path 去 files 表里对就永远对不上 ——
    那边存的是不带前导反斜杠的相对路径。
    """
    if not raw.startswith("\\\\?\\"):
        return None
    body = raw[4:]
    prefix = drive.rstrip("\\").rstrip(":").upper() + ":"
    if not body.upper().startswith(prefix):
        return None
    rest = body[len(prefix) :]
    if rest in ("", "\\"):
        return ""
    if not rest.startswith("\\"):
        # C:Foo 这种「盘上的当前目录」相对形式。API 不会给,给了也解释不了。
        return None
    return rest.lstrip("\\")


# 出现这些路径段就说明是穿联接点回来的。只认这两个:实测 116 个偏差全是
# AppData\Local 被解析到 Packages\<pkg>\LocalCache\Local 造成的。
# 不做通用的联接点检测 —— 那得对每一段都问一次系统,而这里只是想记个数。
_REPARSE_HINTS = frozenset({"LOCALCACHE", "PACKAGES"})


class DirPathResolver:
    """按父目录引用反查路径,一个引用只问一次。

    为什么要缓存:一次日志读 20 万条事件,里面只有 2,572 个不同的父目录 ——
    差 78 倍。不缓存就是把同一个系统调用重复 78 遍。

    **失败也缓存。** 这条容易漏,而且漏了代价最大:实测 2,572 个引用里
    1,266 个是开不出来的(目录已删、槽位回收后序列号变了、脏数据 ——
    都返回错误 87,分不出是哪种)。这些正是最热的一批(删除事件多),
    不缓存失败的话,一半的引用会被反复重试到底。所以字典里 None 也是答案。

    opener 是个 `(完整64位引用) -> 原始路径 或 None` 的可调用对象,生产上传
    `FileIdOpener(drive).path_of`。故意收一个函数而不是收那个类的实例:
    这里唯一需要的能力就是「给引用换路径」,收窄到函数之后,测试用一个
    普通闭包就能替代,不必假造卷句柄。

    opener 传 None 就整体降级成「谁也还不回来」。这么设计是为了让调用方
    不用分两条代码路径:没提权、盘开不了、平台不对,行为都一样 ——
    路径留空,事件照存。少了路径面板差一点,少了事件面板就空了。
    """

    def __init__(
        self, drive: str, opener: Callable[[int], str | None] | None = None
    ) -> None:
        self.drive = drive
        self.opener = opener
        self.hits = 0
        self.misses = 0
        # 拿回来的路径里带 Packages\...\LocalCache\ 之类联接点段的个数。
        # GetFinalPathNameByHandleW 返回重解析之后的规范路径,可能给出
        # 联接点那一侧 —— 实测 1,296 个成功里有 116 个(9%)是这样。
        # 路径本身有效、指向同一个目录,但显示出来会让人以为文件在别处。
        # 不改写它(改写就得猜哪一侧是「真的」),只记个数,方便日后判断
        # 要不要在界面上加提示。
        self.via_reparse = 0
        self._cache: dict[int, str | None] = {}
        # 自己开的 FileIdOpener,close() 时要放掉。别人传进来的不放这儿。
        self._owned: object | None = None

    def resolve(self, reference: int) -> str | None:
        """引用 → 相对路径。还不回来返回 None。"""
        if not reference:
            # 0 不是有效引用,别浪费一次系统调用去证明这件事。
            return None
        if reference in self._cache:
            return self._cache[reference]

        path = self._lookup(reference)
        self._cache[reference] = path
        if path is None:
            self.misses += 1
        else:
            self.hits += 1
            if _REPARSE_HINTS.intersection(seg.upper() for seg in path.split("\\")):
                self.via_reparse += 1
        return path

    def _lookup(self, reference: int) -> str | None:
        if self.opener is None:
            return None
        try:
            raw = self.opener(reference)
        except OSError:
            # 单个目录开不出来不该让整次采集报废。句柄失效之类的问题
            # 后面每个引用都会同样失败,统计里看得出来。
            return None
        if not raw:
            return None
        return _clean_final_path(raw, self.drive)

    def as_dict(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "via_reparse": self.via_reparse}

    def close(self) -> None:
        """放掉自己开的句柄。别人传进来的 opener 不碰 —— 谁开的谁关。"""
        if self._owned is not None:
            self._owned.close()
            self._owned = None


class _Auto:
    """「按默认来」的哨兵。见 collect_usn 的 opener 参数为什么不能默认成 None。"""

    def __repr__(self) -> str:            # pragma: no cover - 只为报错好看
        return "<自动>"


_AUTO = _Auto()


def _make_resolver(
    drive: str,
    opener: Callable[[int], str | None] | None | _Auto,
    stats: ChangeStats,
) -> DirPathResolver:
    """按 opener 的三种取值造解析器,顺手把「为什么没开起来」记进 stats。

    _AUTO(默认)= 真去开一个;None = 明确要求关掉;可调用对象 = 用你给的。

    默认值是 _AUTO 而不是 None,这一条是有意的:None 默认的话,只要有一个
    调用方忘了传,反查就静默地不工作 —— 而它恰好还能跑、还能通过测试、
    表里只是路径全空。这个项目里已经栽过两次(dir_paths 从来没接上、
    prune_usn_events 从来没被调用),两次都是「默认值让它看起来在工作」。
    所以默认必须是「主动去做」,关掉得显式说。
    """
    if isinstance(opener, _Auto):
        try:
            owned = fileid.FileIdOpener(drive)
        except OSError as exc:
            # 开不了句柄不该让整次采集失败:路径还不回来而已,事件照存。
            stats.resolver_reason = f"开不了 {drive} 的句柄,路径只能留空:{exc}"
            return DirPathResolver(drive, opener=None)
        resolver = DirPathResolver(drive, opener=owned.path_of)
        resolver._owned = owned
        return resolver
    if opener is None:
        stats.resolver_reason = "调用方要求不做反查"
        return DirPathResolver(drive, opener=None)
    return DirPathResolver(drive, opener=opener)


def _compose_path(
    event: usn_mod.UsnEvent,
    dir_paths: dict[int, str] | None,
    resolver: DirPathResolver | None = None,
) -> str | None:
    """用父目录拼出相对路径。两条路都还不回来就返回 None。

    先查 dir_paths:MFT 扫描时 resolve_paths() 顺手算出来的,查字典不花钱。
    查不到再问 resolver —— 那是一次系统调用,只对没缓存过的引用发生。
    顺序不能反,反了就是拿系统调用去换本来免费的答案。
    """
    parent: str | None = None
    if dir_paths:
        parent = dir_paths.get(event.parent_reference)
    if parent is None and resolver is not None:
        # 这里要**完整**的 64 位引用:OpenFileById 认序列号,掩过的
        # 一个都开不了(实测 4/4 失败,错误 87)。上面查 dir_paths 用的
        # 是掩过的那份,因为 MFT 记录号里不含序列号。两份都在 UsnEvent 上。
        parent = resolver.resolve(event.parent_reference_full)
    if parent is None:
        return None
    return f"{parent}\\{event.name}" if parent else event.name


def collect_usn(
    conn: sqlite3.Connection,
    drive: str,
    *,
    dir_paths: dict[int, str] | None = None,
    opener: Callable[[int], str | None] | None | _Auto = _AUTO,
    max_events: int = 200_000,
    kinds: tuple[str, ...] = STORED_KINDS,
) -> ChangeStats:
    """增量拉取 USN 日志。

    路径靠两条路还原,先便宜的:

    1. dir_paths —— 上次 MFT 扫描时 resolve_paths() 顺手算出的
       「记录号 → 目录路径」。查字典,不花钱。scandir 那条路给不了(取编号
       太贵,见 walker._scan_one_dir 的注释),所以经常是空的。
    2. opener 反查 —— 拿事件里的父目录引用直接问操作系统。一次系统调用
       91 微秒,同一个目录只问一次;20 万条事件里只有 2,572 个不同的父目录,
       全问下来 0.23 秒。

    opener 默认是 _AUTO(自己开一个),不是 None。这个默认值的选择见
    _make_resolver 的说明 —— 短版本:默认关掉的功能没人会发现它没在跑。

    日志被重建过(journal_id 变了)就从头再读一遍:日志窗口里的记录还在,
    只是编号体系换了,继续用旧游标会读到错的位置。
    """
    started = time.perf_counter()
    stats = ChangeStats(drive=drive)
    resolver = _make_resolver(drive, opener, stats)

    cursor = db.get_usn_cursor(conn, drive)
    try:
        with usn_mod.UsnJournal(drive) as journal:
            info = journal.query()
            start_usn = info.lowest_valid_usn
            if cursor is None:
                # 第一次跑,从日志里现存的最早位置开始。这不是「历史丢了」,
                # 是「历史从这里开始」,界面上的说法不一样
                stats.first_run = True
            else:
                known_journal_id, next_usn = cursor
                if known_journal_id != info.journal_id:
                    # 日志被重建过,旧编号已经没有意义
                    stats.journal_reset = True
                elif next_usn < info.lowest_valid_usn:
                    # 游标滚出窗口了,中间这段历史找不回来 —— 必须让界面知道,
                    # 不能默默跳到窗口起点当作没事发生
                    stats.journal_reset = True
                else:
                    start_usn = next_usn

            stats.first_usn = start_usn
            rows: list[db.UsnRow] = []
            for event in journal.read_all(
                start_usn, journal_id=info.journal_id, max_events=max_events
            ):
                stats.events_read += 1
                kind = event.kind
                if kind not in kinds:
                    continue
                path = _compose_path(event, dir_paths, resolver)
                if path:
                    stats.resolved_paths += 1
                rows.append(
                    db.UsnRow(
                        usn=event.usn,
                        timestamp=event.timestamp or time.time(),
                        reason=event.reason,
                        kind=kind,
                        is_dir=event.is_dir,
                        name=event.name,
                        path=path,
                        bytes=None,
                    )
                )

            stats.last_usn = getattr(journal, "last_usn", start_usn)
            if rows:
                stats.events_stored = db.insert_usn_events(conn, drive, rows)
            db.set_usn_cursor(conn, drive, info.journal_id, stats.last_usn or 0)
            # 读成了。显式写一条,把上次可能挂着的失败原因清掉 —— 不清的话,
            # 界面会对着一栏有数据的表说「读不了,因为没权限」。
            db.set_usn_status(conn, drive, available=True, reason=None)
            conn.commit()

    # 下面这三条路以前只把原因写在 stats 上,打一行日志就扔了。库里什么都不留,
    # 于是「这个盘没删过东西」和「这个盘我没看成」在界面上长得一模一样 —— 都是
    # 一张空表。提了权但日志没开的机器上(NTFS 上 USN 日志可以关,不少机器默认
    # 就是关的),面板整段藏掉,用户看到的跟「什么都没删过」一个样。
    #
    # _remember_failure 里单独 commit:这几条路上外面那个 commit 没跑到。
    except usn_mod.JournalUnavailable as exc:
        stats.available = False
        stats.reason = str(exc)
        _remember_failure(conn, drive, stats.reason)
    except AccessDenied as exc:
        stats.available = False
        stats.reason = str(exc)
        _remember_failure(conn, drive, stats.reason)
    except (NtfsError, OSError) as exc:
        stats.available = False
        stats.reason = f"读取 USN 日志失败:{exc}"
        _remember_failure(conn, drive, stats.reason)
    finally:
        # 上面哪条路走完都得放句柄,包括抛异常那几条。
        stats.lookups_ok = resolver.hits
        stats.lookups_failed = resolver.misses
        resolver.close()

    # 清理过期事件。放在 except 之外、无条件执行:清不清跟这次能不能读日志
    # 没关系,读失败了旧数据也一样该过期。
    #
    # 这个调用以前不存在。prune_usn_events 写好了、默认 keep_days=180 写好了、
    # test_changes.py 里两条测试直接调它也一直是绿的 —— 但 src 里 grep 不到
    # 任何调用点,所以那 180 天从来没生效,表只增不减。
    # 测试证明的是「函数能用」,不是「功能在跑」,这两件事差得远。
    try:
        stats.pruned = prune_usn_events(conn, drive)
    except sqlite3.Error:
        # 清理是维护动作,失败不该让整次采集报废 —— 这次少清一批,下次再清。
        pass

    stats.duration_ms = int((time.perf_counter() - started) * 1000)
    return stats


def enrich_deleted_sizes(
    conn: sqlite3.Connection, drive: str, *, limit: int = 5000
) -> int:
    """给删除事件补上大小 —— 从历史快照里按路径反查。

    只在路径唯一命中时才填,命中不到就保持 NULL。这是估算:快照是某个时刻的
    切片,文件在被删之前可能还长大过,那部分看不到。
    """
    rows = list(
        conn.execute(
            """
            SELECT id, path FROM usn_events
             WHERE drive = ? AND kind = 'delete'
               AND bytes IS NULL AND path IS NOT NULL AND is_dir = 0
             LIMIT ?
            """,
            (drive, limit),
        )
    )
    if not rows:
        return 0

    updates: list[tuple[int, int]] = []
    for row in rows:
        hit = conn.execute(
            """
            SELECT f.bytes FROM files f
              JOIN snapshots s ON s.id = f.snapshot_id
             WHERE s.drive = ? AND f.path = ?
             ORDER BY s.taken_at DESC LIMIT 1
            """,
            (drive, row["path"]),
        ).fetchone()
        if hit is not None:
            updates.append((int(hit["bytes"]), int(row["id"])))

    if updates:
        conn.executemany("UPDATE usn_events SET bytes = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def usn_daily_summary(
    conn: sqlite3.Connection, drive: str, *, days: int = 30, top_n: int = 5
) -> list[DaySummary]:
    """按天汇总变更事件,给时间轴的「消失了什么」那一栏用。"""
    cutoff = time.time() - days * 86400
    out: dict[str, DaySummary] = {}

    for row in conn.execute(
        """
        SELECT kind, timestamp, bytes, path, name, is_dir
          FROM usn_events
         WHERE drive = ? AND timestamp >= ?
         ORDER BY timestamp
        """,
        (drive, cutoff),
    ):
        # USN 的时间戳也是从 FILETIME 换算的,同样可能是坏值
        day = config.safe_day(row["timestamp"])
        if day is None:
            continue
        summary = out.get(day)
        if summary is None:
            summary = DaySummary(day=day)
            out[day] = summary

        kind = row["kind"]
        if kind == usn_mod.KIND_CREATE:
            summary.created += 1
        elif kind == usn_mod.KIND_DELETE:
            summary.deleted += 1
            size = row["bytes"]
            if size:
                summary.deleted_bytes_known += int(size)
            else:
                summary.deleted_bytes_unknown_files += 1
        elif kind in (usn_mod.KIND_RENAME_OLD, usn_mod.KIND_RENAME_NEW):
            summary.renamed += 1
        elif kind == usn_mod.KIND_WRITE:
            summary.written += 1

    # 每天挑几个最大的删除给界面展示
    for day, summary in out.items():
        if not summary.deleted:
            continue
        start = config.day_timestamp(day)
        if start is None:
            # 这一天算不出当地午夜就不查明细,而不是让它把整个结果带崩。
            # 走 HTTP 打不到这里:上面的 cutoff 只放过 days 天内的事件,而
            # days 被接口层夹在 3650。但 days 本身没有上界,函数对任何取值都
            # 该成立 —— UTC+8 上时间戳 0 的事件会被归到 '1970-01-01',那天的
            # 当地午夜在 epoch 之前,原来这行会抛 OverflowError。
            continue
        end = start + 86400 + 7200      # 多给两小时,夏令时不会漏掉边缘事件
        # 按路径合并,理由和 api.get_changes 里那段一样:USN 记的是操作不是文件,
        # 一次生命周期好几条记录。这儿只取 top_n=5 条,更容易被同一个文件占满。
        # 路径为空的按 usn 各自成组 —— 同名不等于同一个文件。
        summary.top_deleted = [
            {
                "path": r["path"] or r["name"],
                "bytes": r["bytes"],
                "is_dir": bool(r["is_dir"]),
                "at": r["timestamp"],
                "count": r["n"],
            }
            for r in conn.execute(
                """
                SELECT path, name, MAX(bytes) bytes, is_dir,
                       MAX(timestamp) timestamp, COUNT(*) n
                  FROM usn_events
                 WHERE drive = ? AND kind = 'delete'
                   AND timestamp >= ? AND timestamp < ?
                 GROUP BY path,
                          CASE WHEN path IS NULL OR path = '' THEN usn ELSE 0 END
                 ORDER BY COALESCE(MAX(bytes), 0) DESC, MAX(timestamp) DESC
                 LIMIT ?
                """,
                (drive, start, end, top_n),
            )
        ]

    return [out[day] for day in sorted(out)]


def _remember_failure(conn: sqlite3.Connection, drive: str, reason: str) -> None:
    """把失败原因落库。自己 commit —— 失败路径上外面那个 commit 没跑到。

    这里再包一层 try:记原因是为了让界面能解释情况,它自己失败了不该把整次扫描
    带崩。真崩了也只是回到「说不出为什么」,不比原来差。
    """
    try:
        db.set_usn_status(conn, drive, available=False, reason=reason)
        conn.commit()
    except Exception:                               # noqa: BLE001
        pass


def usn_coverage(conn: sqlite3.Connection, drive: str) -> dict:
    """USN 数据覆盖到哪一天。界面要靠这个说清楚「这段历史是实测的」。

    还带上「上次读日志成没成」。0 条事件有三种原因,界面得分得开:

        available=True   读成了,确实一条没有 → 那就是真的空
        available=False  读失败了,reason 说为什么 → 得显示出来,不能藏
        available=None   这个盘没扫过 → 不知道,别编成故障

    以前只有 events=0,三种情况长得一模一样。
    """
    row = conn.execute(
        """
        SELECT COUNT(*) n, MIN(timestamp) lo, MAX(timestamp) hi
          FROM usn_events WHERE drive = ?
        """,
        (drive,),
    ).fetchone()

    status = db.get_usn_status(conn, drive)
    # 三个返回口都得带这两个字段,前端才不用分情况取
    extra = {
        "available": status["available"] if status else None,
        "reason": status["reason"] if status else None,
    }

    n = int(row["n"] or 0)
    if not n:
        return {"events": 0, "first_day": None, "last_day": None, "days": 0, **extra}

    first_day = config.safe_day(row["lo"])
    last_day = config.safe_day(row["hi"])
    if first_day is None or last_day is None:
        return {"events": n, "first_day": first_day, "last_day": last_day,
                "days": 0, **extra}
    span = (date.fromisoformat(last_day) - date.fromisoformat(first_day)).days + 1
    return {
        "events": n,
        "first_day": first_day,
        "last_day": last_day,
        "days": span,
        **extra,
    }


def prune_usn_events(conn: sqlite3.Connection, drive: str, *, keep_days: int = 180) -> int:
    """删掉过期事件。日志本身会滚动,库里也没必要无限留。"""
    cutoff = (date.today() - timedelta(days=keep_days))
    cutoff_ts = time.mktime(
        (cutoff.year, cutoff.month, cutoff.day, 0, 0, 0, 0, 0, -1)
    )
    cur = conn.execute(
        "DELETE FROM usn_events WHERE drive = ? AND timestamp < ?", (drive, cutoff_ts)
    )
    conn.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
