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
    这样 get_content_charset() 的行为和真响应一致。

    read() 刻意**不收**长度参数：真实的 http.client 响应是收的，但一些
    file-like 替身不收。传输层要能同时对付这两种，所以这里保留窄签名，
    分块读的那条路由由 _ChunkedResp 覆盖。"""

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


class _ChunkedResp(_FakeResp):
    """收长度参数的 read()，和真实 http.client 响应一致。"""

    def __init__(self, body=b"ok", status=200, headers=None):
        super().__init__(body, status, headers)
        self._offset = 0

    def read(self, amount=None):
        if amount is None:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


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


class TestResponseSizeLimits(unittest.TestCase):
    """ROB-006：响应体没有上限时，一个超大 body 或 gzip 炸弹会把进程 OOM 掉——
    而 OOM 会把本轮已经付费刷完、还没写回的结果**全部**丢掉，
    这是所有崩溃里最贵的一种。"""

    def test_oversized_plain_body_is_rejected_not_buffered(self):
        huge = b"a" * (transport.MAX_COMPRESSED_BYTES + 1024)
        with _urlopen_returning(_ChunkedResp(huge)):
            resp = transport.get("https://x", {})
        self.assertTrue(resp.oversized)
        self.assertEqual(resp.status, 0)
        self.assertIn("响应体过大", resp.body)

    def test_gzip_bomb_is_capped_before_it_can_blow_up_memory(self):
        # 压缩体很小、解压后极大：这正是 gzip 炸弹的形态。
        bomb = gzip.compress(b"\0" * (transport.MAX_BODY_BYTES + 4096))
        self.assertLess(len(bomb), transport.MAX_COMPRESSED_BYTES)
        with _urlopen_returning(_ChunkedResp(bomb, headers={"Content-Encoding": "gzip"})):
            resp = transport.get("https://x", {})
        self.assertTrue(resp.oversized)
        self.assertIn("响应体过大", resp.body)

    def test_normal_body_is_unaffected(self):
        with _urlopen_returning(_ChunkedResp(b'{"ok":true}')):
            resp = transport.get("https://x", {})
        self.assertFalse(resp.oversized)
        self.assertEqual(resp.json(), {"ok": True})

    def test_oversized_response_is_not_retried(self):
        """再来一次还是那么大：重试只是白花时间和上游配额。"""
        calls = {"n": 0}

        def fake_request(method, url, headers, body="", timeout=30.0):
            calls["n"] += 1
            return transport.Response(0, "", "响应体过大", oversized=True)

        with mock.patch.object(transport, "request", side_effect=fake_request):
            transport.request_with_retry("GET", "https://x", {},
                                         should_retry=lambda r: True, sleep=lambda _: None)
        self.assertEqual(calls["n"], 1)


class TestRetryBehaviour(unittest.TestCase):
    """ROB-011：固定的 2/4 秒退避会让多个实例在同一毫秒一起回来，
    重试本身把限流拖得更久。"""

    def test_backoff_has_jitter_and_respects_the_ceiling(self):
        samples = [transport.backoff_delay(1, 2.0) for _ in range(50)]
        self.assertTrue(all(0 <= d <= 4.0 for d in samples))
        self.assertGreater(len(set(samples)), 1, "退避必须带抖动，不能是固定值")

    def test_server_retry_after_becomes_the_floor(self):
        samples = [transport.backoff_delay(0, 2.0, retry_after=10.0) for _ in range(20)]
        self.assertTrue(all(d >= 10.0 for d in samples), "服务端给了等待时间就要至少等这么久")
        self.assertTrue(all(d <= 12.0 for d in samples))

    def test_retry_after_header_is_parsed_and_clamped(self):
        with _urlopen_returning(_ChunkedResp(b"slow down", status=429,
                                             headers={"Retry-After": "7"})):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.retry_after, 7.0)

        with _urlopen_returning(_ChunkedResp(b"slow down", status=429,
                                             headers={"Retry-After": "99999"})):
            resp = transport.get("https://x", {})
        self.assertEqual(resp.retry_after, transport.MAX_RETRY_AFTER_SECONDS)

        with _urlopen_returning(_ChunkedResp(b"x", headers={"Retry-After": "不是数字"})):
            resp = transport.get("https://x", {})
        self.assertIsNone(resp.retry_after)


if __name__ == "__main__":
    unittest.main()
