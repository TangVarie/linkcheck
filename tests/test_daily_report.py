import copy
from datetime import datetime, timezone
import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import cli
from xhsearch import daily_report as d, feishu


START, END = d.window_for_date("2026-10-28")
HOUR = 3_600_000


def scan(at=START + 1000, *, post="xhs:one", record="r1", hits=None, table="tbl1", **kw):
    return {"type": "scan", "at": at, "table_id": table, "record_id": record,
            "post_key": post, "negative_checked": True,
            "hits": hits if hits is not None else [
                {"id": "c1", "content": "PRIVATE 原始正文", "keywords": ["踩雷"]}], **kw}


def status(state, at=START + 2000, record="r1"):
    return {"type": "status", "at": at, "table_id": "tbl1", "record_id": record, "state": state}


def ran(at, run_id="run-normal", table="tbl1", mode="queue"):
    """一轮正常开跑、收尾的巡检。正常的一天里总有几轮。"""
    return [{"type": "run_start", "at": at, "table_id": table, "run_id": run_id, "mode": mode},
            {"type": "run_end", "at": at + 60_000, "table_id": table, "run_id": run_id,
             "mode": mode}]


def report(events, **kwargs):
    return d.aggregate(events, START, END, "tbl1", "途鸽", as_of_ms=kwargs.pop("as_of", END), **kwargs)


class TestReport(unittest.TestCase):
    def test_china_16_window_and_half_open_boundaries(self):
        self.assertEqual(datetime.fromtimestamp(END / 1000, timezone.utc).isoformat(),
                         "2026-10-28T08:00:00+00:00")
        r = report([scan(START - 1), scan(START, hits=[]), scan(END, post="xhs:end")])
        self.assertEqual(r["scanned_posts"], 1)
        self.assertEqual(r["scan_count"], 1)
        self.assertEqual(r["negative_comments"], 0)

    def test_unique_posts_and_comments_differ_from_scans_and_rows(self):
        same = scan()
        r = report([same, same, scan(START + 3000, record="r2"),
                    scan(START + HOUR, post="xhs:two", record="r3"),
                    scan(table="other", post="xhs:foreign")])
        self.assertEqual(r["scan_count"], 3)
        self.assertEqual(r["scanned_posts"], 2)
        self.assertEqual(r["negative_posts"], 2)
        self.assertEqual(r["negative_comments"], 2)
        self.assertEqual(r["keywords"], [{"word": "踩雷", "count": 2}])
        self.assertNotIn("PRIVATE", json.dumps(r, ensure_ascii=False))
        self.assertEqual(r["post_states"]["pending"], 2)

    def test_missing_id_text_dedup_is_explicitly_estimated(self):
        first = scan(hits=[{"content": "过敏  了", "keywords": ["过敏"]}])
        second = scan(START + HOUR, hits=[{"content": "过敏\n了", "keywords": ["踩雷"]}])
        r = report([first, second])
        self.assertEqual(r["negative_comments"], 1)
        self.assertTrue(r["comment_count_estimated"])
        self.assertIn("估计", r["text"])
        self.assertEqual({x["word"] for x in r["keywords"]}, {"过敏", "踩雷"})

    def test_id_identifies_distinct_identical_text_comments(self):
        r = report([scan(hits=[{"id": "one", "content": "相同正文"},
                               {"id": "two", "content": "相同正文"}])])
        self.assertEqual(r["negative_comments"], 2)
        self.assertFalse(r["comment_count_estimated"])

    def test_post_state_counts_map_to_comments_without_claiming_individual_action(self):
        r = report([scan(hits=[{"id": "1"}, {"id": "2"}]), status("processing")])
        self.assertEqual(r["post_states"], {"pending": 0, "processing": 1, "resolved": 0})
        self.assertEqual(r["comment_states"], {"pending": 0, "processing": 2, "resolved": 0})
        self.assertIn("按整帖标记", r["text"])

    def test_processing_survives_new_scan_resolved_reopens(self):
        r = report([scan(), status("resolved"), scan(START + HOUR)])
        self.assertEqual(r["post_states"]["pending"], 1)
        r = report([scan(), status("processing"), scan(START + HOUR)])
        self.assertEqual(r["post_states"]["processing"], 1)
        r = report([scan(), status("processing"), status("pending", START + HOUR)])
        self.assertEqual(r["post_states"]["pending"], 1)

    def test_later_status_on_duplicate_record_applies_to_whole_post(self):
        r = report([scan(), scan(START + 1100, record="r2"),
                    status("resolved", START + 2000), status("processing", START + 3000, "r2")])
        self.assertEqual(r["post_states"]["processing"], 1)

    def test_replaced_link_in_same_row_does_not_inherit_previous_post_state(self):
        r = report([scan(post="xhs:new"),
                    {**status("processing"), "post_key": "xhs:old"}])
        self.assertEqual(r["post_states"]["pending"], 1)
        self.assertEqual(r["post_states"]["processing"], 0)

    def test_canonical_post_status_applies_across_duplicate_business_records(self):
        r = report([scan(record="r1"),
                    {**status("resolved", record="different-row"), "post_key": "xhs:one"}])
        self.assertEqual(r["post_states"]["resolved"], 1)

    def test_separate_post_states_remain_distinct_when_a_business_row_changes_link(self):
        r = report([scan(post="xhs:old"), scan(START + 1100, post="xhs:new"),
                    {**status("processing", START + 2000), "post_key": "xhs:old"},
                    {**status("resolved", START + 3000), "post_key": "xhs:new"}])
        self.assertEqual(r["post_states"], {"pending": 0, "processing": 1, "resolved": 1})

    def test_status_from_before_window_can_apply_but_future_status_does_not(self):
        r = report([status("processing", START - HOUR), scan(), status("resolved", END)])
        self.assertEqual(r["post_states"]["processing"], 1)

    def test_peak_is_first_detection_not_creation_or_repeat_scan(self):
        hits = [{"id": "1", "created_at_ms": START - 10 * HOUR}, {"id": "2"}]
        r = report([scan(START + HOUR, hits=hits), scan(START + 2 * HOUR, hits=hits),
                    scan(START + 3 * HOUR, post="xhs:b", hits=[{"id": "3"}])])
        self.assertEqual(r["peak"], {"start_ms": START + HOUR, "end_ms": START + 2 * HOUR,
                                     "count": 2})
        self.assertTrue(any("发现时间" in x for x in r["limitations"]))

    def test_complete_means_closed_window_separate_from_coverage(self):
        r = report([])
        self.assertTrue(r["window"]["complete"])
        self.assertFalse(r["window"]["coverage_complete"])
        self.assertIn("不可追溯", "".join(r["limitations"]))
        coverage = {"type": "coverage", "at": START, "table_id": "tbl1"}
        r = report([coverage], as_of=START + HOUR)
        self.assertFalse(r["window"]["complete"])
        self.assertTrue(r["window"]["coverage_complete"])
        self.assertIn("实时预览", r["text"])
        r = report([{**coverage, "at": START + HOUR}])
        self.assertFalse(r["window"]["coverage_complete"])

    def test_unclosed_run_crossing_window_start_is_incomplete(self):
        events = [{"type": "coverage", "at": START - 2 * HOUR, "table_id": "tbl1"},
                  {"type": "run_start", "at": START - HOUR, "table_id": "tbl1", "run_id": "run1"}]
        self.assertFalse(report(events)["window"]["coverage_complete"])
        events.append({"type": "run_end", "at": START + 1, "table_id": "tbl1", "run_id": "run1"})
        self.assertTrue(report(events + ran(START + HOUR))["window"]["coverage_complete"])

    def test_run_closing_just_after_the_cutoff_is_closed(self):
        """一轮 15:58 开跑、16:03 收尾：截止 16:00 的那一期它是闭合的，不是缺口。"""
        events = [{"type": "coverage", "at": START - 2 * HOUR, "table_id": "tbl1"},
                  {"type": "run_start", "at": END - 120_000, "table_id": "tbl1", "run_id": "run1"},
                  {"type": "run_end", "at": END + 180_000, "table_id": "tbl1", "run_id": "run1"}]
        r = report(events, as_of=END + HOUR)
        self.assertTrue(r["window"]["coverage_complete"])
        self.assertNotIn("未闭合", "".join(r["limitations"]))

    def test_a_run_still_in_progress_is_not_reported_unclosed(self):
        events = [{"type": "coverage", "at": START - 2 * HOUR, "table_id": "tbl1"},
                  {"type": "run_start", "at": START + HOUR, "table_id": "tbl1", "run_id": "run1"}]
        self.assertTrue(report(events, as_of=START + HOUR + 600_000)["window"]["coverage_complete"])
        late = report(events, as_of=START + HOUR + d.RUN_GRACE_MS)
        self.assertFalse(late["window"]["coverage_complete"])

    def test_gap_spanning_days_is_split_so_every_window_sees_it(self):
        first, last = START - 2 * 86_400_000 + HOUR, END - HOUR
        gaps = d.gap_events("tbl1", first, last, "积压")
        self.assertEqual([(g["at"], g["end"]) for g in gaps], [
            (first, START - 86_400_000 - 1), (START - 86_400_000, START - 1), (START, last)])
        coverage = {"type": "coverage", "at": START - 3 * 86_400_000, "table_id": "tbl1"}
        for day in ("2026-10-26", "2026-10-27", "2026-10-28"):
            start, end = d.window_for_date(day)
            with self.subTest(day=day):
                r = d.aggregate([coverage, *gaps], start, end, "tbl1", "途鸽", as_of_ms=end)
                self.assertFalse(r["window"]["coverage_complete"])
        start, end = d.window_for_date("2026-10-29")
        r = d.aggregate([coverage, *gaps, *ran(start + HOUR)], start, end, "tbl1", "途鸽",
                        as_of_ms=end)
        self.assertTrue(r["window"]["coverage_complete"])

    def test_status_clicked_on_the_short_link_key_applies_after_expansion(self):
        """面板点的时候「作品ID」还没写进表，状态带的是链接那个键。"""
        r = report([scan(post="xhs:real-id", post_alias="xhs:url:abc"),
                    {**status("resolved"), "post_key": "xhs:url:abc"}])
        self.assertEqual(r["post_states"]["resolved"], 1)
        self.assertEqual(r["post_states"]["pending"], 0)

    def test_a_day_with_no_journaled_run_is_not_called_complete(self):
        """留痕整段写不进去时什么都不会留下（缺口、批次也写在同一张表里）：
        不能报一张「0 篇、0 条」的完整日报。"""
        coverage = {"type": "coverage", "at": START - 5 * 86_400_000, "table_id": "tbl1"}
        r = report([coverage])
        self.assertFalse(r["window"]["coverage_complete"])
        self.assertIn("没有留痕到任何巡检批次", "".join(r["limitations"]))
        self.assertTrue(report([coverage, *ran(START + HOUR)])["window"]["coverage_complete"])
        # 当期刚开始几小时还没轮到一批，是正常的
        early = report([coverage], as_of=START + 2 * HOUR)
        self.assertTrue(early["window"]["coverage_complete"])

    def test_a_killed_run_before_the_window_does_not_taint_it(self):
        """前一天被 redeploy 杀掉的一轮：之后同一种巡检又正常跑过，说明它早就结束了。"""
        coverage = {"type": "coverage", "at": START - 5 * 86_400_000, "table_id": "tbl1"}
        killed = {"type": "run_start", "at": START - 6 * HOUR, "table_id": "tbl1",
                  "run_id": "killed", "mode": "queue"}
        later = ran(START - 6 * HOUR + 300_000, "next", mode="queue")
        normal = ran(START + HOUR, "today", mode="queue")
        self.assertTrue(report([coverage, killed, *later, *normal])["window"]["coverage_complete"])
        # 另一个服务（sweep）后来跑过证明不了：两个服务可能同时在跑
        other = ran(START - 6 * HOUR + 300_000, "sweep", mode="sweep")
        self.assertFalse(report([coverage, killed, *other, *normal])["window"]["coverage_complete"])
        # 被杀的那一轮就在本期里：本期确实不完整
        inside = {**killed, "at": START + 2 * HOUR}
        self.assertFalse(report([coverage, inside, *ran(START + 3 * HOUR, "after"),
                                 *normal])["window"]["coverage_complete"])

    def test_as_of_limits_scans_and_statuses_in_preview(self):
        r = report([scan(), status("resolved", START + HOUR),
                    scan(START + 2 * HOUR, post="xhs:future")], as_of=START + HOUR)
        self.assertEqual(r["scanned_posts"], 1)
        self.assertEqual(r["post_states"]["pending"], 1)
        self.assertEqual(r["window"]["as_of_ms"], START + HOUR)

    def test_operations_include_old_problems_without_new_scan_and_distinct_rows(self):
        events = [status("processing", START + 1, "old"),
                  status("processing", START + 2, "old"),
                  status("resolved", START + 3, "old"),
                  status("pending", START - 1, "outside"),
                  status("resolved", END, "outside")]
        r = report(events)
        self.assertEqual(r["operations"], {"pending": 0, "processing": 1, "resolved": 1})
        self.assertEqual(r["operations_unit"], "行")
        self.assertEqual(r["negative_posts"], 0)
        self.assertEqual(r["negative_comments"], 0)
        self.assertIn("含存量问题", r["text"])
        self.assertIn("同一行可有状态流转", r["text"])
        self.assertIn("本期检出预警的截至截止时状态", r["text"])

    def test_operations_can_dedup_canonical_post_key_when_available(self):
        events = [{**status("resolved", START + 1, "r1"), "post_key": "xhs:one"},
                  {**status("resolved", START + 2, "r2"), "post_key": "xhs:one"}]
        r = report(events)
        self.assertEqual(r["operations"]["resolved"], 1)
        self.assertEqual(r["operations_unit"], "篇")


class FakeTable:
    def __init__(self, limit=None):
        self.rows, self.tokens, self.searches = [], {}, []
        self.limit = limit

    def batch_create(self, records, *, client_token):
        if client_token not in self.tokens:
            if self.limit is not None and len(self.rows) + len(records) > self.limit:
                raise feishu.FeishuError(d.TABLE_FULL_CODE, "RecordExceedLimit")
            self.tokens[client_token] = [f"rec{self.made + i}" for i in range(len(records))]
            for rid, record in zip(self.tokens[client_token], records):
                self.rows.append({"record_id": rid, **copy.deepcopy(record)})
            self.made += len(records)
        return self.tokens[client_token]

    made = 0

    def search(self, fields, *, filter_spec=None, **kwargs):
        self.searches.append(filter_spec)
        return copy.deepcopy(self.rows)

    def batch_delete(self, record_ids):
        doomed = set(record_ids)
        self.rows = [r for r in self.rows if r.get("record_id") not in doomed]
        return len(doomed)


class FakeWorkspace:
    def __init__(self):
        self.tables, self.creates = [], 0

    def list_tables(self, token):
        return self.tables

    def create_table(self, token, name, fields):
        self.creates += 1
        self.tables.append({"name": name, "table_id": "tblJournal"})
        return "tblJournal"


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.workspace, self.table = FakeWorkspace(), FakeTable()
        self.journal = d.Journal(self.workspace, lambda *_: self.table, "base", log=lambda _: None)

    def test_read_empty_does_not_create_and_panel_cannot_race_create(self):
        self.assertEqual(self.journal.read_window(START, END, "tbl1"), [])
        self.assertEqual(self.workspace.creates, 0)
        panel = d.Journal(self.workspace, lambda *_: self.table, "base", allow_create=False)
        with self.assertRaisesRegex(ValueError, "首次巡检"):
            panel.append([status("processing")])
        self.assertEqual(self.workspace.creates, 0)

    def test_long_payload_chunks_roundtrip_and_idempotent_retry(self):
        event = scan(hits=[{"id": "id", "content": "长内容" * 12_000}])
        self.journal.append([event])
        rows = len(self.table.rows)
        self.assertGreater(rows, 1)
        self.assertTrue(all(len(r["fields"][d.COL_DATA]) <= d.CHUNK_CHARS for r in self.table.rows))
        self.journal.append([event])
        self.assertEqual(len(self.table.rows), rows)
        self.assertEqual(self.journal.read_window(START, END, "tbl1"), [event])

    def test_missing_or_corrupt_chunk_yields_explicit_gap_not_partial_success(self):
        self.journal.append([scan(hits=[{"content": "超长" * 20_000}])])
        self.table.rows.pop()
        events = self.journal.read_window(START, END, "tbl1")
        self.assertEqual([e["type"] for e in events], ["gap"])
        self.assertFalse(report(events)["window"]["coverage_complete"])
        self.assertIn("统计可能不全", report(events)["text"] if "统计可能不全" in report(events)["text"]
                      else "".join(report(events)["limitations"]))

    def test_flying_numeric_columns_accept_integral_float(self):
        self.journal.append([scan()])
        for row in self.table.rows:
            for name in (d.COL_START, d.COL_END, d.COL_PART, d.COL_PARTS):
                row["fields"][name] = float(row["fields"][name])
        self.assertEqual(len(self.journal.read_window(START, END, "tbl1")), 1)
        for invalid in (True, False, float("nan"), float("inf"), -1, 1.5):
            self.assertIsNone(d._ms(invalid))

    def test_coverage_start_is_first_actual_enable_time_and_append_only(self):
        self.journal.start("tbl1", START + HOUR)
        self.journal.start("tbl1", START + 2 * HOUR)
        events = self.journal.read_window(START, END, "tbl1")
        self.assertEqual([e["at"] for e in events], [START + HOUR])
        self.assertFalse(report(events)["window"]["coverage_complete"])

    def test_window_reads_old_status_but_excludes_other_project_and_future(self):
        self.journal.append([scan(), scan(END), scan(table="other"),
                             status("processing", START - HOUR)])
        events = self.journal.read_window(START, END, "tbl1")
        self.assertEqual(len(events), 2)
        self.assertEqual(report(events)["post_states"]["processing"], 1)

    def test_window_sees_a_run_that_closes_just_after_the_cutoff(self):
        self.journal.append([
            {"type": "coverage", "at": START - 2 * HOUR, "table_id": "tbl1"},
            {"type": "run_start", "at": END - 120_000, "table_id": "tbl1", "run_id": "run1"},
            {"type": "run_end", "at": END + 180_000, "table_id": "tbl1", "run_id": "run1"}])
        events = self.journal.read_window(START, END, "tbl1")
        self.assertTrue(report(events, as_of=END + HOUR)["window"]["coverage_complete"])

    def status_reads(self):
        kind = {"field_name": d.COL_KIND, "operator": "is", "value": ["status"]}
        return [spec for spec in self.table.searches if kind in spec["conditions"]]

    def test_status_history_is_read_incrementally_after_the_first_full_read(self):
        """几个月后每次刷新都整段重读几千条点击记录，还拿着读锁让别人一起等。"""
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        old = status("processing", START - 30 * 86_400_000)
        journal.append([old])
        journal.read_window(START, END, "tbl1")
        self.assertFalse(any(c["field_name"] == d.COL_END
                             for c in self.status_reads()[0]["conditions"]))
        self.table.searches.clear()
        events = journal.read_window(START, END, "tbl1")
        self.assertEqual(len(self.status_reads()), 1)
        self.assertTrue(any(c["field_name"] == d.COL_END
                            for c in self.status_reads()[0]["conditions"]))
        self.assertIn(old, events)

    def test_cached_chunk_gap_clears_once_the_chunk_reads_back_whole(self):
        """分片一时没读全（比如正写着），缓存里的缺口不能一直挂到 6 小时整段重读。"""
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        coverage = {"type": "coverage", "at": START - HOUR, "table_id": "tbl1"}
        long_status = {**status("processing", START + HOUR), "note": "长" * 20_000}
        journal.append([coverage, long_status, *ran(START + HOUR)])
        missing = next(i for i, row in enumerate(self.table.rows)
                       if row["fields"][d.COL_KIND] == "status")
        held = self.table.rows.pop(missing)
        first = journal.read_window(START, END, "tbl1")
        self.assertFalse(report(first)["window"]["coverage_complete"])
        self.table.rows.append(held)
        events = journal.read_window(START, END, "tbl1")
        self.assertNotIn("gap", [e["type"] for e in events])
        self.assertIn(long_status, events)
        self.assertTrue(report(events)["window"]["coverage_complete"])

    def test_recovered_chunk_spanning_the_incremental_cutoff_restores_all_its_events(self):
        """一批里有增量读截止线之前的旧事件：缺口作废时整批都要补回来。"""
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        coverage = {"type": "coverage", "at": START - 20 * 86_400_000, "table_id": "tbl1"}
        old = {**status("processing", START - 10 * 86_400_000, record="r1"),
               "post_key": "xhs:one", "note": "长" * 20_000}
        recent = {**status("processing", START + HOUR, record="r2"), "post_key": "xhs:two"}
        journal.append([coverage, old, recent, *ran(START + HOUR)])
        missing = next(i for i, row in enumerate(self.table.rows)
                       if row["fields"][d.COL_KIND] == "status")
        held = self.table.rows.pop(missing)
        journal.read_window(START, END, "tbl1")
        self.table.rows.append(held)
        events = journal.read_window(START, END, "tbl1")
        self.assertEqual({e.get("record_id") for e in events if e["type"] == "status"},
                         {"r1", "r2"})
        self.assertNotIn("gap", [e["type"] for e in events])
        r = report(events + [scan(START + 2 * HOUR)])
        self.assertTrue(r["window"]["coverage_complete"])
        self.assertEqual(r["post_states"]["processing"], 1)

    def test_a_broken_status_batch_before_the_window_still_flags_it(self):
        """状态是累积的：本期开始前那批坏了，本期各帖的状态照样不可信。"""
        coverage = {"type": "coverage", "at": START - 5 * 86_400_000, "table_id": "tbl1"}
        self.journal.append([coverage, *ran(START + HOUR),
                             {**status("processing", START - 2 * HOUR), "note": "长" * 20_000}])
        missing = next(i for i, row in enumerate(self.table.rows)
                       if row["fields"][d.COL_KIND] == "status")
        self.table.rows.pop(missing)
        events = self.journal.read_window(START, END, "tbl1")
        r = report(events + [scan()])
        self.assertFalse(r["window"]["coverage_complete"])

    def test_unrecovered_chunk_gap_keeps_its_earliest_time(self):
        """一直没读全的那批，增量读报出来的缺口时刻被截到截止线；缓存要留最早那个，
        不然更早那几期日报会悄悄变成「覆盖完整」。"""
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        coverage = {"type": "coverage", "at": START - 20 * 86_400_000, "table_id": "tbl1"}
        old = {**status("processing", START - 10 * 86_400_000), "note": "长" * 20_000}
        journal.append([coverage, old, status("resolved", START + HOUR, record="r2")])
        missing = next(i for i, row in enumerate(self.table.rows)
                       if row["fields"][d.COL_KIND] == "status")
        self.table.rows.pop(missing)
        start, end = d.window_for_date("2026-10-18")
        for attempt in ("full", "incremental"):
            with self.subTest(read=attempt):
                events = journal.read_window(start, end, "tbl1")
                r = d.aggregate(events, start, end, "tbl1", "途鸽", as_of_ms=end)
                self.assertFalse(r["window"]["coverage_complete"])

    def test_own_old_dated_write_is_seen_and_others_are_caught_by_the_full_reread(self):
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        other = d.Journal(self.workspace, lambda *_: self.table, "base", log=lambda _: None)
        journal.read_window(START, END, "tbl1")
        mine = status("resolved", START - 10 * 86_400_000, record="mine")
        journal.append([mine])                      # 面板补写好几天前的状态
        self.assertIn(mine, journal.read_window(START, END, "tbl1"))
        theirs = status("resolved", START - 10 * 86_400_000, record="theirs")
        other.append([theirs])
        clock[0] += d.HISTORY_FULL_SECONDS + 1
        self.assertIn(theirs, journal.read_window(START, END, "tbl1"))

    def test_a_write_does_not_wait_for_a_slow_read(self):
        """面板点「已处理」要写留痕；日报正在翻几页留痕时，这一下不能排在它后面等。"""
        self.journal.append([status("processing")])          # 建表
        gate, entered = threading.Event(), threading.Event()
        real = self.table.search

        def slow(*args, **kwargs):
            entered.set()
            gate.wait(5)
            return real(*args, **kwargs)

        self.table.search = slow
        reader = threading.Thread(target=lambda: self.journal.read_window(START, END, "tbl1"))
        reader.start()
        self.assertTrue(entered.wait(5))
        done = threading.Event()
        writer = threading.Thread(target=lambda: (self.journal.append(
            [status("resolved", START + 3000)]), done.set()))
        writer.start()
        try:
            self.assertTrue(done.wait(2), "写在等读")
        finally:
            gate.set()
            reader.join(5)
            writer.join(5)

    def test_a_write_during_a_full_reread_is_not_lost_from_the_cache(self):
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        journal.append([status("processing")])
        journal.read_window(START, END, "tbl1")             # 建缓存
        clock[0] += d.HISTORY_FULL_SECONDS + 1               # 下一次要整段重读
        mine = status("resolved", START - 10 * 86_400_000, record="mine")
        real = self.table.search

        def search_then_write(*args, filter_spec=None, **kwargs):
            rows = real(*args, filter_spec=filter_spec, **kwargs)   # 重读拿到的是写之前的那份
            kind = {"field_name": d.COL_KIND, "operator": "is", "value": ["status"]}
            if (kind in (filter_spec or {}).get("conditions", [])
                    and not getattr(search_then_write, "wrote", False)):
                search_then_write.wrote = True
                journal.append([mine])
            return rows

        self.table.search = search_then_write
        journal.read_window(START, END, "tbl1")
        self.table.search = lambda *a, **k: []              # 之后飞书那边一时读不到
        clock[0] += 60
        self.assertIn(mine, journal.read_window(START, END, "tbl1"))

    def test_a_write_during_the_very_first_history_read_is_not_lost(self):
        """缓存还没建（面板刚起来的第一次读），正赶上补写一条好几天前的状态。"""
        clock = [END / 1000]
        journal = d.Journal(self.workspace, lambda *_: self.table, "base",
                            log=lambda _: None, wall=lambda: clock[0])
        journal.append([{"type": "coverage", "at": START - 20 * 86_400_000, "table_id": "tbl1"}])
        mine = status("resolved", START - 10 * 86_400_000, record="mine")
        real = self.table.search

        def search_then_write(*args, filter_spec=None, **kwargs):
            rows = real(*args, filter_spec=filter_spec, **kwargs)
            kind = {"field_name": d.COL_KIND, "operator": "is", "value": ["status"]}
            if (kind in (filter_spec or {}).get("conditions", [])
                    and not getattr(search_then_write, "wrote", False)):
                search_then_write.wrote = True
                journal.append([mine])
            return rows

        self.table.search = search_then_write
        self.assertIn(mine, journal.read_window(START, END, "tbl1"))
        self.table.search = lambda *a, **k: []
        clock[0] += 60
        self.assertIn(mine, journal.read_window(START, END, "tbl1"))

    def test_prune_drops_old_scans_and_runs_but_never_coverage(self):
        """表有行数上限，满了什么都写不进去。超期的扫描、批次删掉；启用记录永远留着。"""
        day = 86_400_000
        now = END
        old = now - (d.RETENTION_DAYS + 10) * day
        self.journal.append([{"type": "coverage", "at": old - day, "table_id": "tbl1"}])
        self.journal.append([scan(old, record="gone")])
        self.journal.append(ran(old + 1000, "old-run"))
        self.journal.append([scan(START + HOUR, record="kept"), *ran(START + HOUR, "new-run")])
        before = len(self.table.rows)
        result = self.journal.prune(now)
        self.assertEqual(result["deleted"], 3)                # 旧扫描 1 + 旧批次 2
        self.assertEqual(len(self.table.rows), before - 3)
        kinds = [r["fields"][d.COL_KIND] for r in self.table.rows]
        self.assertIn("coverage", kinds)
        events = self.journal.read_window(START, END, "tbl1")
        self.assertEqual({e.get("record_id") for e in events if e["type"] == "scan"}, {"kept"})
        self.assertTrue(report(events)["window"]["coverage_complete"])
        self.assertEqual(self.journal.prune(now)["deleted"], 0)

    def test_prune_folds_old_statuses_to_the_latest_per_post(self):
        """状态是累积的，不能直接删：折叠成每篇最后一条（原时刻），之后各期照样认得。"""
        day = 86_400_000
        old = END - (d.RETENTION_DAYS + 10) * day
        states = ("processing", "resolved", "pending")
        for i in range(d.COMPACT_MIN_BATCHES + 5):
            self.journal.append([{**status(states[i % 3], old + i * 1000, record="r1"),
                                  "post_key": "xhs:one"}])
        self.journal.append([{**status("processing", old + 99_000, record="r2"),
                              "post_key": "xhs:two"}])
        last_one = {**status(states[(d.COMPACT_MIN_BATCHES + 4) % 3],
                             old + (d.COMPACT_MIN_BATCHES + 4) * 1000, record="r1"),
                    "post_key": "xhs:one"}
        coverage = {"type": "coverage", "at": old - day, "table_id": "tbl1"}
        self.journal.append([coverage])
        before = report(self.journal.read_window(START, END, "tbl1")
                        + [scan(START + HOUR), scan(START + HOUR, post="xhs:two", record="r2")]
                        + ran(START + HOUR))
        result = self.journal.prune(END)
        status_rows = [r for r in self.table.rows if r["fields"][d.COL_KIND] == "status"]
        self.assertLessEqual(len(status_rows), 1)
        self.assertGreater(result["folded"], 0)
        fresh = d.Journal(self.workspace, lambda *_: self.table, "base", log=lambda _: None)
        events = fresh.read_window(START, END, "tbl1")
        statuses = [e for e in events if e["type"] == "status"]
        self.assertIn(last_one, statuses)
        self.assertEqual(len(statuses), 2)
        after = report(events + [scan(START + HOUR), scan(START + HOUR, post="xhs:two", record="r2")]
                       + ran(START + HOUR))
        self.assertEqual(after["post_states"], before["post_states"])
        self.assertEqual(self.journal.prune(END)["deleted"], 0, "折叠过的不再反复重写")

    def test_prune_leaves_a_broken_old_status_batch_alone(self):
        day = 86_400_000
        old = END - (d.RETENTION_DAYS + 10) * day
        for i in range(d.COMPACT_MIN_BATCHES + 1):
            self.journal.append([{**status("processing", old + i, record=f"r{i}"),
                                  "note": "长" * (20_000 if i == 0 else 1)}])
        broken = [r for r in self.table.rows if r["fields"][d.COL_PARTS] > 1]
        self.table.rows.remove(broken[0])
        self.journal.prune(END)
        left = {r["record_id"] for r in self.table.rows}
        self.assertTrue(all(r["record_id"] in left for r in broken[1:]),
                        "缺片的那批读不全，不能当成折叠过了删掉")

    def test_missing_create_response_is_not_claimed_as_written(self):
        self.table.batch_create = lambda *a, **kw: []
        with self.assertRaisesRegex(ValueError, "未完整写入"):
            self.journal.append([scan()])


class FakeBase:
    """注册表那个 base：好几张留痕表，每张有行数上限，能建表、删表。"""

    def __init__(self, limit=None):
        self.limit, self.names, self.tables, self.made, self.dropped = limit, {}, {}, 0, []

    def list_tables(self, token):
        return [{"table_id": tid, "name": name} for tid, name in self.names.items()]

    def create_table(self, token, name, fields):
        self.made += 1
        tid = f"tbl{self.made}"
        self.names[tid], self.tables[tid] = name, FakeTable(self.limit)
        return tid

    def delete_table(self, token, table_id):
        if table_id not in self.names:
            raise feishu.FeishuError(1254041, "TableIdNotFound")
        del self.names[table_id]
        self.dropped.append(table_id)

    def bitable(self, token, table_id):
        if table_id not in self.names:
            raise feishu.FeishuError(1254041, "TableIdNotFound")
        return self.tables[table_id]

    def journal(self, **kwargs):
        kwargs.setdefault("log", lambda _: None)
        return d.Journal(self, self.bitable, "base", **kwargs)


COVERAGE = {"type": "coverage", "at": START - 86_400_000, "table_id": "tbl1"}
DAY = 86_400_000


class TestJournalRotation(unittest.TestCase):
    """飞书单表有行数上限：写满了谁撞上谁开下一张，不用管理员去扩容。"""

    def setUp(self):
        self.base = FakeBase(limit=5)
        self.clock = [END / 1000]                     # 北京时间 2026-10-28 16:00
        self.logs = []
        self.cron = self.journal(log=self.logs.append)

    def journal(self, **kwargs):
        return self.base.journal(wall=lambda: self.clock[0], **kwargs)

    def scans(self, journal, n, offset=0):
        for i in range(offset, offset + n):
            journal.append([scan(START + HOUR + i * 1000, record=f"r{i}")])

    def test_a_full_table_rolls_over_to_a_dated_one_and_reads_see_both(self):
        self.cron.append([COVERAGE])
        self.scans(self.cron, 6)                      # 第一张 5 行就满
        self.cron.append(ran(START + 2 * HOUR))
        self.assertEqual(sorted(self.base.names.values()), ["巡检留痕", "巡检留痕 2026-10-28"])
        self.assertTrue(any("已新开「巡检留痕 2026-10-28」" in line for line in self.logs))
        events = self.journal().read_window(START, END, "tbl1")
        self.assertEqual({e["record_id"] for e in events if e["type"] == "scan"},
                         {f"r{i}" for i in range(6)})
        self.assertTrue(report(events)["window"]["coverage_complete"])

    def test_the_panel_follows_a_rollover_and_opens_the_next_table_itself(self):
        """面板手里的表单是旧的也没关系；队列空着的时候它自己撞满，自己开下一张。"""
        panel = self.journal(allow_create=False)
        self.cron.append([COVERAGE])
        panel.read_window(START, END, "tbl1")         # 面板记下的表单里只有第一张
        self.scans(self.cron, 6)                      # cron 写满、开了第二张
        panel.append([status("resolved", START + HOUR, record="r0")])
        self.assertEqual(len(self.base.names), 2, "跟到 cron 开好的那张，不再多开")
        for i in range(1, 4):                         # 第二张也写满
            panel.append([status("processing", START + HOUR + i, record=f"r{i}")])
        self.assertIn("巡检留痕 2026-10-28 2", self.base.names.values())
        self.clock[0] += d.TABLES_TTL_SECONDS + 1     # cron 那边过一阵也认得新表
        statuses = [e for e in self.cron.read_window(START, END, "tbl1") if e["type"] == "status"]
        self.assertEqual({e["record_id"] for e in statuses}, {"r0", "r1", "r2", "r3"})

    def test_two_writers_rolling_over_at_once_do_not_stop_the_report(self):
        """两个进程同时撞满、各开了一张同名的：两张都读，不能让日报整个停掉。"""
        self.base.limit = None
        self.cron.append([COVERAGE])
        for record in ("a", "b"):
            self.base.create_table("base", "巡检留痕 2026-10-28", d.FIELDS)
            self.journal().append([scan(START + HOUR, record=record), *ran(START + HOUR)])
        events = self.journal().read_window(START, END, "tbl1")
        self.assertEqual({e["record_id"] for e in events if e["type"] == "scan"}, {"a", "b"})
        self.assertTrue(report(events)["window"]["coverage_complete"])

    def test_losing_the_race_to_open_the_next_table_uses_the_winners(self):
        self.cron.append([COVERAGE])
        self.scans(self.cron, 4)                      # 第一张满了
        real_create = self.base.create_table

        def raced(token, name, fields):
            real_create(token, name, fields)          # 另一个进程抢先建成了这张
            raise feishu.FeishuError(1254013, "TableNameDuplicated")
        self.base.create_table = raced
        self.scans(self.cron, 1, offset=4)
        self.assertEqual(len(self.base.names), 2)
        self.assertEqual(sum(len(t.rows) for t in self.base.tables.values()), 6)

    def test_window_reads_only_open_the_tables_that_can_hold_it(self):
        july, sept = d._table_start("巡检留痕 2026-07-01"), d._table_start("巡检留痕 2026-09-01")
        tables = [(0, "A"), (july, "B"), (sept, "C")]
        self.assertEqual(d._tables_for(tables, "scan", START, END), ["C"])
        self.assertEqual(d._tables_for(tables, "run_start", july + DAY, july + 2 * DAY), ["A", "B"])
        self.assertEqual(d._tables_for(tables, "status", None, END), ["A", "B", "C"])
        self.assertEqual(d._tables_for(tables, "gap", START, END), ["A", "B", "C"],
                         "面板补写的缺口带着旧时刻，会落在新表里")
        self.assertGreater(d._table_start("巡检留痕 2026-10-28 2"), d._table_start("巡检留痕 2026-10-28"))
        for other in ("巡检留痕 备份", "巡检留痕 2026-13-40", "被监控的表"):
            self.assertIsNone(d._table_start(other))

    def old_table_then_new(self, successor):
        """第一张里攒着 110 天前的东西，然后换到 `successor` 那张写最近的。"""
        self.base.limit = None
        old = END - (d.RETENTION_DAYS + 10) * DAY
        self.cron.append([{**COVERAGE, "at": old - DAY}])
        self.cron.append([scan(old, record="gone"), *ran(old + 1000, "old-run")])
        self.cron.append([{**status("processing", old + 2000, record="r1"), "post_key": "xhs:one"}])
        self.cron.append([{**status("resolved", old + 3000, record="r2"), "post_key": "xhs:two"}])
        self.base.create_table("base", successor, d.FIELDS)
        journal = self.journal(log=self.logs.append)
        journal.append([scan(START + HOUR, record="r1", post="xhs:one"),
                        scan(START + HOUR, record="r2", post="xhs:two"), *ran(START + HOUR)])
        return journal

    def test_prune_moves_what_is_left_and_drops_the_emptied_old_table(self):
        journal = self.old_table_then_new("巡检留痕 2026-09-01")
        before = report(self.journal().read_window(START, END, "tbl1"))
        result = journal.prune(END)
        self.assertEqual((result["deleted"], result["dropped_tables"]), (3, 1))
        self.assertEqual(list(self.base.names.values()), ["巡检留痕 2026-09-01"])
        self.assertTrue(any("旧表清空了 1 张" in line for line in self.logs))
        after = report(self.journal().read_window(START, END, "tbl1"))
        self.assertTrue(after["window"]["coverage_complete"])
        self.assertEqual(after["post_states"], before["post_states"])
        self.assertEqual(after["window"], before["window"])
        again = journal.prune(END)
        self.assertEqual((again["deleted"], again["dropped_tables"]), (0, 0))

    def test_an_old_table_stays_while_its_successor_is_new_or_a_batch_is_broken(self):
        journal = self.old_table_then_new("巡检留痕 2026-10-28")
        self.assertEqual(journal.prune(END)["dropped_tables"], 0,
                         "接班那张才开一天：别的进程手里的表单可能还没刷新")
        self.assertIn("巡检留痕", self.base.names.values())

        self.setUp()
        journal = self.old_table_then_new("巡检留痕 2026-09-01")
        first = self.base.tables["tbl1"]
        broken = [r for r in first.rows if r["fields"][d.COL_KIND] == "status"][0]
        broken["fields"][d.COL_PARTS] = 2             # 这批读不全
        self.assertEqual(journal.prune(END)["dropped_tables"], 0)
        self.assertIn("巡检留痕", self.base.names.values())

    def test_a_table_deleted_elsewhere_is_noticed_on_the_next_read_and_write(self):
        self.base.limit = None
        self.cron.append([COVERAGE])
        reader, writer = self.journal(), self.journal()
        writer.read_window(START, END, "tbl1")        # 记下的在写的那张还是第一张
        self.base.create_table("base", "巡检留痕 2026-09-01", d.FIELDS)
        reader.read_window(START, END, "tbl1")        # 记下了两张
        self.base.tables["tbl2"].rows = copy.deepcopy(self.base.tables["tbl1"].rows)
        self.base.delete_table("base", "tbl1")        # 清理把第一张搬空删了
        writer.append([scan(START + HOUR), *ran(START + HOUR)])
        self.assertEqual(len(self.base.tables["tbl2"].rows), 4)
        events = reader.read_window(START, END, "tbl1")
        self.assertTrue(report(events)["window"]["coverage_complete"])


class TestCLIJournal(unittest.TestCase):
    def test_empty_queue_run_never_starts_or_closes_a_journal_batch(self):
        journal = mock.Mock()
        with mock.patch.object(cli, "_tables", return_value=[("甲", SimpleNamespace(table_id="tbl1"))]), \
                mock.patch.object(cli, "_daily_journal", return_value=journal), \
                mock.patch.object(cli, "_install_stop_handlers"), \
                mock.patch.object(cli, "_event_sink", return_value=(None, "")), \
                mock.patch.object(cli, "_refresh_table", return_value=(0, set(), 0, 0., None)), \
                redirect_stdout(io.StringIO()):
            code = cli._run_locked("queue", None, None, cli.Settings(), {"tikhub": "fake"})
        self.assertEqual(code, 0)
        journal.start.assert_not_called()
        journal.append.assert_not_called()

    def test_journal_failure_returns_nonzero_but_keeps_paid_result_writeback(self):
        journal = mock.Mock()
        journal.append.side_effect = RuntimeError("offline")
        completed = cli.runner.RunReport(outcomes=[cli.runner.Outcome(
            "r1", cli.runner.STATUS_OK, scan_event=scan())])

        def refreshed(*args, **kwargs):
            kwargs["before_scan"]()
            return 0, {"r1"}, 1, .1, (completed, {})

        with mock.patch.object(cli, "_tables", return_value=[("甲", SimpleNamespace(table_id="tbl1"))]), \
                mock.patch.object(cli, "_daily_journal", return_value=journal), \
                mock.patch.object(cli, "_install_stop_handlers"), \
                mock.patch.object(cli, "_event_sink", return_value=(None, "")), \
                mock.patch.object(cli, "_refresh_table", side_effect=refreshed), \
                mock.patch.object(cli, "_write_back_table", return_value=0) as writeback, \
                redirect_stdout(io.StringIO()):
            code = cli._run_locked("sweep", None, None, cli.Settings(), {"tikhub": "fake"})
        self.assertEqual(code, 1)
        writeback.assert_called_once()

    def test_paid_results_are_written_back_before_journaling_and_runs_carry_mode(self):
        """飞书慢的时候留痕要重试好几轮：排在写回前面，就是拿花过钱的结果去等它。"""
        order = []
        journal = mock.Mock()
        journal.append.side_effect = lambda events: order.append(events[0]["type"])
        completed = cli.runner.RunReport(outcomes=[cli.runner.Outcome(
            "r1", cli.runner.STATUS_OK, scan_event=scan())])

        def refreshed(*args, **kwargs):
            kwargs["before_scan"]()
            return 0, {"r1"}, 1, .1, (completed, {})

        with mock.patch.object(cli, "_tables", return_value=[("甲", SimpleNamespace(table_id="tbl1"))]), \
                mock.patch.object(cli, "_daily_journal", return_value=journal), \
                mock.patch.object(cli, "_install_stop_handlers"), \
                mock.patch.object(cli, "_event_sink", return_value=(None, "")), \
                mock.patch.object(cli, "_refresh_table", side_effect=refreshed), \
                mock.patch.object(cli, "_write_back_table",
                                  side_effect=lambda *a, **k: order.append("writeback") or 0), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli._run_locked("sweep", None, None, cli.Settings(),
                                             {"tikhub": "fake"}), 0)
        self.assertEqual(order, ["run_start", "writeback", "scan", "run_end"])
        modes = {call.args[0][0]["type"]: call.args[0][0].get("mode")
                 for call in journal.append.call_args_list}
        self.assertEqual((modes["run_start"], modes["run_end"]), ("sweep", "sweep"))

    def test_sweep_prunes_the_journal_and_queue_does_not(self):
        for mode, expected in (("sweep", 1), ("queue", 0)):
            journal = mock.Mock()
            journal.prune.return_value = {"deleted": 0, "folded": 0, "dropped_tables": 0}
            with self.subTest(mode=mode), \
                    mock.patch.object(cli, "_tables", return_value=[("甲", SimpleNamespace(table_id="tbl1"))]), \
                    mock.patch.object(cli, "_daily_journal", return_value=journal), \
                    mock.patch.object(cli, "_install_stop_handlers"), \
                    mock.patch.object(cli, "_event_sink", return_value=(None, "")), \
                    mock.patch.object(cli, "_refresh_table", return_value=(0, set(), 0, 0., None)), \
                    redirect_stdout(io.StringIO()):
                cli._run_locked(mode, None, None, cli.Settings(), {"tikhub": "fake"})
            self.assertEqual(journal.prune.call_count, expected)

    def test_upkeep_failure_is_only_a_warning(self):
        journal = mock.Mock()
        journal.prune.side_effect = RuntimeError("feishu down")
        out = io.StringIO()
        with redirect_stdout(out):
            cli._journal_upkeep(journal)
        self.assertIn("清理没做成", out.getvalue())

    def test_journal_tables_share_one_access_token(self):
        """每次留痕都新建 Bitable 会各取一次 token，一轮一半的飞书调用都在取 token。"""
        env = {"FEISHU_REGISTRY": "https://x.feishu.cn/base/appReg?table=tblReg",
               "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s"}
        with mock.patch.dict("os.environ", env, clear=True), redirect_stdout(io.StringIO()):
            journal = cli._daily_journal()
        self.assertEqual(journal._bitable, journal.workspace.table)

    def test_no_registry_does_not_invent_business_table_journal(self):
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()), \
                mock.patch.object(cli.feishu, "Workspace") as workspace:
            self.assertIsNone(cli._daily_journal())
            workspace.assert_not_called()

    def test_failed_append_leaves_run_unclosed_and_reports_failure(self):
        journal = mock.Mock()
        journal.append.side_effect = RuntimeError("offline")
        output = io.StringIO()
        with redirect_stdout(output):
            ok = cli._journal_report(journal, SimpleNamespace(table_id="tbl1"),
                                     SimpleNamespace(outcomes=[SimpleNamespace(scan_event=scan())]), "run")
        self.assertFalse(ok)
        self.assertEqual(journal.append.call_count, 1)
        self.assertIn("业务结果继续写回", output.getvalue())

    def test_success_saves_scan_before_closing_run_and_failed_rows_are_not_scans(self):
        journal = mock.Mock()
        with redirect_stdout(io.StringIO()):
            ok = cli._journal_report(journal, SimpleNamespace(table_id="tbl1"),
                                     SimpleNamespace(outcomes=[SimpleNamespace(scan_event=scan()),
                                                               SimpleNamespace(scan_event=None)]), "run")
        self.assertTrue(ok)
        self.assertEqual(journal.append.call_args_list[0].args[0][0]["type"], "scan")
        self.assertEqual(journal.append.call_args_list[1].args[0][0]["type"], "run_end")


if __name__ == "__main__":
    unittest.main()
