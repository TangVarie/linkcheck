"""双通道降级：主通道倒下时，备胎顶上而不是整批停摆。

这一组测的是双通道**唯一真正值钱的地方**。省钱只是顺带的——一家挂了另一家
接着跑，才是把「半夜 Key 失效 = 一整晚的监控全丢」这个故障模式消掉。

关键的反面用例也在这里：**「笔记没了」绝不能触发降级**。那是行级结论不是
通道故障，换一家只会再花一次钱得到同一个答案。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import runner, transport
from xhsearch.config import Channels, Settings
from xhsearch.rows import Row

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
KEYS = {"tikhub": "t-key", "socialdatax": "s-key"}


def tikhub_ok_comments(count=150) -> transport.Response:
    comments = [] if count == 0 else [
        {"content": "戳主页", "like_count": 9, "ip_location": "Shanghai",
         "show_tags_v2": [{"type": "user_top"}, {"type": "is_author"}],
         "user": {"nickname": "官号"}}
    ]
    return transport.Response(200, "application/json", json.dumps({
        "code": 200, "request_id": "th-1", "data": {
            "code": 0, "success": True, "data": {
                "comments": comments,
                "comment_count": count, "comment_count_l1": count,
                "all_sort_strategies": [{"type": "default"}], "user_id": "u9",
            },
        },
    }, ensure_ascii=False), "th-1")


def tikhub_ok_detail() -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "code": 200, "data": {"code": 0, "success": True, "data": [{"note_list": [{
            "liked_count": 500, "collected_count": 60, "shared_count": 7,
            "comments_count": 150, "in_censor": False}]}]},
    }), "th-2")


def tikhub_err(status: int, message: str) -> transport.Response:
    return transport.Response(status, "application/json", json.dumps(
        {"detail": {"code": status, "message_zh": message, "request_id": "th-e"}},
        ensure_ascii=False), "th-e")


def sdx_ok_comments(count=150) -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "items": [{"content": "戳主页", "like_count": 9, "is_pinned": True,
                   "is_author_comment": True, "ip_location": "上海",
                   "author": {"name": "官号"}}],
        "comment_count": count, "top_level_comment_count": count,
        "points": {"cost": 10, "balance": 8000},
    }, ensure_ascii=False), "sdx-1")


def sdx_ok_detail() -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "like_count": 500, "collect_count": 60, "comment_count": 150,
        "points": {"cost": 10, "balance": 7990},
    }), "sdx-2")


def sdx_err(status: int, code: int, message: str) -> transport.Response:
    return transport.Response(status, "application/json",
                              json.dumps({"code": code, "message": message}, ensure_ascii=False),
                              "sdx-e")


def xhs_row(record_id="rec1", *, age_days=1.0) -> Row:
    return Row(
        record_id=record_id,
        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
        publish_time_ms=int((NOW - timedelta(days=age_days)).timestamp() * 1000),
    )


class FailoverTest(unittest.TestCase):
    """把两家的传输层分开顶掉：GET 一定是 TikHub，POST 一定是 SocialDataX。"""

    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1
        self.settings.soft_deadline_seconds = 0
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def run_with(self, *, on_get, on_post, rows=None, keys=None, **kwargs):
        def _get(url, headers, timeout=30.0):
            self.get_calls.append(url)
            return on_get(len(self.get_calls) - 1)

        def _post(url, headers, body, timeout=30.0):
            self.post_calls.append(url)
            return on_post(len(self.post_calls) - 1)

        with mock.patch.object(transport, "get", side_effect=_get), \
             mock.patch.object(transport, "post", side_effect=_post), \
             mock.patch("time.sleep"):
            return runner.refresh(rows or [xhs_row()], keys or KEYS,
                                  self.settings, now=NOW, **kwargs)


class TestHappyPathUsesPrimary(FailoverTest):
    def test_only_tikhub_is_called_when_it_works(self):
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(), tikhub_ok_detail()][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(self.post_calls, [])
        self.assertEqual(report.used_providers, {"tikhub": 2})
        self.assertEqual(report.failovers, 0)

    def test_cost_is_counted_at_the_channel_actually_used(self):
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(), tikhub_ok_detail()][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        # 小红书 2 次 × $0.01 × 7.2 ≈ ¥0.144，明显低于 SocialDataX 的 ¥0.20
        self.assertLess(report.cost_yuan, 0.20)
        self.assertGreater(report.cost_yuan, 0.0)


class TestFailover(FailoverTest):
    def test_transport_failure_falls_back(self):
        report = self.run_with(
            on_get=lambda i: transport.Response(0, "", "网络错误：timed out"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 150)
        self.assertEqual(report.used_providers, {"socialdatax": 2})
        self.assertEqual(report.failovers, 2)
        self.assertFalse(report.fatal)

    def test_dead_key_on_primary_is_survivable(self):
        """主通道 Key 失效，在单通道时代等于整批停摆。现在只是换一家继续。"""
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertFalse(report.fatal)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_dead_primary_is_tried_once_not_once_per_row(self):
        """600 行各撞一次 401 既慢又会把限流打出来，所以要把它整轮拉黑。"""
        rows = [xhs_row(f"rec{i}") for i in range(5)]
        self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: sdx_ok_comments() if i % 2 == 0 else sdx_ok_detail(),
            rows=rows,
        )
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual(len(self.post_calls), 10)   # 5 行 × （评论 + detail）

    def test_quota_exhausted_on_primary_falls_back(self):
        report = self.run_with(
            on_get=lambda i: tikhub_err(403, "账户余额不足，请充值。"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertFalse(report.fatal)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_both_channels_down_is_finally_fatal(self):
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: sdx_err(401, 1401, "API Key 无效或已失效。"),
        )
        self.assertTrue(report.fatal)


class TestNoPointlessFailover(FailoverTest):
    def test_gone_does_not_burn_a_second_channel(self):
        """「笔记没了」是行级结论。换一家只会再花一次钱换来同一个答案。"""
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "comment_count_l1": 0,
                "all_sort_strategies": [], "user_id": "",
            }},
        }), "th-gone")
        report = self.run_with(
            on_get=lambda i: empty_shell,
            on_post=lambda i: self.fail("GONE 不该触发降级"),
        )
        self.assertEqual(self.post_calls, [])
        # 第一次不定罪，只记一笔——两击定罪没有因为双通道被绕过。
        self.assertEqual(report.outcomes[0].status, runner.STATUS_SUSPECT)
        self.assertEqual(report.failovers, 0)

    def test_a_billed_failure_still_shows_up_on_the_bill(self):
        """TikHub 对不存在的笔记「正常响应、正常计费」，只是 data 是空壳。

        只记成功的调用，账面就会比账单少——少报成本的监控系统没有价值。
        """
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "comment_count_l1": 0,
                "all_sort_strategies": [], "user_id": "",
            }},
        }), "th-gone")
        report = self.run_with(
            on_get=lambda i: empty_shell,
            on_post=lambda i: self.fail("GONE 不该触发降级"),
        )
        self.assertGreater(report.cost_yuan, 0.0)
        self.assertEqual(report.used_providers, {"tikhub": 1})

    def test_an_unbilled_failure_does_not_show_up_on_the_bill(self):
        """401 根本没进业务层，对方明说不计费——算进去就是虚报。"""
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertNotIn("tikhub", report.used_providers)
        self.assertEqual(report.used_providers, {"socialdatax": 2})

    def test_definitive_death_from_detail_convicts_in_one_round(self):
        """detail 返回空 data 是干净的死亡信号，不必等第二次。"""
        row = xhs_row()
        row.consecutive_failures = 0
        dead_detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": []},
        }), "th-dead")
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(count=0), dead_detail][i],
            on_post=lambda i: self.fail("GONE 不该触发降级"),
            rows=[row],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)

    def test_live_note_is_not_killed_by_a_contradictory_detail(self):
        """评论接口刚拿回 150 条评论、detail 却说笔记不存在 —— 那是上游自相矛盾。

        这时候信 detail 就是拿一次抖动杀掉一条好帖子，正是这个项目最不能犯的错。
        """
        dead_detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": []},
        }), "th-dead")
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(count=150), dead_detail][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 150)


class TestSingleChannelIsUnchanged(FailoverTest):
    def test_bare_string_key_behaves_exactly_as_before(self):
        """老调用方传裸字符串：只打 SocialDataX，一次 GET 都不该发出去。"""
        report = self.run_with(
            on_get=lambda i: self.fail("裸 key 不该去打 TikHub"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            keys="only-sdx",
        )
        self.assertEqual(self.get_calls, [])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_channel_can_be_pinned_to_one_vendor(self):
        self.settings.channels = Channels(order={"xhs": ["socialdatax"]})
        self.run_with(
            on_get=lambda i: self.fail("已经指定只走 SocialDataX"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertEqual(self.get_calls, [])

    def test_missing_key_for_primary_silently_uses_the_spare(self):
        report = self.run_with(
            on_get=lambda i: self.fail("没配 TikHub 的 key 就不该去打它"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            keys={"socialdatax": "s-key"},
        )
        self.assertEqual(self.get_calls, [])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)


class TestSecrets(FailoverTest):
    def test_api_key_never_reaches_the_table(self):
        """TikHub 的报错会回显 Key，而报错文案是要写进飞书诊断列的。"""
        leaky = transport.Response(401, "application/json", json.dumps({
            "detail": {"code": 401, "message_zh": "无效的API令牌，您提交的API令牌为 t-key。"},
        }, ensure_ascii=False), "th-e")
        report = self.run_with(
            on_get=lambda i: leaky,
            on_post=lambda i: sdx_err(500, 1005, "服务暂时不可用"),
        )
        blob = json.dumps([o.fields for o in report.outcomes], ensure_ascii=False)
        self.assertNotIn("t-key", blob)
        self.assertNotIn("t-key", report.summary())

    def test_redaction_actually_fires_without_a_backup_channel(self):
        """单通道版本：泄漏文案没有备胎可以顶替，必然要落进报告——
        这才是真正逼 _redact 出手的场景（双通道版本里 TikHub 的文案
        会被 SDX 的结果顶掉，脱敏根本没被触发也能过）。

        Key 要用真实长度：_redact 对 6 个字符以下的 key 不做替换。
        """
        secret = "tikhub-secret-key-1234567890"
        leaky = transport.Response(401, "application/json", json.dumps({
            "detail": {"code": 401,
                       "message_zh": f"无效的API令牌，您提交的API令牌为 {secret}。"},
        }, ensure_ascii=False), "th-e")
        report = self.run_with(
            on_get=lambda i: leaky,
            on_post=lambda i: self.fail("只配了 TikHub，不该打到 SDX"),
            keys={"tikhub": secret},
        )
        # 单通道 AUTH = 整批中止；中止原因也是人会看到的文本，同样不能带 Key。
        self.assertTrue(report.fatal)
        self.assertNotIn(secret, report.aborted_reason)
        self.assertIn("***", report.aborted_reason)


class TestDouyinLinkOnlyRows(FailoverTest):
    """抖音短链（提不出数字 ID）的行。

    TikHub 的抖音端点只收 aweme_id、不收链接——图文和视频共用同一对端点，
    形态无关，问题只在参数形状。这种行必须直接让给吃链接的 SocialDataX，
    而不是把 URL 塞进 aweme_id 白花一次钱、再被空响应骗成「已失效」。
    """

    def _short_link_row(self):
        return Row(
            record_id="dy-url",
            link_cell="7.86 复制打开抖音，看看作品 https://v.douyin.com/iRxYzAb/",
            publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000),
        )

    def _sdx_douyin(self, i):
        if i % 2 == 0:
            return transport.Response(200, "application/json", json.dumps({
                "items": [{"content": "好", "like_count": 1, "ip_location": "上海",
                           "author": {"name": "路人"}}],
                "comment_count": 5, "points": {"cost": 10, "balance": 100},
            }, ensure_ascii=False), "sdx-dy")
        return transport.Response(200, "application/json", json.dumps({
            "like_count": 10, "comment_count": 5, "points": {"cost": 10, "balance": 90},
        }), "sdx-dy2")

    def test_short_link_goes_straight_to_socialdatax(self):
        report = self.run_with(
            on_get=lambda i: self.fail("TikHub 不吃链接，就不该打它"),
            on_post=self._sdx_douyin,
            rows=[self._short_link_row()],
        )
        self.assertEqual(self.get_calls, [])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.used_providers, {"socialdatax": 2})
        # 这是能力路由，不是主通道故障，不该算进「降级」指标——
        # 运营手册教人看到降级不为 0 就去查主通道。
        self.assertEqual(report.failovers, 0)

    def test_tikhub_only_deployment_skips_the_row_cleanly(self):
        """只配了 TikHub：这行没法刷，但要败得明白——
        按「跳过」处理（和坏链接同类）并说清原因：不烧钱、不中止整批、
        不碰定罪计数，也不进熔断的失效比例分母。"""
        report = self.run_with(
            on_get=lambda i: self.fail("不该发出任何请求"),
            on_post=lambda i: self.fail("没配 SDX 的 key"),
            rows=[self._short_link_row()],
            keys={"tikhub": "t-key"},
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_SKIPPED)
        self.assertFalse(report.fatal)
        self.assertEqual(report.cost_yuan, 0.0)
        self.assertIn("不支持这种链接形态", outcome.fields[self.settings.fields.failure_reason])
        self.assertNotIn(self.settings.fields.consecutive_failures, outcome.fields)
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)

    def test_full_video_link_still_uses_tikhub(self):
        """带数字 ID 的完整链接不受影响，照走便宜的主通道。"""
        row = Row(record_id="dy-id",
                  link_cell="https://www.douyin.com/video/7123456789012345678",
                  publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))
        tik_comments = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"comments": [], "total": 5}}), "th-dy")
        tik_detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"aweme_detail": {
                "statistics": {"digg_count": 10, "comment_count": 5}}}}), "th-dy2")
        report = self.run_with(
            on_get=lambda i: [tik_comments, tik_detail][i],
            on_post=lambda i: self.fail("不该降级"),
            rows=[row],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.used_providers, {"tikhub": 2})


class TestFailoverMetricHonesty(FailoverTest):
    def test_running_on_the_only_configured_channel_is_not_a_failover(self):
        """只配了备胎 key 的部署完全合法：每次成功调用都不是「降级」。

        报成降级的话，运营会按手册去查一个根本不存在的主通道故障。
        """
        report = self.run_with(
            on_get=lambda i: self.fail("没配 TikHub 的 key"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            keys={"socialdatax": "s-key"},
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.failovers, 0)
        self.assertNotIn("降级", report.summary())

    def test_already_disabled_primary_is_not_counted_as_a_failover(self):
        """COR-008：主通道在本轮早些时候已经被判死之后，后面每一行都直接
        走备胎——那不是「这一行发生了降级」。

        漏掉这条，降级次数会被持续高估，而降级率恰恰是判断主通道健康度的
        那个指标：运营会照着一个虚高的数字去查一个已经查过的故障。
        """
        report = self.run_with(
            on_get=lambda i: self.fail("TikHub 已被判死，不该再打它"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            disabled={"tikhub"},
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.failovers, 0)

    def test_first_failure_of_the_run_still_counts_as_a_failover(self):
        """反面：主通道是在这一行**当场**倒下的，那就是真的降级，必须记。"""
        report = self.run_with(
            on_get=lambda i: tikhub_err(500, "服务暂时不可用"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertGreaterEqual(report.failovers, 1)


class TestPlatformScopedAbort(FailoverTest):
    """COR-004：一个平台的通道全倒，不该让另一个平台的健康行也停摆。

    场景来自审计报告：xhs 只配了一条已失效的 TikHub，douyin 配着健康的
    SocialDataX。整批级中止会让抖音那些本来能跑完的行全部顺延——
    一个平台的配置故障拖停另一个平台，丢掉本可完成的监控。
    """

    def _douyin_row(self, record_id="dy1") -> Row:
        return Row(record_id=record_id,
                   link_cell="https://www.douyin.com/video/7412345678901234567",
                   publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))

    def test_dead_xhs_channel_does_not_stop_healthy_douyin_rows(self):
        self.settings.channels = Channels(order={"xhs": ["tikhub"],
                                                 "douyin": ["socialdatax"]})
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: [sdx_ok_comments(count=12), sdx_ok_detail()][i],
            rows=[xhs_row("x1"), self._douyin_row("d1")],
        )
        by_id = {o.record_id: o for o in report.outcomes}
        self.assertEqual(by_id["x1"].status, runner.STATUS_DEFERRED)
        self.assertEqual(by_id["d1"].status, runner.STATUS_OK,
                         "抖音有健康通道，不该被小红书的配置故障拖停")
        self.assertIn("xhs", report.dead_platforms)
        self.assertNotIn("douyin", report.dead_platforms)

    def test_all_platforms_dead_still_reports_fatal(self):
        self.settings.channels = Channels(order={"xhs": ["tikhub"],
                                                 "douyin": ["tikhub"]})
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: self.fail("没配 douyin 的备胎"),
            rows=[xhs_row("x1"), self._douyin_row("d1")],
            keys={"tikhub": "t-key"},
        )
        self.assertTrue(report.fatal)
        for outcome in report.outcomes:
            self.assertEqual(outcome.status, runner.STATUS_DEFERRED)
            self.assertEqual(outcome.fields, {})


class TestSocialDataXShapeIsFailClosed(FailoverTest):
    """COR-005：成功响应的形状不符时必须**失败关闭**。

    原来是失败开放：任何 HTTP<400、能解析成 dict、又没有 code 字段的 body
    都算成功。网关兜底返回 `{"message":"gateway fallback"}` 时，这一行会被
    记成巡查正常——死亡计数清零、最近检查时间推进、排队勾清掉，
    表上一切正常，实际这一轮什么都没量到。
    """

    def test_message_only_body_is_not_success(self):
        gateway = transport.Response(
            200, "application/json", json.dumps({"message": "gateway fallback"}), "gw-1")
        report = self.run_with(
            on_get=lambda i: self.fail("只配了 SocialDataX"),
            on_post=lambda i: gateway,
            keys={"socialdatax": "s-key"},
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertIn("形状不对", outcome.reason)
        # 关键：不能推进成功路径的那几个副作用
        f = self.settings.fields
        self.assertNotIn(f.consecutive_failures, outcome.fields)
        self.assertNotIn(f.alive_confirmed, outcome.fields)

    def test_empty_object_is_not_success(self):
        empty = transport.Response(200, "application/json", "{}", "gw-2")
        report = self.run_with(
            on_get=lambda i: self.fail("只配了 SocialDataX"),
            on_post=lambda i: empty,
            keys={"socialdatax": "s-key"},
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_FAILED)

    def test_zero_comment_page_is_still_a_valid_success(self):
        """空 items 是合法的成功（真的零评论），不能被这道闸误伤。"""
        page = transport.Response(200, "application/json", json.dumps({
            "items": [], "comment_count": 0, "top_level_comment_count": 0,
            "points": {"cost": 10, "balance": 10}}), "sdx-0")
        report = self.run_with(
            on_get=lambda i: self.fail("只配了 SocialDataX"),
            on_post=lambda i: [page, sdx_ok_detail()][i],
            keys={"socialdatax": "s-key"},
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)


if __name__ == "__main__":
    unittest.main()
