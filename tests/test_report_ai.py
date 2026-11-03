"""DeepSeek 日报草稿：证据最小化、竞品可核验、缓存与失败降级。

全部离线。没有任何一条用例会真的打 api.deepseek.com——`_call` 的 opener
在每条用例里都被替换成假函数，Key 也只用假值。
"""

import hashlib
import io
import json
import threading
import unittest
import urllib.error
from types import SimpleNamespace

from xhsearch import daily_report as d
from xhsearch import report_ai
from xhsearch.config import Settings

START, END = d.window_for_date("2026-10-28")
TABLE = "tbl1"


def negative_row(title, comments=(("这个用了之后有点不舒服", "不舒服"),), *, post=None,
                 checked=START + 1000, negative=True, record=None):
    """report_rows 里的一行：标题 + 「负面评论快照」解析出来的 (正文, 命中词)。"""
    return SimpleNamespace(record_id=record or f"rec-{title}", post_key=post or f"xhs:{title}",
                           label=title, negative_found=negative,
                           negative_comments=list(comments), last_checked_ms=checked)


def overview_with(*titles, table=TABLE):
    """此刻仍有负面的帖子。不给标题就默认一篇，免得每条用例都要写。"""
    titles = titles or ("露营帐篷实测",)
    return SimpleNamespace(projects=[SimpleNamespace(
        table_id=table, report_rows=[negative_row(t) for t in titles])])


NO_NEGATIVES = SimpleNamespace(projects=[SimpleNamespace(table_id=TABLE, report_rows=[])])


def posts(*titles):
    return report_ai.negative_posts_for(overview_with(*titles), TABLE)


def scan(at=START + 1000, *, post="xhs:one", hits=None, table=TABLE, **kw):
    return {"type": "scan", "at": at, "table_id": table, "record_id": "rec-secret",
            "post_key": post, "negative_checked": True,
            "hits": hits if hits is not None else [
                {"id": "c1", "content": "这个用了之后有点不舒服", "keywords": ["不舒服"]}],
            **kw}


def report(**changes):
    base = {"table_id": TABLE, "label": "途鸽",
            "window": {"start_ms": START, "end_ms": END, "as_of_ms": END},
            # 默认就是「此刻」这一期：只有此刻，待办标题才和它同一时段。
            "display_mode": "snapshot",
            "current_snapshot": {"available": True, "window_is_current": True},
            "keywords": [{"word": "不舒服", "count": 1}]}
    base.update(changes)
    return base


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answer(payload, status=200):
    """按 OpenAI 兼容形状包一层，content 里是模型给的 JSON 字符串。"""
    body = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
    return FakeResponse(json.dumps(body, ensure_ascii=False).encode("utf-8"))


class TestEvidence(unittest.TestCase):
    def test_only_alerting_comments_reach_the_prompt(self):
        evidence = report_ai.evidence_for_prompt(
            [scan(), {"type": "status", "at": START + 2, "table_id": TABLE,
                      "record_id": "rec-secret", "state": "resolved"}],
            report())
        payload = json.dumps(evidence, ensure_ascii=False)
        self.assertNotIn("rec-secret", payload)
        self.assertNotIn("record_id", payload)
        self.assertIn("这个用了之后有点不舒服", payload)
        self.assertEqual(evidence["comments"][0]["keywords"], "不舒服")

    def test_window_table_and_malformed_events_are_ignored(self):
        events = [scan(START - 1), scan(END), scan(table="other"),
                  scan(hits=[{"id": "x"}, "junk", {"content": None}]), scan()]
        evidence = report_ai.evidence_for_prompt(events, report())
        self.assertEqual(len(evidence["comments"]), 1)

    def test_duplicate_comments_are_counted_once_and_ordered_deterministically(self):
        repeated = scan(hits=[{"id": "c1", "content": "重复"}])
        evidence = report_ai.evidence_for_prompt(
            [repeated, repeated, scan(START + 5000, post="xhs:two",
                                      hits=[{"id": "c2", "content": "更新"}])], report())
        self.assertEqual([item["content"] for item in evidence["comments"]], ["更新", "重复"])

    def test_missing_ids_fall_back_to_text_dedup(self):
        first = scan(hits=[{"content": "过敏 了"}])
        second = scan(START + 2000, hits=[{"content": "过敏\n了"}])
        evidence = report_ai.evidence_for_prompt([first, second], report())
        self.assertEqual(len(evidence["comments"]), 1)

    def test_limits_are_enforced_and_sampling_is_disclosed(self):
        hits = [{"id": f"c{i}", "content": "长" * 400} for i in range(200)]
        evidence = report_ai.evidence_for_prompt([scan(hits=hits)], report())
        self.assertLessEqual(len(evidence["comments"]), report_ai.MAX_COMMENTS)
        self.assertLessEqual(sum(len(item["content"]) for item in evidence["comments"]),
                             report_ai.MAX_EVIDENCE_CHARS + report_ai.MAX_COMMENT_CHARS)
        self.assertTrue(evidence["sampled"])
        self.assertTrue(all(len(item["content"]) <= report_ai.MAX_COMMENT_CHARS
                            for item in evidence["comments"]))

    def test_personal_identifiers_are_redacted_before_leaving(self):
        dirty = ("加我微信 abc123，手机 13800008888，邮箱 a@b.com，"
                 "看 https://x.cn/p/1 和 @某账号")
        evidence = report_ai.evidence_for_prompt(
            [scan(hits=[{"id": "c1", "content": dirty}])], report())
        text = evidence["comments"][0]["content"]
        for leaked in ("13800008888", "a@b.com", "https://x.cn", "@某账号"):
            self.assertNotIn(leaked, text)
        self.assertIn("[手机号]", text)
        self.assertIn("[邮箱]", text)

    def test_control_characters_cannot_break_the_json_payload(self):
        evidence = report_ai.evidence_for_prompt(
            [scan(hits=[{"id": "c1", "content": "a\x00b\x1bc"}])], report())
        self.assertNotIn("\x00", evidence["comments"][0]["content"])

    def test_prompt_carries_readable_dates_and_no_raw_identifiers(self):
        evidence = report_ai.evidence_for_prompt([scan()], report(), posts("露营装备清单"))
        messages = report_ai.prompt_messages(evidence)
        self.assertIn("json", messages[0]["content"].lower())
        user = messages[1]["content"]
        self.assertIn("露营装备清单", user)
        self.assertIn("10月27日 16:00", user)
        self.assertNotIn(str(END), user)
        self.assertNotIn("rec-", user)

    def test_prompt_talks_about_the_same_posts_as_the_report_text(self):
        """线上：正文写「0 篇看到负面评论 / 仍有负面的帖子共 7 篇」，AI 却写「本期异常
        样本仅1条……预警评论第一页为空」——读的人分不清它在说哪批、为什么没评论。"""
        system = report_ai.prompt_messages(
            report_ai.evidence_for_prompt([], report(), posts("露营装备清单")))[0]["content"]
        self.assertIn("最近一次检查时第一页评论里仍有负面词的帖子（含运营已处理的）", system)
        self.assertIn("这 N 篇仍有负面的帖子", system)
        self.assertIn("不要用“异常”“样本”", system)
        self.assertIn("不要写“评论为空”“无法归纳”", system)

    def test_the_current_window_sends_the_posts_and_the_report_counts_not_period_comments(self):
        snapshot = {"available": True, "window_is_current": True, "as_of_ms": START + 3_600_000,
                    "window_checked_posts": 50, "window_negative_posts": 0, "negative_posts": 2,
                    "post_states": {"pending": 0, "processing": 1, "resolved": 1}}
        evidence = report_ai.evidence_for_prompt(
            [scan(hits=[{"id": "x", "content": "这一期别的帖子里的评论"}])],
            report(current_snapshot=snapshot), posts("甲", "乙"))
        self.assertEqual(sorted(p["title"] for p in evidence["posts"]), ["乙", "甲"])
        self.assertNotIn("key", evidence["posts"][0], "帖子键是标识，不交给模型")
        self.assertEqual(evidence["comments"], [], "只讲那 N 篇，不混进别的帖子的评论")
        self.assertEqual(evidence["keywords"], [])
        self.assertEqual(evidence["counts"]["negative_now"], 2)
        self.assertEqual(evidence["counts"]["negative_in_period"], 0)
        self.assertNotIn("checked_in_period", evidence["counts"],
                         "每轮巡检都变的数不进提示词，不然缓存指纹跟着变、重复计费")
        user = report_ai.prompt_messages(evidence)[1]["content"]
        self.assertNotIn("17:00", user, "截至几点每分钟都在走，不进提示词")
        self.assertNotIn("这一期别的帖子里的评论", user)

    def test_negative_posts_come_from_the_selected_table_include_handled_and_dedupe(self):
        rows = [negative_row("甲", (("太贵了", "贵"),), post="xhs:a"),
                negative_row("甲", (("太贵了", "贵"), ("售后差", "售后")), post="xhs:a",
                             record="rec-dup"),
                negative_row("乙", (), post="xhs:b"),
                negative_row("没负面的", (), post="xhs:c", negative=False)]
        overview = SimpleNamespace(projects=[
            SimpleNamespace(table_id=TABLE, report_rows=rows),
            SimpleNamespace(table_id="tbl2", report_rows=[negative_row("别人的")])])
        found = report_ai.negative_posts_for(overview, TABLE)
        self.assertEqual([p["title"] for p in found], ["甲", "乙"])
        self.assertEqual([c["content"] for c in found[0]["comments"]], ["太贵了", "售后差"],
                         "同一篇登记两行，评论合并去重")
        self.assertEqual(found[1]["comments"], [], "没评论也照样送标题")

    def test_too_many_posts_are_cut_to_the_latest_and_disclosed(self):
        """21 篇只送 20 篇：要标「抽样」，提示词里也要告诉模型只给了其中几篇，
        不能让它把一部分当成全部 21 篇来写。"""
        rows = [negative_row(f"帖{i:02d}", checked=START + i) for i in range(21)]
        found = report_ai.negative_posts_for(SimpleNamespace(projects=[SimpleNamespace(
            table_id=TABLE, report_rows=rows)]), TABLE)
        snapshot = {"available": True, "window_is_current": True, "negative_posts": 21,
                    "window_negative_posts": 0, "post_states": {}}
        evidence = report_ai.evidence_for_prompt([], report(current_snapshot=snapshot), found)
        self.assertEqual(len(evidence["posts"]), report_ai.MAX_POSTS)
        self.assertNotIn("帖00", [p["title"] for p in evidence["posts"]], "截掉的是最久没查的")
        self.assertTrue(evidence["sampled"])
        self.assertEqual(evidence["counts"]["posts_supplied"], report_ai.MAX_POSTS)
        self.assertIn("posts_supplied", report_ai.prompt_messages(evidence)[0]["content"])

    def test_a_routine_recheck_does_not_reorder_the_prompt(self):
        """只是检查时间往前挪了一下，标题和评论都没变：提示词必须一模一样，
        不然缓存指纹一变，同一份证据 15 分钟内又要计费一次。"""
        def prompt(first_checked, second_checked):
            overview = SimpleNamespace(projects=[SimpleNamespace(table_id=TABLE, report_rows=[
                negative_row("甲", checked=first_checked), negative_row("乙", checked=second_checked)])])
            evidence = report_ai.evidence_for_prompt(
                [], report(), report_ai.negative_posts_for(overview, TABLE))
            return report_ai.prompt_messages(evidence)
        self.assertEqual(prompt(START + 1, START + 2), prompt(START + 3, START + 2))

    def test_snapshot_comments_are_scrubbed_before_leaving(self):
        found = report_ai.negative_posts_for(SimpleNamespace(projects=[SimpleNamespace(
            table_id=TABLE, report_rows=[negative_row("甲", (("加我 13800008888 @某账号", "加我"),))])]),
            TABLE)
        text = found[0]["comments"][0]["content"]
        self.assertNotIn("13800008888", text)
        self.assertNotIn("@某账号", text)


class TestNormalise(unittest.TestCase):
    def evidence(self, *texts):
        return {"comments": [{"content": t, "keywords": ""} for t in texts],
                "titles": [], "keywords": []}

    def test_competitor_must_appear_in_the_evidence(self):
        draft = report_ai.normalize_draft(
            {"features": "围绕露营装备展开。", "competitors": [{"name": "牧高笛"}, {"name": "瞎编品牌"}]},
            self.evidence("牧高笛那款帐篷不结实"))
        self.assertEqual([item["name"] for item in draft["competitors"]], ["牧高笛"])

    def test_a_name_only_in_our_own_titles_is_not_a_competitor(self):
        """标题是运营给自家帖子写的：自家品牌、对比测评里的名字都在里面。"""
        evidence = {"comments": [{"content": "挪客那款更轻", "keywords": ""}],
                    "titles": ["途鸽对比牧高笛和挪客"], "keywords": []}
        draft = report_ai.normalize_draft(
            {"features": "横向对比。", "competitors": ["途鸽", "牧高笛", "挪客"]}, evidence)
        self.assertEqual([c["name"] for c in draft["competitors"]], ["挪客"])
        self.assertEqual(draft["competitors"][0]["mentions"], 1)

    def test_a_competitor_in_the_posts_snapshot_comments_counts(self):
        evidence = {"comments": [], "keywords": [], "posts": [
            {"title": "途鸽对比牧高笛", "comments": [{"content": "挪客那款更轻", "keywords": "挪客"}]}]}
        draft = report_ai.normalize_draft(
            {"features": "横向对比。", "competitors": ["牧高笛", "挪客"]}, evidence)
        self.assertEqual([c["name"] for c in draft["competitors"]], ["挪客"],
                         "只出现在标题里的名字不算")

    def test_no_competitors_is_normal_not_an_error(self):
        draft = report_ai.normalize_draft({"features": "情绪偏焦虑。", "competitors": []},
                                          self.evidence("有点不安"))
        self.assertEqual(draft["competitors"], [])

    def test_missing_features_is_rejected(self):
        for bad in ({}, {"features": "   "}, {"features": 3}):
            with self.subTest(bad=bad), self.assertRaises(report_ai.ReportAIError):
                report_ai.normalize_draft(bad, self.evidence("x"))

    def test_non_object_and_bad_competitor_shapes_are_rejected(self):
        with self.assertRaises(report_ai.ReportAIError):
            report_ai.normalize_draft(["nope"], self.evidence("x"))
        with self.assertRaises(report_ai.ReportAIError):
            report_ai.normalize_draft({"features": "x", "competitors": "牧高笛"},
                                      self.evidence("牧高笛"))


class FakeReports:
    """假 DailyReports：只实现 generate 用到的那一次 get。"""

    def __init__(self, events, report_payload=None, error=""):
        self.events, self.error = events, error
        self.payload = report_payload if report_payload is not None else report()
        self.calls = []

    def get(self, overview, *, date="", table_id="", with_events=False):
        self.calls.append((date, table_id, with_events))
        self.requested = with_events
        return {"reports": [self.payload], "error": self.error}, self.events


class TestGenerate(unittest.TestCase):
    def make(self, responses, events=None, error="", timeout=5.0, report_payload=None):
        reports = FakeReports(events if events is not None else [scan()], error=error,
                              report_payload=report_payload)
        seen = []

        def opener(request, timeout=None):
            seen.append((request, timeout))
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        drafts = report_ai.ReportDrafts("sk-test", reports, timeout=timeout,
                                        opener=opener, wall=lambda: 1000.0)
        drafts.seen = seen
        drafts.reports = reports
        return drafts

    def test_success_returns_draft_without_echoing_evidence(self):
        drafts = self.make([answer({"features": "围绕露营装备，情绪偏焦虑。",
                                    "competitors": [{"name": "牧高笛"}]})])
        overview = SimpleNamespace(projects=[SimpleNamespace(table_id=TABLE, report_rows=[
            negative_row("帐篷实测", (("牧高笛那款不舒服", "不舒服"),))])])
        result = drafts.generate(overview, date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "ok")
        self.assertIn("露营", result["features"])
        self.assertEqual(result["competitors"][0]["name"], "牧高笛")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertNotIn("sk-test", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("不舒服", json.dumps(result, ensure_ascii=False))

    def test_request_body_uses_official_model_and_json_mode(self):
        drafts = self.make([answer({"features": "x", "competitors": []})])
        drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        request, timeout = drafts.seen[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertGreater(body["max_tokens"], 0)
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertIn("Bearer sk-test", request.get_header("Authorization"))
        self.assertEqual(timeout, 5.0)

    def test_same_evidence_is_cached_and_not_billed_twice(self):
        drafts = self.make([answer({"features": "x", "competitors": []})])
        first = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        second = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(drafts.seen), 1)

    def test_force_bypasses_the_cache(self):
        drafts = self.make([answer({"features": "第一次", "competitors": []}),
                            answer({"features": "第二次", "competitors": []})])
        drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        # 冷却过了才算「真的想再来一次」（冷却本身另有专门用例）
        drafts._wall = lambda: 1000.0 + report_ai.FORCE_COOLDOWN_SECONDS + 1
        again = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE, force=True)
        self.assertEqual(again["features"], "第二次")
        self.assertEqual(len(drafts.seen), 2)

    def test_moving_as_of_does_not_make_new_evidence(self):
        """当期的 as_of_ms 每分钟都在走。它不送给模型，就不能让缓存失效、重复计费。"""
        drafts = self.make([answer({"features": "x", "competitors": []})])
        drafts.generate(overview_with("甲"), date="2026-10-28", table_id=TABLE)
        drafts.reports.payload = report(
            window={"start_ms": START, "end_ms": END, "as_of_ms": START + 60_000})
        again = drafts.generate(overview_with("甲"), date="2026-10-28", table_id=TABLE)
        self.assertTrue(again["cached"])
        self.assertEqual(len(drafts.seen), 1)

    def test_handled_posts_are_described_too(self):
        """正文写「仍有负面的帖子共 7 篇：已处理 6……」，AI 也要讲这 7 篇，
        不能只讲还没处理完的那 1 篇。"""
        drafts = self.make([answer({"features": "x", "competitors": []})])
        drafts.generate(overview_with("已处理的", "还没处理的"), date="2026-10-28",
                        table_id=TABLE)
        user = json.loads(drafts.seen[0][0].data.decode("utf-8"))["messages"][1]["content"]
        self.assertIn("还没处理的", user)
        self.assertIn("已处理的", user)

    def test_force_reserves_the_cooldown_before_queueing(self):
        """两个同时到的「重新生成」：冷却要在检查的同一把锁里占上，不能等真正
        外呼时才记——否则两个都过了检查，排队等锁，然后各计费一次。"""
        drafts = self.make([answer({"features": "x", "competitors": []})])
        held = []
        real = drafts._key_lock

        def spy(key):
            held.append(dict(drafts._force_at))
            return real(key)

        drafts._key_lock = spy
        drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE, force=True)
        self.assertIn(f"{TABLE}:2026-10-28", held[0])

    def test_waiters_reuse_the_result_and_locks_do_not_pile_up(self):
        """等同一把锁的请求拿前一个的结果；用完的锁要删掉，不能按条数淘汰在用的。"""
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def opener(request, timeout=None):
            calls.append(request)
            entered.set()
            gate.wait(5)
            return answer({"features": "x", "competitors": []})

        drafts = report_ai.ReportDrafts("sk-test", FakeReports([scan()]),
                                        opener=opener, wall=lambda: 1000.0)
        results = []
        threads = [threading.Thread(target=lambda: results.append(drafts.generate(
            overview_with(), date="2026-10-28", table_id=TABLE))) for _ in range(3)]
        threads[0].start()
        self.assertTrue(entered.wait(5))
        for thread in threads[1:]:
            thread.start()
        gate.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(r["cached"] for r in results), [False, True, True])
        self.assertEqual(drafts._locks, {})

    def test_changed_evidence_is_a_new_request(self):
        drafts = self.make([answer({"features": "一", "competitors": []}),
                            answer({"features": "二", "competitors": []})])
        drafts.generate(overview_with("甲"), date="2026-10-28", table_id=TABLE)
        drafts.generate(overview_with("乙"), date="2026-10-28", table_id=TABLE)
        self.assertEqual(len(drafts.seen), 2)

    def test_no_evidence_never_calls_the_model(self):
        drafts = self.make([answer({"features": "不该被用到", "competitors": []})])
        result = drafts.generate(NO_NEGATIVES, date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("没有最近一次检查仍看到负面评论的帖子", result["error"])
        self.assertEqual(drafts.seen, [])

    def test_missing_key_returns_unavailable_without_network(self):
        drafts = self.make([answer({"features": "x", "competitors": []})])
        drafts.api_key = ""
        result = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(drafts.seen, [])

    def test_http_failure_is_sanitised_and_does_not_leak_the_key(self):
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 401, "unauth", {},
            io.BytesIO(b'{"error":"bad key sk-test"}'))
        drafts = self.make([error])
        with self.assertRaises(report_ai.ReportAIError) as ctx:
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertNotIn("sk-test", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_timeout_and_connection_errors_are_actionable(self):
        drafts = self.make([TimeoutError("timed out")])
        with self.assertRaisesRegex(report_ai.ReportAIError, "超时|连接"):
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)

    def test_garbage_body_is_rejected(self):
        for body in (b"not json", b'{"choices":[]}',
                     json.dumps({"choices": [{"message": {"content": "no json here"}}]}).encode()):
            with self.subTest(body=body):
                drafts = self.make([FakeResponse(body)])
                with self.assertRaises(report_ai.ReportAIError):
                    drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)

    def test_empty_content_is_reported_not_silently_used(self):
        drafts = self.make([FakeResponse(json.dumps(
            {"choices": [{"message": {"content": ""}}]}).encode())])
        with self.assertRaisesRegex(report_ai.ReportAIError, "空白"):
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)

    def test_table_id_is_required(self):
        drafts = self.make([answer({"features": "x", "competitors": []})])
        with self.assertRaises(ValueError):
            drafts.generate(overview_with(), date="2026-10-28", table_id="")

    def test_a_historical_window_never_borrows_todays_titles(self):
        """历史窗口那一刻的标题没被留痕。拿此刻的顶上去 = 把今天写成那一天。"""
        drafts = self.make([answer({"features": "不该被调用", "competitors": []})],
                           events=[], report_payload=report(
                               display_mode="journal",
                               current_snapshot={"available": False,
                                                 "window_is_current": False}))
        result = drafts.generate(overview_with("今天才上榜的帖子"),
                                 date="2026-10-20", table_id=TABLE)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIn("历史窗口", result["error"])
        self.assertEqual(drafts.seen, [], "没有证据就不该花钱")

    def test_a_historical_window_with_comments_omits_titles(self):
        drafts = self.make([answer({"features": "只依据评论。", "competitors": []})],
                           report_payload=report(
                               display_mode="journal",
                               current_snapshot={"available": False,
                                                 "window_is_current": False}))
        result = drafts.generate(overview_with("今天才上榜的帖子"),
                                 date="2026-10-20", table_id=TABLE)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["title_count"], 0)
        user = json.loads(drafts.seen[0][0].data.decode("utf-8"))["messages"][1]["content"]
        self.assertNotIn("今天才上榜的帖子", user)

    def test_the_current_window_does_send_titles(self):
        drafts = self.make([answer({"features": "x", "competitors": []})])
        result = drafts.generate(overview_with("露营装备清单"), date="2026-10-28",
                                 table_id=TABLE)
        self.assertEqual(result["title_count"], 1)
        user = json.loads(drafts.seen[0][0].data.decode("utf-8"))["messages"][1]["content"]
        self.assertIn("露营装备清单", user)

    def test_force_is_cooled_down_server_side(self):
        """口令是共用的，浏览器上的 confirm 挡不住直接打接口。"""
        drafts = self.make([answer({"features": "第一次", "competitors": []}),
                            answer({"features": "第二次", "competitors": []})])
        drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        # 立刻 force：还在冷却里，直接给缓存结果，不再计费
        again = drafts.generate(overview_with(), date="2026-10-28",
                                table_id=TABLE, force=True)
        self.assertEqual(again["features"], "第一次")
        self.assertTrue(again["cached"])
        self.assertEqual(len(drafts.seen), 1)
        # 冷却过去之后 force 才真的再请求一次
        drafts._wall = lambda: 1000.0 + report_ai.FORCE_COOLDOWN_SECONDS + 1
        third = drafts.generate(overview_with(), date="2026-10-28",
                                table_id=TABLE, force=True)
        self.assertEqual(third["features"], "第二次")
        self.assertEqual(len(drafts.seen), 2)

    def test_a_failed_force_is_also_cooled_down(self):
        """失败没有缓存可回，但也不能让连点变成连续计费。"""
        drafts = self.make([TimeoutError("boom"), answer({"features": "x", "competitors": []})])
        with self.assertRaises(report_ai.ReportAIError):
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE, force=True)
        blocked = drafts.generate(overview_with(), date="2026-10-28",
                                  table_id=TABLE, force=True)
        self.assertEqual(blocked["status"], "throttled")
        self.assertEqual(len(drafts.seen), 1, "冷却期内没有第二次请求")

    def test_overlong_features_are_clipped_visibly(self):
        long_text = "长" * (report_ai.MAX_FEATURES_CHARS + 120)
        drafts = self.make([answer({"features": long_text, "competitors": []})])
        result = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertLessEqual(len(result["features"]), report_ai.MAX_FEATURES_CHARS + 1)
        self.assertTrue(result["features"].endswith("…"))

    def test_failed_request_is_not_cached(self):
        drafts = self.make([TimeoutError("boom"), answer({"features": "重试成功", "competitors": []})])
        with self.assertRaises(report_ai.ReportAIError):
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        result = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(drafts.seen), 2)


if __name__ == "__main__":
    unittest.main()
