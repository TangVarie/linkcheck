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
        """留痕一直写不进去、待补写攒满了：运营照样能点；丢最早的，记成留痕缺口。"""
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
        first, last, count, lost = self.store.value[panel.Resolutions.DROPPED]["tblA"]
        self.assertEqual((first, count), (TS - 10_000, 1001 - len(outbox)))
        # 缺口标到覆盖最后一条被丢事件的那次操作（同一条记录的下一条）。
        self.assertEqual(last, backlog[count]["at"])
        self.assertEqual(lost, [], "丢的都是被同一条记录后来的操作覆盖掉的，最新状态还在")

        # 恢复之后：剩下的补写进去，丢掉的那段作为缺口一起写，日报标覆盖不完整。
        recovered = mock.Mock()
        self.assertEqual(self.resolutions.flush_journal(recovered), "")
        sent = recovered.record_events.call_args.args[0]
        gaps = [e for e in sent if e["type"] == "gap"]
        self.assertEqual([(g["at"], g["end"], g["table_id"]) for g in gaps],
                         [(first, last, "tblA")])
        self.assertEqual(len(sent), len(outbox) + 1)
        self.assertNotIn(panel.Resolutions.OUTBOX, self.store.value)
        self.assertNotIn(panel.Resolutions.DROPPED, self.store.value)

    def test_dropped_latest_state_is_rebuilt_from_the_live_state_on_recovery(self):
        """丢掉的要是某条记录的**最新**状态，之后每一期都会接着报错它的状态：
        先丢被同一条记录后续操作覆盖掉的；真丢了最新状态的，补写时按此刻的显示
        状态补一条，缺口一直标到补上为止。"""
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        backlog = [{"type": "status", "at": TS - 10_000 + i, "table_id": "tblA",
                    "record_id": f"old{i}", "state": "processing", "post_key": f"xhs:old{i}"}
                   for i in range(300)]
        backlog[0]["state"] = "pending"                     # 退回待处理，显示里已没有记录
        backlog[150] = {**backlog[150], "record_id": "x", "post_key": "xhs:x"}
        backlog.append({**backlog[150], "at": TS - 1, "state": "resolved"})
        self.store.value = {
            panel.Resolutions.OUTBOX: copy.deepcopy(backlog),
            "tblA/old1": {"reasons": ["有负面"], "seen": TS, "at": TS - 9_999,
                          "state": "processing", "post_key": "xhs:old1"}}
        self.assertEqual(self.request(state="processing")[0], 200)
        outbox = self.store.value[panel.Resolutions.OUTBOX]
        kept = {(e["record_id"], e["state"]) for e in outbox}
        self.assertNotIn(("x", "processing"), kept, "被覆盖的先丢")
        self.assertIn(("x", "resolved"), kept)
        lost = self.store.value[panel.Resolutions.DROPPED]["tblA"][3]
        self.assertIn("old0", lost)
        self.assertNotIn("x", lost)

        recovered = mock.Mock()
        self.resolutions.flush_journal(recovered)
        sent = recovered.record_events.call_args.args[0]
        rebuilt = {e["record_id"]: e for e in sent
                   if e["type"] == "status" and e["record_id"] in lost}
        self.assertEqual(set(rebuilt), set(lost))
        # 显示里还挂着的：就是丢掉的那次点击，按原时刻还原（「已处理」之后又扫出
        # 负面要重新打开，比的就是这个时刻），算一次真实操作。
        self.assertEqual((rebuilt["old1"]["state"], rebuilt["old1"]["post_key"],
                          rebuilt["old1"]["at"]), ("processing", "xhs:old1", TS - 9_999))
        self.assertNotIn("resync", rebuilt["old1"])
        # 显示里没有了（退回待处理）：不知道什么时候退的，按补写时刻记、不算操作。
        latest = max(e["at"] for e in outbox)
        self.assertEqual(rebuilt["old0"]["state"], "pending")
        self.assertTrue(rebuilt["old0"]["resync"])
        self.assertGreater(rebuilt["old0"]["at"], latest)
        gaps = [e for e in sent if e["type"] == "gap"]
        self.assertEqual(max(g["end"] for g in gaps), rebuilt["old0"]["at"])
        self.assertNotIn(panel.Resolutions.DROPPED, self.store.value)

    def test_gap_for_a_superseded_drop_lasts_until_the_replacement(self):
        """丢掉的被几天后的操作覆盖：中间那几期都依赖丢掉的状态，都要标不完整。"""
        from xhsearch import daily_report as d
        day = 86_400_000
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        dropped = {"type": "status", "at": TS - 3 * day, "table_id": "tblA",
                   "record_id": "x", "state": "pending", "post_key": "xhs:x"}
        # 垫底的也都是同一条记录、一条盖一条：丢它们不丢任何最新状态，不会顺带把缺口拉长。
        filler = [{"type": "status", "at": TS - 3 * day + 1 + i, "table_id": "tblA",
                   "record_id": "f", "state": "processing"} for i in range(300)]
        later = {**dropped, "at": TS - 1000, "state": "resolved"}
        self.store.value = {panel.Resolutions.OUTBOX: [dropped, *filler, later]}
        self.assertEqual(self.request(state="processing")[0], 200)
        outbox = self.store.value[panel.Resolutions.OUTBOX]
        self.assertNotIn(dropped, outbox)
        self.assertIn(later, outbox)
        self.assertEqual(self.store.value[panel.Resolutions.DROPPED]["tblA"][3], [])
        recovered = mock.Mock()
        self.resolutions.flush_journal(recovered)
        gaps = [e for e in recovered.record_events.call_args.args[0] if e["type"] == "gap"]
        coverage = {"type": "coverage", "at": TS - 10 * day, "table_id": "tblA"}
        start, end = d.window_for_date((NOW - timedelta(days=1)).date())
        middle = d.aggregate([coverage, *gaps], start, end, "tblA", "项目甲", as_of_ms=end)
        self.assertFalse(middle["window"]["coverage_complete"])

    def test_rebuilt_resolution_keeps_its_time_so_a_later_negative_reopens_it(self):
        """丢掉的「已处理」之后又扫出负面：日报要和面板一样把它算回待处理。"""
        from xhsearch import daily_report as d
        self.reports.record_events.side_effect = RuntimeError("journal unavailable")
        backlog = [{"type": "status", "at": TS - 10_000 + i, "table_id": "tblA",
                    "record_id": f"old{i}", "state": "processing"} for i in range(300)]
        backlog[0] = {**backlog[0], "state": "resolved", "post_key": "xhs:p0"}
        self.store.value = {
            panel.Resolutions.OUTBOX: copy.deepcopy(backlog),
            "tblA/old0": {"reasons": ["有负面"], "seen": TS - 20_000, "at": TS - 10_000,
                          "post_key": "xhs:p0"}}
        self.assertEqual(self.request(state="processing")[0], 200)
        self.assertIn("old0", self.store.value[panel.Resolutions.DROPPED]["tblA"][3])
        recovered = mock.Mock()
        self.resolutions.flush_journal(recovered)
        sent = recovered.record_events.call_args.args[0]
        negative = {"type": "scan", "at": TS - 5_000, "table_id": "tblA", "record_id": "old0",
                    "post_key": "xhs:p0", "negative_checked": True,
                    "hits": [{"id": "c1", "content": "又出现了"}]}
        start, end = d.window_for_date(NOW.date())
        r = d.aggregate(sent + [negative], start, end, "tblA", "项目甲", as_of_ms=end)
        self.assertEqual(r["post_states"], {"pending": 1, "processing": 0, "resolved": 0})

    def test_dropped_counter_survives_other_edits(self):
        self.store.value = {panel.Resolutions.DROPPED: {"tblA": [TS - 5, TS - 1, 3]}}
        self.resolutions.mark("tblA", "r", ["有负面"], TS)
        self.resolutions.unmark("tblA", "r")
        self.assertEqual(self.store.value[panel.Resolutions.DROPPED], {"tblA": [TS - 5, TS - 1, 3]})
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

    def test_invalid_date_and_unknown_project_do_not_read_history(self):
        for values in ({"date": "bad"}, {"date": "2026-11-03"},
                       {"date": "2026-01-01"}, {"table_id": "absent"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.reports.get(overview(todo()), **values)
        self.journal.read_window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
