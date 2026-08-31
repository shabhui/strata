"""命令行入口。

    python -m strata serve      启动本地界面(默认)
    python -m strata scan       扫描一次,不开界面(计划任务用这个)
    python -m strata schedule   注册/删除每日计划任务
    python -m strata doctor     体检:权限、盘、库、日志
"""

from __future__ import annotations

import argparse
import sys
import time

from . import config, privileges


def _fmt(n: float | int | None) -> str:
    """字节数转人话。"""
    if n is None:
        return "—"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:,.1f} TB"


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import app

    if args.admin and not privileges.is_admin():
        try:
            if not privileges.ensure_admin():
                return 0        # 提权进程已接手
        except PermissionError as exc:
            print(f"{exc}", file=sys.stderr)
            print("继续以普通权限运行。", file=sys.stderr)

    app.serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from .scan import changes as changes_mod
    from .scan import snapshot as snapshot_mod
    from .store import db

    drives = args.drives or list(config.DEFAULT_DRIVES)
    if args.admin and not privileges.is_admin():
        try:
            if not privileges.ensure_admin():
                return 0
        except PermissionError as exc:
            print(f"{exc}", file=sys.stderr)

    conn = db.connect()
    failed = 0
    try:
        for raw in drives:
            drive = raw.rstrip("\\").rstrip(":").upper() + ":"
            try:
                started = time.perf_counter()
                result = snapshot_mod.scan_drive(conn, drive)
                elapsed = time.perf_counter() - started

                if not args.quiet:
                    print(
                        f"{drive} 快照 #{result.snapshot_id}:"
                        f"{_fmt(result.scanned_bytes)},"
                        f"{result.file_count:,} 个文件,"
                        f"方式 {result.method},耗时 {elapsed:.1f}s"
                    )
                    reason = getattr(result, "fallback_reason", None)
                    if reason:
                        print(f"  注意:{reason}")

                if privileges.is_admin():
                    # 同 server/app.py:不用 getattr 兜底,见那边的说明。
                    stats = changes_mod.collect_usn(
                        conn, drive, dir_paths=result.dir_paths
                    )
                    filled = changes_mod.enrich_deleted_sizes(conn, drive)
                    if not args.quiet:
                        if stats.available:
                            print(
                                f"  变更日志:读到 {stats.events_read:,} 条,"
                                f"入库 {stats.events_stored:,} 条,补齐大小 {filled:,} 条"
                            )
                            if stats.journal_reset:
                                print("  注意:USN 游标已失效,中间一段变更历史拿不回来了。")
                        else:
                            print(f"  变更日志不可用:{stats.reason}")
            except Exception as exc:                # noqa: BLE001
                failed += 1
                print(f"{drive} 扫描失败:{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        conn.close()

    return 1 if failed else 0


def cmd_schedule(args: argparse.Namespace) -> int:
    from . import schedule

    try:
        if args.action == "on":
            state = schedule.register(at=args.at)
            print(f"已注册:{schedule.TASK_NAME},每天 {args.at}")
        elif args.action == "off":
            state = schedule.unregister()
            print("已删除计划任务。")
        elif args.action == "run":
            state = schedule.run_now()
            print("已触发一次运行。")
        else:
            state = schedule.task_state()
    except (RuntimeError, PermissionError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    print(f"  存在:{'是' if state.exists else '否'}")
    if state.exists:
        print(f"  已启用:{'是' if state.enabled else '否'}")
        print(f"  下次运行:{state.next_run or '—'}")
        print(f"  上次运行:{state.last_run or '—'}(结果 {state.last_result or '—'})")
    elif state.detail:
        print(f"  {state.detail}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .ntfs.volume import volume_space
    from .store import db

    print("Strata 体检")
    print("-" * 52)

    state = privileges.privilege_state()
    print(f"管理员权限:{'是' if state.is_admin else '否'}")
    print(f"  {state.detail}")

    print(f"\n数据目录:{config.data_dir()}")
    print(f"数据库:{config.db_path()}")
    print(f"  大小:{_fmt(db.db_size_bytes())}")
    print(f"日志:{config.log_path()}")
    print(f"界面文件:{config.web_dir()}")
    if not config.web_dir().is_dir():
        print("  警告:界面目录不存在,serve 会返回 404。")

    print("\n盘:")
    for raw in (args.drives or list(config.DEFAULT_DRIVES)):
        drive = raw.rstrip("\\").rstrip(":").upper() + ":"
        try:
            total, free = volume_space(drive)
            print(f"  {drive} 共 {_fmt(total)},可用 {_fmt(free)},已用 {_fmt(total - free)}")
        except OSError as exc:
            print(f"  {drive} 读不到:{exc}")

    conn = db.connect()
    try:
        drives = db.known_drives(conn)
        if not drives:
            print("\n还没有任何快照。跑一次 `python -m strata scan` 就有了。")
        for drive in drives:
            snaps = db.list_snapshots(conn, drive, limit=10_000)
            latest = snaps[0] if snaps else None
            print(f"\n{drive} 快照 {len(snaps)} 个")
            if latest:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest["taken_at"]))
                print(
                    f"  最近一次:{when},{_fmt(latest['scanned_bytes'])},"
                    f"{latest['file_count']:,} 个文件,方式 {latest['method']}"
                )
            from .scan.changes import usn_coverage

            cov = usn_coverage(conn, drive)
            if cov["events"]:
                print(
                    f"  变更日志:{cov['events']:,} 条,"
                    f"覆盖 {cov['first_day']} 到 {cov['last_day']}({cov['days']} 天)"
                )
            else:
                print("  变更日志:暂无")
    finally:
        conn.close()

    from . import schedule

    task = schedule.task_state()
    print(f"\n计划任务:{'已注册' if task.exists else '未注册'}")
    if task.exists:
        print(f"  下次运行:{task.next_run or '—'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strata",
        description="看清 C 盘和 D 盘的空间是什么时候、被什么东西吃掉的。",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动本地界面")
    p_serve.add_argument("--host", default=config.HOST)
    p_serve.add_argument("--port", type=int, default=config.PORT)
    p_serve.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_serve.add_argument("--admin", action="store_true", help="需要时自动提权")
    p_serve.set_defaults(func=cmd_serve)

    p_scan = sub.add_parser("scan", help="扫描一次并写入快照")
    p_scan.add_argument("--drives", nargs="*", help="要扫的盘,默认 C: D:")
    p_scan.add_argument("--quiet", action="store_true", help="只报错误,计划任务用")
    p_scan.add_argument("--admin", action="store_true", help="需要时自动提权")
    p_scan.set_defaults(func=cmd_scan)

    p_sched = sub.add_parser("schedule", help="每日计划任务")
    p_sched.add_argument(
        "action", nargs="?", default="status", choices=["status", "on", "off", "run"]
    )
    p_sched.add_argument("--at", default="12:30", help="每天几点跑,格式 HH:MM")
    p_sched.set_defaults(func=cmd_schedule)

    p_doc = sub.add_parser("doctor", help="体检:权限、盘、库、计划任务")
    p_doc.add_argument("--drives", nargs="*")
    p_doc.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        # 不带参数就当作 serve,双击 bat 的人不需要背命令
        args = parser.parse_args(["serve", "--admin", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
