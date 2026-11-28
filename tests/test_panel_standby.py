"""面板上的「SocialDataX 备用通道」开关，和「最近几轮全部刷新失败」那条带子。

2026-11-28 TikHub 整段回 400 的那五个半小时里，面板上的余额卡全是绿的，备胎一次没被
尝试，运营也没有任何按钮可按。这一组钉住的是事后补上的三件事：开关能点、点了真存下来、
该提醒的时候页面顶上有话、而且话旁边就有那个按钮。
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from xhsearch import balance as balance_mod
from xhsearch import channels, panel, panel_view, railway, standby, summary
from xhsearch.config import Channels, Settings

PASSWORD = "a-long-enough-password"
BOTH = {"tikhub": "t-key", "socialdatax": "s-key"}


class FakeStore:
    def __init__(self, cell=None, *, fail_read=False, fail_write=False):
        self.cell = cell
        self.reads = 0
        self.writes = []
        self.fail_read = fail_read
        self.fail_write = fail_write

    def read_one(self, key):
        assert key == standby.KEY
        self.reads += 1
        if self.fail_read:
            raise RuntimeError("飞书 1254002")
        return self.cell

    def set_json(self, key, value):
        assert key == standby.KEY
        if self.fail_write:
            raise RuntimeError("飞书写失败")
        self.writes.append(value)
        self.cell = json.dumps(value)


class FakeProjects:
    def __init__(self, store, enabled=True):
        self.store = store
        self.enabled = enabled

    def _store(self):
        return self.store

    def why_disabled(self):
        return "还没配 FEISHU_REGISTRY"


def make(cell=None, *, keys=BOTH, environ=None, enabled=True, settings=None, **store_kw):
    store = FakeStore(cell, **store_kw)
    clock = [0.0]
    sb = panel.Standby(FakeProjects(store, enabled), settings or Settings(), keys,
                       environ=environ or {}, clock=lambda: clock[0], wall=lambda: 1795822000.0)
    return sb, store, clock


class TestStandbyState(unittest.TestCase):
    def test_default_is_off(self):
        sb, _, _ = make()
        self.assertFalse(sb.state().enabled)
        self.assertEqual(sb.channels().for_platform("xhs"), ["tikhub"])

    def test_set_persists_and_is_seen_at_once(self):
        sb, store, _ = make()
        state = sb.set(True)
        self.assertTrue(state.enabled)
        self.assertEqual(store.writes, [{"enabled": True, "at": 1795822000000}])
        self.assertTrue(sb.state().enabled)
        self.assertEqual(sb.channels().for_platform("xhs"), ["tikhub", "socialdatax"])

    def test_reads_are_cached_and_failures_retried_sooner(self):
        sb, store, clock = make('{"enabled": true}')
        sb.state(); sb.state()
        self.assertEqual(store.reads, 1)
        clock[0] += 301
        sb.state()
        self.assertEqual(store.reads, 2)

        sb, store, clock = make(fail_read=True)
        self.assertIn("1254002", sb.state().error)
        self.assertFalse(sb.state().enabled)
        clock[0] += 61
        store.fail_read, store.cell = False, '{"enabled": true}'
        self.assertTrue(sb.state().enabled)

    def test_env_pins_it(self):
        sb, store, _ = make(environ={standby.ENV: "on"})
        view = sb.view()
        self.assertTrue(view.state.enabled)
        self.assertFalse(view.writable)
        self.assertIn(standby.ENV, view.why_locked)
        with self.assertRaises(PermissionError):
            sb.set(False)
        self.assertEqual(store.reads + len(store.writes), 0)

    def test_a_bad_env_value_is_shown_not_guessed(self):
        sb, _, _ = make(environ={standby.ENV: "maybe"})
        view = sb.view()
        self.assertFalse(view.state.enabled)
        self.assertFalse(view.writable)
        self.assertIn("maybe", view.why_locked)

    def test_without_a_registry_it_cannot_be_switched(self):
        sb, _, _ = make(enabled=False)
        self.assertFalse(sb.view().writable)
        with self.assertRaises(PermissionError):
            sb.set(True)

    def test_nothing_to_switch_when_socialdatax_is_not_a_standby(self):
        settings = Settings()
        settings.channels = Channels(order={"xhs": ["socialdatax", "tikhub"],
                                            "douyin": ["tikhub"]})
        sb, _, _ = make(settings=settings)
        self.assertFalse(sb.view().relevant)
        with self.assertRaises(PermissionError):
            sb.set(True)

    def test_the_panel_need_not_hold_the_upstream_keys(self):
        """文档建议面板服务上不配上游 Key：开关照样要摆出来，只是说一句看不到。"""
        for keys in ({}, {"tikhub": "t"}):
            with self.subTest(keys=keys):
                sb, _, _ = make(keys=keys)
                view = sb.view()
                self.assertTrue(view.relevant)
                self.assertTrue(view.writable)
                self.assertFalse(view.key_seen)
                self.assertIn("看不到 cron 那边配没配", panel_view._standby_box(view))


class TestStandbyRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.config = panel.PanelConfig(password=PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.sb, cls.store, _ = make()
        cls.server = panel.build_server(cls.config, cache, standby=cls.sb,
                                        settings=Settings(), host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.store.writes.clear()
        self.store.cell = None
        self.store.fail_write = False
        self.sb._state = None

    def _call(self, body, headers=None, path="/api/standby", method="POST"):
        req = urllib.request.Request(f"http://localhost:{self.port}{path}",
                                     data=body, headers=headers or {}, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def _auth(self):
        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NR)
        req = urllib.request.Request(f"http://localhost:{self.port}/login",
                                     data=b"password=" + PASSWORD.encode())
        try:
            cookie = opener.open(req, timeout=5).headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie")
        token = cookie.split("=", 1)[1].split(";")[0]
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token),
                "Content-Type": "application/json"}

    def test_needs_a_session_and_a_csrf_header(self):
        self.assertEqual(self._call(b'{"enabled":true}')[0], 401)
        auth = self._auth()
        auth.pop(panel.CSRF_HEADER)
        self.assertEqual(self._call(b'{"enabled":true}', auth)[0], 403)
        self.assertEqual(self.store.writes, [])

    def test_turning_it_on_and_off(self):
        auth = self._auth()
        status, body = self._call(b'{"enabled":true}', auth)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["enabled"])
        status, body = self._call(b'{"enabled":false}', auth)
        self.assertEqual(status, 200, body)
        self.assertEqual([w["enabled"] for w in self.store.writes], [True, False])

    def test_only_a_real_boolean(self):
        auth = self._auth()
        for raw in (b'{"enabled":"true"}', b'{"enabled":1}', b'{}', b'[true]', b'nope'):
            with self.subTest(raw=raw):
                self.assertEqual(self._call(raw, auth)[0], 400)
        self.assertEqual(self.store.writes, [])

    def test_a_failed_write_says_so_and_changes_nothing(self):
        self.store.fail_write = True
        status, body = self._call(b'{"enabled":true}', self._auth())
        self.assertEqual(status, 502)
        self.assertIn("开关没改", json.loads(body)["error"])
        self.assertFalse(self.sb.state().enabled)

    def test_the_page_carries_the_switch(self):
        auth = self._auth()
        status, body = self._call(None, {"Cookie": auth["Cookie"]}, path="/", method="GET")
        self.assertEqual(status, 200)
        self.assertIn("id=standbyBox", body)
        self.assertIn("data-standby=on", body)
        self.assertIn("/api/standby", body)


def _view(enabled=False, *, writable=True):
    return standby.View(state=standby.State(enabled=enabled,
                                            source=standby.SOURCE_PANEL if enabled
                                            else standby.SOURCE_DEFAULT),
                        platforms=("xhs", "douyin"),
                        why_locked="" if writable else "钉在环境变量里了")


class TestCard(unittest.TestCase):
    def test_off_says_what_that_costs_and_offers_to_open(self):
        html = panel_view._standby_box(_view(False))
        self.assertIn("已关闭", html)
        self.assertIn("刷新失败", html)
        self.assertIn("data-standby=on", html)
        self.assertIn("class=primary", html)

    def test_on_says_when_and_offers_to_close(self):
        view = standby.View(state=standby.State(enabled=True, changed_at_ms=1795822000000,
                                                source=standby.SOURCE_PANEL),
                            platforms=("xhs",))
        html = panel_view._standby_box(view)
        self.assertIn("已打开", html)
        self.assertIn("小红书的请求先走 TikHub", html)
        self.assertIn("data-standby=off", html)

    def test_locked_shows_why_and_no_button(self):
        html = panel_view._standby_box(_view(True, writable=False))
        self.assertIn("钉在环境变量里了", html)
        self.assertNotIn("data-standby", html)

    def test_not_relevant_renders_nothing(self):
        self.assertEqual(panel_view._standby_box(standby.View(state=standby.State())), "")
        self.assertEqual(panel_view._standby_box(None), "")


def _run(counts, started_at=1795822000.0):
    return railway.Run(run_id=str(started_at), started_at=started_at, tables=[
        railway.TableRun(label="t", rows=sum(counts.values()), counts=counts)])


class TestFailingStreak(unittest.TestCase):
    def test_the_2026_11_28_shape(self):
        runs = [_run({"刷新失败": 5}, 300.0), _run({}, 200.0), _run({"刷新失败": 1}, 150.0),
                _run({"刷新失败": 4}, 100.0), _run({"正常": 7}, 50.0)]
        streak = channels.failing_streak(runs)
        self.assertEqual((streak.runs, streak.rows, streak.since), (3, 10, 100.0))

    def test_a_single_success_breaks_it(self):
        self.assertIsNone(channels.failing_streak(
            [_run({"刷新失败": 5}), _run({"刷新失败": 4, "正常": 1}), _run({"刷新失败": 9})]))

    def test_one_bad_link_queued_twice_is_not_an_outage(self):
        self.assertIsNone(channels.failing_streak([_run({"刷新失败": 1}), _run({"刷新失败": 1})]))

    def test_one_round_is_not_enough(self):
        self.assertIsNone(channels.failing_streak([_run({"刷新失败": 14})]))

    def test_dead_posts_are_answers_not_failures(self):
        self.assertIsNone(channels.failing_streak(
            [_run({"已失效": 5}), _run({"已失效": 5})]))

    def test_banner_offers_the_switch_when_it_is_off(self):
        streak = channels.FailingStreak(runs=3, rows=10, since=1795822000.0)
        html = panel_view._streak_note(streak, _view(False))
        self.assertIn("全部「刷新失败」", html)
        self.assertIn("data-standby=on", html)
        on = panel_view._streak_note(streak, _view(True))
        self.assertIn("已经开着", on)
        self.assertNotIn("data-standby", on)
        self.assertEqual(panel_view._streak_note(None, _view(False)), "")


class TestOutagesFollowTheSwitch(unittest.TestCase):
    """TikHub 没钱了：备用通道关着 = 巡查停了；开着 = 降到 SocialDataX。"""

    def balances(self):
        return [balance_mod.Balance(channel="tikhub", label="TikHub", amount=0.0, unit="USD",
                                    yuan=0.0, rate=7.2),
                balance_mod.Balance(channel="socialdatax", label="SocialDataX", amount=8000,
                                    unit="points", yuan=80.0)]

    def outages(self, enabled):
        sb, _, _ = make(json.dumps({"enabled": enabled}))
        settings = Settings()
        deps = panel._Deps(config=panel.PanelConfig(password=PASSWORD, secret=b"s" * 32),
                           cache=None, settings=settings, standby=sb)
        deps.config.api_keys = dict(BOTH)
        handler = panel.PanelHandler.__new__(panel.PanelHandler)
        handler.server = mock.Mock(deps=deps)
        return handler._outages(self.balances())

    def test_off_means_stopped_and_the_note_offers_the_switch(self):
        found = self.outages(False)
        self.assertTrue(found and all(o.stopped for o in found))
        html = panel_view._outage_note(found, _view(False))
        self.assertIn("备用通道关着", html)
        self.assertIn("data-standby=on", html)

    def test_on_means_it_falls_back(self):
        found = self.outages(True)
        self.assertTrue(found)
        self.assertFalse(any(o.stopped for o in found))
        self.assertEqual({o.fallback_label for o in found}, {"SocialDataX"})


if __name__ == "__main__":
    unittest.main()
