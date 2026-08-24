"""编排层的端到端测试。用假传输层顶掉网络，不需要任何 Key。

这一组测的是最容易出事、也最难在生产上发现的行为：失败时不要写坏表。
"""

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import protocol, runner, transport
from xhsearch.config import Settings
from xhsearch.rows import Row

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def sse(payload: dict) -> transport.Response:
    """成功响应。REST 接口返回纯 JSON，成功时不带 code 字段。

    （函数名沿用 sse 只是历史习惯，实际是普通 JSON。）
    """
    return transport.Response(200, "application/json", json.dumps(payload), "req-ok")


def err(status: int, code: int, message: str, **extra) -> transport.Response:
    """错误响应。注意业务错误的 HTTP 状态码是 200，靠 body 里的 code 区分。"""
    return transport.Response(
        status, "application/json", json.dumps({"code": code, "message": message, **extra}), "req-err"
    )


def comment_page(count=50, pinned=True, balance=9000):
    items = []
    if pinned:
        items.append({"content": "戳主页领券", "like_count": 10, "is_pinned": True,
                      "is_author_comment": True, "ip_location": "上海", "author": {"name": "官号"}})
    items.append({"content": "好用！", "like_count": 3, "is_pinned": False,
                  "is_author_comment": False, "ip_location": "广东", "author": {"name": "路人"}})
    return {"items": items, "comment_count": count, "top_level_comment_count": count,
            "next_page_token": "", "points": {"cost": 10, "balance": balance}}


def xhs_row(record_id="rec1", *, age_days=1.0, tags=None, prev_count=None, keywords=None):
    published = NOW - timedelta(days=age_days)
    return Row(
        record_id=record_id,
        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
        publish_time_ms=int(published.timestamp() * 1000),
        current_tags=tags or [],
        previous_comment_count=prev_count,
        seed_keywords=keywords or [],
    )


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1     # 让断言顺序可预期
        self.settings.soft_deadline_seconds = 0

    def run_with(self, responses, rows, **kwargs):
        """responses: 按调用顺序返回的响应列表，或一个 callable(url, headers, body)。"""
        if callable(responses):
            side_effect = responses
        else:
            queue = list(responses)
            side_effect = lambda *a, **k: queue.pop(0) if queue else err(500, 1005, "测试响应队列已耗尽")
        # 退避是真的会 sleep，测试里一律掐掉，否则重试用例会让整套跑十几秒。
        with mock.patch.object(transport, "post", side_effect=side_effect), \
             mock.patch("time.sleep"):
            return runner.refresh(rows, "fake-key", self.settings, now=NOW, **kwargs)


class TestHappyPath(RunnerTest):
    def test_writes_all_expected_columns(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 8000, "collect_count": 900,
                                                "comment_count": 150, "points": {"cost": 10, "balance": 8990}})],
            [xhs_row()],
        )
        self.assertEqual(report.counts(), {runner.STATUS_OK: 1})
        fields = report.outcomes[0].fields
        f = self.settings.fields
        self.assertEqual(fields[f.comment_count], 150)
        self.assertEqual(fields[f.like_count], 8000)
        self.assertEqual(fields[f.collect_count], 900)
        self.assertEqual(fields[f.pinned_status], self.settings.pin_status.pinned_ok)
        self.assertIn("1. [置顶", fields[f.comment_digest])
        self.assertIn("戳主页领券", fields[f.comment_digest])   # 置顶内容在快照里看
        self.assertEqual(fields[f.platform], "小红书")
        self.assertIn("大爆", fields[f.traffic_status])
        self.assertEqual(report.credits, 20)
        self.assertEqual(report.points_balance, 8990)

    def test_credits_reported_in_yuan(self):
        report = self.run_with([sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
                               [xhs_row()])
        self.assertIn("¥0.20", report.summary())


class TestHumanTagsAreNeverClobbered(RunnerTest):
    def test_manual_tags_survive_machine_write(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["已复盘", "客户确认", "风控中"])],
        )
        final = report.outcomes[0].fields[self.settings.fields.traffic_status]
        self.assertIn("已复盘", final)
        self.assertIn("客户确认", final)
        self.assertIn("大爆", final)
        self.assertNotIn("风控中", final)   # 机器标签可撤回

    def test_unknown_option_is_filtered_out(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
            known_options=["风控中", "已失效"],       # 表里没建热度档位的选项
        )
        fields = report.outcomes[0].fields
        # 过滤后无标签可写 → 该字段根本不进 payload，而不是写个空数组把人工标签抹掉
        self.assertNotIn(self.settings.fields.traffic_status, fields)
        self.assertIn("还没建选项", fields[self.settings.fields.failure_reason])

    def test_retired_gone_tag_is_cleaned_up(self):
        """旧版机器会打「已失效」，这个标签已退役（失效并入「风控中」）——
        行上的残留旧值要在下一轮成功刷新时被自动摘掉，不能永远挂着。"""
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["已失效", "已复盘"])],
        )
        final = report.outcomes[0].fields[self.settings.fields.traffic_status]
        self.assertNotIn("已失效", final)
        self.assertIn("已复盘", final)
        self.assertIn("大爆", final)

    def test_no_tag_change_means_no_write(self):
        """无变化不写：省一次写、避开选项冲突，也不让人看到这行被反复改动。"""
        report = self.run_with(
            [sse(comment_page(count=10)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["已复盘"])],
        )
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)

    def test_heat_tier_never_downgrades(self):
        """爆过就是爆过。评论被删导致数字掉下去，不该从大爆退回评估中。"""
        report = self.run_with(
            [sse(comment_page(count=25)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["大爆"], prev_count=25)],
        )
        fields = report.outcomes[0].fields
        # 没变化就不写；关键是没被降档
        if self.settings.fields.traffic_status in fields:
            self.assertIn("大爆", fields[self.settings.fields.traffic_status])


class TestDeadPostDetection(RunnerTest):
    def test_first_strike_does_not_convict(self):
        """两击定罪。一次抖动/一次上游话术变化就判死，会把整张表刷红。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")], [xhs_row(tags=["已复盘"])])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_SUSPECT)
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)  # 一个标签都不打
        self.assertEqual(outcome.fields[self.settings.fields.consecutive_failures], 1)
        self.assertIn("第 1 次", outcome.fields[self.settings.fields.failure_reason])

    def test_first_strike_does_not_strip_existing_risk_tag(self):
        """上一轮判过风控，这一轮取不到 —— 不能因此把风控摘掉。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")],
                               [xhs_row(tags=["风控中", "已复盘"])])
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)

    def test_second_strike_convicts(self):
        row = xhs_row(tags=["已复盘"])
        row.consecutive_failures = 1
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_GONE)
        final = outcome.fields[self.settings.fields.traffic_status]
        self.assertIn("风控中", final)     # 链接失效 = 风控中的硬证据
        self.assertNotIn("已失效", final)  # 该标签已退役，不再产出
        self.assertIn("已复盘", final)     # 人工标签照样保住
        self.assertIn("未找到对应内容", outcome.fields[self.settings.fields.failure_reason])

    def test_content_deleted_convicts_on_first_strike(self):
        """错误码 1008 是上游的权威结论（规范原文「不要重试」）——
        再等一轮只是让运营晚一天看到，没有任何收益。"""
        report = self.run_with([err(200, 1008, "当前作品已删除。")], [xhs_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_GONE)
        self.assertIn("风控中", outcome.fields[self.settings.fields.traffic_status])

    def test_surface_unavailable_is_not_treated_as_dead(self):
        """1007「页面暂时不可访问」是瞬时故障，不是内容没了。
        判错这条会把好帖子标成失效。"""
        report = self.run_with([err(200, 1007, "当前页面暂时不可访问")] * 3, [xhs_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)

    def test_request_id_is_written_into_the_table(self):
        """找厂商排查时唯一的凭据，运营截图就能给出去。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")], [xhs_row()])
        self.assertIn("req-err", report.outcomes[0].fields[self.settings.fields.failure_reason])

    def test_success_resets_the_strike_counter(self):
        row = xhs_row()
        row.consecutive_failures = 1
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})], [row])
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.consecutive_failures], 0)


class TestCircuitBreaker(RunnerTest):
    """几百条笔记不可能在同一小时里被集体删除。真出现这种批量失效，
    一定是上游挂了或错误话术改版了，宁可这轮什么都不写。"""

    def test_mass_failure_voids_all_tag_writes(self):
        rows = []
        for i in range(12):
            row = xhs_row(f"rec{i}")
            row.consecutive_failures = 1      # 都已经是第二击了，本该全部判死
            rows.append(row)
        report = self.run_with(lambda *a, **k: err(200, 1003, "未找到对应内容"), rows)

        self.assertTrue(report.breaker_tripped)
        for outcome in report.outcomes:
            self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
            self.assertEqual(outcome.status, runner.STATUS_FAILED)
            self.assertIn("疑似上游故障", outcome.fields[self.settings.fields.failure_reason])

    def test_small_batch_does_not_trip_breaker(self):
        row = xhs_row()
        row.consecutive_failures = 1
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)


class TestCooldown(RunnerTest):
    def test_recently_refreshed_row_is_skipped_free(self):
        row = xhs_row()
        row.last_updated_ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([row], "k", self.settings, now=NOW)
        posted.assert_not_called()
        self.assertEqual(report.outcomes[0].status, runner.STATUS_COOLDOWN)
        self.assertEqual(report.credits, 0)

    def test_forced_refresh_ignores_cooldown(self):
        row = xhs_row()
        row.last_updated_ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [row], forced=True)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_network_blip_does_NOT_tag_as_dead(self):
        # 这是最重要的一条：一次 5xx 不能把好帖子标成风控/失效
        blip = transport.Response(0, "", "网络错误：timed out")
        report = self.run_with([blip, blip, blip], [xhs_row(tags=["大爆"])])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        # 流量状态整列不写 —— 保留上一次的结论
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
        self.assertEqual(outcome.fields[self.settings.fields.refresh_status], runner.STATUS_FAILED)


class TestPartialFailure(RunnerTest):
    def test_detail_failure_still_writes_comment_data(self):
        # 评论拿到了，detail 挂了 —— 不该整行判失败
        report = self.run_with(
            [sse(comment_page(count=42)), err(500, 1005, "服务暂时不可用，请稍后重试")],
            [xhs_row()],
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertEqual(outcome.fields[self.settings.fields.comment_count], 42)
        self.assertNotIn(self.settings.fields.like_count, outcome.fields)

    def test_comments_failure_fails_the_row(self):
        report = self.run_with([err(500, 1005, "服务暂时不可用，请稍后重试")] * 3, [xhs_row()])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_FAILED)


class TestFatalErrorsStopTheBatch(RunnerTest):
    def test_invalid_key_aborts_but_keeps_finished_rows(self):
        calls = {"n": 0}

        def responder(url, headers, body, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return sse(comment_page()) if calls["n"] == 1 else sse(
                    {"like_count": 1, "points": {"cost": 10, "balance": 1}})
            return err(401, 1401, "API Key 无效或已失效。")

        report = self.run_with(responder, [xhs_row("rec1"), xhs_row("rec2"), xhs_row("rec3")])
        # 第一行的结果保住了；中止后的行产出「留待下一轮」的空结果，**不写回**——
        # 它们的最后更新时间保持原样，Key 修好后下一轮自然重捞。
        written = [o for o in report.outcomes if o.fields]
        self.assertEqual([o.record_id for o in written], ["rec1"])
        self.assertEqual(written[0].status, runner.STATUS_OK)
        deferred = [o for o in report.outcomes if o.status == runner.STATUS_DEFERRED]
        self.assertEqual({o.record_id for o in deferred}, {"rec2", "rec3"})
        self.assertIn("1401", report.aborted_reason)
        self.assertTrue(report.fatal)

    def test_insufficient_balance_aborts(self):
        report = self.run_with([err(200, 1004, "当前 API Key 积分不足。")], [xhs_row()])
        # 中止的行不写回任何字段，只在报告里留痕。
        self.assertEqual([o for o in report.outcomes if o.fields], [])
        self.assertIn("积分不足", report.aborted_reason)
        self.assertTrue(report.fatal)


class TestRateLimit(RunnerTest):
    def test_retries_once_then_succeeds(self):
        queue = [
            transport.Response(429, "application/json", json.dumps(
                {"error": "rate_limited", "error_description": "请求过于频繁", "retry_after": 0})),
            sse(comment_page(count=7)),
            sse({"like_count": 1, "points": {"cost": 10, "balance": 1}}),
        ]
        with mock.patch.object(transport, "post", side_effect=lambda *a, **k: queue.pop(0)), \
             mock.patch("time.sleep"):
            report = runner.refresh([xhs_row()], "k", self.settings, now=NOW)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 7)


class TestBadLink(RunnerTest):
    def test_unparseable_link_costs_nothing(self):
        row = Row(record_id="rec9", link_cell="待补链接")
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([row], "k", self.settings, now=NOW)
        posted.assert_not_called()
        self.assertEqual(report.outcomes[0].status, runner.STATUS_SKIPPED)
        self.assertEqual(report.credits, 0)

    def test_unparseable_link_does_not_strip_existing_tags(self):
        """链接识别不了 ≠ 帖子没事了。这一轮没获得任何新信息，就别碰流量状态。"""
        row = Row(record_id="rec9", link_cell="待补链接", current_tags=["风控中", "已复盘"])
        with mock.patch.object(transport, "post"):
            report = runner.refresh([row], "k", self.settings, now=NOW)
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)


class TestDouyin(RunnerTest):
    def _row(self):
        return Row(record_id="d1", link_cell="https://www.douyin.com/video/7123456789012345678",
                   publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))

    def test_null_comment_count_backfilled_from_detail(self):
        page = {"items": [{"content": "哈哈", "like_count": 2, "is_hot": True,
                           "ip_location": "浙江", "author": {"nickname": "路人"}}],
                "comment_count": None, "points": {"cost": 10, "balance": 100}}
        report = self.run_with(
            [sse(page), sse({"comment_count": 321, "like_count": 5000, "points": {"cost": 10, "balance": 90}})],
            [self._row()],
        )
        fields = report.outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_count], 321)
        self.assertEqual(fields[self.settings.fields.platform], "抖音")

    def test_douyin_never_touches_pin_status(self):
        """抖音评论接口没有置顶字段，判不了——「置顶状态」列绝不碰，
        写「无置顶」冒充结论会被运营当真。"""
        page = {"items": [], "comment_count": 5, "points": {"cost": 10, "balance": 1}}
        report = self.run_with([sse(page), sse({"comment_count": 5, "points": {"cost": 10, "balance": 1}})],
                               [self._row()])
        self.assertNotIn(self.settings.fields.pinned_status, report.outcomes[0].fields)

    def test_douyin_never_touches_comment_status(self):
        """没填关键词的抖音行判不了「显示没显示」，绝不能碰「评论状态」列。
        （填了关键词的抖音行现在能判——匹配的是第一页评论，不依赖置顶字段。）"""
        page = {"items": [], "comment_count": 5, "points": {"cost": 10, "balance": 1}}
        report = self.run_with([sse(page), sse({"comment_count": 5, "points": {"cost": 10, "balance": 1}})],
                               [self._row()])
        self.assertNotIn(self.settings.fields.comment_status, report.outcomes[0].fields)


class TestPinnedTracking(RunnerTest):
    """「置顶状态」「评论状态」都是单选，机器直接覆盖当前值。
    置顶三值的「掉了/从来没有」全看「置顶状态」列自己的历史。"""

    def _row(self, *, keywords=None, pin_status=""):
        row = xhs_row(keywords=keywords)
        row.pin_status = pin_status
        return row

    def _run(self, pinned, row, **kwargs):
        return self.run_with(
            [sse(comment_page(pinned=pinned)),
             sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [row], **kwargs,
        )

    def test_hit_writes_displayed(self):
        fields = self._run(True, self._row(keywords=["好用"])).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         self.settings.comment_status.displayed)

    def test_miss_writes_not_displayed(self):
        fields = self._run(True, self._row(keywords=["艾时达口溶膜"])).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         self.settings.comment_status.not_displayed)

    def test_no_keywords_leaves_comment_status_alone(self):
        """没填关键词的行判不了「显示没显示」，这一列绝不能碰。"""
        fields = self._run(False, self._row()).outcomes[0].fields
        self.assertNotIn(self.settings.fields.comment_status, fields)

    def test_pinned_writes_ok(self):
        fields = self._run(True, self._row()).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.pinned_status],
                         self.settings.pin_status.pinned_ok)

    def test_never_pinned(self):
        fields = self._run(False, self._row()).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.pinned_status],
                         self.settings.pin_status.never_pinned)

    def test_pin_fell_off_after_ok(self):
        """上一轮「置顶成功」、这一轮没置顶 → 「置顶掉了」，
        掉落那一轮诊断信息里额外报警。"""
        ps = self.settings.pin_status
        fields = self._run(False, self._row(pin_status=ps.pinned_ok)).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.pinned_status], ps.pinned_lost)
        self.assertIn("置顶已不在", fields[self.settings.fields.failure_reason])

    def test_lost_stays_lost(self):
        """掉了之后一直没恢复保持「置顶掉了」，不退回「无置顶」。"""
        ps = self.settings.pin_status
        fields = self._run(False, self._row(pin_status=ps.pinned_lost)).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.pinned_status], ps.pinned_lost)

    def test_unknown_option_is_skipped_with_note(self):
        """选项还没建：跳过这一列并在诊断里提示，别拖垮整行写回。"""
        report = self._run(True, self._row(keywords=["好用"]),
                           comment_status_options=["待评论"],
                           pin_status_options=["无置顶"])
        fields = report.outcomes[0].fields
        self.assertNotIn(self.settings.fields.comment_status, fields)
        self.assertNotIn(self.settings.fields.pinned_status, fields)
        self.assertIn("还没建选项", fields[self.settings.fields.failure_reason])


class TestFailurePathsLeaveStatusColumnsAlone(RunnerTest):
    """失败/存疑路径对「评论状态」「置顶状态」零发言权。

    一旦有人让失败路径也下「没有显示/无置顶」的结论，一次上游故障就会
    给全表批量覆盖真实状态，诱导运营无谓补评论/补置顶。
    """

    def _row(self, *, strikes=0):
        row = xhs_row(keywords=["艾时达口溶膜"])
        row.pin_status = self.settings.pin_status.pinned_ok
        row.consecutive_failures = strikes
        return row

    def assert_untouched(self, outcome):
        self.assertNotIn(self.settings.fields.comment_status, outcome.fields)
        self.assertNotIn(self.settings.fields.pinned_status, outcome.fields)

    def test_first_strike_suspect(self):
        report = self.run_with([err(200, 1003, "未找到对应内容")], [self._row()])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_SUSPECT)
        self.assert_untouched(report.outcomes[0])

    def test_convicted_gone(self):
        report = self.run_with([err(200, 1008, "当前作品已删除。")],
                               [self._row(strikes=1)])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)
        self.assert_untouched(report.outcomes[0])

    def test_network_failure(self):
        blip = transport.Response(0, "", "网络错误：timed out")
        report = self.run_with([blip] * 3, [self._row()])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_FAILED)
        self.assert_untouched(report.outcomes[0])


class TestSeedKeywordColumn(RunnerTest):
    """关键词命中不占单独一列：命中的那条评论排在「评论区快照」第一行
    并带「命中」标记；未命中由「评论状态=没有显示」+ 诊断信息表达。"""

    def test_hit_comment_leads_the_digest(self):
        page = comment_page(pinned=True)
        page["items"].append({"content": "朋友安利的西地那非口溶膜", "like_count": 2,
                              "is_pinned": False, "is_author_comment": False,
                              "ip_location": "上海", "author": {"name": "路人"}})
        report = self.run_with(
            [sse(page), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(keywords=["西地那非口溶膜", "cGMP因子"])],
        )
        digest = report.outcomes[0].fields[self.settings.fields.comment_digest]
        first = digest.splitlines()[0]
        self.assertTrue(first.startswith("1. [命中「西地那非口溶膜」"))
        self.assertIn("西地那非口溶膜", first)
        # 置顶那条紧随其后，没有因为命中置前而丢
        self.assertIn("置顶", digest.splitlines()[1])

    def test_miss_reported_via_status_and_notes(self):
        report = self.run_with(
            [sse(comment_page(pinned=False)),
             sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(keywords=["西地那非口溶膜"])],
        )
        fields = report.outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         self.settings.comment_status.not_displayed)
        self.assertIn("未命中", fields[self.settings.fields.failure_reason])
        # 快照保持普通排序，第一行不是命中标记
        self.assertNotIn("命中「", fields[self.settings.fields.comment_digest])

    def test_zero_comment_page_is_a_real_miss(self):
        """评论数确认为 0 的空页不是空壳：第一页真的什么都没有，
        「没有显示」是可靠结论，端到端也要落列。"""
        page = {"items": [], "comment_count": 0, "top_level_comment_count": 0,
                "next_page_token": "", "points": {"cost": 10, "balance": 1}}
        report = self.run_with(
            [sse(page), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(keywords=["艾时达口溶膜"])],
        )
        fields = report.outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         self.settings.comment_status.not_displayed)


class TestPreviousMetricsShift(RunnerTest):
    """点赞/收藏与评论数完全对称：写新值前先把现值搬进「上次」列。
    判定仍然只基于评论数，这两组只作参考。"""

    def test_previous_values_are_shifted(self):
        row = xhs_row(prev_count=40)
        row.previous_like_count = 500
        row.previous_collect_count = 60
        report = self.run_with(
            [sse(comment_page(count=50)),
             sse({"like_count": 800, "collect_count": 90,
                  "points": {"cost": 10, "balance": 1}})],
            [row],
        )
        f = self.settings.fields
        fields = report.outcomes[0].fields
        self.assertEqual(fields[f.previous_comment_count], 40)
        self.assertEqual(fields[f.comment_count], 50)
        self.assertEqual(fields[f.previous_like_count], 500)
        self.assertEqual(fields[f.like_count], 800)
        self.assertEqual(fields[f.previous_collect_count], 60)
        self.assertEqual(fields[f.collect_count], 90)

    def test_first_round_writes_only_current(self):
        report = self.run_with(
            [sse(comment_page(count=50)),
             sse({"like_count": 800, "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
        )
        f = self.settings.fields
        fields = report.outcomes[0].fields
        self.assertNotIn(f.previous_like_count, fields)
        self.assertEqual(fields[f.like_count], 800)


class TestAliveConfirmed(RunnerTest):
    def test_ok_ticks_the_box(self):
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
        )
        self.assertIs(report.outcomes[0].fields[self.settings.fields.alive_confirmed], True)

    def test_confirmed_gone_unticks(self):
        row = xhs_row()
        row.consecutive_failures = 1
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        self.assertIs(report.outcomes[0].fields[self.settings.fields.alive_confirmed], False)

    def test_suspect_leaves_box_alone(self):
        report = self.run_with([err(200, 1003, "未找到对应内容")], [xhs_row()])
        self.assertNotIn(self.settings.fields.alive_confirmed, report.outcomes[0].fields)

    def test_likes_only_round_ticks_the_box(self):
        """有点赞没评论的活帖：非零互动数就是存活证据。之前只认评论数，
        这种行明明点赞/收藏都写进表了，存活勾却一直空着。"""
        report = self.run_with(
            [sse({"items": [], "comment_count": 0, "points": {"cost": 10, "balance": 1}}),
             sse({"like_count": 500, "collect_count": 0, "comment_count": 0,
                  "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertIs(outcome.fields[self.settings.fields.alive_confirmed], True)

    def test_zero_engagement_round_still_ticks_the_box(self):
        """互动全为 0 但确实是「量到的」（不是 None）——本轮巡查成功本身
        就证明这篇内容还在，不需要互动数字非零才算存活。"""
        report = self.run_with(
            [sse({"items": [], "comment_count": 0, "points": {"cost": 10, "balance": 1}}),
             sse({"like_count": 0, "collect_count": 0, "comment_count": 0,
                  "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertIs(outcome.fields[self.settings.fields.alive_confirmed], True)

    def test_ok_round_without_evidence_leaves_box_alone(self):
        """评论页空壳（comment_count 是 None，不是 0）+ detail 兜底也失败：
        这一轮「成功」但对存亡零信息（压根没量到任何数字）。
        勾上「已确认存活」等于替上游缺数作保——复选框必须原样不动。"""
        row = Row(record_id="d1",
                  link_cell="https://www.douyin.com/video/7123456789012345678",
                  publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))
        calls = {"n": 0}

        def responder(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return sse({"items": [], "comment_count": None,
                            "points": {"cost": 10, "balance": 100}})
            return err(500, 1005, "服务暂时不可用，请稍后重试")

        report = self.run_with(responder, [row])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertNotIn(self.settings.fields.alive_confirmed, outcome.fields)


class TestSoftDeadline(RunnerTest):
    """软截止的承诺是「没跑到的行**不写回**，留给下一轮」。

    写成「刷新失败」会顶掉最后更新时间（行要等满整个分层间隔才再被捞）、
    清掉排队勾（运营的手动请求被静默吞掉）、给失败计数 +1（为下一次
    非权威 GONE 一击定罪铺路）——断点续跑就全毁了。
    """

    def test_rows_past_deadline_are_not_written_back(self):
        # 负值 = 起跑线上截止就已过。0.0001 这种「快到期」在快机器上会让
        # 第一行赶在 100µs 内被派发出去，CI 上必挂——测试要的是确定性。
        self.settings.soft_deadline_seconds = -1
        rows = [xhs_row(f"rec{i}") for i in range(3)]
        row_with_history = rows[1]
        row_with_history.consecutive_failures = 1
        row_with_history.queued = True

        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh(rows, "fake-key", self.settings, now=NOW)

        posted.assert_not_called()                       # 一个请求都没发，一分钱没花
        for outcome in report.outcomes:
            self.assertEqual(outcome.status, runner.STATUS_DEFERRED)
            self.assertEqual(outcome.fields, {})         # 不写回 = 不动任何列
        self.assertIn("留给下一轮", report.aborted_reason)
        self.assertFalse(report.fatal)                   # 正常分批，不是故障

    def test_deferred_rows_do_not_trip_the_breaker(self):
        self.settings.soft_deadline_seconds = -1
        rows = [xhs_row(f"rec{i}") for i in range(12)]
        with mock.patch.object(transport, "post"):
            report = runner.refresh(rows, "fake-key", self.settings, now=NOW)
        self.assertFalse(report.breaker_tripped)


class TestStrikeSemantics(RunnerTest):
    """「连续失败次数」是两击定罪的计数器，只有 GONE 观测才有资格累加。

    超时、5xx 这类失败对「内容在不在」没有证据力：计进去的话，
    一次网络抖动 + 一次非权威空壳嫌疑就能把活帖标成「已失效」。
    """

    def test_network_failure_does_not_touch_the_counter(self):
        blip = transport.Response(0, "", "网络错误：timed out")
        report = self.run_with([blip] * 3, [xhs_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertNotIn(self.settings.fields.consecutive_failures, outcome.fields)

    def test_network_failure_does_not_erase_previous_gone_strike(self):
        """已经有一击 GONE 在案，中间隔一次网络故障：计数保持 1，不清零也不 +1。"""
        row = xhs_row()
        row.consecutive_failures = 1
        blip = transport.Response(0, "", "网络错误：timed out")
        report = self.run_with([blip] * 3, [row])
        self.assertNotIn(self.settings.fields.consecutive_failures,
                         report.outcomes[0].fields)

    def test_two_gone_strikes_still_convict_across_a_blip(self):
        """GONE → (网络故障，不计) → GONE：两次「取不到内容」的观测依然定罪。"""
        row = xhs_row()
        row.consecutive_failures = 1          # 上一轮的 GONE 一击
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)


class TestRateLimitThenGone(RunnerTest):
    def test_definitive_death_after_rate_limit_retry_still_convicts(self):
        """限流退避重试拿到的结果必须走同一套分类。

        之前的写法在重试后直接 return，权威的 1008「内容已删除」会被
        降级成一条备注、行状态「正常」、计数清零——一次限流抖动就让
        该死的行漏判。
        """
        queue = [
            transport.Response(429, "application/json", json.dumps(
                {"code": 1429, "message": "请求过于频繁，请稍后重试。",
                 "retry_after_seconds": 0})),
            err(200, 1008, "当前作品已删除。"),
        ]
        with mock.patch.object(transport, "post", side_effect=lambda *a, **k: queue.pop(0)), \
             mock.patch("time.sleep"):
            report = runner.refresh([xhs_row()], "fake-key", self.settings, now=NOW)
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_GONE)
        self.assertIn("风控中", outcome.fields[self.settings.fields.traffic_status])


class TestUnknownCommentCount(RunnerTest):
    """comment_count 为 None 的「成功」轮：热度/风控标签必须保持原样。

    抖音评论接口的 total 是 integer|null，detail 兜底又恰好失败时，
    这一轮对热度没有获得任何新证据——merge 把「大爆」「风控中」摘掉，
    等于用一次上游缺数抹掉棘轮历史和一条在生效的告警。
    """

    def _douyin_row(self):
        return Row(record_id="d1",
                   link_cell="https://www.douyin.com/video/7123456789012345678",
                   publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000),
                   current_tags=["大爆", "风控中", "已复盘"])

    def test_null_count_with_failed_detail_keeps_tags(self):
        calls = {"n": 0}

        def responder(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return sse({"items": [], "comment_count": None,
                            "points": {"cost": 10, "balance": 100}})
            return err(500, 1005, "服务暂时不可用，请稍后重试")

        report = self.run_with(responder, [self._douyin_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        # 标签无变化 → 字段根本不进 payload（保持原样的最强形式）
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
        self.assertIn("保持原样", outcome.fields[self.settings.fields.failure_reason])
        # 空壳轮也不该拿「暂无评论」覆盖上一轮的真实快照，或写置顶结论
        self.assertNotIn(self.settings.fields.comment_digest, outcome.fields)
        self.assertNotIn(self.settings.fields.pinned_status, outcome.fields)


class TestBreakerAccounting(RunnerTest):
    def _gone_rows(self, n):
        rows = []
        for i in range(n):
            row = xhs_row(f"gone{i}")
            row.consecutive_failures = 1
            rows.append(row)
        return rows

    def _cooldown_rows(self, n):
        rows = []
        for i in range(n):
            row = xhs_row(f"cool{i}")
            row.last_updated_ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
            rows.append(row)
        return rows

    def test_cooldown_rows_do_not_dilute_the_ratio(self):
        """分母只算真正打过上游的行。9 行全失效 + 3 行冷却 = 样本 9，
        不到 10 就不该熔断（旧算法会把冷却行混进分母凑够样本）。"""
        report = self.run_with(lambda *a, **k: err(200, 1003, "未找到对应内容"),
                               self._gone_rows(9) + self._cooldown_rows(3))
        self.assertFalse(report.breaker_tripped)

    def test_breaker_voids_strike_increment_and_keeps_diagnostics(self):
        report = self.run_with(lambda *a, **k: err(200, 1003, "未找到对应内容"),
                               self._gone_rows(12))
        self.assertTrue(report.breaker_tripped)
        for outcome in report.outcomes:
            # 判定作废，这一轮的计数增量也要一并撤销
            self.assertNotIn(self.settings.fields.consecutive_failures, outcome.fields)
            # 「已确认存活=False」也是这轮 GONE 判定的产物，同样要撤销——
            # 留着它，熔断就只救了状态列，没救勾选框。
            self.assertNotIn(self.settings.fields.alive_confirmed, outcome.fields)
            reason = outcome.fields[self.settings.fields.failure_reason]
            self.assertIn("未找到对应内容", reason)     # 原始错误（含 request_id 线索）还在
            self.assertIn("疑似上游故障", reason)       # 熔断说明是追加的，不是覆盖


class TestCrossRunBreaker(RunnerTest):
    """多表部署：单表可能只有三五行，永远凑不满熔断的最小样本——
    但上游故障是通道级的，跟行分在哪张表没关系，样本要全局算。"""

    def _gone_report(self, n, prefix):
        rows = []
        for i in range(n):
            row = xhs_row(f"{prefix}{i}")
            row.consecutive_failures = 1
            rows.append(row)
        return self.run_with(lambda *a, **k: err(200, 1003, "未找到对应内容"), rows)

    def test_small_tables_add_up_and_trip_together(self):
        r1 = self._gone_report(6, "a")
        r2 = self._gone_report(6, "b")
        self.assertFalse(r1.breaker_tripped)    # 单表 6 行不到样本线
        self.assertFalse(r2.breaker_tripped)
        self.assertTrue(runner.apply_cross_run_breaker([r1, r2], self.settings))
        for report in (r1, r2):
            self.assertTrue(report.breaker_tripped)
            for outcome in report.outcomes:
                self.assertEqual(outcome.status, runner.STATUS_FAILED)
                self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
                self.assertNotIn(self.settings.fields.consecutive_failures, outcome.fields)
                self.assertIn("疑似上游故障",
                              outcome.fields[self.settings.fields.failure_reason])

    def test_below_global_sample_stays_calm(self):
        r1 = self._gone_report(2, "a")
        r2 = self._gone_report(2, "b")
        self.assertFalse(runner.apply_cross_run_breaker([r1, r2], self.settings))
        self.assertEqual(r1.outcomes[0].status, runner.STATUS_GONE)

    def test_already_tripped_table_is_not_voided_twice(self):
        """大表自己熔断过：全局熔断不能给它的诊断信息再追加一遍，
        但它的原始样本要照常计入全局比例，小表才能被一起救下。"""
        big = self._gone_report(12, "big")
        small = self._gone_report(3, "small")
        self.assertTrue(big.breaker_tripped)
        self.assertTrue(runner.apply_cross_run_breaker([big, small], self.settings))
        self.assertTrue(small.breaker_tripped)
        reason = big.outcomes[0].fields[self.settings.fields.failure_reason]
        self.assertEqual(reason.count("疑似上游故障"), 1)


class TestSharedRunBudget(RunnerTest):
    """多表共享的运行预算：deadline 传绝对截止点（软截止是整次运行一份，
    不是每张表各一份），disabled 传共享集合（死讯跨表生效）。"""

    def test_external_deadline_defers_everything(self):
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([xhs_row()], "fake-key", self.settings,
                                    now=NOW, deadline=time.monotonic() - 1)
        posted.assert_not_called()
        self.assertEqual(report.outcomes[0].status, runner.STATUS_DEFERRED)

    def test_shared_disabled_channel_prevents_any_spend(self):
        """上一张表已确认唯一的 Key 失效：这张表一个请求都不该发。"""
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([xhs_row()], "fake-key", self.settings,
                                    now=NOW, disabled={"socialdatax"})
        posted.assert_not_called()
        self.assertTrue(report.fatal)
        self.assertIn("没有可用的数据通道", report.aborted_reason)

    def test_disabled_set_is_shared_back_to_caller(self):
        """本表发现的死讯要落进调用方传入的集合，下一张表才看得见。"""
        shared: set[str] = set()
        self.run_with([err(401, 1401, "API Key 无效或已失效。")], [xhs_row()],
                      disabled=shared)
        self.assertIn("socialdatax", shared)


if __name__ == "__main__":
    unittest.main()
