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
from xhsearch import daily_report as d


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
    def __init__(self):
        self.rows, self.tokens, self.searches = [], {}, []

    def batch_create(self, records, *, client_token):
        if client_token not in self.tokens:
            self.tokens[client_token] = [f"rec{len(self.rows) + i}" for i in range(len(records))]
            self.rows.extend(copy.deepcopy(records))
        return self.tokens[client_token]

    def search(self, fields, *, filter_spec=None, **kwargs):
        self.searches.append(filter_spec)
        return copy.deepcopy(self.rows)


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

    def test_missing_create_response_is_not_claimed_as_written(self):
        self.table.batch_create = lambda *a, **kw: []
        with self.assertRaisesRegex(ValueError, "未完整写入"):
            self.journal.append([scan()])


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
