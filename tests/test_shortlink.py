"""抖音短链展开：v.douyin.com → aweme_id，免费的那一跳。

它存在的理由是让抖音真的走上 TikHub：TikHub 的抖音端点只吃数字 ID，而运营
贴的几乎全是短链。没有它，抖音实际上是 SocialDataX 单通道——SocialDataX
积分一空整个平台停摆，「抖音省 93%」一分都没兑现。

这里验三件事：能展开的展得对；展不开的败得干净（不抛、不敲陌生主机、
不无限跟跳）；一轮之内同一条短链只展一次。
"""

import threading
import unittest

from xhsearch import shortlink, transport

SHORT = "https://v.douyin.com/iRxYzAb/"
LANDING = "https://www.iesdouyin.com/share/video/7123456789012345678/?region=CN&mid=1"


def redirect(location: str, status: int = 302) -> transport.Response:
    return transport.Response(status, "text/html", "", location=location)


def page(status: int = 200) -> transport.Response:
    return transport.Response(status, "text/html", "<html>...</html>")


class _Fetch:
    """按顺序回放响应，并记下每一跳敲的是哪个地址。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, timeout):
        self.urls.append(url)
        self.headers.append(headers)
        if not self.responses:
            raise AssertionError(f"多发了一跳：{url}")
        return self.responses.pop(0)


class TestExpand(unittest.TestCase):
    def test_one_hop_video_share_link(self):
        fetch = _Fetch(redirect(LANDING))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertTrue(result.ok)
        self.assertEqual(result.aweme_id, "7123456789012345678")
        self.assertEqual(result.requests, 1)
        self.assertEqual(result.final_url, LANDING)
        self.assertEqual(fetch.urls, [SHORT])

    def test_note_share_link_is_recognised_too(self):
        """图文的落地页是 /share/note/<id>。"""
        fetch = _Fetch(redirect("https://www.iesdouyin.com/share/note/7000000000000000001/"))
        self.assertEqual(shortlink.expand_douyin(SHORT, fetch=fetch).aweme_id,
                         "7000000000000000001")

    def test_sends_a_browser_user_agent(self):
        """抖音对脚本 UA 回验证页而不是 302；这一跳必须像浏览器。"""
        fetch = _Fetch(redirect(LANDING))
        shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertEqual(fetch.headers[0]["User-Agent"], transport.BROWSER_UA)

    def test_relative_location_is_resolved_against_the_current_hop(self):
        fetch = _Fetch(redirect("/share/video/7123456789012345678/"))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertEqual(result.aweme_id, "7123456789012345678")
        self.assertTrue(result.final_url.startswith("https://v.douyin.com/"))

    def test_follows_a_second_hop_when_the_first_has_no_id(self):
        fetch = _Fetch(redirect("https://www.douyin.com/redirect?x=1"),
                       redirect("https://www.douyin.com/video/7123456789012345678"))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertEqual(result.aweme_id, "7123456789012345678")
        self.assertEqual(result.requests, 2)

    def test_link_that_already_carries_an_id_sends_nothing(self):
        fetch = _Fetch()
        result = shortlink.expand_douyin("https://www.douyin.com/video/7123456789012345678",
                                         fetch=fetch)
        self.assertEqual(result.aweme_id, "7123456789012345678")
        self.assertEqual(result.requests, 0)
        self.assertEqual(fetch.urls, [])


class TestExpandFailsCleanly(unittest.TestCase):
    """展开是免费的辅助步骤：失败不抛异常、说清原因，让调用方按原路走。"""

    def test_network_failure(self):
        fetch = _Fetch(transport.Response(0, "", "网络错误：timed out"))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertFalse(result.ok)
        self.assertIsNone(result.aweme_id)
        self.assertIn("网络", result.reason)
        self.assertEqual(result.requests, 1)

    def test_no_redirect_means_the_landing_page_changed_or_link_expired(self):
        fetch = _Fetch(page(200))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertFalse(result.ok)
        self.assertIn("没有跳转", result.reason)
        self.assertIn("200", result.reason)

    def test_404_is_a_failure_not_an_exception(self):
        result = shortlink.expand_douyin(SHORT, fetch=_Fetch(page(404)))
        self.assertFalse(result.ok)
        self.assertIn("404", result.reason)

    def test_never_follows_a_redirect_off_douyin(self):
        """运营贴的链接谁都能改。跳出抖音域名就停，不拿它去敲陌生主机。"""
        fetch = _Fetch(redirect("https://evil.example/share/video/7123456789012345678/"))
        result = shortlink.expand_douyin(SHORT, fetch=fetch)
        self.assertFalse(result.ok)
        self.assertIn("非抖音域名", result.reason)
        self.assertEqual(fetch.urls, [SHORT])   # 只敲了第一跳

    def test_lookalike_domain_is_not_douyin(self):
        fetch = _Fetch(redirect("https://douyin.com.evil.example/share/video/7123456789012345678/"))
        self.assertFalse(shortlink.expand_douyin(SHORT, fetch=fetch).ok)

    def test_non_douyin_input_sends_nothing(self):
        fetch = _Fetch()
        result = shortlink.expand_douyin("https://xhslink.com/a/AbC", fetch=fetch)
        self.assertFalse(result.ok)
        self.assertEqual(fetch.urls, [])

    def test_empty_input(self):
        self.assertFalse(shortlink.expand_douyin("", fetch=_Fetch()).ok)

    def test_redirect_loop_is_capped(self):
        loop = [redirect("https://v.douyin.com/loopA/"), redirect("https://v.douyin.com/loopB/")]
        fetch = _Fetch(*(loop * 10))
        result = shortlink.expand_douyin(SHORT, fetch=fetch, max_hops=3)
        self.assertFalse(result.ok)
        self.assertEqual(result.requests, 3)
        self.assertIn("3 跳", result.reason)


class TestResolverCache(unittest.TestCase):
    def test_same_link_is_expanded_once_per_run(self):
        fetch = _Fetch(redirect(LANDING))
        resolver = shortlink.Resolver(fetch=fetch)
        first = resolver.resolve(SHORT)
        second = resolver.resolve(SHORT)
        self.assertEqual(first.aweme_id, second.aweme_id)
        self.assertEqual(fetch.urls, [SHORT])
        self.assertEqual(resolver.expanded, 1)
        self.assertEqual(resolver.requests, 1)
        # 命中缓存的那次没发请求，别让调用方把同一跳记两次账
        self.assertEqual(first.requests, 1)
        self.assertEqual(second.requests, 0)

    def test_failures_are_cached_too(self):
        """同一轮里再展一次多半还是同一个结果，而每次都是一个真实请求。"""
        fetch = _Fetch(page(200))
        resolver = shortlink.Resolver(fetch=fetch)
        resolver.resolve(SHORT)
        resolver.resolve(SHORT)
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual(resolver.failed, 1)

    def test_deadline_stops_new_expansions(self):
        fetch = _Fetch()
        resolver = shortlink.Resolver(fetch=fetch, deadline=0.0)   # 早就过了
        result = resolver.resolve(SHORT)
        self.assertFalse(result.ok)
        self.assertIn("软截止", result.reason)
        self.assertEqual(fetch.urls, [])

    def test_concurrent_callers_do_not_corrupt_the_counters(self):
        fetch = _Fetch(*[redirect(LANDING)] * 8)
        resolver = shortlink.Resolver(fetch=fetch)
        links = [f"https://v.douyin.com/L{i}/" for i in range(8)]

        def work(url):
            resolver.resolve(url)

        threads = [threading.Thread(target=work, args=(u,)) for u in links]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(resolver.expanded, 8)
        self.assertEqual(resolver.requests, 8)


if __name__ == "__main__":
    unittest.main()
