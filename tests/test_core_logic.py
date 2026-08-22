"""核心逻辑的单测。全部离线，不需要 API Key，不发任何请求。

跑法：python3 -m unittest discover -s tests -v
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from xhsearch import analyze, links, protocol, rows, tags
from xhsearch.config import Settings

UTC = timezone.utc


class TestLinkParsing(unittest.TestCase):
    def test_xhs_explore_url(self):
        parsed = links.parse("https://www.xiaohongshu.com/explore/65a1b2c3d4e5f60718293a4b?xsec_token=ABC")
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_xhs_discovery_url(self):
        parsed = links.parse("https://www.xiaohongshu.com/discovery/item/65a1b2c3d4e5f60718293a4b")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_xhs_share_text_shortlink(self):
        cell = "12 复制本条信息，打开【小红书】App查看精彩内容！ http://xhslink.com/a/AbC123"
        parsed = links.parse(cell)
        self.assertEqual(parsed.platform, "xhs")
        self.assertIsNone(parsed.content_id)          # 短链拿不到 ID，透传给 by_url
        self.assertEqual(parsed.url, "http://xhslink.com/a/AbC123")
        self.assertTrue(parsed.usable)

    def test_xhs_bare_note_id(self):
        parsed = links.parse("  65A1B2C3D4E5F60718293A4B  ")
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_douyin_video_url(self):
        parsed = links.parse("https://www.douyin.com/video/7123456789012345678")
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_douyin_modal_id(self):
        parsed = links.parse("https://www.douyin.com/discover?modal_id=7123456789012345678")
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_douyin_shortlink_share_text(self):
        cell = "7.86 复制打开抖音，看看【某某】的作品 https://v.douyin.com/iRxYzAb/"
        parsed = links.parse(cell)
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.url, "https://v.douyin.com/iRxYzAb/")

    def test_trailing_chinese_punctuation_stripped(self):
        parsed = links.parse("看这条：https://www.douyin.com/video/7123456789012345678，很火")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_ambiguous_cell_refuses_to_guess(self):
        cell = "https://xhslink.com/a/AAA 和 https://v.douyin.com/BBB"
        parsed = links.parse(cell)
        self.assertIsNone(parsed.platform)
        self.assertFalse(parsed.usable)

    def test_empty_and_garbage(self):
        for cell in ["", "   ", "待补链接", "13800138000"]:
            self.assertFalse(links.parse(cell).usable, cell)

    def test_bare_numeric_needs_douyin_keyword(self):
        self.assertFalse(links.parse("7123456789012345678").usable)
        self.assertTrue(links.parse("抖音 7123456789012345678").usable)


class TestTagMerge(unittest.TestCase):
    NS = ["评估中", "爆贴", "大爆", "风控中", "已失效"]

    def test_human_tags_survive(self):
        result = tags.merge(["已复盘", "客户确认", "爆贴"], {"风控中"}, self.NS)
        self.assertIn("已复盘", result.final)
        self.assertIn("客户确认", result.final)
        self.assertIn("风控中", result.final)
        self.assertNotIn("爆贴", result.final)   # 机器标签可以撤回
        self.assertEqual(result.removed, ["爆贴"])
        self.assertEqual(result.added, ["风控中"])

    def test_idempotent(self):
        first = tags.merge(["爆贴"], {"爆贴"}, self.NS)
        self.assertEqual(first.final, ["爆贴"])
        self.assertFalse(first.changed)

    def test_unknown_option_is_dropped_not_written(self):
        result = tags.merge([], {"爆贴"}, self.NS, known_options=["风控中", "已失效"])
        self.assertEqual(result.final, [])
        self.assertEqual(result.dropped_unknown, ["爆贴"])

    def test_computed_tag_outside_namespace_raises(self):
        with self.assertRaises(ValueError):
            tags.merge([], {"随便编的"}, self.NS)

    def test_none_current(self):
        self.assertEqual(tags.merge(None, {"风控中"}, self.NS).final, ["风控中"])


class TestCommentAnalysis(unittest.TestCase):
    def _xhs_page(self, count=42, with_pinned=True):
        items = []
        if with_pinned:
            items.append({
                "content": "戳这里领优惠券 www.example.com",
                "like_count": 88,
                "is_pinned": True,
                "is_author_comment": True,
                "ip_location": "上海",
                "author": {"name": "品牌官号"},
            })
        items += [
            {"content": "求链接！", "like_count": 12, "is_pinned": False,
             "is_author_comment": False, "ip_location": "广东", "author": {"name": "用户A"}},
            {"content": "踩雷了，不推荐", "like_count": 30, "is_pinned": False,
             "is_author_comment": False, "ip_location": "北京", "author": {"name": "用户B"}},
        ]
        return {"items": items, "comment_count": count, "top_level_comment_count": count,
                "next_page_token": "", "points": {"cost": 10, "balance": 5000}}

    def test_xhs_one_call_gives_count_pinned_and_top(self):
        snap = analyze.read_comment_page("xhs", self._xhs_page())
        self.assertEqual(snap.comment_count, 42)
        self.assertIsNotNone(snap.pinned)
        self.assertEqual(len(snap.comments), 3)
        self.assertEqual(snap.points_balance, 5000)

    def test_pinned_sorted_to_front_of_digest(self):
        page = self._xhs_page()
        page["items"] = list(reversed(page["items"]))   # 置顶排在最后
        snap = analyze.read_comment_page("xhs", page)
        digest = analyze.format_digest(snap, Settings().digest)
        self.assertTrue(digest.splitlines()[0].startswith("1. [置顶"))

    def test_douyin_pinned_is_explicitly_unsupported(self):
        page = {"items": [{"content": "哈哈哈", "like_count": 5, "is_hot": True,
                           "ip_location": "浙江", "author": {"nickname": "路人"}}],
                "comment_count": None, "points": {"cost": 10, "balance": 4990}}
        snap = analyze.read_comment_page("douyin", page)
        self.assertFalse(snap.supports_pinned)
        self.assertEqual(analyze.format_pinned(snap), analyze.DOUYIN_PINNED_UNSUPPORTED)
        self.assertIsNone(snap.comment_count)          # 必须由 detail 兜底

    def test_douyin_detail_backfills_null_comment_count(self):
        snap = analyze.read_comment_page("douyin", {"items": [], "comment_count": None})
        analyze.merge_detail(snap, {"like_count": 900, "comment_count": 77, "collect_count": 12})
        self.assertEqual(snap.comment_count, 77)
        self.assertEqual(snap.like_count, 900)

    def test_detail_does_not_clobber_known_comment_count(self):
        snap = analyze.read_comment_page("xhs", self._xhs_page(count=42))
        analyze.merge_detail(snap, {"comment_count": 999, "like_count": 1})
        self.assertEqual(snap.comment_count, 42)   # 评论接口的数更贴近评论区实况

    def test_digest_respects_char_budget(self):
        page = self._xhs_page()
        page["items"] = [{"content": "很长的评论" * 200, "like_count": 1, "is_pinned": False,
                          "is_author_comment": False, "ip_location": "", "author": {"name": "X"}}] * 30
        snap = analyze.read_comment_page("xhs", page)
        digest = analyze.format_digest(snap, Settings().digest)
        self.assertLessEqual(len(digest), Settings().digest.total_chars + 40)

    def test_empty_comments_is_not_an_error(self):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertEqual(analyze.format_digest(snap, Settings().digest), "（暂无评论）")
        self.assertEqual(analyze.format_pinned(snap), "")


class TestPinnedState(unittest.TestCase):
    """置顶判定。帖子是我们自己发的，置顶评论必然是我方置顶的——
    所以只需要回答「置顶还在不在」，不做任何内容比对。"""

    def _snap(self, comments, platform="xhs"):
        items = [
            {"content": text, "is_pinned": pinned, "like_count": 0,
             "is_author_comment": pinned, "ip_location": "", "author": {"name": "某人"}}
            for text, pinned in comments
        ]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": len(items)})

    def test_pinned_exists(self):
        self.assertIs(analyze.decide_pin(self._snap([("随便什么内容", True)])),
                      analyze.Pin.PINNED)

    def test_no_pinned(self):
        self.assertIs(analyze.decide_pin(self._snap([("路过", False)])),
                      analyze.Pin.NONE_PINNED)

    def test_douyin_always_unsupported(self):
        self.assertIs(analyze.decide_pin(self._snap([("哈哈", False)], platform="douyin")),
                      analyze.Pin.UNSUPPORTED)


class TestSeedKeywordMatch(unittest.TestCase):
    """蓝词组 × 第一页评论：任一词出现在任一条评论里即算命中，
    返回命中的词和那条评论。规则刻意简单，没有长度门槛。"""

    def _snap(self, texts):
        items = [{"content": t, "is_pinned": False, "is_author_comment": False,
                  "like_count": 0, "ip_location": "", "author": {"name": "x"}}
                 for t in texts]
        return analyze.read_comment_page("xhs", {"items": items, "comment_count": len(items)})

    KEYWORDS = ["西地那非口溶膜", "艾时达口溶膜", "cGMP因子"]

    def test_any_keyword_in_any_comment_hits(self):
        snap = self._snap(["路过", "朋友推荐了艾时达口溶膜，用着还行", "求链接"])
        hit = analyze.match_seed_keywords(snap, self.KEYWORDS)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.keyword, "艾时达口溶膜")
        self.assertIn("艾时达口溶膜", hit.comment)

    def test_case_insensitive(self):
        """实际表里「cGMP因子」和「cgmp因子」混着写，必须都能命中。"""
        snap = self._snap(["听说是 CGMP 因子的原理？"])
        self.assertIsNotNone(analyze.match_seed_keywords(snap, ["cgmp因子"]))

    def test_keyword_order_decides_which_hit_is_reported(self):
        snap = self._snap(["西地那非口溶膜和艾时达口溶膜都见过"])
        hit = analyze.match_seed_keywords(snap, self.KEYWORDS)
        self.assertEqual(hit.keyword, "西地那非口溶膜")

    def test_no_hit_returns_none(self):
        snap = self._snap(["好用", "求链接"])
        self.assertIsNone(analyze.match_seed_keywords(snap, self.KEYWORDS))

    def test_empty_keywords_no_crash(self):
        snap = self._snap(["好用"])
        self.assertIsNone(analyze.match_seed_keywords(snap, []))
        self.assertIsNone(analyze.match_seed_keywords(snap, ["", "  "]))

    def test_decide_wires_hit_into_verdict(self):
        snap = self._snap(["用的西地那非口溶膜"])
        v = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=10,
                           seed_keywords=self.KEYWORDS)
        self.assertTrue(v.seed_checked)
        self.assertEqual(v.seed_hit.keyword, "西地那非口溶膜")
        self.assertIn("命中「西地那非口溶膜」", analyze.format_seed_match(v, snap))

    def test_decide_reports_miss_in_notes(self):
        snap = self._snap(["好用", "路过"])
        v = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=10,
                           seed_keywords=self.KEYWORDS)
        self.assertTrue(v.seed_checked)
        self.assertIsNone(v.seed_hit)
        self.assertTrue(any("未命中" in n for n in v.notes))
        self.assertIn("未命中", analyze.format_seed_match(v, snap))

    def test_no_keywords_means_column_untouched(self):
        snap = self._snap(["好用"])
        v = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=10)
        self.assertFalse(v.seed_checked)
        self.assertIsNone(analyze.format_seed_match(v, snap))


class TestHeatTiers(unittest.TestCase):
    """热度三档：≥20 评估中，≥50 爆贴，≥100 大爆。互斥，取最高，只升不降。"""

    def setUp(self):
        self.settings = Settings()

    def _snap(self, count, platform="xhs"):
        return analyze.read_comment_page(platform, {"items": [], "comment_count": count})

    def decide(self, count, **kw):
        kw.setdefault("previous_comment_count", None)
        kw.setdefault("age_hours", 10)
        return analyze.decide(self._snap(count), self.settings, **kw)

    def test_boundaries(self):
        cases = [(0, None), (19, None), (20, "评估中"), (49, "评估中"),
                 (50, "爆贴"), (99, "爆贴"), (100, "大爆"), (9999, "大爆")]
        for count, expected in cases:
            heat = {t for t in self.decide(count).tags
                    if self.settings.tags.rank(t) >= 0}
            self.assertEqual(heat, {expected} if expected else set(), f"评论数 {count}")

    def test_tiers_are_mutually_exclusive(self):
        """同时挂着评估中+爆贴+大爆没有意义，只能有一个。"""
        for count in (25, 60, 500):
            heat = [t for t in self.decide(count).tags if self.settings.tags.rank(t) >= 0]
            self.assertEqual(len(heat), 1, f"评论数 {count} 得到 {heat}")

    def test_tier_upgrades_as_comments_grow(self):
        v = self.decide(120, current_tags=["爆贴"])
        self.assertIn("大爆", v.tags)
        self.assertNotIn("爆贴", v.tags)

    def test_tier_never_downgrades(self):
        """评论被删导致数字掉下去，不该让一条帖子从大爆退回爆贴——
        那是风控信号，该由风控标签表达。"""
        v = self.decide(30, current_tags=["大爆"])
        self.assertIn("大爆", v.tags)
        self.assertNotIn("评估中", v.tags)
        self.assertTrue(any("保留高档位" in n for n in v.notes))

    def test_same_thresholds_for_both_platforms(self):
        for platform in ("xhs", "douyin"):
            v = analyze.decide(self._snap(60, platform), self.settings,
                               previous_comment_count=None, age_hours=10)
            self.assertIn("爆贴", v.tags, platform)

    def test_human_tags_do_not_confuse_the_ratchet(self):
        v = self.decide(60, current_tags=["已复盘", "客户确认"])
        self.assertIn("爆贴", v.tags)


class TestRiskDetection(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def _snap(self, count):
        return analyze.read_comment_page("xhs", {"items": [], "comment_count": count})

    def decide(self, count, **kw):
        kw.setdefault("previous_comment_count", None)
        kw.setdefault("age_hours", 10)
        return analyze.decide(self._snap(count), self.settings, **kw)

    def test_comment_halving_flags_risk(self):
        self.assertIn("风控中", self.decide(20, previous_comment_count=100).tags)

    def test_small_baseline_drop_is_noise(self):
        self.assertNotIn("风控中", self.decide(2, previous_comment_count=8).tags)

    def test_zero_comments_after_window_flags_risk(self):
        self.assertIn("风控中", self.decide(0, age_hours=72).tags)

    def test_zero_comments_during_cold_start_is_fine(self):
        self.assertNotIn("风控中", self.decide(0, age_hours=3).tags)

    def test_risk_is_volatile_and_clears_on_recovery(self):
        """恢复正常要能自动摘掉，否则表会越来越红，最后没人看。"""
        v = self.decide(80, previous_comment_count=75, current_tags=["风控中"])
        self.assertNotIn("风控中", v.tags)

    def test_gone_tags_both_gone_and_risk(self):
        self.assertEqual(analyze.gone_verdict(self.settings).tags, {"已失效", "风控中"})

    def test_first_strike_tags_nothing(self):
        """一次抖动就把好帖子标成风控 = 运营全线停投。绝不能发生。"""
        self.assertEqual(analyze.suspect_verdict(self.settings, 1, "取不到").tags, set())

    def test_all_decided_tags_stay_inside_namespace(self):
        ns = set(self.settings.tags.namespace())
        for platform in ("xhs", "douyin"):
            for count in (0, 19, 20, 50, 100, 5000):
                for prev in (None, 0, 10, 500):
                    for age in (1, 50, 500):
                        snap = analyze.read_comment_page(platform, {
                            "items": [{"content": "官方置顶文案在此", "is_pinned": True,
                                       "is_author_comment": True, "like_count": 0,
                                       "ip_location": "", "author": {"name": "官号"}}],
                            "comment_count": count})
                        v = analyze.decide(snap, self.settings, previous_comment_count=prev,
                                           age_hours=age,
                                           seed_keywords=["官方置顶文案在此"])
                        self.assertTrue(v.tags <= ns, f"{v.tags} 越界")


class TestCommentStatusColumn(unittest.TestCase):
    """「评论状态」由评论关键词的命中结果驱动：命中=显示评论，
    未命中=没有显示。和置顶无关（置顶内容由「置顶评论」列单独展示），
    匹配的是第一页评论内容，所以抖音行同样能判。"""

    def setUp(self):
        self.settings = Settings()
        self.cs = self.settings.comment_status

    def _snap(self, texts=("这条真好用",), platform="xhs", count=1):
        items = [{"content": t, "is_pinned": False, "is_author_comment": False,
                  "like_count": 0, "ip_location": "", "author": {"name": "路人"}}
                 for t in texts]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": count})

    def values(self, snap, keywords):
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, seed_keywords=keywords)
        return analyze.comment_status_values(v, self.settings)

    def test_hit_means_displayed(self):
        snap = self._snap(("路过", "艾时达口溶膜真不错"))
        self.assertEqual(self.values(snap, ["艾时达口溶膜"]), {self.cs.displayed})

    def test_miss_means_not_displayed(self):
        self.assertEqual(self.values(self._snap(("路过",)), ["艾时达口溶膜"]),
                         {self.cs.not_displayed})

    def test_no_keywords_returns_none_not_empty(self):
        """None = 不碰这一列；空集合会把上一轮的结论摘掉，两者天差地别。"""
        self.assertIsNone(self.values(self._snap(), []))

    def test_douyin_rows_are_judged_too(self):
        """旧口径靠置顶字段所以抖音判不了；新口径匹配第一页评论内容，
        两个平台都拿得到。"""
        snap = self._snap(("cgmp因子 冲了",), platform="douyin")
        self.assertEqual(self.values(snap, ["cGMP因子"]), {self.cs.displayed})

    def test_empty_shell_round_returns_none(self):
        """评论页空壳（没有评论、评论数也没拿到）：拿上游缺数当「没有显示」
        的证据会诱导运营去无谓补评论——这一轮必须不碰这一列。"""
        snap = analyze.read_comment_page("douyin", {"items": [], "comment_count": None})
        self.assertIsNone(self.values(snap, ["艾时达口溶膜"]))

    def test_zero_comments_is_evidence_of_not_displayed(self):
        """评论数确认为 0 的空页不是空壳——第一页真的什么都没有，
        「没有显示」是可靠结论。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertEqual(self.values(snap, ["艾时达口溶膜"]), {self.cs.not_displayed})

    def test_verdict_supersedes_pending(self):
        """「待评论」本质上是还没显示：机器拿到结论后替掉它，不共存；
        这一列里其他人工值原样保留。"""
        merged = tags.merge(["待评论", "重点盯"], {self.cs.displayed},
                            self.cs.namespace(),
                            exclusive=(self.cs.namespace(),))
        self.assertIn(self.cs.displayed, merged.final)
        self.assertNotIn(self.cs.pending, merged.final)
        self.assertIn("重点盯", merged.final)

    def test_loss_is_called_out_in_notes(self):
        """上一轮「置顶评论」列里有内容、这一轮没有置顶 → 置顶刚掉，
        诊断信息里必须报出来。"""
        v = analyze.decide(self._snap(), self.settings, previous_comment_count=None,
                           age_hours=10, previous_pinned="戳主页领券")
        self.assertTrue(any("置顶已不在" in n for n in v.notes))

    def test_douyin_placeholder_is_not_pin_history(self):
        """「—（抖音不支持置顶监控）」是占位不是置顶记录：链接被换过平台的行
        不该因为这个占位报「置顶掉了」。"""
        v = analyze.decide(self._snap(), self.settings, previous_comment_count=None,
                           age_hours=10,
                           previous_pinned=analyze.DOUYIN_PINNED_UNSUPPORTED)
        self.assertFalse(any("置顶已不在" in n for n in v.notes))


class TestCallPlanning(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, cell, age_days=1.0):
        published = self.now - timedelta(days=age_days)
        return rows.Row(record_id="rec1", link_cell=cell,
                        publish_time_ms=int(published.timestamp() * 1000))

    def test_fresh_xhs_note_costs_two_calls(self):
        plan = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24), self.settings, self.now)
        self.assertEqual([c.purpose for c in plan], ["comments", "detail"])
        self.assertEqual(plan[0].arguments["sort"], "default")

    def test_old_xhs_note_costs_one_call(self):
        plan = rows.plan_calls(
            self._row("https://www.xiaohongshu.com/explore/" + "a" * 24, age_days=30), self.settings, self.now)
        self.assertEqual([c.purpose for c in plan], ["comments"])

    def test_detail_can_be_switched_off_entirely(self):
        self.settings.detail_within_days = 0
        plan = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24), self.settings, self.now)
        self.assertEqual(len(plan), 1)

    def test_douyin_always_costs_two_calls(self):
        for age in (1, 30, 365):
            plan = rows.plan_calls(
                self._row("https://www.douyin.com/video/7123456789012345678", age_days=age), self.settings, self.now)
            self.assertEqual([c.purpose for c in plan], ["comments", "detail"], f"age={age}")

    def test_unusable_link_costs_nothing(self):
        self.assertEqual(rows.plan_calls(self._row("待补"), self.settings, self.now), [])

    def test_credit_estimate(self):
        batch = [self._row("https://www.xiaohongshu.com/explore/" + "a" * 24, age_days=30)] * 10
        self.assertEqual(rows.estimate_credits(batch, self.settings, self.now), 100)  # 10 篇 × 1 次 × 10 积分


class TestRefreshTiering(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, age_days, updated_hours_ago):
        published = self.now - timedelta(days=age_days)
        updated = self.now - timedelta(hours=updated_hours_ago)
        return rows.Row(record_id="r", link_cell="x",
                        publish_time_ms=int(published.timestamp() * 1000),
                        last_updated_ms=int(updated.timestamp() * 1000))

    def test_fresh_post_due_after_8h(self):
        self.assertTrue(self._row(1, 9).is_due(self.settings, self.now))
        self.assertFalse(self._row(1, 3).is_due(self.settings, self.now))

    def test_week_old_post_due_daily(self):
        self.assertTrue(self._row(5, 25).is_due(self.settings, self.now))
        self.assertFalse(self._row(5, 9).is_due(self.settings, self.now))

    def test_month_old_post_due_every_three_days(self):
        self.assertTrue(self._row(20, 80).is_due(self.settings, self.now))
        self.assertFalse(self._row(20, 25).is_due(self.settings, self.now))

    def test_archived_post_never_due(self):
        self.assertFalse(self._row(45, 9999).is_due(self.settings, self.now))

    def test_never_updated_is_due(self):
        row = rows.Row(record_id="r", link_cell="x",
                       publish_time_ms=int((self.now - timedelta(days=3)).timestamp() * 1000))
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_unknown_publish_time_is_due(self):
        self.assertTrue(rows.Row(record_id="r", link_cell="x").is_due(self.settings, self.now))


class TestProtocol(unittest.TestCase):
    """错误码取自 SocialDataX 公开 OpenAPI 规范的 x-socialdatax-error-contract。

    最关键的一条协议特性：**业务错误也返回 HTTP 200**，靠 body 里有没有 `code`
    字段来区分。只看 HTTP 状态码，会把每一个「笔记已删除」当成成功。
    """

    def rest(self, status, payload, request_id="req-123"):
        return protocol.parse_response(status, "application/json",
                                       json.dumps(payload), request_id)

    # —— 这两条是实地验证过的真实响应 ——

    def test_missing_api_key(self):
        result = self.rest(401, {"code": 1401,
                                 "message": "API Key 缺失，请通过 Authorization 或 X-API-Key 传入。"})
        self.assertEqual(result.kind, protocol.Failure.AUTH)
        self.assertIn(result.kind, protocol.FATAL)

    def test_invalid_api_key(self):
        result = self.rest(401, {"code": 1401, "message": "API Key 无效或已失效。"})
        self.assertEqual(result.kind, protocol.Failure.AUTH)

    # —— HTTP 200 + code 的业务错误，全部来自官方错误码表 ——

    def test_business_error_on_http_200_is_not_success(self):
        """整个协议层最容易写错的一处。"""
        result = self.rest(200, {"code": 1008, "message": "当前作品已删除。"})
        self.assertIsInstance(result, protocol.Err)

    def test_1003_not_found_is_gone_but_not_definitive(self):
        result = self.rest(200, {"code": 1003, "message": "未找到对应内容"})
        self.assertEqual(result.kind, protocol.Failure.GONE)
        self.assertFalse(result.definitive)      # 走两击定罪

    def test_1006_content_unavailable_is_the_fengkong_case(self):
        # 规范原文：「目标内容存在但当前无法读取，例如权限、状态或平台限制导致不可访问」
        result = self.rest(200, {"code": 1006, "message": "当前内容暂时不可用"})
        self.assertEqual(result.kind, protocol.Failure.GONE)
        self.assertFalse(result.definitive)

    def test_1008_content_deleted_is_definitive(self):
        # 规范原文：「作为业务失败处理，不要重试」→ 不必等第二击
        result = self.rest(200, {"code": 1008, "message": "当前作品已删除。"})
        self.assertEqual(result.kind, protocol.Failure.GONE)
        self.assertTrue(result.definitive)

    def test_1007_surface_unavailable_is_transient_not_gone(self):
        """页面暂时打不开 ≠ 内容没了。判错这条会把好帖子标成失效。"""
        result = self.rest(200, {"code": 1007, "message": "当前页面暂时不可访问"})
        self.assertEqual(result.kind, protocol.Failure.TRANSPORT)
        self.assertIn(result.kind, protocol.RETRYABLE)

    def test_1005_service_failure_is_transient(self):
        self.assertEqual(self.rest(200, {"code": 1005, "message": "服务暂时不可用，请稍后重试"}).kind,
                         protocol.Failure.TRANSPORT)

    def test_1004_insufficient_balance_is_fatal(self):
        result = self.rest(200, {"code": 1004, "message": "当前 API Key 积分不足。"})
        self.assertEqual(result.kind, protocol.Failure.QUOTA)
        self.assertIn(result.kind, protocol.FATAL)

    def test_1429_rate_limit_carries_retry_after(self):
        result = protocol.parse_response(429, "application/json", json.dumps(
            {"code": 1429, "message": "请求过于频繁，请稍后重试。",
             "retry_after_seconds": 7, "rate_limit_window_seconds": 60}))
        self.assertEqual(result.kind, protocol.Failure.RATE_LIMIT)
        self.assertNotIn(result.kind, protocol.FATAL)
        self.assertEqual(result.retry_after_seconds, 7.0)   # 字段名来自规范，不是猜的

    def test_1001_invalid_argument_is_not_gone(self):
        """参数错 ≠ 内容没了。混淆会把一个录入错误标成风控。"""
        result = self.rest(200, {"code": 1001, "message": "参数不正确，请检查后重试"})
        self.assertEqual(result.kind, protocol.Failure.UNKNOWN)
        self.assertNotEqual(result.kind, protocol.Failure.GONE)

    # —— 成功与兜底 ——

    def test_success_has_no_code_field(self):
        result = self.rest(200, {"comment_count": 42, "items": [],
                                 "points": {"cost": 10, "balance": 990}})
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["comment_count"], 42)
        self.assertEqual(result.points_balance, 990)
        self.assertEqual(result.points_cost, 10)

    def test_request_id_is_carried_through(self):
        """找厂商排查问题时唯一的凭据。"""
        self.assertEqual(self.rest(200, {"code": 1003, "message": "未找到"}, "abc123").request_id, "abc123")
        self.assertIn("abc123", str(self.rest(200, {"code": 1003, "message": "未找到"}, "abc123")))

    def test_unknown_code_falls_back_to_message_hints(self):
        result = self.rest(200, {"code": 9999, "message": "该内容因违规已下架"})
        self.assertEqual(result.kind, protocol.Failure.GONE)

    def test_server_error_is_transport_retryable(self):
        self.assertEqual(protocol.parse_response(503, "text/html", "<html>bad gateway</html>").kind,
                         protocol.Failure.TRANSPORT)

    def test_unparseable_body(self):
        self.assertIsInstance(protocol.parse_response(200, "application/json", "not json"), protocol.Err)

    # —— 请求组装 ——

    def test_endpoints(self):
        self.assertTrue(protocol.endpoint("xhs", "comments").endswith("/xhs/note/comment/list"))
        self.assertTrue(protocol.endpoint("douyin", "detail").endswith("/douyin/video/detail"))
        with self.assertRaises(ValueError):
            protocol.endpoint("weibo", "comments")

    def test_only_one_auth_header(self):
        """规范明确：Authorization 和 X-API-Key 只能用一种，同时传会被判配置冲突。"""
        sent = protocol.headers("k")
        self.assertIn("Authorization", sent)
        self.assertNotIn("X-API-Key", sent)

    def test_body_is_plain_json_not_jsonrpc(self):
        body = json.loads(protocol.build_body({"note_id": "a" * 24, "sort_type": "default"}))
        self.assertEqual(body, {"note_id": "a" * 24, "sort_type": "default"})

    def test_sse_fallback_still_works(self):
        """REST 不返回 SSE，但 MCP 端点会——留着这条防御分支不亏。"""
        payload = {"jsonrpc": "2.0", "id": 1,
                   "result": {"structuredContent": {"comment_count": 7, "items": []}}}
        result = protocol.parse_response(200, "text/event-stream",
                                         f"event: message\ndata: {json.dumps(payload)}\n\n")
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["comment_count"], 7)

    # —— 传输层网络失败（status=0）的分类 ——

    def test_network_failure_is_transport_and_retryable(self):
        """transport 层把超时/拒连表示成 status=0。归 UNKNOWN 的话既不重试
        也不降级——SocialDataX 作主通道时双通道就白配了。"""
        for body in ("请求超时（30.0s）", "网络错误：Connection refused",
                     "网络错误：timed out"):
            result = protocol.parse_response(0, "", body)
            self.assertEqual(result.kind, protocol.Failure.TRANSPORT, body)
            self.assertIn(result.kind, protocol.RETRYABLE)

    def test_transient_unavailable_hint_beats_gone_hint(self):
        """「当前页面暂时不可用」含「不可用」三个字。GONE 的 hint 排在前面的话，
        一次临时故障会被计成死亡一击——两击定罪的第一击就这么被白送了。"""
        result = self.rest(200, {"code": 9999, "message": "当前页面暂时不可用，请稍后重试"})
        self.assertEqual(result.kind, protocol.Failure.TRANSPORT)


class TestGoneKeepsHeatHistory(unittest.TestCase):
    """「爆过就是爆过」：帖子被删恰恰是最需要留着热度档位供复盘的时候。"""

    def test_gone_verdict_preserves_the_tier(self):
        settings = Settings()
        verdict = analyze.gone_verdict(settings, "已删除", current_tags=["大爆", "已复盘"])
        self.assertEqual(verdict.tags, {"已失效", "风控中", "大爆"})

    def test_gone_verdict_without_history_stays_minimal(self):
        settings = Settings()
        self.assertEqual(analyze.gone_verdict(settings).tags, {"已失效", "风控中"})


class TestMergeWithMissingOptions(unittest.TestCase):
    NS = ["评估中", "爆贴", "大爆", "风控中", "已失效"]

    HEAT = (["评估中", "爆贴", "大爆"],)

    def test_dropped_computed_tag_does_not_strip_existing_machine_tags(self):
        """想写「大爆」但选项没建：这一轮不能顺手把行上的「爆贴」摘掉——
        一次配置疏漏不该抹掉行上仅存的热度信息。"""
        result = tags.merge(["爆贴", "已复盘"], {"大爆"}, self.NS,
                            known_options=["评估中", "爆贴", "风控中", "已失效"],
                            exclusive=self.HEAT)
        self.assertIn("爆贴", result.final)
        self.assertIn("已复盘", result.final)
        self.assertEqual(result.dropped_unknown, ["大爆"])
        self.assertEqual(result.removed, [])

    def test_preservation_does_not_resurrect_unrelated_tags(self):
        """被拦的是「风控中」：只该考虑保留同类的旧值——行上残留的
        「已失效」和它不是一类，照常摘掉。否则一条刚恢复正常、
        本轮成功取到数的行会继续顶着死亡标签。"""
        result = tags.merge(["爆贴", "已失效"], {"爆贴", "风控中"}, self.NS,
                            known_options=["评估中", "爆贴", "大爆", "已失效"],
                            exclusive=self.HEAT)
        self.assertIn("爆贴", result.final)
        self.assertNotIn("已失效", result.final)     # 不复活无关旧标签
        self.assertEqual(result.dropped_unknown, ["风控中"])

    def test_normal_upgrade_still_swaps_the_tier(self):
        """选项齐全时行为不变：升档照样换标签。"""
        result = tags.merge(["爆贴"], {"大爆"}, self.NS, known_options=self.NS)
        self.assertIn("大爆", result.final)
        self.assertNotIn("爆贴", result.final)

    def test_preservation_respects_tier_exclusivity(self):
        """升「大爆」成功、但「风控中」恰好没建选项：保留旧标签的动作
        不能把已让位的「爆贴」又捡回来——热度档位同时只能有一个。"""
        result = tags.merge(
            ["爆贴", "已复盘"], {"大爆", "风控中"}, self.NS,
            known_options=["评估中", "爆贴", "大爆", "已失效"],   # 缺 风控中
            exclusive=(["评估中", "爆贴", "大爆"],),
        )
        self.assertIn("大爆", result.final)
        self.assertNotIn("爆贴", result.final)      # 同组旧档位正常让位
        self.assertIn("已复盘", result.final)
        self.assertEqual(result.dropped_unknown, ["风控中"])


class TestUrlBoundary(unittest.TestCase):
    def test_cjk_text_glued_to_url_is_not_swallowed(self):
        """「看这条https://v.douyin.com/xxx很火」这种没有空格的写法，
        右边界不排 CJK 就会把「很火」吞进 URL，短链直接 404。"""
        parsed = links.parse("看这条https://v.douyin.com/iRxYzAb很火")
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.url, "https://v.douyin.com/iRxYzAb")

    def test_fullwidth_left_bracket_terminates(self):
        parsed = links.parse("链接https://xhslink.com/a/AbC123（点开看）")
        self.assertEqual(parsed.url, "https://xhslink.com/a/AbC123")


class TestArchiveCutoff(unittest.TestCase):
    def test_archive_after_days_actually_extends_the_window(self):
        settings = Settings()
        settings.refresh.archive_after_days = 60
        # 超出最后一档（30 天）但没到归档线：沿用最后一档的间隔
        self.assertEqual(settings.refresh.interval_hours_for_age(45), 72)
        self.assertIsNone(settings.refresh.interval_hours_for_age(61))

    def test_default_behaviour_is_unchanged(self):
        settings = Settings()
        self.assertEqual(settings.refresh.interval_hours_for_age(20), 72)
        self.assertIsNone(settings.refresh.interval_hours_for_age(31))


class TestEstimateByActualChannel(unittest.TestCase):
    def _xhs_row(self):
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        return rows.Row(record_id="r",
                        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
                        publish_time_ms=int((now - timedelta(days=30)).timestamp() * 1000)), now

    def test_price_follows_the_channel_that_will_actually_run(self):
        """只配了 SocialDataX 的 key：估算就得按 SDX 的 ¥0.10 报，
        按配置主通道 TikHub 的 ¥0.072 报就是错账。"""
        row, now = self._xhs_row()
        settings = Settings()
        yuan = rows.estimate_yuan([row], settings, now, keys={"socialdatax": "s"})
        self.assertAlmostEqual(yuan, 0.10)

    def test_douyin_short_link_is_priced_at_socialdatax(self):
        """抖音短链行 TikHub 接不了，实际会走 SDX——估算要跟着能力路由走。"""
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = rows.Row(record_id="r", link_cell="https://v.douyin.com/iRxYzAb/",
                       publish_time_ms=int((now - timedelta(days=1)).timestamp() * 1000))
        settings = Settings()
        yuan = rows.estimate_yuan([row], settings, now,
                                  keys={"tikhub": "t", "socialdatax": "s"})
        self.assertAlmostEqual(yuan, 0.20)   # 评论 + detail 各 ¥0.10

    def test_row_no_channel_can_handle_costs_nothing(self):
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        row = rows.Row(record_id="r", link_cell="https://v.douyin.com/iRxYzAb/",
                       publish_time_ms=int((now - timedelta(days=1)).timestamp() * 1000))
        yuan = rows.estimate_yuan([row], Settings(), now, keys={"tikhub": "t"})
        self.assertEqual(yuan, 0.0)


if __name__ == "__main__":
    unittest.main()
