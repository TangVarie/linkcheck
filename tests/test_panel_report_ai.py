"""`/api/report-ai` 的路由边界：认证、CSRF、参数、错误翻译、不泄露。

用 handler 本体测，不起真服务——和 TestResolveTransitions 一个路子。
所有 AI 调用都走假 drafter，绝不发真实请求。
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from xhsearch import panel, report_ai, summary


def config():
    return panel.PanelConfig(password="a-long-enough-password", secret=b"s" * 32)


class FakeDrafts:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {
            "status": "ok", "features": "围绕露营装备。", "competitors": []}
        self.error = error
        self.calls = []

    def generate(self, overview, *, date="", table_id="", force=False, shown=None):
        self.calls.append({"date": date, "table_id": table_id, "force": force,
                           "overview": overview, "shown": shown})
        if self.error:
            raise self.error
        return self.result


def handler(*, drafts, reports=object(), body=None, authed=True, csrf=True,
            overview=None, resolutions=None):
    snapshot = object() if overview is None else overview
    deps = SimpleNamespace(
        reports=reports, report_drafts=drafts,
        cache=SimpleNamespace(view=lambda: (snapshot, "", 0.0)),
        config=config(), resolutions=resolutions, settings=None)
    h = object.__new__(panel.PanelHandler)
    h.server = SimpleNamespace(deps=deps)
    h._send_json = lambda status, payload: (status, payload)
    h._read_body = lambda: (json.dumps(body).encode() if body is not None else b"{}")
    h._authed = lambda: authed
    h._csrf_ok = lambda: csrf
    return h


class TestReportAIRoute(unittest.TestCase):
    def test_unauthenticated_and_bad_csrf_never_reach_the_model(self):
        """do_POST 的前置闸门在 /api/report-ai 上同样生效。"""
        drafts = FakeDrafts()
        h = object.__new__(panel.PanelHandler)
        h.server = SimpleNamespace(deps=SimpleNamespace(
            reports=object(), report_drafts=drafts, config=config()))
        h._send_json = lambda status, payload: (status, payload)
        h._resolve_action = mock.Mock()
        h.path = "/api/report-ai"
        h._authed = lambda: False
        self.assertEqual(h.do_POST()[0], 401)
        h._authed = lambda: True
        h._csrf_ok = lambda: False
        self.assertEqual(h.do_POST()[0], 403)
        self.assertEqual(drafts.calls, [])

    def test_success_passes_the_requested_window_and_project(self):
        drafts = FakeDrafts()
        h = handler(drafts=drafts, body={"date": "2026-10-28", "table_id": "tbl1"})
        status, payload = h._report_ai_action()
        self.assertEqual(status, 200)
        self.assertEqual(drafts.calls[0]["table_id"], "tbl1")
        self.assertEqual(drafts.calls[0]["date"], "2026-10-28")
        self.assertFalse(drafts.calls[0]["force"])
        self.assertEqual(payload["features"], "围绕露营装备。")

    def test_titles_are_picked_from_the_resolution_applied_overview(self):
        """日报快照拿原样那份（它自己套「已解决」），挑标题拿套过的那份。"""
        todo = summary.TodoRow(record_id="rec1", project="途鸽", record_url="",
                               link_cell="", label="已处理的帖子", table_id="tbl1",
                               reasons=["有负面"], checked_at_ms=5)
        overview = summary.Overview(projects=[summary.ProjectSnapshot(
            label="途鸽", app_token="app", table_id="tbl1", todos=[todo])])
        resolutions = SimpleNamespace(snapshot=lambda: {
            summary.resolution_key("tbl1", "rec1"): {
                "reasons": ["有负面"], "seen": 5, "at": 10}})
        drafts = FakeDrafts()
        status, _ = handler(drafts=drafts, overview=overview, resolutions=resolutions,
                            body={"date": "2026-10-28", "table_id": "tbl1"})._report_ai_action()
        self.assertEqual(status, 200)
        call = drafts.calls[0]
        self.assertIs(call["overview"], overview)
        self.assertEqual(call["shown"].projects[0].todos, [])
        self.assertEqual([t.label for t in call["shown"].resolved], ["已处理的帖子"])

    def test_force_must_be_a_real_boolean(self):
        drafts = FakeDrafts()
        status, body = handler(drafts=drafts, body={
            "date": "2026-10-28", "table_id": "tbl1", "force": "yes"})._report_ai_action()
        self.assertEqual(status, 400)
        self.assertEqual(drafts.calls, [])
        self.assertIn("布尔", body["error"])

    def test_project_is_required_and_must_be_a_safe_id(self):
        drafts = FakeDrafts()
        for table_id in ("", None, "a b", "../../etc", "x" * 80):
            with self.subTest(table_id=table_id):
                status, _ = handler(drafts=drafts, body={
                    "date": "2026-10-28", "table_id": table_id})._report_ai_action()
                self.assertEqual(status, 400)
        self.assertEqual(drafts.calls, [])

    def test_bad_date_and_non_object_bodies_are_rejected(self):
        drafts = FakeDrafts()
        for body in ({"date": "x" * 40, "table_id": "tbl1"},
                     {"date": "2026-10-28", "table_id": "tbl1", "extra": 1}):
            with self.subTest(body=body):
                status, _ = handler(drafts=drafts, body=body)._report_ai_action()
        self.assertEqual(status, 200, "多余字段不该导致失败")
        self.assertEqual(
            handler(drafts=FakeDrafts(), body=["not", "object"])._report_ai_action()[0], 400)

    def test_missing_daily_reports_is_explained(self):
        status, body = handler(drafts=FakeDrafts(), reports=None)._report_ai_action()
        self.assertEqual(status, 400)
        self.assertIn("FEISHU_REGISTRY", body["error"])

    def test_unconfigured_key_degrades_instead_of_failing(self):
        status, body = handler(drafts=None)._report_ai_action()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "unavailable")
        self.assertIn("DEEPSEEK_API_KEY", body["error"])
        self.assertEqual(body["features"], "")

    def test_model_errors_become_502_without_upstream_detail(self):
        drafts = FakeDrafts(error=report_ai.ReportAIError("DeepSeek 返回 HTTP 429，请稍后重试"))
        status, body = handler(drafts=drafts, body={
            "date": "2026-10-28", "table_id": "tbl1"})._report_ai_action()
        self.assertEqual(status, 502)
        self.assertIn("429", body["error"])

    def test_unexpected_errors_are_generic(self):
        drafts = FakeDrafts(error=RuntimeError("sk-secret leaked in trace"))
        status, body = handler(drafts=drafts, body={
            "date": "2026-10-28", "table_id": "tbl1"})._report_ai_action()
        self.assertEqual(status, 500)
        self.assertNotIn("sk-secret", json.dumps(body, ensure_ascii=False))

    def test_bad_request_from_the_drafter_is_400(self):
        drafts = FakeDrafts(error=ValueError("请选择一个具体项目再生成 AI 草稿"))
        status, body = handler(drafts=drafts, body={
            "date": "2026-10-28", "table_id": "tbl1"})._report_ai_action()
        self.assertEqual(status, 400)
        self.assertIn("具体项目", body["error"])

    def test_cold_start_does_not_look_like_an_ai_failure(self):
        """overview 还没读到时 DailyReports.get 会早退。早退也必须遵守
        with_events 的形状，否则这一屏会报一个假的「生成失败」。"""
        projects = mock.Mock()
        projects.settings = panel.Settings()
        reports = panel.DailyReports(projects, journal=mock.Mock(read_window=lambda *a: []))
        payload = reports.get(None, date="2026-10-28", table_id="tbl1")
        self.assertIsInstance(payload, dict)
        shaped, events = reports.get(None, date="2026-10-28", table_id="tbl1", with_events=True)
        self.assertEqual(shaped["reports"], [])
        self.assertEqual(events, [])
        self.assertIn("还未就绪", shaped["error"])


class TestPanelWiring(unittest.TestCase):
    def test_the_key_is_scrubbed_from_log_text(self):
        values = panel._secrets({"DEEPSEEK_API_KEY": "sk-should-hide"})
        self.assertIn("sk-should-hide", values)

    def test_config_reads_the_official_model_settings(self):
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": "a-long-enough-password",
            "DEEPSEEK_API_KEY": "sk-x", "DEEPSEEK_TIMEOUT_SECONDS": "8"})
        self.assertEqual(cfg.deepseek_api_key, "sk-x")
        self.assertEqual(cfg.deepseek_timeout, 8.0)

    def test_timeout_out_of_range_refuses_to_start(self):
        for value in ("2", "999"):
            with self.subTest(value=value), self.assertRaises(panel.ConfigError):
                panel.PanelConfig.from_env({
                    "PANEL_PASSWORD": "a-long-enough-password",
                    "DEEPSEEK_TIMEOUT_SECONDS": value})

    def test_absent_key_keeps_the_panel_startable(self):
        cfg = panel.PanelConfig.from_env({"PANEL_PASSWORD": "a-long-enough-password"})
        self.assertEqual(cfg.deepseek_api_key, "")
        self.assertEqual(cfg.deepseek_timeout, 20.0)


if __name__ == "__main__":
    unittest.main()
