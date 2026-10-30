"""逐项目参数（2026-10-30）：刷新频率三档、小红书加扫「最新」那一页、
开了最新评论的项目负面评论攒到点「已处理」为止、改参数前后的花费估算。"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import cli
from xhsearch import (analyze, negatives, panel, protocol, providers, registry, rows,
                      runner, summary)
from xhsearch.config import Settings
from xhsearch.rows import Row

NOW = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
HOUR_MS = 3_600_000
POST_ID = "6aa74952000000000a0256a3"
LINK = "https://www.xiaohongshu.com/explore/" + POST_ID


def entry(**values):
    return registry.Entry(record_id="recP", label="途鸽", thresholds=values)


def comment(text, cid, **kw):
    return analyze.CommentView(text, comment_id=cid, **kw)


def xhs_row(**kw):
    kw.setdefault("publish_time_ms", int((NOW - timedelta(days=1)).timestamp() * 1000))
    kw.setdefault("negative_keywords", ["踩雷"])
    return Row("rec1", LINK, **kw)


class TestParameters(unittest.TestCase):
    def test_intervals_and_latest_threshold_apply_to_that_table_only(self):
        base = Settings()
        per_table = registry.apply_overrides(base, entry(**{
            registry.COL_INTERVAL_LATE: 48, registry.COL_LATEST_OVER: 5}))
        self.assertEqual(per_table.refresh.tiers, [(2, 8), (7, 24), (30, 48)])
        self.assertEqual(per_table.latest_comments_over, 5)
        self.assertEqual(base.refresh.tiers, [(2, 8), (7, 24), (30, 72)], "不能串到别的表")
        self.assertIsNone(base.latest_comments_over)
        self.assertEqual(per_table.refresh.interval_hours_for_age(20), 48)

    def test_zero_means_always_scan_latest_and_bad_values_are_reported(self):
        override = registry.read_overrides(entry(**{
            registry.COL_LATEST_OVER: 0, registry.COL_INTERVAL_EARLY: 0,
            registry.COL_INTERVAL_WEEK: 24 * 61}), Settings())
        self.assertEqual(override.values, {registry.COL_LATEST_OVER: 0})
        self.assertEqual(len(override.problems), 2)

    def test_every_parameter_column_has_a_form_spec_with_its_global_default(self):
        specs = registry.param_specs(Settings())
        self.assertEqual({s["column"] for s in specs}, set(registry.THRESHOLD_COLUMNS))
        by = {s["column"]: s for s in specs}
        self.assertEqual(by[registry.COL_INTERVAL_LATE]["default"], 72)
        self.assertIsNone(by[registry.COL_LATEST_OVER]["default"], "默认不扫最新")
        self.assertIn("只对小红书", by[registry.COL_LATEST_OVER]["hint"])

    def test_saving_creates_missing_registry_columns_instead_of_failing(self):
        table = mock.Mock()
        table.fields_meta.return_value = {registry.COL_LABEL: {}, registry.COL_ARCHIVE_DAYS: {}}
        made = registry.set_thresholds(table, "recP", {
            registry.COL_INTERVAL_LATE: 48, registry.COL_LATEST_OVER: None,
            registry.COL_ARCHIVE_DAYS: 40})
        self.assertEqual(made, [registry.COL_INTERVAL_LATE])
        table.create_field.assert_called_once_with(
            {"field_name": registry.COL_INTERVAL_LATE, "type": 2, "property": {"formatter": "0"}})
        fields = table.batch_update.call_args.args[0][0]["fields"]
        self.assertEqual(fields, {registry.COL_INTERVAL_LATE: 48, registry.COL_ARCHIVE_DAYS: 40},
                         "清空一个不存在的格子不用建列")


class TestLatestPage(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.latest_comments_over = 5

    def test_xhs_plans_a_conditional_latest_page_and_douyin_does_not(self):
        calls = rows.plan_calls(xhs_row(), self.settings, NOW)
        self.assertEqual([c.arguments.get("sort") for c in calls], ["default", "latest"])
        self.assertTrue(calls[1].latest_page)
        self.assertEqual(calls[1].min_count, 5)
        douyin = Row("rec2", "https://www.douyin.com/video/7448118827402972455",
                     publish_time_ms=NOW_MS - 86_400_000)
        self.assertFalse(any(c.latest_page for c in rows.plan_calls(douyin, self.settings, NOW)))
        self.assertEqual(len(rows.plan_calls(xhs_row(), Settings(), NOW)), 1, "默认不扫")

    def test_both_vendors_get_their_own_name_for_latest(self):
        tikhub = providers._tikhub_build("k", "xhs", "comments", {"note_id": POST_ID, "sort": "latest"})
        self.assertIn("sort_strategy=latest_v2", tikhub.url)
        sdx = providers._sdx_build("k", "xhs", "comments", {"note_id": POST_ID, "sort": "latest"})
        self.assertIn('"sort_type": "time_descending"', sdx.body)
        self.assertIn("sort_strategy=default",
                      providers._tikhub_build("k", "xhs", "comments",
                                              {"note_id": POST_ID, "sort": "default"}).url)

    def fetch(self, count, latest_result):
        sent = []

        def fake_call(call, *args, **kwargs):
            sent.append(call.arguments.get("sort"))
            if call.latest_page:
                return latest_result
            return protocol.Ok({"comment_count": count, "items": [
                {"content": "好用", "comment_id": "c1", "is_pinned": True,
                 "is_author_comment": False}]})
        with mock.patch.object(runner, "_call", side_effect=fake_call):
            fetched = runner._fetch_calls(
                rows.plan_calls(xhs_row(), self.settings, NOW), {"tikhub": "k"}, self.settings,
                deadline=None, timeout=1, disabled=set(), tally={}, lock=None, watch=None,
                expand_failure="")
        return fetched, sent

    def test_latest_page_is_only_bought_when_the_post_has_more_comments(self):
        latest = protocol.Ok({"comment_count": 9, "items": [
            {"content": "踩雷了", "comment_id": "c9"}]})
        fetched, sent = self.fetch(5, latest)
        self.assertEqual(sent, ["default"], "5 条没超过门槛，默认那一页已经装全")
        self.assertIsNone(fetched.snapshot.latest_comments)
        fetched, sent = self.fetch(9, latest)
        self.assertEqual(sent, ["default", "latest"])
        self.assertEqual([c.comment_id for c in fetched.snapshot.latest_comments], ["c9"])
        self.assertEqual([c.comment_id for c in fetched.snapshot.comments], ["c1"],
                         "默认那一页不能被最新那一页盖掉")

    def test_a_failed_latest_page_does_not_fail_the_row(self):
        err = protocol.Err(protocol.Failure.RATE_LIMIT, "429", "too fast")
        with mock.patch.object(runner.time, "sleep"):
            fetched, _ = self.fetch(9, err)
        self.assertIsNone(fetched.error)
        self.assertIn("too fast", fetched.snapshot.latest_error)

    def test_negatives_use_both_pages_but_pin_and_seed_only_the_default_page(self):
        snap = analyze.Snapshot("xhs", comment_count=30, comments=[
            comment("好用 推荐", "c1", is_pinned=True), comment("踩雷 别买", "c2")],
            latest_comments=[comment("踩雷 别买", "c2"), comment("用了踩雷", "c3"),
                             comment("求链接 推荐", "c4")])
        verdict = analyze.decide(snap, self.settings, previous_comment_count=30, age_hours=24,
                                 seed_keywords=["求链接"], negative_keywords=["踩雷"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits], ["c2", "c3"],
                         "两页都有的那条只算一次")
        self.assertTrue(verdict.negative_hits[1].comment.from_latest)
        self.assertIsNone(verdict.seed_hit, "评论状态说的是默认那一屏，最新页里的不算")
        self.assertIs(verdict.pin, analyze.Pin.PINNED)
        digest = analyze.format_negative_digest(verdict.negative_hits, self.settings.digest)
        self.assertIn("最新", digest.splitlines()[1])
        self.assertIn("1 条只在最新那一页", "；".join(verdict.notes))

    def test_a_missing_latest_page_is_said_out_loud(self):
        snap = analyze.Snapshot("xhs", comment_count=30, comments=[comment("好", "c1")],
                                latest_error="[rate_limit/429] too fast")
        verdict = analyze.decide(snap, self.settings, previous_comment_count=30, age_hours=24,
                                 negative_keywords=["踩雷"])
        self.assertIn("「最新」排序那一页没取到", "；".join(verdict.notes))


def hit(text, cid, word="踩雷", latest=False):
    return analyze.KeywordHit(word, comment(text, cid, from_latest=latest))


class TestNegativeLedger(unittest.TestCase):
    def test_comments_pushed_off_the_page_stay_until_resolved(self):
        post = "xhs:" + POST_ID
        pending, memory = negatives.advance(None, post_key=post, hits=[hit("踩雷A", "a")],
                                            now_ms=NOW_MS, cut_ms=None)
        self.assertEqual([p.key for p in pending], ["id:a"])
        later = NOW_MS + 8 * HOUR_MS
        pending, memory = negatives.advance(memory, post_key=post, hits=[hit("踩雷B", "b")],
                                            now_ms=later, cut_ms=None)
        self.assertEqual([p.key for p in pending], ["id:b", "id:a"], "这一轮的在前，挤出去的还在")
        self.assertIsNone(pending[1].hit)
        # 运营看到的是 later 那一次的结果、点了已处理；之后 a 又出现、c 是新的
        pending, memory = negatives.advance(
            memory, post_key=post, hits=[hit("踩雷A", "a"), hit("踩雷C", "c")],
            now_ms=later + 8 * HOUR_MS, cut_ms=later)
        self.assertEqual([p.key for p in pending], ["id:c"], "处理过的再看到也不提醒")
        self.assertEqual(len(memory["i"]), 3)
        pending, _ = negatives.advance(memory, post_key="xhs:other", hits=[],
                                       now_ms=later, cut_ms=None)
        self.assertEqual(pending, [], "链接换成另一篇，旧的那篇的不算")

    def test_old_items_expire_and_rows_are_capped(self):
        post = "xhs:p"
        old = NOW_MS - (negatives.KEEP_DAYS + 1) * 86_400_000
        _, memory = negatives.advance(None, post_key=post, hits=[hit("踩雷", "old")],
                                      now_ms=old, cut_ms=None)
        pending, memory = negatives.advance(memory, post_key=post, hits=[],
                                            now_ms=NOW_MS, cut_ms=None)
        self.assertEqual(pending, [])
        many = [hit(f"踩雷{i}", f"x{i}") for i in range(negatives.MAX_ITEMS_PER_ROW + 5)]
        pending, memory = negatives.advance(None, post_key=post, hits=many,
                                            now_ms=NOW_MS, cut_ms=None)
        self.assertEqual(len(pending), negatives.MAX_ITEMS_PER_ROW + 5, "这一轮看到的不丢")
        _, memory = negatives.advance(memory, post_key=post, hits=[hit("踩雷new", "new")],
                                      now_ms=NOW_MS + 1, cut_ms=None)
        self.assertEqual(len(memory["i"]), negatives.MAX_ITEMS_PER_ROW)

    def test_handled_items_keep_only_identity_and_the_cell_never_overflows(self):
        _, memory = negatives.advance(None, post_key="p", hits=[hit("踩雷" * 20, "a")],
                                      now_ms=NOW_MS, cut_ms=None)
        _, memory = negatives.advance(memory, post_key="p", hits=[], now_ms=NOW_MS + 1,
                                      cut_ms=NOW_MS)
        self.assertEqual(set(memory["i"][0]), {"k", "t", "d"})
        ledger = negatives.TableLedger("tbl1", {}, {}, Settings())
        for i in range(400):
            ledger.apply(f"rec{i}", "p", [hit("踩雷" * 30 + str(j), f"c{i}-{j}") for j in range(5)],
                         NOW_MS + i)
        payload = ledger.payload(NOW_MS + 500)
        import json
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), negatives.MAX_CHARS)
        self.assertGreater(ledger.dropped_rows, 0)
        self.assertNotIn("rec0", payload["rows"], "先丢最久没动的行")
        self.assertIn("rec399", payload["rows"])

    def test_resolution_cut_reads_what_the_operator_saw(self):
        found = Settings().negative_status.found
        resolved = {"tbl1/rec1": {"reasons": [found], "seen": 111, "at": 222, "post_key": "xhs:p"}}
        self.assertEqual(negatives.resolution_cut(resolved, "tbl1", "rec1", "xhs:p", found), 111)
        self.assertIsNone(negatives.resolution_cut(resolved, "tbl1", "rec1", "xhs:q", found))
        resolved["tbl1/rec1"]["state"] = "processing"
        self.assertIsNone(negatives.resolution_cut(resolved, "tbl1", "rec1", "xhs:p", found),
                          "正在处理不是处理完")
        other = {"tbl1/rec1": {"reasons": ["置顶掉了"], "seen": 111}}
        self.assertIsNone(negatives.resolution_cut(other, "tbl1", "rec1", "xhs:p", found))

    def test_digest_keeps_the_panel_count_format(self):
        settings = Settings()
        pending, _ = negatives.advance(None, post_key="p", hits=[
            hit(f"踩雷{i}", f"c{i}") for i in range(12)], now_ms=NOW_MS, cut_ms=None)
        text = negatives.format_digest(pending, settings.digest, minute=settings.display.minute)
        self.assertEqual(summary.negative_digest_count(text, settings), (12, False))
        earlier = negatives.Pending("id:z", "踩雷", "很久以前" * 20, NOW_MS, from_latest=True)
        line = negatives.format_digest([earlier], settings.digest, minute=settings.display.minute)
        self.assertIn("这一轮没再看到", line)
        self.assertIn("最新", line)
        self.assertEqual(summary.negative_digest_count(line, settings), (1, False))
        self.assertIn("已处理过", negatives.format_digest([], settings.digest,
                                                       minute=settings.display.minute,
                                                       resolved_now=2))


class TestLedgerInRunner(unittest.TestCase):
    """一行连刷三轮：负面被挤出页面也留着；点了已处理收掉；新的再冒出来。"""

    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1
        self.settings.soft_deadline_seconds = 0
        self.settings.latest_comments_over = 5
        self.resolved = {}
        self.ledger = negatives.TableLedger("tbl1", {}, self.resolved, self.settings)
        self.f = self.settings.fields

    def run_once(self, at, comments):
        snap = analyze.Snapshot("xhs", comment_count=30, comments=comments)
        with mock.patch.object(runner, "_fetch_one", return_value=runner._Fetched(snapshot=snap)):
            return runner.refresh([xhs_row()], "unused-test-key", self.settings, forced=True,
                                  now=at, clock=lambda: at,
                                  negatives=self.ledger).outcomes[0].fields

    def test_accumulates_then_resolves_then_raises_only_new_ones(self):
        found, clean = self.settings.negative_status.found, self.settings.negative_status.clean
        first = self.run_once(NOW, [comment("踩雷 别买", "a")])
        self.assertEqual(first[self.f.negative_status], found)
        second_at = NOW + timedelta(hours=8)
        second = self.run_once(second_at, [comment("好用", "ok")])
        self.assertEqual(second[self.f.negative_status], found, "被挤出那一页不等于处理了")
        self.assertIn("这一轮没再看到", second[self.f.negative_digest])
        self.assertIn("还有 1 条之前扫到的负面评论没处理", second[self.f.failure_reason])
        self.resolved["tbl1/rec1"] = {"reasons": [found], "seen": second[self.f.last_updated],
                                      "at": second[self.f.last_updated] + 1000}
        third = self.run_once(second_at + timedelta(hours=8), [comment("踩雷 别买", "a")])
        self.assertEqual(third[self.f.negative_status], clean)
        self.assertIn("之前已处理过", third[self.f.failure_reason])
        self.assertNotIn(f"→ {found}", third[self.f.failure_reason], "诊断信息不能和负面状态打架")
        fourth = self.run_once(second_at + timedelta(hours=16),
                               [comment("踩雷 别买", "a"), comment("又踩雷", "b")])
        self.assertEqual(fourth[self.f.negative_status], found)
        self.assertEqual(summary.negative_digest_count(fourth[self.f.negative_digest],
                                                       self.settings), (1, False))

    def test_without_a_ledger_the_old_behaviour_is_unchanged(self):
        self.ledger = None
        self.run_once(NOW, [comment("踩雷", "a")])
        fields = self.run_once(NOW + timedelta(hours=8), [comment("好用", "ok")])
        self.assertEqual(fields[self.f.negative_status], self.settings.negative_status.clean)


class TestLedgerStore(unittest.TestCase):
    def test_load_and_save_go_through_panel_settings(self):
        settings = Settings()
        store = mock.Mock()
        store.get_json.side_effect = lambda key, default: (
            {"tbl1/rec1": {}} if key == "todo.resolved" else {"v": 1, "rows": {}})
        ledger = negatives.Ledger(store, settings, log=lambda _: None)
        table = ledger.for_table("tbl1", settings)
        table.apply("rec1", "xhs:p", [hit("踩雷", "a")], NOW_MS)
        self.assertTrue(ledger.save(NOW_MS))
        key, change, _default = store.update_json.call_args.args
        self.assertEqual(key, "negatives.tbl1")
        self.assertIn("rec1", change(None)["rows"])
        store.get_json.side_effect = RuntimeError("feishu down")
        broken = negatives.Ledger(store, settings, log=lambda _: None)
        self.assertIsNone(broken.for_table("tbl2", settings))

    def test_cli_hands_a_ledger_only_to_tables_that_scan_latest(self):
        settings = Settings()
        on = Settings()
        on.latest_comments_over = 5
        ledger = mock.Mock()
        seen = {}

        def refresh(mode, record_ids, per_table, *args, **kwargs):
            seen[args[1].table_id] = kwargs.get("negatives")
            return 0, set(), 0, 0.0, None
        tables = [("开了", SimpleNamespace(table_id="tblOn")),
                  ("没开", SimpleNamespace(table_id="tblOff"))]
        with mock.patch.object(cli, "_tables", return_value=tables), \
                mock.patch.object(cli, "settings_for_table",
                                  side_effect=lambda s, t: on if t.table_id == "tblOn" else s), \
                mock.patch.object(cli, "_daily_journal", return_value=None), \
                mock.patch.object(cli, "_negative_ledger", return_value=ledger), \
                mock.patch.object(cli, "_install_stop_handlers"), \
                mock.patch.object(cli, "_event_sink", return_value=(None, "")), \
                mock.patch.object(cli, "_refresh_table", side_effect=refresh), \
                redirect_stdout(io.StringIO()):
            cli._run_locked("sweep", None, None, settings, {"tikhub": "k"})
        ledger.for_table.assert_called_once_with("tblOn", on)
        self.assertIs(seen["tblOn"], ledger.for_table.return_value)
        self.assertIsNone(seen["tblOff"])
        ledger.save.assert_called_once()


class TestForecast(unittest.TestCase):
    def rows(self, settings, *, days_old=10, count=30):
        published = int((NOW - timedelta(days=days_old)).timestamp() * 1000)
        return [runner.row_from_record({"record_id": "rec1", "fields": {
            settings.fields.link: LINK, settings.fields.publish_time: published,
            settings.fields.last_updated: NOW_MS - HOUR_MS,
            settings.fields.comment_count: count, settings.fields.monitoring: True}}, settings)]

    def test_faster_late_tier_and_latest_page_both_show_up_in_the_estimate(self):
        old = Settings()
        new = registry.apply_overrides(old, entry(**{
            registry.COL_INTERVAL_LATE: 48, registry.COL_LATEST_OVER: 5}))
        keys = {"tikhub": "k"}
        before = rows.forecast(self.rows(old), old, NOW, keys=keys)
        after = rows.forecast(self.rows(new), new, NOW, keys=keys)
        # 发布 10 天、满 31 天归档，1 小时前刚查过：72 小时一次是第 3、6、…、21 天
        # 共 7 次；48 小时一次是第 2、4、…、20 天共 10 次（第 22 天已过归档线）。
        self.assertEqual(before.refreshes, 7)
        self.assertEqual(after.refreshes, 10)
        price = providers._tikhub_yuan("xhs", "comments")
        self.assertAlmostEqual(before.yuan, 7 * price)
        self.assertAlmostEqual(after.latest_yuan, 10 * price)
        self.assertAlmostEqual(after.yuan, 20 * price)
        self.assertEqual(after.latest_rows, 1)
        text = summary.describe_forecast(before, after)
        self.assertIn("多扫「最新」那一页", text)
        self.assertIn("之后新发的帖子另算", text)

    def test_few_comments_do_not_buy_the_latest_page_and_archived_rows_cost_nothing(self):
        on = Settings()
        on.latest_comments_over = 5
        few = rows.forecast(self.rows(on, count=3), on, NOW, keys={"tikhub": "k"})
        self.assertEqual(few.latest_yuan, 0)
        gone = rows.forecast(self.rows(on, days_old=40), on, NOW, keys={"tikhub": "k"})
        self.assertEqual((gone.refreshes, gone.yuan), (0, 0))

    def test_next_round_estimate_skips_the_latest_page_for_small_posts(self):
        on = Settings()
        on.latest_comments_over = 5
        small, big = self.rows(on, count=3), self.rows(on, count=30)
        price = providers._tikhub_yuan("xhs", "comments")
        self.assertAlmostEqual(rows.estimate_yuan(small, on, NOW, keys={"tikhub": "k"}), price)
        self.assertAlmostEqual(rows.estimate_yuan(big, on, NOW, keys={"tikhub": "k"}), 2 * price)
        self.assertAlmostEqual(rows.estimate_yuan(small, on, NOW, keys={"tikhub": "k"},
                                                  worst_case=True), 2 * price,
                               "预算预留按悲观口径，最新那一页也留出钱")


class TestPanelSave(unittest.TestCase):
    def projects(self, entry_values):
        p = panel.Projects(panel.PanelConfig(password="p", secret="s", registry_target="x",
                                             app_id="a", app_secret="s"),
                           Settings(), log=lambda _: None)
        e = registry.Entry(record_id="recP", label="途鸽", app_token="app", table_id="tblT",
                           thresholds=entry_values)
        p.list = lambda: [e]
        p._registry = mock.Mock()
        p._registry.return_value.fields_meta.return_value = {
            c: {} for c in registry.THRESHOLD_COLUMNS}
        return p

    def test_changing_frequency_also_fixes_that_tables_next_check_formula(self):
        p = self.projects({registry.COL_INTERVAL_LATE: 48})
        table = mock.Mock()
        table.fields_meta.return_value = {"下次检查时间": {"type": 20, "formula": "old"}}
        plan = SimpleNamespace(action="update", wanted="NEW", reason="")
        with mock.patch.object(p, "_bitable", return_value=table), \
                mock.patch.object(panel.provision, "next_check_plan", return_value=plan) as planner:
            result = p.set_thresholds("recP", {registry.COL_INTERVAL_LATE: 48})
        used = planner.call_args.args[0]
        self.assertEqual(used.refresh.tiers[-1], (30, 48), "公式按这张表自己的频率写")
        table.set_formula.assert_called_once()
        self.assertIn("已按新频率换好", result["formula"])

    def test_threshold_only_changes_leave_the_formula_alone(self):
        p = self.projects({})
        with mock.patch.object(p, "_bitable") as bitable:
            result = p.set_thresholds("recP", {registry.COL_TIER_HOT: 60})
        bitable.assert_not_called()
        self.assertEqual(result["formula"], "")

    def test_entry_json_carries_the_grouped_form(self):
        data = panel._entry_json(registry.Entry(record_id="r", label="x"), Settings())
        self.assertEqual({p["group"] for p in data["params"]}, {"刷新频率", "评论扫描", "热度判定"})


if __name__ == "__main__":
    unittest.main()
