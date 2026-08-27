"""本地 HTTP 服务 —— 只用标准库,不装任何依赖。

只绑 127.0.0.1。这个工具能看到整块盘的目录结构,那是敏感信息,
绝不监听外部接口。

扫描是耗时操作(全盘 MFT 要几十秒),放后台线程跑,前端轮询 /api/scan/state
看进度,不然浏览器会一直转圈。
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import config, privileges
from ..store import db
from . import api

# 请求体上限。这些接口收的都是盘符和路径,几百字节就够;设个上限免得
# 一个乱报 Content-Length 的请求让我们照着读几个 GB 进内存。
_MAX_BODY = 1 << 20                                  # 1 MiB

# 扫描是全局互斥的:两个线程同时读同一块盘只会互相拖慢
_scan_lock = threading.Lock()
_scan_state: dict = {
    "running": False,
    "drive": None,
    "phase": "空闲",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_state_lock = threading.Lock()


def _set_state(**kw) -> None:
    with _state_lock:
        _scan_state.update(kw)


def scan_state() -> dict:
    with _state_lock:
        return dict(_scan_state)


def _run_scan(drive: str, *, with_usn: bool = True) -> None:
    """后台扫描。异常一律吞进 state,不能让线程静默死掉。"""
    from ..scan import changes as changes_mod
    from ..scan import snapshot as snapshot_mod

    conn = None
    try:
        _set_state(running=True, drive=drive, phase="正在扫描", started_at=time.time(),
                   finished_at=None, result=None, error=None)
        # 每个线程一个连接:sqlite 的连接不能跨线程用
        conn = db.connect()
        result = snapshot_mod.scan_drive(conn, drive)

        payload = {
            "drive": drive,
            "snapshot_id": result.snapshot_id,
            "method": result.method,
            "scanned_bytes": result.scanned_bytes,
            "file_count": result.file_count,
            "dir_count": result.dir_count,
            "duration_ms": result.duration_ms,
            "fallback_reason": getattr(result, "fallback_reason", None),
        }

        if with_usn and privileges.is_admin():
            _set_state(phase="正在读取变更日志")
            stats = changes_mod.collect_usn(
                conn, drive, dir_paths=getattr(result, "dir_paths", None)
            )
            changes_mod.enrich_deleted_sizes(conn, drive)
            payload["usn"] = stats.as_dict()

        _set_state(running=False, phase="完成", finished_at=time.time(), result=payload)
    except Exception as exc:                       # noqa: BLE001
        _set_state(
            running=False,
            phase="失败",
            finished_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )
        config.log_path().parent.mkdir(parents=True, exist_ok=True)
        with open(config.log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 扫描 {drive} 失败\n")
            fh.write(traceback.format_exc())
    finally:
        if conn is not None:
            conn.close()
        _scan_lock.release()


def start_scan(drive: str) -> tuple[bool, str]:
    """启动后台扫描。已经在扫就直接拒绝,不排队。"""
    if not _scan_lock.acquire(blocking=False):
        return False, "已经有一次扫描在进行中,请等它结束。"
    thread = threading.Thread(target=_run_scan, args=(drive,), daemon=True,
                              name=f"scan-{drive}")
    thread.start()
    return True, f"已开始扫描 {drive}"


class Handler(BaseHTTPRequestHandler):
    server_version = "Strata"
    sys_version = ""

    # ---- 基础设施 ----
    def log_message(self, fmt: str, *args) -> None:
        # 默认实现会把每个请求打到 stderr,轮询状态时刷屏
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 本地工具,禁掉一切嵌入和嗅探
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        # 顺手接受带 as_dict 的 dataclass:忘了调 .as_dict() 是很容易犯的错,
        # 而代价是整个连接被 TypeError 掐断、前端连报错都收不到
        if hasattr(payload, "as_dict"):
            payload = payload.as_dict()
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": status}, status=status)

    # ---- 路由 ----
    def do_GET(self) -> None:        # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        if path.startswith("/api/"):
            self._handle_api(path, params)
        else:
            self._serve_static(path)

    def do_HEAD(self) -> None:       # noqa: N802
        self.do_GET()

    def _same_origin(self) -> bool:
        """拦跨站 POST。

        浏览器允许任何网页往 127.0.0.1 发 POST,所以别人的页面能悄悄
        触发扫描、改计划任务、弹资源管理器。同源的请求带的 Origin 是
        我们自己,跨站的不是。

        两个头都没有时放过:那不是浏览器发的(curl、脚本),而 CSRF 要
        借的就是受害者的浏览器。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            port = self.server.server_address[1]
            allowed = {
                f"http://127.0.0.1:{port}",
                f"http://localhost:{port}",
                f"http://[::1]:{port}",
            }
            return origin in allowed

        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site:
            return site in ("same-origin", "none")
        return True

    def do_POST(self) -> None:       # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if not self._same_origin():
            self._error(403, "拒绝跨站请求")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "Content-Length 不是数字")
            return
        if length < 0 or length > _MAX_BODY:
            self._error(413, "请求体过大")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except UnicodeDecodeError:
            # JSONDecodeError 抓不到这个 —— 两个都是 ValueError 的子类,但互相
            # 没有继承关系。漏出去的话线程直接崩,客户端收到的是断掉的连接,
            # 不是 HTTP 响应,前端只能报「网络错误」。
            # 非 UTF-8 的请求体不是假想:任何按系统编码(GBK 之类)拼 JSON
            # 的客户端都会这样发。
            self._error(400, "请求体不是 UTF-8 编码")
            return
        except json.JSONDecodeError:
            self._error(400, "请求体不是合法 JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "请求体应当是一个 JSON 对象")
            return

        if path == "/api/scan":
            drive = str(payload.get("drive") or "").strip()
            if not drive:
                self._error(400, "缺少 drive 参数")
                return
            drive = drive.rstrip("\\").rstrip(":").upper() + ":"
            ok, message = start_scan(drive)
            self._json({"ok": ok, "message": message}, status=200 if ok else 409)
            return

        if path == "/api/reveal":
            from ..reveal import RevealError, reveal

            try:
                result = reveal(
                    str(payload.get("drive") or ""),
                    str(payload.get("path") or ""),
                )
            except RevealError as exc:
                self._error(exc.status, exc.message)
                return
            self._json(result)
            return

        if path == "/api/schedule":
            from ..schedule import set_enabled

            want = bool(payload.get("enabled"))
            try:
                state = set_enabled(want)
            except Exception as exc:                # noqa: BLE001
                self._error(500, f"设置计划任务失败:{exc}")
                return
            self._json(state.as_dict())
            return

        self._error(404, f"没有这个接口:{path}")

    def _handle_api(self, path: str, params: dict) -> None:
        if path == "/api/scan/state":
            self._json(scan_state())
            return
        if path == "/api/schedule":
            from ..schedule import task_state

            try:
                self._json(task_state().as_dict())
            except Exception as exc:                # noqa: BLE001
                self._error(500, f"读取计划任务状态失败:{exc}")
            return

        handler = api.ROUTES.get(path)
        if handler is None:
            self._error(404, f"没有这个接口:{path}")
            return

        conn = None
        try:
            conn = db.connect()
            self._json(handler(conn, params))
        except api.ApiError as exc:
            self._error(exc.status, exc.message)
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:                    # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")
            config.log_path().parent.mkdir(parents=True, exist_ok=True)
            with open(config.log_path(), "a", encoding="utf-8") as fh:
                fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {path} 出错\n")
                fh.write(traceback.format_exc())
        finally:
            if conn is not None:
                conn.close()

    def _serve_static(self, path: str) -> None:
        root = config.web_dir().resolve()
        rel = "index.html" if path in ("/", "") else path.lstrip("/")

        target = (root / rel).resolve()
        # 目录穿越防护:解析后必须还在 web 目录里。
        # 用 is_relative_to 而不是比字符串前缀:前缀比较挡不住同级的
        # web 开头目录 —— /../webhooks/x 解析出来照样以 ...\web 开头,
        # 只要哪天多一个这样的目录,就能读到 web 之外的文件。
        if not target.is_relative_to(root):
            self._error(403, "拒绝访问")
            return
        if not target.is_file():
            self._error(404, "文件不存在")
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def serve(
    *, host: str = config.HOST, port: int = config.PORT, open_browser: bool = True
) -> None:
    """启动服务并阻塞。Ctrl+C 退出。"""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"拒绝监听 {host}。这个工具会暴露整块盘的目录结构,只能绑本机。"
        )

    # 提前建库,免得第一个请求才发现建不出来
    conn = db.connect()
    conn.close()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{port}/"

    print(f"Strata 已启动:{url}")
    print(f"数据库:{config.db_path()}")
    state = privileges.privilege_state()
    print(f"权限:{state.detail}")
    print("按 Ctrl+C 停止。")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        httpd.shutdown()
        httpd.server_close()
