"""静态文件服务:只许拿 web 目录里的东西。

这个工具端到端暴露的是整块盘的目录结构,静态服务要是能穿出 web 目录,
读到的就不止是几个 css。所以起一个真服务器,用真 HTTP 请求打。
"""

from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from strata import config
from strata.server.app import Handler


class StaticServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 端口交给系统挑,免得撞上真在跑的实例
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, bytes]:
        """发一个原始请求,不让 urllib 帮忙规整路径。"""
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_serves_index_at_root(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_serves_known_assets(self) -> None:
        # i18n.js 少了的话页面上每个 data-i18n 节点都会停在 HTML 里的中文默认值,
        # 而 app.js 一上来就调 t(),整页直接白 —— 属于「404 一个文件就全废」。
        for name in ("/app.js", "/app.css", "/i18n.js"):
            with self.subTest(name=name):
                status, body = self.get(name)
                self.assertEqual(status, 200)
                self.assertTrue(body)

    def test_missing_file_is_404(self) -> None:
        status, _ = self.get("/nope-does-not-exist.txt")
        self.assertEqual(status, 404)

    def test_traversal_is_refused(self) -> None:
        """各种往上爬的写法都不能拿到 web 之外的文件。

        %2e%2e 那几行是重点:服务器会先解码再拼路径,只挡字面上的 '..'
        是挡不住的。
        """
        attempts = [
            "/../config.py",
            "/../../strata/config.py",
            "/..%2fconfig.py",
            "/%2e%2e/config.py",
            "/%2e%2e%2fconfig.py",
            "/....//config.py",
            "/..\\config.py",
            "/%2e%2e%5cconfig.py",
            "/web/../../config.py",
        ]
        for path in attempts:
            with self.subTest(path=path):
                status, body = self.get(path)
                self.assertIn(status, (403, 404), f"{path} 返回了 {status}")
                self.assertNotIn(b"TS_MIN", body)      # config.py 里的东西
                self.assertNotIn(b"def safe_day", body)

    def test_sibling_dir_sharing_the_web_prefix_is_refused(self) -> None:
        """比字符串前缀会漏的那种:web 旁边有个 web 开头的目录。

        现在恰好没有这样的目录,所以这条测的是规则本身 —— 造一个出来,
        确认它读不到,免得哪天真多了一个 webhooks\\ 就成了漏洞。
        """
        root = config.web_dir().resolve()
        sibling = root.parent / (root.name + "hooks")
        secret = sibling / "secret.txt"
        created = not sibling.exists()
        sibling.mkdir(parents=True, exist_ok=True)
        secret.write_text("SENTINEL-DO-NOT-SERVE", encoding="utf-8")
        try:
            for path in (
                f"/../{sibling.name}/secret.txt",
                f"/%2e%2e/{sibling.name}/secret.txt",
            ):
                with self.subTest(path=path):
                    status, body = self.get(path)
                    self.assertIn(status, (403, 404))
                    self.assertNotIn(b"SENTINEL", body)
        finally:
            secret.unlink(missing_ok=True)
            if created:
                sibling.rmdir()

    def test_does_not_serve_the_database(self) -> None:
        status, body = self.get("/strata.db")
        self.assertIn(status, (403, 404))
        self.assertNotIn(b"SQLite format", body)


if __name__ == "__main__":
    unittest.main()
