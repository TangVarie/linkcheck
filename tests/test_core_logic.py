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

    def test_sticky_tags_are_never_removed(self):
        """机器只加不减（2026-09-15）：行上的「风控中」不管谁打的，本轮没算出来
        也照单保留；热度档位照常换档。"""
        result = tags.merge(["风控中", "爆贴", "已复盘"], {"大爆"}, self.NS,
                            sticky=["风控中"])
        self.assertEqual(result.final, ["风控中", "已复盘", "大爆"])
        self.assertEqual(result.removed, ["爆贴"])
        self.assertEqual(result.added, ["大爆"])

    def test_sticky_tag_added_counts_as_a_change(self):
        result = tags.merge(["评估中"], {"评估中", "风控中"}, self.NS, sticky=["风控中"])
        self.assertEqual(result.added, ["风控中"])
        self.assertTrue(result.changed)

    def test_sticky_tag_already_present_is_idempotent(self):
        result = tags.merge(["风控中", "评估中"], {"评估中"}, self.NS, sticky=["风控中"])
        self.assertFalse(result.changed)
        self.assertEqual(result.final, ["风控中", "评估中"])


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


class TestBlueWords(unittest.TestCase):
    """蓝词：小红书评论正文里 `#词[搜索高亮]#` 的原始标记（TikHub 透传）。

    2026-09 实测能稳定看到：
        1. [1赞] MOMO: #快充小片是什么[搜索高亮]#
        3. [2赞] 点兵点将能咋办: 是我理解的这个 #快充小片[搜索高亮]# 吗，笑死
    """

    def _snap(self, *contents, platform="xhs"):
        items = [{"content": c, "like_count": 0, "is_pinned": False,
                  "is_author_comment": False, "ip_location": "", "author": {"name": "x"}}
                 for c in contents]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": len(items)})

    def test_extracts_highlighted_words_in_order(self):
        snap = self._snap("#快充小片是什么[搜索高亮]#",
                          "比起这个还是关注你老公吧",
                          "是我理解的这个 #快充小片[搜索高亮]# 吗，笑死")
        self.assertEqual(analyze.highlighted_words(snap), ["快充小片是什么", "快充小片"])

    def test_same_word_twice_is_listed_once(self):
        snap = self._snap("#GT33[搜索高亮]# 好用", "买了 #gt33[搜索高亮]#", "# GT33 [搜索高亮]#")
        self.assertEqual(analyze.highlighted_words(snap), ["GT33"])

    def test_topic_tags_are_not_blue_words(self):
        """话题也是蓝色链接，但那是发评论的人自己打的，不是平台判定的搜索词。"""
        snap = self._snap("#露营装备[话题]# 推荐", "普通评论 #没有标记")
        self.assertEqual(analyze.highlighted_words(snap), [])

    def test_two_words_in_one_comment(self):
        snap = self._snap("#西屋按摩椅[搜索高亮]# 和 #GT33白色款[搜索高亮]# 都不错")
        self.assertEqual(analyze.highlighted_words(snap), ["西屋按摩椅", "GT33白色款"])

    def test_new_words_ignore_case_and_spacing(self):
        """表里人工填的「gt33」和评论里高亮的「GT33」是同一个词，不该再提醒。"""
        self.assertEqual(analyze.new_blue_words(["GT33", "西屋按摩椅"], ["gt33", "西屋GT33"]),
                         ["西屋按摩椅"])
        self.assertEqual(analyze.new_blue_words(["西屋按摩椅"], []), ["西屋按摩椅"])
        self.assertEqual(analyze.new_blue_words([], ["gt33"]), [])

    def test_decide_carries_the_words_only_when_the_page_was_seen(self):
        settings = Settings()
        snap = self._snap("#快充小片[搜索高亮]#")
        verdict = analyze.decide(snap, settings, previous_comment_count=None, age_hours=30)
        self.assertEqual(verdict.blue_words, ["快充小片"])
        # 空壳轮：一条评论都没看到、评论数也没拿到 → 不下任何结论
        empty = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        verdict = analyze.decide(empty, settings, previous_comment_count=None, age_hours=30)
        self.assertEqual(verdict.blue_words, [])

    def test_digest_keeps_the_raw_marker_so_operators_can_see_it(self):
        """快照里保留原始标记：运营正是靠它一眼看出哪个词变蓝了。"""
        snap = self._snap("是我理解的这个 #快充小片[搜索高亮]# 吗")
        self.assertIn("#快充小片[搜索高亮]#", analyze.format_digest(snap, Settings().digest))

    def test_each_word_remembers_the_first_comment_it_appeared_in(self):
        snap = self._snap("#GT33[搜索高亮]# 好用", "又是 #gt33[搜索高亮]#",
                          "换行的\n#西屋\n按摩椅[搜索高亮]#")
        found = analyze.highlighted(snap)
        self.assertEqual([s.word for s in found], ["GT33", "西屋 按摩椅"])
        self.assertEqual(found[0].comment.content, "#GT33[搜索高亮]# 好用")
        self.assertEqual(found[1].text, "换行的 #西屋 按摩椅[搜索高亮]#")   # 空白收成一个空格

    def test_a_channel_that_strips_the_markers_is_not_read_as_no_blue_words(self):
        """SocialDataX 把标记抹平了：那一轮蓝词判定不做（不是「没有蓝词」）。"""
        items = [{"content": "#快充小片[搜索高亮]#", "is_pinned": False}]
        snap = analyze.read_comment_page("xhs", {
            "items": items, "comment_count": 1,
            "_capabilities": {"pinned": False, "author": False, "blue_words": False}})
        self.assertFalse(snap.blue_words_reported)
        verdict = analyze.decide(snap, Settings(), previous_comment_count=None, age_hours=30)
        self.assertFalse(verdict.blue_words_checked)
        self.assertEqual(verdict.blue_words, [])


class TestBlueWordScanPlan(unittest.TestCase):
    """什么时候扫蓝词（2026-10-22 口径）：还没抓到过就一直扫、抓到就停；
    到爆贴、到大爆各再扫一次；截图提醒只在爆贴（含）以上。"""

    def setUp(self):
        self.settings = Settings()

    def plan(self, *, words=("GT33",), log="", read=None, landed=None):
        return analyze.plan_blue_word_scan(self.settings, known_words=list(words),
                                           log_text=log, read_tier=read, landed_tier=landed)

    def test_nothing_captured_yet_always_scans(self):
        for landed in (None, "观察中", "评估中", "爆贴", "大爆"):
            scan = self.plan(words=(), landed=landed)
            self.assertIsNotNone(scan, landed)
            self.assertFalse(scan.rescan)

    def test_reminders_start_at_hot(self):
        self.assertFalse(self.plan(words=(), landed="评估中").remind)
        self.assertTrue(self.plan(words=(), landed="爆贴").remind)
        self.assertTrue(self.plan(words=(), landed="大爆").remind)

    def test_no_tier_is_written_as_untiered(self):
        self.assertEqual(self.plan(words=(), landed=None).stage, "未定档")

    def test_captured_and_still_below_hot_stops(self):
        self.assertIsNone(self.plan(read="评估中", landed="评估中"))
        self.assertIsNone(self.plan(log="2026-08-20 10:00 【观察中】GT33 ← …",
                                    read="观察中", landed="评估中"))

    def test_each_milestone_is_scanned_once(self):
        log = "2026-08-20 10:00 【评估中】GT33 ← …"
        self.assertTrue(self.plan(log=log, read="评估中", landed="爆贴").rescan)
        log += "\n2026-08-21 10:00 【爆贴】复扫：没有新蓝词，评论里也没看到蓝词"
        self.assertIsNone(self.plan(log=log, read="爆贴", landed="爆贴"))
        self.assertTrue(self.plan(log=log, read="爆贴", landed="大爆").rescan)
        log += "\n2026-08-22 10:00 【大爆】复扫：没有新蓝词，评论里也没看到蓝词"
        self.assertIsNone(self.plan(log=log, read="大爆", landed="大爆"))

    def test_the_ledger_wins_over_the_tier_in_the_table(self):
        """「流量状态」已经升到爆贴、账上还没有爆贴那一次（那一轮没扫成）→ 补扫。"""
        log = "2026-08-20 10:00 【评估中】GT33 ← …"
        self.assertTrue(self.plan(log=log, read="爆贴", landed="爆贴").rescan)

    def test_without_a_ledger_the_tier_in_the_table_is_the_ledger(self):
        """老行（「蓝词来源」空着）：开跑时已经是爆贴就当爆贴扫过了。"""
        self.assertIsNone(self.plan(read="爆贴", landed="爆贴"))
        self.assertTrue(self.plan(read="评估中", landed="爆贴").rescan)
        self.assertTrue(self.plan(read=None, landed="大爆").rescan)

    def test_only_line_heads_count_as_the_ledger(self):
        log = "2026-08-20 10:00 【评估中】GT33 ← 路人: 必须 【大爆】"
        self.assertEqual(analyze.scanned_tier_rank(log, self.settings),
                         self.settings.tags.rank("评估中"))
        self.assertIsNone(analyze.scanned_tier_rank("", self.settings))
        # 不认识的档位按最低算：最坏多扫一次，不会少扫
        self.assertEqual(analyze.scanned_tier_rank("2026-08-20 10:00 【未定档】GT33 ← …",
                                                   self.settings), -1)


class TestBlueWordSourceLine(unittest.TestCase):
    def _sighting(self, content, author="路人"):
        snap = analyze.read_comment_page("xhs", {"items": [
            {"content": content, "author": {"name": author}}], "comment_count": 1})
        return analyze.highlighted(snap)[0]

    def test_line_shape(self):
        line = analyze.blue_word_source_line(
            self._sighting("是我理解的这个 #快充小片[搜索高亮]# 吗"),
            when="2026-10-22 14:05", stage="评估中", fmt=Settings().digest)
        self.assertEqual(line, "2026-10-22 14:05 【评估中】快充小片 ← 路人: "
                               "是我理解的这个 #快充小片[搜索高亮]# 吗")

    def test_long_comment_is_cut_around_the_marker(self):
        content = "前" * 150 + "#西屋911GT[搜索高亮]#" + "后" * 150
        line = analyze.blue_word_source_line(
            self._sighting(content), when="2026-10-22 14:05", stage="爆贴",
            fmt=Settings().digest)
        self.assertIn("#西屋911GT[搜索高亮]#", line)
        self.assertIn("← 路人: …", line)
        self.assertTrue(line.endswith("…"))
        self.assertLess(len(line), 140)

    def test_author_name_follows_the_digest_switch(self):
        fmt = Settings().digest
        fmt.show_author_name = False
        line = analyze.blue_word_source_line(
            self._sighting("#GT33[搜索高亮]#", author="张三"), when="2026-10-22 14:05",
            stage="爆贴", fmt=fmt)
        self.assertNotIn("张三", line)


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

    def test_a_small_count_with_an_empty_page_means_nobody_can_see_them(self):
        """2026-10-16 实例：评论接口自己说「共 1 条」，却一条都不给；点开链接
        也是空的、没有「有评论被折叠」的提示。表里却一直挂着上一轮的「有负面」
        ——因为这一页被当成「没看到」，负面状态保持原样。

        评论接口自己报的数、一页就装得下，却一条都不给：说的就是那几条别人
        看不到（被删、被折叠、审核中、只有发评论的人自己看得到）。按「没有
        可见评论」判：负面清掉、我们种的评论算「没有显示」、置顶算掉了。
        """
        snap = analyze.read_comment_page(
            "xhs", {"items": [], "comment_count": 1, "top_level_comment_count": 1,
                    "_empty_page": True})
        self.assertTrue(analyze.comments_hidden(snap))
        self.assertTrue(analyze.saw_comment_page(snap))
        self.assertEqual(snap.comment_count, 0,
                         "实时评论数跟小红书界面一致：别人一条都看不到，就是 0")
        self.assertEqual(snap.hidden_comment_count, 1)

        verdict = self.decide(snap, seed_keywords=["领券"], negative_keywords=["过敏"])
        self.assertTrue(verdict.negative_checked)
        self.assertEqual(analyze.negative_status_value(verdict, self.settings),
                         self.settings.negative_status.clean)
        self.assertTrue(verdict.seed_checked)
        self.assertEqual(analyze.comment_status_value(verdict, self.settings),
                         self.settings.comment_status.not_displayed)
        self.assertTrue(verdict.pin_checked)
        notes = "；".join(verdict.notes)
        self.assertIn("接口报评论数 1，但评论区一条都看不到", notes)
        self.assertIn("实时评论数按 0 记", notes)
        self.assertIn("评论区一条可见的评论都没有 → 评论没有显示", notes)
        self.assertNotIn("本轮未取到评论页内容", notes)
        self.assertIn("接口报评论数 1，但评论区一条都看不到",
                      analyze.format_digest(snap, self.settings.digest),
                      "评论区快照不能写「暂无评论」——那和真的 0 条分不开")

    def test_the_top_level_count_decides_whether_it_fits_on_one_page(self):
        """总数含楼中楼：12 条里只有 3 条一级评论，一页照样装得下。"""
        snap = analyze.read_comment_page(
            "xhs", {"items": [], "comment_count": 12, "top_level_comment_count": 3,
                    "_empty_page": True})
        self.assertTrue(analyze.comments_hidden(snap))
        self.assertEqual((snap.comment_count, snap.hidden_comment_count), (0, 12))

    def test_zero_top_level_comments_also_fit_on_one_page(self):
        """总数 1、一级 0：剩下的只是看不到的楼中楼，或者一级计数先归零了。"""
        snap = analyze.read_comment_page(
            "xhs", {"items": [], "comment_count": 1, "top_level_comment_count": 0,
                    "_empty_page": True})
        self.assertTrue(analyze.comments_hidden(snap))

    def test_an_empty_list_the_channel_did_not_vouch_for_is_not_hidden(self):
        """缺字段、类型不对、条目全解析不了，归一化之后也是空列表——
        那是没拿到，不能拿来清掉一条真负面。"""
        snap = analyze.read_comment_page(
            "xhs", {"items": [], "comment_count": 1, "top_level_comment_count": 1})
        self.assertFalse(analyze.comments_hidden(snap))
        self.assertFalse(analyze.saw_comment_page(snap))
        self.assertEqual(snap.comment_count, 1)

    def test_douyin_is_left_alone(self):
        """只在小红书上实测过；抖音一页 20 条、计数口径没对过，不推断。"""
        snap = analyze.read_comment_page(
            "douyin", {"items": [], "comment_count": 1, "_empty_page": True})
        self.assertFalse(analyze.comments_hidden(snap))
        self.assertEqual(snap.comment_count, 1)

    def test_an_empty_page_on_a_post_that_needs_paging_is_still_not_a_verdict(self):
        """评论多到要翻页时，空页更可能是上游这一页没给——照旧不下结论。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 11})
        self.assertFalse(analyze.comments_hidden(snap))
        self.assertEqual(snap.comment_count, 11, "判不了的时候评论数照原样记")
        self.assertFalse(analyze.saw_comment_page(snap))
        verdict = self.decide(snap, negative_keywords=["过敏"])
        self.assertFalse(verdict.negative_checked)
        self.assertIn("本轮未取到评论页内容，负面词判定保持原样", verdict.notes)

    def test_a_count_backfilled_from_detail_is_never_read_as_hidden(self):
        """数是 detail 补的（两次请求），评论页空着只说明这次没拿到——
        哪怕数小到一页装得下，也不能当成「别人看不到」。"""
        snap = analyze.read_comment_page("xhs", {"items": []})
        analyze.merge_detail(snap, {"comment_count": 1, "like_count": 9})
        self.assertEqual(snap.comment_count, 1)
        self.assertEqual(snap.hidden_comment_count, 0)
        self.assertFalse(analyze.comments_hidden(snap))
        self.assertFalse(analyze.saw_comment_page(snap))

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

    def test_flop_holds_until_hot(self):
        """判过的无水花没到爆贴之前不回评估中（2026-10-22 口径）；到了爆贴照常升。
        以前排序上评估中高一档，下一轮就被改回去了。"""
        held = self.decide(49, age_hours=72, current_tags=["无水花"])
        self.assertEqual(held.tags, {"无水花"})
        self.assertTrue(any("不回「评估中」" in n for n in held.notes))
        up = self.decide(50, age_hours=72, current_tags=["无水花"])
        self.assertEqual(up.tags, {"爆贴"})

    def test_evaluating_does_not_drop_to_flop_without_the_growth_check(self):
        """评估中只有增长判据能把它降成无水花——评论数掉回 20 以下的这一轮
        只是记基线，不当场判。"""
        down = self.decide(3, age_hours=72, current_tags=["评估中"])
        self.assertIn("评估中", down.tags)
        self.assertNotIn("无水花", down.tags)
        self.assertTrue(down.eval_baseline_reset)

    def test_censor_flag_from_upstream_flags_risk(self):
        """风控中的硬证据之一：上游明确返回了审核/受限标记。"""
        snap = self._snap(30)
        snap.censored = True
        v = analyze.decide(snap, self.settings, previous_comment_count=None, age_hours=10)
        self.assertIn("风控中", v.tags)
        self.assertTrue(any("审核中/受限" in n for n in v.notes))

    def test_censor_reason_is_spelled_out_in_the_note(self):
        """上游给了具体原因（仅作者可见那种）时，诊断信息说具体的那句，
        而不是通用的「审核中/受限」——运营要知道去平台上看什么。"""
        snap = self._snap(30)
        snap.censored = True
        snap.censor_reason = "详情接口不给笔记本体，只回「Note is not available」——笔记对外不可见"
        v = analyze.decide(snap, self.settings, previous_comment_count=None, age_hours=10)
        self.assertIn("风控中", v.tags)
        self.assertTrue(any("Note is not available" in n and "风控中" in n for n in v.notes))
        self.assertFalse(any("审核中/受限" in n for n in v.notes))

    def test_throttled_is_not_reasserted_on_recovery(self):
        """decide 只产出**本轮**成立的标签：数字回升了就不再算出疑似限流/风控中。
        行上已有的这两个留不留，由 tags.merge 的 sticky 决定（2026-09-15 起
        一律保留，人来摘），这里只管本轮有没有新证据。"""
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

    def test_both_gates_include_their_boundary(self):
        """40 → 60 正好涨 50%、正好 +20：算起量。

        含不含边界必须和掉量那边**一样松**——100 → 50 正好腰斩是算限流的，
        一个口径的两个方向边界松紧不同，散文里怎么写都会有一边是错的。
        文档一律写 `≥`，不写「超过」。
        """
        th = self.settings.thresholds
        self.assertTrue(th.surged(60, 40))                      # 比例与增量都正好卡线
        self.assertTrue(50 <= 100 * (1 - th.risk_drop_ratio))   # 掉量那边同样含边界
        self.assertFalse(th.surged(59, 40))                     # 差一点比例
        self.assertFalse(th.surged(29, 10))                     # 比例够，增量差一条

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


class TestPinBlindChannel(unittest.TestCase):
    """通道不报置顶的那种轮次：**一列都不碰**，绝不写「置顶掉了」。

    真实事故：TikHub 余额见底 → runner 把它判死 → 整轮小红书全部降到备胎，
    而备胎的响应里没有置顶这个维度。评论照常抓到、诊断信息一个字不报，
    「置顶状态」却被整表刷成「置顶掉了」——运营对着一屋子假告警去补置顶，
    而置顶一直好好的。平台能看见置顶 ≠ 这一轮的通道会报置顶。
    """

    def setUp(self):
        self.settings = Settings()
        self.ps = self.settings.pin_status

    def _blind_page(self, count=12):
        """备胎通道的形状：评论齐全，就是没有 is_pinned 这个键。"""
        return analyze.read_comment_page("xhs", {
            "items": [
                {"content": "戳主页领券", "like_count": 3, "ip_location": "上海",
                 "author": {"name": "官号"}},
                {"content": "求链接", "like_count": 1, "ip_location": "浙江",
                 "author": {"name": "路人"}},
            ],
            "comment_count": count,
        })

    def test_blind_channel_is_not_qualified_to_judge(self):
        snap = self._blind_page()
        self.assertFalse(snap.pin_reported)
        self.assertFalse(snap.supports_pinned)
        self.assertIs(analyze.decide_pin(snap), analyze.Pin.UNSUPPORTED)

    def test_blind_channel_never_writes_pin_lost(self):
        """这就是那个假告警的正解：保持原样，不是写「掉了」。"""
        v = analyze.decide(self._blind_page(), self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_ok)
        self.assertFalse(v.pin_checked)
        self.assertIsNone(
            analyze.pin_status_value(v, self.ps.pinned_ok, self.settings))

    def test_blind_channel_never_writes_never_pinned_either(self):
        """没历史的行同样不能写「无置顶」——那也是一个它没资格下的结论。"""
        v = analyze.decide(self._blind_page(), self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status="")
        self.assertIsNone(analyze.pin_status_value(v, "", self.settings))

    def test_blind_round_says_so_in_the_notes(self):
        """不碰那一列的结果和「什么都没发生」长得一样——必须在诊断信息里出声，
        否则运营看到的是一个再也不更新的旧状态，还以为机器在正常巡查。"""
        v = analyze.decide(self._blind_page(), self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_ok)
        self.assertTrue(any("没查过，不是没置顶" in n for n in v.notes))
        # 而且绝不能同时再报一句「置顶已不在」——那正是要消灭的假告警。
        self.assertFalse(any("置顶已不在" in n for n in v.notes))

    def test_reporting_channel_still_judges_normally(self):
        """会报置顶的通道一行行为都不变：False 也是结论，照样判「掉了」。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [{"content": "路过", "is_pinned": False,
                       "is_author_comment": False, "like_count": 0,
                       "ip_location": "", "author": {"name": "路人"}}],
            "comment_count": 12,
        })
        self.assertTrue(snap.pin_reported)
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_ok)
        self.assertEqual(
            analyze.pin_status_value(v, self.ps.pinned_ok, self.settings),
            self.ps.pinned_lost)

    def test_one_marked_item_is_enough_to_prove_the_channel_reports(self):
        """能力判据是「整页有没有一条写过这个键」，不是「每条都写了」——
        只在置顶那条上写标记的通道照样算会报置顶。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [
                {"content": "路过", "like_count": 0, "author": {"name": "路人"}},
                {"content": "戳主页领券", "is_pinned": True, "like_count": 9,
                 "author": {"name": "官号"}},
            ],
            "comment_count": 12,
        })
        self.assertTrue(snap.pin_reported)
        self.assertIsNotNone(snap.pinned)

    def test_zero_comments_is_still_a_conclusion(self):
        """一条评论都没有的笔记不可能有置顶，哪家通道都得出同一个结论——
        这种轮次照常判，不要被能力闸误伤。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertTrue(snap.supports_pinned)
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, current_pin_status=self.ps.pinned_ok)
        self.assertEqual(
            analyze.pin_status_value(v, self.ps.pinned_ok, self.settings),
            self.ps.pinned_lost)

    def test_blind_channel_does_not_disturb_the_other_columns(self):
        """只掐置顶这一件事。评论状态/负面状态匹配的是评论正文，
        和置顶字段无关，不能被这道闸连坐。"""
        snap = self._blind_page()
        v = analyze.decide(snap, self.settings, previous_comment_count=None,
                           age_hours=10, seed_keywords=["戳主页"],
                           negative_keywords=["踩雷"])
        self.assertTrue(v.seed_checked)
        self.assertIsNotNone(v.seed_hit)
        self.assertTrue(v.negative_checked)
        self.assertEqual(
            analyze.comment_status_value(v, self.settings),
            self.settings.comment_status.displayed)


class TestAuthorBlindChannel(unittest.TestCase):
    """通道不报作者标记的那种轮次：负面**照常判**，但要说清自家回复没被排除。

    和置顶同源（同一个 show_tags_v2、同一条降级路径），后果却落在另一列上、
    方向也相反：置顶是无中生有报一个「掉了」，这里是把品牌号自己回的
    「温和配方，不会过敏」算成负面，一条干净的帖子被写成「有负面」。

    所以处置**故意和置顶不一样**：置顶判不了就整列不碰，负面判不了却不能
    不判——漏掉真负面比误报一条糟得多。照常出结论 + 诊断信息里点名。
    """

    def setUp(self):
        self.settings = Settings()
        self.ns = self.settings.negative_status

    def _page(self, items, count=12):
        return analyze.read_comment_page("xhs", {"items": items, "comment_count": count})

    def _blind_page(self):
        """备胎形状：评论齐全，就是没有 is_author_comment 这个键。
        自家回复和路人的负面话混在一页里——正是会出事的那种局面。"""
        return self._page([
            {"content": "温和配方，不会过敏的哦", "like_count": 2,
             "ip_location": "上海", "author": {"name": "官号"}},
            {"content": "路过看看", "like_count": 1,
             "ip_location": "浙江", "author": {"name": "路人"}},
        ])

    def _reporting_page(self):
        """会报作者标记的通道：同样两条评论，自家那条带着标记。"""
        return self._page([
            {"content": "温和配方，不会过敏的哦", "is_pinned": False,
             "is_author_comment": True, "like_count": 2,
             "ip_location": "上海", "author": {"name": "官号"}},
            {"content": "路过看看", "is_pinned": False,
             "is_author_comment": False, "like_count": 1,
             "ip_location": "浙江", "author": {"name": "路人"}},
        ])

    def _decide(self, snap):
        return analyze.decide(snap, self.settings, previous_comment_count=None,
                              age_hours=10, negative_keywords=["过敏"])

    def test_blind_channel_is_detected(self):
        self.assertFalse(self._blind_page().author_reported)

    def test_negative_verdict_is_still_produced(self):
        """**和置顶的处置不同**：不能因为排除失效就不判负面——
        漏掉真的负面比误报一条糟得多。"""
        v = self._decide(self._blind_page())
        self.assertTrue(v.negative_checked)
        self.assertTrue(v.negative_hits)
        self.assertEqual(analyze.negative_status_value(v, self.settings), self.ns.found)

    def test_blind_round_says_own_replies_were_not_excluded(self):
        """不说的话，运营看到的是一条和真负面一模一样的「有负面」，
        照着它去走公关流程，而命中的是自己昨天回的那一句。"""
        v = self._decide(self._blind_page())
        self.assertTrue(any("自家账号的回复没有被排除" in n for n in v.notes))

    def test_no_warning_when_nothing_hit(self):
        """没命中就不存在「排除没生效」这回事。每轮都挂一句只会把真正
        要看的那几条淹掉。"""
        v = analyze.decide(self._blind_page(), self.settings, previous_comment_count=None,
                           age_hours=10, negative_keywords=["踩雷"])
        self.assertTrue(v.negative_checked)
        self.assertFalse(v.negative_hits)
        self.assertFalse(any("自家账号的回复没有被排除" in n for n in v.notes))

    def test_reporting_channel_still_excludes_the_author(self):
        """会报作者标记的通道行为一行不变：自家那条照旧被排除，
        这一页就只剩「路过看看」，一条负面都不该命中。"""
        v = self._decide(self._reporting_page())
        self.assertTrue(v.negative_checked)
        self.assertFalse(v.negative_hits)
        self.assertEqual(analyze.negative_status_value(v, self.settings), self.ns.clean)
        self.assertFalse(any("自家账号的回复没有被排除" in n for n in v.notes))

    def test_untrusted_author_flags_never_exclude_anything(self):
        """**不可信的标记不许拿来排除任何东西**——排除就是信了它。

        Codex 在 PR #39 上逮到的那个洞：原来不管可不可信都 skip_author=True，
        于是一条**真负面**被上游错标成作者，就会被静默丢掉——命中数归零，
        负面告警和「自家回复没被排除」那句提醒**一个都不发**。
        坏在最要命的方向上，正好是「漏掉真负面比误报一条糟得多」的反面。
        """
        snap = analyze.read_comment_page("xhs", {
            "items": [
                # 路人写的真负面，却被这家不可信的通道标成了「作者」
                {"content": "用了过敏，踩雷", "like_count": 30,
                 "is_author_comment": True, "author": {"name": "路人"}},
            ],
            "comment_count": 12,
            "_capabilities": {"pinned": False, "author": False},
        })
        self.assertFalse(snap.author_reported)
        v = self._decide(snap)
        self.assertTrue(v.negative_hits, "真负面被不可信的作者标记吞掉了")
        self.assertEqual(analyze.negative_status_value(v, self.settings), self.ns.found)
        self.assertTrue(any("自家账号的回复没有被排除" in n for n in v.notes))

    def test_trusted_author_flags_still_exclude(self):
        """对照组：标记可信时照旧排除，这道改动不能把正常链路一起放开。"""
        snap = analyze.read_comment_page("xhs", {
            "items": [
                {"content": "温和配方，不会过敏的哦", "like_count": 2,
                 "is_author_comment": True, "author": {"name": "官号"}},
            ],
            "comment_count": 12,
            "_capabilities": {"pinned": True, "author": True},
        })
        self.assertTrue(snap.author_reported)
        v = self._decide(snap)
        self.assertFalse(v.negative_hits)
        self.assertEqual(analyze.negative_status_value(v, self.settings), self.ns.clean)

    def test_one_marked_item_is_enough_to_prove_the_channel_reports(self):
        """能力判据是「整页有没有一条写过这个键」，不是「每条都写了」。"""
        snap = self._page([
            {"content": "路过看看", "like_count": 0, "author": {"name": "路人"}},
            {"content": "温和配方，不会过敏的哦", "is_author_comment": True,
             "like_count": 2, "author": {"name": "官号"}},
        ])
        self.assertTrue(snap.author_reported)

    def test_douyin_does_not_get_the_warning(self):
        """抖音两家通道都恒定写这个键（值恒为 False），所以标志为真、不报。
        抖音看不见作者是**平台**边界，docs/表结构.md 已经写明「这个排除等于
        没开」——每一行再报一次只是固定噪音，不是新信息。"""
        snap = analyze.read_comment_page("douyin", {"items": [
            {"content": "温和配方，不会过敏的哦", "is_pinned": False,
             "is_author_comment": False, "like_count": 2,
             "ip_location": "上海", "author": {"name": "官号"}},
        ], "comment_count": 12})
        self.assertTrue(snap.author_reported)
        v = self._decide(snap)
        self.assertTrue(v.negative_hits)          # 排除没生效，照样命中
        self.assertFalse(any("自家账号的回复没有被排除" in n for n in v.notes))

    def test_zero_comments_needs_no_warning(self):
        """一条评论都没有，就不存在「自家回复被算成负面」这件事。"""
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertTrue(snap.author_reported)

    def test_the_two_capabilities_are_independent(self):
        """置顶和作者是两个维度，各认各的键——一家只报其中一个完全可能。"""
        snap = self._page([
            {"content": "戳主页领券", "is_pinned": True, "like_count": 9,
             "author": {"name": "官号"}},
        ])
        self.assertTrue(snap.pin_reported)
        self.assertFalse(snap.author_reported)


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

    def test_unknown_publish_time_is_not_patrolled(self):
        """发布时间空着、链接里也解不出：不巡查（Ziao 2026-10-16 拍板）。"""
        self.assertFalse(rows.Row(record_id="r", link_cell="x").is_due(self.settings, self.now))


class TestUnknownAgeIsNotPatrolled(unittest.TestCase):
    """帖龄算不出来的行（发布时间空着、链接里也解不出 ID 时间）不自动巡查。

    这个口径改过两次：最早是「每一轮都重刷」（is_due 在看「最近检查时间」
    之前就 return True，一条小红书一天 ¥20）；后来兜底成最快一档——有了上界，
    但这种行永远跨不过归档线，按 8 小时一直刷、没有尽头。现在干脆不查，
    补上发布时间才开始（Ziao 2026-10-16 拍板）。面板项目卡会数出来。
    """

    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _blind(self, checked_hours_ago=None):
        updated = (None if checked_hours_ago is None else
                   int((self.now - timedelta(hours=checked_hours_ago)).timestamp() * 1000))
        return rows.Row(record_id="r", link_cell="http://xhslink.com/a/AbC",
                        last_updated_ms=updated)

    def test_never_checked_is_not_due(self):
        self.assertFalse(self._blind().is_due(self.settings, self.now))

    def test_long_unchecked_is_not_due(self):
        self.assertFalse(self._blind(9999).is_due(self.settings, self.now))

    def test_it_has_no_interval_so_it_never_rides_along(self):
        """搭车也看 refresh_interval_hours：不巡查的行不跟着同篇写数据。"""
        self.assertIsNone(self._blind(9).refresh_interval_hours(self.settings, self.now))

    def test_filling_the_publish_time_starts_it(self):
        row = self._blind()
        row.publish_time_ms = int((self.now - timedelta(days=1)).timestamp() * 1000)
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_a_full_link_still_gets_its_age_from_the_id(self):
        """完整链接不受影响：ID 里编着时刻，按 ID 推的保守帖龄照常巡查。"""
        created = self.now - timedelta(days=1)
        row = rows.Row(record_id="r", link_cell="https://www.xiaohongshu.com/explore/"
                       + f"{int(created.timestamp()):08x}" + "0" * 16)
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_never_checked_with_a_publish_time_is_due_at_once(self):
        """从没刷过、帖龄知道的行照样立刻刷。"""
        row = rows.Row(record_id="r", link_cell="x",
                       publish_time_ms=int((self.now - timedelta(days=1)).timestamp() * 1000))
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_no_tiers_at_all_means_no_automatic_refresh(self):
        """一档都没配 = 没有任何刷新节奏可言，不能退化成「每轮都刷」。"""
        self.settings.refresh.tiers = []
        self.assertIsNone(self.settings.refresh.fastest_interval_hours())
        row = rows.Row(record_id="r", link_cell="x",
                       publish_time_ms=int((self.now - timedelta(days=1)).timestamp() * 1000))
        self.assertFalse(row.is_due(self.settings, self.now))


class TestTierIsPinnedAtTheLastCheck(unittest.TestCase):
    """档位按**上次检查那一刻**的帖龄定，不是按此刻的帖龄定。

    按此刻算的话，中午显示「下次 14:00」的行，下午一跨过 2 天线就自己变成
    「次日 06:00」——14:00 什么也没发生，运营完全看不出为什么。
    钉死这条，「下次检查时间」那一列才是一个定值、才能当成承诺看。
    """

    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, age_days_now, checked_hours_ago):
        published = self.now - timedelta(days=age_days_now)
        updated = self.now - timedelta(hours=checked_hours_ago)
        return rows.Row(record_id="r", link_cell="x",
                        publish_time_ms=int(published.timestamp() * 1000),
                        last_updated_ms=int(updated.timestamp() * 1000))

    def test_crossing_the_two_day_line_does_not_postpone_the_next_check(self):
        # 上次检查时才 1.6 天（8 小时档），现在已经 2.6 天。
        # 按此刻定档会变成 24 小时档、这一行还要再等 15 小时；
        # 按上次检查定档，9 小时就该刷了——和表里那一格写的一致。
        row = self._row(age_days_now=2.6, checked_hours_ago=9)
        self.assertEqual(row.refresh_interval_hours(self.settings, self.now), 8)
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_the_next_check_time_is_exactly_last_check_plus_interval(self):
        """表里那一格算的就是这个时刻：早一秒不刷，到点就刷。"""
        for age_days_now in (0.5, 2.6, 6.9, 12.0):
            with self.subTest(age_days_now):
                row = self._row(age_days_now, checked_hours_ago=0)
                interval = row.refresh_interval_hours(self.settings, self.now)
                updated = self.now
                self.assertFalse(row.is_due(
                    self.settings, updated + timedelta(hours=interval, seconds=-1)))
                self.assertTrue(row.is_due(
                    self.settings, updated + timedelta(hours=interval)))

    def test_archiving_still_follows_the_age_right_now(self):
        """归档看当下：上次检查发生在归档线之前，也不该再多刷一轮。"""
        row = self._row(age_days_now=31.5, checked_hours_ago=9999)
        self.assertIsNone(row.refresh_interval_hours(self.settings, self.now))
        self.assertFalse(row.is_due(self.settings, self.now))


class TestWholeDayTiers(unittest.TestCase):
    """档位按完整天数判，和飞书公式里的 DATEDIF(…, "D") 同口径。

    以前这里比的是浮点天数：「发布 2.5 天」在代码里已经进了 24 小时档，
    而表里那列公式（DATEDIF 得 2）还显示 8 小时档——运营看到的下次检查
    时间和机器真正的判断差一整天，而且没有任何地方能看出这个差别。
    """

    def setUp(self):
        self.refresh = Settings().refresh

    def test_the_boundaries_land_on_whole_days(self):
        for age, expected in ((0.0, 8), (2.0, 8), (2.9, 8), (3.0, 24),
                              (7.9, 24), (8.0, 72), (30.9, 72)):
            with self.subTest(age=age):
                self.assertEqual(self.refresh.interval_hours_for_age(age), expected)

    def test_archiving_also_waits_for_the_whole_day(self):
        self.assertEqual(self.refresh.interval_hours_for_age(30.99), 72)
        self.assertIsNone(self.refresh.interval_hours_for_age(31.0))


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
        self.assertFalse(row.is_due(Settings(), NOW))  # 算不出帖龄 = 不巡查，面板会数出来

    def test_absurd_last_updated_does_not_explode(self):
        row = rows.Row(record_id="r", link_cell="x", last_updated_ms=-(10 ** 30),
                       publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))
        self.assertFalse(row.in_cooldown(Settings(), NOW))
        self.assertTrue(row.is_due(Settings(), NOW))   # 脏的「最近检查时间」不能把这一行锁死

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

    def _short_link_row(self):
        now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        return rows.Row(record_id="r", link_cell="https://v.douyin.com/iRxYzAb/",
                        publish_time_ms=int((now - timedelta(days=1)).timestamp() * 1000)), now

    def test_douyin_short_link_is_priced_at_tikhub_optimistically(self):
        """抖音短链开跑时会先免费展开成 aweme_id、走 TikHub。乐观口径
        （报给人看的「预计花费」）就按 TikHub 算——按 SDX 报是 14 倍的虚高。"""
        row, now = self._short_link_row()
        yuan = rows.estimate_yuan([row], Settings(), now,
                                  keys={"tikhub": "t", "socialdatax": "s"})
        self.assertAlmostEqual(yuan, 2 * 0.001 * 7.2)   # 评论 + detail 各 $0.001

    def test_douyin_short_link_reserves_at_socialdatax_in_worst_case(self):
        """预算闸门预留时展开还没发生：展不开就退回 SDX，悲观口径必须把它算上。"""
        row, now = self._short_link_row()
        yuan = rows.estimate_yuan([row], Settings(), now,
                                  keys={"tikhub": "t", "socialdatax": "s"}, worst_case=True)
        self.assertAlmostEqual(yuan, 0.20)   # 评论 + detail 各 ¥0.10

    def test_short_link_is_priced_at_socialdatax_when_tikhub_is_dead(self):
        """TikHub 本轮已判死：展开不会发生（换成 ID 也没通道接），按 SDX 算。"""
        row, now = self._short_link_row()
        yuan = rows.estimate_yuan([row], Settings(), now,
                                  keys={"tikhub": "t", "socialdatax": "s"},
                                  disabled={"tikhub"})
        self.assertAlmostEqual(yuan, 0.20)

    def test_tikhub_only_short_link_is_priced_at_tikhub(self):
        """只配 TikHub 的部署：这行能不能跑取决于短链展不展得开。
        估算按「能」算——展不开时实际花 0，估高一点点比估 0 然后花了钱强。"""
        row, now = self._short_link_row()
        yuan = rows.estimate_yuan([row], Settings(), now, keys={"tikhub": "t"})
        self.assertAlmostEqual(yuan, 2 * 0.001 * 7.2)

    def test_row_no_channel_can_handle_costs_nothing(self):
        """一家能接的通道都没有（key 都没配）：不产生费用。"""
        row, now = self._short_link_row()
        yuan = rows.estimate_yuan([row], Settings(), now, keys={})
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


class TestEvalGrowth(unittest.TestCase):
    """「评估中」的增长判据（2026-10-22 运营定的口径）：

    发布满 48 小时、评论数在 [评估中门槛, 爆贴门槛) 的帖记一个基线；满 24 小时
    再看，涨了不到 10 条 → 无水花，涨够了 → 继续评估中、基线重记。
    """

    NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.settings = Settings()

    def decide(self, count, *, age=72.0, tags=("评估中",), base=None, hours_ago=24.0,
               settings=None, **kw):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": count})
        return analyze.decide(
            snap, settings or self.settings, previous_comment_count=None, age_hours=age,
            current_tags=list(tags), eval_baseline_count=base,
            eval_baseline_at=None if base is None else self.NOW - timedelta(hours=hours_ago),
            now=self.NOW, **kw)

    def heat(self, verdict):
        return {t for t in verdict.tags if self.settings.tags.rank(t) >= 0}

    def test_first_round_after_48h_records_a_baseline(self):
        v = self.decide(30, age=50)
        self.assertTrue(v.eval_baseline_reset)
        self.assertEqual(self.heat(v), {"评估中"})
        self.assertTrue(any("记下评估基线" in n for n in v.notes))

    def test_before_48h_nothing_happens(self):
        v = self.decide(30, age=40)
        self.assertFalse(v.eval_baseline_reset)
        self.assertEqual(self.heat(v), {"评估中"})

    def test_gain_of_9_in_24h_is_a_flop(self):
        v = self.decide(39, base=30)
        self.assertEqual(self.heat(v), {"无水花"})
        self.assertFalse(v.eval_baseline_reset)
        self.assertTrue(any("只涨了 9 条" in n and "→ 无水花" in n for n in v.notes))

    def test_gain_of_exactly_10_keeps_evaluating_and_resets_the_baseline(self):
        v = self.decide(40, base=30)
        self.assertEqual(self.heat(v), {"评估中"})
        self.assertTrue(v.eval_baseline_reset)

    def test_inside_the_window_nothing_changes(self):
        v = self.decide(31, base=30, hours_ago=20)
        self.assertEqual(self.heat(v), {"评估中"})
        self.assertFalse(v.eval_baseline_reset)

    def test_a_longer_window_after_a_failed_round_is_judged_on_the_whole_gap(self):
        """中间一轮巡查失败：基线没动，窗口顺延到这一次（48 小时），按整段的增长判。"""
        v = self.decide(42, base=30, hours_ago=48)
        self.assertEqual(self.heat(v), {"评估中"})
        self.assertTrue(v.eval_baseline_reset)

    def test_falling_comments_count_as_no_growth(self):
        """评论数掉回 20 以下、靠棘轮挂着评估中的帖也在判断里：掉了就是没涨。"""
        v = self.decide(12, base=30)
        self.assertEqual(self.heat(v), {"无水花"})

    def test_hot_rows_are_not_judged(self):
        v = self.decide(45, tags=("爆贴",), base=30)
        self.assertEqual(self.heat(v), {"爆贴"})
        self.assertFalse(v.eval_baseline_reset)

    def test_reaching_hot_leaves_the_judgement(self):
        v = self.decide(50, base=45)
        self.assertEqual(self.heat(v), {"爆贴"})

    def test_missing_columns_skip_the_check_and_say_why(self):
        v = self.decide(31, base=30, eval_unready="表里没建「评估基线评论数」")
        self.assertEqual(self.heat(v), {"评估中"})
        self.assertFalse(v.eval_baseline_reset)
        self.assertTrue(any("表里没建「评估基线评论数」" in n and "跳过" in n for n in v.notes))

    def test_a_future_baseline_is_rewritten(self):
        v = self.decide(31, base=30, hours_ago=-5)
        self.assertTrue(v.eval_baseline_reset)

    def test_per_table_thresholds_are_used(self):
        """门槛按表：登记表里填了 评估中 10 / 爆贴 30 的表，15 条也在判断里，30 条已经是爆贴。"""
        from xhsearch import registry
        entry = registry.Entry(label="小表", thresholds={
            registry.COL_TIER_EVALUATING: 10, registry.COL_TIER_HOT: 30})
        table = registry.apply_overrides(self.settings, entry)
        self.assertEqual(self.heat(self.decide(15, base=10, settings=table)), {"无水花"})
        self.assertEqual(self.heat(self.decide(20, base=10, settings=table)), {"评估中"})
        self.assertEqual(self.heat(self.decide(30, base=10, settings=table)), {"爆贴"})
        # 全局口径下 15 条够不上评估中：发布 72 小时直接是无水花（旧规则），不走增长判据
        v = self.decide(15, tags=(), base=None)
        self.assertEqual(self.heat(v), {"无水花"})
        self.assertFalse(v.eval_baseline_reset)

    def test_the_three_knobs_are_configurable(self):
        self.settings.thresholds.eval_min_gain = 5
        self.assertEqual(self.heat(self.decide(35, base=30)), {"评估中"})
        self.settings.thresholds.eval_window_hours = 48
        self.assertFalse(self.decide(35, base=30).eval_baseline_reset)   # 窗口还没满
        self.settings.thresholds.eval_start_hours = 96
        self.assertFalse(self.decide(30, age=72).eval_baseline_reset)


class TestFlopHolds(unittest.TestCase):
    """无水花不能被改回评估中（2026-10-22）：格子里已有无水花、评论数还没到爆贴门槛，
    就保持无水花。人手改的也一样。爆贴 / 大爆照旧只升不降。"""

    def setUp(self):
        self.settings = Settings()

    def decide(self, count, tags, **kw):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": count})
        return analyze.decide(snap, self.settings, previous_comment_count=None,
                              age_hours=kw.pop("age", 96), current_tags=list(tags), **kw)

    def heat(self, verdict):
        return {t for t in verdict.tags if self.settings.tags.rank(t) >= 0}

    def test_grows_to_49_still_flop_then_50_is_hot(self):
        self.assertEqual(self.heat(self.decide(49, ["无水花"])), {"无水花"})
        self.assertEqual(self.heat(self.decide(50, ["无水花"])), {"爆贴"})

    def test_hand_set_flop_next_to_evaluating_wins(self):
        """运营在多选里把「无水花」加上、没去掉「评估中」：按无水花算，下一轮机器
        不会把它摘掉。"""
        v = self.decide(30, ["评估中", "无水花", "客户已确认"])
        self.assertEqual(self.heat(v), {"无水花"})
        merged = tags.merge(["评估中", "无水花", "客户已确认"], v.tags,
                            self.settings.tags.namespace(),
                            exclusive=(self.settings.tags.heat_tiers(),),
                            sticky=self.settings.tags.sticky())
        self.assertIn("无水花", merged.final)
        self.assertNotIn("评估中", merged.final)
        self.assertIn("客户已确认", merged.final)

    def test_flop_held_rows_skip_the_growth_check(self):
        v = self.decide(45, ["无水花"], eval_baseline_count=20,
                        eval_baseline_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                        now=datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertFalse(v.eval_baseline_reset)
        self.assertEqual(self.heat(v), {"无水花"})

    def test_hot_beats_a_stray_flop(self):
        """爆贴 / 大爆只升不降：格子里混着一个无水花也不退回去。"""
        self.assertEqual(self.heat(self.decide(30, ["爆贴", "无水花"])), {"爆贴"})

    def test_unknown_count_keeps_the_flop(self):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": None})
        v = analyze.decide(snap, self.settings, previous_comment_count=None, age_hours=96,
                           current_tags=["评估中", "无水花"])
        self.assertEqual(self.heat(v), {"无水花"})

    def test_dead_link_keeps_the_flop(self):
        v = analyze.gone_verdict(self.settings, current_tags=["评估中", "无水花"])
        self.assertEqual(self.heat(v), {"无水花"})


class TestEvalWindowSchedule(unittest.TestCase):
    """评估中的增长判据要每 24 小时看一次：发布 8 天以后分层是 72 小时，凑不出窗口。"""

    NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)

    def row(self, *, age_days=10.0, hours_since=25.0, tags=("评估中",), base=30):
        ms = lambda dt: int(dt.timestamp() * 1000)
        return rows.Row(
            record_id="r", link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
            publish_time_ms=ms(self.NOW - timedelta(days=age_days)),
            last_updated_ms=ms(self.NOW - timedelta(hours=hours_since)),
            current_tags=list(tags), previous_comment_count=35,
            eval_baseline_count=base,
            eval_baseline_ms=None if base is None else ms(self.NOW - timedelta(hours=hours_since)))

    def test_rows_in_the_judgement_are_checked_every_24h(self):
        settings = Settings()
        self.assertEqual(self.row().refresh_interval_hours(settings, self.NOW), 24)
        self.assertTrue(self.row().is_due(settings, self.NOW))

    def test_rows_without_a_baseline_keep_the_tier_schedule(self):
        settings = Settings()
        self.assertEqual(self.row(base=None).refresh_interval_hours(settings, self.NOW), 72)
        self.assertFalse(self.row(base=None).is_due(settings, self.NOW))

    def test_decided_rows_go_back_to_the_tier_schedule(self):
        settings = Settings()
        for tags_ in (("无水花",), ("爆贴",), ("评估中", "无水花")):
            self.assertEqual(self.row(tags=tags_).refresh_interval_hours(settings, self.NOW), 72)

    def test_archived_rows_stay_archived(self):
        self.assertIsNone(self.row(age_days=31).refresh_interval_hours(Settings(), self.NOW))


class TestBlueWordExcludes(unittest.TestCase):
    """蓝词排除词（竞品名）：中文按子串、英文按整词，都不分大小写，至少 2 个字。"""

    def test_chinese_terms_match_as_substrings(self):
        self.assertEqual(analyze.excluded_by("奥佳华7598vpro", ["奥佳华"]), "奥佳华")
        self.assertEqual(analyze.excluded_by("荣康按摩椅", ["荣康"]), "荣康")
        self.assertIsNone(analyze.excluded_by("西屋911GT", ["奥佳华"]))

    def test_english_terms_match_whole_words_ignoring_case(self):
        self.assertEqual(analyze.excluded_by("sis黑胶", ["sis"]), "sis")
        self.assertEqual(analyze.excluded_by("SiS 能量胶", ["sis"]), "sis")
        self.assertEqual(analyze.excluded_by("Maurten320", ["maurten"]), None,
                         "后面挨着数字就不是整词")
        self.assertEqual(analyze.excluded_by("maurten 320", ["MAURTEN"]), "MAURTEN")
        self.assertIsNone(analyze.excluded_by("analysis", ["sis"]))
        self.assertIsNone(analyze.excluded_by("careplus", ["are"]))
        self.assertIsNone(analyze.excluded_by("OliveYoung", ["live"]))

    def test_mixed_terms_match_as_substrings_ignoring_case(self):
        self.assertEqual(analyze.excluded_by("西屋911gt按摩椅", ["西屋911GT"]), "西屋911GT")

    def test_terms_shorter_than_two_characters_do_nothing(self):
        self.assertIsNone(analyze.excluded_by("S码", ["S"]))
        self.assertIsNone(analyze.excluded_by("黑胶", ["黑"]))

    def test_registry_reads_the_list_and_reports_short_terms(self):
        from xhsearch import registry
        entry = registry.Entry(label="SPX", blue_word_excludes=["康比特", "迈胜", "S", "maurten"])
        kept, problems = registry.read_blue_word_excludes(entry)
        self.assertEqual(kept, ["康比特", "迈胜", "maurten"])
        self.assertEqual(len(problems), 1)
        self.assertIn("S", problems[0])
        logged = []
        settings = registry.apply_overrides(Settings(), entry, log=logged.append)
        self.assertEqual(settings.blue_word_excludes, ("康比特", "迈胜", "maurten"))
        self.assertTrue(any("不到 2 个字" in line for line in logged))
        self.assertEqual(Settings().blue_word_excludes, (), "不能串到全局那一份上")

    def test_registry_splits_a_comma_separated_cell(self):
        from xhsearch import feishu
        self.assertEqual(feishu.read_keywords("康比特,迈胜，maurten、sis"),
                         ["康比特", "迈胜", "maurten", "sis"])

    def test_decide_drops_excluded_words_before_anything_else(self):
        settings = Settings()
        settings.blue_word_excludes = ("康比特",)
        items = [{"content": c} for c in ("#康比特[搜索高亮]# 也不错", "#SPX[搜索高亮]# 好用")]
        snap = analyze.read_comment_page("xhs", {"items": items, "comment_count": 2})
        verdict = analyze.decide(snap, settings, previous_comment_count=None, age_hours=30)
        self.assertEqual(verdict.blue_words, ["SPX"])
        self.assertEqual(verdict.blue_words_excluded, ["康比特"])


class TestDueSlack(unittest.TestCase):
    """到期判断的余量（DUE_SLACK_MINUTES）：sweep 一天只在固定几个时刻跑时，
    「最近检查时间」比 sweep 启动晚一两分钟，没有余量的话每一档都要多等一整轮。"""

    SWEEP = datetime(2026, 10, 23, 8, 7, tzinfo=timezone.utc)   # 北京 16:07 那轮

    def row(self, *, age_days, checked):
        ms = lambda dt: int(dt.timestamp() * 1000)
        return rows.Row(record_id="r", link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
                        publish_time_ms=ms(checked - timedelta(days=age_days)),
                        last_updated_ms=ms(checked))

    def next_sweep(self, row, settings):
        """一天三次 sweep（每 8 小时，每次都晚两分钟启动）里，第一次判它到期的是哪一次。"""
        for k in range(1, 13):
            slot = self.SWEEP + timedelta(hours=8 * k)
            if row.is_due(settings, slot):
                return (slot - self.SWEEP).total_seconds() / 3600
        return None

    def test_without_slack_every_tier_slips_a_whole_sweep(self):
        settings = Settings()
        checked = self.SWEEP + timedelta(seconds=30)      # 这一行在开跑 30 秒后刷完
        self.assertEqual(self.next_sweep(self.row(age_days=1, checked=checked), settings), 16)
        self.assertEqual(self.next_sweep(self.row(age_days=5, checked=checked), settings), 32)

    def test_with_slack_each_tier_lands_on_its_own_sweep(self):
        settings = Settings()
        settings.refresh.due_slack_minutes = 30
        checked = self.SWEEP + timedelta(minutes=3)       # 一轮跑了几分钟，最后几行也一样
        self.assertEqual(self.next_sweep(self.row(age_days=1, checked=checked), settings), 8)
        self.assertEqual(self.next_sweep(self.row(age_days=5, checked=checked), settings), 24)
        self.assertEqual(self.next_sweep(self.row(age_days=10, checked=checked), settings), 72)

    def test_slack_never_makes_a_row_due_twice_in_one_interval(self):
        """余量最多让到间隔的一半：配错成很大的数也不会变成每一轮都刷。"""
        settings = Settings()
        settings.refresh.due_slack_minutes = 600
        self.assertEqual(settings.refresh.slack_hours(8), 4)
        row = self.row(age_days=1, checked=self.SWEEP)
        self.assertFalse(row.is_due(settings, self.SWEEP + timedelta(hours=3, minutes=59)))
        self.assertTrue(row.is_due(settings, self.SWEEP + timedelta(hours=4)))

    def test_the_eval_window_uses_the_same_slack(self):
        """一行到期被刷了，增长判据也得认它满了 24 小时，不然要白等一整轮。"""
        now = self.SWEEP + timedelta(hours=24)
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 32})
        def decide(slack):
            settings = Settings()
            settings.refresh.due_slack_minutes = slack
            return analyze.decide(snap, settings, previous_comment_count=None, age_hours=80,
                                  current_tags=["评估中"], eval_baseline_count=30,
                                  eval_baseline_at=self.SWEEP + timedelta(minutes=2), now=now)
        self.assertNotIn("无水花", decide(0).tags)
        self.assertIn("无水花", decide(30).tags)
