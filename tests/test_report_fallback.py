"""线上首日场景：历史留痕为空时，仍能看见现有业务表里的工作。"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from xhsearch import daily_report, panel, runner, summary
from xhsearch.config import Settings


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 28, 14, 40, tzinfo=TZ)
NOW_MS = int(NOW.timestamp() * 1000)
START, END = daily_report.window_for_date(NOW.date())


def business_record(identity, *, negative=False, status=runner.STATUS_OK, digest="",
                    collation=False, checked=None):
    settings = Settings()
    f = settings.fields
    cells = {
        f.link: "https://www.xiaohongshu.com/explore/" + f"{identity:024x}",
        f.publish_time: NOW_MS - 12 * 3_600_000,
        f.last_updated: NOW_MS - 2 * 3_600_000 if checked is None else checked,
        f.monitoring: True,
        f.refresh_status: status,
        f.negative_status: settings.negative_status.found if negative else settings.negative_status.clean,
        f.negative_keywords: ["贵"],
        # 整帖总评论量必须永远不能被拿来补负面评论量。
        f.comment_count: 900,
    }
    if digest:
        cells[f.negative_digest] = digest
    if collation:
        cells[f.collation_status] = settings.collation_status.stale
    return {"record_id": "r" + str(identity), "fields": cells}


def business_overview():
    settings = Settings()
    records = [business_record(1, negative=True, digest="1. [命中「贵」] 示例评论"),
               business_record(2),
               business_record(3, collation=True),
               business_record(4, status=runner.STATUS_FAILED)]
    snap = summary.build_snapshot(label="途鸽第一期", app_token="baseA", table_id="tblA",
                                  records=records, settings=settings, now=NOW,
                                  max_todos=1)
    return summary.Overview(projects=[snap], generated_at=NOW)


class TestExistingTableFallback(unittest.TestCase):
    def setUp(self):
        self.journal = mock.Mock()
        self.journal.read_window.return_value = []
        projects = SimpleNamespace(settings=Settings())
        self.reports = panel.DailyReports(projects, journal=self.journal,
                                         wall=lambda: NOW.timestamp())

    def test_empty_journal_shows_existing_checks_and_only_negative_work(self):
        result = self.reports.get(business_overview())
        report = result["reports"][0]
        self.assertEqual(report["display_mode"], "snapshot")
        snapshot = report["current_snapshot"]
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["window_checked_posts"], 3,
                         "无异常的成功检查也必须统计，刷新失败则不能算成功")
        self.assertEqual(snapshot["negative_posts"], 1)
        self.assertEqual(snapshot["post_states"], {"pending": 1, "processing": 0, "resolved": 0})
        self.assertEqual(snapshot["negative_comments"], 1)
        self.assertNotIn("成功扫描途鸽第一期 0", report["text"])
        self.assertNotIn("900", report["text"])

    def test_unavailable_journal_does_not_hide_good_business_data(self):
        self.journal.read_window.side_effect = RuntimeError("留痕暂不可读")
        report = self.reports.get(business_overview())["reports"][0]
        self.assertEqual(report["display_mode"], "snapshot")
        self.assertTrue(report["current_snapshot"]["available"])
        self.assertEqual(report["current_snapshot"]["window_checked_posts"], 3)

    def test_historical_window_does_not_use_current_table_as_past_report(self):
        report = self.reports.get(business_overview(), date="2026-10-27")["reports"][0]
        self.assertEqual(report["display_mode"], "unavailable")
        self.assertEqual(report["current_snapshot"]["negative_posts"], 1)
        self.assertFalse(report["current_snapshot"]["window_is_current"])
        self.assertNotIn("已检查 0", report["text"])
        self.assertNotIn("待处理 0", report["text"])

    def test_current_report_text_is_short_and_has_readable_lines(self):
        report = self.reports.get(business_overview())["reports"][0]
        self.assertLess(len(report["text"]), 450)
        self.assertGreaterEqual(len(report["text"].splitlines()), 3)
        self.assertIn("项目已纳入监测 4 篇", report["text"])
        self.assertIn("10月27日 16:00—10月28日 14:40", report["text"])
        self.assertIn("新帖高频检查，历史帖分批复查", report["text"])
        self.assertNotIn("本期", report["text"])
        for jargon in ("批次闭合", "留痕分片", "同一行可有状态流转"):
            self.assertNotIn(jargon, report["text"])

    def test_missing_digest_remains_unknown_instead_of_total_post_comments(self):
        snap = summary.build_snapshot(label="途鸽第一期", app_token="baseA", table_id="tblA",
                                      records=[business_record(1, negative=True)],
                                      settings=Settings(), now=NOW)
        report = self.reports.get(summary.Overview(projects=[snap], generated_at=NOW))["reports"][0]
        self.assertIsNone(report["current_snapshot"]["negative_comments"])
        self.assertNotIn("900", report["text"])

    def test_missing_check_columns_do_not_copy_none_or_zero_as_known_work(self):
        source = business_overview()
        source.projects[0].report_checked_available = False
        report = self.reports.get(source)["reports"][0]
        self.assertIsNone(report["current_snapshot"]["window_checked_posts"])
        self.assertEqual(report["current_snapshot"]["negative_posts"], 1)
        self.assertIn("待核实", report["text"])
        self.assertNotIn("None", report["text"])
        self.assertNotIn("已检查 0", report["text"])

    def test_recorded_history_is_retained_without_substituting_current_values(self):
        old_start, _ = daily_report.window_for_date("2026-10-27")
        self.journal.read_window.return_value = [
            {"type": "coverage", "at": old_start - 1000, "table_id": "tblA"},
            {"type": "scan", "at": old_start + 1000, "table_id": "tblA",
             "record_id": "historical", "post_key": "xhs:historical",
             "negative_checked": True, "hits": [{"id": "a"}, {"id": "b"}]},
        ]
        report = self.reports.get(business_overview(), date="2026-10-27")["reports"][0]
        self.assertEqual(report["display_mode"], "journal")
        self.assertEqual(report["negative_comments"], 2)
        self.assertEqual(report["current_snapshot"]["negative_comments"], 1)
        self.assertIn("2 条评论", report["text"])

    def test_daily_report_does_not_publish_internal_snapshots_or_raw_comment_text(self):
        report = self.reports.get(business_overview())["reports"][0]
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("示例评论", serialized)
        self.assertNotIn("report_rows", serialized)
        self.assertNotIn("negative_digest", serialized)


if __name__ == "__main__":
    unittest.main()
