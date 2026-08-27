"""POST 请求体的解析:坏输入要变成 HTTP 错误,不能把连接搞死。

这些用例只能打真 socket。请求体解析出问题时,异常是在处理线程里抛的 ——
单元测试直接调函数看不出区别,但客户端那边收到的是断掉的连接,
浏览器只会显示「网络错误」,什么都排查不出来。
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from strata.server.app import Handler


class PostBodyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
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

    def post(self, path: str, body: bytes, headers: dict | None = None):
        """发一个原始 POST,自己拼 header,好塞进各种畸形值。

        默认带上同源 Origin,让请求体的用例不被来源检查提前挡掉。
        传 None 表示把某个头去掉(要验「这个头缺席」时用)。
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            h = {"Content-Type": "application/json",
                 "Origin": f"http://127.0.0.1:{self.port}"}
            h.update(headers or {})
            h = {k: v for k, v in h.items() if v is not None}
            conn.request("POST", path, body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_non_utf8_body_is_400_not_a_dead_socket(self) -> None:
        """GBK 编码的请求体 —— 按系统编码拼 JSON 的客户端就是这么发的。

        真机上用 curl 撞出来的:服务端抛 UnicodeDecodeError,
        连接直接断,curl 报 exit 52(empty reply),没有任何 HTTP 响应。
        """
        body = '{"drive":"C:","path":"中文目录"}'.encode("gbk")
        status, payload = self.post("/api/reveal", body)
        self.assertEqual(status, 400)
        self.assertIn("UTF-8", json.loads(payload)["error"])

    def test_utf8_body_still_works(self) -> None:
        """修完非 UTF-8 之后,正常的 UTF-8 中文路径不能被一起挡掉。"""
        body = json.dumps({"drive": "C:", "path": "中文-绝对没有这个目录-xyz"},
                          ensure_ascii=False).encode("utf-8")
        status, payload = self.post("/api/reveal", body)
        # 路径不存在 → 404。关键是它走到了业务逻辑,而不是卡在解码。
        self.assertEqual(status, 404)
        self.assertIn("中文-绝对没有这个目录-xyz", json.loads(payload)["error"])

    def test_malformed_json_is_400(self) -> None:
        status, payload = self.post("/api/reveal", b"{not json at all")
        self.assertEqual(status, 400)
        self.assertIn("JSON", json.loads(payload)["error"])

    def test_json_that_is_not_an_object_is_400(self) -> None:
        for body in (b"[1,2,3]", b'"just a string"', b"42", b"null"):
            with self.subTest(body=body):
                status, _ = self.post("/api/reveal", body)
                self.assertEqual(status, 400)

    def test_bogus_content_length_is_400(self) -> None:
        """Content-Length 不是数字时,int() 会抛 —— 同样得变成 400。"""
        status, _ = self.post("/api/reveal", b"{}",
                              headers={"Content-Length": "abc"})
        self.assertEqual(status, 400)

    def test_oversized_content_length_is_refused_without_reading(self) -> None:
        """报一个巨大的 Content-Length,不能真去读那么多字节。

        身体只有几个字节,声明 4 GiB。要是照着读,这个请求会一直挂着
        等永远不会来的数据。
        """
        status, _ = self.post("/api/reveal", b"{}",
                              headers={"Content-Length": str(4 << 30)})
        self.assertEqual(status, 413)

    # 注意请求体里用的是不存在的路径。空路径会解析成盘根 —— 那是真存在的
    # 目录,一旦哪条用例意外放行,测试跑一遍就会弹出一堆资源管理器窗口。
    BAIT = b'{"drive":"C:","path":"Strata-test-no-such-dir"}'

    def test_cross_site_origin_is_refused(self) -> None:
        """任何网页都能往 127.0.0.1 发 POST,所以要看来源。"""
        status, _ = self.post("/api/reveal", self.BAIT,
                              headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_cross_site_fetch_metadata_is_refused(self) -> None:
        """没有 Origin 时退回看 Sec-Fetch-Site。

        这里必须显式清掉 Origin:两个头都在的时候,Origin 更具体,
        先看它是对的 —— 带同源 Origin 又标 cross-site 的组合浏览器发不出来。
        所以要验 Sec-Fetch-Site 这条分支,就得让 Origin 缺席。
        """
        status, _ = self.post("/api/reveal", self.BAIT,
                              headers={"Origin": None,
                                       "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_same_site_fetch_metadata_passes_the_guard(self) -> None:
        """同源的取值要放过去,不然正常页面自己也用不了。"""
        for site in ("same-origin", "none"):
            with self.subTest(site=site):
                status, _ = self.post("/api/reveal", self.BAIT,
                                      headers={"Origin": None,
                                               "Sec-Fetch-Site": site})
                # 过了来源检查,然后因为路径不存在而 404
                self.assertEqual(status, 404)

    def test_unknown_post_route_is_404(self) -> None:
        status, _ = self.post("/api/no-such-thing", b"{}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
