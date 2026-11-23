"""Regression tests for linkcheck audit hardening fixes (stdlib unittest only)."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from xhsearch import (
    balance,
    config,
    feishu,
    panel,
    panel_view,
    protocol,
    providers,
    registry,
    runner,
    shortlink,
    summary,
    transport,
)


class TestBreakerVoiding(unittest.TestCase):
    def test_void_gone_writes_clears_coupled_and_blue_word_plan_on_ok_row(self):
        settings = config.Settings()
        f = settings.fields
        ok_outcome = runner.Outcome(
            record_id="rec_ok",
            status=runner.STATUS_OK,
            fields={
                f.traffic_status: ["疑似限流", "爆贴"],
                f.throttle_baseline: 0,
                f.refresh_status: runner.STATUS_OK,
            },
            reason="",
            credits=1,
            tag_plan=runner.TagPlan(
                field_name=f.traffic_status,
                snapshot_tags=[],
                computed=["疑似限流", "爆贴"],
                namespace=frozenset({"疑似限流", "爆贴"}),
                known_options=None,
                exclusive=(),
                sticky=frozenset(),
                coupled=(f.throttle_baseline,),
            ),
            blue_word_plan=runner.BlueWordPlan(
                field_name=f.blue_words,
                snapshot_words=[],
                new_words=["蓝词示例"],
                remind=True,
                shot_field=f.blue_word_shot,
                shot_value="未截图",
                reason_field=f.failure_reason,
                rescan=True,
                stage="爆贴",
                hot_tier="爆贴",
                sources_field=f.blue_word_sources,
                source_lines={"蓝词示例": "line"},
                rescan_line="rescan",
            ),
        )
        report = runner.RunReport(outcomes=[ok_outcome])
        runner._void_gone_writes(report, settings, gone=4, total=5)

        self.assertTrue(report.breaker_tripped)
        self.assertNotIn(f.traffic_status, ok_outcome.fields)
        self.assertNotIn(f.throttle_baseline, ok_outcome.fields)
        self.assertIsNone(ok_outcome.tag_plan)
        self.assertIsNone(ok_outcome.blue_word_plan)


class TestProviderHardening(unittest.TestCase):
    def test_tikhub_xhs_detail_distinguishes_empty_from_malformed_envelope(self):
        p = providers.get_provider(providers.TIKHUB)

        # Explicit empty data [] -> definitive GONE
        r_empty = p.parse("xhs", "detail", 200, "application/json",
                          json.dumps({"code": 200, "data": {"code": 0, "data": []}}), "r1", "k")
        self.assertIsInstance(r_empty, protocol.Err)
        self.assertEqual(r_empty.kind, protocol.Failure.GONE)
        self.assertTrue(r_empty.definitive)

        # Explicit empty note_list [] -> definitive GONE
        r_empty_nl = p.parse("xhs", "detail", 200, "application/json",
                             json.dumps({"code": 200, "data": {"code": 0, "data": [{"note_list": []}]}}), "r2", "k")
        self.assertIsInstance(r_empty_nl, protocol.Err)
        self.assertEqual(r_empty_nl.kind, protocol.Failure.GONE)
        self.assertTrue(r_empty_nl.definitive)

        # Malformed envelopes -> TRANSPORT unexpected_shape (NOT definitive GONE)
        for bad_data in (None, {}, [{"other_key": 1}], [{"note_list": {"not": "list"}}]):
            r_bad = p.parse("xhs", "detail", 200, "application/json",
                            json.dumps({"code": 200, "data": {"code": 0, "data": bad_data}}), "r3", "k")
            self.assertIsInstance(r_bad, protocol.Err)
            self.assertEqual(r_bad.kind, protocol.Failure.TRANSPORT, f"bad_data={bad_data!r}")
            self.assertEqual(r_bad.code, "unexpected_shape")
            self.assertFalse(r_bad.definitive)

    def test_parsers_redact_api_key_in_error_snippets(self):
        secret = "super-secret-provider-key-999"
        tik = providers.get_provider(providers.TIKHUB)
        r1 = tik.parse("xhs", "detail", 200, "application/json",
                       json.dumps([f"echo {secret}"]), "r1", secret)
        self.assertIsInstance(r1, protocol.Err)
        self.assertNotIn(secret, r1.message)

        sdx = providers.get_provider(providers.SOCIALDATAX)
        r2 = sdx.parse("xhs", "detail", 200, "application/json",
                       json.dumps({"code": 0, "data": {"echo": secret}}), "r2", secret)
        self.assertIsInstance(r2, protocol.Err)
        self.assertNotIn(secret, r2.message)


class TestBalanceHardening(unittest.TestCase):
    def test_disabled_account_clears_yuan_and_rejects_bool_nan(self):
        def _resp(payload):
            return lambda _u, _h: transport.Response(200, "application/json", json.dumps(payload))

        dis = balance.read_tikhub(
            "k", base="https://api.example", usd_to_cny=7.2,
            get=_resp({"code": 200, "user_data": {"balance": 12.5, "account_disabled": True}}),
        )
        self.assertIsNone(dis.amount)
        self.assertIsNone(dis.yuan)
        self.assertTrue(dis.error)

        for bad in (True, "NaN", "inf"):
            b = balance.read_tikhub(
                "k", base="https://api.example", usd_to_cny=7.2,
                get=_resp({"code": 200, "user_data": {"balance": bad}}),
            )
            self.assertIsNone(b.amount, f"bad={bad!r}")
            self.assertTrue(b.error)


class TestShortlinkDomainGuard(unittest.TestCase):
    def test_spoofed_domain_with_valid_id_is_rejected(self):
        res_xhs = shortlink.expand_xhs("https://evil.example/explore/6ad8209b000000003300aac2")
        self.assertFalse(res_xhs.ok)
        self.assertIn("不是小红书域名", res_xhs.reason)

        res_dy = shortlink.expand_douyin("https://evil.example/video/7123456789012345678")
        self.assertFalse(res_dy.ok)
        self.assertIn("不是抖音域名", res_dy.reason)


class TestPanelViewEscapeSlice(unittest.TestCase):
    def test_digest_truncates_before_html_escaping(self):
        # Place quotes right across the 300-char boundary; slicing after _e() would split &quot;
        digest = "a" * 298 + '"""TRAILING_MARKER'
        todo = summary.TodoRow(
            record_id="rec1",
            project="示例项目",
            record_url="https://feishu.cn/base/app1?table=tbl1",
            link_cell="https://www.xiaohongshu.com/explore/6ad8209b000000003300aac2",
            label="测试项",
            app_token="app1",
            table_id="tbl1",
            reasons=["负面评论"],
            digest=digest,
        )
        html = panel_view._todo_row(todo, offset_hours=8, show_digest=True, main=True, resolvable=True)
        self.assertIn("&quot;&quot;</div>", html)
        self.assertNotIn("&quo<", html)
        self.assertNotIn("TRAILING_MARKER", html)


class TestPanelHttpAndResolutions(unittest.TestCase):
    def test_head_healthz_and_queue_app_token_validation(self):
        quiet = mock.patch.object(panel.PanelHandler, "log_message", lambda *a, **k: None)
        quiet.start()
        self.addCleanup(quiet.stop)

        cfg = panel.PanelConfig(password="good-password-1234", secret=b"s" * 32, port=0, cache_seconds=999)
        cfg.app_id, cfg.app_secret = "cli_x", "s"
        queueing = mock.Mock(spec=panel.Queueing)
        queueing.queue.return_value = panel.QueueResult(queued=1, record_ids=["r1"])

        entry = registry.Entry(
            record_id="r-on", label="在管", target="bascnREAL:tblON",
            enabled=True, app_token="bascnREAL", table_id="tblON",
        )
        projects = mock.Mock()
        projects.enabled = True
        projects.list.return_value = [entry]

        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        self.addCleanup(cache.stop)

        srv = panel.build_server(cfg, cache, queueing=queueing, projects=projects, host="127.0.0.1")
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        # HEAD /healthz -> 200 with empty body
        req_head = urllib.request.Request(f"http://127.0.0.1:{port}/healthz", method="HEAD")
        with urllib.request.urlopen(req_head, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

        # Login to get session + CSRF
        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(_NR)
        login_req = urllib.request.Request(f"http://127.0.0.1:{port}/login", data=b"password=good-password-1234")
        try:
            cookie = opener.open(login_req, timeout=5).headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie")
        token = cookie.split("=", 1)[1].split(";")[0]
        auth_headers = {
            "Cookie": f"{panel.COOKIE_NAME}={token}",
            panel.CSRF_HEADER: panel.csrf_token(cfg, token),
        }

        # Spoofed app_token with valid table_id must be skipped, NOT queued
        spoof_body = json.dumps({
            "rows": [{"app_token": "bascnEVIL", "table_id": "tblON", "record_id": "r1"}]
        }).encode()
        req_q = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/queue", data=spoof_body, headers=auth_headers, method="POST"
        )
        with urllib.request.urlopen(req_q, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        self.assertEqual(payload["queued"], 0)
        self.assertTrue(payload["skipped"])
        queueing.queue.assert_not_called()

    def test_resolutions_snapshot_resets_reading_if_spawn_raises(self):
        cfg = panel.PanelConfig(password="good-password-1234", secret=b"s" * 32, port=0)
        cfg.app_id, cfg.app_secret, cfg.registry_target = "cli_x", "s", "bascnA:tblR"
        projects = panel.Projects(cfg, config.Settings(), log=lambda _: None)

        now = [1000.0]

        def _bad_spawn(_fn):
            raise RuntimeError("thread limit")

        res = panel.Resolutions(projects, log=lambda _: None, clock=lambda: now[0], spawn=_bad_spawn)
        res._loaded_at = 0.0  # force stale refresh path (non-first)
        res.snapshot()
        self.assertFalse(res._reading)
        self.assertEqual(res._failed_at, 1000.0)


class TestFeishuTokenLock(unittest.TestCase):
    def test_bitable_token_fetches_once_under_concurrency(self):
        bt = feishu.Bitable("cli_a", "sec_b", "app_c", "tbl_d")
        calls = 0
        barrier = threading.Barrier(6)

        def _fake_post(*_a, **_k):
            nonlocal calls
            calls += 1
            return transport.Response(
                200, "application/json",
                json.dumps({"code": 0, "tenant_access_token": "tok-1", "expire": 7200}),
            )

        with mock.patch.object(feishu.transport, "post_with_retry", side_effect=_fake_post):
            tokens = []

            def _worker():
                barrier.wait()
                tokens.append(bt.token())

            threads = [threading.Thread(target=_worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(calls, 1)
        self.assertEqual(tokens, ["tok-1"] * 6)


if __name__ == "__main__":
    unittest.main()
