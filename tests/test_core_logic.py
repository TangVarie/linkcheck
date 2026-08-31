"""核心逻辑的单测。全部离线，不需要 API Key，不发任何请求。

跑法：python3 -m unittest discover -s tests -v
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from xhsearch import analyze, links, protocol, rows, tags
from xhsearch.config import Settings

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


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
        self.assertIs(analyze.decide_pin(snap), analyze.Pin.UNSUPPORTED)
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
    """评论关键词组 × 第一页评论：任一词出现在任一条评论里即算命中，
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
        # 命中的那条评论排在快照第一行、带「命中」标记——不占单独一列
        digest = analyze.format_digest(snap, Settings().digest, hit=v.seed_hit)
        self.assertTrue(digest.splitlines()[0].startswith("1. [命中「西地那非口溶膜」"))

    def test_decide_reports_miss_in_notes(self):
        snap = self._snap(["好用", "路过"])
        v = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=10,
                           seed_keywords=self.KEYWORDS)
        self.assertTrue(v.seed_checked)
        self.assertIsNone(v.seed_hit)
        self.assertTrue(any("未命中" in n for n in v.notes))

    def test_no_keywords_means_column_untouched(self):
        snap = self._snap(["好用"])
        v = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=10)
        self.assertFalse(v.seed_checked)
        self.assertIsNone(analyze.comment_status_value(v, Settings()))


def _page(*comments, count=42):
    return analyze.read_comment_page("xhs", {
        "items": [{"content": c, "like_count": 5, "ip_location": "上海",
                   "author": {"name": "路人"}} for c in comments],
        "comment_count": count,
    })


class TestNegativeKeywords(unittest.TestCase):
    """负面词/竞品词：和「评论关键词」同一套机制、相反的方向。

    共用**同一份**第一页评论，不额外发请求、不翻页——多一列判定不该多花钱。
    """

    def setUp(self):
        self.settings = Settings()

    def decide(self, snapshot, **kw):
        return analyze.decide(snapshot, self.settings,
                              previous_comment_count=None, age_hours=10, **kw)

    def test_hit_marks_the_row_and_lists_the_offending_comments(self):
        snap = _page("好用，回购了", "用了过敏，客服还不理人", "我一直用竞品A")
        verdict = self.decide(snap, negative_keywords=["过敏", "竞品A"])
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.found)
        digest = analyze.format_negative_digest(verdict.negative_hits, self.settings.digest)
        self.assertIn("命中「过敏」", digest)
        self.assertIn("命中「竞品A」", digest)
        self.assertNotIn("回购", digest, "没命中的评论不该进负面快照")

    def test_configured_but_clean_is_an_explicit_verdict(self):
        """「查过、没中」和「没查过」是两个不同的结论，不能都留空。"""
        verdict = self.decide(_page("好用，回购了"), negative_keywords=["过敏"])
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.clean)
        self.assertEqual(
            analyze.format_negative_digest(verdict.negative_hits, self.settings.digest),
            "（未命中）")

    def test_no_negative_words_configured_leaves_the_column_alone(self):
        verdict = self.decide(_page("用了过敏"))
        self.assertFalse(verdict.negative_checked)
        self.assertIsNone(analyze.negative_status_value(verdict, self.settings))

    def test_empty_comment_page_never_claims_clean(self):
        """空壳轮拿上游缺数当「无负面」写进表里，等于给运营一个假的安全感。"""
        empty = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        verdict = self.decide(empty, negative_keywords=["过敏"])
        self.assertFalse(verdict.negative_checked)
        self.assertIsNone(analyze.negative_status_value(verdict, self.settings))
        self.assertTrue(any("保持原样" in n for n in verdict.notes))

    def test_the_brands_own_reply_never_counts_as_negative(self):
        """负面词问的是「**别人**说了什么」。自家回复里的「不会过敏」
        按字面照样命中，会把一条干净的帖子写成「有负面」。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [
                {"content": "温和配方，不会过敏", "like_count": 9,
                 "is_author_comment": True, "author": {"name": "官号"}},
                {"content": "好用，回购了", "like_count": 3,
                 "author": {"name": "路人"}},
            ],
            "comment_count": 2,
        })
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertEqual(verdict.negative_hits, [])
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.clean)

    def test_a_real_person_saying_it_still_counts(self):
        """把自家回复排除掉，不能顺手把真正的负面也漏了。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [
                {"content": "温和配方，不会过敏", "like_count": 9,
                 "is_author_comment": True, "author": {"name": "官号"}},
                {"content": "我用了过敏，退款还难", "like_count": 31,
                 "author": {"name": "路人"}},
            ],
            "comment_count": 2,
        })
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertEqual(len(verdict.negative_hits), 1)
        self.assertEqual(verdict.negative_hits[0].comment.content, "我用了过敏，退款还难")

    def test_seed_keywords_still_see_the_authors_own_comment(self):
        """反方向：自家置顶的引导评论正是「评论关键词」要找的东西。
        两个方向天生相反，跳过作者的开关不能串到种子词那边。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [{"content": "戳主页领券", "like_count": 10,
                       "is_author_comment": True, "author": {"name": "官号"}}],
            "comment_count": 1,
        })
        verdict = self.decide(snap, seed_keywords=["领券"])
        self.assertIsNotNone(verdict.seed_hit)

    def test_matching_is_as_loose_as_seed_keywords(self):
        """和种子词共用同一套归一化：大小写、空格、标点都不该影响命中。"""
        snap = _page("这个 Xx 牌 更好用！")
        verdict = self.decide(snap, negative_keywords=["xx牌"])
        self.assertTrue(verdict.negative_hits)

    def test_one_comment_counts_once_even_if_it_hits_several_words(self):
        snap = _page("过敏了，还是竞品A好")
        verdict = self.decide(snap, negative_keywords=["过敏", "竞品A"])
        self.assertEqual(len(verdict.negative_hits), 1)
        self.assertEqual(verdict.negative_hits[0].keyword, "过敏")   # 按表里的词序取第一个

    def test_an_empty_page_on_a_busy_post_is_never_a_verdict(self):
        """评论接口返回空页、detail 兜底回填「评论数 150」——我们对这条
        帖子的评论区**一眼都没看到**，却曾经据此下了三个结论：
        「无负面」「评论没有显示」「（暂无评论）」，还把上一轮真实的
        评论快照覆盖掉。全是拿上游缺数当证据。
        """
        snap = analyze.read_comment_page("xhs", {"items": []})
        analyze.merge_detail(snap, {"comment_count": 150, "like_count": 9})
        self.assertEqual(snap.comment_count, 150)      # 帖子确实活着
        self.assertFalse(analyze.saw_comment_page(snap))   # 但没看到评论内容

        verdict = self.decide(snap, seed_keywords=["领券"],
                              negative_keywords=["过敏"])
        self.assertFalse(verdict.negative_checked)
        self.assertIsNone(analyze.negative_status_value(verdict, self.settings))
        self.assertFalse(verdict.seed_checked)
        self.assertIsNone(analyze.comment_status_value(verdict, self.settings))
        self.assertFalse(verdict.pin_checked)

    def test_a_post_with_genuinely_zero_comments_is_a_verdict(self):
        """「量到了、就是 0」是结论，「压根没量到」才不是。别把两者一起拦掉。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertTrue(analyze.saw_comment_page(snap))
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertTrue(verdict.negative_checked)
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.clean)

    def test_negative_and_seed_keywords_are_independent(self):
        """两列查的是相反的东西，互不干扰：我们的评论显示出来了，
        底下同时有人骂——这两件事都要如实写出来。"""
        snap = _page("戳主页领券", "用了过敏")
        verdict = self.decide(snap, seed_keywords=["领券"], negative_keywords=["过敏"])
        self.assertEqual(analyze.comment_status_value(verdict, self.settings),
                         self.settings.comment_status.displayed)
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.found)

    def test_douyin_rows_are_covered_too(self):
        """匹配的是评论正文，不依赖置顶字段，所以抖音同样能判。"""
        snap = analyze.read_comment_page("douyin", {
            "items": [{"content": "用了过敏", "like_count": 1}], "comment_count": 9})
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.found)

    def test_digest_respects_the_privacy_switches(self):
        snap = _page("用了过敏")
        verdict = self.decide(snap, negative_keywords=["过敏"])
        fmt = self.settings.digest
        fmt.show_author_name = False
        fmt.show_ip_location = False
        digest = analyze.format_negative_digest(verdict.negative_hits, fmt)
        self.assertNotIn("路人", digest)
        self.assertNotIn("上海", digest)
        self.assertIn("过敏", digest)

    def test_digest_is_capped_like_the_normal_one(self):
        snap = _page(*[f"过敏第{i}条" for i in range(20)])
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertEqual(len(verdict.negative_hits), 20)
        digest = analyze.format_negative_digest(verdict.negative_hits, self.settings.digest)
        self.assertLessEqual(len(digest.splitlines()), self.settings.digest.max_comments)


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
        # age_hours=10 还在冷启动窗口内，所以够不上 20 条的落「观察中」
        # （窗口外是「无水花」，见 TestRiskDetection）。
        cases = [(0, "观察中"), (19, "观察中"), (20, "评估中"), (49, "评估中"),
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

    def test_comment_halving_means_throttled_not_risk(self):
        """腰斩是数字异常（软证据）→ 疑似限流；风控中只认硬证据。"""
        tags_ = self.decide(20, previous_comment_count=100).tags
        self.assertIn("疑似限流", tags_)
        self.assertNotIn("风控中", tags_)

    def test_small_baseline_drop_is_noise(self):
        tags_ = self.decide(2, previous_comment_count=8).tags
        self.assertNotIn("疑似限流", tags_)
        self.assertNotIn("风控中", tags_)

    def test_low_comments_after_window_means_flop(self):
        """发出去够久还起不来 = 无水花，不是限流也不是风控——
        把「没起来」标成异常，运营会去查一个不存在的问题。"""
        for count in (0, 5, 19):
            tags_ = self.decide(count, age_hours=72).tags
            self.assertIn("无水花", tags_, f"评论数 {count}")
            self.assertNotIn("风控中", tags_)
            self.assertNotIn("疑似限流", tags_)

    def test_low_comments_during_cold_start_is_observing_not_flop(self):
        """刚发三小时只有几条评论再正常不过——不是「无水花」，但也**不能留空**。

        留空是这个项目踩过的坑：一批新帖巡查完 `流量状态` 全是空的，
        运营分不出「还没轮到它」「轮到了没结论」和「机器坏了」，
        只能一条条手工去填。「观察中」把这三种情况分开，
        而且到 48 小时会自己升成「无水花」，不需要人去清。
        """
        v = self.decide(0, age_hours=3)
        self.assertEqual(v.tags, {"观察中"})
        self.assertNotIn("无水花", v.tags)
        self.assertTrue(any("冷启动窗口" in n for n in v.notes))

    def test_observing_upgrades_to_flop_once_the_window_closes(self):
        """观察中是最低档：到点没起来就升成无水花，起来了就升更高，绝不倒退。"""
        aged = self.decide(3, age_hours=72, current_tags=["观察中"])
        self.assertEqual(aged.tags, {"无水花"})
        grew = self.decide(25, age_hours=10, current_tags=["观察中"])
        self.assertEqual(grew.tags, {"评估中"})
        # 反过来不成立：已经无水花的行不会因为「窗口内」退回观察中
        self.assertEqual(self.decide(0, age_hours=3, current_tags=["无水花"]).tags,
                         {"无水花"})

    def test_observing_can_be_switched_off(self):
        """TAG_OBSERVING= 关掉这一档，行为完全退回从前（一个标签都不打）。"""
        self.settings.tags.observing = ""
        self.assertEqual(self.decide(0, age_hours=3).tags, set())

    def test_missing_publish_time_says_so_instead_of_going_quiet(self):
        """发布时间是空的 → 算不出发布多久，热度判不了。

        这时同样不能默默留空：留空看起来和「机器没跑」一模一样。
        诊断信息要指出是**表里那一格缺数据**，填上就自动补判。
        """
        v = self.decide(3, age_hours=None)
        self.assertEqual({t for t in v.tags if self.settings.tags.rank(t) >= 0}, set())
        self.assertTrue(any("发布时间" in n for n in v.notes), v.notes)

    def test_bad_publish_time_is_reported_even_when_a_tier_is_retained(self):
        """表里已经有档位时也必须报出来——这是最难发现的一种卡死。

        一行卡在「观察中」、发布时间却是空的或未来的：棘轮保住了旧档位，
        诊断信息只会说「保留高档位」，而真正的问题是那一格坏了——
        它永远熬不到 48 小时，也就永远升不成「无水花」，表面上一切正常。
        """
        for age, keyword in ((None, "是空的"), (-30.0, "未来时间")):
            with self.subTest(age=age):
                v = self.decide(3, age_hours=age, current_tags=["观察中"])
                self.assertIn("观察中", v.tags)       # 棘轮照旧不倒退
                self.assertTrue(any(keyword in n for n in v.notes), v.notes)
                self.assertTrue(any("发布时间" in n for n in v.notes), v.notes)

    def test_future_publish_time_never_gets_the_cold_start_tier(self):
        """发布时间填成未来（年份手滑、时区搞反）：负的时长同样「不足 48 小时」，
        照打「观察中」的话，一个看着很合理的标签会把日期错误盖住，
        而且要等到那个错误时间点之后 48 小时才可能露出来。"""
        v = self.decide(3, age_hours=-30.0)
        self.assertEqual({t for t in v.tags if self.settings.tags.rank(t) >= 0}, set())
        self.assertNotIn("观察中", v.tags)
        self.assertTrue(any("未来时间" in n for n in v.notes), v.notes)

    def test_a_real_tier_from_comment_count_does_not_need_the_publish_time(self):
        """评论数够得上档位时发布时长根本不参与判定——这时不该报缺数据，
        否则每一行没填发布时间的爆贴都会带一句与判定无关的告警。"""
        v = self.decide(60, age_hours=None)
        self.assertIn("爆贴", v.tags)
        self.assertFalse(any("发布时间" in n for n in v.notes), v.notes)

    def test_flop_ratchets_up_but_never_back(self):
        """无水花是最低热度档：起来了就换高档，绝不从评估中退回无水花。"""
        up = self.decide(25, age_hours=72, current_tags=["无水花"])
        self.assertIn("评估中", up.tags)
        self.assertNotIn("无水花", up.tags)
        down = self.decide(3, age_hours=72, current_tags=["评估中"])
        self.assertIn("评估中", down.tags)
        self.assertNotIn("无水花", down.tags)

    def test_censor_flag_from_upstream_flags_risk(self):
        """风控中的硬证据之一：上游明确返回了审核/受限标记。"""
        snap = self._snap(30)
        snap.censored = True
        v = analyze.decide(snap, self.settings, previous_comment_count=None, age_hours=10)
        self.assertIn("风控中", v.tags)
        self.assertTrue(any("审核中/受限" in n for n in v.notes))

    def test_throttled_is_volatile_and_clears_on_recovery(self):
        """恢复正常要能自动摘掉，否则表会越来越红，最后没人看。"""
        v = self.decide(80, previous_comment_count=75,
                        current_tags=["疑似限流", "风控中"])
        self.assertNotIn("疑似限流", v.tags)
        self.assertNotIn("风控中", v.tags)

    def test_unknown_count_round_keeps_throttled_alarm(self):
        """评论数没取到的轮次没有新证据，生效中的疑似限流不能被抹掉。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, current_tags=["疑似限流"])
        self.assertIn("疑似限流", v.tags)

    def test_gone_maps_to_risk_only(self):
        """链接失效 = 风控中的另一种硬证据；「已失效」标签已退役。"""
        self.assertEqual(analyze.gone_verdict(self.settings).tags, {"风控中"})

    def test_a_clear_jump_is_a_surge(self):
        """起量是掉量的镜像：涨幅够 + 增量够，两个闸门都过才算。"""
        v = self.decide(60, previous_comment_count=12)
        self.assertTrue(v.surged)
        self.assertTrue(any("起量" in n for n in v.notes))
        # 它不是标签——热度档位说「现在多热」，起量说「这一轮涨得猛」
        self.assertLessEqual(v.tags, set(self.settings.tags.namespace()))

    def test_the_first_ever_reading_is_never_a_surge(self):
        """**这一条最要紧。** 第一次量这一行时手里只有一个孤零零的数字，
        没有任何速率信息。漏了它，一张刚入册的老表（每条都几百评论）
        会被整表盖上同一个「起量时间」——正好是入册那天，而且是错的。
        """
        self.assertFalse(self.decide(800, previous_comment_count=None).surged)

    def test_a_tiny_base_does_not_fake_a_surge(self):
        """2 → 6 是 +200%，但那是三条评论的事。绝对增量闸挡的就是它。"""
        self.assertFalse(self.decide(6, previous_comment_count=2).surged)

    def test_a_big_post_growing_normally_is_not_a_surge(self):
        """1000 → 1100 增量够了，涨幅够不上：大体量帖子的日常波动不是起飞。"""
        self.assertFalse(self.decide(1100, previous_comment_count=1000).surged)

    def test_zero_baseline_falls_back_to_the_absolute_gate(self):
        """上一轮真的是 0 条：比例闸退化（乘法不除法，不会炸），
        由绝对增量说了算。"""
        self.assertTrue(self.decide(30, previous_comment_count=0).surged)
        self.assertFalse(self.decide(3, previous_comment_count=0).surged)

    def test_a_round_without_a_count_is_never_a_surge(self):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        v = analyze.decide(snap, self.settings, previous_comment_count=10,
                           age_hours=10)
        self.assertFalse(v.surged)

    def test_a_drop_and_a_surge_cannot_both_fire(self):
        """一个数不可能同时涨一半和跌一半。两条口径互斥，钉住别改出交集。"""
        for prev, count in ((100, 20), (12, 60), (40, 41), (0, 0)):
            v = self.decide(count, previous_comment_count=prev)
            self.assertFalse(v.surged and "疑似限流" in v.tags,
                             f"{prev} → {count} 同时判成了起量和限流")

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
    """「评论状态」单选：由评论关键词的命中结果驱动，直接覆盖当前值。
    命中=显示评论，未命中=没有显示；匹配的是第一页评论内容，
    不依赖置顶字段，所以抖音行同样能判。"""

    def setUp(self):
        self.settings = Settings()
        self.cs = self.settings.comment_status

    def _snap(self, texts=("这条真好用",), platform="xhs", count=1):
        items = [{"content": t, "is_pinned": False, "is_author_comment": False,
                  "like_count": 0, "ip_location": "", "author": {"name": "路人"}}
                 for t in texts]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": count})

    def value(self, snap, keywords):
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, seed_keywords=keywords)
        return analyze.comment_status_value(v, self.settings)

    def test_hit_means_displayed(self):
        snap = self._snap(("路过", "艾时达口溶膜真不错"))
        self.assertEqual(self.value(snap, ["艾时达口溶膜"]), self.cs.displayed)

    def test_miss_means_not_displayed(self):
        self.assertEqual(self.value(self._snap(("路过",)), ["艾时达口溶膜"]),
                         self.cs.not_displayed)

    def test_no_keywords_returns_none(self):
        """None = 不碰这一列，保持人工填的任何值。"""
        self.assertIsNone(self.value(self._snap(), []))

    def test_douyin_rows_are_judged_too(self):
        """匹配的是第一页评论内容，不依赖置顶字段——抖音行同样有效。"""
        snap = self._snap(("cgmp因子 冲了",), platform="douyin")
        self.assertEqual(self.value(snap, ["cGMP因子"]), self.cs.displayed)

    def test_empty_shell_round_returns_none(self):
        """评论页空壳（没有评论、评论数也没拿到）：拿上游缺数当「没有显示」
        的证据会诱导运营去无谓补评论——这一轮必须不碰这一列。"""
        snap = analyze.read_comment_page("douyin", {"items": [], "comment_count": None})
        self.assertIsNone(self.value(snap, ["艾时达口溶膜"]))

    def test_zero_comments_is_evidence_of_not_displayed(self):
        """评论数确认为 0 的空页不是空壳——第一页真的什么都没有，
        「没有显示」是可靠结论。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertEqual(self.value(snap, ["艾时达口溶膜"]), self.cs.not_displayed)


class TestPinStatusColumn(unittest.TestCase):
    """「置顶状态」单选：置顶成功/置顶掉了/无置顶，直接覆盖。
    「掉了」和「从来没有」的区分全看这一列自己的历史。"""

    def setUp(self):
        self.settings = Settings()
        self.ps = self.settings.pin_status

    def _snap(self, pinned, platform="xhs", count=1, empty=False):
        items = [] if empty else [
            {"content": "戳主页领券" if pinned else "路过", "is_pinned": pinned,
             "is_author_comment": pinned, "like_count": 0, "ip_location": "",
             "author": {"name": "官号"}}]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": count})

    def value(self, snap, current=""):
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=current)
        return analyze.pin_status_value(v, current, self.settings)

    def test_pinned_means_ok(self):
        """自家帖子，有置顶就是「置顶成功」，不做内容比对。"""
        self.assertEqual(self.value(self._snap(True)), self.ps.pinned_ok)

    def test_never_pinned_when_no_history(self):
        self.assertEqual(self.value(self._snap(False)), self.ps.never_pinned)

    def test_lost_when_previously_ok(self):
        self.assertEqual(self.value(self._snap(False), current=self.ps.pinned_ok),
                         self.ps.pinned_lost)

    def test_lost_stays_lost(self):
        """掉了之后一直没恢复保持「置顶掉了」，不退回「无置顶」——
        曾经置顶过这件事不抹掉。"""
        self.assertEqual(self.value(self._snap(False), current=self.ps.pinned_lost),
                         self.ps.pinned_lost)

    def test_recovery_back_to_ok(self):
        self.assertEqual(self.value(self._snap(True), current=self.ps.pinned_lost),
                         self.ps.pinned_ok)

    def test_douyin_returns_none(self):
        """抖音评论接口没有置顶字段，判不了——不碰这一列，
        绝不写「无置顶」冒充结论。"""
        self.assertIsNone(self.value(self._snap(False, platform="douyin")))

    def test_empty_shell_round_returns_none(self):
        """空壳轮（评论和评论数都没拿到）没资格说「掉了」——
        拿上游缺数当证据的假告警会让运营白跑一趟手机端核对。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        self.assertIsNone(self.value(snap, current=self.ps.pinned_ok))

    def test_loss_transition_is_called_out_in_notes(self):
        """从「置顶成功」掉下来的那一轮，诊断信息里要额外报一声。"""
        v = analyze.decide(self._snap(False), self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_ok)
        self.assertTrue(any("置顶已不在" in n for n in v.notes))

    def test_already_lost_does_not_repeat_the_note(self):
        v = analyze.decide(self._snap(False), self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_lost)
        self.assertFalse(any("置顶已不在" in n for n in v.notes))


class TestCallPlanning(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, cell, age_days=1.0):
        published = self.now - timedelta(days=age_days)
        return rows.Row(record_id="rec1", link_cell=cell,
                        publish_time_ms=int(published.timestamp() * 1000))

    def test_a_fresh_xhs_note_costs_one_call_by_default(self):
        """出厂默认 detail_within_days=0：小红书任何年龄都只有评论那一次。

        这是省掉 39% 月成本的那个默认值——「点赞数/收藏数」四列去掉之后，
        detail 买回来的东西没有一样落进表里。
        """
        self.assertEqual(self.settings.detail_within_days, 0)
        plan = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24),
                               self.settings, self.now)
        self.assertEqual([c.purpose for c in plan], ["comments"])
        self.assertEqual(plan[0].arguments["sort"], "default")

    def test_detail_can_be_switched_back_on(self):
        """仍然是受支持的配置：填个正数就回来了。"""
        self.settings.detail_within_days = 7
        fresh = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24),
                                self.settings, self.now)
        self.assertEqual([c.purpose for c in fresh], ["comments", "detail"])
        # 超出窗口的老帖仍然只有一次
        old = rows.plan_calls(
            self._row("https://www.xiaohongshu.com/explore/" + "a" * 24, age_days=30),
            self.settings, self.now)
        self.assertEqual([c.purpose for c in old], ["comments"])

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
        self.assertEqual(verdict.tags, {"风控中", "大爆"})

    def test_gone_verdict_without_history_stays_minimal(self):
        settings = Settings()
        self.assertEqual(analyze.gone_verdict(settings).tags, {"风控中"})


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

    def test_halfwidth_bracket_is_stripped_from_the_tail(self):
        """`(https://xhslink.com/abc123)` 在飞书表里很常见。
        `)` 留在 URL 里，短链直接 404。"""
        parsed = links.parse("(https://xhslink.com/abc123)")
        self.assertEqual(parsed.url, "https://xhslink.com/abc123")

    def test_paired_brackets_inside_the_url_are_kept(self):
        parsed = links.parse("https://xhslink.com/a_(b)")
        self.assertEqual(parsed.url, "https://xhslink.com/a_(b)")


class TestDomainSpoofing(unittest.TestCase):
    """COR-006：平台判定必须按 URL 的 **hostname** 做边界匹配，
    不能在整串文本里搜子串。搜子串会让我们拿着运营贴的链接，
    带着 API Key 去给一个陌生域名发付费请求。"""

    NOTE = "0123456789abcdef01234567"

    def test_lookalike_suffix_domain_is_rejected(self):
        parsed = links.parse(f"https://xiaohongshu.com.evil.example/explore/{self.NOTE}")
        self.assertFalse(parsed.usable)
        self.assertIsNone(parsed.platform)

    def test_lookalike_prefix_domain_is_rejected(self):
        parsed = links.parse(f"https://evil-xiaohongshu.com/explore/{self.NOTE}")
        self.assertFalse(parsed.usable)

    def test_userinfo_trick_is_rejected(self):
        """`https://xiaohongshu.com@evil.example/x` 的真实 host 是 evil.example。"""
        parsed = links.parse("https://xiaohongshu.com@evil.example/x")
        self.assertFalse(parsed.usable)

    def test_domain_in_query_string_does_not_count(self):
        parsed = links.parse("https://evil.example/?u=xiaohongshu.com")
        self.assertFalse(parsed.usable)

    def test_real_subdomain_still_works(self):
        parsed = links.parse(f"https://www.xiaohongshu.com/explore/{self.NOTE}")
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, self.NOTE)

    def test_bare_domain_without_scheme_still_works(self):
        """运营有时只粘域名开头的一段，没有 http://。"""
        parsed = links.parse(f"xiaohongshu.com/explore/{self.NOTE}")
        self.assertEqual(parsed.platform, "xhs")

    def test_bare_lookalike_domain_is_still_rejected(self):
        parsed = links.parse(f"xiaohongshu.com.evil.example/explore/{self.NOTE}")
        self.assertFalse(parsed.usable)


class TestNumericGuards(unittest.TestCase):
    """COR-012：上游给的数字必须先过一道类型/范围闸。"""

    def test_bool_is_not_a_count(self):
        """True 在 Python 里是 int：`comment_count: true` 会被当成「1 条评论」，
        于是一条爆文被判成「无水花」。"""
        snapshot = analyze.read_comment_page("xhs", {"items": [], "comment_count": True})
        self.assertIsNone(snapshot.comment_count)

    def test_negative_and_absurd_counts_are_rejected(self):
        for value in (-5, 10 ** 12):
            snapshot = analyze.read_comment_page("xhs", {"items": [], "comment_count": value})
            self.assertIsNone(snapshot.comment_count, value)

    def test_retry_after_is_clamped(self):
        self.assertEqual(protocol.clamp_retry_after(-1), 0.0)
        self.assertIsNone(protocol.clamp_retry_after(float("nan")))
        self.assertIsNone(protocol.clamp_retry_after(float("inf")))
        self.assertIsNone(protocol.clamp_retry_after(True))
        self.assertIsNone(protocol.clamp_retry_after("30"))
        self.assertEqual(protocol.clamp_retry_after(3600),
                         protocol.MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(protocol.clamp_retry_after(12.5), 12.5)

    def test_rate_limit_error_carries_a_safe_retry_after(self):
        result = protocol.parse_response(
            200, "application/json",
            json.dumps({"code": 1429, "message": "过于频繁", "retry_after_seconds": -3}))
        self.assertIsInstance(result, protocol.Err)
        self.assertEqual(result.retry_after_seconds, 0.0)


class TestRowTimestampSafety(unittest.TestCase):
    """COR-007：脏日期不能在选行阶段抛异常，那会让整张表一行都刷不了。"""

    def test_absurd_publish_time_does_not_explode(self):
        row = rows.Row(record_id="r", link_cell="x", publish_time_ms=10 ** 30)
        self.assertIsNone(row.age_hours(NOW))
        self.assertIsNone(row.age_days(NOW))
        self.assertTrue(row.is_due(Settings(), NOW))   # 算不出年龄按「该刷」处理

    def test_absurd_last_updated_does_not_explode(self):
        row = rows.Row(record_id="r", link_cell="x", last_updated_ms=-(10 ** 30))
        self.assertFalse(row.in_cooldown(Settings(), NOW))
        self.assertTrue(row.is_due(Settings(), NOW))

    def test_future_last_updated_does_not_lock_the_row_forever(self):
        """表里的「最近检查时间」被填成未来：这一行必须还能被刷，
        否则它会被那个未来时间锁死到那天为止，而且没人会发现。"""
        future = int((NOW + timedelta(days=30)).timestamp() * 1000)
        row = rows.Row(record_id="r", link_cell="x",
                       publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000),
                       last_updated_ms=future)
        self.assertFalse(row.in_cooldown(Settings(), NOW))
        self.assertTrue(row.is_due(Settings(), NOW))


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


class TestDisplayTimezone(unittest.TestCase):
    """日志里的时间要和飞书那一格逐字相同，否则「日志和表对不上」永远存在。"""

    def test_utc_moment_prints_as_beijing_time(self):
        display = Settings().display
        moment = datetime(2026, 8, 26, 0, 7, 14, tzinfo=UTC)
        self.assertEqual(display.stamp(moment), "2026-08-26 08:07:14 +08")
        self.assertEqual(display.clock(moment), "08:07:14")

    def test_offset_label_covers_negative_and_half_hours(self):
        from xhsearch.config import Display
        self.assertEqual(Display(utc_offset_hours=0).label(), "+00")
        self.assertEqual(Display(utc_offset_hours=-3).label(), "-03")
        self.assertEqual(Display(utc_offset_hours=5.5).label(), "+05:30")
        self.assertEqual(Display(utc_offset_hours=-3.5).label(), "-03:30")

    def test_it_never_touches_the_value_that_gets_written(self):
        """只影响打印。同一个时刻在不同偏移下显示不同，落表的毫秒数完全一样。"""
        from xhsearch.config import Display
        moment = datetime(2026, 8, 26, 0, 7, 14, tzinfo=UTC)
        rendered = {Display(utc_offset_hours=o).stamp(moment) for o in (0, 8, -5.5)}
        self.assertEqual(len(rendered), 3, "不同偏移应该渲染出不同的字符串")
        self.assertEqual(int(moment.timestamp() * 1000), 1787702834000)


if __name__ == "__main__":
    unittest.main()
