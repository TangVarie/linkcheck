"""楼中楼（2026-11-05）：负面藏在别人评论下面的回复里（途鸽）。

* 跟着评论页露出来的那几条回复（原始 `sub_comments`）每张表都查，不花钱；
* 开了「楼中楼展开条数」的表，回复比露出来的多的评论再各取一页回复（TikHub）。
"""

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tools import probe_channel
from xhsearch import (analyze, negatives, protocol, providers, registry, rows, runner, summary,
                      transport)
from xhsearch.config import Settings
from xhsearch.rows import Row

NOW = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
POST_ID = "6aa74952000000000a0256a3"
LINK = "https://www.xiaohongshu.com/explore/" + POST_ID


def raw_comment(cid, text, *, subs=None, count=None, more=None, author=False):
    out = {"id": cid, "content": text, "user": {"nickname": "u" + cid},
           "show_tags_v2": [{"type": "is_author"}] if author else []}
    if subs is not None:
        out["sub_comments"] = subs
    if count is not None:
        out["sub_comment_count"] = count
    if more is not None:
        out["sub_comment_has_more"] = more
    return out


def tikhub_page(comments, **core):
    body = {"code": 200, "data": {"data": {"comments": comments, "comment_count": 30,
                                            "comment_count_l1": 10, "user_id": "u",
                                            **core}}}
    return providers._tikhub_parse("xhs", "comments", 200, "application/json",
                                   json.dumps(body), "rid", "k")


def xhs_row(**kw):
    kw.setdefault("publish_time_ms", int((NOW - timedelta(days=1)).timestamp() * 1000))
    kw.setdefault("negative_keywords", ["有出入"])
    return Row("rec1", LINK, **kw)


class TestInlineReplies(unittest.TestCase):
    """评论页本来就带着的那几条回复。"""

    def page(self):
        return tikhub_page([
            raw_comment("c1", "随便艾特王嫂", count=1, subs=[
                raw_comment("r1", "途鸽....和我知道的有出入")]),
            raw_comment("c2", "好用", count="5", more=True, subs=[
                raw_comment("r2", "有出入？不会的，正品", author=True)]),
        ])

    def test_tikhub_keeps_the_replies_that_come_with_the_page(self):
        result = self.page()
        self.assertIsInstance(result, protocol.Ok)
        first, second = result.data["items"]
        self.assertEqual([r["comment_id"] for r in first["replies"]], ["r1"])
        self.assertEqual(first["reply_count"], 1)
        self.assertEqual(second["reply_count"], 5, "web 端给字符串，也认")
        self.assertTrue(second["replies_more"])
        self.assertTrue(second["replies"][0]["is_author_comment"])
        self.assertNotIn("replies", first["replies"][0], "回复下面不再收楼中楼")

    def test_a_negative_reply_is_found_and_marked_with_its_parent(self):
        snap = analyze.read_comment_page("xhs", self.page().data)
        self.assertEqual([c.comment_id for c in snap.comments], ["c1", "c2"],
                         "一级评论还是那两条：回复不进评论区快照、不判置顶")
        self.assertEqual([c.comment_id for c in snap.replies], ["r1", "r2"])
        verdict = analyze.decide(snap, Settings(), previous_comment_count=30, age_hours=24,
                                 negative_keywords=["有出入"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits], ["r1"],
                         "自家号在楼中楼里的回复照样排除")
        line = analyze.format_negative_digest(verdict.negative_hits, Settings().digest)
        self.assertIn("回复「随便艾特王嫂」", line)
        self.assertIn("其中 1 条在楼中楼回复里", "；".join(verdict.notes))
        # 日报那边按标记段拆，命中词和正文都还拆得出来
        self.assertEqual(summary.negative_digest_comments(line, Settings()),
                         [("途鸽....和我知道的有出入", "有出入")])

    def test_replies_do_not_count_for_seed_keywords_or_pins(self):
        snap = analyze.read_comment_page("xhs", tikhub_page([
            raw_comment("c1", "好", subs=[raw_comment("r1", "求链接")])]).data)
        verdict = analyze.decide(snap, Settings(), previous_comment_count=30, age_hours=24,
                                 seed_keywords=["求链接"])
        self.assertIsNone(verdict.seed_hit, "评论状态说的是一级评论那一屏")
        self.assertNotIn("求链接", analyze.format_digest(snap, Settings().digest))

    def test_douyin_replies_are_read_too(self):
        snap = analyze.read_comment_page("douyin", {"comment_count": 3, "items": [
            {"content": "好", "comment_id": "c1", "reply_count": 4,
             "replies": [{"content": "有出入", "comment_id": "r1"}]}]})
        self.assertEqual([r.comment_id for r in snap.replies], ["r1"])
        self.assertEqual(snap.comments[0].reply_count, 4)

    def test_reply_excerpt_cannot_break_the_digest_marks(self):
        self.assertEqual(analyze.reply_excerpt("[笑哭R]「好」 用  的]"), "好 用 的")
        self.assertEqual(analyze.reply_excerpt("#sis[搜索高亮]# 好用"), "sis 好用")
        self.assertEqual(analyze.reply_excerpt("一二三四五六七八九十十一"), "一二三四五六七八九…")


def view(cid, text="好", **kw):
    return analyze.CommentView(text, comment_id=cid, **kw)


class TestReplyTargets(unittest.TestCase):
    def test_most_hidden_replies_first_and_capped(self):
        snap = analyze.Snapshot("xhs", comments=[
            view("a", reply_count=3, replies_shown=3),          # 都露出来了
            view("b", reply_count=8, replies_shown=1),          # 藏 7
            view("c", reply_count=None, replies_more=True),     # 只说还有
            view("d", reply_count=4, replies_shown=1),          # 藏 3
            view("", reply_count=9),                            # 没 ID 展不了
        ], latest_comments=[view("b", reply_count=8, replies_shown=1),
                            view("e", reply_count=20, replies_shown=2)])
        self.assertEqual([c.comment_id for c in analyze.reply_targets(snap, 3)], ["e", "b", "d"])
        self.assertEqual([c.comment_id for c in analyze.reply_targets(snap, 9)],
                         ["e", "b", "d", "c"])
        self.assertEqual(analyze.reply_targets(snap, 0), [])

    def test_a_parent_on_both_pages_keeps_the_copy_that_reports_replies(self):
        # 默认那一页降到备胎（不报回复数），最新那一页走 TikHub、报了 6 条回复
        snap = analyze.Snapshot("xhs", comments=[view("a"), view("b")],
                                latest_comments=[view("b", reply_count=6, replies_shown=1)])
        targets = analyze.reply_targets(snap, 3)
        self.assertEqual([c.comment_id for c in targets], ["b"])
        self.assertEqual(targets[0].reply_count, 6)

    def test_expanded_replies_join_the_pool_once(self):
        parent = view("b", "这个牌子怎么样")
        page = analyze.read_reply_page({"items": [
            {"content": "和我知道的有出入", "comment_id": "r1", "is_author_comment": False},
            {"content": "踩雷", "comment_id": "r9", "is_author_comment": False}]}, parent)
        snap = analyze.Snapshot("xhs", comment_count=30, comments=[parent],
                                replies=[view("r1", "和我知道的有出入", is_reply=True)],
                                expanded_replies=page.replies)
        pool = analyze.negative_pool(snap)
        self.assertEqual([c.comment_id for c in pool], ["b", "r1", "r9"],
                         "露出来的那条在展开页里又出现一次，只算一次")
        self.assertTrue(pool[2].from_reply_page)
        self.assertEqual(pool[2].reply_to, "这个牌子怎么样")

    def test_expanded_page_author_trust_is_its_own(self):
        own = view("r1", "有出入？正品", is_reply=True, is_author=True, from_reply_page=True)
        snap = analyze.Snapshot("xhs", comment_count=30, comments=[view("c1")],
                                author_reported=True, expanded_replies=[own],
                                replies_author_reported=False)
        verdict = analyze.decide(snap, Settings(), previous_comment_count=30, age_hours=24,
                                 negative_keywords=["有出入"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits], ["r1"],
                         "展开那一页的作者标记不可信就不拿来排除")
        self.assertIn("不报作者标记", "；".join(verdict.notes))

    def test_unfinished_expansion_is_said_out_loud(self):
        snap = analyze.Snapshot("xhs", comment_count=30, comments=[view("c1")],
                                expanded_replies=[], replies_expanded=1,
                                reply_error="[rate_limit/429] too fast")
        verdict = analyze.decide(snap, Settings(), previous_comment_count=30, age_hours=24,
                                 negative_keywords=["有出入"])
        self.assertIn("楼中楼没展开完（展开了 1 条，", "；".join(verdict.notes))


class TestReplyEndpoint(unittest.TestCase):
    def test_tikhub_request_carries_the_parent_comment(self):
        request = providers._tikhub_build("k", "xhs", "replies",
                                          {"note_id": POST_ID, "comment_id": "c9"})
        self.assertIn("/app_v2/get_note_sub_comments?", request.url)
        self.assertIn("comment_id=c9", request.url)
        self.assertIn("index=1", request.url)
        self.assertIn("note_id=" + POST_ID, request.url)

    def test_only_tikhub_takes_reply_pages_and_douyin_needs_an_id(self):
        sdx = providers.get_provider("socialdatax")
        self.assertFalse(sdx.can_handle("xhs", "replies", {"note_id": POST_ID}))
        self.assertFalse(sdx.can_handle("douyin", "replies", {"aweme_id": "1"}))
        tikhub = providers.get_provider("tikhub")
        self.assertTrue(tikhub.can_handle("xhs", "replies", {"note_id": POST_ID}))
        self.assertTrue(tikhub.can_handle("douyin", "replies", {"aweme_id": "1"}))
        self.assertFalse(tikhub.can_handle("douyin", "replies",
                                           {"url": "https://v.douyin.com/x/"}))

    def parse(self, core):
        return providers._tikhub_parse("xhs", "replies", 200, "application/json",
                                       json.dumps({"code": 200, "data": {"data": core}}),
                                       "rid", "k")

    def test_reply_page_is_normalized_like_a_comment_page(self):
        result = self.parse({"comments": [raw_comment("r1", "有出入", author=True)],
                             "cursor": {"cursor": "x", "index": 2}, "has_more": True})
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["items"][0]["comment_id"], "r1")
        self.assertTrue(result.data["items"][0]["is_author_comment"])
        self.assertTrue(result.data["_capabilities"]["author"])
        self.assertIsInstance(self.parse({"sub_comments": []}), protocol.Ok)

    def test_a_service_error_in_data_is_a_billed_non_contract_failure(self):
        result = self.parse({"msg": "服务异常"})
        self.assertIsInstance(result, protocol.Err)
        self.assertIs(result.kind, protocol.Failure.UNKNOWN)
        self.assertNotIn(result.code, providers.CONTRACT_CODES,
                         "一条评论被删了不该给整条通道记「契约坏了」")
        self.assertEqual(result.http_status, 200, "对方照样计费，账上要记")


class TestPlanAndCost(unittest.TestCase):
    def setUp(self):
        self.on = Settings()
        self.on.reply_pages_per_post = 3

    def test_plan_adds_one_capped_reply_step_only_when_enabled(self):
        calls = rows.plan_calls(xhs_row(), self.on, NOW)
        replies = [c for c in calls if c.reply_pages]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].max_calls, 3)
        self.assertEqual(replies[0].requests, 3)
        self.assertFalse(any(c.reply_pages for c in rows.plan_calls(xhs_row(), Settings(), NOW)))
        douyin = Row("rec2", "https://www.douyin.com/video/7448118827402972455",
                     publish_time_ms=NOW_MS - 86_400_000)
        self.assertEqual([c.purpose for c in rows.plan_calls(douyin, self.on, NOW)],
                         ["comments", "replies", "detail"], "抖音一样排上")
        self.assertEqual([c.purpose for c in rows.plan_calls(douyin, Settings(), NOW)],
                         ["comments", "detail"])

    def test_projects_with_different_caps_do_not_share_a_fetch(self):
        one = Settings()
        one.reply_pages_per_post = 1
        self.assertNotEqual(rows.share_key(rows.plan_calls(xhs_row(), self.on, NOW)),
                            rows.share_key(rows.plan_calls(xhs_row(), one, NOW)))

    def test_budget_reserves_the_full_cap_and_the_estimate_guesses(self):
        keys = {"tikhub": "k"}
        price = providers.get_provider("tikhub").yuan_per_call("xhs", "comments")
        small, big = xhs_row(previous_comment_count=4), xhs_row(previous_comment_count=40)
        base = rows.estimate_yuan([small], Settings(), NOW, keys=keys)
        self.assertAlmostEqual(rows.estimate_yuan([small], self.on, NOW, keys=keys,
                                                  worst_case=True), base + 3 * price)
        self.assertAlmostEqual(rows.estimate_yuan([small], self.on, NOW, keys=keys), base,
                               msg="评论不到一页的帖子，乐观口径不算展开")
        self.assertAlmostEqual(rows.estimate_yuan([big], self.on, NOW, keys=keys),
                               base + 3 * price)

    def test_forecast_reports_the_reply_share_as_an_upper_bound(self):
        keys = {"tikhub": "k"}
        row = xhs_row(previous_comment_count=40)
        before = rows.forecast([row], Settings(), NOW, days=7, keys=keys)
        after = rows.forecast([row], self.on, NOW, days=7, keys=keys)
        price = providers.get_provider("tikhub").yuan_per_call("xhs", "comments")
        self.assertAlmostEqual(after.reply_yuan, after.refreshes * 3 * price)
        self.assertAlmostEqual(after.yuan - before.yuan, after.reply_yuan)
        self.assertEqual(after.reply_rows, 1)
        self.assertIn("展开楼中楼最多", summary.describe_forecast(before, after))


class TestRunnerExpansion(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.reply_pages_per_post = 2

    def fetch(self, reply_result, keys=None, disabled=None):
        sent = []

        def fake_call(call, *args, **kwargs):
            if call.reply_pages:
                sent.append(call.arguments["comment_id"])
                return reply_result(call)
            return protocol.Ok({"comment_count": 40, "items": [
                {"content": "好用", "comment_id": "c1", "is_author_comment": False,
                 "reply_count": 2, "replies": [{"content": "嗯", "comment_id": "x"}]},
                {"content": "这牌子咋样", "comment_id": "c2", "is_author_comment": False,
                 "reply_count": 9, "replies": []},
                {"content": "求链接", "comment_id": "c3", "is_author_comment": False,
                 "reply_count": 4, "replies": [{"content": "私", "comment_id": "y"}]},
                {"content": "一般", "comment_id": "c4", "is_author_comment": False,
                 "reply_count": 1, "replies": [{"content": "嗯", "comment_id": "z"}]}]})
        with mock.patch.object(runner, "_call", side_effect=fake_call):
            fetched = runner._fetch_calls(
                rows.plan_calls(xhs_row(), self.settings, NOW), keys or {"tikhub": "k"},
                self.settings, deadline=None, timeout=1, disabled=disabled or set(),
                tally={}, lock=None, watch=None, expand_failure="")
        return fetched, sent

    def test_expands_the_comments_with_most_hidden_replies(self):
        def ok(call):
            cid = call.arguments["comment_id"]
            return protocol.Ok({"items": [{"content": f"{cid} 下面：有出入",
                                           "comment_id": "r-" + cid,
                                           "is_author_comment": False}]})
        fetched, sent = self.fetch(ok)
        self.assertEqual(sent, ["c2", "c3"], "藏 9 条的先、藏 3 条的次；c1 只藏 1 条，超出上限")
        snap = fetched.snapshot
        self.assertEqual(snap.replies_expanded, 2)
        self.assertEqual(snap.reply_error, "")
        self.assertEqual([r.comment_id for r in snap.expanded_replies], ["r-c2", "r-c3"])
        self.assertEqual(snap.expanded_replies[0].reply_to, "这牌子咋样")
        verdict = analyze.decide(snap, self.settings, previous_comment_count=40, age_hours=24,
                                 negative_keywords=["有出入"])
        self.assertEqual(len(verdict.negative_hits), 2)

    def test_a_failure_skips_that_comment_and_rate_limit_stops(self):
        def flaky(call):
            if call.arguments["comment_id"] == "c2":
                return protocol.Err(protocol.Failure.UNKNOWN, "no_replies", "服务异常")
            return protocol.Ok({"items": []})
        fetched, sent = self.fetch(flaky)
        self.assertIsNone(fetched.error, "楼中楼没展开不影响这一行的结论")
        self.assertEqual(sent, ["c2", "c3"], "一条失败接着展开下一条")
        self.assertEqual(fetched.snapshot.replies_expanded, 1)
        self.assertIn("服务异常", fetched.snapshot.reply_error)

        fetched, sent = self.fetch(lambda call: protocol.Err(
            protocol.Failure.RATE_LIMIT, "429", "too fast"))
        self.assertEqual(sent, ["c2"], "限流就不再展开，也不睡着等")
        self.assertIn("too fast", fetched.snapshot.reply_error)

    def test_a_dead_tikhub_does_not_abort_the_platform(self):
        fetched, sent = self.fetch(lambda call: protocol.Err(
            protocol.Failure.QUOTA, "402", "余额不足", definitive=True))
        self.assertIsNone(fetched.error)
        self.assertEqual(sent, ["c2"])

    def test_without_tikhub_nothing_is_sent_and_it_is_said(self):
        fetched, sent = self.fetch(lambda call: self.fail("不该发"),
                                   keys={"socialdatax": "k"})
        self.assertEqual(sent, [])
        self.assertIn("只有 TikHub 能", fetched.snapshot.reply_error)


class TestParameter(unittest.TestCase):
    def test_registry_column_sets_the_cap_for_that_table_only(self):
        base = Settings()
        per_table = registry.apply_overrides(base, registry.Entry(
            record_id="recP", label="途鸽", thresholds={registry.COL_REPLY_PAGES: 3}))
        self.assertEqual(per_table.reply_pages_per_post, 3)
        self.assertEqual(base.reply_pages_per_post, 0)
        self.assertTrue(negatives.accumulates(per_table), "每轮展开的不一定是同几条，要攒")
        self.assertFalse(negatives.accumulates(base))

    def test_out_of_range_is_reported_not_applied(self):
        override = registry.read_overrides(registry.Entry(
            thresholds={registry.COL_REPLY_PAGES: registry.MAX_REPLY_PAGES + 1}), Settings())
        self.assertEqual(override.values, {})
        self.assertEqual(len(override.problems), 1)

    def test_form_spec_says_it_is_off_by_default(self):
        spec = next(s for s in registry.param_specs(Settings())
                    if s["column"] == registry.COL_REPLY_PAGES)
        self.assertEqual(spec["group"], "评论扫描")
        self.assertEqual(spec["max"], registry.MAX_REPLY_PAGES)
        self.assertEqual(spec["default_text"], "默认不展开")
        self.assertIn("小红书、抖音都管", spec["hint"])


class TestLedger(unittest.TestCase):
    def test_a_reply_pushed_off_keeps_its_reply_mark(self):
        reply = analyze.CommentView("和我知道的有出入", comment_id="r1", is_reply=True,
                                    reply_to="随便艾特王嫂")
        _, memory = negatives.advance(None, post_key="xhs:p",
                                      hits=[analyze.KeywordHit("有出入", reply)],
                                      now_ms=NOW_MS, cut_ms=None)
        pending, _ = negatives.advance(memory, post_key="xhs:p", hits=[],
                                       now_ms=NOW_MS + 3_600_000, cut_ms=None)
        self.assertTrue(pending[0].is_reply)
        text = negatives.format_digest(pending, Settings().digest,
                                       minute=lambda dt: dt.strftime("%m-%d %H:%M"))
        self.assertIn("回复「随便艾特王嫂」", text)
        self.assertIn("这一轮没再看到", text)


DOUYIN_ID = "7448118827402972455"


def douyin_raw(cid, text, *, uid="u" , sec="s", replies=None, total=None):
    out = {"cid": cid, "text": text, "digg_count": 0,
           "user": {"nickname": "n" + cid, "uid": uid, "sec_uid": sec}}
    if replies is not None:
        out["reply_comment"] = replies
    if total is not None:
        out["reply_comment_total"] = total
    return out


def douyin_parse(purpose, inner):
    return providers._tikhub_parse("douyin", purpose, 200, "application/json",
                                   json.dumps({"code": 200, "data": inner}), "rid", "k")


class TestDouyin(unittest.TestCase):
    """抖音评论没有作者标记：贴主靠账号 ID 对 detail 里发视频的账号。"""

    def snapshot(self, *, with_detail=True):
        page = douyin_parse("comments", {"total": 30, "comments": [
            douyin_raw("c1", "用了过敏", uid="9", sec="S9", total=3, replies=[
                douyin_raw("r1", "亲，我们是温和配方，不会过敏哦", uid="1", sec="S1")]),
            douyin_raw("c2", "好用", uid="8", sec="S8", total=0),
            douyin_raw("c3", "过敏是正常的，用两周就好", uid="1", sec="S1")]})
        snap = analyze.read_comment_page("douyin", page.data)
        if with_detail:
            detail = douyin_parse("detail", {"aweme_detail": {
                "statistics": {"comment_count": 30}, "author": {"uid": "1", "sec_uid": "S1"},
                "author_user_id": 1}})
            analyze.merge_detail(snap, detail.data)
        return snap

    def test_tikhub_reads_inline_replies_counts_and_account_ids(self):
        page = douyin_parse("comments", {"total": 2, "comments": [
            douyin_raw("c1", "好", total=5, replies=[douyin_raw("r1", "谢谢")])]})
        item = page.data["items"][0]
        self.assertEqual(item["reply_count"], 5)
        self.assertEqual([r["comment_id"] for r in item["replies"]], ["r1"])
        self.assertEqual(item["author_ids"], ["u", "s"])
        detail = douyin_parse("detail", {"aweme_detail": {
            "statistics": {}, "author": {"uid": "1", "sec_uid": "S1"}, "author_user_id": 1}})
        self.assertEqual(detail.data["_post_author_ids"], ["1", "S1"])

    def test_the_posters_own_comments_and_replies_are_excluded(self):
        verdict = analyze.decide(self.snapshot(), Settings(), previous_comment_count=30,
                                 age_hours=24, negative_keywords=["过敏"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits], ["c1"],
                         "贴主在楼中楼里的回复和自己发的一级评论都不算")
        self.assertNotIn("没有被排除", "；".join(verdict.notes))

    def test_without_the_post_author_nothing_is_excluded_and_it_is_said(self):
        verdict = analyze.decide(self.snapshot(with_detail=False), Settings(),
                                 previous_comment_count=30, age_hours=24,
                                 negative_keywords=["过敏"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits],
                         ["c1", "c3", "r1"], "认不出贴主就不排除，宁可多报")
        self.assertIn("贴主自己的评论和回复没有被排除", "；".join(verdict.notes))

    def test_reply_request_and_page(self):
        request = providers._tikhub_build("k", "douyin", "replies",
                                          {"aweme_id": DOUYIN_ID, "comment_id": "c9"})
        self.assertIn("/douyin/app/v3/fetch_video_comment_replies?", request.url)
        for part in ("item_id=" + DOUYIN_ID, "comment_id=c9", "cursor=0", "count=20"):
            self.assertIn(part, request.url)
        page = douyin_parse("replies", {"comments": [douyin_raw("r1", "过敏了")],
                                        "has_more": 0})
        self.assertEqual(page.data["items"][0]["comment_id"], "r1")
        self.assertNotIn("replies", page.data["items"][0])
        self.assertEqual(douyin_parse("replies", {"comments": None}).data["items"], [],
                         "没有回复时上游给 null，不是坏了")
        self.assertIsInstance(douyin_parse("replies", {"status_msg": "x"}), protocol.Err)

    def test_expansion_runs_for_douyin(self):
        settings = Settings()
        settings.reply_pages_per_post = 1
        row = Row("rec2", "https://www.douyin.com/video/" + DOUYIN_ID,
                  publish_time_ms=NOW_MS - 86_400_000, negative_keywords=["过敏"])
        sent = []

        def fake_call(call, *args, **kwargs):
            if call.reply_pages:
                sent.append(call.arguments)
                return protocol.Ok({"items": [{"content": "我也过敏", "comment_id": "r9",
                                               "is_author_comment": False,
                                               "author_ids": ["7"]}]})
            if call.purpose == "detail":
                return protocol.Ok({"comment_count": 30, "_post_author_ids": ["1"]})
            return protocol.Ok({"comment_count": 30, "items": [
                {"content": "好", "comment_id": "c1", "is_author_comment": False,
                 "reply_count": 6, "replies": [], "author_ids": ["8"]}]})
        with mock.patch.object(runner, "_call", side_effect=fake_call):
            fetched = runner._fetch_calls(
                rows.plan_calls(row, settings, NOW), {"tikhub": "k"}, settings,
                deadline=None, timeout=1, disabled=set(), tally={}, lock=None, watch=None,
                expand_failure="")
        self.assertEqual(sent, [{"aweme_id": DOUYIN_ID, "comment_id": "c1"}])
        self.assertEqual(fetched.snapshot.post_author_ids, ("1",))
        verdict = analyze.decide(fetched.snapshot, settings, previous_comment_count=30,
                                 age_hours=24, negative_keywords=["过敏"])
        self.assertEqual([h.comment.comment_id for h in verdict.negative_hits], ["r9"])


class TestProbe(unittest.TestCase):
    def run_probe(self, name, responses, replies):
        settings = Settings()
        settings.reply_pages_per_post = replies
        with mock.patch.object(transport, "request", side_effect=responses) as sent, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok = probe_channel.probe(name, "k", LINK, settings, raw=True)
        return ok, out.getvalue(), sent

    def test_replies_flag_expands_and_prints_what_came_back(self):
        page = json.dumps({"code": 200, "data": {"data": {
            "user_id": "u", "comment_count": 12, "comment_count_l1": 2, "comments": [
                raw_comment("c1", "随便艾特王嫂", count=6, subs=[raw_comment("r1", "嗯")]),
                raw_comment("c2", "好用")]}}})
        sub = json.dumps({"code": 200, "data": {"data": {"comments": [
            raw_comment("r1", "嗯"), raw_comment("r2", "和我知道的有出入")]}}})
        ok, output, sent = self.run_probe("tikhub", [
            transport.Response(200, "application/json", page, "a"),
            transport.Response(200, "application/json", sub, "b")], 1)
        self.assertTrue(ok)
        self.assertEqual(sent.call_count, 2)
        self.assertIn("comment_id=c1", sent.call_args.args[1])
        self.assertIn("共露出 1 条；回复比露出来的多的 1 条", output)
        self.assertIn("拿回 2 条", output)
        self.assertIn("和我知道的有出入", output)

    def test_douyin_probe_shows_the_poster_ids_and_expands(self):
        comments = json.dumps({"code": 200, "data": {"total": 9, "comments": [
            douyin_raw("c1", "好", uid="8", total=6, replies=[douyin_raw("r1", "谢谢", uid="1")])]}})
        detail = json.dumps({"code": 200, "data": {"aweme_detail": {
            "statistics": {"comment_count": 9}, "author": {"uid": "1", "sec_uid": "S1"}}}})
        replies = json.dumps({"code": 200, "data": {"comments": [douyin_raw("r2", "过敏了")]}})
        settings = Settings()
        settings.reply_pages_per_post = 1
        with mock.patch.object(transport, "request", side_effect=[
                transport.Response(200, "application/json", body, "x")
                for body in (comments, detail, replies)]) as sent, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok = probe_channel.probe("tikhub", "k", "https://www.douyin.com/video/" + DOUYIN_ID,
                                     settings)
        output = out.getvalue()
        self.assertTrue(ok)
        self.assertIn("fetch_video_comment_replies", sent.call_args.args[1])
        self.assertIn("贴主账号 ID（详情接口）：1、S1", output)
        self.assertIn("拿回 1 条", output)

    def test_socialdatax_skips_expansion_without_skipping_the_probe(self):
        body = json.dumps({"code": 0, "data": {"items": [], "comment_count": 0}})
        ok, output, sent = self.run_probe("socialdatax", [
            transport.Response(200, "application/json", body, "a")], 1)
        self.assertEqual(sent.call_count, 1, "评论页照常验，楼中楼不发")
        self.assertIn("不接这条的楼中楼", output)


if __name__ == "__main__":
    unittest.main()
