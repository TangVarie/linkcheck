"""运营帖子进度：完整统计、请求校验及日报边界的离线回归。"""

import copy
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from xhsearch import panel, summary
from xhsearch.config import Settings


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 28, 14, tzinfo=TZ)
TS = int(NOW.timestamp() * 1000)


def todo(record_id="r", **kwargs):
    return summary.TodoRow(record_id=record_id, project="项目甲", record_url="",
                           link_cell="", table_id="tblA", reasons=["有负面"],
                           checked_at_ms=TS, **kwargs)


def overview(*rows):
    return summary.Overview(projects=[summary.ProjectSnapshot(
        label="项目甲", app_token="baseA", table_id="tblA", todos=list(rows))])


class MemoryStore:
    def __init__(self):
        self.value = {}

    def get_json(self, key, default):
        return copy.deepcopy(self.value)

    def update_json(self, key, change, default):
        self.value = change(copy.deepcopy(self.value))
        return copy.deepcopy(self.value)


class TestWorkProgress(unittest.TestCase):
    def test_replacing_link_in_same_row_does_not_inherit_work_state(self):
        row = todo(post_key="xhs:new-post")
        for state in ("processing", "resolved"):
            marked = {"tblA/r": {"state": state, "reasons": ["有负面"],
                                  "seen": TS, "at": TS, "post_key": "xhs:old-post"}}
            result = summary.apply_resolutions(overview(row), marked, Settings())
            self.assertEqual(result.projects[0].todos[0].work_state, "pending")
            self.assertEqual(result.resolved, [])
            self.assertEqual(summary.trim_resolutions(overview(row), marked), {"tblA/r": None})

    def test_processing_survives_refresh_without_mutating_cache(self):
        source = overview(todo())
        marked = {"tblA/r": {"state": "processing", "reasons": ["有负面"],
                              "seen": TS - 60000, "at": TS - 30000}}
        result = summary.apply_resolutions(source, marked, Settings())
        self.assertEqual(result.projects[0].todos[0].work_state, "processing")
        self.assertEqual(result.projects[0].todos[0].working_at_ms, TS - 30000)
        self.assertEqual(source.projects[0].todos[0].work_state, "pending")
        self.assertEqual(result.resolved, [])

    def test_new_problem_reopens_processing_post(self):
        row = todo()
        row.reasons.append("刷新失败")
        marked = {"tblA/r": {"state": "processing", "reasons": ["有负面"],
                              "seen": TS, "at": TS}}
        result = summary.apply_resolutions(overview(row), marked, Settings())
        self.assertEqual(result.projects[0].todos[0].work_state, "pending")

    def test_counts_include_archived_and_adhoc_without_display_cap(self):
        source = overview(*(todo(str(i), archived=i == 204) for i in range(205)))
        source.projects[0].adhoc_todos.append(todo("adhoc", adhoc=True))
        marked = {
            "tblA/0": {"state": "processing", "reasons": ["有负面"], "at": TS},
            "tblA/1": {"reasons": ["有负面"], "seen": TS, "at": TS},
        }
        counts = summary.work_stats(source, marked, Settings())[0]
        self.assertEqual({key: counts[key] for key in ("pending", "processing", "resolved")},
                         {"pending": 204, "processing": 1, "resolved": 1})
        shown = summary.for_display(source, marked, Settings(), max_todos=2)
        self.assertEqual(len(shown.projects[0].todos), 2)
        self.assertEqual(len(source.projects[0].todos), 205)

    def test_legacy_resolution_remains_resolved(self):
        marked = {"tblA/r": {"reasons": ["有负面"], "seen": TS, "at": TS}}
        result = summary.apply_resolutions(overview(todo()), marked, Settings())
        self.assertEqual(result.projects[0].todos, [])
        self.assertEqual(result.resolved[0].work_state, "resolved")

    def test_serialization_preserves_information_needed_by_live_rows(self):
        row = todo(label="标题", persona="K12", post_url="https://example.com/a", archived=True)
        payload = panel._todo_json(row)
        for key in ("label", "persona", "post_url", "archived", "adhoc", "recent"):
            self.assertEqual(payload[key], getattr(row, key))
        project = overview(row).projects[0]
        self.assertEqual(panel._project_json(project)["table_id"], "tblA")


class TestResolveTransitions(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.projects = mock.Mock(spec=panel.Projects)
        self.projects.enabled = True
        self.projects._store.return_value = self.store
        self.resolutions = panel.Resolutions(self.projects, log=lambda *_: None,
                                             wall=lambda: NOW.timestamp())
        self.source = overview(todo())
        self.reports = mock.Mock()
        self.cache = mock.Mock()
        self.cache.view.return_value = self.source, "", NOW.timestamp()
        self.deps = SimpleNamespace(resolutions=self.resolutions, projects=self.projects,
                                    reports=self.reports, cache=self.cache, settings=Settings())
        self.handler = object.__new__(panel.PanelHandler)
        self.handler.server = SimpleNamespace(deps=self.deps)
        self.handler._send_json = lambda status, body: (status, body)
        self.handler.log_error = mock.Mock()

    def request(self, **changes):
        body = {"table_id": "tblA", "record_id": "r", "reasons": ["有负面"],
                "checked_at_ms": TS, **changes}
        self.handler._read_body = lambda: json.dumps(body).encode()
        return self.handler._resolve_action()

    def test_processing_and_pending_are_persisted_and_journaled(self):
        status, body = self.request(state="processing")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "processing")
        self.assertEqual(self.store.value["tblA/r"]["state"], "processing")
        event = self.reports.record_events.call_args.args[0][0]
        self.assertEqual((event["table_id"], event["record_id"], event["state"]),
                         ("tblA", "r", "processing"))
        self.assertEqual(event["at"], TS)
        self.assertEqual(body["work_stats"][0]["processing"], 1)
        status, body = self.request(state="pending")
        self.assertEqual(status, 200)
        self.assertEqual(self.store.value, {})
        event = self.reports.record_events.call_args.args[0][0]
        self.assertEqual(event["state"], "pending")
        self.assertGreater(event["at"], TS)

    def test_unknown_state_and_forged_record_never_write(self):
        for changes, expected in (({"state": "anything"}, 400),
                                  ({"record_id": "absent"}, 409),
                                  ({"table_id": "absent"}, 409)):
            with self.subTest(changes=changes):
                status, _ = self.request(**changes)
                self.assertEqual(status, expected)
        self.assertEqual(self.store.value, {})
        self.reports.record_events.assert_not_called()

    def test_stale_scan_or_forged_reason_returns_conflict(self):
        self.assertEqual(self.request(checked_at_ms=TS - 1)[0], 409)
        self.assertEqual(self.request(reasons=["风控中"])[0], 409)
        self.assertEqual(self.store.value, {})
        self.reports.record_events.assert_not_called()

    def test_history_failure_does_not_report_status_as_unsaved(self):
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        status, body = self.request(state="processing")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("留痕待同步", body["warning"])
        self.assertEqual(self.store.value["tblA/r"]["state"], "processing")
        self.assertEqual(self.store.value[panel.Resolutions.OUTBOX][0]["state"], "processing")

    def test_full_outbox_drops_the_oldest_instead_of_blocking_work(self):
        """留痕一直写不进去、待补写攒满了：运营照样能点；丢的那段记成留痕缺口。"""
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        backlog = [{"type": "status", "at": TS - 10_000 + i, "table_id": "tblA",
                    "record_id": "old", "state": "processing"} for i in range(1000)]
        self.store.value = {panel.Resolutions.OUTBOX: copy.deepcopy(backlog)}
        status, body = self.request(state="processing")
        self.assertEqual(status, 200)
        self.assertIn("已丢弃", body["warning"])
        self.assertEqual(self.store.value["tblA/r"]["state"], "processing")
        outbox = self.store.value[panel.Resolutions.OUTBOX]
        self.assertLessEqual(len(json.dumps(outbox, ensure_ascii=False)),
                             panel.Resolutions.OUTBOX_MAX_CHARS)
        self.assertEqual(outbox[-1]["record_id"], "r")
        self.assertEqual(outbox[0], backlog[1001 - len(outbox)])
        first, count, latest = self.store.value[panel.Resolutions.DROPPED]["tblA"]
        self.assertEqual((first, count), (TS - 10_000, 1001 - len(outbox)))
        self.assertEqual(latest, 0, "丢的都是被同一篇帖子后来的操作盖掉的，最新状态还在")

        # 恢复之后：剩下的补写进去；从最早丢的那一刻到补写这一刻写成缺口。
        recovered = mock.Mock()
        self.assertEqual(self.resolutions.flush_journal(recovered), "")
        sent = recovered.record_events.call_args.args[0]
        gaps = [e for e in sent if e["type"] == "gap"]
        self.assertEqual([(g["at"], g["end"], g["table_id"]) for g in gaps],
                         [(first, TS, "tblA")])
        self.assertEqual(len(sent), len(outbox) + 1)
        self.assertNotIn(panel.Resolutions.OUTBOX, self.store.value)
        self.assertNotIn(panel.Resolutions.DROPPED, self.store.value)

    def test_superseded_drops_go_first_and_the_gap_covers_until_recovery(self):
        """丢掉的被几天后的操作盖掉：中间那几期依赖丢掉的状态，一直标到补写为止。"""
        from xhsearch import daily_report as d
        day = 86_400_000
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        dropped = {"type": "status", "at": TS - 3 * day, "table_id": "tblA",
                   "record_id": "x", "state": "pending", "post_key": "xhs:x"}
        filler = [{"type": "status", "at": TS - 3 * day + 1 + i, "table_id": "tblA",
                   "record_id": f"f{i}", "state": "processing"} for i in range(300)]
        later = {**dropped, "at": TS - 1000, "state": "resolved"}
        self.store.value = {panel.Resolutions.OUTBOX: [*filler[:150], dropped,
                                                       *filler[150:], later]}
        self.assertEqual(self.request(state="processing")[0], 200)
        outbox = self.store.value[panel.Resolutions.OUTBOX]
        self.assertNotIn(dropped, outbox, "被盖掉的先丢，哪怕它不是最早的")
        self.assertIn(later, outbox)
        recovered = mock.Mock()
        self.resolutions.flush_journal(recovered)
        gaps = [e for e in recovered.record_events.call_args.args[0] if e["type"] == "gap"]
        coverage = {"type": "coverage", "at": TS - 10 * day, "table_id": "tblA"}
        start, end = d.window_for_date((NOW - timedelta(days=1)).date())
        middle = d.aggregate([coverage, *gaps], start, end, "tblA", "项目甲", as_of_ms=end)
        self.assertFalse(middle["window"]["coverage_complete"])

    def test_link_change_does_not_count_as_superseding_the_old_post(self):
        """同一行换了链接：新帖子的操作盖不掉旧帖子的状态。旧帖子的最新状态丢了，
        之后很久的日报都要标不完整，不能装作覆盖完整。"""
        from xhsearch import daily_report as d
        day = 86_400_000
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        old_post = {"type": "status", "at": TS - 2 * day, "table_id": "tblA",
                    "record_id": "x", "state": "pending", "post_key": "xhs:old"}
        new_post = {**old_post, "at": TS - 1000, "state": "resolved", "post_key": "xhs:new"}
        filler = [{"type": "status", "at": TS - 2 * day + 1 + i, "table_id": "tblA",
                   "record_id": f"f{i}", "state": "processing"} for i in range(300)]
        self.store.value = {panel.Resolutions.OUTBOX: [old_post, *filler, new_post]}
        self.assertEqual(self.request(state="processing")[0], 200)
        outbox = self.store.value[panel.Resolutions.OUTBOX]
        self.assertNotIn(old_post, outbox)
        self.assertIn(new_post, outbox)
        _, count, latest = self.store.value[panel.Resolutions.DROPPED]["tblA"]
        self.assertEqual(latest, count, "这些都不是被同一篇帖子盖掉的")
        recovered = mock.Mock()
        self.resolutions.flush_journal(recovered)
        gaps = [e for e in recovered.record_events.call_args.args[0] if e["type"] == "gap"]
        self.assertIn("可能不准", gaps[0]["reason"])
        coverage = {"type": "coverage", "at": TS - 10 * day, "table_id": "tblA"}
        start, end = d.window_for_date((NOW + timedelta(days=30)).date())
        later = d.aggregate([coverage, *gaps], start, end, "tblA", "项目甲", as_of_ms=end)
        self.assertFalse(later["window"]["coverage_complete"])
        start, end = d.window_for_date(
            (NOW + timedelta(days=panel.Resolutions.LOST_STATE_DAYS + 2)).date())
        normal = [{"type": "run_start", "at": start + 1, "table_id": "tblA", "run_id": "n",
                   "mode": "queue"},
                  {"type": "run_end", "at": start + 2, "table_id": "tblA", "run_id": "n",
                   "mode": "queue"}]
        after = d.aggregate([coverage, *gaps, *normal], start, end, "tblA", "项目甲",
                            as_of_ms=end)
        self.assertTrue(after["window"]["coverage_complete"])

    def test_processing_cleared_by_reconcile_is_journaled_back_to_pending(self):
        """10:00 标正在处理 → 11:00 负面没了、自动收掉 → 12:00 又出负面：面板是待处理，
        过了 16 点按留痕出的日报也得是待处理，不能还说有人在跟。"""
        from xhsearch import daily_report as d
        self.resolutions.journaling = True
        self.store.value = {"tblA/r": {"reasons": ["有负面"], "seen": TS - 3_600_000,
                                       "at": TS - 3_600_000, "state": "processing",
                                       "post_key": "xhs:r"}}
        self.assertEqual(self.resolutions.reconcile(overview()), 1)
        self.assertNotIn("tblA/r", self.store.value)
        queued = self.store.value[panel.Resolutions.OUTBOX]
        self.assertEqual([(e["record_id"], e["state"], e.get("auto")) for e in queued],
                         [("r", "pending", True)])
        start, end = d.window_for_date(NOW.date())
        events = [{"type": "coverage", "at": start - 86_400_000, "table_id": "tblA"},
                  {"type": "run_start", "at": start + 1, "table_id": "tblA", "run_id": "n",
                   "mode": "queue"},
                  {"type": "run_end", "at": start + 2, "table_id": "tblA", "run_id": "n",
                   "mode": "queue"},
                  {"type": "status", "at": TS - 3_600_000, "table_id": "tblA",
                   "record_id": "r", "state": "processing", "post_key": "xhs:r"},
                  *queued,
                  {"type": "scan", "at": TS - 1000, "table_id": "tblA", "record_id": "r",
                   "post_key": "xhs:r", "negative_checked": True,
                   "hits": [{"id": "c1", "content": "又来了"}]}]
        r = d.aggregate(events, start, end, "tblA", "项目甲", as_of_ms=end)
        self.assertEqual(r["post_states"], {"pending": 1, "processing": 0, "resolved": 0})
        self.assertEqual(r["operations"]["pending"], 0, "自动收掉的不是运营的操作")

    def test_reconcile_without_a_journal_queues_nothing(self):
        self.store.value = {"tblA/r": {"reasons": ["有负面"], "seen": TS, "at": TS,
                                       "state": "processing"}}
        self.resolutions.reconcile(overview())
        self.assertNotIn(panel.Resolutions.OUTBOX, self.store.value)

    def test_resolved_records_stay_under_the_feishu_cell_limit(self):
        """500 条带帖子键的记录一条两百来字，攒满就超过飞书单格 10 万字，
        之后每一次点击都写不进去。按字数收掉最早的，最新的留着。"""
        from xhsearch import panel_settings
        url_key = "xhs:url:" + "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.store.value = {f"tblAbcdefghijklm/rec{i:012d}": {
            "reasons": ["有负面", "风控中"], "seen": TS - 600_000 + i, "at": TS - 600_000 + i,
            "state": "processing", "post_key": url_key} for i in range(500)}
        self.assertGreater(len(json.dumps(self.store.value, ensure_ascii=False)),
                           panel_settings.CELL_MAX_CHARS)
        self.resolutions.mark("tblA", "r", ["有负面"], TS)
        text = json.dumps(self.store.value, ensure_ascii=False)
        self.assertLessEqual(len(text), panel.Resolutions.ENTRIES_MAX_CHARS)
        self.assertIn("tblA/r", self.store.value)
        self.assertIn("tblAbcdefghijklm/rec000000000499", self.store.value)
        self.assertNotIn("tblAbcdefghijklm/rec000000000000", self.store.value)

    def test_dropped_counter_survives_other_edits(self):
        self.store.value = {panel.Resolutions.DROPPED: {"tblA": [TS - 5, 3, 0]}}
        self.resolutions.mark("tblA", "r", ["有负面"], TS)
        self.resolutions.unmark("tblA", "r")
        self.assertEqual(self.store.value[panel.Resolutions.DROPPED], {"tblA": [TS - 5, 3, 0]})
        self.assertNotIn(panel.Resolutions.DROPPED, self.resolutions.snapshot())

    def test_empty_outbox_clears_a_stale_sync_warning(self):
        reports = SimpleNamespace(warning="处理状态暂时读取失败，待补写记录可能尚未同步；日报可能不完整。")
        self.assertEqual(self.resolutions.flush_journal(reports), "")
        self.assertEqual(reports.warning, "")

    def test_store_failure_does_not_journal_uncommitted_state(self):
        with mock.patch.object(self.store, "update_json", side_effect=RuntimeError("unavailable")):
            self.assertEqual(self.request(state="processing")[0], 500)
        self.reports.record_events.assert_not_called()

    def test_failed_history_is_replayed_after_process_restart(self):
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        self.assertEqual(self.request(state="processing")[0], 200)
        stored_event = copy.deepcopy(self.store.value[panel.Resolutions.OUTBOX][0])
        restarted = panel.Resolutions(self.projects, log=lambda *_: None,
                                      wall=lambda: NOW.timestamp())
        recovered_reports = mock.Mock()
        self.assertEqual(restarted.flush_journal(recovered_reports), "")
        recovered_reports.record_events.assert_called_once_with([stored_event])
        self.assertNotIn(panel.Resolutions.OUTBOX, self.store.value)
        self.assertEqual(restarted.snapshot()["tblA/r"]["state"], "processing")

    def test_restart_does_not_reorder_events_in_the_same_millisecond(self):
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        self.request(state="processing")
        first = self.store.value[panel.Resolutions.OUTBOX][0]
        restarted = panel.Resolutions(self.projects, log=lambda *_: None,
                                      wall=lambda: NOW.timestamp())
        restarted.snapshot()
        restarted.transition("tblA", "r", ["有负面"], TS, "resolved", self.reports)
        pending = self.store.value[panel.Resolutions.OUTBOX]
        self.assertEqual([event["state"] for event in pending], ["processing", "resolved"])
        self.assertGreater(pending[1]["at"], first["at"])

    def test_concurrent_transitions_keep_live_and_history_order_aligned(self):
        entered, release, started_second, done_second = (threading.Event() for _ in range(4))
        history = []

        def record(events):
            if events[0]["state"] == "processing":
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test timed out")
            history.extend(copy.deepcopy(events))

        self.reports.record_events.side_effect = record
        first = threading.Thread(target=self.resolutions.transition,
                                 args=("tblA", "r", ["有负面"], TS, "processing", self.reports))

        def finish():
            started_second.set()
            self.resolutions.transition("tblA", "r", ["有负面"], TS, "resolved", self.reports)
            done_second.set()

        second = threading.Thread(target=finish)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        self.assertTrue(started_second.wait(2))
        try:
            self.assertFalse(done_second.wait(0.05))
            self.assertEqual(self.resolutions.snapshot()["tblA/r"]["state"], "processing")
        finally:
            release.set()
            first.join(2)
            second.join(2)
        self.assertEqual([event["state"] for event in history], ["processing", "resolved"])
        self.assertGreater(history[1]["at"], history[0]["at"])
        self.assertNotIn("state", self.resolutions.snapshot()["tblA/r"])

    def test_authentication_and_csrf_precede_transition(self):
        self.handler.path = "/api/resolve"
        self.handler._resolve_action = mock.Mock()
        self.handler._authed = lambda: False
        self.assertEqual(self.handler.do_POST()[0], 401)
        self.handler._authed = lambda: True
        self.handler._csrf_ok = lambda: False
        self.assertEqual(self.handler.do_POST()[0], 403)
        self.handler._resolve_action.assert_not_called()


class TestDailyReportRequests(unittest.TestCase):
    def setUp(self):
        self.journal = mock.Mock()
        self.journal.read_window.return_value = []
        self.clock = [NOW.timestamp()]
        self.projects = mock.Mock()
        self.projects.settings = Settings()
        self.reports = panel.DailyReports(self.projects, journal=self.journal,
                                         wall=lambda: self.clock[0])

    def test_window_is_1600_beijing_and_preview_reads_only_until_now(self):
        self.reports.get(overview(todo()), date="2026-10-28")
        start = int(datetime(2026, 10, 27, 16, tzinfo=TZ).timestamp() * 1000)
        self.journal.read_window.assert_called_once_with(start, TS)

    def test_cached_preview_does_not_become_final_without_reading_cutoff(self):
        self.clock[0] = datetime(2026, 10, 28, 15, 59, 50, tzinfo=TZ).timestamp()
        first = self.reports.get(overview(todo()))
        self.clock[0] += 20
        cached = self.reports.get(overview(todo()))
        self.assertFalse(cached["reports"][0]["window"]["complete"])
        self.assertEqual(cached["as_of"], first["as_of"])
        self.clock[0] += 45
        final = self.reports.get(overview(todo()))
        self.assertTrue(final["reports"][0]["window"]["complete"])
        self.assertEqual(self.journal.read_window.call_count, 2)

    def test_cache_is_reused_and_invalidated_by_new_status(self):
        source = overview(todo())
        self.reports.get(source)
        self.reports.get(source)
        self.assertEqual(self.journal.read_window.call_count, 1)
        self.reports.record_status("tblA", "r", "processing")
        self.reports.get(source)
        self.assertEqual(self.journal.read_window.call_count, 2)
        self.journal.append.assert_called_once()
        event = self.journal.append.call_args.args[0][0]
        self.assertEqual(event["type"], "status")
        self.assertEqual(event["at"], TS)

    def test_sync_warning_reaches_the_copied_report_text(self):
        """运营复制出去的那段汇报里也得看到「可能不完整」，不能只在页面限制说明里。"""
        from xhsearch import daily_report as d
        start, _ = d.window_for_date("2026-10-27")
        self.journal.read_window.return_value = [
            {"type": "coverage", "at": start - 86_400_000, "table_id": "tblA"},
            {"type": "run_start", "at": start + 1, "table_id": "tblA", "run_id": "n", "mode": "queue"},
            {"type": "run_end", "at": start + 2, "table_id": "tblA", "run_id": "n", "mode": "queue"},
            {"type": "scan", "at": start + 3, "table_id": "tblA", "record_id": "r",
             "post_key": "xhs:r", "negative_checked": True, "hits": []}]
        self.reports.warning = "状态已保存，日报留痕待同步；系统会自动重试。"
        payload = self.reports.get(overview(todo()), date="2026-10-27")
        text = payload["reports"][0]["text"]
        self.assertTrue(text, payload["reports"][0].get("display_mode"))
        self.assertTrue(text.startswith("部分处理状态留痕待同步"), text[:60])

    def test_invalid_date_and_unknown_project_do_not_read_history(self):
        for values in ({"date": "bad"}, {"date": "2026-11-03"},
                       {"date": "2026-01-01"}, {"table_id": "absent"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.reports.get(overview(todo()), **values)
        self.journal.read_window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
