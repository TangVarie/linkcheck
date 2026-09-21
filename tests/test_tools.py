"""tools/ 下两个运维脚本的测试。

这两个脚本此前零覆盖，而它们恰恰是**做决策**用的：
probe_channel 决定「这条通道能不能上线」，estimate_cost 决定「这套东西一个月
要花多少钱」。它们算错的代价不是崩一个进程，是照着错结论做了错决定。
"""

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import cli
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

    @staticmethod
    def _expand(response):
        """顶掉短链展开那一跳（transport.get_no_redirect），别让测试真去敲抖音。"""
        return mock.patch.object(transport, "get_no_redirect", return_value=response)

    UNREACHABLE = transport.Response(0, "", "网络错误：test")
    REDIRECT = transport.Response(
        302, "", "", location="https://www.iesdouyin.com/share/video/7123456789012345678/")

    def test_douyin_short_link_on_tikhub_sends_no_request(self):
        """短链展不开时，和从前一样：TikHub 不吃链接，不发请求。"""
        with self._expand(self.UNREACHABLE), mock.patch.object(transport, "request") as sent:
            ok, output = self._probe(providers.TIKHUB, "t-key",
                                     "https://v.douyin.com/iRxYzAb/", Settings())
        sent.assert_not_called()
        self.assertTrue(ok, "跳过一条这家吃不了的链接不算「通道不可用」")
        self.assertIn("不发请求、不扣费", output)
        self.assertIn("短链展不开", output)

    def test_douyin_short_link_expands_and_then_probes_tikhub(self):
        """线上开跑会先免费展开短链再挑通道，探针必须走同一条路——
        否则它会对一条线上明明能走 TikHub 的链接报「TikHub 不吃」。"""
        response = transport.Response(401, "application/json",
                                      '{"detail": {"code": 401, "message": "no"}}', "r")
        with self._expand(self.REDIRECT), \
             mock.patch.object(transport, "request", return_value=response) as sent:
            ok, output = self._probe(providers.TIKHUB, "t-key",
                                     "https://v.douyin.com/iRxYzAb/", Settings())
        self.assertTrue(sent.called)
        self.assertIn("aweme_id=7123456789012345678", sent.call_args.args[1])
        self.assertIn("短链已展开", output)
        self.assertFalse(ok)   # 401 是真失败，照实报

    def test_raw_dumps_every_unnormalised_comment_without_the_noise(self):
        """--raw 打印整页评论的原始字段：验抖音有没有蓝词字段全靠它。
        蓝词往往不在第一条，所以要整页；头像等杂项和空值要去掉，否则一条
        评论两千多字符，真正要看的字段全被挤出屏幕。"""
        body = json.dumps({"code": 200, "data": {"comments": [
            {"text": "好看", "text_extra": [], "label_type": -1, "digg_count": 3,
             "user": {"nickname": "甲", "avatar_thumb": {"url_list": ["https://x/a.heic"]}},
             "reply_comment": [{"text": "谢谢", "label_text": "作者", "text_extra": [],
                                "user": {"nickname": "作者本人", "sec_uid": "MS4w",
                                         "avatar_thumb": {"url_list": ["https://x/b.heic"]}}}]},
            {"text": "买了 协春堂", "text_extra": [{"hashtag_name": "协春堂", "type": 1}],
             "digg_count": 0, "user": {"nickname": "乙", "sec_uid": "MS4w"}},
            {"text": "@丙 看", "digg_count": 0, "user": {"nickname": "丁"},
             "text_extra": [{"start": 0, "end": 2, "user_id": "9", "type": 0,
                             "hashtag_name": "", "hashtag_id": ""}]},
        ], "total": 2, "cursor": 20, "has_more": 0, "extra": {"now": 0, "fatal_item_ids": []},
            "comment_search_words": [{"word": "协春堂", "cid": "1"}]}})
        comments = transport.Response(200, "application/json", body, "r1")
        detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"aweme_detail": {"statistics": {"digg_count": 1,
                                                                  "comment_count": 1}}}}), "r2")
        with mock.patch.object(transport, "request", side_effect=[comments, detail]):
            _ok, output = self._probe(providers.TIKHUB, "t-key",
                                      "https://www.douyin.com/video/7123456789012345678",
                                      Settings(), raw=True)
        self.assertIn("共 3 条", output)
        self.assertIn("#3 ", output)                       # 后面的条目也打了
        self.assertIn('"hashtag_name": "协春堂"', output)   # 有值的富文本结构保留
        self.assertNotIn("avatar_thumb", output)           # 头像杂项去掉
        self.assertNotIn("sec_uid", output)
        self.assertNotIn('"label_type"', output)           # -1 这种占位去掉
        self.assertIn('"nickname": "乙"', output)          # 昵称留着，好对照手机
        # 二级回复（作者回复）也是评论条目，里面的 user 同样只留昵称
        self.assertIn('"nickname": "作者本人"', output)
        self.assertIn('"label_text": "作者"', output)
        # 但 text_extra 这类元数据**不**递归去空值：`type: 0`（@ 提及）、
        # `start: 0`（从第 0 个字开始）的零是有信息的，--raw 正是要看它们。
        self.assertIn('"start": 0, "end": 2, "user_id": "9", "type": 0', output)
        self.assertIn("蓝词", output)
        # 页级字段：评论列表换成占位，其余保留，空值去掉——抖音的蓝词若不在
        # 每条评论上，就只可能在这里。
        self.assertIn("页级字段", output)
        self.assertIn('"comments": "（3 条评论，见上）"', output)
        self.assertIn('"comment_search_words": [{"word": "协春堂"', output)
        self.assertIn('"total": 2', output)
        self.assertNotIn('"has_more"', output)              # 0 这种占位去掉
        self.assertNotIn('"extra"', output)                 # 全空的子对象整个去掉

    def _sdx_page(self, items):
        """SocialDataX 的成功响应：顶层就是归一化后的形状，**不带 code 字段**
        （规范原文：「成功响应不会返回该字段」）。"""
        return transport.Response(200, "application/json", json.dumps({
            "items": items, "comment_count": 3, "top_level_comment_count": 2,
            "points": {"cost": 10, "balance": 990},
        }), "r1")

    SDX_XHS_LINK = "https://www.xiaohongshu.com/explore/" + "b" * 24

    def test_probe_prints_the_declaration_not_just_the_observed_keys(self):
        """探针必须按**线上的判据**给结论，而线上按通道声明判。

        SocialDataX 照常返回 is_pinned（所以「键在不在」看着一切正常），
        只是对真正置顶的那条也给 false。只打实测的话，探针会对它印出
        「✅ 这家报置顶」——正好是错的那个结论，然后有人照着它上线。
        """
        present_but_false = [
            {"content": "戳主页领券", "is_pinned": False, "is_author_comment": False,
             "like_count": 9, "author": {"name": "官号"}},
        ]
        with mock.patch.object(transport, "request",
                               return_value=self._sdx_page(present_but_false)):
            _ok, output = self._probe(providers.SOCIALDATAX, "s-key",
                                      self.SDX_XHS_LINK, Settings())
        # 声明：不可信
        self.assertIn("通道声明：❌ 不可信，线上不拿这家的值判置顶", output)
        self.assertNotIn("✅ 可信，线上照常判置顶", output)
        # 实测：键确实在，值是 false——证据照样要打出来
        self.assertIn("响应实测：is_pinned 1/1 条带这个键（其中 True 0 条）", output)
        # 结论那一段要说清「不可信」和「不报」是两种原因
        self.assertIn("字段在、值是错的", output)

    def test_probe_falls_back_to_observed_keys_without_a_declaration(self):
        """没有声明时退回看键在不在（老行为，给还没登记的通道兜底）。"""
        no_flags = [
            {"content": "戳主页领券", "like_count": 9, "author": {"name": "官号"}},
            {"content": "路过", "like_count": 0, "author": {"name": "路人"}},
        ]
        page = self._sdx_page(no_flags)
        with mock.patch.dict(providers.XHS_COMMENT_CAPABILITIES, clear=True), \
             mock.patch.object(transport, "request", return_value=page):
            _ok, output = self._probe(providers.SOCIALDATAX, "s-key",
                                      self.SDX_XHS_LINK, Settings())
        self.assertIn("❌ 这家不报置顶", output)
        self.assertIn("0/2 条带这个键", output)
        # 「置顶评论」那一行也要说清这不等于「没置顶」
        self.assertIn("不等于没置顶", output)

    def test_probe_confirms_a_trusted_channel(self):
        """对照组：声明可信的通道照常印 ✅，别把正常链路也吓成红的。"""
        items = [{"content": "戳主页领券", "is_pinned": True, "is_author_comment": True,
                  "like_count": 9, "author": {"name": "官号"}}]
        page = self._sdx_page(items)
        with mock.patch.dict(
                providers.XHS_COMMENT_CAPABILITIES,
                {providers.SOCIALDATAX: {"pinned": True, "author": True}}), \
             mock.patch.object(transport, "request", return_value=page):
            _ok, output = self._probe(providers.SOCIALDATAX, "s-key",
                                      self.SDX_XHS_LINK, Settings())
        self.assertIn("✅ 可信，线上照常判置顶", output)
        self.assertIn("响应实测：is_pinned 1/1 条带这个键（其中 True 1 条）", output)

    def test_raw_keeps_these_two_keys_even_when_false(self):
        """去空值那一步**不能**吃掉这两个键的 false——吃掉之后，
        「没返回」和「返回了 false」在屏幕上长得一模一样，
        而分清这两件事正是这个脚本存在的一大半理由。"""
        items = [{"content": "路过", "is_pinned": False, "is_author_comment": False,
                  "like_count": 0, "reply_count": 0, "author": {"name": "路人"}}]
        with mock.patch.object(transport, "request", return_value=self._sdx_page(items)):
            _ok, output = self._probe(providers.SOCIALDATAX, "s-key",
                                      self.SDX_XHS_LINK, Settings(), raw=True)
        self.assertIn('"is_pinned": false', output)
        self.assertIn('"is_author_comment": false', output)
        # 其余的 0 照旧去掉——只有这两个键的 false 是有信息的
        self.assertNotIn('"reply_count"', output)

    XHS_LINK = "https://www.xiaohongshu.com/explore/" + "a" * 24
    XHS_COMMENTS = transport.Response(200, "application/json", json.dumps({
        "code": 200, "data": {"data": {
            "user_id": "u1", "all_sort_strategies": ["default"],
            "comment_count": 3, "comment_count_l1": 2,
            "comments": [{"content": "好看", "user": {"nickname": "甲"}}]}}}), "r1")

    def test_detail_flag_calls_the_detail_endpoint_and_dumps_the_note(self):
        """--detail：线上默认不调小红书详情，探针显式要了才调，并把笔记本体的
        原始字段打出来——验「分享链接打不开、评论接口却正常」的笔记详情接口
        怎么说，全靠它。"""
        detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"data": [{"note_list": [{
                "id": "n1", "in_censor": True, "liked_count": 12, "comments_count": 3,
                "note_status": 2, "images_list": [{"url": "https://x/big.jpg"}],
                "user": {"nickname": "作者", "userid": "u1"}}]}]}}), "r2")
        settings = Settings()
        settings.detail_within_days = 3650
        with mock.patch.object(transport, "request",
                               side_effect=[self.XHS_COMMENTS, detail]) as sent:
            _ok, output = self._probe(providers.TIKHUB, "t-key", self.XHS_LINK,
                                      settings, raw=True)
        self.assertEqual(sent.call_count, 2)
        self.assertIn("详情：✅", output)
        self.assertIn("上游审核标记 True", output)
        self.assertIn('"in_censor": true', output)          # 状态字段原样保留
        self.assertIn('"note_status": 2', output)
        self.assertNotIn("images_list", output)              # 媒体杂项去掉
        self.assertNotIn('"userid"', output)                 # 作者只留昵称

    def test_without_detail_flag_xhs_probe_sends_one_request(self):
        with mock.patch.object(transport, "request", return_value=self.XHS_COMMENTS) as sent:
            self._probe(providers.TIKHUB, "t-key", self.XHS_LINK, Settings(), raw=True)
        self.assertEqual(sent.call_count, 1)

    def test_detail_gone_still_shows_the_raw_envelope(self):
        """笔记「仅作者可见」时详情接口可能直接回空（GONE）——那时候信封里的
        code / msg 才是要看的东西，失败也要打出来。"""
        detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "msg": "note not found", "data": {"data": []}}), "r2")
        settings = Settings()
        settings.detail_within_days = 3650
        with mock.patch.object(transport, "request", side_effect=[self.XHS_COMMENTS, detail]):
            _ok, output = self._probe(providers.TIKHUB, "t-key", self.XHS_LINK,
                                      settings, raw=True)
        self.assertIn("详情：❌ [gone]", output)
        self.assertIn('"msg": "note not found"', output)

    def test_force_actually_sends_the_request(self):
        """--force 是给「我就是要验一次」准备的，行为要如实。"""
        response = transport.Response(401, "application/json",
                                      '{"detail": {"code": 401, "message": "no"}}', "r")
        with self._expand(self.UNREACHABLE), \
             mock.patch.object(transport, "request", return_value=response) as sent:
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
    # +1：档位和归档都按**完整天数**判（interval_hours_for_age 里的 floor，
    # 和飞书 DATEDIF 同口径），所以 archive=30 的帖子在第 30 天当天照样在刷，
    # 满 31 天才停。仿真的上界要跟着走，否则它自己就少数了一天。
    archive = settings.refresh.archive_after_days + 1
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
        """旧模型会把整个 8–30 天档算进来，成本凭空多算一倍多。

        端点是 15 而不是 14：归档按完整天数判，第 14 天当天还在刷，满 15 天才停。
        """
        settings = Settings()
        settings.refresh.archive_after_days = 14
        segments = estimate_cost.tier_segments(settings)
        self.assertTrue(all(end <= 15 for _, end, _ in segments))
        self.assertEqual(max(end for _, end, _ in segments), 15)

    def test_archive_longer_than_tiers_extends_with_the_last_interval(self):
        settings = Settings()
        settings.refresh.archive_after_days = 45
        segments = estimate_cost.tier_segments(settings)
        self.assertEqual(max(end for _, end, _ in segments), 46)
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

    def test_standalone_estimator_honours_the_same_overrides(self):
        """`tools/estimate_cost.py` 是做**月度成本决策**的脚本。

        它如果不读同一份覆盖，运维改完价格之后就会出现：生产预算用新价、
        成本规划脚本还在报编译进代码的旧价，两边数字长期对不上。
        """
        providers.set_pricing(socialdatax_yuan=0.25)
        result = estimate_cost.estimate(20, 0.7, Settings())
        self.assertAlmostEqual(result["yuan_per_day"],
                               result["calls_per_day"] * 0.25, places=6)

    def test_estimator_respects_the_detail_window_override(self):
        """生产设了 DETAIL_WITHIN_DAYS=0（关掉小红书 detail）时，这个脚本
        如果还按编译进代码的 7 天窗口算，会把每一次合格的小红书刷新都多算
        一个付费调用——对一个用来做月度成本决策的脚本，这是致命的。"""
        import os

        saved = os.environ.get("DETAIL_WITHIN_DAYS")
        try:
            os.environ["DETAIL_WITHIN_DAYS"] = "0"
            settings = cli.build_settings()
            self.assertEqual(settings.detail_within_days, 0)
            lean = estimate_cost.estimate(20, 0.7, settings)

            os.environ["DETAIL_WITHIN_DAYS"] = "7"
            full = estimate_cost.estimate(20, 0.7, cli.build_settings())
            self.assertLess(lean["calls_per_day"], full["calls_per_day"],
                            "关掉 detail 之后调用数必须真的少下来")
        finally:
            if saved is None:
                os.environ.pop("DETAIL_WITHIN_DAYS", None)
            else:
                os.environ["DETAIL_WITHIN_DAYS"] = saved

    def test_estimator_entry_point_applies_env_overrides(self):
        import os

        saved = os.environ.get("SOCIALDATAX_YUAN")
        os.environ["SOCIALDATAX_YUAN"] = "0.33"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                estimate_cost.main.__globals__["cli"].apply_pricing_overrides()
            self.assertAlmostEqual(providers.SOCIALDATAX_YUAN, 0.33, places=6)
        finally:
            if saved is None:
                os.environ.pop("SOCIALDATAX_YUAN", None)
            else:
                os.environ["SOCIALDATAX_YUAN"] = saved


if __name__ == "__main__":
    unittest.main()
