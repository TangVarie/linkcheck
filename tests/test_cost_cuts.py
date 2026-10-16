"""2026-10 那一轮降本：每一刀都不许以「少看见一点东西」为代价。

七件事，每件都有一个「以前多花在哪」和一个「改完为什么不丢效果」：

1. 已失效的行退避复查，不再按活帖的节奏每轮付一次钱
2. 「发布时间」空着的行用链接 ID 里自带的时刻算帖龄，照常分层、归档
3. 同一篇内容登记了多行：一轮只付一次钱，判定照旧逐行做
4. 主通道「收了钱、回的东西认不出来」连续够数，本轮就直接走备胎
5. 请求发出去了没回答（读超时）：最多再试一次，而且照实记账
6. 面板没人看就停止整表拉取，有人回来先取一趟新的
7. 抖音短链展开一次就记进「作品ID」，之后按 ID 走

外加一件新功能：数据整理过时提醒（评论数比整理那一刻涨了一大截）。
"""

import http.client
import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import cli
from xhsearch import analyze, links, panel, provision, rows, runner, schema, summary, transport
from xhsearch.config import Budget, Settings
from xhsearch.rows import Row

from tests import test_failover
from tests.test_failover import (KEYS, LANDING, expand_ok, sdx_ok_comments,
                                 tikhub_douyin, tikhub_ok_comments)

UTC = timezone.utc
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
XHS_ID = "6a8538d6000000003300c037"                       # 2026-08-19 05:02:14 UTC
XHS_LINK = "https://www.xiaohongshu.com/explore/" + XHS_ID
DY_ID = "7683833454907837105"                             # 2026-09-10
SHORT_DY = "https://v.douyin.com/iRxYzAb/"


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class FailoverTest(test_failover.FailoverTest):
    """沿用那一组的传输层替身，但「开跑那一刻」用这个文件的 NOW——
    这里的行按 NOW 算发布时间，两个时刻不一致的话帖龄会算成负数。"""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(test_failover, "NOW", NOW)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------- 2. ID 里的时间


class TestIdTimestamp(unittest.TestCase):
    def test_xhs_note_id_carries_its_creation_second(self):
        self.assertEqual(links.id_timestamp("xhs", XHS_ID), 0x6a8538d6)
        self.assertEqual(datetime.fromtimestamp(0x6a8538d6, UTC).date().isoformat(),
                         "2026-08-19")

    def test_douyin_aweme_id_carries_it_in_the_high_bits(self):
        seconds = links.id_timestamp("douyin", DY_ID)
        self.assertEqual(seconds, int(DY_ID) >> 32)
        self.assertEqual(datetime.fromtimestamp(seconds, UTC).date().isoformat(), "2026-09-10")

    def test_values_that_are_not_this_encoding_are_unknown(self):
        self.assertIsNone(links.id_timestamp("xhs", "0123456789abcdef01234567"))  # 1970
        self.assertIsNone(links.id_timestamp("xhs", "not-hex"))
        self.assertIsNone(links.id_timestamp("douyin", "abc"))
        self.assertIsNone(links.id_timestamp("douyin", "123"))
        self.assertIsNone(links.id_timestamp(None, XHS_ID))
        self.assertIsNone(links.id_timestamp("xhs", None))


def note_created(days_ago: float) -> str:
    """一个「创建于 N 天前」的小红书 note_id。"""
    created = NOW - timedelta(days=days_ago)
    return "https://www.xiaohongshu.com/explore/" + f"{int(created.timestamp()):08x}" + "0" * 16


class TestPublishTimeFallback(unittest.TestCase):
    """「发布时间」空着时用 ID 推一个**最晚可能**的发布时刻：ID 时间 + 14 天
    （定时发布的笔记 ID 在排期时就有了）。推出来的帖龄只会偏小。"""

    def setUp(self):
        self.settings = Settings()

    def test_blank_publish_time_eventually_archives(self):
        """以前：8 小时一轮、永不归档。现在：ID 生成于 58 天前，最晚也是 44 天前发的 → 归档。"""
        row = Row("r", XHS_LINK, last_updated_ms=ms(NOW - timedelta(hours=9)))
        self.assertEqual(row.schedule_publish_ms(self.settings, NOW),
                         (0x6a8538d6 + 14 * 86400) * 1000)
        self.assertTrue(row.publish_time_inferred(NOW))
        self.assertIsNone(row.refresh_interval_hours(self.settings, NOW))
        self.assertFalse(row.is_due(self.settings, NOW))

    def test_a_scheduled_post_is_never_archived_early(self):
        """ID 生成于 40 天前、按最晚的情况是 26 天前才发的：还在管，不能归档。"""
        row = Row("r", note_created(40), last_updated_ms=ms(NOW - timedelta(hours=73)))
        self.assertAlmostEqual(row.schedule_age_days(self.settings, NOW), 26, delta=0.01)
        self.assertEqual(row.refresh_interval_hours(self.settings, NOW), 72)
        self.assertTrue(row.is_due(self.settings, NOW))
        self.assertIsNone(Row("old", note_created(46)).refresh_interval_hours(self.settings, NOW))

    def test_the_tier_is_never_slower_than_the_true_date_would_give(self):
        """ID 生成于 20 天前：真实发布可能就在 6 天前（定时发布），按 24 小时档刷，
        而不是按 ID 时间算出来的 72 小时档。"""
        row = Row("r", note_created(20), last_updated_ms=ms(NOW - timedelta(hours=25)))
        self.assertEqual(row.refresh_interval_hours(self.settings, NOW), 24)

    def test_a_fresh_id_is_treated_as_brand_new(self):
        row = Row("r", note_created(1.25), last_updated_ms=ms(NOW - timedelta(hours=7)))
        self.assertEqual(row.schedule_age_days(self.settings, NOW), 0)
        self.assertEqual(row.refresh_interval_hours(self.settings, NOW), 8)

    def test_the_margin_is_configurable(self):
        self.settings.refresh.inferred_publish_margin_days = 0
        row = Row("r", note_created(20))
        self.assertAlmostEqual(row.schedule_age_days(self.settings, NOW), 20, delta=0.01)

    def test_heat_tiers_do_not_use_the_inferred_time(self):
        """热度档位是给人看的结论：发布时间空着就照旧「判不了」，不拿推出来的时刻打无水花。"""
        self.assertIsNone(Row("r", note_created(20)).age_hours(NOW))

    def test_the_table_wins_when_it_is_filled(self):
        filled = ms(NOW - timedelta(days=1))
        row = Row("r", XHS_LINK, publish_time_ms=filled)
        self.assertEqual(row.schedule_publish_ms(self.settings, NOW), filled)
        self.assertFalse(row.publish_time_inferred(NOW))

    def test_an_id_that_decodes_into_the_future_is_ignored(self):
        """测试固件里的 "a"*24 解出来在 2060 年：当不知道——和短链一样不巡查。"""
        row = Row("r", "https://www.xiaohongshu.com/explore/" + "a" * 24)
        self.assertIsNone(row.schedule_publish_ms(self.settings, NOW))
        self.assertIsNone(row.schedule_age_days(self.settings, NOW))
        self.assertIsNone(row.refresh_interval_hours(self.settings, NOW))

    def test_a_short_link_without_an_id_is_still_unknown(self):
        row = Row("r", "http://xhslink.com/a/AbC")
        self.assertIsNone(row.schedule_age_days(self.settings, NOW))
        self.assertFalse(row.publish_time_inferred(NOW))


# ---------------------------------------------------------------- 1. 已失效退避


class TestGoneBackoff(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def gone(self, *, strikes=2, checked_hours_ago=9.0, **extra) -> Row:
        return Row("r", XHS_LINK, publish_time_ms=ms(NOW - timedelta(days=1)),
                   last_updated_ms=ms(NOW - timedelta(hours=checked_hours_ago)),
                   refresh_status=runner.STATUS_GONE, consecutive_failures=strikes, **extra)

    def test_a_dead_row_is_not_rechecked_on_the_live_schedule(self):
        self.assertFalse(self.gone(checked_hours_ago=9).is_due(self.settings, NOW))
        self.assertTrue(self.gone(checked_hours_ago=72).is_due(self.settings, NOW))

    def test_each_further_confirmation_doubles_the_wait(self):
        self.assertEqual(self.gone(strikes=2).refresh_interval_hours(self.settings, NOW), 72)
        self.assertEqual(self.gone(strikes=3).refresh_interval_hours(self.settings, NOW), 144)
        self.assertEqual(self.gone(strikes=4).refresh_interval_hours(self.settings, NOW), 288)

    def test_an_absurd_counter_cannot_overflow_the_interval(self):
        self.assertEqual(self.gone(strikes=10_000).gone_backoff_hours(self.settings), 72 * 1024)

    def test_live_and_suspect_rows_keep_their_tier(self):
        for status in (runner.STATUS_OK, runner.STATUS_SUSPECT, runner.STATUS_FAILED, ""):
            row = Row("r", XHS_LINK, publish_time_ms=ms(NOW - timedelta(days=1)),
                      last_updated_ms=ms(NOW - timedelta(hours=9)),
                      refresh_status=status, consecutive_failures=1)
            self.assertIsNone(row.gone_backoff_hours(self.settings), status)

    def test_backoff_starts_from_the_configured_slowest_tier(self):
        self.settings.refresh.tiers = [(1, 4), (5, 12)]
        self.assertEqual(self.gone(strikes=2).gone_backoff_hours(self.settings), 12)

    def test_archive_still_wins(self):
        row = Row("r", XHS_LINK, publish_time_ms=ms(NOW - timedelta(days=40)),
                  last_updated_ms=ms(NOW - timedelta(days=20)),
                  refresh_status=runner.STATUS_GONE, consecutive_failures=2)
        self.assertIsNone(row.refresh_interval_hours(self.settings, NOW))

    def test_a_queued_dead_row_is_still_picked_up(self):
        """人工勾「排队刷新」随时能复查，退避管不着它。"""
        f = self.settings.fields

        class _Table:
            def search(self, field_names, *, filter_spec=None, max_records=None):
                return [{"record_id": "dead", "fields": {
                    f.link: XHS_LINK, f.queued: True,
                    f.publish_time: ms(NOW - timedelta(days=1)),
                    f.last_updated: ms(NOW - timedelta(hours=1)),
                    f.refresh_status: runner.STATUS_GONE, f.consecutive_failures: 2}}]

        picked = runner.load_rows(_Table(), self.settings, only_queued=True, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["dead"])


# ---------------------------------------------------------------- 3. 同篇共享


def xhs(record_id, *, note=XHS_ID, token="", keywords=None, checked_hours_ago=None,
        age_days=1.0) -> Row:
    link = f"https://www.xiaohongshu.com/explore/{note}" + (f"?xsec_token={token}" if token else "")
    return Row(record_id, link, publish_time_ms=ms(NOW - timedelta(days=age_days)),
               seed_keywords=keywords or [],
               last_updated_ms=(None if checked_hours_ago is None
                                else ms(NOW - timedelta(hours=checked_hours_ago))))


class SharingTest(FailoverTest):
    def setUp(self):
        super().setUp()
        self.settings.detail_within_days = 0   # 一行一个付费请求，账好算

    def run_rows(self, rows_, **kwargs):
        return self.run_with(on_get=lambda i: tikhub_ok_comments(),
                             on_post=lambda i: self.fail("不该降级"),
                             rows=rows_, **kwargs)


class TestSameNoteIsPaidOnce(SharingTest):
    def test_two_rows_of_one_note_cost_one_call(self):
        report = self.run_rows([xhs("a", token="t1"), xhs("b", token="t2")])
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 2)
        f = self.settings.fields
        self.assertEqual([o.fields[f.comment_count] for o in report.outcomes], [150, 150])
        self.assertEqual(report.shared_rows, 1)
        self.assertAlmostEqual(report.cost_yuan, rows.estimate_yuan(
            [xhs("a")], self.settings, NOW, keys=KEYS))
        self.assertEqual(report.outcomes[1].cost_yuan, 0.0)

    def test_each_row_still_gets_its_own_verdict(self):
        """共享的是「上游回了什么」，不是结论：两行的关键词不一样，结论就不一样。"""
        report = self.run_rows([xhs("hit", keywords=["戳主页"]), xhs("miss", keywords=["没有的词"])])
        f, cs = self.settings.fields, self.settings.comment_status
        self.assertEqual(report.outcomes[0].fields[f.comment_status], cs.displayed)
        self.assertEqual(report.outcomes[1].fields[f.comment_status], cs.not_displayed)

    def test_different_notes_are_not_shared(self):
        other = "6a8538d7000000003300c037"
        report = self.run_rows([xhs("a"), xhs("b", note=other)])
        self.assertEqual(len(self.get_calls), 2)
        self.assertEqual(report.shared_rows, 0)

    def test_the_share_spans_tables_when_the_caller_passes_one(self):
        share = runner.FetchShare()
        self.run_rows([xhs("a")], share=share)
        report = self.run_rows([xhs("copy-in-another-table")], share=share)
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_a_free_row_does_not_eat_the_budget(self):
        budget = runner.RunBudget(Budget(max_records_per_run=1))
        report = self.run_rows([xhs("a"), xhs("b")], budget=budget)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 2)
        self.assertEqual(budget.records, 1)

    def test_a_dead_note_is_dead_for_every_row(self):
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "all_sort_strategies": [],
                "user_id": ""}}}), "th-x")
        report = self.run_with(on_get=lambda i: empty_shell,
                               on_post=lambda i: self.fail("GONE 不降级"),
                               rows=[xhs("a"), xhs("b")])
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_SUSPECT] * 2)


class TestRideAlong(SharingTest):
    def table(self, records):
        class _Table:
            def search(self, field_names, *, filter_spec=None, max_records=None):
                return records
        return _Table()

    def record(self, record_id, *, note=XHS_ID, checked_hours_ago, age_days=1.0):
        f = self.settings.fields
        return {"record_id": record_id, "fields": {
            f.link: "https://www.xiaohongshu.com/explore/" + note,
            f.publish_time: ms(NOW - timedelta(days=age_days)),
            f.last_updated: ms(NOW - timedelta(hours=checked_hours_ago))}}

    def test_a_not_yet_due_sibling_rides_along_at_the_end(self):
        other = "6a8538d7000000003300c037"
        picked = runner.load_rows(self.table([
            self.record("sibling", checked_hours_ago=1),            # 同篇，没到期
            self.record("due", checked_hours_ago=9),                # 到期了
            self.record("stranger", note=other, checked_hours_ago=1),  # 别的篇，没到期
        ]), self.settings, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["due", "sibling"])
        self.assertEqual([r.ride_along for r in picked], [False, True])

    def test_archived_siblings_are_left_alone(self):
        picked = runner.load_rows(self.table([
            self.record("due", checked_hours_ago=9),
            self.record("old", checked_hours_ago=1, age_days=40),
        ]), self.settings, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["due"])

    def test_riders_get_the_result_for_free_and_line_up_for_next_time(self):
        picked = runner.load_rows(self.table([
            self.record("sibling", checked_hours_ago=1),
            self.record("due", checked_hours_ago=9),
        ]), self.settings, now=NOW)
        report = self.run_rows(picked)
        self.assertEqual(len(self.get_calls), 1)
        f = self.settings.fields
        stamps = [o.fields[f.last_updated] for o in report.outcomes]
        self.assertEqual(len(set(s // 60000 for s in stamps)), 1, "两行的检查时间该对齐")

    def test_a_rider_never_pays_on_its_own(self):
        rider = xhs("rider", checked_hours_ago=1)
        rider.ride_along = True
        report = self.run_with(on_get=lambda i: self.fail("搭车的行不能自己发请求"),
                               on_post=lambda i: self.fail("搭车的行不能自己发请求"),
                               rows=[rider])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_RIDE_SKIPPED)
        self.assertEqual(report.outcomes[0].fields, {})
        self.assertEqual(report.aborted_reason, "", "没到期的行不算「没处理完」")

    def test_estimates_count_paying_rows_only(self):
        rider = xhs("rider")
        rider.ride_along = True
        batch = [xhs("a", token="1"), xhs("b", token="2"), rider,
                 xhs("c", note="6a8538d7000000003300c037")]
        self.assertEqual([r.record_id for r in rows.paying_rows(batch, self.settings, NOW)],
                         ["a", "c"])


# ---------------------------------------------------------------- 4. 契约坏了


def tikhub_no_data() -> transport.Response:
    """HTTP 200、成功信封，但 data 不是对象：收了钱、认不出来。"""
    return transport.Response(200, "application/json",
                              json.dumps({"code": 200, "data": "upstream changed"}), "th-bad")


class TestContractBreak(SharingTest):
    def five_notes(self):
        return [xhs(f"r{i}", note=f"6a8538d{i}000000003300c037") for i in range(5)]

    def test_after_three_billed_garbage_answers_the_rest_go_straight_to_the_backup(self):
        report = self.run_with(on_get=lambda i: tikhub_no_data(),
                               on_post=lambda i: sdx_ok_comments(),
                               rows=self.five_notes())
        self.assertEqual(len(self.get_calls), 3)
        self.assertEqual(len(self.post_calls), 5)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 5)
        self.assertEqual(report.contract_trips, [("tikhub", "xhs")])
        self.assertAlmostEqual(report.cost_yuan, 3 * 0.072 + 5 * 0.10)

    def test_without_a_backup_nothing_is_switched_off(self):
        """没有备胎时停掉它只是把同样的钱挪到下一轮，还会把这一轮报成故障。"""
        report = self.run_with(on_get=lambda i: tikhub_no_data(),
                               on_post=lambda i: self.fail("没配备胎"),
                               rows=self.five_notes(), keys={"tikhub": "t-key"})
        self.assertEqual(len(self.get_calls), 5)
        self.assertEqual(report.contract_trips, [])
        self.assertFalse(report.fatal)

    def test_a_good_answer_resets_the_streak(self):
        answers = [tikhub_no_data(), tikhub_no_data(), tikhub_ok_comments(),
                   tikhub_no_data(), tikhub_no_data()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: sdx_ok_comments(),
                               rows=self.five_notes())
        self.assertEqual(len(self.get_calls), 5)
        self.assertEqual(report.contract_trips, [])

    def test_only_the_broken_platform_is_switched_off(self):
        """小红书端点改版，抖音端点照样好：整家停掉会把抖音推到贵 14 倍的备胎上。"""
        douyin = Row("dy", f"https://www.douyin.com/video/{DY_ID}",
                     publish_time_ms=ms(NOW - timedelta(days=1)))

        def on_get(i):
            url = self.get_calls[i]
            if "xiaohongshu" in url:
                return tikhub_no_data()
            return tikhub_douyin(0 if "comments" in url else 1)

        report = self.run_with(on_get=on_get, on_post=lambda i: sdx_ok_comments(),
                               rows=self.five_notes() + [douyin])
        self.assertEqual(report.outcomes[-1].status, runner.STATUS_OK)
        self.assertEqual(sum("douyin" in u for u in self.get_calls), 2)
        self.assertTrue(rows.is_disabled("tikhub", "xhs", {"tikhub:xhs"}))
        self.assertFalse(rows.is_disabled("tikhub", "douyin", {"tikhub:xhs"}))


# ---------------------------------------------------------------- 5. 发出去了没回答


class TestInDoubtTransport(unittest.TestCase):
    def perform(self, exc):
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            return transport.get("https://api.tikhub.dev/x", {})

    def test_a_read_timeout_is_in_doubt(self):
        self.assertTrue(self.perform(TimeoutError("timed out")).in_doubt)

    def test_a_dropped_connection_after_sending_is_in_doubt(self):
        self.assertTrue(self.perform(http.client.RemoteDisconnected("bye")).in_doubt)

    def test_a_connect_failure_is_not(self):
        response = self.perform(urllib.error.URLError("refused"))
        self.assertEqual(response.status, 0)
        self.assertFalse(response.in_doubt)


class TestInDoubtRetries(unittest.TestCase):
    def call(self, provider, response):
        from xhsearch.rows import ToolCall
        sent = []

        def fake(*a, **k):
            sent.append(1)
            return response

        target = "get" if provider == "tikhub" else "post"
        with mock.patch.object(transport, target, side_effect=fake), mock.patch("time.sleep"):
            result, attempts, unanswered = runner._call_once(
                provider, "k", ToolCall("xhs", "comments", {"note_id": XHS_ID}),
                deadline=None, timeout=1)
        return attempts, unanswered, len(sent)

    def test_a_billing_channel_retries_an_unanswered_request_once(self):
        timeout = transport.Response(0, "", "请求超时", in_doubt=True)
        self.assertEqual(self.call("tikhub", timeout), (2, 2, 2))

    def test_a_connect_failure_keeps_the_full_three_tries(self):
        refused = transport.Response(0, "", "网络错误：refused")
        self.assertEqual(self.call("tikhub", refused), (3, 0, 3))

    def test_a_channel_that_does_not_bill_failures_keeps_three_tries(self):
        timeout = transport.Response(0, "", "请求超时", in_doubt=True)
        attempts, unanswered, sent = self.call("socialdatax", timeout)
        self.assertEqual((attempts, sent), (3, 3))


class TestInDoubtIsOnTheBill(SharingTest):
    def test_unanswered_tikhub_requests_are_charged_and_reported(self):
        timeout = transport.Response(0, "", "请求超时（30s）", in_doubt=True)
        report = self.run_with(on_get=lambda i: timeout,
                               on_post=lambda i: sdx_ok_comments(),
                               rows=[xhs("a")])
        self.assertEqual(len(self.get_calls), 2)
        self.assertEqual(report.unanswered, 2)
        self.assertAlmostEqual(report.cost_yuan, 2 * 0.072 + 0.10)
        self.assertIn("没拿到回答", report.summary())


# ---------------------------------------------------------------- 6. 面板没人看就停


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestPanelPausesWhenNobodyLooks(unittest.TestCase):
    def make(self, idle=600.0):
        self.produced = 0

        def produce():
            self.produced += 1
            return summary.Overview(projects=[])

        self.clock = FakeClock()
        return panel.Cache(produce, ttl=60, idle_seconds=idle, clock=self.clock)

    def tick(self, cache):
        """后台线程一圈做的事（不起线程，免得测试看时机）。"""
        if cache._someone_watching():
            cache.refresh()
        else:
            with cache._lock:
                cache._paused = True

    def test_keeps_refreshing_while_someone_was_here_recently(self):
        cache = self.make()
        self.tick(cache)
        self.clock.now += 300
        self.tick(cache)
        self.assertEqual(self.produced, 2)

    def test_stops_after_the_idle_window(self):
        cache = self.make()
        self.clock.now += 601
        self.tick(cache)
        self.tick(cache)
        self.assertEqual(self.produced, 0)

    def test_the_first_visit_after_a_pause_waits_for_fresh_data(self):
        cache = self.make()
        self.clock.now += 601
        self.tick(cache)
        overview, _err, fetched = cache.view()
        self.assertEqual(self.produced, 1)
        self.assertIsNotNone(overview)
        cache.view()
        self.assertEqual(self.produced, 1, "恢复之后交还给后台节奏，不是每个请求都取一趟")

    def test_a_forced_refresh_after_a_pause_is_not_followed_by_another(self):
        """点「重新取数」= 先 refresh 再出页面：停过之后也只取一趟。"""
        cache = self.make()
        self.clock.now += 601
        self.tick(cache)
        cache.refresh()
        cache.view()
        self.assertEqual(self.produced, 1)

    def test_any_request_keeps_it_alive(self):
        cache = self.make()
        self.clock.now += 500
        cache.touch()
        self.clock.now += 500
        self.tick(cache)
        self.assertEqual(self.produced, 1)

    def test_zero_means_never_pause(self):
        cache = self.make(idle=None)
        self.clock.now += 10 ** 6
        self.tick(cache)
        self.assertEqual(self.produced, 1)

    def test_the_idle_window_is_configurable(self):
        base = {"PANEL_PASSWORD": "x" * 16, "PANEL_SECRET": "s" * 32}
        self.assertEqual(panel.PanelConfig.from_env(base).idle_minutes, 15.0)
        self.assertEqual(panel.PanelConfig.from_env(
            {**base, "PANEL_IDLE_MINUTES": "0"}).idle_minutes, 0.0)


# ---------------------------------------------------------------- 7. 作品ID


class TestResolvedId(unittest.TestCase):
    def test_it_only_speaks_for_the_link_it_came_from(self):
        cell = rows.format_resolved_id("7123456789012345678", SHORT_DY)
        self.assertEqual(rows.read_resolved_id(cell, SHORT_DY), "7123456789012345678")
        self.assertEqual(rows.read_resolved_id(cell, SHORT_DY.rstrip("/")), "7123456789012345678")
        self.assertIsNone(rows.read_resolved_id(cell, "https://v.douyin.com/other/"))
        self.assertIsNone(rows.read_resolved_id("乱填的", SHORT_DY))
        self.assertIsNone(rows.read_resolved_id("", SHORT_DY))

    def test_a_known_id_turns_the_row_into_id_form(self):
        row = Row("r", f"复制打开抖音 {SHORT_DY}",
                  resolved_id_cell=rows.format_resolved_id("7123456789012345678", SHORT_DY))
        self.assertEqual(row.parsed.content_id, "7123456789012345678")
        calls = rows.plan_calls(row, Settings(), NOW)
        self.assertEqual(calls[0].arguments, {"aweme_id": "7123456789012345678"})

    def test_a_stale_id_for_another_link_is_ignored(self):
        row = Row("r", "https://v.douyin.com/brandNew/",
                  resolved_id_cell=rows.format_resolved_id("7123456789012345678", SHORT_DY))
        self.assertIsNone(row.parsed.content_id)

    def test_the_cell_is_read_back_from_the_table(self):
        settings = Settings()
        f = settings.fields
        row = runner.row_from_record({"record_id": "r", "fields": {
            f.link: SHORT_DY, f.resolved_id: f"7123456789012345678 ← {SHORT_DY}"}}, settings)
        self.assertEqual(row.parsed.content_id, "7123456789012345678")


class TestResolvedIdIsRemembered(FailoverTest):
    def row(self, cell=""):
        return Row("dy", f"7.86 复制打开抖音，看看作品 {SHORT_DY}",
                   publish_time_ms=ms(NOW - timedelta(days=1)), resolved_id_cell=cell)

    def test_the_first_expansion_is_written_down(self):
        report = self.run_with(on_get=tikhub_douyin, on_post=lambda i: self.fail("走 TikHub"),
                               on_expand=expand_ok, rows=[self.row()])
        f = self.settings.fields
        self.assertEqual(report.outcomes[0].fields[f.resolved_id],
                         f"7123456789012345678 ← {SHORT_DY}")
        self.assertIn("7123456789012345678", LANDING)

    def test_next_time_there_is_nothing_to_expand(self):
        cell = rows.format_resolved_id("7123456789012345678", SHORT_DY)
        report = self.run_with(on_get=tikhub_douyin, on_post=lambda i: self.fail("走 TikHub"),
                               on_expand=lambda *a: self.fail("记过 ID 就不该再展开"),
                               rows=[self.row(cell)])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertNotIn(self.settings.fields.resolved_id, report.outcomes[0].fields)
        self.assertIn("aweme_id=7123456789012345678", self.get_calls[0])


# ---------------------------------------------------------------- 8. estimate 的计数


class TestWasteReport(unittest.TestCase):
    def test_counts_the_three_shapes(self):
        settings = Settings()
        f = settings.fields
        batch = [
            xhs("dup1"), xhs("dup2", token="x"),
            Row("blind", "http://xhslink.com/a/AbC"),
            Row("inferred", XHS_LINK.replace("6a8538d6", "6a8538d5")),
            Row("dead", "https://www.xiaohongshu.com/explore/6a8538d4000000003300c037",
                publish_time_ms=ms(NOW - timedelta(days=3)),
                refresh_status=runner.STATUS_GONE, consecutive_failures=2),
        ]
        text = "\n".join(cli._waste_report(batch, settings, NOW))
        self.assertIn("在管 5 行", text)
        self.assertIn("1 篇 / 2 行", text)
        self.assertIn(f"「{f.publish_time}」空着：2 行", text)
        self.assertIn("其中 1 行按链接 ID 推了保守帖龄", text)
        self.assertIn("1 行连 ID 也解不出", text)
        self.assertIn("已失效、还没归档：1 行", text)


# ---------------------------------------------------------------- 新功能：数据整理过时


COLLATED = "曝光量：600；阅读量：43；互动量：35"


class TestCollationParse(unittest.TestCase):
    def test_the_interaction_number_is_read_like_the_formula_reads_it(self):
        self.assertEqual(analyze.collation_interactions(COLLATED), 35)
        self.assertEqual(analyze.collation_interactions("曝光量：/；阅读量：/；互动量：2"), 2)
        self.assertEqual(analyze.collation_interactions("互动量: 7"), 7)

    def test_unreadable_values_mean_no_collation(self):
        for text in ("曝光量：600；阅读量：43；互动量：/", "互动量：1.2万", "", None, 35,
                     "曝光量：600"):
            self.assertIsNone(analyze.collation_interactions(text), text)


class TestRecollectCheck(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def check(self, *, text=None, comments=None, interactions=None, count):
        if text is None:
            # 默认：表里那份整理就是基线记的那一份（互动量对得上）。
            text = COLLATED if interactions is None else f"曝光量：1；阅读量：1；互动量：{interactions}"
        return analyze.recollect_check(collation_text=text, stored_comments=comments,
                                       stored_interactions=interactions,
                                       comment_count=count, settings=self.settings)

    def test_nothing_to_say_without_a_collation_or_a_count(self):
        self.assertIsNone(self.check(text="", count=10))
        self.assertIsNone(self.check(count=None))

    def test_the_first_sight_records_a_baseline(self):
        result = self.check(count=12)
        self.assertEqual((result.status, result.baseline_comments,
                          result.baseline_interactions, result.stale),
                         ("已整理", 12, 35, False))
        self.assertIn("记下这份数据整理的基线", result.note)

    def test_a_big_rise_since_the_collation_asks_for_a_new_one(self):
        result = self.check(comments=12, interactions=35, count=40)
        self.assertEqual(result.status, "需重新整理")
        self.assertTrue(result.stale)
        self.assertIsNone(result.baseline_comments, "基线不动")
        self.assertIn("12", result.note)
        self.assertIn("40", result.note)
        self.assertIn("重新回收后台数据", result.note)

    def test_both_gates_must_pass(self):
        # 比例够、增量不够：2 → 6
        self.assertFalse(self.check(comments=2, interactions=35, count=6).stale)
        # 增量够、比例不够：1000 → 1100
        self.assertFalse(self.check(comments=1000, interactions=5000, count=1100).stale)
        # 边界都算：40 → 60
        self.assertTrue(self.check(comments=40, interactions=200, count=60).stale)

    def test_a_drop_never_asks_for_a_new_collation(self):
        self.assertFalse(self.check(comments=100, interactions=300, count=20).stale)

    def test_an_old_collation_already_outgrown_is_flagged_on_first_sight(self):
        """上线那一刻已经整理过的老帖：评论数都比当时整个互动量多一截了，
        那份整理早过时了，不用再等一轮。"""
        result = self.check(count=80)
        self.assertEqual(result.baseline_comments, 35)
        self.assertTrue(result.stale)
        self.assertIn("整个互动量 35", result.note)

    def test_a_new_collation_resets_the_baseline(self):
        result = self.check(text="曝光量：900；阅读量：80；互动量：120",
                            comments=12, interactions=35, count=40)
        self.assertEqual((result.status, result.baseline_comments, result.baseline_interactions),
                         ("已整理", 40, 120))

    def test_clearing_the_baseline_by_hand_re_records_it(self):
        result = self.check(comments=None, interactions=35, count=30)
        self.assertEqual(result.baseline_comments, 30)
        self.assertFalse(result.stale)


class TestRecollectInTheRunner(SharingTest):
    def collated(self, **extra) -> Row:
        row = xhs("c")
        row.collation_text = COLLATED
        for key, value in extra.items():
            setattr(row, key, value)
        return row

    def run_one(self, row, **kwargs):
        kwargs.setdefault("collation_ready", True)
        return self.run_rows([row], **kwargs).outcomes[0]

    def test_writes_status_and_baseline(self):
        f = self.settings.fields
        fields = self.run_one(self.collated(collation_comments=12,
                                            collation_interactions=35)).fields   # 互动量 35 对得上
        self.assertEqual(fields[f.collation_status], "需重新整理")      # 12 → 150
        self.assertNotIn(f.collation_comments, fields)
        self.assertIn("请重新回收后台数据截图", fields[f.failure_reason])

    def test_first_sight_writes_the_baseline(self):
        f = self.settings.fields
        fields = self.run_one(self.collated()).fields
        self.assertEqual(fields[f.collation_comments], 35)       # min(150, 35)
        self.assertEqual(fields[f.collation_interactions], 35)
        self.assertEqual(fields[f.collation_status], "需重新整理")

    def test_an_unchanged_status_is_not_rewritten(self):
        f = self.settings.fields
        fields = self.run_one(self.collated(collation_text="曝光量：1；阅读量：1；互动量：300",
                                            collation_comments=140, collation_interactions=300,
                                            collation_status="已整理")).fields
        self.assertNotIn(f.collation_status, fields)

    def test_half_a_set_of_columns_means_hands_off(self):
        f = self.settings.fields
        fields = self.run_one(self.collated(), collation_ready=False).fields
        for column in (f.collation_status, f.collation_comments, f.collation_interactions):
            self.assertNotIn(column, fields)

    def test_a_missing_option_is_skipped_and_said(self):
        f = self.settings.fields
        fields = self.run_one(self.collated(), collation_status_options=["已整理"]).fields
        self.assertNotIn(f.collation_status, fields)
        self.assertIn("还没建选项「需重新整理」", fields[f.failure_reason])

    def test_a_failed_round_does_not_touch_it(self):
        f = self.settings.fields
        report = self.run_with(on_get=lambda i: transport.Response(0, "", "网络错误：x"),
                               on_post=lambda i: transport.Response(0, "", "网络错误：x"),
                               rows=[self.collated()], collation_ready=True)
        self.assertNotIn(f.collation_status, report.outcomes[0].fields)

    def test_the_column_check_in_cli(self):
        f = self.settings.fields
        full = {f.collation_source, f.collation_status, f.collation_comments,
                f.collation_interactions}
        with mock.patch("builtins.print"):
            self.assertTrue(cli._collation_ready(self.settings, full))
            self.assertFalse(cli._collation_ready(self.settings, full - {f.collation_interactions}))
            self.assertFalse(cli._collation_ready(self.settings, full - {f.collation_source}))
            self.assertFalse(cli._collation_ready(self.settings, None))


class TestRecollectOnThePanel(unittest.TestCase):
    def test_it_is_a_todo_and_a_count(self):
        settings = Settings()
        f = settings.fields
        snap = summary.build_snapshot(
            label="甲", app_token="app", table_id="tbl", settings=settings, now=NOW,
            records=[{"record_id": "r", "fields": {
                f.link: XHS_LINK, f.publish_time: ms(NOW - timedelta(days=10)),
                f.last_updated: ms(NOW - timedelta(hours=1)),
                f.collation_status: "需重新整理"}}])
        self.assertEqual(snap.recollect_rows, 1)
        self.assertEqual(snap.todos[0].reasons, ["需重新整理"])
        self.assertIn(f.collation_status, summary.panel_fields(settings))


class TestTheNewColumnsAreBuilt(unittest.TestCase):
    def test_schema_and_template_know_them(self):
        settings = Settings()
        f = settings.fields
        expected = {name for name, *_ in schema.expected_schema(settings)}
        new = (f.resolved_id, f.collation_status, f.collation_comments, f.collation_interactions)
        for column in new:
            self.assertIn(column, expected)
        built, _deferred, _skipped = provision.template_fields(settings)
        names = [body["field_name"] for body in built]
        for column in new:
            self.assertIn(column, names)
        status = next(b for b in built if b["field_name"] == f.collation_status)
        self.assertEqual([o["name"] for o in status["property"]["options"]],
                         ["已整理", "需重新整理"])


class TestInferredPublishTimeIsSaid(SharingTest):
    def test_the_diagnosis_mentions_it(self):
        row = Row("r", XHS_LINK.replace("6a8538d6", f"{int((NOW - timedelta(days=3)).timestamp()):08x}"))
        report = self.run_rows([row])
        reason = report.outcomes[0].fields[self.settings.fields.failure_reason]
        self.assertIn("刷新节奏按链接 ID 推算", reason)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- Codex 审查（2026-10-16）的回归


import threading
import time as _time

# 模块加载时就拿住真的 sleep：FailoverTest.run_with 会把 time.sleep 换成空操作
# （掐掉退避），在那里面再调 time.sleep 等于没等。
_real_sleep = _time.sleep


def slow_tikhub_comments(delay=0.05):
    def respond(i):
        _real_sleep(delay)          # 让第一行「在飞」的时候第二行也到了
        return tikhub_ok_comments()
    return respond


class TestClaimsAreMadeBeforeDispatch(SharingTest):
    def setUp(self):
        super().setUp()
        self.settings.max_concurrency = 2

    def slow_reservation(self, record_id, delay=0.1):
        """让 record_id 那一行在**预留预算之前**慢一拍：同篇的另一行一定先跑到。
        谁先跑到谁取的话，这时候后一行看不到它；派发前认领就不怕。"""
        real = runner.estimate_yuan

        def slow(batch, *a, **k):
            if batch and batch[0].record_id == record_id:
                _real_sleep(delay)
            return real(batch, *a, **k)
        return mock.patch.object(runner, "estimate_yuan", side_effect=slow)

    def test_an_in_flight_duplicate_does_not_take_a_budget_slot(self):
        """两行同一篇同时在飞：后一行等前一行，不另占一行预算（MAX_RECORDS_PER_RUN=1 也能刷完）。"""
        budget = runner.RunBudget(Budget(max_records_per_run=1))
        with self.slow_reservation("a"):
            report = self.run_with(on_get=slow_tikhub_comments(),
                                   on_post=lambda i: self.fail("不该降级"),
                                   rows=[xhs("a", token="1"), xhs("b", token="2")],
                                   budget=budget)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 2)
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual(budget.records, 1)

    def test_a_rider_waits_for_its_due_sibling_instead_of_skipping(self):
        due = xhs("due", checked_hours_ago=9)
        rider = xhs("rider", checked_hours_ago=1)
        rider.ride_along = True
        with self.slow_reservation("due"):
            report = self.run_with(on_get=slow_tikhub_comments(),
                                   on_post=lambda i: self.fail("不该降级"),
                                   rows=[due, rider])
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 2)
        self.assertEqual(len(self.get_calls), 1)

    def test_when_the_claiming_row_gives_up_a_duplicate_takes_over(self):
        """认领的那一行在冷却里（90 秒内刚刷过）：重复行自己接手、照常付钱。"""
        cooling = xhs("cooling")
        cooling.last_updated_ms = ms(NOW - timedelta(seconds=30))
        report = self.run_rows([cooling, xhs("due")])
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_COOLDOWN, runner.STATUS_OK])
        self.assertEqual(len(self.get_calls), 1)

    def test_a_single_worker_never_waits_on_a_row_queued_behind_it(self):
        """单线程 + 短链展开后指纹变成另一行认领的 ID：不能等那一行（它排在后面）。"""
        self.settings.max_concurrency = 1
        short = f"7.86 复制打开抖音 {SHORT_DY}"
        cooling = Row("u1", short, publish_time_ms=ms(NOW - timedelta(days=1)),
                      last_updated_ms=ms(NOW - timedelta(seconds=30)))
        taker = Row("u2", short, publish_time_ms=ms(NOW - timedelta(days=1)))
        by_id = Row("b", "https://www.douyin.com/video/7123456789012345678",
                    publish_time_ms=ms(NOW - timedelta(days=1)))
        done = {}

        def go():
            done["report"] = self.run_with(on_get=tikhub_douyin,
                                           on_post=lambda i: self.fail("走 TikHub"),
                                           on_expand=expand_ok,
                                           rows=[cooling, taker, by_id])

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "死锁了")
        self.assertEqual([o.status for o in done["report"].outcomes],
                         [runner.STATUS_COOLDOWN, runner.STATUS_OK, runner.STATUS_OK])

    def test_a_row_that_explodes_while_being_claimed_does_not_take_down_the_batch(self):
        real = runner.plan_calls

        def flaky(row, settings, now=None):
            if row.record_id == "boom":
                raise RuntimeError("坏行")
            return real(row, settings, now)

        with mock.patch.object(runner, "plan_calls", side_effect=flaky):
            report = self.run_rows([xhs("a"), xhs("boom", note="6a8538d7000000003300c037")])
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_OK, runner.STATUS_FAILED])


class TestSharedResultsCountOnceInTheBreaker(SharingTest):
    def test_three_rows_of_one_dead_note_are_one_observation(self):
        """一条死链登记三行：同一次上游回答，凑不满小样本熔断（≥3 行同码）的门槛。"""
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "all_sort_strategies": [],
                "user_id": ""}}}), "th-x")
        report = self.run_with(on_get=lambda i: empty_shell,
                               on_post=lambda i: self.fail("GONE 不降级"),
                               rows=[xhs("a"), xhs("b", token="2"), xhs("c", token="3")])
        self.assertEqual(len(self.get_calls), 1)
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(report.breaker_attempted, 1)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_SUSPECT] * 3)

    def test_three_distinct_dead_notes_still_trip_it(self):
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "all_sort_strategies": [],
                "user_id": ""}}}), "th-x")
        notes = [f"6a8538d{i}000000003300c037" for i in range(3)]
        report = self.run_with(on_get=lambda i: empty_shell,
                               on_post=lambda i: self.fail("GONE 不降级"),
                               rows=[xhs(f"r{i}", note=n) for i, n in enumerate(notes)])
        self.assertTrue(report.breaker_tripped)


class TestEstimatesDedupeAcrossTables(unittest.TestCase):
    def test_a_shared_seen_set_counts_a_note_once_across_tables(self):
        settings = Settings()
        seen: set = set()
        first = rows.paying_rows([xhs("a")], settings, NOW, seen=seen)
        second = rows.paying_rows([xhs("copy")], settings, NOW, seen=seen)
        self.assertEqual((len(first), len(second)), (1, 0))

    def test_the_panel_counts_it_once_too(self):
        settings = Settings()
        f = settings.fields
        due = [{"record_id": "r", "fields": {
            f.link: XHS_LINK, f.publish_time: ms(NOW - timedelta(days=1))}}]
        seen: set = set()
        one = summary.build_snapshot(label="甲", app_token="a", table_id="t1", records=due,
                                     settings=settings, now=NOW, api_keys=KEYS, seen_calls=seen)
        two = summary.build_snapshot(label="乙", app_token="a", table_id="t2", records=due,
                                     settings=settings, now=NOW, api_keys=KEYS, seen_calls=seen)
        self.assertGreater(one.due_yuan, 0)
        self.assertEqual(two.due_yuan, 0)
        self.assertEqual(two.due_rows, 1, "到期行数照报，只是不重复算钱")


class TestPanelReadsTheResolvedId(unittest.TestCase):
    def test_the_field_is_requested_and_used(self):
        settings = Settings()
        f = settings.fields
        self.assertIn(f.resolved_id, summary.panel_fields(settings))
        snap = summary.build_snapshot(
            label="甲", app_token="a", table_id="t", settings=settings, now=NOW,
            records=[{"record_id": "dy", "fields": {
                f.link: SHORT_DY,
                f.resolved_id: rows.format_resolved_id("7123456789012345678", SHORT_DY)}}])
        self.assertEqual(snap.missing_publish_time_rows, 0, "记过作品ID的短链算得出帖龄")
        self.assertEqual(snap.archived_rows, 1)


class TestFlatDailyUsesTheDetailWindow(unittest.TestCase):
    def test_only_young_xhs_posts_get_the_extra_detail(self):
        from tools import estimate_cost
        lean = estimate_cost.flat_daily(620, 0.7)
        windowed = estimate_cost.flat_daily(620, 0.7, xhs_detail_posts=20 * 7 * 0.7)
        self.assertAlmostEqual(windowed["calls_per_day"] - lean["calls_per_day"], 98)


class TestConcurrentResumeWaitsForFreshData(unittest.TestCase):
    def test_a_second_viewer_during_resume_gets_the_new_snapshot(self):
        gate = threading.Event()
        entered = threading.Event()
        produced = []

        def produce():
            produced.append(1)
            if len(produced) == 2:          # 第一趟是开机那趟；第二趟是恢复那趟
                entered.set()
                gate.wait(5)
            return summary.Overview(projects=[], generated_at=NOW + timedelta(minutes=len(produced)))

        clock = FakeClock()
        cache = panel.Cache(produce, ttl=60, idle_seconds=600, clock=clock)
        cache.refresh()
        clock.now += 601
        with cache._lock:
            cache._paused = True
        results = {}

        def viewer(name):
            results[name] = cache.view()[0]

        first = threading.Thread(target=viewer, args=("first",), daemon=True)
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=viewer, args=("second",), daemon=True)
        second.start()
        second.join(timeout=0.2)
        self.assertTrue(second.is_alive(), "第二个请求不该拿着旧快照先走")
        gate.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(produced), 2, "并发进来的几个请求只取一趟")
        self.assertEqual(results["first"].generated_at, results["second"].generated_at)
        self.assertEqual(results["second"].generated_at, NOW + timedelta(minutes=2))


class TestBlindRowsAreNotPatrolled(unittest.TestCase):
    """「发布时间」空着、链接里也解不出发布时刻的行：sweep 不查、不搭车，
    排队刷新照常（Ziao 2026-10-16 拍板）。面板的「到期 / 预计花费」和 sweep
    同一个口径，项目卡上「没填发布时间」数的就是它们。"""

    SHORT = "http://xhslink.com/a/AbC"

    def setUp(self):
        self.settings = Settings()

    def table(self, records):
        class _Table:
            def search(self, field_names, *, filter_spec=None, max_records=None):
                return records
        return _Table()

    def record(self, record_id, link, *, published=True, queued=False, checked_hours_ago=None):
        f = self.settings.fields
        cells = {f.link: link}
        if published:
            cells[f.publish_time] = int((NOW - timedelta(days=1)).timestamp() * 1000)
        if checked_hours_ago is not None:
            cells[f.last_updated] = int((NOW - timedelta(hours=checked_hours_ago)).timestamp() * 1000)
        if queued:
            cells[f.queued] = True
        return {"record_id": record_id, "fields": cells}

    def test_sweep_skips_it(self):
        picked = runner.load_rows(self.table([
            self.record("blind", self.SHORT, published=False),
            self.record("filled", self.SHORT),
        ]), self.settings, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["filled"])

    def test_queue_still_takes_it(self):
        """勾「排队刷新」是人明确要看这一行，不看到期。"""
        picked = runner.load_rows(self.table([
            self.record("blind", self.SHORT, published=False, queued=True),
        ]), self.settings, only_queued=True, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["blind"])

    def test_it_does_not_ride_along_with_a_due_sibling(self):
        """同篇搭车是免费的，但「不巡查」就是不碰它。两行是同一篇（"a"*24），
        这个 ID 解出来在 2060 年、不算数——没填发布时间的那行就是帖龄算不出。"""
        note = "https://www.xiaohongshu.com/explore/" + "a" * 24
        picked = runner.load_rows(self.table([
            self.record("due", note, checked_hours_ago=30),
            self.record("blind_sibling", note, published=False, checked_hours_ago=1),
        ]), self.settings, now=NOW)
        self.assertEqual([r.record_id for r in picked], ["due"])

    def test_the_panel_counts_it_but_does_not_price_it(self):
        snap = summary.build_snapshot(
            label="A", app_token="bascnA", table_id="tblA",
            records=[self.record("blind", self.SHORT, published=False)],
            settings=self.settings, now=NOW)
        self.assertEqual(snap.missing_publish_time_rows, 1)
        self.assertEqual(snap.due_rows, 0, "面板的到期数要和 sweep 一个口径")
