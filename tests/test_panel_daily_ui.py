"""项目工作台的服务端边界：状态、快照身份、未加载态及不可信日报文本。"""

import json
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


if __name__ == "__main__":
    unittest.main()
