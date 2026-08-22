"""传输层「永远返回 Response」契约的测试。

urllib 只把建连阶段的 OSError 包成 URLError；读取阶段的异常（状态行前断连、
读 body 中途连接重置、坏 gzip、乱报的 charset）会裸抛。这里逐一验证它们
都被兜住——漏一个，一次网络毛刺就会炸穿整批，write_back 执行不到，
本轮已经花钱刷完的行一条都不写回。
"""

import gzip
import http.client
import io
import unittest
from email.message import Message
from unittest import mock

from xhsearch import transport


class _FakeResp:
    """顶替 urlopen 返回值的最小实现。headers 用真的 email.Message，
    这样 get_content_charset() 的行为和真响应一致。"""

    def __init__(self, body=b"ok", status=200, headers=None, read_exc=None):
        self.status = status
        self._body = body
        self._read_exc = read_exc
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self):
        if self._read_exc is not None:
            raise self._read_exc
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _urlopen_returning(resp):
    return mock.patch("urllib.request.urlopen", return_value=resp)


class TestExceptionEnvelope(unittest.TestCase):
    def test_remote_disconnected_before_status_line(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http.client.RemoteDisconnected("closed")):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.status, 0)
        self.assertIn("RemoteDisconnected", resp.body)

    def test_connection_reset_while_reading_body(self):
        with _urlopen_returning(_FakeResp(read_exc=ConnectionResetError("reset"))):
            resp = transport.post("https://x", {}, "{}")
        self.assertEqual(resp.status, 0)
        self.assertIn("ConnectionResetError", resp.body)

    def test_incomplete_read(self):
        exc = http.client.IncompleteRead(b"partial")
        with _urlopen_returning(_FakeResp(read_exc=exc)):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.status, 0)

    def test_bad_gzip_body_falls_back_to_raw(self):
        """响应头声称 gzip、body 却不是：不炸，按原样解码交给上层报错。"""
        with _urlopen_returning(_FakeResp(b"not-gzip", headers={"Content-Encoding": "gzip"})):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.status, 200)
        self.assertIn("not-gzip", resp.body)

    def test_good_gzip_still_decompresses(self):
        payload = gzip.compress("你好".encode("utf-8"))
        with _urlopen_returning(_FakeResp(payload, headers={"Content-Encoding": "gzip"})):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.body, "你好")

    def test_unknown_charset_falls_back_to_utf8(self):
        headers = {"Content-Type": "text/plain; charset=definitely-not-a-charset"}
        with _urlopen_returning(_FakeResp("中文".encode("utf-8"), headers=headers)):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.body, "中文")

    def test_http_error_with_unreadable_body_keeps_status(self):
        import urllib.error

        http_error = urllib.error.HTTPError("https://x", 502, "Bad Gateway", Message(), io.BytesIO())
        http_error.read = mock.MagicMock(side_effect=ConnectionResetError("mid-read"))
        with mock.patch("urllib.request.urlopen", side_effect=http_error):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.status, 502)
        self.assertIn("读取错误响应失败", resp.body)


if __name__ == "__main__":
    unittest.main()
