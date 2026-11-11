"""每期日报结算时记一笔各项目的负面处置数，工作台指标卡的「比上一期」靠它（2026-11-11）。"""

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import daily_report, panel, panel_settings, panel_view
from xhsearch.config import Settings

TZ = timezone(timedelta(hours=8))
CLOSE = datetime(2026, 11, 10, 16, tzinfo=TZ)
CLOSE_MS = int(CLOSE.timestamp() * 1000)
DAY_MS = 86_400_000


class FakeStore:
    def __init__(self, data=None):
        self.data = {} if data is None else data
        self.reads = self.writes = 0

    def get_json(self, key, default):
        self.reads += 1
        return json.loads(json.dumps(self.data.get(key, default)))

    def update_json(self, key, change, default):
        self.writes += 1
        self.data[key] = change(json.loads(json.dumps(self.data.get(key, default))))
        return self.data[key]


class FakeCache:
    def __init__(self, error=""):
        self.refreshes = 0
        self.error = error

    def refresh(self):
        self.refreshes += 1

    def snapshot(self):
        return object(), self.error, 0.0


def item(table_id, *, available=True, checked=62, negative=10, pending=0, processing=3,
         resolved=7):
    return {"table_id": table_id, "available": available, "window_checked_posts": checked,
            "negative_posts": negative,
            "post_states": {"pending": pending, "processing": processing, "resolved": resolved}}


class TestLastClose(unittest.TestCase):
    def test_the_boundary_is_1600_beijing(self):
        at = lambda *a: int(datetime(*a, tzinfo=TZ).timestamp() * 1000)   # noqa: E731
        self.assertEqual(daily_report.last_close_ms(at(2026, 11, 10, 15, 59)), CLOSE_MS - DAY_MS)
        self.assertEqual(daily_report.last_close_ms(at(2026, 11, 10, 16, 0)), CLOSE_MS)
        self.assertEqual(daily_report.last_close_ms(at(2026, 11, 11, 0, 5)), CLOSE_MS)


class TestPeriodCloses(unittest.TestCase):
    def make(self, minutes_after_close, stored=None):
        self.store = FakeStore({panel_settings.KEY_CLOSES: stored} if stored else None)
        projects = mock.Mock()
        projects._store.return_value = self.store
        projects.settings = Settings()
        self.clock = [(CLOSE + timedelta(minutes=minutes_after_close)).timestamp()]
        self.logs = []
        return panel.PeriodCloses(projects, mock.Mock(snapshot=lambda: {}),
                                  wall=lambda: self.clock[0], log=self.logs.append)

    def tick(self, closes, cache, items):
        with mock.patch.object(panel.summary, "snapshot_report", return_value=items) as report:
            done = closes.tick(cache)
        return done, report

    def test_records_once_right_after_the_close_without_anyone_watching(self):
        closes, cache = self.make(1), FakeCache()
        done, report = self.tick(closes, cache, [item("tblA"), item("tblB", available=False)])
        self.assertTrue(done)
        self.assertEqual(cache.refreshes, 1, "没人开着面板也要自己取一趟数")
        self.assertEqual(report.call_args.args[3:5], (CLOSE_MS - DAY_MS, CLOSE_MS),
                         "「已检查」数的是刚结算的这一期")
        record = closes.get("tblA", CLOSE_MS)
        self.assertEqual({k: record[k] for k in ("checked", "negative", "pending",
                                                 "processing", "resolved")},
                         {"checked": 62, "negative": 10, "pending": 0, "processing": 3,
                          "resolved": 7})
        self.assertIsNone(closes.get("tblB", CLOSE_MS), "读不出来的表不记，不拿空数冒充")
        self.clock[0] += 60
        done, _ = self.tick(closes, cache, [item("tblA", pending=9)])
        self.assertFalse(done)
        self.assertEqual((cache.refreshes, self.store.writes), (1, 1), "一期只记一次")

    def test_too_late_is_not_the_closing_number(self):
        closes, cache = self.make(panel.PeriodCloses.GRACE_SECONDS // 60 + 1), FakeCache()
        done, _ = self.tick(closes, cache, [item("tblA")])
        self.assertFalse(done)
        self.assertEqual(cache.refreshes, 0, "面板停过、晚了一个多小时：这一期宁可没有基准")
        self.assertIsNone(closes.get("tblA", CLOSE_MS))

    def test_a_restart_does_not_record_the_same_close_twice(self):
        stored = {"tblA": {str(CLOSE_MS): {"at": CLOSE_MS + 1, "negative": 10, "pending": 0,
                                           "processing": 3, "resolved": 7, "checked": 62}}}
        closes, cache = self.make(5, stored), FakeCache()
        done, _ = self.tick(closes, cache, [item("tblA", pending=9)])
        self.assertFalse(done)
        self.assertEqual((cache.refreshes, self.store.writes), (0, 0))
        self.assertEqual(closes.get("tblA", CLOSE_MS)["pending"], 0)

    def test_a_failed_read_is_retried_next_minute(self):
        closes, cache = self.make(1), FakeCache(error="FeishuError: boom")
        self.assertFalse(self.tick(closes, cache, [item("tblA")])[0])
        self.assertEqual(self.store.writes, 0, "读表失败时手上是上一份旧快照，不记")
        cache.error = ""
        self.clock[0] += 60
        self.assertTrue(self.tick(closes, cache, [item("tblA")])[0])

    def test_old_closes_are_dropped_and_others_kept(self):
        old = CLOSE_MS - (panel.PeriodCloses.KEEP_DAYS + 1) * DAY_MS
        stored = {"tblA": {str(old): {"negative": 1}, str(CLOSE_MS - DAY_MS): {"negative": 2}},
                  "tblGone": {str(old): {"negative": 3}}}
        closes, cache = self.make(1, stored), FakeCache()
        self.assertTrue(self.tick(closes, cache, [item("tblA")])[0])
        saved = self.store.data[panel_settings.KEY_CLOSES]
        self.assertEqual(sorted(saved), ["tblA"])
        self.assertEqual(sorted(saved["tblA"]), [str(CLOSE_MS - DAY_MS), str(CLOSE_MS)])

    def test_get_never_reads_feishu(self):
        closes = self.make(1)
        self.assertIsNone(closes.get("tblA", CLOSE_MS))
        self.assertEqual(self.store.reads, 0, "出页面的请求不为它等飞书")

    def test_a_failing_tick_does_not_kill_the_thread(self):
        closes = self.make(1)
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        with mock.patch.object(closes, "tick", side_effect=RuntimeError("boom")):
            closes.run(FakeCache(), stop)
        self.assertIn("boom", self.logs[0])


class TestReportCarriesTheCloses(unittest.TestCase):
    def test_each_report_gets_its_start_and_end_close(self):
        from tests.test_panel_workflow import NOW, overview, todo
        projects = mock.Mock()
        projects.settings = Settings()
        reports = panel.DailyReports(projects, journal=mock.Mock(read_window=lambda *a: []),
                                     wall=lambda: NOW.timestamp())
        self.assertNotIn("closes", reports.get(overview(todo()))["reports"][0], "没接就不带")
        reports.closes = mock.Mock()
        reports.closes.get.side_effect = lambda table_id, ms: {"at": ms, "pending": 1}
        shown = reports.get(overview(todo()))["reports"][0]
        window = shown["window"]
        self.assertEqual(shown["closes"]["start"]["at"], window["start_ms"])
        self.assertEqual(shown["closes"]["end"]["at"], window["end_ms"])


@unittest.skipUnless(shutil.which("node"), "浏览器脚本行为检查需要 Node")
class TestDeltaChips(unittest.TestCase):
    def render(self, reports):
        start = panel_view._SCRIPT.index("  function renderReports(){")
        end = panel_view._SCRIPT.index("  function loadReports(poll){", start)
        script = """
var values = {}, nodes = {};
var document = {
  getElementById:function(id){ return nodes[id] || (nodes[id] = {
    textContent:'', appendChild:function(child){} }); },
  createElement:function(){ return {textContent:''}; }
};
var reportLoaded = true;
function setText(id, value){ values[id] = String(value); }
function chinaTime(){ return '11/09 16:00'; }
function composeReport(){}
var currentReports = REPORT;
FUNCTION
renderReports();
var classes = {};
Object.keys(nodes).forEach(function(k){ if (nodes[k].className) classes[k] = nodes[k].className; });
process.stdout.write(JSON.stringify({values: values, classes: classes}));
""".replace("REPORT", json.dumps(reports, ensure_ascii=False)).replace(
            "FUNCTION", panel_view._SCRIPT[start:end])
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)

    def report(self, *, states, before=None, after=None, complete=False, current=True,
               checked=62):
        closes = {"start": before, "end": after}
        return {"label": "途鸽", "display_mode": "snapshot", "scanned_posts": 0,
                "closes": closes,
                "window": {"start_ms": 1, "end_ms": 2, "complete": complete,
                           "coverage_started_at_ms": None, "coverage_complete": False},
                "current_snapshot": {"available": True, "window_is_current": current,
                                     "window_checked_posts": checked,
                                     "negative_posts": sum(states.values()),
                                     "negative_comments": None, "post_states": states}}

    BEFORE = {"checked": 54, "negative": 10, "pending": 1, "processing": 4, "resolved": 5}

    def test_changes_since_the_previous_close(self):
        out = self.render([self.report(states={"pending": 0, "processing": 3, "resolved": 7},
                                       before=self.BEFORE)])
        values, classes = out["values"], out["classes"]
        self.assertEqual(values["metricNegativeDelta"], "↓ 比上一期少 2")
        self.assertEqual(classes["metricNegativeDelta"], "metric-delta good")
        self.assertEqual(values["metricPendingDelta"], "↓ 少 1")
        self.assertEqual(values["metricProcessingDelta"], "↓ 少 1")
        self.assertEqual(classes["metricProcessingDelta"], "metric-delta",
                         "正在处理变少不说明好坏：可能处理完了，也可能退回待处理")
        self.assertEqual(values["metricResolvedDelta"], "↑ 多 2")
        self.assertEqual(classes["metricResolvedDelta"], "metric-delta good")
        self.assertEqual(values["metricCheckedDelta"], "", "这一期没结算完，半期的检查数不和上一整期比")
        self.assertIn("数字后面的变化是和上一期结算时（11/09 16:00）比", values["workSummary"])

    def test_getting_worse_is_red_and_no_change_says_so(self):
        out = self.render([self.report(states={"pending": 3, "processing": 4, "resolved": 5},
                                       before=self.BEFORE)])
        self.assertEqual(out["values"]["metricNegativeDelta"], "↑ 比上一期多 2")
        self.assertEqual(out["classes"]["metricNegativeDelta"], "metric-delta bad")
        self.assertEqual(out["values"]["metricProcessingDelta"], "持平")
        self.assertEqual(out["values"]["metricResolvedDelta"], "持平")

    def test_checked_is_compared_only_between_two_settled_periods(self):
        after = dict(self.BEFORE, checked=62)
        out = self.render([self.report(states={"pending": 0, "processing": 3, "resolved": 7},
                                       before=self.BEFORE, after=after, complete=True)])
        self.assertEqual(out["values"]["metricCheckedDelta"], "↑ 比上一期多 8")
        self.assertEqual(out["classes"]["metricCheckedDelta"], "metric-delta")

    def test_no_baseline_means_no_chip(self):
        states = {"pending": 0, "processing": 3, "resolved": 7}
        for reports in (
                [self.report(states=states)],                                  # 没记过
                [self.report(states=states, before=self.BEFORE),               # 两个项目缺一个
                 self.report(states=states)],
                [self.report(states=states, before=self.BEFORE, current=False)]):  # 历史时段
            values = self.render(reports)["values"]
            for name in ("metricNegativeDelta", "metricPendingDelta", "metricProcessingDelta",
                         "metricResolvedDelta", "metricCheckedDelta"):
                self.assertEqual(values[name], "", name)
            self.assertNotIn("和上一期结算时", values["workSummary"])


if __name__ == "__main__":
    unittest.main()
