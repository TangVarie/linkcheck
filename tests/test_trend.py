"""工作台趋势图：最近 14 期每期新出现负面、标为已处理各几篇（2026-11-11）。"""

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import daily_report, panel, panel_view
from xhsearch.config import Settings

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 11, 10, tzinfo=TZ)            # 进行中的这一期：11/10 16:00 起
NOW_MS = int(NOW.timestamp() * 1000)
DAY = daily_report.DAY_MS
LIVE_START = int(datetime(2026, 11, 10, 16, tzinfo=TZ).timestamp() * 1000)


def at(days_ago, hour=12):
    """进行中这一期往前第 days_ago 期里的一个时刻（0 = 进行中的这一期）。"""
    return LIVE_START - days_ago * DAY + (hour - 16) * 3_600_000 + (DAY if hour < 16 else 0)


def scan(post, when, *, negative=True, table="tblA", **extra):
    return {"type": "scan", "at": when, "table_id": table, "record_id": "rec-" + post,
            "post_key": post, "hits": [{"id": "c-" + post, "content": "x"}] if negative else [],
            **extra}


def status(post, when, state="resolved", *, table="tblA", **extra):
    return {"type": "status", "at": when, "table_id": table, "record_id": "rec-" + post,
            "post_key": post, "state": state, **extra}


def coverage(when, table="tblA"):
    return {"type": "coverage", "at": when, "table_id": table}


class TestTrend(unittest.TestCase):
    def run_trend(self, events, tables=("tblA",)):
        return daily_report.trend(events, list(tables), NOW_MS)

    def test_fourteen_periods_ending_with_the_one_in_progress(self):
        periods = self.run_trend([coverage(at(30))])
        self.assertEqual(len(periods), 14)
        self.assertEqual(periods[-1]["start_ms"], LIVE_START)
        self.assertFalse(periods[-1]["complete"])
        self.assertTrue(all(p["complete"] for p in periods[:-1]))
        self.assertEqual({(p["found"], p["handled"]) for p in periods}, {(0, 0)})

    def test_a_negative_counts_once_when_it_appears_not_every_time_it_is_seen(self):
        events = [coverage(at(30)),
                  scan("p1", at(3)), scan("p1", at(2)), scan("p1", at(1)),     # 连着三期都看到
                  scan("p2", at(1)), scan("p3", at(1), negative=False)]
        periods = self.run_trend(events)
        self.assertEqual([p["found"] for p in periods[-4:]], [1, 0, 1, 0],
                         "p1 只在冒出来的那一期算；p3 没负面不算")

    def test_a_negative_that_comes_back_after_a_week_is_new_again(self):
        events = [coverage(at(30)), scan("p1", at(12)), scan("p1", at(2))]
        periods = self.run_trend(events)
        self.assertEqual(periods[-3]["found"], 1, "隔了 7 天以上再出现，算新出现")
        events.append(scan("p1", at(6)))
        self.assertEqual(self.run_trend(events)[-3]["found"], 0, "4 天前刚看到过，不算新")

    def test_handled_counts_posts_the_operator_resolved_in_that_period(self):
        events = [coverage(at(30)),
                  status("p1", at(1)), status("p1", at(1, hour=13)),            # 同一篇点了两次
                  status("p2", at(1)), status("p3", at(1), state="processing"),
                  status("p4", at(1), auto=True),                                # 面板自己补的
                  status("p5", at(0, hour=17))]
        periods = self.run_trend(events)
        self.assertEqual(periods[-2]["handled"], 2)
        self.assertEqual(periods[-1]["handled"], 1, "进行中的这一期也算到此刻")

    def test_a_short_link_expanded_later_is_the_same_post(self):
        events = [coverage(at(30)), scan("xhs:url:abc", at(2)),
                  scan("xhs:6aa7", at(1), post_alias="xhs:url:abc")]
        self.assertEqual([p["found"] for p in self.run_trend(events)[-3:]], [1, 0, 0])

    def test_before_journaling_started_is_unknown_not_zero(self):
        events = [coverage(at(3)), coverage(at(30), "tblB"), scan("p1", at(5), table="tblB")]
        alone = self.run_trend(events)
        self.assertIsNone(alone[0]["found"], "这张表那会儿还没开始留痕")
        self.assertIsNone(alone[0]["handled"])
        self.assertEqual(alone[-1]["found"], 0)
        both = self.run_trend(events, ("tblA", "tblB"))
        self.assertEqual(both[-6]["found"], 1)
        self.assertTrue(both[-6]["partial"], "只有一个项目有留痕：算它的，并标出来")
        self.assertFalse(both[-1]["partial"])

    def test_other_tables_and_future_events_are_ignored(self):
        events = [coverage(at(30)), scan("p1", at(1), table="tblZ"), scan("p2", NOW_MS + 1000)]
        self.assertEqual([p["found"] for p in self.run_trend(events)[-2:]], [0, 0])


class TestTrendRequests(unittest.TestCase):
    def setUp(self):
        from tests.test_panel_workflow import overview, todo
        self.overview = overview(todo())
        self.journal = mock.Mock()
        self.journal.read_scans.return_value = []
        self.journal.read_history.return_value = []
        self.clock = [NOW.timestamp()]
        projects = mock.Mock()
        projects.settings = Settings()
        self.reports = panel.DailyReports(projects, journal=self.journal,
                                         wall=lambda: self.clock[0])

    def test_scans_are_read_for_three_weeks_and_cached_statuses_are_not(self):
        out = self.reports.trend(self.overview)
        self.assertEqual(len(out["periods"]), 14)
        start, end = self.journal.read_scans.call_args.args
        self.assertEqual(end, NOW_MS)
        self.assertEqual(start, LIVE_START + DAY - 21 * DAY, "14 期 + 往前看 7 天")
        self.clock[0] += 600
        self.reports.trend(self.overview)
        self.assertEqual(self.journal.read_scans.call_count, 1, "半小时内不重读三周的扫描")
        self.assertEqual(self.journal.read_history.call_count, 2, "处理状态每次都要最新的")
        self.clock[0] += panel.DailyReports.TREND_TTL_SECONDS
        self.reports.trend(self.overview)
        self.assertEqual(self.journal.read_scans.call_count, 2)

    def test_a_failed_read_comes_back_as_an_error_not_an_exception(self):
        self.journal.read_scans.side_effect = RuntimeError("飞书挂了")
        out = self.reports.trend(self.overview)
        self.assertEqual(out["periods"], [])
        self.assertIn("飞书挂了", out["error"])

    def test_unknown_project_and_cold_start(self):
        with self.assertRaises(ValueError):
            self.reports.trend(self.overview, table_id="tblNope")
        self.assertIn("还未就绪", self.reports.trend(None)["error"])
        self.journal.read_scans.assert_not_called()


@unittest.skipUnless(shutil.which("node"), "浏览器脚本行为检查需要 Node")
class TestTrendChart(unittest.TestCase):
    def render(self, payload):
        start = panel_view._SCRIPT.index("  function renderTrend(j){")
        end = panel_view._SCRIPT.index("  function loadTrend(force, poll){", start)
        script = """
var values = {}, nodes = {};
var document = { getElementById:function(id){ return nodes[id] || (nodes[id] = {
  textContent:'', innerHTML:'', hidden:true, setAttribute:function(k, v){ this[k] = v; } }); } };
function setText(id, value){ values[id] = String(value); }
function chinaTime(ms){ return '11/' + String(ms).padStart(2, '0') + ' 16:00'; }
function esc(t){ return String(t); }
function attr(t){ return String(t); }
FUNCTION
renderTrend(PAYLOAD);
process.stdout.write(JSON.stringify({note: values.trendNote, html: nodes.trendChart.innerHTML,
  text: nodes.trendChart.textContent, hidden: nodes.trend.hidden, label: nodes.trendChart['aria-label'] || ''}));
""".replace("PAYLOAD", json.dumps(payload, ensure_ascii=False)).replace(
            "FUNCTION", panel_view._SCRIPT[start:end])
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)

    def test_bars_carry_their_numbers_and_the_live_period_is_marked(self):
        periods = [{"end_ms": 9, "complete": True, "found": 4, "handled": 2, "partial": False},
                   {"end_ms": 10, "complete": True, "found": None, "handled": None, "partial": False},
                   {"end_ms": 11, "complete": False, "found": 1, "handled": 0, "partial": True}]
        out = self.render({"periods": periods, "error": ""})
        self.assertFalse(out["hidden"])
        self.assertEqual(out["html"].count('class="t-col'), 3)
        self.assertIn('<b>4</b><i class="t-found" style="height:76.0%"></i>', out["html"])
        self.assertIn('<b>2</b><i class="t-handled" style="height:38.0%"></i>', out["html"])
        self.assertIn('class="t-col live"', out["html"])
        self.assertIn(">进行中<", out["html"])
        self.assertIn("没有留痕记录", out["html"], "没留痕的那一期不画成 0")
        self.assertIn("11/09 4 / 2；11/10 无记录；11/11 1 / 0", out["label"])
        self.assertIn("此前 7 天没看到过", out["note"])
        self.assertIn("有的项目当时还没开始留痕", out["note"])

    def test_nothing_to_draw_says_why(self):
        out = self.render({"periods": [], "error": "读取留痕失败：boom"})
        self.assertFalse(out["hidden"])
        self.assertIn("boom", out["note"])
        empty = self.render({"periods": [{"end_ms": 1, "complete": True, "found": None,
                                          "handled": None}], "error": ""})
        self.assertIn("还没有留痕记录", empty["note"])


if __name__ == "__main__":
    unittest.main()
