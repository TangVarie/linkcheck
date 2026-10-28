"""上线前已有数据也能显示工作台；不把缺失历史和全部待办伪装成负面统计。"""

import copy
import unittest
from datetime import datetime, timedelta, timezone

from xhsearch import runner, summary
from xhsearch.config import Settings


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 28, 14, 40, tzinfo=TZ)
START = datetime(2026, 10, 27, 16, tzinfo=TZ)
END = datetime(2026, 10, 28, 16, tzinfo=TZ)


def ms(value):
    return int(value.timestamp() * 1000)


def record(rid, *, post=None, status=runner.STATUS_OK, negative=False, count=99,
           digest=None, checked=None, reasons=None, link=True):
    f = Settings().fields
    cells = {
        f.link: ("https://www.xiaohongshu.com/explore/" + (post or f"65a1b2c3d4e5f6071829{int(rid):04x}")) if link else "",
        f.publish_time: ms(NOW - timedelta(hours=12)),
        f.last_updated: ms(checked or NOW - timedelta(hours=1)),
        f.refresh_status: status,
        f.comment_count: count,
        f.monitoring: True,
    }
    if negative:
        cells[f.negative_status] = Settings().negative_status.found
    if digest is not None:
        cells[f.negative_digest] = digest
    if reasons:
        cells[f.traffic_status] = reasons
    return {"record_id": str(rid), "fields": cells}


def digest(count):
    return "\n".join(f"{i}. [命中「费用」] 私人昵称: 示例预警内容" for i in range(1, count + 1))


def project(records, **kwargs):
    return summary.build_snapshot(label="途鸽", app_token="base", table_id="tbl",
                                  records=records, settings=Settings(), now=NOW, **kwargs)


def report(snap, resolved=None):
    return summary.snapshot_report(summary.Overview(projects=[snap]), resolved or {},
                                   Settings(), ms(START), ms(END), ms(NOW))[0]


class TestExistingSnapshot(unittest.TestCase):
    def test_existing_successful_rows_populate_before_any_journal_exists(self):
        snap = project([record(1, negative=True, digest=digest(2)), record(2),
                        record(3, reasons=["风控中"]),
                        record(4, status=runner.STATUS_FAILED, negative=True, digest=digest(1))])
        actual = report(snap)
        self.assertTrue(actual["available"])
        self.assertEqual(actual["source"], "current_table")
        self.assertEqual(actual["window_checked_posts"], 3)
        self.assertEqual(actual["negative_posts"], 2)
        self.assertEqual(actual["negative_comments"], 3)
        self.assertEqual(actual["post_states"], {"pending": 2, "processing": 0, "resolved": 0})
        self.assertEqual(actual["window_negative_posts"], 1)
        self.assertEqual(actual["window_negative_comments"], 2)

    def test_other_todo_reasons_never_become_negative_work(self):
        records = [record(i, reasons=["风控中"]) for i in range(1, 49)]
        records[0] = record(1, negative=True, digest=digest(1))
        actual = report(project(records))
        self.assertEqual(actual["window_checked_posts"], 48)
        self.assertEqual(actual["negative_posts"], 1)
        self.assertEqual(actual["post_states"]["pending"], 1)

    def test_negative_count_is_not_total_post_comment_count(self):
        actual = report(project([record(1, negative=True, count=500, digest=None)]))
        self.assertEqual(actual["negative_posts"], 1)
        self.assertIsNone(actual["negative_comments"])
        self.assertIsNone(actual["comment_states"]["pending"])
        self.assertTrue(actual["negative_comments_lower_bound"])

    def test_missing_some_digests_reports_known_lower_bound(self):
        actual = report(project([record(1, negative=True, digest=digest(2)),
                                 record(2, negative=True)]))
        self.assertEqual(actual["negative_comments"], 2)
        self.assertTrue(actual["negative_comments_lower_bound"])

    def test_report_uses_complete_records_before_todo_truncation(self):
        snap = project([record(i, negative=True, digest=digest(1)) for i in range(1, 6)],
                       max_todos=1)
        self.assertEqual(len(snap.todos), 1)
        self.assertEqual(len(snap.report_rows), 5)
        self.assertEqual(report(snap)["negative_posts"], 5)

    def test_duplicates_share_post_identity_without_double_counting_comments(self):
        post = "65a1b2c3d4e5f60718293a4b"
        actual = report(project([record(1, post=post, negative=True, digest=digest(2)),
                                 record(2, post=post, negative=True, digest=digest(3))]))
        self.assertEqual(actual["window_checked_posts"], 1)
        self.assertEqual(actual["negative_posts"], 1)
        self.assertEqual(actual["negative_comments"], 3)
        self.assertTrue(actual["negative_comments_lower_bound"])

    def test_resolution_progress_matches_current_todo_and_does_not_mutate_input(self):
        snap = project([record(1, negative=True, digest=digest(1)),
                        record(2, negative=True, digest=digest(2)),
                        record(3, negative=True, digest=digest(3))])
        checked = ms(NOW - timedelta(hours=1))
        resolved = {"tbl/1": {"state": "processing", "reasons": ["有负面"], "seen": checked, "at": checked},
                    "tbl/2": {"reasons": ["有负面"], "seen": checked, "at": checked}}
        original = copy.deepcopy(resolved)
        actual = report(snap, resolved)
        self.assertEqual(actual["post_states"], {"pending": 1, "processing": 1, "resolved": 1})
        self.assertEqual(actual["comment_states"], {"pending": 3, "processing": 1, "resolved": 2})
        self.assertEqual(resolved, original)
        self.assertTrue(all(t.work_state == "pending" for t in snap.todos))

    def test_fresh_negative_reopens_resolved_and_new_reason_reopens_processing(self):
        snap = project([record(1, negative=True), record(2, negative=True, reasons=["风控中"])])
        old = ms(NOW - timedelta(hours=2))
        actual = report(snap, {"tbl/1": {"reasons": ["有负面"], "seen": old, "at": old},
                               "tbl/2": {"state": "processing", "reasons": ["有负面"], "at": old}})
        self.assertEqual(actual["post_states"]["pending"], 2)

    def test_window_boundaries_and_stale_rows_are_not_historical_daily_counts(self):
        snap = project([record(1, checked=START, negative=True),
                        record(2, checked=START - timedelta(milliseconds=1), negative=True),
                        record(3, checked=END, negative=True)])
        actual = report(snap)
        self.assertEqual(actual["window_checked_posts"], 1)
        self.assertEqual(actual["negative_posts"], 3)
        self.assertEqual(actual["window_negative_posts"], 1)
        self.assertFalse(actual["historical"])

    def test_failed_project_or_missing_full_collection_is_unknown_not_zero(self):
        for snap in (summary.ProjectSnapshot("甲", "base", "tbl", error="读取失败"),
                     summary.ProjectSnapshot("甲", "base", "tbl")):
            actual = report(snap)
            self.assertFalse(actual["available"])
            self.assertIsNone(actual["negative_posts"])
            self.assertIsNone(actual["window_checked_posts"])
        self.assertEqual(report(project([]))["negative_posts"], 0)

    def test_missing_columns_leave_only_unsupported_quantities_unknown(self):
        snap = project([record(1, negative=True, digest=digest(1))])
        snap.report_checked_available = False
        actual = report(snap)
        self.assertIsNone(actual["window_checked_posts"])
        self.assertIsNone(actual["window_negative_posts"])
        self.assertEqual(actual["negative_posts"], 1)
        snap.report_checked_available = True
        snap.report_negative_available = False
        actual = report(snap)
        self.assertEqual(actual["window_checked_posts"], 1)
        self.assertIsNone(actual["negative_posts"])
        self.assertIsNone(actual["post_states"]["pending"])

    def test_unmonitored_rows_only_include_recent_temporary_checks(self):
        rows = summary.build_report_rows(
            [record(1, negative=True), record(2, checked=NOW - timedelta(hours=25), negative=True),
             record(3)], Settings(), now=NOW, adhoc=True)
        self.assertEqual({r.record_id for r in rows}, {"1", "3"})
        self.assertTrue(all(r.adhoc for r in rows))

    def test_private_comment_contents_are_not_retained_or_exposed(self):
        snap = project([record(1, negative=True, digest=digest(2))], show_digest=False)
        self.assertIn(Settings().fields.negative_digest, summary.panel_fields(Settings()))
        self.assertEqual(snap.todos[0].negative_digest, "")
        self.assertNotIn("私人昵称", repr(snap.report_rows))
        self.assertNotIn("示例预警内容", repr(report(snap)))

    def test_missing_links_do_not_collapse_unrelated_business_rows(self):
        actual = report(project([record(1, link=False, negative=True), record(2, link=False, negative=True)]))
        self.assertEqual(actual["negative_posts"], 2)


class TestDigestCounts(unittest.TestCase):
    def test_silent_max_comments_cap_is_a_lower_bound(self):
        self.assertEqual(summary.negative_digest_count(digest(8), Settings()), (8, True))
        self.assertEqual(summary.negative_digest_count(digest(2), Settings()), (2, False))

    def test_explicit_remaining_count_restores_snapshot_count(self):
        self.assertEqual(summary.negative_digest_count(digest(2) + "\n…（还有 4 条未显示）", Settings()), (6, False))
        self.assertEqual(summary.negative_digest_count("…（还有 3 条未显示）", Settings()), (3, False))

    def test_unrecognizable_or_missing_digest_is_unknown(self):
        for value in ("", "（未命中）", "2. [命中「费用」] 破损编号", "手工修改的文本"):
            self.assertEqual(summary.negative_digest_count(value, Settings()), (None, True))


if __name__ == "__main__":
    unittest.main()
