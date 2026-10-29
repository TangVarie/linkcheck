"""日报采集层只留真实评论页；身份和上游时间不能在展示层丢失。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import analyze, protocol, providers, runner, summary
from xhsearch.config import Settings
from xhsearch.rows import Row


NOW = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
AT = int(NOW.timestamp() * 1000)
POST_ID = "6aa74952000000000a0256a3"


class TestCommentIdentity(unittest.TestCase):
    def test_xhs_identity_and_millisecond_time_survive_normalization(self):
        items = providers._xhs_comment_items([
            {"id": "xhs-comment", "content": "踩雷", "create_time": AT - 1000}])
        comment = analyze.read_comment_page("xhs", {"items": items}).comments[0]
        self.assertEqual(comment.comment_id, "xhs-comment")
        self.assertEqual(comment.created_at_ms, AT - 1000)

    def test_douyin_identity_and_second_time_survive_normalization(self):
        result = providers._tikhub_normalize(
            "douyin", "comments", {"data": {
                "comments": [{"cid": "dy-comment", "text": "踩雷", "create_time": AT // 1000}],
                "total": 1}}, 200, "request", "unused-test-key")
        self.assertIsInstance(result, protocol.Ok)
        comment = analyze.read_comment_page("douyin", result.data).comments[0]
        self.assertEqual(comment.comment_id, "dy-comment")
        self.assertEqual(comment.created_at_ms, AT)

    def test_socialdatax_normalized_identity_and_missing_time(self):
        comments = analyze.read_comment_page("xhs", {"items": [
            {"comment_id": "sdx-comment", "content": "踩雷", "create_time": str(AT // 1000)},
            {"content": "踩雷"}]}).comments
        self.assertEqual((comments[0].comment_id, comments[0].created_at_ms), ("sdx-comment", AT))
        self.assertEqual((comments[1].comment_id, comments[1].created_at_ms), ("", None))

    def test_invalid_timestamps_stay_unknown(self):
        for value in (None, False, True, "bad", "nan", float("inf"), -1, 0, AT * 1000, {}):
            with self.subTest(value=value):
                self.assertIsNone(analyze._comment_created_at_ms(value))


class TestScanFacts(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1
        self.settings.soft_deadline_seconds = 0
        self.settings.detail_within_days = 0

    def scan(self, snapshot=None, *, error=None, keywords=("踩雷",), link=None,
             expanded_id="", shared=False):
        row = Row("rec1", link or "https://www.xiaohongshu.com/explore/" + POST_ID,
                  publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000),
                  negative_keywords=list(keywords))
        fetched = runner._Fetched(snapshot=snapshot, error=error,
                                  expanded_id=expanded_id,
                                  expanded_url=link or "", shared=shared)
        with mock.patch.object(runner, "_fetch_one", return_value=fetched):
            return runner.refresh([row], "unused-test-key", self.settings,
                                  forced=True, now=NOW, clock=lambda: NOW).outcomes[0]

    def test_full_negative_fact_is_independent_of_display_truncation(self):
        content = "踩雷" + "正文" * 300
        comment = analyze.CommentView(content, comment_id="c1", created_at_ms=AT - 2000)
        outcome = self.scan(analyze.Snapshot("xhs", comment_count=1, comments=[comment]))
        event = outcome.scan_event
        self.assertEqual(event["at"], AT)
        self.assertEqual(event["record_id"], "rec1")
        self.assertEqual(event["post_key"], "xhs:" + POST_ID)
        self.assertFalse(event["post_key_estimated"])
        self.assertTrue(event["negative_checked"])
        self.assertEqual(event["hits"], [{"id": "c1", "content": content,
                                         "created_at_ms": AT - 2000, "keywords": ["踩雷"]}])
        self.assertNotIn(content, outcome.fields[self.settings.fields.negative_digest])

    def test_confirmed_zero_comments_is_a_scan_without_negative_hits(self):
        event = self.scan(analyze.Snapshot("xhs", comment_count=0)).scan_event
        self.assertEqual(event["visible_comments"], 0)
        self.assertEqual(event["hits"], [])
        self.assertTrue(event["negative_checked"])

    def test_missing_keywords_does_not_claim_negative_check(self):
        event = self.scan(analyze.Snapshot("xhs", comment_count=0), keywords=()).scan_event
        self.assertFalse(event["negative_checked"])

    def test_detail_count_without_visible_comment_page_is_not_a_scan(self):
        outcome = self.scan(analyze.Snapshot("xhs", comment_count=150))
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertIsNone(outcome.scan_event)

    def test_upstream_failure_has_no_successful_scan_fact(self):
        error = protocol.Err(protocol.Failure.TRANSPORT, "offline", "offline")
        outcome = self.scan(error=error)
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertIsNone(outcome.scan_event)

    def test_unresolved_link_has_explicitly_estimated_identity(self):
        event = self.scan(analyze.Snapshot("xhs", comment_count=0),
                          link="https://xhslink.com/abc123").scan_event
        self.assertTrue(event["post_key_estimated"])
        self.assertTrue(event["post_key"].startswith("xhs:url:"))

    def test_expanded_shortlink_uses_canonical_post_id(self):
        event = self.scan(analyze.Snapshot("douyin", comment_count=0),
                          link="https://v.douyin.com/abc123/", expanded_id="7581234567890123456").scan_event
        self.assertEqual(event["post_key"], "douyin:7581234567890123456")
        self.assertFalse(event["post_key_estimated"])

    def test_expanded_shortlink_keeps_the_key_the_panel_still_uses(self):
        """「作品ID」写进表之前，面板只认得链接那个键，运营点的处理状态带的是它。
        扫描事件把它记成别名，日报才认得出那是同一篇。"""
        link = "https://v.douyin.com/abc123/"
        event = self.scan(analyze.Snapshot("douyin", comment_count=0), link=link,
                          expanded_id="7581234567890123456").scan_event
        self.assertEqual(event["post_alias"], summary._post_key(Row("rec1", link)))
        self.assertNotEqual(event["post_alias"], event["post_key"])

    def test_panel_and_journal_compute_the_same_post_key(self):
        for link in ("https://www.xiaohongshu.com/explore/" + POST_ID,
                     "https://xhslink.com/abc123"):
            with self.subTest(link=link):
                event = self.scan(analyze.Snapshot("xhs", comment_count=0), link=link).scan_event
                self.assertEqual(event["post_key"], summary._post_key(Row("rec1", link)))
                self.assertNotIn("post_alias", event)

    def test_same_post_shared_fetch_preserves_expanded_identity(self):
        fetched = runner._Fetched(snapshot=analyze.Snapshot("douyin", comment_count=0),
                                  expanded_id="7581234567890123456", expanded_url="https://v.douyin.com/a/")
        sibling = fetched.for_sibling()
        self.assertEqual((sibling.expanded_id, sibling.expanded_url),
                         (fetched.expanded_id, fetched.expanded_url))
        self.assertTrue(sibling.shared)
        self.assertIsNot(sibling.snapshot, fetched.snapshot)


if __name__ == "__main__":
    unittest.main()
