"""`/api/report-ai` 的路由边界：认证、CSRF、参数、错误翻译、不泄露。

用 handler 本体测，不起真服务——和 TestResolveTransitions 一个路子。
所有 AI 调用都走假 drafter，绝不发真实请求。
"""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from xhsearch import panel, report_ai


def config():
    return panel.PanelConfig(password="a-long-enough-password", secret=b"s" * 32)


class FakeDrafts:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {
            "status": "ok", "features": "围绕露营装备。", "competitors": []}
        self.error = error
        self.calls = []

    def generate(self, overview, *, date="", table_id="", force=False):
        self.calls.append({"date": date, "table_id": table_id, "force": force})
        if self.error:
            raise self.error
        return self.result


def handler(*, drafts, reports=object(), body=None, authed=True, csrf=True):
    deps = SimpleNamespace(
        reports=reports, report_drafts=drafts,
        cache=SimpleNamespace(view=lambda: (object(), "", 0.0)),
        config=config())
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
