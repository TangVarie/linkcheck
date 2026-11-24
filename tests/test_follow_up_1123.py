"""11-23 follow-up: login throttle bucket key + runway shared-balance note."""
import http.client
import io
import types
import unittest

from xhsearch import balance, panel, panel_view


def _handler(headers, peer="10.0.0.9"):
    """用真实的请求头解析器造头，不用 dict：dict 测不到同名头出现两行，
    也测不到头名大小写不敏感——生产上 self.headers 是 HTTPMessage。"""
    raw = "".join(f"{name}: {value}\r\n" for name, value in headers) + "\r\n"
    parsed = http.client.parse_headers(io.BytesIO(raw.encode("latin-1")))
    return types.SimpleNamespace(headers=parsed, client_address=(peer, 4242))


class TestClientKey(unittest.TestCase):
    key = staticmethod(panel.PanelHandler._client_key)

    def test_real_ip_wins(self):
        h = _handler([("X-Real-IP", " 203.0.113.7 "), ("X-Forwarded-For", "1.1.1.1, 2.2.2.2")])
        self.assertEqual(self.key(h), "203.0.113.7")

    def test_duplicated_real_ip_takes_the_last_line(self):
        """边缘遇到客户端自带的 X-Real-IP，是覆盖还是再加一行，文档没写。
        取最后一行，两种做法下拿到的都是边缘写的那行；取第一行就是客户端写的。"""
        a = _handler([("X-Real-IP", "6.6.6.6"), ("X-Real-IP", "203.0.113.50")])
        b = _handler([("x-real-ip", "7.7.7.7"), ("X-Real-IP", "203.0.113.50")])
        self.assertEqual(self.key(a), "203.0.113.50")
        self.assertEqual(self.key(a), self.key(b))

    def test_rightmost_forwarded_hop_not_spoofable_leftmost(self):
        a = _handler([("X-Forwarded-For", "6.6.6.6, 198.51.100.4")])
        b = _handler([("X-Forwarded-For", "7.7.7.7, 198.51.100.4")])
        self.assertEqual(self.key(a), "198.51.100.4")
        self.assertEqual(self.key(a), self.key(b))

    def test_forwarded_lines_are_joined_before_taking_the_rightmost_hop(self):
        h = _handler([("X-Forwarded-For", "6.6.6.6"),
                      ("X-Forwarded-For", "203.0.113.9, 198.51.100.4")])
        self.assertEqual(self.key(h), "198.51.100.4")

    def test_blank_hops_ignored_and_peer_fallback(self):
        self.assertEqual(self.key(_handler([("X-Forwarded-For", "198.51.100.4, , ")])),
                         "198.51.100.4")
        self.assertEqual(self.key(_handler([("X-Forwarded-For", " , ")])), "10.0.0.9")
        self.assertEqual(self.key(_handler([])), "10.0.0.9")

    def test_key_is_bounded(self):
        self.assertEqual(len(self.key(_handler([("X-Real-IP", "x" * 500)]))), 64)
        self.assertEqual(len(self.key(_handler([("X-Forwarded-For", "y" * 500)]))), 64)


class _Run:
    def __init__(self, started, ended, cost):
        self.started_at, self.ended_at, self.cost_yuan = started, ended, cost


class TestRunwayNote(unittest.TestCase):
    RUNS = [_Run(i * 8 * 3600, i * 8 * 3600 + 60, 1.0) for i in range(4)]

    def _sub(self, balances):
        runway = panel.runway_from_runs(balances, self.RUNS)
        self.assertTrue(runway.known)
        return "".join(panel_view._runway_box(runway, types.SimpleNamespace()))

    def _tikhub(self, **kw):
        return balance.Balance(channel=balance.TIKHUB, label="TikHub",
                               amount=5.0, unit="USD", yuan=36.0, **kw)

    def _sdx(self, **kw):
        return balance.Balance(channel=balance.SOCIALDATAX, label="SocialDataX",
                               amount=3000, unit="积分", yuan=30.0, **kw)

    def test_runway_box_mentions_shared_balance(self):
        html = self._sub([self._tikhub(), self._sdx()])
        self.assertIn("实际花速", html)
        self.assertIn("外部抓取共用", html)
        # 约高估四分之一（显示 14 天实际约 11 天），不能说成「略少」
        self.assertIn("约少两成", html)
        self.assertNotIn("略少", html)

    def test_no_note_without_tikhub(self):
        """没配 TikHub（或只有 SocialDataX 的余额）时，共用余额无从谈起。"""
        html = self._sub([self._sdx()])
        self.assertNotIn("外部抓取共用", html)

    def test_partial_keeps_lower_bound_and_drops_the_note(self):
        """有一家读不到时前半句说「这是下界」（实际只会更多），后半句再说
        「实际更少」，两句方向相反，运营读着糊涂。"""
        for items in ([self._tikhub(), balance.Balance(channel=balance.SOCIALDATAX,
                                                       error="读不到")],
                      [balance.Balance(channel=balance.TIKHUB, error="没配 Key"),
                       self._sdx()]):
            html = self._sub(items)
            self.assertIn("这是下界", html)
            self.assertNotIn("外部抓取共用", html)

    def test_runway_box_hidden_when_unknown(self):
        self.assertEqual(panel_view._runway_box(None, types.SimpleNamespace()), [])


if __name__ == "__main__":
    unittest.main()
