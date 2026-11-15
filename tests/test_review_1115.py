"""2026-11-15 外部复核那一轮改的四件事。全部离线，评论正文都是编的。

1. 二连限流、手里没数据的行：不写回、不算失败，留给下一轮。
2. 金额闸门的悲观预留：一行可能被两家都收一次钱，按两家之和预留。
3. 「评论数观测时间」：只在这一轮真的量到评论数时写。
4. 「评论区明细」：评论区快照的机器可读版（一行 JSON）。
"""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import analyze, provision, rows, runner, schema, transport
from xhsearch.config import Settings
from xhsearch.rows import Row

from tests.test_failover import (
    KEYS, NOW, FailoverTest, note_id, sdx_err, sdx_ok_comments, tikhub_err,
    tikhub_ok_comments, tikhub_ok_detail, xhs_row,
)
from tests.test_provision import full_meta


def limited() -> transport.Response:
    return tikhub_err(429, "请求过于频繁")


def tikhub_garbage() -> transport.Response:
    """HTTP 200、成功信封，但 data 不是对象：TikHub 照样扣钱，东西认不出来。"""
    return transport.Response(200, "application/json",
                              json.dumps({"code": 200, "data": "upstream changed"}), "th-bad")


class FakeTable:
    def __init__(self):
        self.updates = None

    def batch_get(self, record_ids):
        return [{"record_id": rid, "fields": {}} for rid in record_ids]

    def batch_update(self, updates, errors=None):
        self.updates = updates
        return len(updates)


# ------------------------------------------------------------ 1. 二连限流


class TestSecondRateLimitDefersTheRow(FailoverTest):
    """退避一次再试还是 429、手里一点数据都没有：以前写成「刷新失败」，
    盖了「最近检查时间」、清了「排队刷新」的勾——这一行要等一整档才再查，
    运营勾的排队也像是处理完了。"""

    def test_nothing_is_written_and_it_is_not_a_failed_check(self):
        row = xhs_row()
        row.queued = True
        report = self.run_with(on_get=lambda i: limited(),
                               on_post=lambda i: self.fail("限流不换通道"),
                               rows=[row])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_DEFERRED)
        self.assertEqual(outcome.fields, {})         # 时间戳、排队勾、状态一个字都没动
        self.assertIsNone(outcome.checked_at)
        self.assertEqual(len(self.get_calls), 2)     # 一次 + 退避后重试一次
        self.assertNotIn(runner.STATUS_FAILED, report.counts())
        self.assertEqual(report.rate_limited_rows, 1)
        self.assertIn("上游限流", report.aborted_reason)
        self.assertIn("上游限流", report.summary())
        self.assertFalse(report.fatal)
        # 不进熔断的样本：这一轮对「内容还在不在」什么都没看到。
        self.assertEqual(report.breaker_attempted, 0)

    def test_write_back_sends_nothing_for_the_row(self):
        report = self.run_with(on_get=lambda i: limited(),
                               on_post=lambda i: self.fail("限流不换通道"))
        table = FakeTable()
        self.assertEqual(runner.write_back(table, report), 0)
        self.assertIsNone(table.updates)

    def test_a_rate_limited_detail_still_finishes_the_row(self):
        """评论已经到手，只是后面的 detail 被限流：照常按已有数据下结论。"""
        answers = [tikhub_ok_comments(), limited(), limited()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("限流不换通道"))
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertEqual(outcome.fields[self.settings.fields.comment_count], 150)
        self.assertEqual(report.rate_limited_rows, 0)

    def test_one_retry_that_works_is_a_normal_row(self):
        answers = [limited(), tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("不该降级"))
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.rate_limited_rows, 0)
        self.assertEqual(report.aborted_reason, "")

    def test_a_persistent_limit_asks_the_remaining_rows_only_once(self):
        """连着几行都是二连限流：这一轮剩下的行各问一次，被限流就直接留给下一轮，
        不再睡退避、不再重试。"""
        batch = [xhs_row(f"r{i}") for i in range(6)]
        report = self.run_with(on_get=lambda i: limited(),
                               on_post=lambda i: self.fail("限流不换通道"),
                               rows=batch)
        pause = runner.RATE_LIMIT_ROWS_BEFORE_PAUSE
        # 前几行各两次（中间睡一次退避），后面的行各一次（重试只跟在退避后面）。
        self.assertEqual(len(self.get_calls), 2 * pause + (6 - pause))
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_DEFERRED] * 6)
        self.assertTrue(all(o.fields == {} for o in report.outcomes))
        self.assertEqual(report.rate_limited_rows, 6)
        self.assertIn("只问了一次", report.outcomes[-1].reason)

    def test_limited_rows_at_the_head_do_not_starve_the_healthy_rows(self):
        """限流只粘在排最前面的几行上（它们的时间戳没动，每一轮都排第一）：
        后面健康的行照样刷到。整个平台一停，就是每一轮都被这几行堵死。"""
        self.settings.max_concurrency = 2
        stuck = [xhs_row(f"stuck{i}") for i in range(4)]
        healthy = [xhs_row(f"ok{i}") for i in range(8)]
        stuck_ids = [note_id(r.record_id) for r in stuck]

        def answer(i):
            url = self.get_calls[i]
            if any(n in url for n in stuck_ids):
                return limited()
            return tikhub_ok_comments() if "comments" in url else tikhub_ok_detail()

        for _ in range(2):      # 下一轮这几行还排在最前面，结果一样
            report = self.run_with(on_get=answer,
                                   on_post=lambda i: self.fail("限流不换通道"),
                                   rows=stuck + healthy)
            self.assertEqual([o.status for o in report.outcomes],
                             [runner.STATUS_DEFERRED] * 4 + [runner.STATUS_OK] * 8)
            self.assertTrue(all(o.fields for o in report.outcomes[4:]))
            self.assertEqual(report.rate_limited_rows, 4)
            self.assertIn("4 行未处理（上游限流）", report.aborted_reason)

    def test_limited_rows_do_not_use_up_the_row_cap(self):
        """行数上限是 3，排最前面的 3 行持续被限流：它们占的名额要还回去，
        否则后面那一行健康的每一轮都轮不到。"""
        self.settings.max_concurrency = 1
        stuck = [xhs_row(f"stuck{i}") for i in range(3)]
        stuck_ids = [note_id(r.record_id) for r in stuck]

        def answer(i):
            url = self.get_calls[i]
            if any(n in url for n in stuck_ids):
                return limited()
            return tikhub_ok_comments() if "comments" in url else tikhub_ok_detail()

        for _ in range(2):
            budget = runner.RunBudget(runner.Budget(max_records_per_run=3))
            report = self.run_with(on_get=answer,
                                   on_post=lambda i: self.fail("限流不换通道"),
                                   rows=stuck + [xhs_row("ok0")], budget=budget)
            self.assertEqual([o.status for o in report.outcomes],
                             [runner.STATUS_DEFERRED] * 3 + [runner.STATUS_OK])
            self.assertEqual(budget.records, 1)

    def test_a_real_answer_brings_the_backoff_back(self):
        """只问一次的那一段里有一行拿到了真回答：后面的行恢复退避重试。"""
        pause = runner.RATE_LIMIT_ROWS_BEFORE_PAUSE
        answers = ([limited()] * (2 * pause) + [tikhub_ok_comments(), tikhub_ok_detail()]
                   + [limited(), tikhub_ok_comments(), tikhub_ok_detail()])
        batch = [xhs_row(f"r{i}") for i in range(pause + 2)]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("限流不换通道"),
                               rows=batch)
        self.assertEqual(len(self.get_calls), len(answers))
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_DEFERRED] * pause + [runner.STATUS_OK] * 2)

    def test_a_row_that_already_cost_money_is_stamped_not_deferred(self):
        """主通道收了钱却给了认不出的东西，降到备胎又被二连限流：这一行付过钱了。
        不盖时间戳的话，每 5 分钟一轮都会把它再买一遍，表里什么都不留。"""
        row = xhs_row()
        row.queued = True
        report = self.run_with(on_get=lambda i: tikhub_garbage(),
                               on_post=lambda i: sdx_err(429, 429, "rate limited"),
                               rows=[row])
        outcome = report.outcomes[0]
        f = self.settings.fields
        self.assertGreater(report.cost_yuan, 0)
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertIsNotNone(outcome.checked_at)
        self.assertIn(f.last_updated, outcome.fields)     # 盖了戳：等它的下一档
        self.assertEqual(report.rate_limited_rows, 0)
        self.assertEqual(report.aborted_reason, "")
        # 下一轮：盖过戳的行不到期，一个请求都不发、一分钱不花。
        stamped = xhs_row()
        stamped.last_updated_ms = outcome.fields[f.last_updated]
        # 戳是写回那一刻的真实时间（不是固件里的 NOW），下一轮从它往后数 5 分钟。
        later = (datetime.fromtimestamp(stamped.last_updated_ms / 1000, timezone.utc)
                 + timedelta(minutes=5))
        self.assertFalse(stamped.is_due(self.settings, later))
        self.assertEqual(rows.estimate_yuan(
            [r for r in [stamped] if r.is_due(self.settings, later)],
            self.settings, later, keys=KEYS), 0)

    def test_the_same_limit_with_nothing_spent_is_still_deferred(self):
        """只配了备胎、它二连限流：一分钱没花，照旧不写回。"""
        report = self.run_with(on_get=lambda i: self.fail("没配主通道"),
                               on_post=lambda i: sdx_err(429, 429, "rate limited"),
                               keys={"socialdatax": "s-key"})
        self.assertEqual(report.cost_yuan, 0)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_DEFERRED)
        self.assertEqual(report.outcomes[0].fields, {})

    def test_a_duplicate_of_a_billed_limited_row_is_stamped_too(self):
        """同一篇登记了两行：付钱的那一行盖了戳，抄它结果的那一行也盖——
        不然下一轮它自己去把同一篇再买一遍。"""
        same = [Row(record_id=f"dup{i}",
                    link_cell="https://www.xiaohongshu.com/explore/" + note_id("same"),
                    publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))
                for i in range(2)]
        report = self.run_with(on_get=lambda i: tikhub_garbage(),
                               on_post=lambda i: sdx_err(429, 429, "rate limited"),
                               rows=same)
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_FAILED] * 2)

    def test_a_real_answer_in_between_starts_the_count_again(self):
        answers = ([limited()] * 4 + [tikhub_ok_comments(), tikhub_ok_detail()]
                   + [limited()] * 4 + [tikhub_ok_comments(), tikhub_ok_detail()])
        batch = [xhs_row(f"r{i}") for i in range(6)]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("限流不换通道"),
                               rows=batch)
        self.assertEqual(len(self.get_calls), len(answers))
        self.assertEqual([o.status for o in report.outcomes],
                         [runner.STATUS_DEFERRED, runner.STATUS_DEFERRED, runner.STATUS_OK,
                          runner.STATUS_DEFERRED, runner.STATUS_DEFERRED, runner.STATUS_OK])

    def test_rows_of_the_same_post_count_as_one_limit(self):
        """同一篇登记了几行：抄来的是同一次限流，不能凑满「连着几行」。"""
        same = [Row(record_id=f"dup{i}",
                    link_cell="https://www.xiaohongshu.com/explore/" + note_id("same"),
                    publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))
                for i in range(4)]
        answers = [limited(), limited(), tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("限流不换通道"),
                               rows=same + [xhs_row("other")])
        self.assertEqual([o.status for o in report.outcomes[:4]],
                         [runner.STATUS_DEFERRED] * 4)
        self.assertEqual(report.outcomes[4].status, runner.STATUS_OK)

    def test_other_deferrals_keep_their_own_reason(self):
        """预算触顶的行还是报预算，不被说成限流。"""
        self.settings.budget.max_records_per_run = 1
        answers = [tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("不该降级"),
                               rows=[xhs_row("a"), xhs_row("b")])
        self.assertIn("行数上限", report.aborted_reason)
        self.assertNotIn("限流", report.aborted_reason)
        self.assertEqual(report.rate_limited_rows, 0)


# ------------------------------------------------------------ 2. 悲观预留


class TestWorstCaseCoversEveryChannelThatCanBill(FailoverTest):
    TIKHUB_XHS = 0.01 * 7.2
    SDX = 0.10

    def setUp(self):
        super().setUp()
        self.settings.detail_within_days = 0     # 一行一次评论调用，数好算

    def worst(self, **kwargs):
        return rows.estimate_yuan([xhs_row()], self.settings, NOW, worst_case=True, **kwargs)

    def test_primary_that_bills_failures_plus_the_backup(self):
        self.assertAlmostEqual(self.worst(keys=KEYS), self.TIKHUB_XHS + self.SDX)

    def test_a_single_channel_is_just_its_own_price(self):
        self.assertAlmostEqual(self.worst(keys={"tikhub": "t"}), self.TIKHUB_XHS)
        self.assertAlmostEqual(self.worst(keys={"socialdatax": "s"}), self.SDX)

    def test_a_channel_already_out_this_run_is_not_reserved(self):
        self.assertAlmostEqual(self.worst(keys=KEYS, disabled={"tikhub"}), self.SDX)

    def test_a_primary_that_does_not_bill_failures_is_not_added(self):
        """SocialDataX 失败不收费：它失败了再换 TikHub，只有 TikHub 那一笔。"""
        self.settings.channels.order["xhs"] = ["socialdatax", "tikhub"]
        self.assertAlmostEqual(self.worst(keys=KEYS), max(self.SDX, self.TIKHUB_XHS))

    def test_the_optimistic_number_is_unchanged(self):
        self.assertAlmostEqual(
            rows.estimate_yuan([xhs_row()], self.settings, NOW, keys=KEYS), self.TIKHUB_XHS)

    def test_a_cap_between_the_two_prices_is_no_longer_crossed(self):
        """复核时离线复现的那一幕：上限 0.10，TikHub 回 200 却认不出来（照扣），
        再降到 SocialDataX 成功，一行花了 0.172。现在这一行根本不放行。"""
        self.settings.budget.max_yuan_per_run = 0.10
        report = self.run_with(on_get=lambda i: tikhub_garbage(),
                               on_post=lambda i: sdx_ok_comments())
        self.assertEqual(report.outcomes[0].status, runner.STATUS_DEFERRED)
        self.assertEqual(self.get_calls + self.post_calls, [])
        self.assertEqual(report.cost_yuan, 0)
        self.assertIn("金额上限", report.budget_stopped)

    def test_spend_stays_under_the_cap_when_both_channels_bill(self):
        self.settings.budget.max_yuan_per_run = 0.30
        batch = [xhs_row(f"r{i}") for i in range(4)]
        report = self.run_with(on_get=lambda i: tikhub_garbage(),
                               on_post=lambda i: sdx_ok_comments(), rows=batch)
        self.assertLessEqual(report.cost_yuan, 0.30 + 1e-9)
        self.assertAlmostEqual(report.cost_yuan, self.TIKHUB_XHS + self.SDX)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual([o.status for o in report.outcomes[1:]],
                         [runner.STATUS_DEFERRED] * 3)

    def test_the_difference_is_given_back_after_a_normal_row(self):
        """平常主通道一次就成：多预留的那一截跑完就退，不压后面的行。
        三行实花 0.216；不退的话第三行预留时账上是 2 × 0.172，放不进 0.35。"""
        self.settings.budget.max_yuan_per_run = 0.35
        batch = [xhs_row(f"r{i}") for i in range(3)]
        report = self.run_with(on_get=lambda i: tikhub_ok_comments(),
                               on_post=lambda i: self.fail("不该降级"), rows=batch)
        self.assertEqual([o.status for o in report.outcomes], [runner.STATUS_OK] * 3)
        self.assertAlmostEqual(report.cost_yuan, 3 * self.TIKHUB_XHS)


# ------------------------------------------------------------ 3. 评论数观测时间


class TestCountObservedAt(FailoverTest):
    def test_a_measured_round_writes_it_with_the_same_moment_as_the_check_time(self):
        f = self.settings.fields
        answers = [tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("不该降级"))
        fields = report.outcomes[0].fields
        self.assertEqual(f.count_observed_at, "评论数观测时间")
        self.assertEqual(fields[f.count_observed_at], fields[f.last_updated])
        self.assertIsInstance(fields[f.count_observed_at], int)

    def test_a_failed_round_moves_the_check_time_only(self):
        f = self.settings.fields
        report = self.run_with(
            on_get=lambda i: transport.Response(500, "application/json", "{}"),
            on_post=lambda i: transport.Response(500, "application/json", "{}"))
        fields = report.outcomes[0].fields
        self.assertEqual(report.outcomes[0].status, runner.STATUS_FAILED)
        self.assertIn(f.last_updated, fields)
        self.assertNotIn(f.count_observed_at, fields)
        self.assertNotIn(f.comment_count, fields)

    def test_a_gone_round_does_not_write_it(self):
        f = self.settings.fields
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "comment_count_l1": 0,
                "all_sort_strategies": [], "user_id": ""}}}), "th-gone")
        report = self.run_with(on_get=lambda i: empty_shell,
                               on_post=lambda i: self.fail("GONE 不该触发降级"))
        outcome = report.outcomes[0]
        self.assertIn(outcome.status, (runner.STATUS_SUSPECT, runner.STATUS_GONE))
        self.assertIn(f.last_updated, outcome.fields)
        self.assertNotIn(f.count_observed_at, outcome.fields)

    def test_it_is_written_exactly_when_the_count_is(self):
        """评论页有条目、评论数却没报（null）：数没量到，这一格也不动。"""
        f = self.settings.fields
        self.settings.detail_within_days = 0
        page = transport.Response(200, "application/json", json.dumps({
            "items": [{"content": "合成评论甲", "like_count": 1, "is_pinned": False,
                       "is_author_comment": False}],
            "comment_count": None, "points": {"cost": 10, "balance": 1}}), "sdx-null")
        report = self.run_with(on_get=lambda i: self.fail("只配了备胎"),
                               on_post=lambda i: page, keys={"socialdatax": "s"})
        fields = report.outcomes[0].fields
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertNotIn(f.comment_count, fields)
        self.assertNotIn(f.count_observed_at, fields)
        self.assertIn(f.comment_digest, fields)      # 评论页是看到了的
        self.assertIn(f.comment_records, fields)

    def test_it_is_not_written_when_the_count_cannot_be(self):
        """评论数那一列写不进去（表里是公式列，或者根本没有这一列）：
        观测时间也不写——不给一个没写进去的数盖新时间。"""
        f = self.settings.fields
        answers = [tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("不该降级"))
        self.assertIn(f.count_observed_at, report.outcomes[0].fields)
        table, bad = FakeTable(), set()
        runner.write_back(table, report, field_types={f.comment_count: 20, f.count_observed_at: 5},
                          mistyped_fields=bad)
        self.assertEqual(bad, {f.comment_count})
        self.assertNotIn(f.count_observed_at, table.updates[0]["fields"])
        table = FakeTable()
        known = set(report.outcomes[0].fields) - {f.comment_count}
        runner.write_back(table, report, known_fields=known)
        self.assertNotIn(f.count_observed_at, table.updates[0]["fields"])

    def test_a_table_without_the_new_columns_is_written_as_before(self):
        f = self.settings.fields
        answers = [tikhub_ok_comments(), tikhub_ok_detail()]
        report = self.run_with(on_get=lambda i: answers[i],
                               on_post=lambda i: self.fail("不该降级"))
        known = set(report.outcomes[0].fields) - {f.count_observed_at, f.comment_records}
        table, dropped = FakeTable(), set()
        written = runner.write_back(table, report, known_fields=known, dropped_fields=dropped)
        self.assertEqual(written, 1)
        self.assertEqual(dropped, {f.count_observed_at, f.comment_records})
        self.assertEqual(set(table.updates[0]["fields"]), known)

    def test_the_schema_knows_both_columns_and_the_panel_can_build_them(self):
        f = self.settings.fields
        expected = {name: allowed for name, allowed, *_ in
                    schema.expected_schema(self.settings)}
        self.assertEqual(expected[f.count_observed_at], (5,))
        self.assertEqual(expected[f.comment_records], (1,))
        names = (f.count_observed_at, f.comment_records)
        meta = full_meta(self.settings, drop=names)
        missing = schema.diff(self.settings, meta).missing_columns
        self.assertEqual({c.name: c.type_code for c in missing},
                         {f.count_observed_at: 5, f.comment_records: 1})
        self.assertTrue(schema.diff(self.settings, full_meta(self.settings)).clean)

    def test_an_old_table_without_them_is_still_healthy(self):
        """只给下游用的两列：老表缺了不算体检问题——不把每张老表的项目卡染红、
        催运营挨个去点。只说一句。"""
        f = self.settings.fields
        names = (f.count_observed_at, f.comment_records)
        meta = full_meta(self.settings, drop=names)
        self.assertEqual(schema.schema_problems(self.settings, meta), [])
        self.assertEqual(schema.downstream_missing(self.settings, meta), list(names))
        note = schema.downstream_note(list(names))
        for name in names:
            self.assertIn(name, note)
        self.assertIn("补齐缺的列", note)
        self.assertEqual(schema.downstream_note([]), "")
        self.assertEqual(schema.downstream_missing(self.settings, full_meta(self.settings)), [])

    def test_built_with_the_wrong_type_is_still_reported(self):
        """建了但类型不对：每一轮都会被类型闸摘掉、退出码变红，得说。"""
        f = self.settings.fields
        meta = full_meta(self.settings, retype={f.count_observed_at: 1001,
                                                f.comment_records: 2})
        problems = " ".join(schema.schema_problems(self.settings, meta))
        self.assertIn(f.count_observed_at, problems)
        self.assertIn(f.comment_records, problems)

    def test_new_tables_are_built_with_both(self):
        f = self.settings.fields
        for template in provision.TEMPLATES:
            fields, _deferred, _skipped = provision.template_fields(self.settings, template)
            by_name = {body["field_name"]: body for body in fields}
            self.assertEqual(by_name[f.count_observed_at]["type"], 5, template)
            self.assertEqual(by_name[f.comment_records]["type"], 1, template)

    def test_the_machine_does_not_need_to_read_them(self):
        f = self.settings.fields
        self.assertNotIn(f.count_observed_at, f.must_read())
        self.assertNotIn(f.comment_records, f.must_read())


# ------------------------------------------------------------ 4. 评论区明细


def synthetic_page(extra_items=()):
    """一页合成评论（SocialDataX 归一化之后的形状）。"""
    items = [
        {"content": "合成置顶  评论\n第一条", "like_count": 12, "is_pinned": True,
         "is_author_comment": True, "ip_location": "上海", "author": {"name": "合成昵称甲"},
         "comment_id": "c-001", "create_time": 1_760_000_000,
         "reply_count": 1,
         "replies": [{"content": "合成回复一条", "like_count": 2, "is_pinned": True,
                      "is_author_comment": False, "author": {"name": "合成昵称丙"},
                      "comment_id": "c-001-r1", "create_time": 1_760_000_500_000}]},
        {"content": "这个 TestWord 好用吗" + "很" * 300, "like_count": 0,
         "is_pinned": False, "is_author_comment": False, "ip_location": "广东",
         "author": {"name": "合成昵称乙"}, "comment_id": "c-002"},
        *extra_items,
    ]
    return {"items": items, "comment_count": 37, "top_level_comment_count": 30}


class TestFormatRecords(unittest.TestCase):
    KEYS = {"id", "ts", "a", "p", "l", "k", "r", "x", "tr"}

    def parse(self, snapshot, **kwargs):
        kwargs.setdefault("observed_at_ms", 1_763_000_000_000)
        text = analyze.format_records(snapshot, **kwargs)
        self.assertNotIn("\n", text)
        return text, json.loads(text)

    def test_every_comment_carries_the_fixed_keys(self):
        snapshot = analyze.read_comment_page("xhs", synthetic_page())
        text, data = self.parse(snapshot, seed_keywords=["testword"])
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["t"], 1_763_000_000_000)
        self.assertEqual(data["n"], 37)
        self.assertEqual((data["kw"], data["ar"], data["pr"]), (1, 1, 1))
        self.assertEqual(data["lim"], analyze.RECORD_TEXT_CHARS)
        self.assertEqual(data["more"], 0)
        self.assertEqual(len(data["c"]), 3)          # 两条一级评论 + 一条露出来的回复
        for record in data["c"]:
            self.assertEqual(set(record), self.KEYS)
        first, second, reply = data["c"]
        self.assertEqual(first, {"id": "c-001", "ts": 1_760_000_000_000, "a": 1, "p": 1,
                                 "l": 12, "k": 0, "r": "", "x": "合成置顶 评论 第一条",
                                 "tr": 0})
        # 上游没给发布时刻：留 null，不拿发现时刻代填。
        self.assertIsNone(second["ts"])
        self.assertEqual((second["a"], second["p"], second["k"]), (0, 0, 1))
        self.assertEqual(second["tr"], 1)
        self.assertEqual(len(second["x"]), analyze.RECORD_TEXT_CHARS)
        # 回复：带着父评论的 ID，不算置顶。
        self.assertEqual((reply["id"], reply["r"], reply["p"]), ("c-001-r1", "c-001", 0))
        self.assertEqual(reply["ts"], 1_760_000_500_000)
        # 昵称、IP 属地一个字都不落进来。
        for private in ("合成昵称", "上海", "广东"):
            self.assertNotIn(private, text)

    def test_no_keywords_means_unknown_not_clean(self):
        snapshot = analyze.read_comment_page("xhs", synthetic_page())
        _, data = self.parse(snapshot)
        self.assertEqual(data["kw"], 0)
        self.assertEqual({r["k"] for r in data["c"]}, {0})

    def test_an_untrusted_channel_says_so(self):
        """备胎通道的置顶 / 作者标记不可信：整格标出来，下游别信那两个键。"""
        page = dict(synthetic_page(), _capabilities={"pinned": False, "author": False})
        _, data = self.parse(analyze.read_comment_page("xhs", page))
        self.assertEqual((data["ar"], data["pr"]), (0, 0))

    def test_douyin_has_no_pin_and_knows_the_author_only_by_account(self):
        page = {"items": [{"content": "合成抖音评论", "comment_id": "7001",
                           "create_time": 1_760_000_000, "author_ids": ["u-1"]}],
                "comment_count": 1}
        snapshot = analyze.read_comment_page("douyin", page)
        _, data = self.parse(snapshot)
        self.assertEqual((data["ar"], data["pr"], data["c"][0]["a"]), (0, 0, 0))
        snapshot.post_author_ids = ("u-1",)
        _, data = self.parse(snapshot)
        self.assertEqual((data["ar"], data["c"][0]["a"]), (1, 1))

    def test_an_empty_page_is_still_a_record(self):
        snapshot = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        _, data = self.parse(snapshot)
        self.assertEqual((data["n"], data["hid"], data["c"]), (0, 0, []))

    def test_comments_nobody_can_see_are_counted_not_listed(self):
        page = {"items": [], "comment_count": 3, "top_level_comment_count": 3,
                "_empty_page": True}
        snapshot = analyze.read_comment_page("xhs", page)
        _, data = self.parse(snapshot)
        self.assertEqual((data["n"], data["hid"], data["c"]), (0, 3, []))

    def test_the_cell_is_capped_and_says_how_many_were_left_out(self):
        extra = [{"content": f"合成长评论{i}" + "字" * 400, "comment_id": f"x-{i:03d}",
                  "create_time": 1_760_000_000 + i,
                  "replies": [{"content": "合成回复" + "字" * 400,
                               "comment_id": f"x-{i:03d}-r"}]}
                 for i in range(30)]
        snapshot = analyze.read_comment_page("xhs", synthetic_page(extra))
        text, data = self.parse(snapshot)
        self.assertLessEqual(len(text), analyze.RECORDS_MAX_CHARS)
        self.assertGreater(data["more"], 0)
        self.assertEqual(data["more"] + len(data["c"]), 2 + 1 + 60)
        # 丢是从末尾丢：一级评论按页面顺序留在前面。
        self.assertEqual(data["c"][0]["id"], "c-001")

    def test_the_cap_is_far_inside_what_one_feishu_cell_holds(self):
        from xhsearch import panel_settings
        self.assertLess(analyze.RECORDS_MAX_CHARS, panel_settings.CELL_MAX_CHARS // 10)


class TestRecordsInTheRunner(FailoverTest):
    def tikhub_page(self):
        return transport.Response(200, "application/json", json.dumps({
            "code": 200, "request_id": "th-1", "data": {"code": 0, "success": True, "data": {
                "comments": [
                    {"id": "aa01", "content": "合成评论 提到了 SeedWord", "like_count": 4,
                     "create_time": 1_760_000_000_000, "ip_location": "Shanghai",
                     "show_tags_v2": [{"type": "user_top"}, {"type": "is_author"}],
                     "user": {"nickname": "合成昵称甲"},
                     "sub_comment_count": "1",
                     "sub_comments": [{"id": "aa01-1", "content": "合成回复",
                                       "create_time": 1_760_000_100_000,
                                       "user": {"nickname": "合成昵称乙"}}]},
                    {"id": "aa02", "content": "另一条合成评论", "like_count": 0,
                     "user": {"nickname": "合成昵称丙"}},
                ],
                "comment_count": 3, "comment_count_l1": 2,
                "all_sort_strategies": [{"type": "default"}], "user_id": "u9"}}},
            ensure_ascii=False), "th-1")

    def test_it_is_written_next_to_the_digest_with_the_check_moment(self):
        f = self.settings.fields
        self.settings.detail_within_days = 0
        row = xhs_row()
        row.seed_keywords = ["seedword"]
        report = self.run_with(on_get=lambda i: self.tikhub_page(),
                               on_post=lambda i: self.fail("不该降级"), rows=[row])
        fields = report.outcomes[0].fields
        self.assertEqual(f.comment_records, "评论区明细")
        self.assertIn(f.comment_digest, fields)
        data = json.loads(fields[f.comment_records])
        self.assertEqual(data["t"], fields[f.last_updated])
        self.assertEqual(data["n"], 3)
        self.assertEqual([c["id"] for c in data["c"]], ["aa01", "aa02", "aa01-1"])
        self.assertEqual([c["k"] for c in data["c"]], [1, 0, 0])
        self.assertEqual([c["r"] for c in data["c"]], ["", "", "aa01"])
        self.assertEqual((data["c"][0]["a"], data["c"][0]["p"]), (1, 1))
        self.assertEqual(data["c"][0]["ts"], 1_760_000_000_000)
        self.assertNotIn("合成昵称", fields[f.comment_records])
        self.assertLessEqual(len(fields[f.comment_records]), analyze.RECORDS_MAX_CHARS)

    def test_a_round_that_did_not_see_the_page_leaves_both_cells_alone(self):
        """评论多、上游这一页却没给条目：快照不动，明细也不动（评论数照写）。"""
        f = self.settings.fields
        self.settings.detail_within_days = 0
        page = transport.Response(200, "application/json", json.dumps({
            "items": [], "comment_count": 150, "top_level_comment_count": 120,
            "points": {"cost": 10, "balance": 1}}), "sdx-empty")
        report = self.run_with(on_get=lambda i: self.fail("只配了备胎"),
                               on_post=lambda i: page, keys={"socialdatax": "s"})
        fields = report.outcomes[0].fields
        self.assertEqual(fields[f.comment_count], 150)
        self.assertIn(f.count_observed_at, fields)
        self.assertNotIn(f.comment_digest, fields)
        self.assertNotIn(f.comment_records, fields)

    def test_a_failed_round_does_not_touch_it(self):
        f = self.settings.fields
        report = self.run_with(
            on_get=lambda i: transport.Response(500, "application/json", "{}"),
            on_post=lambda i: transport.Response(500, "application/json", "{}"))
        self.assertNotIn(f.comment_records, report.outcomes[0].fields)

    def test_a_text_column_takes_the_value(self):
        """写回的类型闸：文本列收字符串，日期列收毫秒数。"""
        from xhsearch import feishu
        f = self.settings.fields
        self.settings.detail_within_days = 0
        report = self.run_with(on_get=lambda i: self.tikhub_page(),
                               on_post=lambda i: self.fail("不该降级"))
        fields = report.outcomes[0].fields
        self.assertTrue(feishu.value_fits(1, fields[f.comment_records]))
        self.assertTrue(feishu.value_fits(5, fields[f.count_observed_at]))


class TestOldTablesAreNotNagged(unittest.TestCase):
    """命令行那一层：老表缺下游用的两列，体检不计问题、写回不当警告、退出码不变。"""

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields
        self.names = (self.f.count_observed_at, self.f.comment_records)

    class _Table:
        def __init__(self, meta):
            self._meta = meta

        def token(self):
            return "t"

        def fields_meta(self):
            return self._meta

        def search(self, field_names, *, filter_spec=None, max_records=None):
            return []

        def batch_get(self, ids):
            return [{"record_id": i, "fields": {}} for i in ids]

        def batch_update(self, updates, errors=None):
            self.updates = updates
            return len(updates)

    def test_doctor_counts_no_problem_and_says_one_line(self):
        import cli
        table = self._Table(full_meta(self.settings, drop=self.names))
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            count = cli._doctor_table(self.settings, table)
        text = out.getvalue()
        self.assertEqual(count, 0, text)
        self.assertIn("检查通过", text)
        self.assertIn("给下游数据仓库用的列还没建", text)
        for name in self.names:
            self.assertIn(name, text)

    def test_doctor_is_silent_once_they_exist(self):
        import cli
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            count = cli._doctor_table(self.settings, self._Table(full_meta(self.settings)))
        self.assertEqual(count, 0)
        self.assertNotIn("下游数据仓库", out.getvalue())

    def _write_back(self, meta):
        import cli
        f = self.f

        class _Outcome:
            tag_plan = None
            blue_word_plan = None
            record_id = "rec1"
            checked_at = None
            fields = {f.refresh_status: "正常", f.last_updated: 1787313600000,
                      f.queued: False, f.comment_count: 12,
                      f.count_observed_at: 1787313600000,
                      f.comment_records: '{"v":1}', f.surge_time: 1787313600000}

        class _Report:
            fatal = False
            outcomes = [_Outcome()]

            def summary(self):
                return ""

            def checked_span(self, display, *, skip=()):
                return ""

        table = self._Table(meta)
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = cli._write_back_table(table, _Report(), meta, self.settings, "表A")
        return code, out.getvalue(), table.updates[0]["fields"]

    def test_write_back_skips_them_quietly_and_exits_green(self):
        code, text, written = self._write_back(full_meta(self.settings, drop=self.names))
        self.assertEqual(code, 0)
        self.assertNotIn("⚠", text)
        self.assertIn("ℹ 给下游数据仓库用的列还没建", text)
        self.assertEqual(written[self.f.comment_count], 12)
        for name in self.names:
            self.assertNotIn(name, written)

    def test_other_missing_columns_are_still_a_warning(self):
        drop = (*self.names, self.f.surge_time)
        code, text, _written = self._write_back(full_meta(self.settings, drop=drop))
        self.assertEqual(code, 0)
        warning = next(line for line in text.splitlines() if line.startswith("⚠"))
        self.assertIn(self.f.surge_time, warning)
        for name in self.names:
            self.assertNotIn(name, warning)

    def test_with_the_columns_everything_lands(self):
        code, text, written = self._write_back(full_meta(self.settings))
        self.assertEqual(code, 0)
        self.assertNotIn("还没建", text)
        self.assertEqual(written[self.f.count_observed_at], 1787313600000)
        self.assertEqual(written[self.f.comment_records], '{"v":1}')


# ------------------------------------------------------------ 5. 维护情况的提示


class TestPaidBoostHintMatchesWhatOperatorsWrite(unittest.TestCase):
    def test_the_hint_is_the_dated_marker(self):
        hints = dict(schema.MAINTENANCE_COLUMNS)
        self.assertIn("月/日🍟", hints["维护情况"])
        self.assertNotIn("准备投流", hints["维护情况"])
        missing = schema.diff(Settings(), full_meta(drop=("维护情况",))).missing_columns
        self.assertEqual([c.name for c in missing], ["维护情况"])
        self.assertIn("🍟", missing[0].note)
        self.assertEqual(missing[0].body(), {"field_name": "维护情况", "type": 4})


if __name__ == "__main__":
    unittest.main()
