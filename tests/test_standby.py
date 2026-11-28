"""SocialDataX 备用通道的开关（xhsearch/standby.py）和 cron 那一头怎么读它。

开关的安全方向是**关**：关着最坏是这几轮刷新失败，开着是在花钱。所以凡是读不到、
存坏了、认不出的，一律按关；只有确确实实存着 `"enabled": true` 才开。
"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import cli
from xhsearch import providers, standby
from xhsearch.config import Channels

BOTH = {"tikhub": "t", "socialdatax": "s"}


class TestApply(unittest.TestCase):
    def test_off_drops_socialdatax_where_it_is_the_standby(self):
        channels = standby.apply(Channels(), BOTH, enabled=False)
        self.assertEqual(channels.for_platform("xhs"), ["tikhub"])
        self.assertEqual(channels.for_platform("douyin"), ["tikhub"])

    def test_on_keeps_the_configured_order(self):
        base = Channels()
        self.assertIs(standby.apply(base, BOTH, enabled=True), base)

    def test_the_shared_instance_is_not_mutated(self):
        """面板进程里 settings.channels 是共享的：关一次不能把它永久改掉。"""
        base = Channels()
        standby.apply(base, BOTH, enabled=False)
        self.assertEqual(base.for_platform("xhs"), ["tikhub", "socialdatax"])

    def test_socialdatax_only_deployment_keeps_running(self):
        """只配了 SocialDataX：它是主通道不是备胎，「关备胎」不能关掉整个巡查。"""
        keys = {"socialdatax": "s"}
        self.assertEqual(standby.standby_platforms(Channels(), keys), [])
        channels = standby.apply(Channels(), keys, enabled=False)
        self.assertEqual(providers.usable_order(channels, "xhs", keys), ["socialdatax"])

    def test_socialdatax_first_in_channel_order_is_left_alone(self):
        order = Channels(order={"xhs": ["socialdatax", "tikhub"], "douyin": ["tikhub", "socialdatax"]})
        self.assertEqual(standby.standby_platforms(order, BOTH), ["douyin"])
        channels = standby.apply(order, BOTH, enabled=False)
        self.assertEqual(channels.for_platform("xhs"), ["socialdatax", "tikhub"])
        self.assertEqual(channels.for_platform("douyin"), ["tikhub"])

    def test_no_socialdatax_key_means_nothing_to_switch(self):
        self.assertEqual(standby.standby_platforms(Channels(), {"tikhub": "t"}), [])


class TestStoredValue(unittest.TestCase):
    def test_nothing_stored_is_off(self):
        for text in (None, "", "  "):
            with self.subTest(text=text):
                state = standby.from_text(text)
                self.assertFalse(state.enabled)
                self.assertEqual(state.source, standby.SOURCE_DEFAULT)

    def test_round_trip(self):
        text = json.dumps(standby.stored_value(True, 1795822000000))
        state = standby.from_text(text)
        self.assertTrue(state.enabled)
        self.assertEqual(state.changed_at_ms, 1795822000000)
        self.assertEqual(state.source, standby.SOURCE_PANEL)

    def test_only_a_real_true_turns_it_on(self):
        """人在飞书里手改坏的值按关：开着是在花钱，不能靠猜。"""
        for raw in ('{"enabled": "true"}', '{"enabled": 1}', '[true]', 'true', "{bad json"):
            with self.subTest(raw=raw):
                self.assertFalse(standby.from_text(raw).enabled)


class TestResolve(unittest.TestCase):
    def test_env_pins_it_and_the_panel_is_not_read(self):
        for raw, expected in (("on", True), ("OFF", False), ("1", True), ("关", False)):
            with self.subTest(raw=raw):
                state = standby.resolve({standby.ENV: raw},
                                        lambda: self.fail("钉死了就不该去读面板"))
                self.assertEqual(state.enabled, expected)
                self.assertTrue(state.locked)

    def test_panel_or_unset_follows_the_panel(self):
        for env in ({}, {standby.ENV: "panel"}, {standby.ENV: ""}):
            with self.subTest(env=env):
                state = standby.resolve(env, lambda: '{"enabled": true, "at": 5}')
                self.assertTrue(state.enabled)
                self.assertFalse(state.locked)

    def test_unknown_env_value_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            standby.resolve({standby.ENV: "maybe"}, lambda: None)

    def test_a_failed_read_is_off_and_says_why(self):
        def boom():
            raise RuntimeError("飞书 1254002")
        state = standby.resolve({}, boom)
        self.assertFalse(state.enabled)
        self.assertIn("1254002", state.error)
        self.assertIn("读不到", state.describe())


class TestCronReadsTheSwitch(unittest.TestCase):
    """cron 每一轮开跑时读一次开关，按它定 settings.channels。"""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None)
                       for k in (standby.ENV, "FEISHU_REGISTRY", "CHANNEL_ORDER")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _apply(self, cell, keys=BOTH):
        settings = cli.build_settings()
        with mock.patch.object(cli, "_read_standby_cell", return_value=cell) as read:
            settings, state = cli._apply_standby(settings, keys)
        return settings, state, read

    def test_default_is_off(self):
        settings, state, _ = self._apply(None)
        self.assertFalse(state.enabled)
        self.assertEqual(providers.usable_order(settings.channels, "xhs", BOTH), ["tikhub"])

    def test_panel_on_keeps_the_standby(self):
        settings, state, _ = self._apply('{"enabled": true, "at": 1}')
        self.assertTrue(state.enabled)
        self.assertEqual(providers.usable_order(settings.channels, "xhs", BOTH),
                         ["tikhub", "socialdatax"])

    def test_nothing_to_switch_means_no_feishu_read(self):
        settings, state, read = self._apply(None, keys={"tikhub": "t"})
        self.assertIsNone(state)
        read.assert_not_called()

    def test_without_a_registry_it_is_off_and_says_how_to_pin_it(self):
        settings = cli.build_settings()
        settings, state = cli._apply_standby(settings, BOTH)
        self.assertFalse(state.enabled)
        self.assertIn(f"{standby.ENV}=on", state.describe())

    def test_a_bad_env_value_stops_before_any_request(self):
        os.environ[standby.ENV] = "sometimes"
        with self.assertRaises(SystemExit):
            cli._apply_standby(cli.build_settings(), BOTH)

    def test_the_state_is_printed_and_recorded_at_run_start(self):
        """「TikHub 出事那几轮为什么没降级」第一个要查的就是这一行。"""
        events = []
        state = standby.State(enabled=False, source=standby.SOURCE_DEFAULT)
        out = io.StringIO()
        with mock.patch.object(cli, "_tables", return_value=[]), \
             mock.patch.object(cli, "_event_sink",
                               return_value=(events.append, "run-1")), \
             mock.patch.object(cli, "_daily_journal", return_value=None), \
             redirect_stdout(out):
            cli._run_locked("sweep", None, None, cli.Settings(), {"tikhub": "k"},
                            standby_state=state)
        self.assertIn("SocialDataX 备用通道：关", out.getvalue())
        start = next(e for e in events if e.get("event") == "run_start")
        self.assertIs(start["standby"], False)


if __name__ == "__main__":
    unittest.main()
