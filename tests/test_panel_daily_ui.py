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

    def test_the_negative_metric_says_what_it_counts(self):
        """顶上那个大数字是「还没处理完的」负面帖子（待处理 + 正在处理），不是时段累计。
        上一版叫「当前负面帖子（累计）」，读的人就问「从什么时候累计到什么时候」；再上一版
        叫「仍有负面的帖子」、含已处理的，运营看着 10 篇、底下只剩 3 条（2026-11-10）。"""
        self.assertIn("id=metricNegativeLabel>还没处理完的负面帖子<", self.page())
        self.assertIn('setText("metricNegativeLabel", "还没处理完的负面帖子")', panel_view._SCRIPT)
        self.assertIn('"最近一次检查有负面的共 "', panel_view._SCRIPT)
        self.assertNotIn("仍有负面共", panel_view._SCRIPT, "每篇按各自最近一次检查算，说不了「仍有」")
        self.assertNotIn("累计", self.page())

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
        self.assertEqual(dom.buttons("pending")[0]["aria-pressed"], "false")
        self.assertEqual(len(dom.buttons("resolve")), 1)
        self.assertIn("正在处理", markup)

    def test_pending_state_does_not_render_as_processing(self):
        markup = panel_view._todo_row(self.todo(), 8, False, main=True, resolvable=True)
        self.assertEqual(Elements(markup).buttons("processing")[0]["aria-pressed"], "false")
        self.assertEqual(Elements(markup).buttons("pending")[0]["aria-pressed"], "true")

    def test_the_state_is_one_three_way_switch_not_a_badge_plus_buttons(self):
        """以前一行里是「正在处理」徽标 + 「正在处理」「已处理」两个文字按钮：同一行两个
        「正在处理」，一个是状态、一个是按钮（2026-11-11 运营看混）。现在一个三段切换，
        每个状态只出现一次，当前那一段亮着；点亮着的那一段不发请求。"""
        markup = panel_view._todo_row(self.todo("processing"), 8, False, main=True, resolvable=True)
        self.assertEqual(markup.count("正在处理"), 2, "按钮文字一次 + 它的 title 一次，没有第二个徽标")
        self.assertNotIn("work-state'", markup)
        labels = [a["class"] for tag, a in Elements(markup).tags if tag == "button"]
        self.assertEqual(labels, ["pending", "processing", "resolve"])
        pressed = [a["class"] for tag, a in Elements(markup).tags
                   if tag == "button" and a.get("aria-pressed") == "true"]
        self.assertEqual(pressed, ["processing"])
        script = panel_view._SCRIPT
        self.assertIn('if (b.getAttribute("aria-pressed") === "true") return;', script)
        self.assertIn('".seg button.pending, .seg button.processing, .seg button.resolve, button.unresolve"',
                      script)
        self.assertIn("去飞书这一行", markup)

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
        dom = Elements(markup)
        body = [a for tag, a in dom.tags if a.get("class") == "diagnostic-body"]
        self.assertEqual(len(body), 1)
        self.assertIn("hidden", body[0], "完整诊断不在格子里展开，点「详情」在右侧抽屉里看")
        self.assertEqual(len(dom.buttons("detail")), 1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", markup)
        self.assertNotIn("<script>", markup)

    def test_each_project_is_a_row_with_progress_not_a_bare_number_tab(self):
        """以前是一排横着的标签，上面只有「2 +1」「14 +26」，不解释看不懂。现在一行一个
        项目：要管几行、另几行已归档写成字；进度条、三个数、最近检查时间由脚本按
        work_stats 填（2026-11-11）。"""
        page = self.page()
        tab = page[page.index("<button type=button role=tab"):]
        tab = tab[:tab.index("</button>")]
        self.assertIn("<span class=tab-name>途鸽</span>", tab)
        self.assertIn("行要管", tab)
        for part in ("class=pbar", "class=p-pending", "class=p-processing", "class=p-resolved",
                     "class=tab-states", "class=tab-latest"):
            self.assertIn(part, tab)
        script = panel_view._SCRIPT
        self.assertIn("function renderProjectRows(){", script)
        self.assertIn('"最近检查 " + chinaTime(latest, true)', script)
        self.assertIn('more.textContent = "另 " + old + " 行已归档"', script)

    def test_work_stats_carry_the_latest_check_per_project(self):
        from tests.test_panel_workflow import overview, todo
        source = overview(todo())
        source.projects[0].latest_checked_ms = 1234
        stats = summary.work_stats(source, {}, panel.Settings())
        self.assertEqual(stats[0]["latest_checked_ms"], 1234)

    def test_row_details_open_in_a_side_drawer(self):
        """诊断信息以前挤在窄列里点小三角展开。现在点「详情」或点这一行，在右侧抽屉里看
        完整标题、原因、诊断、处理状态和直达链接（2026-11-11）。"""
        page = self.page()
        dom = Elements(page)
        self.assertIn("hidden", dom.by_id("rowDrawer"))
        for name in ("drawerTitle", "drawerReasons", "drawerActs", "drawerBody", "drawerError",
                     "drawerClose"):
            dom.by_id(name)
        script = panel_view._SCRIPT
        self.assertIn('if (t.closest("button.detail")) { openDrawer(tr); return; }', script)
        self.assertIn('if (t.closest("#rowDrawer .drawer-close")) { closeDrawer(); return; }', script)
        self.assertIn('if (t.closest("a, button, input, label, select, summary")) return;', script,
                      "点行里的链接、按钮、勾选框不算点这一行")
        self.assertIn('var tr = inDrawer ? drawerRow : b.closest("tr");', script,
                      "抽屉里的切换条改的是抽屉开着的那一行")
        self.assertIn('ev.key === "Escape"', script)
        upsert = script[script.index("  function upsertTodo(t, archived){"):
                        script.index("  function upsertResolved(t){")]
        self.assertTrue(upsert.rstrip().endswith("if (tr === drawerRow) fillDrawer(tr);\n  }"),
                        "后台同步把这一行各格都写完之后再重画抽屉，不然抽屉里的诊断慢一拍")
        self.assertIn("!drawerRow.getClientRects().length", script,
                      "这一行被筛选滤掉、或所在项目被切走，都算不在眼前")
        show_tab = script[script.index("  function showTab(id, remember){"):
                          script.index("  var box = document.getElementById(\"filter\");")]
        self.assertIn("closeDrawerIfRowGone();", show_tab, "切项目时抽屉跟着关")
        filtering = script[script.index("  function applyWorkFilter(){"):
                           script.index("  function updateRowState(tr, state){")]
        self.assertIn("closeDrawerIfRowGone();", filtering, "筛选/搜索把这一行滤掉时抽屉跟着关")
        cleanup = script[script.index('      tr.querySelectorAll("button").forEach(function(x){ x.disabled = false; });'):]
        self.assertLess(cleanup.index("drawerHtml.drawerActs = null;"),
                        cleanup.index('updateRowState(tr, tr.dataset.workState || "pending");'),
                        "保存失败后抽屉里的切换条要重画，不然一直灰着没法再试")
        row = panel_view._todo_row(self.todo(), 8, False, main=True, resolvable=True)
        self.assertNotIn("<details", row, "行里不再有要点开的小三角")

    def test_drawer_puts_the_post_text_last_and_has_three_close_buttons(self):
        """标题正文是整段笔记文案，放在抽屉最上面会把原因、处理状态、诊断全挤下去：
        挪到最下面。「关闭」上、中、下各一个，都不跟着内容滚（2026-11-12）。"""
        drawer = panel_view._DRAWER
        scroll = drawer[drawer.index("<div class=drawer-scroll>"):drawer.index("<div class=drawer-foot>")]
        order = [scroll.index(mark) for mark in (
            "id=drawerReasons", "id=drawerComments", "id=drawerActs", "id=drawerError",
            "id=drawerBody", "<h4>标题与正文</h4>", "id=drawerTitle")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(scroll.rstrip().endswith("id=drawerTitle></div></div>"), "标题正文后面什么都没有")
        self.assertEqual(drawer.count("drawer-close"), 3)
        head = drawer[drawer.index("<div class=drawer-head>"):drawer.index("<div class=drawer-scroll>")]
        self.assertIn("id=drawerClose", head)
        self.assertIn("drawer-handle", head, "中间那个是抽屉左边沿的把手，在滚动区外面")
        self.assertNotIn("drawer-close", scroll, "三个都钉在抽屉上，不在滚动的内容里")
        self.assertIn("drawer-close", drawer[drawer.index("<div class=drawer-foot>"):])
        style = panel_view._STYLE
        self.assertIn(".drawer-scroll{flex:1;min-height:0;overflow-y:auto", style)
        self.assertIn(".drawer-head{flex:none", style)
        self.assertIn(".drawer-foot{flex:none", style)

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
        self.assertEqual(numbers["metricNegative"], "9", "待处理 7 + 正在处理 2")
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

    def test_the_big_number_leaves_out_handled_posts(self):
        """评论区里还看得到负面、但已经点了已处理的帖子不算进大数字，放到下面那行小字。"""
        numbers = self.render(self.report(
            negative_posts=10, post_states={"pending": 0, "processing": 3, "resolved": 7}))
        self.assertEqual(numbers["metricNegative"], "3")
        self.assertEqual(numbers["metricNegativeTotal"], "最近一次检查有负面的共 10 篇，其中已处理 7 篇")
        numbers = self.render(self.report(
            negative_posts=10, negative_comments=10, negative_comments_estimated=True,
            post_states={"pending": 0, "processing": 3, "resolved": 7}))
        self.assertEqual(numbers["metricComments"], "这 10 篇共约 10 条负面评论",
                         "条数是这 10 篇加起来的，不是只算已处理的 7 篇")
        self.assertEqual(numbers["metricResolved"], "7")

    def test_unavailable_current_table_keeps_counts_unknown(self):
        numbers = self.render(self.report(available=False))
        for name in ("metricChecked", "metricNegative", "metricPending",
                     "metricProcessing", "metricResolved"):
            self.assertEqual(numbers[name], "—")
        self.assertEqual(numbers["metricNegativeTotal"], "")


if __name__ == "__main__":
    unittest.main()
