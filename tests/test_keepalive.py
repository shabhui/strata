"""连接复用:开了 HTTP/1.1 之后,连接边界上的行为都得钉住。

BaseHTTPRequestHandler 默认走 HTTP/1.0,一个请求一个连接,一个连接一个线程。
页面一次要发好几个请求,握手的开销比查询本身还大。改成 HTTP/1.1 之后连接会
留着复用 —— 也就意味着"响应完就把连接扔掉"这个隐含前提没了,原来靠它兜住的
几处得显式处理。这里全部用真 socket,因为要验的正是连接层的行为。
"""

from __future__ import annotations

import socket
import threading
import unittest
from http.server import ThreadingHTTPServer

from strata.server.app import Handler


class KeepAliveFixture(unittest.TestCase):
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

    def raw(self, payload: bytes, *, read_timeout: float = 5.0) -> bytes:
        """把原始字节灌进去,把对方回的全部读回来,直到它关连接。

        不用 http.client:要验的就是"服务端会不会把请求体当成第二个请求",
        任何帮我们管连接的客户端库都会把这件事藏起来。
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout)
        try:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)     # 告诉对方没有更多请求了
            chunks = []
            while True:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks)
        finally:
            sock.close()


class ConnectionReuseTest(KeepAliveFixture):
    def test_two_requests_share_one_connection(self) -> None:
        """一条连接上连发两个请求,两个都要有响应。

        这是 keep-alive 的全部意义:实测目录树接口 20 ms 降到 4.5 ms,省下的
        就是第二次握手和第二个线程。
        """
        req = (b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
               b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        out = self.raw(req)
        self.assertEqual(out.count(b"HTTP/1.1 200"), 2, out[:200])

    def test_announces_http_11(self) -> None:
        out = self.raw(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 "), out[:60])

    def test_every_response_declares_length(self) -> None:
        """没有 Content-Length,客户端会一直等下一个字节 —— keep-alive 的前提。"""
        for path in (b"/", b"/app.js", b"/nope-does-not-exist", b"/api/nope"):
            with self.subTest(path=path.decode()):
                out = self.raw(b"GET " + path + b" HTTP/1.1\r\nHost: x\r\n\r\n")
                head = out.split(b"\r\n\r\n", 1)[0].lower()
                self.assertIn(b"content-length:", head, out[:200])

    def test_idle_connections_are_not_held_forever(self) -> None:
        """连接会一直挂着,而 socketserver 是一个连接一个线程。

        不设 timeout 就等于每个开过页面的浏览器永久占一个线程。基类的
        handle_one_request 捕获 socket.timeout 之后会置 close_connection。
        真去等一次超时太慢,这里只钉住属性本身。
        """
        self.assertIsNotNone(Handler.timeout)
        self.assertGreater(Handler.timeout, 0)


class UnreadBodyTest(KeepAliveFixture):
    """在没读请求体的时候拒掉请求 —— 必须同时关连接。

    否则没读走的请求体躺在接收缓冲里,基类会把它的第一行当成下一个请求的起始行。
    HTTP/1.0 时代连接随手就关了,所以这几条路径从来没露出来过。
    """

    # 请求体本身长得像一个请求。要是服务端把它当请求处理了,就会多出一个响应。
    SMUGGLED = b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"

    def _post(self, headers: bytes, body: bytes) -> bytes:
        return self.raw(b"POST /api/scan HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        + headers + b"\r\n" + body)

    def test_cross_origin_reject_does_not_leak_body(self) -> None:
        out = self._post(
            b"Origin: http://evil.example\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(self.SMUGGLED)).encode() + b"\r\n",
            self.SMUGGLED,
        )
        self.assertIn(b" 403", out.split(b"\r\n", 1)[0], out[:200])
        self.assertEqual(self._count_responses(out), 1, out[:400])

    def test_bad_content_length_does_not_leak_body(self) -> None:
        """长度读不出来就更不能留着连接 —— 根本不知道该丢掉多少字节。"""
        out = self._post(
            b"Origin: http://127.0.0.1:" + str(self.port).encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: abc\r\n",
            self.SMUGGLED,
        )
        self.assertIn(b" 400", out.split(b"\r\n", 1)[0], out[:200])
        self.assertEqual(self._count_responses(out), 1, out[:400])

    def test_oversized_body_does_not_leak_body(self) -> None:
        """超限的请求体不能"先读干净再拒" —— 要读的正是判定读不起的东西。"""
        out = self._post(
            b"Origin: http://127.0.0.1:" + str(self.port).encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 99999999\r\n",
            self.SMUGGLED,
        )
        self.assertIn(b" 413", out.split(b"\r\n", 1)[0], out[:200])
        self.assertEqual(self._count_responses(out), 1, out[:400])

    def test_reject_closes_the_connection(self) -> None:
        out = self._post(
            b"Origin: http://evil.example\r\nContent-Length: 0\r\n", b"")
        self.assertIn(b"connection: close", out.lower(), out[:300])

    @staticmethod
    def _count_responses(raw: bytes) -> int:
        return raw.count(b"HTTP/1.1 ") + raw.count(b"HTTP/1.0 ")


if __name__ == "__main__":
    unittest.main()
