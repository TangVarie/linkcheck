"""tools/ 下两个运维脚本的测试。

这两个脚本此前零覆盖，而它们恰恰是**做决策**用的：
probe_channel 决定「这条通道能不能上线」，estimate_cost 决定「这套东西一个月
要花多少钱」。它们算错的代价不是崩一个进程，是照着错结论做了错决定。
"""

import contextlib
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tools import estimate_cost, probe_channel
from xhsearch import providers, transport
from xhsearch.config import Settings
from xhsearch.rows import Row, plan_calls

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class TestProbeUsesTheSameRoutingAsProduction(unittest.TestCase):
    """COR-009：探针以前不过 provider.can_handle，和线上路由分叉。

    差别是真金白银的：把 v.douyin.com 短链塞进 TikHub 的 aweme_id 参数
    必然失败，而 TikHub 对失败的业务查询**照样计费**——探针会白扣一次费，
    然后报「这条通道不可用」，而线上其实会自动让给吃短链的 SocialDataX。
    """

    @staticmethod
    def _probe(*args, **kwargs):
        """探针是给人看的脚本，输出很吵。测试只关心它发不发请求。"""
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            result = probe_channel.probe(*args, **kwargs)
        return result, captured.getvalue()

    def test_douyin_short_link_on_tikhub_sends_no_request(self):
        with mock.patch.object(transport, "request") as sent:
            ok, output = self._probe(providers.TIKHUB, "t-key",
                                     "https://v.douyin.com/iRxYzAb/", Settings())
        sent.assert_not_called()
        self.assertTrue(ok, "跳过一条这家吃不了的链接不算「通道不可用」")
        self.assertIn("不发请求、不扣费", output)

    def test_force_actually_sends_the_request(self):
        """--force 是给「我就是要验一次」准备的，行为要如实。"""
        response = transport.Response(401, "application/json",
                                      '{"detail": {"code": 401, "message": "no"}}', "r")
        with mock.patch.object(transport, "request", return_value=response) as sent:
            self._probe(providers.TIKHUB, "t-key",
                        "https://v.douyin.com/iRxYzAb/", Settings(), force=True)
        self.assertTrue(sent.called)

    def test_supported_link_still_probes_normally(self):
        response = transport.Response(401, "application/json",
                                      '{"detail": {"code": 401, "message": "no"}}', "r")
        with mock.patch.object(transport, "request", return_value=response) as sent:
            ok, _ = self._probe(
                providers.TIKHUB, "t-key",
                "https://www.xiaohongshu.com/explore/" + "a" * 24, Settings())
        self.assertTrue(sent.called)
        self.assertFalse(ok)


def _simulate_calls_per_day(per_day, xhs_share, settings, step=0.02):
    """逐「日龄」仿真：直接用线上那份 plan_calls 数一天到底发多少个请求。

    这是成本模型的独立真值来源——两边用完全不同的算法算同一个数，
    对得上才说明模型没错。
    """
    xhs_calls = dy_calls = 0.0
    age = 0.0
    archive = settings.refresh.archive_after_days
    while age < archive:
        interval = settings.refresh.interval_hours_for_age(age)
        if interval is None:
            break
        refreshes = 24 / interval
        population = per_day * step
        published = int((NOW - timedelta(days=age)).timestamp() * 1000)
        xhs_row = Row("x", "https://www.xiaohongshu.com/explore/" + "a" * 24,
                      publish_time_ms=published)
        dy_row = Row("d", "https://www.douyin.com/video/7412345678901234567",
                     publish_time_ms=published)
        xhs_calls += population * xhs_share * refreshes * len(plan_calls(xhs_row, settings, NOW))
        dy_calls += population * (1 - xhs_share) * refreshes * len(plan_calls(dy_row, settings, NOW))
        age += step
    return xhs_calls, dy_calls


class TestCostModelMatchesTheRealCallPlan(unittest.TestCase):
    """SUP-005：旧模型拿「档位端点」代表整档，两种配置下会整档算错——

    * detail 窗口落在某一档的**中间**（detail=5，档位 2–7 天）
    * archive_after_days **小于** tiers 末端（tiers 到 30 天，archive 设 14）

    「预计成本」一旦不可信就等于没有，所以这里对着逐日仿真钉死误差。
    """

    CASES = [
        ("默认配置", 7, 30, None),
        ("detail 窗口切在档位中间", 5, 30, None),
        ("archive 小于 tiers 末端", 7, 14, None),
        ("archive 大于 tiers 末端", 7, 45, None),
        ("完全关掉 detail", 0, 30, None),
        ("自定义档位", 10, 60, [(3, 6), (10, 24), (20, 48)]),
    ]

    def test_model_matches_simulation_within_one_percent(self):
        for label, detail_days, archive, tiers in self.CASES:
            with self.subTest(label):
                settings = Settings()
                settings.detail_within_days = detail_days
                settings.refresh.archive_after_days = archive
                if tiers:
                    settings.refresh.tiers = tiers

                model_xhs, model_dy, _, _ = estimate_cost._calls_by_platform(20, 0.7, settings)
                sim_xhs, sim_dy = _simulate_calls_per_day(20, 0.7, settings)

                for model, sim, who in ((model_xhs, sim_xhs, "小红书"),
                                        (model_dy, sim_dy, "抖音")):
                    error = abs(model - sim) / max(sim, 1e-9)
                    self.assertLess(error, 0.01,
                                    f"{label} 的{who}调用数偏差 {error:.1%}"
                                    f"（模型 {model:.1f} vs 仿真 {sim:.1f}）")

    def test_archive_shorter_than_tiers_actually_shrinks_the_population(self):
        """旧模型会把整个 8–30 天档算进来，成本凭空多算一倍多。"""
        settings = Settings()
        settings.refresh.archive_after_days = 14
        segments = estimate_cost.tier_segments(settings)
        self.assertTrue(all(end <= 14 for _, end, _ in segments))
        self.assertEqual(max(end for _, end, _ in segments), 14)

    def test_archive_longer_than_tiers_extends_with_the_last_interval(self):
        settings = Settings()
        settings.refresh.archive_after_days = 45
        segments = estimate_cost.tier_segments(settings)
        self.assertEqual(max(end for _, end, _ in segments), 45)
        # 末段沿用最后一档的间隔，和 RefreshTiers.interval_hours_for_age 口径一致
        self.assertEqual(segments[-1][2], settings.refresh.tiers[-1][1])

    def test_estimate_reports_are_internally_consistent(self):
        settings = Settings()
        result = estimate_cost.estimate(20, 0.7, settings)
        self.assertAlmostEqual(
            result["calls_per_day"],
            result["comment_calls"] + result["detail_calls"], places=6)
        self.assertAlmostEqual(
            result["calls_per_day"],
            result["xhs_calls_per_day"] + result["douyin_calls_per_day"], places=6)


class TestPricingIsConfigurable(unittest.TestCase):
    """SUP-005：价格和汇率会变，改价不该需要改代码重新发版。"""

    def setUp(self):
        self._saved = (providers._USD_TO_CNY, dict(providers.TIKHUB_USD),
                       providers.SOCIALDATAX_YUAN)

    def tearDown(self):
        providers._USD_TO_CNY, tikhub, providers.SOCIALDATAX_YUAN = self._saved
        providers.TIKHUB_USD.clear()
        providers.TIKHUB_USD.update(tikhub)

    def test_override_changes_the_quoted_price(self):
        providers.set_pricing(usd_to_cny=8.0, tikhub_usd={"xhs": 0.02})
        self.assertAlmostEqual(
            providers.get_provider("tikhub").yuan_per_call("xhs", "comments"), 0.16)

    def test_nonsense_prices_are_rejected(self):
        for kwargs in ({"usd_to_cny": 0}, {"usd_to_cny": -1},
                       {"socialdatax_yuan": 0}, {"tikhub_usd": {"xhs": -0.1}}):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                providers.set_pricing(**kwargs)


if __name__ == "__main__":
    unittest.main()
