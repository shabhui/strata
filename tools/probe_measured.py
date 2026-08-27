"""在真实库的副本上造一个更早的快照,验证实测层和快照对比。

只读真库:全程操作副本,原库一个字节都不动。

为什么要造:保留策略是每天只留一个快照(db.prune_snapshots),所以同一天
扫两次也凑不出两个快照,实测层在装好当天永远是空的。想在真实规模的数据
(6 万个目录)上看到实测层、分界线、增减和删除,就得有一个落在别的日子里的快照。

做法:复制库,把最新快照的目录表按比例缩小写成「3 天前」的快照,
再删掉几个目录、加一个后来消失的目录,这样四种变化(涨/缩/新增/消失)都有。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strata import config                      # noqa: E402
from strata.analysis import diff, timeline      # noqa: E402
from strata.store import db                    # noqa: E402

DAY = 86400.0
SHRINK = 0.97          # 早先的快照整体小 3%,于是「现在」看起来在涨
GONE_AT_END = "StrataProbe\\已经删掉的大目录"


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def build_earlier_snapshot(conn, latest_id: int, drive: str, taken_at: float) -> int:
    """把 latest_id 的目录表缩小,写成一个更早的快照。返回新快照 id。"""
    rows = list(
        conn.execute(
            "SELECT path, depth, bytes, own_bytes, files, dirs, newest_mtime, newest_ctime"
            "  FROM dirs WHERE snapshot_id = ?",
            (latest_id,),
        )
    )
    latest = conn.execute(
        "SELECT total_bytes, free_bytes, used_bytes FROM snapshots WHERE id = ?",
        (latest_id,),
    ).fetchone()

    dir_rows: list[db.DirRow] = []
    dropped = 0
    for i, r in enumerate(rows):
        # 每 500 个目录抽一个,让它在早先的快照里不存在 => 后来「新增」
        if i % 500 == 0 and int(r["bytes"]) > 8 * 1024**2:
            dropped += 1
            continue
        dir_rows.append(
            db.DirRow(
                path=r["path"],
                depth=int(r["depth"]),
                bytes=int(int(r["bytes"]) * SHRINK),
                own_bytes=int(int(r["own_bytes"]) * SHRINK),
                files=int(r["files"]),
                dirs=int(r["dirs"]),
                newest_mtime=r["newest_mtime"],
                newest_ctime=r["newest_ctime"],
            )
        )

    # 一个当时有、现在没了的目录 => 「消失」,也就是被删掉的那种
    dir_rows.append(
        db.DirRow(
            path=GONE_AT_END,
            depth=2,
            bytes=6 * 1024**3,
            own_bytes=6 * 1024**3,
            files=1200,
            dirs=3,
            newest_mtime=taken_at,
            newest_ctime=taken_at,
        )
    )

    scanned = sum(r.own_bytes for r in dir_rows)
    sid = db.insert_snapshot(
        conn,
        db.Snapshot(
            drive=drive,
            taken_at=taken_at,
            method="scandir",
            total_bytes=int(latest["total_bytes"]),
            free_bytes=int(latest["free_bytes"]),
            used_bytes=scanned,
            scanned_bytes=scanned,
            complete=True,
            note="probe 造的假快照",
        ),
    )
    db.insert_dirs(conn, sid, dir_rows)
    conn.commit()
    print(f"造出快照 #{sid}:{len(dir_rows)} 个目录(抽掉 {dropped} 个,加了 1 个后来消失的)")
    return sid


def main() -> int:
    src = config.db_path()
    if not src.exists():
        print("真库还不存在,先扫一次")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="tc-probe-")) / "copy.db"
    shutil.copy2(src, tmp)
    print(f"副本: {tmp}\n")

    conn = db.connect(tmp)
    latest = db.latest_snapshot(conn, "C:")
    if latest is None:
        print("副本里没有 C: 的完整快照")
        return 1
    latest_id = int(latest["id"])
    latest_ts = float(latest["taken_at"])
    print(f"真快照 #{latest_id}  {timeline._day_of(latest_ts)}  "
          f"{human(latest['scanned_bytes'])}")

    earlier = build_earlier_snapshot(conn, latest_id, "C:", latest_ts - 3 * DAY)
    print(f"   #{earlier} 记在 {timeline._day_of(latest_ts - 3 * DAY)}\n")

    print("--- 时间轴 ---")
    tl = timeline.build_timeline(conn, "C:", days=90, top_n=3)
    s = timeline.timeline_summary(tl)
    print(f"天数 {s['days']}   净 {human(s['net'])}   "
          f"实测 {s['measured_days']} 天 / 回溯 {s['retro_days']} 天")
    print(f"分界线 first_measured_day = {s['first_measured_day']}")
    print(f"轴范围 {tl[0].day} -> {tl[-1].day}")

    print("\n最后 6 天:")
    for c in tl[-6:]:
        mark = "实测" if c.basis == "measured" else "回溯"
        print(f"  {c.day} [{mark}] 增 {human(c.added):>9}  "
              f"减 {human(c.removed):>9}  净 {human(c.net):>9}")
        for k in c.contributors[:2]:
            print(f"        + {human(k.bytes):>9}  {k.path[:54]}")
        for k in c.shrinkers[:2]:
            print(f"        - {human(k.bytes):>9}  {k.path[:54]}")

    print("\n--- 快照对比 ---")
    d = diff.diff_snapshots(conn, earlier, latest_id)
    payload = d.as_dict()
    print(f"before {timeline._day_of(payload['before_at'])}  "
          f"after {timeline._day_of(payload['after_at'])}")
    print(f"净变化 {human(payload['net'])}   "
          f"({human(payload['before_bytes'])} -> {human(payload['after_bytes'])})")

    kinds: dict[str, int] = {}
    for row in d.dir_deltas:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
    print(f"目录变化 {len(d.dir_deltas)} 条,按类型 {kinds}")

    print(f"涨得最多({len(payload['grew'])} 条,展示 3):")
    for r in payload["grew"][:3]:
        print(f"   +{human(r['delta']):>9}  [{r['kind']}]  {r['path'][:50]}")
    print(f"缩得最多({len(payload['shrank'])} 条,展示 3):")
    for r in payload["shrank"][:3]:
        print(f"   {human(r['delta']):>10}  [{r['kind']}]  {r['path'][:50]}")

    for want in ("appeared", "vanished"):
        hit = [r for r in d.dir_deltas if r.kind == want][:2]
        for r in hit:
            print(f"   {want:9} {human(r.delta):>10}  {r.path[:50]}")

    print(f"文件级 {len(payload['files'])} 条")
    if payload.get("caveats"):
        for c in payload["caveats"]:
            print(f"caveat: {c}")

    print(
        "\n注意:上面两个数是这个脚本自己造出来的偏差,不是产品的问题。\n"
        "  1) net 偏大 —— 真快照的 scanned_bytes 由扫描器逐个文件累加,\n"
        "     假快照只是把留下来的目录行的 own_bytes 相加(小于 4 MB 或超过\n"
        "     4 层的目录本来就不入表),两个数的算法不同,差值没有意义。\n"
        "  2) removed 是 0 而下面列着 6 GB 的消失目录 —— removed 只累加顶层\n"
        "     目录以免父子重复计算,而假数据里没造顶层的那一行。真实扫描\n"
        "     每一层目录都会入表,这一项不会漏。"
    )

    conn.close()
    print(f"\n副本留在 {tmp.parent} ,可以删")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
