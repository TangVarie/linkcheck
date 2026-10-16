"""两个运维脚本：C1 核对（tools/check_id_time.py）和换「下次检查时间」公式
（tools/update_next_check_formula.py）。全部离线。

C1 核对给的是一个**全局参数**的建议值，算错了就是照着错数去调
INFERRED_PUBLISH_MARGIN_DAYS——调小了，定时发布的帖子会还活着就被归档。
换公式那个会**写表**：盯的是「默认不写」「只写公式列」「只写要换的表」。
"""

import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from unittest import mock

import cli
from tools import check_id_time, update_next_check_formula as updater
from xhsearch import feishu, provision, registry, transport
from xhsearch.config import Settings
from xhsearch.provision import NEXT_CHECK_COLUMN, next_check_formula, next_check_plan
from xhsearch.rows import Row, format_resolved_id

UTC = timezone.utc
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
DAY_MS = 86_400_000


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def xhs_link(created: datetime) -> str:
    return "https://www.xiaohongshu.com/explore/" + f"{int(created.timestamp()):08x}" + "0" * 16


def row_with_gap(record_id: str, gap_days: float, *, created_days_ago: float = 40) -> Row:
    """ID 时间 = N 天前；发布时间 = ID 时间 + gap_days。"""
    created = (NOW - timedelta(days=created_days_ago)).replace(microsecond=0)
    return Row(record_id, xhs_link(created),
               publish_time_ms=ms(created + timedelta(days=gap_days)))


def samples(gaps) -> list:
    return [check_id_time.Sample("A", f"r{i}", g) for i, g in enumerate(gaps)]


# ---------- C1 核对 ----------


class TestCollect(unittest.TestCase):
    def test_gap_is_publish_time_minus_id_time(self):
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", [row_with_gap("r1", 3.5)], now=NOW)
        self.assertEqual(len(found.samples), 1)
        self.assertAlmostEqual(found.samples[0].gap_days, 3.5, places=3)

    def test_only_rows_with_both_times_are_samples(self):
        created = NOW - timedelta(days=5)
        rows = [
            row_with_gap("both", 1.0),
            Row("no_publish", xhs_link(created)),                          # 发布时间空着
            Row("xhs_short", "http://xhslink.com/a/AbC",
                publish_time_ms=ms(created)),                              # 小红书短链解不出
        ]
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", rows, now=NOW)
        count = found.counts[0]
        self.assertEqual((count.rows, count.with_publish, count.with_both, count.no_id),
                         (3, 2, 1, 1))
        self.assertEqual([s.record_id for s in found.samples], ["both"])

    def test_douyin_short_link_with_a_recorded_id_counts(self):
        """抖音短链展开过、记进「作品ID」的，ID 时间解得出——这是有效样本。"""
        created = int((NOW - timedelta(days=10)).timestamp())
        aweme = str(created << 32 | 12345)
        short = "https://v.douyin.com/iRxYzAb/"
        row = Row("dy", short, publish_time_ms=created * 1000 + 2 * DAY_MS,
                  resolved_id_cell=format_resolved_id(aweme, short))
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", [row], now=NOW)
        self.assertEqual(len(found.samples), 1)
        self.assertAlmostEqual(found.samples[0].gap_days, 2.0, places=3)


class TestRecommend(unittest.TestCase):
    def test_too_few_samples_gives_no_number(self):
        suggested, why = check_id_time.recommend(samples([0.5] * 29), 14)
        self.assertIsNone(suggested)
        self.assertIn("29", why)

    def test_a_gap_beyond_the_margin_asks_for_a_bigger_one(self):
        suggested, why = check_id_time.recommend(samples([0.2] * 40 + [16.3]), 14)
        self.assertEqual(suggested, 18, "最大差向上取整再多留一天")
        self.assertIn("不够", why)
        self.assertIn("1 行", why)

    def test_small_gaps_suggest_a_smaller_margin(self):
        suggested, why = check_id_time.recommend(samples([0.1] * 30 + [3.2]), 14)
        self.assertEqual(suggested, 5)
        self.assertIn("调小", why)

    def test_never_suggests_below_two_days(self):
        """全是负的（发布时间只填到天）也不能建议 0 或 1：留一天日期误差 + 一天余量。"""
        suggested, _ = check_id_time.recommend(samples([-0.6] * 40), 14)
        self.assertEqual(suggested, 2)

    def test_a_snug_margin_is_left_alone(self):
        suggested, why = check_id_time.recommend(samples([0.1] * 30 + [13.2]), 14)
        self.assertEqual(suggested, 14)
        self.assertIn("不用改", why)


class TestReport(unittest.TestCase):
    def test_lists_rows_beyond_the_margin_and_the_env_to_change(self):
        found = check_id_time.Findings()
        rows = [row_with_gap(f"r{i}", 0.3) for i in range(30)]
        rows += [row_with_gap("late", 20.0), row_with_gap("scheduled", 5.0),
                 row_with_gap("wrong", -3.0)]
        check_id_time.collect(found, "A", rows, now=NOW)
        text = "\n".join(check_id_time.report(found, 14))
        self.assertIn("晚于 ID 时间超过 14 天", text)
        self.assertIn("late：+20.0 天", text)
        self.assertIn("1–14 天（多半是定时发布，余量罩得住）：1 行", text)
        self.assertIn("wrong：-3.0 天", text, "早于 ID 时间超过一天是表里填错了，要列出来")
        self.assertIn("INFERRED_PUBLISH_MARGIN_DAYS=21", text)

    def test_date_only_publish_times_are_not_anomalies(self):
        """「发布时间」只填到天 = 当天 0 点，比 ID 时间早不到一天，正常。"""
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", [row_with_gap("r", -0.7)], now=NOW)
        text = "\n".join(check_id_time.report(found, 14))
        self.assertIn("早于 ID 时间超过 1 天（发布时间多半填错了，或链接贴错了，去表里看看）：0 行",
                      text)

    def test_long_lists_are_truncated(self):
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", [row_with_gap(f"r{i}", 20.0 + i) for i in range(12)],
                              now=NOW)
        text = "\n".join(check_id_time.report(found, 14))
        self.assertIn("……还有 4 行", text)
        self.assertIn("r11：+31.0 天", text, "最离谱的排在前面")

    def test_no_samples(self):
        found = check_id_time.Findings()
        check_id_time.collect(found, "A", [Row("r", "http://xhslink.com/a/AbC",
                                                 publish_time_ms=ms(NOW))], now=NOW)
        text = "\n".join(check_id_time.report(found, 14))
        self.assertIn("一行能比的都没有", text)
        self.assertIn("1 行是短链", text)


class FakeTable:
    """够 _read_rows / 换公式用的那几个方法。"""

    def __init__(self, meta, records=(), table_id="tblA", fail=None):
        self.meta = meta
        self.records = list(records)
        self.table_id = table_id
        self.fail = fail
        self.searched = []
        self.formulas = []

    def fields_meta(self):
        if self.fail:
            raise self.fail
        return self.meta

    def search(self, field_names, *, filter_spec=None, max_records=None):
        self.searched.append((list(field_names), filter_spec))
        return self.records

    def set_formula(self, column, expression):
        self.formulas.append((column, expression))
        self.meta = dict(self.meta)
        self.meta[column] = {**self.meta[column], "formula": expression}


class TestReadRows(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields

    def test_reads_every_row_unfiltered(self):
        """结案的、归档的、没勾巡查的行都是有效样本。"""
        created = NOW - timedelta(days=9)
        table = FakeTable({self.f.link: {}, self.f.publish_time: {}},
                          [{"record_id": "r1", "fields": {
                              self.f.link: xhs_link(created),
                              self.f.publish_time: ms(created) + DAY_MS}}])
        rows = check_id_time._read_rows(table, self.settings)
        self.assertEqual([r.record_id for r in rows], ["r1"])
        self.assertEqual(table.searched, [([self.f.link, self.f.publish_time], None)])

    def test_no_publish_time_column_reads_nothing(self):
        table = FakeTable({self.f.link: {}})
        self.assertEqual(check_id_time._read_rows(table, self.settings), [])
        self.assertEqual(table.searched, [])

    def test_unreadable_metadata_raises(self):
        with self.assertRaises(RuntimeError):
            check_id_time._read_rows(FakeTable(None), self.settings)


class TestCheckIdTimeMain(unittest.TestCase):
    def test_one_unreadable_table_does_not_stop_the_others(self):
        f = Settings().fields
        created = NOW - timedelta(days=9)
        good = FakeTable({f.link: {}, f.publish_time: {}},
                         [{"record_id": "r1", "fields": {
                             f.link: xhs_link(created), f.publish_time: ms(created)}}])
        bad = FakeTable(None)
        with mock.patch.object(cli, "load_env_or_exit"), \
             mock.patch.object(cli, "_tables", return_value=[("坏", bad), ("好", good)]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = check_id_time.main(["check_id_time.py"])
        self.assertEqual(code, 0)
        self.assertIn("表「坏」读不到", out.getvalue())
        self.assertIn("表「好」：读了 1 行", out.getvalue())
        self.assertIn("不发付费请求、不写表", out.getvalue())


class TestMarginEnv(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("INFERRED_PUBLISH_MARGIN_DAYS", None)

    def tearDown(self):
        os.environ.pop("INFERRED_PUBLISH_MARGIN_DAYS", None)
        if self._saved is not None:
            os.environ["INFERRED_PUBLISH_MARGIN_DAYS"] = self._saved

    def test_default_is_fourteen(self):
        self.assertEqual(cli.build_settings().refresh.inferred_publish_margin_days, 14)

    def test_the_env_sets_it(self):
        os.environ["INFERRED_PUBLISH_MARGIN_DAYS"] = "5"
        self.assertEqual(cli.build_settings().refresh.inferred_publish_margin_days, 5)

    def test_out_of_range_is_rejected(self):
        for value in ("-1", "61", "abc"):
            os.environ["INFERRED_PUBLISH_MARGIN_DAYS"] = value
            with self.subTest(value=value), self.assertRaises(SystemExit):
                cli.build_settings()


class TestPopTableFlag(unittest.TestCase):
    def test_absent(self):
        args = ["sweep"]
        self.assertIsNone(cli.pop_table_flag(args))
        self.assertEqual(args, ["sweep"])

    def test_removes_the_flag_and_its_value(self):
        args = ["--apply", "--table", "A, B", "x"]
        self.assertEqual(cli.pop_table_flag(args), ["A", "B"])
        self.assertEqual(args, ["--apply", "x"])

    def test_bad_values_exit(self):
        for args in (["--table"], ["--table", ","], ["--table", "A,A"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                cli.pop_table_flag(list(args))


# ---------- 换「下次检查时间」公式 ----------


OLD_FORMULA = ('IF(DATEDIF([发布时间], NOW(), "D") > 30, "", [最近检查时间] + '
               'IF(DATEDIF([发布时间], [最近检查时间], "D") <= 2, 8, '
               'IF(DATEDIF([发布时间], [最近检查时间], "D") <= 7, 24, 72)) / 24)')


def meta_with(formula=OLD_FORMULA, *, type_code=20, drop=()):
    f = Settings().fields
    meta = {c: {"type": 1} for c in (f.publish_time, f.last_updated,
                                      f.refresh_status, f.consecutive_failures)
            if c not in drop}
    entry = {"type": type_code}
    if formula is not None:
        entry["formula"] = formula
    meta[NEXT_CHECK_COLUMN] = entry
    return meta


class TestNextCheckPlan(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def test_the_old_formula_is_updated(self):
        plan = next_check_plan(self.settings, meta_with())
        self.assertEqual(plan.action, "update")
        self.assertEqual(plan.current, OLD_FORMULA)
        self.assertIn('[巡查状态] = "已失效"', plan.wanted)

    def test_the_new_formula_reformatted_is_up_to_date(self):
        """运营在飞书里多行缩进写的和一行写的是同一条公式。"""
        spaced = next_check_formula(self.settings).replace(", ", ",\n    ")
        self.assertEqual(next_check_plan(self.settings, meta_with(spaced)).action, "up_to_date")

    def test_unreadable_formula_is_overwritten(self):
        self.assertEqual(next_check_plan(self.settings, meta_with(None)).action, "update")

    def test_missing_column(self):
        meta = meta_with()
        del meta[NEXT_CHECK_COLUMN]
        self.assertEqual(next_check_plan(self.settings, meta).action, "missing")
        self.assertEqual(next_check_plan(self.settings, None).action, "missing")

    def test_a_hand_filled_date_column_is_not_touched(self):
        plan = next_check_plan(self.settings, meta_with(type_code=5))
        self.assertEqual(plan.action, "wrong_type")
        self.assertIn("不是公式列", plan.reason)

    def test_missing_referenced_columns(self):
        f = self.settings.fields
        plan = next_check_plan(self.settings, meta_with(drop=(f.consecutive_failures,)))
        self.assertEqual(plan.action, "no_refs")
        self.assertIn(f.consecutive_failures, plan.reason)

    def test_per_table_archive_days_go_into_the_formula(self):
        settings = Settings()
        settings.refresh.archive_after_days = 14
        self.assertTrue(next_check_plan(settings, meta_with()).wanted.startswith(
            'IF(DATEDIF([发布时间], NOW(), "D") > 14, ""'))


def ok(payload=None):
    return transport.Response(status=200, content_type="application/json",
                              body=json.dumps({"code": 0, "data": payload or {}}))


class OkResp:
    """urlopen 的替身：飞书的一个空成功响应。"""

    status = 200

    def __init__(self):
        self.headers = Message()
        self._body = b'{"code":0,"data":{}}'

    def read(self, amount=None):
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestFeishuWritesUseTheRightVerb(unittest.TestCase):
    """真走一遍传输层（只顶掉 urlopen）。上面那些用例顶的是 request_with_retry，
    看不见动词被改成 POST——实机上就是这样 404 的。"""

    def table(self):
        return feishu.Bitable(app_id="cli_x", app_secret="s",
                              app_token="bascnA", table_id="tblB")

    def opened(self, call):
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: OkResp()) as op:
            call(self.table())
        return [c.args[0] for c in op.call_args_list]

    def test_set_formula_is_a_put_with_id_refs(self):
        f = Settings().fields
        raw = {name: {"field_id": fid, "field_name": name, "type": 1}
               for name, fid in ((f.publish_time, "fldP"), (f.last_updated, "fldL"),
                                 (f.refresh_status, "fldS"), (f.consecutive_failures, "fldC"))}
        raw[NEXT_CHECK_COLUMN] = {"field_id": "fldN", "field_name": NEXT_CHECK_COLUMN,
                                  "type": 20, "property": {"formula_expression": "OLD"}}
        wanted = next_check_formula(Settings())
        with mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=raw):
            [request] = self.opened(lambda t: t.set_formula(NEXT_CHECK_COLUMN, wanted))
        self.assertEqual(request.get_method(), "PUT")
        self.assertTrue(request.full_url.endswith("/apps/bascnA/tables/tblB/fields/fldN"))
        sent = json.loads(request.data)["property"]["formula_expression"]
        self.assertNotIn("[发布时间]", sent)
        self.assertIn("bitable::$table[tblB].$field[fldP]", sent)
        self.assertIn('"已失效"', sent, "引号里的字符串原样不动")
        names = {fid: name for name, fid in
                 ((n, r["field_id"]) for n, r in raw.items())}
        self.assertEqual(feishu.readable_formula(sent, names, "tblB"), wanted,
                         "读回来翻成列名，要和新版逐字一样——不然脚本会报「不一样」")

    def test_delete_record_is_a_delete(self):
        [request] = self.opened(lambda t: t.delete_record("recX"))
        self.assertEqual(request.get_method(), "DELETE")
        self.assertTrue(request.full_url.endswith("/tables/tblB/records/recX"))

    def test_add_field_options_is_a_put(self):
        raw = {"流量状态": {"field_id": "fld1", "field_name": "流量状态", "type": 4,
                            "property": {"options": [{"id": "o1", "name": "爆贴"}]}}}
        with mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=raw):
            [request] = self.opened(lambda t: t.add_field_options("流量状态", ["风控中"]))
        self.assertEqual(request.get_method(), "PUT")


class TestIdFormula(unittest.TestCase):
    def test_only_known_columns_outside_quotes_are_swapped(self):
        got = feishu.id_formula('IF([甲] = "[甲]", [乙], [没有])', {"甲": "fldA", "乙": "fldB"}, "tblX")
        self.assertEqual(got, 'IF(bitable::$table[tblX].$field[fldA] = "[甲]", '
                              'bitable::$table[tblX].$field[fldB], [没有])')


class TestSetFormula(unittest.TestCase):
    """`PUT fields` 对 property 整体覆盖：显示格式等现有设置要原样带回去。"""

    def table(self):
        return feishu.Bitable(app_id="cli_x", app_secret="s",
                              app_token="bascnA", table_id="tblB")

    def raw(self, type_code=20):
        return {NEXT_CHECK_COLUMN: {
            "field_id": "fldN", "field_name": NEXT_CHECK_COLUMN, "type": type_code,
            "property": {"formula_expression": "OLD", "formatter": "yyyy/MM/dd HH:mm",
                         "type": {"data_type": 5}}}}

    def put(self, raw):
        sent = []

        def capture(method, url, headers, body, **kwargs):
            sent.append({"method": method, "url": url, "body": json.loads(body)})
            return ok()

        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=raw), \
             mock.patch.object(transport, "request_with_retry", capture):
            self.table().set_formula(NEXT_CHECK_COLUMN, "NEW")
        return sent

    def test_swaps_only_the_expression(self):
        sent = self.put(self.raw())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "PUT")
        self.assertTrue(sent[0]["url"].endswith("/tables/tblB/fields/fldN"))
        self.assertEqual(sent[0]["body"], {
            "field_name": NEXT_CHECK_COLUMN, "type": 20,
            "property": {"formula_expression": "NEW", "formatter": "yyyy/MM/dd HH:mm",
                         "type": {"data_type": 5}}})

    def test_refuses_a_column_that_is_not_a_formula(self):
        """把一列手填的日期改成公式 = 清空那一列。"""
        for raw in (self.raw(type_code=5), {}, None):
            with self.subTest(raw=raw), self.assertRaises(feishu.FeishuError):
                self.put(raw)

    def test_a_business_error_raises(self):
        bad = transport.Response(status=200, content_type="application/json",
                                 body=json.dumps({"code": 1254045, "msg": "formula invalid"}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=self.raw()), \
             mock.patch.object(transport, "request_with_retry", return_value=bad):
            with self.assertRaises(feishu.FeishuError):
                self.table().set_formula(NEXT_CHECK_COLUMN, "NEW")


class TestUpdaterRun(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        cli._REGISTRY_ROWS.clear()
        self.addCleanup(cli._REGISTRY_ROWS.clear)

    def run_it(self, tables, apply):
        lines = []
        code = updater.run(tables, self.settings, apply, out=lines.append)
        return code, "\n".join(lines)

    def test_dry_run_writes_nothing(self):
        old = FakeTable(meta_with(), table_id="tblOld")
        code, text = self.run_it([("老表", old)], apply=False)
        self.assertEqual(code, 0)
        self.assertEqual(old.formulas, [])
        self.assertIn("表「老表」：要换", text)
        self.assertIn("现在：" + OLD_FORMULA, text)
        self.assertIn("加 --apply 再跑一次才真换", text)

    def test_apply_writes_only_the_tables_that_need_it(self):
        old = FakeTable(meta_with(), table_id="tblOld")
        new = FakeTable(meta_with(next_check_formula(self.settings)), table_id="tblNew")
        hand = FakeTable(meta_with(type_code=5), table_id="tblHand")
        missing = FakeTable({k: v for k, v in meta_with().items() if k != NEXT_CHECK_COLUMN},
                            table_id="tblMissing")
        code, text = self.run_it([("老", old), ("新", new), ("手填", hand), ("缺", missing)],
                                 apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(old.formulas, [(NEXT_CHECK_COLUMN, next_check_formula(self.settings))])
        self.assertEqual((new.formulas, hand.formulas, missing.formulas), ([], [], []))
        self.assertIn("已换成新版（读回来核对过）", text)
        self.assertIn("这次换了 1 张", text)
        self.assertIn("公式粘这条", text, "缺这一列的表要给出能直接粘的公式")

    def test_a_failed_write_does_not_stop_the_others(self):
        broken = FakeTable(meta_with(), table_id="tblBroken")
        broken.set_formula = mock.Mock(side_effect=feishu.FeishuError(1254045, "formula invalid"))
        fine = FakeTable(meta_with(), table_id="tblFine")
        unreadable = FakeTable(None, table_id="tblGone", fail=feishu.FeishuError(-1, "403"))
        code, text = self.run_it([("坏", broken), ("读不到", unreadable), ("好", fine)],
                                 apply=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(fine.formulas), 1)
        self.assertIn("没换成", text)
        self.assertIn("表「读不到」读不到", text)
        self.assertIn("没换成 1 张", text)

    def test_read_back_that_cannot_be_compared_is_not_called_a_success(self):
        old = FakeTable(meta_with(), table_id="tblOld")

        def lose_formula(column, expression):
            old.meta = meta_with(None)

        old.set_formula = lose_formula
        _, text = self.run_it([("老", old)], apply=True)
        self.assertIn("飞书没给能比的公式原文", text)

    def test_read_back_that_differs_is_flagged(self):
        old = FakeTable(meta_with(), table_id="tblOld")

        def rewrite(column, expression):
            old.meta = meta_with("SOMETHING_ELSE()")

        old.set_formula = rewrite
        _, text = self.run_it([("老", old)], apply=True)
        self.assertIn("读回来的和新版不一样", text)

    def test_each_table_uses_its_own_archive_days(self):
        """注册表里改过「归档天数」的表，公式里的 30 要跟着变。"""
        custom = FakeTable(meta_with(), table_id="tblCustom")
        plain = FakeTable(meta_with(), table_id="tblPlain")
        cli._REGISTRY_ROWS["tblCustom"] = registry.Entry(
            label="定制", table_id="tblCustom", thresholds={registry.COL_ARCHIVE_DAYS: 14})
        with contextlib.redirect_stdout(io.StringIO()):
            self.run_it([("定制", custom), ("默认", plain)], apply=True)
        self.assertIn('NOW(), "D") > 14, ""', custom.formulas[0][1])
        self.assertIn('NOW(), "D") > 30, ""', plain.formulas[0][1])
        self.assertEqual(self.settings.refresh.archive_after_days, 30, "基准 Settings 不能被改")


class TestUpdaterMain(unittest.TestCase):
    def main(self, *args):
        old = FakeTable(meta_with(), table_id="tblOld")
        with mock.patch.object(cli, "load_env_or_exit"), \
             mock.patch.object(cli, "_tables", return_value=[("老", old)]) as tables, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = updater.main(["update_next_check_formula.py", *args])
        return code, old, tables, out.getvalue()

    def test_default_is_a_dry_run(self):
        code, old, _, text = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(old.formulas, [])
        self.assertIn("只看不改", text)

    def test_apply_writes(self):
        code, old, _, _ = self.main("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(old.formulas), 1)

    def test_table_flag_is_passed_through(self):
        _, _, tables, _ = self.main("--table", "老", "--apply")
        tables.assert_called_once_with(["老"])

    def test_unknown_args_print_usage(self):
        code, old, tables, text = self.main("--aply")
        self.assertEqual(code, 2)
        tables.assert_not_called()
        self.assertEqual(old.formulas, [])


if __name__ == "__main__":
    unittest.main()
