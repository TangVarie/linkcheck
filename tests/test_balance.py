"""余额那一块。全部离线，一个真请求都不发。

这一层最要紧的三件事：

* **只许打那两个零费用端点。** 面板最硬的不变量是「不发付费请求」，
  而余额查询是个请求——所以它被单独关在 `balance.py` 里，
  这里用 AST 钉死它碰不到别的东西。
* **读不到就说读不到，不报 0。** 真的余额为 0 和读不到，要人做的事
  完全相反（去充值 vs 去查 Key），而 ¥0 长得像前者。
* **一家读不到不影响另一家。** 两个账户独立，合并成一句「读余额失败」
  会让人分不清该去哪家后台看。
"""

import ast
import inspect
import json
import unittest

from xhsearch import balance, panel, transport


def response(status=200, body="", content_type="application/json"):
    return transport.Response(status, content_type, body)


def ok(payload):
    return lambda url, headers: response(200, json.dumps(payload))


TIKHUB_OK = {"code": 200, "user_data": {"balance": 12.5, "free_credit": 3,
                                        "account_disabled": False}}
SDX_OK = {"balance": 4200}


# ---------- 不变量：只碰那两个零费用端点 ----------

class TestOnlyFreeEndpoints(unittest.TestCase):
    def test_the_whitelist_has_exactly_two_paths(self):
        """加第三个端点必须是**有意**的动作，会在这里绊一下。"""
        self.assertEqual(
            set(balance.FREE_ENDPOINTS),
            {balance.TIKHUB, balance.SOCIALDATAX})

    def test_no_other_api_path_appears_in_the_module(self):
        """模块里不许出现别的 /api/ 路径字面量——一条也不行。

        走 AST 取字符串常量，不搜源码文本：注释和文档表格里本来就写着
        付费端点的名字（那是**为了说明对照**），搜文本会把它们当成违规。
        """
        allowed = set(balance.FREE_ENDPOINTS.values())
        tree = ast.parse(inspect.getsource(balance))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if text.startswith("/") and len(text) > 1 and text not in allowed:
                    self.fail(f"balance.py 里出现了别的路径字面量：{text!r}")

    def test_it_does_not_import_the_paid_layers(self):
        """不 import providers / runner / protocol —— 这是这个模块存在的
        全部意义：不蹭付费端点的那根线，以后谁改一行也漏不过去。"""
        tree = ast.parse(inspect.getsource(balance))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(a.name for a in node.names)
                if node.module:
                    imported.add(node.module.split(".")[0])
        for forbidden in ("providers", "runner", "protocol", "feishu"):
            self.assertNotIn(forbidden, imported,
                             f"balance.py 不该 import {forbidden}")

    def test_the_panel_reaches_the_network_only_through_this_module(self):
        """面板自己仍然不许直接调 transport —— 新开的这条路必须走 balance。"""
        tree = ast.parse(inspect.getsource(panel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and \
                        node.func.value.id == "transport":
                    self.fail("panel.py 直接调了 transport")

    def test_urls_are_built_from_the_whitelist_only(self):
        seen = []
        balance.read_all({balance.TIKHUB: "k", balance.SOCIALDATAX: "k"},
                         tikhub_base="https://api.tikhub.io",
                         socialdatax_base="https://mcp.socialdatax.com/socialdatax/api/v1",
                         usd_to_cny=7.2,
                         get=lambda url, headers: (seen.append(url),
                                                   response(200, "{}"))[1])
        self.assertEqual(seen, [
            "https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
            "https://mcp.socialdatax.com/socialdatax/api/v1/points/balance",
        ])


# ---------- TikHub ----------

class TestTikHub(unittest.TestCase):
    def read(self, get, **kw):
        return balance.read_tikhub("key", base="https://api.tikhub.io",
                                   usd_to_cny=7.2, get=get, **kw)

    def test_balance_and_free_credit(self):
        b = self.read(ok(TIKHUB_OK))
        self.assertTrue(b.ok)
        self.assertEqual(b.amount, 12.5)
        self.assertAlmostEqual(b.yuan, 90.0)
        self.assertEqual(b.free_credit, 3)
        self.assertIn("$12.50", b.describe())

    def test_no_key_is_reported_not_treated_as_zero(self):
        b = balance.read_tikhub("", base="https://api.tikhub.io",
                                usd_to_cny=7.2, get=ok(TIKHUB_OK))
        self.assertIsNone(b.amount)
        self.assertIn("TIKHUB_API_KEY", b.error)

    def test_401_says_the_key_is_wrong(self):
        b = self.read(lambda url, headers: response(401, "nope"))
        self.assertIsNone(b.amount)
        self.assertIn("Key", b.error)

    def test_a_disabled_account_is_worse_than_a_low_balance(self):
        """余额可能还很好看，但一个请求都发不出去——那比余额低更急。"""
        payload = {"user_data": {"balance": 999, "account_disabled": True}}
        b = self.read(ok(payload))
        self.assertIsNone(b.amount)
        self.assertIn("停用", b.error)

    def test_garbage_body_does_not_become_zero(self):
        for body in ("", "not json", "[]", '{"user_data": {}}'):
            b = self.read(lambda url, headers, body=body: response(200, body))
            self.assertIsNone(b.amount, body)
            self.assertTrue(b.error, body)


# ---------- SocialDataX ----------

class TestSocialDataX(unittest.TestCase):
    def read(self, get):
        return balance.read_socialdatax(
            "key", base="https://mcp.socialdatax.com/socialdatax/api/v1", get=get)

    def test_points_convert_at_one_fen_each(self):
        b = self.read(ok(SDX_OK))
        self.assertTrue(b.ok)
        self.assertEqual(b.amount, 4200)
        self.assertAlmostEqual(b.yuan, 42.0)
        self.assertIn("4200 积分", b.describe())

    def test_a_wrapped_body_is_also_accepted(self):
        b = self.read(ok({"code": 0, "data": {"balance": 100}}))
        self.assertEqual(b.amount, 100)

    def test_a_real_zero_balance_is_not_an_error(self):
        """真的用光了要显示成 0 并报警，不能和「读不到」混为一谈。"""
        b = self.read(ok({"balance": 0}))
        self.assertTrue(b.ok)
        self.assertEqual(b.amount, 0)
        self.assertEqual(b.yuan, 0)


# ---------- 一家倒了不拖累另一家 ----------

class TestReadAll(unittest.TestCase):
    def test_one_channel_failing_leaves_the_other_readable(self):
        def get(url, headers):
            if "tikhub" in url:
                return response(500, "boom")
            return response(200, json.dumps(SDX_OK))

        tik, sdx = balance.read_all(
            {balance.TIKHUB: "a", balance.SOCIALDATAX: "b"},
            tikhub_base="https://api.tikhub.io",
            socialdatax_base="https://mcp.socialdatax.com/socialdatax/api/v1",
            usd_to_cny=7.2, get=get)
        self.assertFalse(tik.ok)
        self.assertIn("500", tik.error)
        self.assertTrue(sdx.ok)
        self.assertEqual(sdx.amount, 4200)


# ---------- 面板这一侧 ----------

def config(**kw):
    base = {"PANEL_PASSWORD": "a-long-enough-password"}
    base.update(kw)
    cfg = panel.PanelConfig.from_env(base)
    cfg.api_keys = {balance.TIKHUB: "a", balance.SOCIALDATAX: "b"}
    cfg.tikhub_base = "https://api.tikhub.io"
    cfg.socialdatax_base = "https://mcp.socialdatax.com/socialdatax/api/v1"
    return cfg


class TestBalanceFeed(unittest.TestCase):
    def feed(self, result=None, ttl=60.0):
        calls = []

        def read(keys, **kwargs):
            calls.append(1)
            if isinstance(result, Exception):
                raise result
            return result if result is not None else []

        return panel.BalanceFeed(config(), ttl, read=read), calls

    def test_it_caches_within_the_ttl(self):
        feed, calls = self.feed([balance.Balance(channel="x", amount=1, yuan=1)])
        feed.get(); feed.get(); feed.get()
        self.assertEqual(len(calls), 1)

    def test_an_empty_result_is_still_cached(self):
        """和日志那边同一个坑：空结果不进缓存就等于没有缓存。"""
        feed, calls = self.feed([])
        feed.get(); feed.get()
        self.assertEqual(len(calls), 1)

    def test_a_blowup_keeps_the_panel_alive(self):
        feed, calls = self.feed(RuntimeError("boom"))
        items, error = feed.get()
        self.assertEqual(items, [])
        self.assertIn("boom", error)

    def test_disabled_without_keys(self):
        cfg = config()
        cfg.api_keys = {}
        feed = panel.BalanceFeed(cfg, 60.0, read=lambda *a, **k: [])
        self.assertFalse(feed.enabled)
        _, why = feed.get()
        self.assertIn("免费", why)

    def test_the_switch_turns_it_off(self):
        cfg = config(PANEL_SHOW_BALANCE="0")
        feed = panel.BalanceFeed(cfg, 60.0, read=lambda *a, **k: [])
        self.assertFalse(feed.enabled)


# ---------- 还够跑多久 ----------

class FakeRun:
    def __init__(self, started, ended, cost):
        self.started_at, self.ended_at, self.cost_yuan = started, ended, cost


class TestRunway(unittest.TestCase):
    def balances(self, *yuan):
        return [balance.Balance(channel=f"c{i}", amount=v, yuan=v)
                for i, v in enumerate(yuan)]

    def runs(self, n, cost, span_hours):
        step = span_hours * 3600 / max(n - 1, 1)
        return [FakeRun(i * step, i * step + 60, cost) for i in range(n)]

    def test_days_left_from_the_real_burn_rate(self):
        # 24 小时里 4 轮，每轮 ¥1 → 约 ¥4/天；余额 ¥40 → 约 10 天。
        # 用 delta 而不是精确值：跨度是「第一轮开跑」到「最后一轮收尾」，
        # 比 24 小时多出最后那一轮自己跑的时间，那是对的。
        r = panel.runway_from_runs(self.balances(40.0), self.runs(4, 1.0, 24))
        self.assertTrue(r.known)
        self.assertAlmostEqual(r.yuan_per_day, 4.0, delta=0.05)
        self.assertAlmostEqual(r.days, 10.0, delta=0.1)

    def test_a_run_starting_at_epoch_zero_is_not_dropped(self):
        """真值判断会把 started_at == 0 的轮子悄悄丢掉，花速跟着偏。
        这个仓库踩过同一类坑（`now or time.time()` 把 epoch 0 当没传）。"""
        r = panel.runway_from_runs(self.balances(40.0), self.runs(4, 1.0, 24))
        self.assertEqual(r.runs_used, 4)

    def test_too_few_runs_says_so_instead_of_guessing(self):
        """一轮恰好赶上批量勾排队刷新，算出的日花速能高一个数量级，
        据此说「只够跑 2 天」会让人白白去充一笔钱。"""
        r = panel.runway_from_runs(self.balances(40.0), self.runs(2, 1.0, 24))
        self.assertFalse(r.known)
        self.assertIn("至少要", r.reason)

    def test_too_short_a_window_says_so(self):
        r = panel.runway_from_runs(self.balances(40.0), self.runs(5, 1.0, 0.5))
        self.assertFalse(r.known)
        self.assertIn("小时", r.reason)

    def test_an_unreadable_channel_makes_it_a_lower_bound(self):
        items = self.balances(40.0) + [
            balance.Balance(channel="dead", error="读不到")]
        r = panel.runway_from_runs(items, self.runs(4, 1.0, 24))
        self.assertTrue(r.partial)
        self.assertAlmostEqual(r.days, 10.0, delta=0.1)

    def test_no_readable_balance_at_all(self):
        r = panel.runway_from_runs(
            [balance.Balance(channel="x", error="读不到")], self.runs(4, 1.0, 24))
        self.assertFalse(r.known)
        self.assertIn("读不到", r.reason)

    def test_zero_spend_is_not_an_alarm(self):
        r = panel.runway_from_runs(self.balances(40.0), self.runs(4, 0.0, 24))
        self.assertFalse(r.known)
        self.assertIn("不用担心", r.reason)


# ---------- 页面：读不到绝不渲染成 ¥0 ----------

class TestRendering(unittest.TestCase):
    def render(self, balances, runway=None):
        from xhsearch import panel_view, summary
        return panel_view.overview_page(
            overview=summary.Overview(projects=[]), error="", fetched_at=0.0,
            config=config(), balances=balances, runway=runway)

    def test_an_unreadable_channel_never_shows_a_zero(self):
        html = self.render([balance.Balance(channel="tikhub", label="TikHub",
                                            error="HTTP 401：Key 不对或没权限")])
        self.assertIn("读不到", html)
        self.assertIn("这不等于余额为 0", html)
        self.assertNotIn("$0.00", html)

    def test_a_readable_channel_shows_both_units(self):
        html = self.render([balance.Balance(
            channel="tikhub", label="TikHub", amount=12.5, unit="USD",
            yuan=90.0, rate=7.2)])
        self.assertIn("$12.50", html)
        self.assertIn("¥90.00", html)

    def test_the_free_endpoint_claim_is_on_the_page(self):
        """页面要自己说清「看余额不花钱」，否则第一个看到它的人会怀疑。"""
        html = self.render([balance.Balance(channel="x", label="X",
                                            amount=1, unit="积分", yuan=0.01)])
        self.assertIn("零费用", html)


class TestKeysNeverReachTheBrowser(unittest.TestCase):
    """`PanelConfig` 现在**装着 API Key**（余额查询要用）。

    在这之前它只装口令和密钥，那两个本来就有专门的测试盯着。多装了东西
    就得多一条盯着的测试——不然哪天有人加个「把配置 dump 出来」的调试端点，
    漏的就是生产 Key。
    """

    KEY = "sk-live-DO-NOT-LEAK-0123456789"

    def test_no_rendered_page_contains_an_api_key(self):
        from xhsearch import panel_view, summary
        cfg = config()
        cfg.api_keys = {balance.TIKHUB: self.KEY,
                        balance.SOCIALDATAX: self.KEY}
        html = panel_view.overview_page(
            overview=summary.Overview(projects=[]), error="", fetched_at=0.0,
            config=cfg,
            balances=[balance.Balance(channel="tikhub", label="TikHub",
                                      amount=1.0, unit="USD", yuan=7.2, rate=7.2)],
            runway=panel.Runway(days=9.0, yuan_per_day=1.0, yuan_left=9.0,
                                runs_used=5, hours_covered=24.0))
        self.assertNotIn(self.KEY, html)

    def test_a_key_leaking_into_an_exception_is_scrubbed(self):
        """异常文本的内容不由我们决定——第三方库可能把请求头原样塞进去。"""
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"TIKHUB_API_KEY": self.KEY},
                             clear=False):
            cfg = panel.PanelConfig.from_env({
                "PANEL_PASSWORD": "a-long-enough-password",
                "TIKHUB_API_KEY": self.KEY})
        cfg.api_keys = {balance.TIKHUB: self.KEY}

        def boom(keys, **kwargs):
            raise RuntimeError(f"connection failed: Bearer {self.KEY}")

        feed = panel.BalanceFeed(cfg, 60.0, read=boom)
        _items, error = feed.get()
        self.assertNotIn(self.KEY, error)
        self.assertIn("connection failed", error)

    def test_the_config_is_never_serialised_wholesale(self):
        """页面和 JSON 接口都只读**具名字段**，没有 asdict/vars(config)
        这种把整个配置倒出去的写法。"""
        import inspect
        for module in (panel, __import__("xhsearch.panel_view",
                                         fromlist=["panel_view"])):
            source = inspect.getsource(module)
            for pattern in ("asdict(config", "vars(config",
                            "config.__dict__", "asdict(self.config"):
                self.assertNotIn(pattern, source,
                                 f"{module.__name__} 里有 {pattern}")


if __name__ == "__main__":
    unittest.main()
