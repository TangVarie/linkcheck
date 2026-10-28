"""DeepSeek 日报草稿：证据最小化、竞品可核验、缓存与失败降级。

全部离线。没有任何一条用例会真的打 api.deepseek.com——`_call` 的 opener
在每条用例里都被替换成假函数，Key 也只用假值。
"""

import hashlib
import io
import json
import unittest
import urllib.error
from types import SimpleNamespace

from xhsearch import daily_report as d
from xhsearch import report_ai

START, END = d.window_for_date("2026-10-28")
TABLE = "tbl1"


def scan(at=START + 1000, *, post="xhs:one", hits=None, table=TABLE, **kw):
    return {"type": "scan", "at": at, "table_id": table, "record_id": "rec-secret",
            "post_key": post, "negative_checked": True,
            "hits": hits if hits is not None else [
                {"id": "c1", "content": "这个用了之后有点不舒服", "keywords": ["不舒服"]}],
            **kw}


def report(**changes):
    base = {"table_id": TABLE, "label": "途鸽",
            "window": {"start_ms": START, "end_ms": END, "as_of_ms": END},
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
        evidence = report_ai.evidence_for_prompt([scan()], report(),
                                                 ["露营装备清单"])
        messages = report_ai.prompt_messages(evidence)
        self.assertIn("json", messages[0]["content"].lower())
        user = messages[1]["content"]
        self.assertIn("露营装备清单", user)
        self.assertIn("10月27日 16:00", user)
        self.assertNotIn(str(END), user)
        self.assertNotIn("rec-secret", user)

    def test_titles_come_only_from_the_selected_table_and_dedupe(self):
        overview = SimpleNamespace(projects=[
            SimpleNamespace(table_id=TABLE, todos=[SimpleNamespace(label="甲"), SimpleNamespace(label="甲")],
                            adhoc_todos=[SimpleNamespace(label="乙")]),
            SimpleNamespace(table_id="tbl2", todos=[SimpleNamespace(label="别人的")],
                            adhoc_todos=[]),
        ])
        self.assertEqual(report_ai.alert_titles_for(overview, TABLE), ["甲", "乙"])


class TestNormalise(unittest.TestCase):
    def evidence(self, *texts):
        return {"comments": [{"content": t, "keywords": ""} for t in texts],
                "titles": [], "keywords": []}

    def test_competitor_must_appear_in_the_evidence(self):
        draft = report_ai.normalize_draft(
            {"features": "围绕露营装备展开。", "competitors": [{"name": "牧高笛"}, {"name": "瞎编品牌"}]},
            self.evidence("牧高笛那款帐篷不结实"))
        self.assertEqual([item["name"] for item in draft["competitors"]], ["牧高笛"])

    def test_competitor_in_a_title_also_counts(self):
        evidence = {"comments": [{"content": "一般", "keywords": ""}],
                    "titles": ["对比牧高笛和挪客"], "keywords": []}
        draft = report_ai.normalize_draft(
            {"features": "横向对比。", "competitors": ["挪客"]}, evidence)
        self.assertEqual(draft["competitors"][0]["name"], "挪客")

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


def overview_with(*titles):
    return SimpleNamespace(projects=[SimpleNamespace(
        table_id=TABLE, todos=[SimpleNamespace(label=t) for t in titles], adhoc_todos=[])])


class TestGenerate(unittest.TestCase):
    def make(self, responses, events=None, error="", timeout=5.0):
        reports = FakeReports(events if events is not None else [scan()], error=error)
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
                                    "competitors": [{"name": "牧高笛"}]})],
                           events=[scan(hits=[{"id": "c1", "content": "牧高笛那款不舒服",
                                               "keywords": ["不舒服"]}])])
        result = drafts.generate(overview_with("帐篷实测"), date="2026-10-28", table_id=TABLE)
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
        again = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE, force=True)
        self.assertEqual(again["features"], "第二次")
        self.assertEqual(len(drafts.seen), 2)

    def test_changed_evidence_is_a_new_request(self):
        drafts = self.make([answer({"features": "一", "competitors": []}),
                            answer({"features": "二", "competitors": []})])
        drafts.generate(overview_with("甲"), date="2026-10-28", table_id=TABLE)
        drafts.generate(overview_with("乙"), date="2026-10-28", table_id=TABLE)
        self.assertEqual(len(drafts.seen), 2)

    def test_no_evidence_never_calls_the_model(self):
        drafts = self.make([answer({"features": "不该被用到", "competitors": []})], events=[])
        result = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "insufficient_evidence")
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

    def test_failed_request_is_not_cached(self):
        drafts = self.make([TimeoutError("boom"), answer({"features": "重试成功", "competitors": []})])
        with self.assertRaises(report_ai.ReportAIError):
            drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        result = drafts.generate(overview_with(), date="2026-10-28", table_id=TABLE)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(drafts.seen), 2)


if __name__ == "__main__":
    unittest.main()
