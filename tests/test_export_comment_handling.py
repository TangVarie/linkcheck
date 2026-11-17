"""tools/export_comment_handling.py：评论区处置底稿的导出。

这份东西是拿去给客户验收「24 小时内处置」的，算错的代价是对外说错话：
把撤销过的「已处理」当成处置、把换了链接的旧帖状态算到新帖上、把攒负面的表
「处理后不再留痕」当成「评论被删了」，都会让台账比事实好看。
"""

import contextlib
import csv
import io
import pathlib
import tempfile
import unittest
from datetime import datetime

from tools import export_comment_handling as ex
from xhsearch import daily_report
from xhsearch.config import Settings

BJ = daily_report.SHANGHAI
H = ex.HOUR_MS


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=BJ).timestamp() * 1000)


def scan(when, record="rec1", post="xhs:abc", hits=(), checked=True, **extra):
    return {"type": "scan", "at": when, "table_id": "tbl1", "record_id": record,
            "post_key": post, "negative_checked": checked,
            "hits": [{"id": cid, "content": text, "created_at_ms": created,
                      "keywords": [word]} for cid, text, word, created in hits], **extra}


def status(when, state, record="rec1", post="xhs:abc", **extra):
    event = {"type": "status", "at": when, "table_id": "tbl1", "record_id": record,
             "state": state, **extra}
    if post:
        event["post_key"] = post
    return event


C1 = ("c1", "邦乐健 比这个好用多了", "邦乐健", at(1, 8))
C2 = ("c2", "用了过敏，智商税", "过敏", None)


class TestHandlingTimes(unittest.TestCase):

    def test_first_resolved_after_discovery(self):
        events = [(at(1, 9), "resolved"), (at(1, 12), "processing"), (at(1, 14), "resolved")]
        self.assertEqual(ex.handling_times(events, at(1, 10)), (at(1, 12), at(1, 14)),
                         "发现之前点的「已处理」管不到这条评论")

    def test_undo_does_not_count(self):
        events = [(at(1, 11), "resolved"), (at(1, 11, 5), "pending"),
                  (at(2, 15), "resolved")]
        self.assertEqual(ex.handling_times(events, at(1, 10))[1], at(2, 15),
                         "点了又撤销的那一下不能算处置")

    def test_moved_back_to_processing_is_an_undo(self):
        events = [(at(1, 11), "resolved"), (at(1, 12), "processing")]
        self.assertEqual(ex.handling_times(events, at(1, 10)), (at(1, 12), None))


class TestSla(unittest.TestCase):

    def test_labels(self):
        self.assertEqual(ex.sla_label(at(1, 10), at(2, 10), at(3, 0)), "是")
        self.assertEqual(ex.sla_label(at(1, 10), at(2, 10, 1), at(3, 0)), "否")
        self.assertEqual(ex.sla_label(at(1, 10), None, at(1, 20)), "未标已处理（未满 24 小时）")
        self.assertEqual(ex.sla_label(at(1, 10), None, at(3, 10)), "未标已处理（已过 48 小时）")


class TestCompetitors(unittest.TestCase):

    def test_chinese_ignores_spaces_and_punctuation(self):
        self.assertEqual(ex.competitors_in("邦 乐健，荣康都试过", [], ["邦乐健", "荣康", "东方神"]),
                         ["邦乐健", "荣康"])

    def test_english_is_whole_word(self):
        self.assertEqual(ex.competitors_in("analysis 一下", [], ["sis"]), [])
        self.assertEqual(ex.competitors_in("SiS 能量胶更好", [], ["sis"]), ["sis"])

    def test_keyword_counts_when_text_was_cut(self):
        self.assertEqual(ex.competitors_in("用这个不如用", ["东方神"], ["东方神"]), ["东方神"])


class TestCollectComments(unittest.TestCase):

    def test_discovery_handling_and_still_there_after_handled(self):
        events = [scan(at(1, 10), hits=[C1]), status(at(1, 15), "resolved"),
                  scan(at(1, 18), hits=[C1]), scan(at(2, 2))]
        [c] = ex.collect_comments(events)
        self.assertEqual((c["first_seen_ms"], c["last_seen_ms"], c["sightings"]),
                         (at(1, 10), at(1, 18), 2))
        self.assertEqual(c["created_at_ms"], at(1, 8))
        self.assertEqual((c["handled_ms"], c["handled_source"]), (at(1, 15), ex.SOURCE_PANEL))
        self.assertEqual(c["seen_after_handled"], "是", "标了已处理之后还扫到，评论多半还在")
        self.assertEqual(c["gone_ms"], at(2, 2))

    def test_duplicate_events_are_counted_once(self):
        events = [scan(at(1, 10), hits=[C1]), scan(at(1, 10), hits=[C1])]
        [c] = ex.collect_comments(events)
        self.assertEqual(c["sightings"], 1)

    def test_panel_auto_status_is_not_an_operator_action(self):
        events = [scan(at(1, 10), hits=[C1]), status(at(1, 11), "resolved", auto=True)]
        [c] = ex.collect_comments(events)
        self.assertIsNone(c["handled_ms"])

    def test_status_of_the_previous_post_in_the_same_row_does_not_count(self):
        events = [scan(at(1, 10), post="xhs:new", hits=[C1]),
                  status(at(1, 11), "resolved", post="xhs:old")]
        [c] = ex.collect_comments(events)
        self.assertIsNone(c["handled_ms"], "这一行换过链接，点的是上一篇")

    def test_short_link_alias_joins_the_same_post(self):
        events = [scan(at(1, 10), post="xhs:url:aaa", hits=[C1]),
                  scan(at(1, 18), post="xhs:abc", post_alias="xhs:url:aaa", hits=[C1]),
                  status(at(1, 12), "resolved", post="xhs:url:aaa")]
        [c] = ex.collect_comments(events)
        self.assertEqual((c["sightings"], c["handled_ms"]), (2, at(1, 12)))

    def test_accumulating_table_cannot_tell_after_handled(self):
        events = [scan(at(1, 10), hits=[C1]), status(at(1, 11), "resolved"),
                  scan(at(1, 18))]
        [c] = ex.collect_comments(events, accumulating=True)
        self.assertIsNone(c["gone_ms"], "攒负面的表处理过就不记了，没扫到不等于没了")
        self.assertEqual(c["gone_note"], "已处理后不再留痕，看不出")
        self.assertEqual(c["seen_after_handled"], "—")

    def test_scan_without_negative_check_is_not_an_absence(self):
        events = [scan(at(1, 10), hits=[C1]), scan(at(1, 18), checked=False)]
        [c] = ex.collect_comments(events)
        self.assertIsNone(c["gone_ms"])

    def test_ledger_fills_earlier_discovery_and_per_comment_handling(self):
        events = [scan(at(1, 10), hits=[C1])]
        ledger = {"rec1": {"p": "xhs:abc", "i": [
            {"k": "id:c1", "t": at(1, 9), "w": "邦乐健", "c": "邦乐健 比这个", "d": at(1, 20)},
            {"k": "id:c9", "t": at(1, 9, 30), "w": "荣康", "c": "荣康更便宜"}]}}
        c1, c9 = ex.collect_comments(events, ledger_rows=ledger, accumulating=True)
        self.assertEqual(c1["first_seen_ms"], at(1, 9))
        self.assertEqual((c1["handled_ms"], c1["handled_source"]), (at(1, 20), ex.SOURCE_LEDGER))
        self.assertTrue(c9["from_ledger_only"])
        self.assertEqual((c9["comment_id"], c9["content"]), ("c9", "荣康更便宜"))


class FakeJournal:
    def __init__(self, events):
        self.events = events

    def read_window(self, start, end, table_id):
        if table_id == "broken":
            raise RuntimeError("飞书 500")
        return [e for e in self.events if e.get("table_id") == table_id]


class FakeStore:
    def get_json(self, key, default):
        return default


class FakeTable:
    def __init__(self, table_id):
        self.table_id = table_id

    def fields_meta(self):
        return {"反馈链接": {}, "素人编号": {}, "笔记内容": {}}

    def search(self, columns):
        return [{"record_id": "rec1", "fields": {
            "反馈链接": "https://www.xiaohongshu.com/explore/6650aa000000000012345678",
            "素人编号": "S-07", "笔记内容": "换季过敏怎么办"}}]


class TestRunWritesSpreadsheets(unittest.TestCase):

    def _run(self, tables, events, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            out_dir = pathlib.Path(tmp) / "out"
            code = ex.run(tables, Settings(), FakeJournal(events), FakeStore(), out_dir,
                          now_ms=at(5, 12), **kwargs)
            files = {}
            for path in out_dir.glob("*.csv"):
                raw = path.read_bytes()
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "要带 BOM，Excel 才认得中文")
                files[path.name] = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        return code, files, printed.getvalue()

    def test_end_to_end(self):
        events = [{"type": "coverage", "at": at(1, 0), "table_id": "tbl1"},
                  scan(at(1, 10), hits=[C1, C2]), status(at(1, 15), "resolved"),
                  scan(at(3, 10), hits=[("c3", "荣康便宜", "荣康", None)])]
        code, files, printed = self._run([("项目A", FakeTable("tbl1"))], events,
                                         competitors=["邦乐健", "荣康", "东方神"])
        self.assertEqual(code, 0)
        detail = files["评论明细.csv"]
        self.assertEqual([r["评论ID"] for r in detail], ["c1", "c2", "c3"])
        self.assertEqual(detail[0]["素人编号"], "S-07")
        self.assertEqual(detail[0]["帖子标题"], "换季过敏怎么办")
        self.assertEqual(detail[0]["首次扫到（发现时间）"], "2026-11-01 10:00")
        self.assertEqual(detail[0]["发现→标已处理（小时）"], "5.0")
        self.assertEqual(detail[0]["24小时内标已处理"], "是")
        self.assertEqual(detail[2]["24小时内标已处理"], "未标已处理（已过 50 小时）")
        by_name = {r["竞品"]: r for r in files["按竞品汇总.csv"]}
        self.assertEqual(by_name["邦乐健"]["评论条数"], "1")
        self.assertEqual(by_name["荣康"]["还没标已处理"], "1")
        self.assertEqual(by_name[ex.NO_COMPETITOR]["评论条数"], "1")
        self.assertNotIn("东方神", by_name, "一条都没有的竞品不出空行")
        [post] = files["帖子汇总.csv"]
        self.assertEqual(post["全部24小时内标已处理"], "有还没标已处理的")
        self.assertIn("按竞品：邦乐健 1 条，荣康 1 条，东方神 0 条", printed)

    def test_window_filters_on_discovery(self):
        events = [scan(at(1, 10), hits=[C1]), scan(at(3, 10), hits=[C2])]
        since, until = ex.parse_window("2026-11-02", "2026-11-03")
        _, files, _ = self._run([("项目A", FakeTable("tbl1"))], events,
                                since_ms=since, until_ms=until)
        self.assertEqual([r["评论ID"] for r in files["评论明细.csv"]], ["c2"],
                         "11-01 首次扫到的那条，11-02 之后再扫到也不算进这一段")

    def test_one_broken_table_does_not_stop_the_rest(self):
        events = [scan(at(1, 10), hits=[C1])]
        code, files, printed = self._run(
            [("坏表", FakeTable("broken")), ("项目A", FakeTable("tbl1"))], events)
        self.assertEqual(code, 1)
        self.assertEqual(len(files["评论明细.csv"]), 1)
        self.assertIn("✗ 留痕读不到", printed)
        self.assertNotIn("按竞品汇总.csv", files, "没给竞品名单就不出这张")

    def test_nothing_found_still_writes_the_header(self):
        _, files, printed = self._run([("项目A", FakeTable("tbl1"))], [])
        self.assertEqual(files["评论明细.csv"], [])
        self.assertIn("没有扫到负面/竞品评论", printed)


class TestParseWindow(unittest.TestCase):

    def test_until_is_inclusive(self):
        since, until = ex.parse_window("2026-11-01", "2026-11-01")
        self.assertEqual((since, until), (at(1, 0), at(2, 0)))

    def test_until_before_since_is_refused(self):
        with self.assertRaises(ValueError):
            ex.parse_window("2026-11-05", "2026-11-01")


if __name__ == "__main__":
    unittest.main()
