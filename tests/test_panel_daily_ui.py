"""项目工作台的服务端边界：状态、快照身份、未加载态及不可信日报文本。"""

import json
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from xhsearch import panel, panel_view, summary


class Elements(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def by_id(self, identity):
        return next(attrs for _, attrs in self.tags if attrs.get("id") == identity)

    def buttons(self, class_name):
        return [attrs for tag, attrs in self.tags if tag == "button"
                and class_name in attrs.get("class", "").split()]


class TestPanelDailyUI(unittest.TestCase):
    def todo(self, state="pending", **values):
        defaults = dict(record_id="recA", project="途鸽", table_id="tblA",
                        app_token="baseA", record_url="https://x.feishu.cn/base/baseA?record=recA",
                        link_cell="", label="一个需要跟进的帖子", persona="S-01",
                        reasons=["有负面"], checked_at_ms=1_793_163_600_000)
        defaults.update(values)
        row = summary.TodoRow(**defaults)
        row.work_state = state
        return row

    def page(self, *, reports=None, resolvable=True):
        projects = [summary.ProjectSnapshot(label="途鸽", app_token="baseA", table_id="tblA",
                                            todos=[self.todo()]),
                    summary.ProjectSnapshot(label="没有待办的项目", app_token="baseB", table_id="tblB")]
        config = panel.PanelConfig.from_env({"PANEL_PASSWORD": "test-long-password"})
        return panel_view.overview_page(overview=summary.Overview(projects=projects),
                                       error="", fetched_at=0, config=config,
                                       resolvable=resolvable, reports=reports)

    def test_projects_with_no_todos_are_available_for_reporting(self):
        dom = Elements(self.page())
        options = [a.get("value") for tag, a in dom.tags if tag == "option"]
        self.assertIn("tblA", options)
        self.assertIn("tblB", options)
        self.assertIn("", options, "全部项目是初始汇总入口")
        self.assertNotIn("open", dom.by_id("dailyPanel"), "日报不挤占默认监控视图")

    def test_processing_is_a_shared_visible_state_and_preserves_record_identity(self):
        row = self.todo("processing")
        markup = panel_view._todo_row(row, 8, False, main=True, resolvable=True)
        dom = Elements(markup)
        attrs = next(a for tag, a in dom.tags if tag == "tr")
        self.assertEqual(attrs["data-work-state"], "processing")
        self.assertEqual(attrs["data-tbl"], "tblA")
        self.assertEqual(attrs["data-rec"], "recA")
        self.assertEqual(json.loads(attrs["data-reasons"]), ["有负面"])
        self.assertEqual(int(attrs["data-checked"]), row.checked_at_ms)
        self.assertEqual(dom.buttons("processing")[0]["aria-pressed"], "true")
        self.assertEqual(len(dom.buttons("resolve")), 1)
        self.assertIn("正在处理", markup)

    def test_pending_state_does_not_render_as_processing(self):
        markup = panel_view._todo_row(self.todo(), 8, False, main=True, resolvable=True)
        self.assertEqual(Elements(markup).buttons("processing")[0]["aria-pressed"], "false")

    def test_unavailable_storage_has_no_write_controls(self):
        dom = Elements(panel_view._todo_row(self.todo(), 8, False, main=True, resolvable=False))
        self.assertEqual(dom.buttons("processing"), [])
        self.assertEqual(dom.buttons("resolve"), [])
        self.assertEqual(Elements(self.page(resolvable=False)).by_id("s-work")["data-resolvable"], "false")

    def test_report_data_cannot_end_the_inline_script_or_inject_markup(self):
        report = {"table_id": "tblA", "label": "途鸽", "text": "</script><img src=x onerror=alert(1)>",
                  "limitations": ["'\"<script>alert(2)</script>"]}
        dom = Elements(self.page(reports=[report]))
        self.assertEqual(json.loads(dom.by_id("s-work")["data-reports"]), [report])
        self.assertEqual(sum(tag == "script" for tag, _ in dom.tags), 1)
        self.assertEqual(sum(tag == "img" for tag, _ in dom.tags), 0)

    def test_resolved_record_retains_snapshot_for_safe_undo(self):
        row = self.todo("resolved")
        row.resolved_at_ms = row.checked_at_ms + 1000
        markup = panel_view._resolved_list([row], 8)
        dom = Elements(markup)
        attrs = next(a for tag, a in dom.tags if tag == "tr" and "data-rec" in a)
        self.assertEqual(attrs["data-work-state"], "resolved")
        self.assertEqual(json.loads(attrs["data-reasons"]), ["有负面"])
        self.assertEqual(int(attrs["data-checked"]), row.checked_at_ms)
        self.assertEqual(len(dom.buttons("unresolve")), 1)
        self.assertNotIn("open", dom.by_id("resolvedList"))

    def test_empty_resolved_container_is_hidden_and_does_not_claim_completed_work(self):
        markup = panel_view._resolved_list([], 8)
        self.assertIn("hidden", Elements(markup).by_id("resolvedList"))
        self.assertNotIn("标了「已解决」", markup)

    def test_long_diagnosis_is_collapsed_without_losing_escaped_evidence(self):
        diagnosis = "需人工核实的情况" * 30 + "<script>alert(1)</script>"
        markup = panel_view._todo_row(self.todo(diagnosis=diagnosis), 8, False,
                                      main=True, resolvable=True)
        details = [a for tag, a in Elements(markup).tags
                   if tag == "details" and a.get("class") == "diagnostic"]
        self.assertEqual(len(details), 1)
        self.assertNotIn("open", details[0])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", markup)
        self.assertNotIn("<script>", markup)

    def test_report_loading_does_not_present_unknown_counts_as_zero(self):
        dom = Elements(self.page())
        self.assertIn("disabled", dom.by_id("copyReport"))
        self.assertEqual(dom.by_id("reportText")["placeholder"], "日报载入后显示在这里，可直接复制。")
        self.assertIn("readonly", dom.by_id("reportText"))

    def test_snapshot_metrics_and_optional_details_start_compact(self):
        dom = Elements(self.page())
        for name in ("metricChecked", "metricNegative", "metricPending",
                     "metricProcessing", "metricResolved"):
            dom.by_id(name)
        self.assertNotIn("open", dom.by_id("reportNotes"))
        self.assertNotIn("open", dom.by_id("reportMethod"))
        self.assertIn("全部异常待办 · 行", self.page())

    def test_analysis_is_collapsed_while_operational_filters_stay_available(self):
        """首屏先给处置上下文；五指标与日报需要时再展开。"""
        dom = Elements(self.page())
        self.assertNotIn("open", dom.by_id("workDetails"))
        self.assertNotIn("open", dom.by_id("dailyPanel"))
        dom.by_id("workProject")
        self.assertEqual(dom.by_id("reasonFilter")["id"], "reasonFilter")
        options = [a.get("value") for tag, a in dom.tags
                   if tag == "option" and a.get("value") in ("all", "r", "a", "other")]
        self.assertEqual(options, ["all", "r", "a", "other"])

    def test_compact_todo_row_keeps_all_fields_and_names_the_checkbox(self):
        row = self.todo(persona="S-01", post_url="https://example.com/post/1")
        dom = Elements(panel_view._todo_row(row, 8, False, main=True, resolvable=True))
        cells = [a for tag, a in dom.tags if tag == "td"]
        self.assertEqual(len(cells), 7, "勾选 + 帖子 / 问题 / 诊断 / 评论 / 时间 / 处理")
        checkbox = next(a for tag, a in dom.tags if tag == "input" and a.get("class") == "todoPick")
        self.assertIn("排队刷新", checkbox["aria-label"])
        self.assertTrue(any(a.get("class") == "persona" for _, a in dom.tags))
        self.assertTrue(any(a.get("class") == "post" for _, a in dom.tags))
        self.assertTrue(any("comments" in a.get("class", "").split() for a in cells))

    def test_narrow_screen_rows_become_cards_without_hiding_actions(self):
        css = panel_view._STYLE
        narrow = css[css.index("@media (max-width:700px)"):]
        self.assertIn("grid-template-columns:24px minmax(0,1fr)", narrow)
        self.assertIn("td.acts", narrow)
        self.assertIn("flex-wrap:wrap", narrow)
        self.assertNotIn("display:none}\n  .todos tbody td.acts", narrow)


@unittest.skipUnless(shutil.which("node"), "浏览器脚本行为检查需要 Node")
class TestLiveReportNumbers(unittest.TestCase):
    """执行实际数字渲染函数，防止空留痕覆盖业务表已有数字。"""

    def render(self, report):
        start = panel_view._SCRIPT.index("  function renderReports(){")
        end = panel_view._SCRIPT.index("  function loadReports(poll){", start)
        function = panel_view._SCRIPT[start:end]
        script = """
var values = {}, nodes = {};
var document = {
  getElementById:function(id){ return nodes[id] || (nodes[id] = {
    textContent:'', appendChild:function(child){} }); },
  createElement:function(){ return {textContent:''}; }
};
var reportLoaded = true;
function setText(id, value){ values[id] = String(value); }
function chinaTime(){ return '14:00'; }
function composeReport(){}
var currentReports = REPORT;
FUNCTION
renderReports();
process.stdout.write(JSON.stringify(values));
""".replace("REPORT", json.dumps([report], ensure_ascii=False)).replace("FUNCTION", function)
        result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True,
                                text=True, check=True)
        return json.loads(result.stdout)

    def report(self, **snapshot):
        return {"label": "途鸽", "display_mode": "snapshot", "scanned_posts": 0,
                "negative_posts": 0, "post_states": {"pending": 0},
                "window": {"coverage_started_at_ms": None, "coverage_complete": False},
                "current_snapshot": {"available": True, "window_is_current": True,
                    "window_checked_posts": 120, "negative_posts": 9,
                    "negative_comments": None,
                    "post_states": {"pending": 7, "processing": 2, "resolved": 0}, **snapshot}}

    def test_existing_table_counts_survive_empty_journal(self):
        numbers = self.render(self.report())
        self.assertEqual(numbers["metricChecked"], "120")
        self.assertEqual(numbers["metricNegative"], "9")
        self.assertEqual(numbers["metricPending"], "7")
        self.assertEqual(numbers["metricProcessing"], "2")
        self.assertEqual(numbers["metricResolved"], "0")
        self.assertEqual(numbers["metricComments"], "评论条数待核实")
        self.assertEqual(numbers["workSource"], "现有表快照")

    def test_unknown_history_does_not_invent_zero_or_use_current_check_count(self):
        report = self.report(window_is_current=False)
        report["display_mode"] = "unavailable"
        numbers = self.render(report)
        self.assertEqual(numbers["metricChecked"], "—")
        self.assertEqual(numbers["metricPending"], "7", "明确标识的当前处置仍可查看")
        self.assertIn("当前快照", numbers["workSummary"])

    def test_unavailable_current_table_keeps_counts_unknown(self):
        numbers = self.render(self.report(available=False))
        for name in ("metricChecked", "metricNegative", "metricPending",
                     "metricProcessing", "metricResolved"):
            self.assertEqual(numbers[name], "—")


if __name__ == "__main__":
    unittest.main()
