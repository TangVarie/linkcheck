"""11-23 follow-up: login throttle bucket key + runway shared-balance note."""
import types
import unittest

from xhsearch import panel, panel_view


def _handler(headers, peer="10.0.0.9"):
    return types.SimpleNamespace(headers=dict(headers), client_address=(peer, 4242))


class TestClientKey(unittest.TestCase):
    key = staticmethod(panel.PanelHandler._client_key)

    def test_real_ip_wins(self):
        h = _handler({"X-Real-IP": " 203.0.113.7 ", "X-Forwarded-For": "1.1.1.1, 2.2.2.2"})
        self.assertEqual(self.key(h), "203.0.113.7")

    def test_rightmost_forwarded_hop_not_spoofable_leftmost(self):
        a = _handler({"X-Forwarded-For": "6.6.6.6, 198.51.100.4"})
        b = _handler({"X-Forwarded-For": "7.7.7.7, 198.51.100.4"})
        self.assertEqual(self.key(a), "198.51.100.4")
        self.assertEqual(self.key(a), self.key(b))

    def test_blank_hops_ignored_and_peer_fallback(self):
        self.assertEqual(self.key(_handler({"X-Forwarded-For": "198.51.100.4, , "})),
                         "198.51.100.4")
        self.assertEqual(self.key(_handler({"X-Forwarded-For": " , "})), "10.0.0.9")
        self.assertEqual(self.key(_handler({})), "10.0.0.9")

    def test_key_is_bounded(self):
        self.assertEqual(len(self.key(_handler({"X-Real-IP": "x" * 500}))), 64)
        self.assertEqual(len(self.key(_handler({"X-Forwarded-For": "y" * 500}))), 64)


class TestRunwayNote(unittest.TestCase):
    def test_runway_box_mentions_shared_balance(self):
        runway = types.SimpleNamespace(known=True, days=30.0, yuan_per_day=1.5, partial=False)
        html = "".join(panel_view._runway_box(runway, types.SimpleNamespace()))
        self.assertIn("实际花速", html)
        self.assertIn("外部抓取共用", html)

    def test_runway_box_hidden_when_unknown(self):
        self.assertEqual(panel_view._runway_box(None, types.SimpleNamespace()), [])


if __name__ == "__main__":
    unittest.main()
