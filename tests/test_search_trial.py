"""搜索可见试跑：搜索端点的组包和归一化、逐表开关、硬上限、断点续跑、只有计数的报告。

全部离线：发请求的那一下是替身（`send`），响应是照另一个仓库在用的同一个端点的
外层信封（`{code: 200, data: {code: 0, success: true, data: {items: [...]}}}`）
造的假数据——里面的标题、昵称是故意放进去的，用来证明它们一个字都出不来。
"""

import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import cli
from xhsearch import (panel, protocol, providers, registry, rows as rows_mod, schema,
                      transport, visibility)
from xhsearch.config import Settings

KEY = "SECRET-abcdefgh-12345678"
BJ = Settings().display.tz()


def nid(n: int) -> str:
    return f"{n:024x}"


def item(n: int, *, kind: str = "note", **extra) -> dict:
    return {"model_type": kind,
            "note": {"id": nid(n), "title": "某某品牌的标题", "desc": "正文正文",
                     "user": {"nickname": "某某昵称"}},
            **extra}


def body(items, *, session=True, inner_code=0, success=True) -> str:
    core = {"items": items}
    if session:
        core.update({"search_id": "sid-1", "search_session_id": "ssid-1"})
    return json.dumps({"code": 200, "request_id": "rid-1",
                       "data": {"code": inner_code, "success": success, "data": core}},
                      ensure_ascii=False)


def ok(items, **kwargs) -> transport.Response:
    return transport.Response(200, "application/json", body(items, **kwargs))


def page_of(start: int, count: int = 20) -> list:
    return [item(n) for n in range(start, start + count)]


class Sender:
    """`send` 的替身：按顺序吐预先排好的响应，记下每一次发了什么。"""

    def __init__(self, responses=None, default=None):
        self.responses = list(responses or [])
        self.default = default
        self.requests: list[providers.Request] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        if self.default is not None:
            return self.default(request)
        raise AssertionError("发了一个没安排的请求")


def by_page(request) -> transport.Response:
    """每个词每页 20 条、各不相同的笔记；ID 由页码决定。"""
    page = int(request.url.split("page=")[1].split("&")[0])
    return ok(page_of(page * 100))


def run(round_name, keywords, state, sender, **kwargs):
    logs: list[str] = []
    result = visibility.run_round(
        visibility.ROUNDS[round_name], keywords, state, KEY, send=sender,
        sleep=lambda _s: None, clock=lambda: 1_800_000_000.0, log=logs.append, **kwargs)
    return result, logs


def fresh_state(directory) -> visibility.State:
    return visibility.State.load(pathlib.Path(directory) / "state.json")


def words(count: int) -> list[tuple[str, str]]:
    return [(f"词{i}", visibility.keyword_hash(f"词{i}")) for i in range(1, count + 1)]


# ---------- providers：搜索端点 ----------


class TestSearchBuild(unittest.TestCase):
    def build(self, **arguments):
        return providers.get_provider("tikhub").build(KEY, "xhs", providers.SEARCH, arguments)

    def test_first_page_uses_default_sort_and_no_session(self):
        req = self.build(keyword="快充 小片", page=1, sort="default")
        self.assertEqual(req.method, "GET")
        self.assertIn("/api/v1/xiaohongshu/app_v2/search_notes?", req.url)
        self.assertIn("sort_type=general", req.url)
        self.assertIn("page=1", req.url)
        self.assertIn("keyword=%E5%BF%AB%E5%85%85%20%E5%B0%8F%E7%89%87", req.url)
        self.assertNotIn("search_id", req.url)
        self.assertEqual(req.headers["User-Agent"], transport.BROWSER_UA)

    def test_later_pages_send_the_session_back(self):
        req = self.build(keyword="a", page=2, sort="latest",
                         search_id="sid-1", search_session_id="ssid-1")
        self.assertIn("sort_type=time_descending", req.url)
        self.assertIn("page=2", req.url)
        self.assertIn("search_id=sid-1", req.url)
        self.assertIn("search_session_id=ssid-1", req.url)

    def test_only_filters_that_were_asked_for_are_sent(self):
        """笔记类型、时间范围不传：要看的是什么都不筛时排在哪。"""
        req = self.build(keyword="a", page=1)
        query = req.url.split("?", 1)[1]
        self.assertEqual(sorted(p.split("=")[0] for p in query.split("&")),
                         ["keyword", "page", "sort_type"])


class TestSearchParse(unittest.TestCase):
    def parse(self, text, status=200):
        return providers.get_provider("tikhub").parse(
            "xhs", providers.SEARCH, status, "application/json", text, "", KEY)

    def test_notes_come_back_in_order_with_ids_only(self):
        result = self.parse(body([item(1), item(2), item(3)]))
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual([n["note_id"] for n in result.data["notes"]], [nid(1), nid(2), nid(3)])
        self.assertEqual(result.data["items"], 3)
        self.assertEqual(result.data["search_id"], "sid-1")
        self.assertEqual(result.data["search_session_id"], "ssid-1")

    def test_titles_and_nicknames_never_leave_the_provider(self):
        result = self.parse(body([item(1), item(2)]))
        dumped = json.dumps(result.data, ensure_ascii=False)
        for secret in ("某某品牌的标题", "正文正文", "某某昵称"):
            self.assertNotIn(secret, dumped)

    def test_cards_that_are_not_notes_are_counted_not_ranked(self):
        topic = {"model_type": "hot_query", "id": "abc", "queries": []}
        result = self.parse(body([item(1), topic, "乱入的字符串", item(2)]))
        self.assertEqual([n["note_id"] for n in result.data["notes"]], [nid(1), nid(2)])
        self.assertEqual(result.data["other_items"], 2)
        self.assertEqual(result.data["model_types"]["hot_query"], 1)

    def test_ad_like_items_are_flagged(self):
        result = self.parse(body([item(1, kind="ads"), item(2, ads_info={"x": 1}), item(3)]))
        self.assertEqual([n["ad"] for n in result.data["notes"]], [True, True, False])

    def test_note_id_can_sit_on_the_item_itself(self):
        flat = {"note_id": nid(7).upper(), "display_title": "标题"}
        result = self.parse(body([flat]))
        self.assertEqual(result.data["notes"], [{"note_id": nid(7), "ad": False}])

    def test_session_is_found_one_level_up_too(self):
        text = json.dumps({"code": 200, "data": {
            "code": 0, "success": True, "search_id": "outer",
            "data": {"items": [item(1)]}}})
        self.assertEqual(self.parse(text).data["search_id"], "outer")

    def test_empty_page_is_a_valid_answer(self):
        result = self.parse(body([], session=False))
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["notes"], [])
        self.assertNotIn("search_id", result.data)

    def test_inner_refusal_is_a_row_level_failure(self):
        text = json.dumps({"code": 200, "data": {"code": -1, "success": False, "msg": "x"}})
        result = self.parse(text)
        self.assertIsInstance(result, protocol.Err)
        self.assertEqual((result.kind, result.code), (protocol.Failure.UNKNOWN, "search_refused"))

    def test_inner_refusal_with_an_empty_list_is_not_an_empty_page(self):
        """里层说没成功，就算带着一个空的 items，也是失败，不是「一条都没有」。"""
        for kwargs in ({"success": False}, {"success": None, "inner_code": 5001},
                       {"success": False, "inner_code": 5001}):
            result = self.parse(body([], **kwargs))
            self.assertIsInstance(result, protocol.Err, kwargs)
            self.assertEqual((result.kind, result.code),
                             (protocol.Failure.UNKNOWN, "search_refused"))
        self.assertIsInstance(self.parse(body(page_of(100), success=False)), protocol.Err)
        self.assertIsInstance(self.parse(body([])), protocol.Ok)       # 真的空页照旧

    def test_unrecognised_success_shape_is_a_contract_failure(self):
        text = json.dumps({"code": 200, "data": {"code": 0, "success": True,
                                                 "data": {"whatever": 1}}})
        result = self.parse(text)
        self.assertEqual((result.kind, result.code),
                         (protocol.Failure.TRANSPORT, "unexpected_shape"))

    def test_http_400_is_an_error_and_the_key_is_redacted(self):
        text = json.dumps({"detail": {"code": 400, "message": f"请求失败 {KEY}"}})
        result = self.parse(text, status=400)
        self.assertIsInstance(result, protocol.Err)
        self.assertNotIn(KEY, result.message)

    def test_rate_limit_is_classified(self):
        text = json.dumps({"detail": {"code": 429, "message": "Too many requests"}})
        self.assertIs(self.parse(text, status=429).kind, protocol.Failure.RATE_LIMIT)


class TestSearchProviderRules(unittest.TestCase):
    def test_price_is_its_own_entry(self):
        tikhub = providers.get_provider("tikhub")
        self.assertAlmostEqual(tikhub.yuan_per_call("xhs", providers.SEARCH), 0.072)
        self.assertTrue(tikhub.bills_failed_lookups)
        self.assertEqual(providers.SEARCH_PRICING_CHECKED_ON, "2026-11-16")

    def test_search_price_does_not_follow_the_comment_price_override(self):
        before = dict(providers.TIKHUB_USD)
        try:
            providers.set_pricing(tikhub_usd={"xhs": 0.5})
            self.assertAlmostEqual(
                providers.get_provider("tikhub").yuan_per_call("xhs", providers.SEARCH), 0.072)
        finally:
            providers.TIKHUB_USD.update(before)

    def test_standby_vendor_does_not_take_search(self):
        sdx = providers.get_provider("socialdatax")
        self.assertFalse(sdx.can_handle("xhs", providers.SEARCH, {"keyword": "a"}))

    def test_tikhub_needs_a_keyword_and_only_on_xhs(self):
        tikhub = providers.get_provider("tikhub")
        self.assertTrue(tikhub.can_handle("xhs", providers.SEARCH, {"keyword": "a"}))
        self.assertFalse(tikhub.can_handle("xhs", providers.SEARCH, {"keyword": "  "}))
        self.assertFalse(tikhub.can_handle("douyin", providers.SEARCH, {"keyword": "a"}))

    def test_the_patrol_never_plans_a_search(self):
        """巡查一行都没改：排出来的调用里没有搜索。"""
        settings = Settings()
        settings.latest_comments_over = 0
        settings.reply_pages_per_post = 3
        settings.detail_within_days = 7
        for link in (f"https://www.xiaohongshu.com/explore/{nid(5)}",
                     "https://www.douyin.com/video/7412345678901234567"):
            row = rows_mod.Row(record_id="r", link_cell=link,
                               publish_time_ms=1_700_000_000_000)
            purposes = {c.purpose for c in rows_mod.plan_calls(row, settings)}
            self.assertNotIn(providers.SEARCH, purposes)
            self.assertTrue(purposes <= {"comments", "detail", "replies"}, purposes)


# ---------- 逐表开关：没开的表看不出多了一个功能 ----------


def healthy_meta(settings: Settings) -> dict:
    meta = {}
    for name, allowed, _label, options, _note in schema.expected_schema(settings):
        meta[name] = {"type": allowed[0], "options": list(options or []), "ui_type": ""}
    return meta


class TestSearchSwitch(unittest.TestCase):
    def setUp(self):
        self.off = Settings()
        self.on = Settings()
        self.on.search_check = True
        self.column = self.off.fields.search_keywords

    def test_column_names(self):
        self.assertEqual(self.column, "搜索关键词")
        self.assertEqual(registry.COL_SEARCH_CHECK, "搜索检查")

    def test_the_patrol_does_not_read_or_expect_the_column(self):
        self.assertNotIn(self.column, self.off.fields.must_read())
        self.assertNotIn(self.column, [row[0] for row in schema.expected_schema(self.on)])

    def test_switch_off_missing_column_is_silent(self):
        meta = healthy_meta(self.off)
        self.assertFalse([p for p in schema.schema_problems(self.off, meta) if "搜索" in p])
        self.assertNotIn(self.column,
                         [c.name for c in schema.diff(self.off, meta).missing_columns])

    def test_switch_off_wrong_type_is_silent_too(self):
        meta = healthy_meta(self.off)
        meta[self.column] = {"type": 2, "options": None, "ui_type": ""}
        self.assertFalse([p for p in schema.schema_problems(self.off, meta) if "搜索" in p])
        self.assertFalse([w for w in schema.diff(self.off, meta).wrong_types
                          if w.column == self.column])

    def test_switch_on_missing_column_is_reported_and_buildable(self):
        meta = healthy_meta(self.on)
        problems = [p for p in schema.schema_problems(self.on, meta) if self.column in p]
        self.assertEqual(len(problems), 1)
        self.assertIn("巡查不受影响", problems[0])
        missing = [c for c in schema.diff(self.on, meta).missing_columns
                   if c.name == self.column]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].body(), {"field_name": "搜索关键词", "type": 1})

    def test_switch_on_column_present_is_clean(self):
        for type_code in (1, 4):
            meta = healthy_meta(self.on)
            meta[self.column] = {"type": type_code, "options": [], "ui_type": ""}
            self.assertFalse([p for p in schema.schema_problems(self.on, meta)
                              if self.column in p])
            self.assertNotIn(self.column,
                             [c.name for c in schema.diff(self.on, meta).missing_columns])

    def test_switch_on_wrong_type_is_reported_not_rebuilt(self):
        meta = healthy_meta(self.on)
        meta[self.column] = {"type": 2, "options": None, "ui_type": ""}
        self.assertTrue([p for p in schema.schema_problems(self.on, meta)
                         if self.column in p and "数字" in p])
        diff = schema.diff(self.on, meta)
        self.assertTrue([w for w in diff.wrong_types if w.column == self.column])
        self.assertNotIn(self.column, [c.name for c in diff.missing_columns])

    def test_only_the_operator_column_exists_for_now(self):
        self.assertEqual([row[0] for row in schema.search_columns(self.on)], ["搜索关键词"])


class RegistryTable:
    def __init__(self, columns, records):
        self._meta = {name: {"type": 1, "options": None} for name in columns}
        self._records = records
        self.asked: list = []

    def fields_meta(self):
        return self._meta

    def search(self, field_names, **_kwargs):
        self.asked = list(field_names)
        return self._records


class TestRegistrySwitch(unittest.TestCase):
    BASE = [registry.COL_LABEL, registry.COL_TARGET, registry.COL_ENABLED]

    def record(self, record_id, table, **cells):
        return {"record_id": record_id,
                "fields": {registry.COL_LABEL: record_id,
                           registry.COL_TARGET: f"bascnAAAAAAAAAAAAAAA:tbl{table}",
                           registry.COL_ENABLED: True, **cells}}

    def test_old_registry_without_the_column_is_all_off(self):
        table = RegistryTable(self.BASE, [self.record("A", "AAAAAAAAAAAAAAA1")])
        entries = registry.read(table)
        self.assertNotIn(registry.COL_SEARCH_CHECK, table.asked)
        self.assertFalse(entries[0].search_check)

    def test_ticked_is_on_and_blank_is_off(self):
        table = RegistryTable(
            [*self.BASE, registry.COL_SEARCH_CHECK],
            [self.record("A", "AAAAAAAAAAAAAAA1", **{registry.COL_SEARCH_CHECK: True}),
             self.record("B", "AAAAAAAAAAAAAAA2")])
        entries = {e.label: e for e in registry.read(table)}
        self.assertIn(registry.COL_SEARCH_CHECK, table.asked)
        self.assertTrue(entries["A"].search_check)
        self.assertFalse(entries["B"].search_check)

    def test_new_registries_get_the_checkbox(self):
        made = {f["field_name"]: f["type"] for f in registry.REGISTRY_FIELDS}
        self.assertEqual(made[registry.COL_SEARCH_CHECK], 7)

    def test_overrides_carry_the_switch_without_touching_the_base(self):
        base = Settings()
        self.assertIs(registry.apply_overrides(base, registry.Entry(label="A")), base)
        per_table = registry.apply_overrides(base, registry.Entry(label="A", search_check=True))
        self.assertTrue(per_table.search_check)
        self.assertFalse(base.search_check)
        self.assertEqual(per_table.thresholds, base.thresholds)

    def test_panel_builds_with_the_tables_own_switch(self):
        projects = panel.Projects.__new__(panel.Projects)
        projects.settings = Settings()
        known = [registry.Entry(label="开", table_id="tblON", search_check=True),
                 registry.Entry(label="关", table_id="tblOFF")]
        with mock.patch.object(panel.Projects, "list", return_value=known):
            self.assertTrue(projects._settings_for_table("tblON").search_check)
            self.assertFalse(projects._settings_for_table("tblOFF").search_check)
            self.assertFalse(projects._settings_for_table("tblELSE").search_check)
        with mock.patch.object(panel.Projects, "list", side_effect=RuntimeError("读不到")):
            self.assertFalse(projects._settings_for_table("tblON").search_check)

    def test_doctor_uses_the_tables_own_switch(self):
        class T:
            table_id = "tblON"
        settings = Settings()
        with mock.patch.dict(cli._REGISTRY_ROWS,
                             {"tblON": registry.Entry(label="开", search_check=True)}, clear=True):
            self.assertTrue(cli._schema_settings(settings, T()).search_check)
        with mock.patch.dict(cli._REGISTRY_ROWS, {}, clear=True):
            self.assertIs(cli._schema_settings(settings, T()), settings)


# ---------- 读表 ----------


def xhs_row(n: int, **kwargs) -> rows_mod.Row:
    kwargs.setdefault("publish_time_ms", 1_700_000_000_000)
    return rows_mod.Row(record_id=f"rec{n}",
                        link_cell=f"https://www.xiaohongshu.com/explore/{nid(n)}", **kwargs)


class TestIntake(unittest.TestCase):
    def test_first_keyword_rules(self):
        self.assertEqual(visibility.first_keyword("#快充小片"), ("快充小片", 0))
        self.assertEqual(visibility.first_keyword("快充、小片；充电宝"), ("快充", 2))
        self.assertEqual(visibility.first_keyword([{"name": "a b"}, {"name": "c"}]), ("a b", 1))
        self.assertEqual(visibility.first_keyword(None), ("", 0))
        self.assertEqual(visibility.first_keyword(" # "), ("", 0))

    def test_blank_cells_are_not_counted_at_all(self):
        info = visibility.intake([(xhs_row(1), ""), (xhs_row(2), None)])
        self.assertEqual((info.rows_with_keyword, info.posts, info.skipped), (0, [], {}))

    def test_rows_that_cannot_be_checked_are_counted_by_reason(self):
        douyin = rows_mod.Row(record_id="d", link_cell="https://www.douyin.com/video/7412345678901234567")
        info = visibility.intake([
            (xhs_row(1), "词甲"),
            (douyin, "词甲"),
            (xhs_row(2, refresh_status=rows_mod.STATUS_GONE), "词甲"),
            (xhs_row(3, refresh_status=rows_mod.STATUS_SUSPECT), "词甲"),
            (xhs_row(4), "很" * 31),
        ])
        self.assertEqual([p.note_id for p in info.posts], [nid(1)])
        self.assertEqual(info.skipped, {visibility.SKIP_NOT_XHS: 1, visibility.SKIP_DEAD: 2,
                                        visibility.SKIP_TOO_LONG: 1})
        self.assertEqual(info.keywords, ["词甲"])

    def test_same_post_twice_is_one_post_and_case_does_not_split_a_keyword(self):
        info = visibility.intake([(xhs_row(1), "SPX"), (xhs_row(1), "spx"), (xhs_row(2), "Spx")])
        self.assertEqual(info.keywords, ["SPX"])
        self.assertEqual(len(info.posts), 2)

    def test_short_links_are_expanded_for_free_or_left_pending(self):
        short = rows_mod.Row(record_id="s", link_cell="https://xhslink.cn/o/abcDEF",
                             publish_time_ms=1_700_000_000_000)
        pending = visibility.intake([(short, "词甲")])
        self.assertEqual((pending.short_links, pending.posts, pending.keywords),
                         (1, [], ["词甲"]))
        resolved = visibility.intake([(short, "词甲")], resolve=lambda url: nid(9))
        self.assertEqual([p.note_id for p in resolved.posts], [nid(9)])
        failed = visibility.intake([(short, "词甲")], resolve=lambda url: None)
        self.assertEqual((failed.skipped, failed.keywords), ({visibility.SKIP_NO_ID: 1}, []))

    def test_empty_publish_time_is_not_searched_and_takes_no_keyword_slot(self):
        """设计第 9 条：发布时间空着的行查不了、不花钱。只被这种行带着的词也不该占名额。"""
        resolved = []
        short = rows_mod.Row(record_id="s", link_cell="https://xhslink.cn/o/abcDEF")
        info = visibility.intake(
            [(xhs_row(1, publish_time_ms=None), "只有空时间的词"),
             (short, "只有空时间的词"),
             (xhs_row(2), "词甲"), (xhs_row(3, publish_time_ms=None), "词甲")],
            resolve=lambda url: resolved.append(url) or nid(9))
        self.assertEqual(info.keywords, ["词甲"])
        self.assertEqual([(p.note_id, p.publish_ms) for p in info.posts],
                         [(nid(2), 1_700_000_000_000)])
        self.assertEqual(info.skipped, {visibility.SKIP_NO_TIME: 3})
        self.assertEqual(resolved, [])            # 连免费的短链展开都不做


# ---------- 账本和上限 ----------


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_caps_are_the_approved_ones(self):
        self.assertEqual((visibility.MAX_CALLS, visibility.MAX_YUAN, visibility.MAX_KEYWORDS),
                         (280, 20.0, 15))
        self.assertEqual(visibility.PLANNED_CALLS, 185)
        self.assertEqual({r.name: r.planned_calls for r in visibility.ROUNDS.values()},
                         {"first": 45, "latest": 30, "repeat1": 45, "repeat2": 45, "depth": 20})

    def test_call_cap_blocks_the_281st(self):
        state = fresh_state(self.dir.name)
        state.data["calls"] = 279
        self.assertEqual(state.blocked(0.01), "")
        state.charge(0.01)
        self.assertIn("次数上限", state.blocked(0.01))

    def test_yuan_cap_blocks_before_crossing(self):
        state = fresh_state(self.dir.name)
        for _ in range(277):
            self.assertEqual(state.blocked(0.072), "")
            state.charge(0.072)
        self.assertAlmostEqual(state.yuan, 19.944)
        self.assertIn("金额上限", state.blocked(0.072))
        self.assertLessEqual(state.yuan, visibility.MAX_YUAN)

    def test_ledger_survives_a_restart(self):
        state = fresh_state(self.dir.name)
        state.charge(0.072)
        state.save()
        again = fresh_state(self.dir.name)
        self.assertEqual((again.calls, again.yuan), (1, 0.072))
        self.assertEqual(pathlib.Path(self.dir.name, "state.json").stat().st_mode & 0o777, 0o600)

    def test_unreadable_ledger_refuses_instead_of_starting_from_zero(self):
        path = pathlib.Path(self.dir.name, "state.json")
        path.write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(visibility.StateError):
            visibility.State.load(path)
        path.write_text(json.dumps({"trial": "别的试跑", "version": 1}), encoding="utf-8")
        with self.assertRaises(visibility.StateError):
            visibility.State.load(path)

    def test_sixteenth_keyword_is_refused_and_nothing_is_registered(self):
        state = fresh_state(self.dir.name)
        self.assertEqual(state.register([f"词{i}" for i in range(1, 15)]), "")
        refusal = state.register(["词1", "新词甲", "新词乙"])
        self.assertIn("最多 15 个", refusal)
        self.assertEqual(len(state.data["keywords"]), 14)
        self.assertEqual(state.register(["新词甲", "新词甲", "词3"]), "")
        self.assertEqual(state.number(visibility.keyword_hash("新词甲")), 15)
        self.assertEqual(state.number(visibility.keyword_hash("词1")), 1)

    def test_state_file_holds_no_keyword_text(self):
        state = fresh_state(self.dir.name)
        state.register(["某某品牌"])
        run("first", [("某某品牌", visibility.keyword_hash("某某品牌"))], state,
            Sender(default=by_page))
        text = pathlib.Path(self.dir.name, "state.json").read_text(encoding="utf-8")
        self.assertNotIn("某某品牌", text)
        self.assertNotIn("某某昵称", text)
        self.assertNotIn(KEY, text)
        self.assertNotIn("http", text)

    def test_state_dir_must_be_outside_the_repo(self):
        repo = pathlib.Path(cli.__file__).resolve().parent
        self.assertIn("不能放在仓库目录里", visibility.check_state_dir(repo / "scratch", repo))
        self.assertIn("不能放在仓库目录里", visibility.check_state_dir(repo, repo))
        self.assertEqual(visibility.check_state_dir(pathlib.Path(self.dir.name), repo), "")


# ---------- 跑一轮 ----------


class TestRunRound(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = fresh_state(self.dir.name)
        self.words = words(15)
        self.state.register([w for w, _d in self.words])

    def test_first_round_is_45_calls_at_the_listed_price(self):
        sender = Sender(default=by_page)
        result, _logs = run("first", self.words, self.state, sender)
        self.assertEqual((result.fetched, result.reused, result.failed, result.requests),
                         (45, 0, 0, 45))
        self.assertTrue(result.clean)
        self.assertEqual(self.state.calls, 45)
        self.assertAlmostEqual(self.state.yuan, 3.24)
        self.assertTrue(all("sort_type=general" in r.url for r in sender.requests))
        # 第 2、3 页带着第 1 页给的会话值，第 1 页不带。
        self.assertNotIn("search_id", sender.requests[0].url)
        self.assertIn("search_id=sid-1", sender.requests[1].url)

    def test_rerun_pays_for_nothing_already_fetched(self):
        run("first", self.words, self.state, Sender(default=by_page))
        again = fresh_state(self.dir.name)
        result, _logs = run("first", self.words, again, Sender())   # 一发请求就炸
        self.assertEqual((result.fetched, result.reused, result.requests), (0, 45, 0))
        self.assertEqual(again.calls, 45)

    def test_rounds_do_not_share_pages(self):
        run("first", self.words, self.state, Sender(default=by_page))
        sender = Sender(default=by_page)
        result, _logs = run("latest", self.words, self.state, sender)
        self.assertEqual(result.requests, 30)
        self.assertTrue(all("sort_type=time_descending" in r.url for r in sender.requests))
        self.assertEqual(self.state.calls, 75)

    def test_depth_round_only_takes_the_first_two_keywords(self):
        result, _logs = run("depth", self.words, self.state, Sender(default=by_page))
        self.assertEqual(result.requests, 20)

    def test_call_cap_stops_before_the_call_that_would_cross(self):
        self.state.data["calls"] = 278
        sender = Sender(default=by_page)
        result, _logs = run("first", self.words, self.state, sender)
        self.assertEqual(len(sender.requests), 2)
        self.assertEqual(self.state.calls, 280)
        self.assertIn("次数上限", result.stopped)
        self.assertEqual(result.todo, 43)
        self.assertFalse(result.clean)

    def test_yuan_cap_stops_before_the_call_that_would_cross(self):
        self.state.data["yuan"] = 19.9
        sender = Sender(default=by_page)
        result, _logs = run("first", self.words, self.state, sender)
        self.assertEqual(len(sender.requests), 1)
        self.assertLessEqual(self.state.yuan, 20.0)
        self.assertIn("金额上限", result.stopped)

    def test_money_is_on_the_books_before_the_request_leaves(self):
        seen = []

        def spy(request, timeout):
            seen.append(fresh_state(self.dir.name).calls)   # 读的是磁盘上那一份
            return by_page(request)
        run("first", self.words[:1], self.state, spy)
        self.assertEqual(seen, [1, 2, 3])

    def test_failed_page_is_billed_not_kept_and_refetched_next_time(self):
        bad = transport.Response(400, "application/json",
                                 json.dumps({"detail": {"code": 400, "message": "请求失败"}}))
        sender = Sender([ok(page_of(100)), bad], default=by_page)
        result, logs = run("first", self.words[:2], self.state, sender)
        # 词1：第 1 页拿到、第 2 页失败、第 3 页不翻；词2：3 页。
        self.assertEqual((result.fetched, result.failed, result.requests), (4, 1, 5))
        self.assertEqual(self.state.calls, 5)
        self.assertEqual(result.todo, 2)
        self.assertEqual(self.state.data["failures"][0]["http"], 400)
        self.assertTrue(any("没拿到" in line for line in logs))
        again, _logs = run("first", self.words[:2], self.state, Sender(default=by_page))
        self.assertEqual((again.fetched, again.reused, again.requests), (2, 4, 2))
        self.assertEqual(self.state.calls, 7)

    def test_server_error_is_retried_once_through_the_ledger(self):
        boom = transport.Response(502, "text/html", "<html>bad gateway</html>")
        sender = Sender([boom, ok(page_of(100))], default=by_page)
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.fetched, result.failed, result.requests), (3, 0, 4))
        self.assertEqual(self.state.calls, 4)

    def test_timeout_counts_as_spent(self):
        lost = transport.Response(0, "", "请求超时（30s）", in_doubt=True)
        sender = Sender([lost, lost], default=by_page)
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.failed, result.requests, self.state.calls), (1, 2, 2))

    def test_connection_failure_is_refunded_and_stops_the_run(self):
        down = transport.Response(0, "", "网络错误：拒绝连接", not_sent=True)
        sender = Sender([down, down])
        result, _logs = run("first", self.words, self.state, sender)
        self.assertEqual((result.requests, self.state.calls, self.state.yuan), (0, 0, 0.0))
        self.assertIn("连不上", result.stopped)
        self.assertEqual(len(sender.requests), 2)

    def test_status_zero_without_the_not_sent_mark_stays_on_the_books(self):
        """读回答时的 TLS / 系统错误也是 status 0，但请求已经发出去了：不许退账。"""
        murky = transport.Response(0, "", "网络错误：SSLEOFError: EOF occurred")
        sender = Sender([murky, murky], default=by_page)
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.requests, self.state.calls), (2, 2))
        self.assertAlmostEqual(self.state.yuan, 0.144)
        self.assertNotIn("没算钱", result.stopped)

    def test_errors_after_the_request_left_are_never_refunded_end_to_end(self):
        """真的走一遍传输层：urlopen 在读回答时抛 SSLError / 裸 OSError，账要记上；
        只有 URLError（建连那一步）才退。"""
        import ssl
        import urllib.error
        request = providers.Request("GET", "https://api.tikhub.dev/x", {}, "")
        for exc, billed in ((ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), True),
                            (OSError(5, "Input/output error"), True),
                            (ValueError("bad chunk"), True),
                            (ConnectionResetError(104, "reset"), True),
                            (TimeoutError("timed out"), True),
                            (urllib.error.URLError(ConnectionRefusedError(111, "refused")), False)):
            with self.subTest(type(exc).__name__), tempfile.TemporaryDirectory() as folder, \
                    mock.patch("urllib.request.urlopen", side_effect=exc):
                response = visibility._send_once(request, 5.0)
                self.assertEqual((response.status, response.not_sent), (0, not billed))
                state = fresh_state(folder)
                state.register([w for w, _d in self.words])
                sent = []
                result = visibility.run_round(
                    visibility.ROUNDS["first"], self.words[:1], state, KEY,
                    send=lambda req, t: sent.append(1) or visibility._send_once(req, t),
                    sleep=lambda _s: None, clock=lambda: 1_800_000_000.0, log=lambda _l: None)
                self.assertEqual(len(sent), 2)
                self.assertEqual((state.calls, result.requests),
                                 (2, 2) if billed else (0, 0))

    def test_page_with_items_but_no_recognisable_id_is_not_the_end(self):
        """形状认不出来（ID 放在别的键下面）：不留这一页、整轮停、只花一次的钱。"""
        odd = [{"model_type": "note", "note": {"noteId": nid(n)}} for n in range(100, 120)]
        sender = Sender([ok(odd)])
        result, _logs = run("first", self.words, self.state, sender)
        self.assertEqual((len(sender.requests), self.state.calls), (1, 1))
        self.assertEqual((result.fetched, result.failed, result.clean), (0, 1, False))
        self.assertIn("认不出来", result.stopped)
        self.assertEqual(result.todo, 45)
        self.assertEqual(self.state.data["pages"], {})
        failure = self.state.data["failures"][0]
        self.assertEqual((failure["kind"], failure["items"], failure["keys"]),
                         (visibility.UNREADABLE, 20, ["model_type", "note"]))
        # 改好之后再跑：这一页会重新拿，不是「拿到过了」。
        again, _logs = run("first", self.words[:1], self.state, Sender(default=by_page))
        self.assertEqual((again.fetched, again.reused, again.clean), (3, 0, True))

    def test_unreadable_later_page_is_not_kept_and_the_keyword_stays_incomplete(self):
        odd = [{"model_type": "hot_query", "queries": ["x"]}]
        sender = Sender([ok(page_of(100)), ok(odd)], default=by_page)
        result, logs = run("first", self.words[:2], self.state, sender)
        self.assertEqual((result.fetched, result.failed, result.stopped), (4, 1, ""))
        self.assertFalse(result.clean)
        self.assertEqual(result.todo, 2)
        self.assertTrue(any("不是到底了" in line for line in logs))
        found = visibility.listing(visibility.ROUNDS["first"], self.words[0][1], self.state)
        self.assertEqual((found.pages, found.complete, found.ended), (1, False, False))

    def test_unreadable_page_already_in_the_state_file_is_not_trusted(self):
        """进度文件里已经存着一份「有条目、没 ID」的页（旧版本存的）：当它没拿到。"""
        digest = self.words[0][1]
        self.state.data["pages"][visibility.State.page_key("first", digest, 1)] = {
            "at": 1, "ids": [], "ads": [], "items": 20, "other": 20, "types": {}, "keys": [],
            "sid": {"search_id": "s"}}
        round_ = visibility.ROUNDS["first"]
        self.assertEqual(visibility.plan(round_, [digest], self.state), (3, 0))
        found = visibility.listing(round_, digest, self.state)
        self.assertEqual((found.pages, found.complete), (0, False))
        result, _logs = run("first", self.words[:1], self.state, Sender(default=by_page))
        self.assertEqual((result.fetched, result.reused), (3, 0))

    def test_rate_limit_waits_once_then_stops_the_run(self):
        limited = transport.Response(429, "application/json",
                                     json.dumps({"detail": {"code": 429, "message": "Too many requests"}}),
                                     retry_after=7.0)
        waits = []
        sender = Sender([limited, limited])
        result = visibility.run_round(
            visibility.ROUNDS["first"], self.words, self.state, KEY, send=sender,
            sleep=waits.append, clock=lambda: 1_800_000_000.0, log=lambda _l: None, pace=0)
        self.assertEqual(waits, [7.0])
        self.assertIn("限流", result.stopped)
        self.assertEqual(len(sender.requests), 2)
        self.assertEqual(self.state.calls, 2)

    def test_rate_limit_then_success_carries_on(self):
        limited = transport.Response(429, "application/json",
                                     json.dumps({"detail": {"code": 429, "message": "Too many requests"}}))
        waits = []
        result = visibility.run_round(
            visibility.ROUNDS["first"], self.words[:1], self.state, KEY,
            send=Sender([limited], default=by_page), sleep=waits.append,
            clock=lambda: 1_800_000_000.0, log=lambda _l: None, pace=0)
        self.assertEqual(waits, [visibility.RATE_LIMIT_WAIT_SECONDS])
        self.assertEqual((result.fetched, result.requests), (3, 4))
        self.assertTrue(result.clean)

    def test_bad_key_stops_at_once(self):
        denied = transport.Response(401, "application/json", json.dumps(
            {"detail": {"code": 401, "message": f"无效的API令牌，您提交的API令牌为 {KEY}"}}))
        sender = Sender([denied])
        result, logs = run("first", self.words, self.state, sender)
        self.assertEqual(len(sender.requests), 1)
        self.assertIn("Key", result.stopped)
        self.assertNotIn(KEY, " ".join(logs) + result.stopped)

    def test_no_session_on_page_one_is_a_failure_and_is_fetched_again(self):
        sender = Sender([ok(page_of(100), session=False)], default=by_page)
        result, logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.fetched, result.failed, result.requests, result.todo),
                         (0, 1, 1, 3))
        self.assertFalse(result.clean)
        self.assertTrue(any("会话值" in line for line in logs))
        self.assertEqual(self.state.data["pages"], {})                 # 这一页不留
        self.assertEqual(self.state.data["failures"][0]["kind"], visibility.NO_SESSION)
        # 再跑：第 1 页重新拿（再付一次），这回给了会话值，三页都拿到。
        again, _logs = run("first", self.words[:1], fresh_state(self.dir.name), sender)
        self.assertEqual((again.fetched, again.reused, again.todo), (3, 0, 0))
        self.assertTrue(again.clean)
        self.assertEqual(len(sender.requests), 4)

    def test_session_less_first_page_already_in_the_state_file_is_not_trusted(self):
        digest = self.words[0][1]
        self.state.keep_page("first", digest, 1, {
            "notes": [{"note_id": nid(i), "ad": False} for i in range(100, 120)],
            "items": 20}, 1_800_000_000.0)
        self.assertIsNone(self.state.page("first", digest, 1))
        self.assertEqual(visibility.plan(visibility.ROUNDS["first"], [digest], self.state),
                         (3, 0))
        sender = Sender(default=by_page)
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.fetched, result.reused), (3, 0))

    def test_refused_search_with_an_empty_list_is_not_kept_as_the_end(self):
        sender = Sender([ok([], success=False)], default=by_page)
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.fetched, result.failed, result.todo), (0, 1, 3))
        self.assertFalse(result.clean)
        self.assertEqual(self.state.data["pages"], {})
        again, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual(again.fetched, 3)

    def test_old_state_file_without_round_times_still_loads(self):
        run("first", self.words[:1], self.state, Sender(default=by_page))
        path = pathlib.Path(self.dir.name, "state.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.pop("rounds"), {"first": {"at": 1_800_000_000}})
        path.write_text(json.dumps(data), encoding="utf-8")
        state = fresh_state(self.dir.name)
        self.assertEqual(state.calls, 3)
        self.assertEqual(state.round_at("first"), 1_800_000_000.0)     # 退回页上的时间

    def test_empty_page_ends_the_keyword(self):
        sender = Sender([ok(page_of(100)), ok([])])
        result, _logs = run("first", self.words[:1], self.state, sender)
        self.assertEqual((result.fetched, result.requests, result.todo), (2, 2, 0))
        self.assertTrue(result.clean)

    def test_logs_never_show_keywords_urls_or_error_bodies(self):
        bad = transport.Response(400, "application/json",
                                 json.dumps({"detail": {"code": 400, "message": "上游的原话 secret-body"}}))
        _result, logs = run("first", [("某某品牌", visibility.keyword_hash("某某品牌"))],
                            self.state, Sender([bad]))
        text = " ".join(logs)
        for forbidden in ("某某品牌", "http", "secret-body", KEY):
            self.assertNotIn(forbidden, text)

    def test_requests_are_paced(self):
        waits = []
        ticks = iter(range(1000))
        visibility.run_round(
            visibility.ROUNDS["first"], self.words[:1], self.state, KEY,
            send=Sender(default=by_page), sleep=waits.append,
            clock=lambda: 1_800_000_000.0 + next(ticks) * 0.01, log=lambda _l: None)
        self.assertEqual(len(waits), 2)                 # 第一次不用等
        self.assertTrue(all(0 < w <= visibility.PACE_SECONDS for w in waits))

    def test_default_sender_makes_exactly_one_http_request(self):
        """不用传输层自带的三次重试：每个请求都得先过账本。"""
        calls = []

        def fake_request(method, url, headers, body="", timeout=30.0):
            calls.append(url)
            return transport.Response(502, "", "bad gateway")
        request = providers.Request("GET", "https://api.tikhub.dev/x", {}, "")
        with mock.patch.object(transport, "request", fake_request):
            visibility._send_once(request, 5.0)
        self.assertEqual(len(calls), 1)


# ---------- 报告 ----------


AT = 1_800_000_000.0        # 2027-01-15 16:00 北京时间


def days_before(days: float) -> int:
    return int((AT - days * 86400) * 1000)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = fresh_state(self.dir.name)
        self.word = "某某品牌"
        self.digest = visibility.keyword_hash(self.word)
        self.state.register([self.word])

    def keep(self, round_name, page, ids, ads=()):
        self.state.keep_page(round_name, self.digest, page, {
            "notes": [{"note_id": nid(i), "ad": i in ads} for i in ids],
            "items": len(ids), "other_items": 0, "model_types": {"note": len(ids)},
            "item_keys": ["model_type", "note"], "search_id": "s"}, AT)

    def post(self, n, age_days=None, comments=None):
        return visibility.Post(nid(n), self.word,
                               None if age_days is None else days_before(age_days), comments)

    def report(self, posts):
        return visibility.build_report(self.state, posts, tz=BJ, now=AT)

    def test_age_bands_follow_calendar_days_in_table_time(self):
        band = lambda days: visibility.age_band(days_before(days), AT, BJ)   # noqa: E731
        self.assertEqual([band(0), band(3), band(3.9), band(4), band(7), band(8)],
                         ["3天内", "3天内", "4到7天", "4到7天", "4到7天", "更早"])
        self.assertEqual(visibility.age_band(None, AT, BJ), "发布时间空")
        self.assertEqual(visibility.age_band(10 ** 20, AT, BJ), "发布时间空")

    def test_rank_bands(self):
        self.assertEqual([visibility.rank_band(r) for r in (1, 20, 21, 40, 41, 60, 61)],
                         ["前20", "前20", "21到40", "21到40", "41到60", "41到60", "60以后"])

    def test_found_not_found_and_incomplete_are_kept_apart(self):
        self.keep("first", 1, range(100, 120))
        self.keep("first", 2, range(120, 140))
        posts = [self.post(105, 2), self.post(125, 2), self.post(999, 2)]
        day3 = self.report(posts)["各轮"]["first"]["按帖龄"]["3天内"]
        # 第 3 页还没拿到：没出现的那一篇是「没查全」，不是「没搜到」。
        self.assertEqual((day3["篇数"], day3["前20"], day3["21到40"], day3["没搜到"], day3["没查全"]),
                         (3, 1, 1, 0, 1))
        self.keep("first", 3, range(140, 160))
        report = self.report(posts)
        day3 = report["各轮"]["first"]["按帖龄"]["3天内"]
        self.assertEqual((day3["没搜到"], day3["没查全"]), (1, 0))
        self.assertEqual(report["回答"], {
            "问题": "发布 3 天内的帖子，综合排序前 3 页里看不看得见",
            "3天内的帖子": 3, "搜到的": 2, "没搜到的": 1, "没查全的": 0})
        row = report["各轮"]["first"]["按词"][0]
        self.assertEqual((row["词"], row["看了前几位"], row["查全了"]), ("K01", 60, True))

    def test_posts_under_a_keyword_that_was_never_fetched_are_incomplete(self):
        """第二个词第 1 页 HTTP 400：它下面的帖子是「没查全」，不能从表里消失。"""
        other = "另一个词"
        self.state.register([other])
        bad = transport.Response(400, "application/json",
                                 json.dumps({"detail": {"code": 400, "message": "x"}}))
        pairs = [(self.word, self.digest), (other, visibility.keyword_hash(other))]
        result, _logs = run("first", pairs, self.state,
                            Sender([ok(page_of(100)), ok(page_of(200)), ok(page_of(300)), bad]))
        self.assertEqual(result.failed, 1)
        posts = [self.post(105, 2, comments=30),
                 visibility.Post(nid(999), other, days_before(2), 30)]
        report = self.report(posts)
        self.assertEqual(report["帖子"], 2)
        self.assertEqual(report["回答"], {
            "问题": "发布 3 天内的帖子，综合排序前 3 页里看不看得见",
            "3天内的帖子": 2, "搜到的": 1, "没搜到的": 0, "没查全的": 1})
        block = report["各轮"]["first"]
        self.assertEqual(block["按评论数"]["评论数到 20 条"]["没查全"], 1)
        self.assertEqual(block["按词"][1]["按帖龄"]["3天内"]["没查全"], 1)
        text = visibility.render_report(report)
        self.assertIn("搜到 1 篇，没搜到 0 篇，没查全 1 篇", text)
        self.assertIn("K02：还没查｜3天内 1 篇：没查全 1", text)

    def test_posts_under_a_keyword_the_cap_stopped_before_are_incomplete(self):
        other = "另一个词"
        self.state.register([other])
        self.state.data["calls"] = visibility.MAX_CALLS - 3
        pairs = [(self.word, self.digest), (other, visibility.keyword_hash(other))]
        sender = Sender(default=by_page)
        result, _logs = run("first", pairs, self.state, sender)
        self.assertEqual((len(sender.requests), bool(result.stopped)), (3, True))
        # 帖龄按这一轮跑的那一刻算（这个词没有自己的第 1 页时间）。
        posts = [visibility.Post(nid(999), other, days_before(2)),
                 visibility.Post(nid(998), other, days_before(5))]
        by_age = self.report(posts)["各轮"]["first"]["按帖龄"]
        self.assertEqual((by_age["3天内"]["没查全"], by_age["4到7天"]["没查全"]), (1, 1))

    def test_round_where_every_first_page_failed_still_counts_its_posts(self):
        bad = transport.Response(400, "application/json",
                                 json.dumps({"detail": {"code": 400, "message": "x"}}))
        run("first", [(self.word, self.digest)], self.state, Sender([bad]))
        report = self.report([self.post(105, 2)])
        self.assertEqual((report["回答"]["3天内的帖子"], report["回答"]["没查全的"]), (1, 1))

    def test_post_published_after_the_round_was_searched_is_left_out(self):
        """同一个词后来又填了一篇新帖：它不在当时查的那批里，不能算成「3 天内、没搜到」。"""
        for page in (1, 2, 3):
            self.keep("first", page, range(page * 100, page * 100 + 20))
        later = visibility.Post(nid(999), self.word, days_before(-2))   # 查完两天后才发
        report = self.report([self.post(105, 2), later])
        self.assertEqual(report["回答"], {
            "问题": "发布 3 天内的帖子，综合排序前 3 页里看不看得见",
            "3天内的帖子": 1, "搜到的": 1, "没搜到的": 0, "没查全的": 0})
        block = report["各轮"]["first"]
        self.assertEqual(block["查之后才发的帖子（不算）"], 1)
        self.assertEqual(sum(b["篇数"] for b in block["按帖龄"].values()), 1)
        self.assertIn("有 1 篇帖子是这一轮查完之后才发的", visibility.render_report(report))
        # 没查到的词也一样：这一轮跑完之后才发的不算。
        other = "另一个词"
        self.state.register([other])
        self.state.mark_round("first", AT)
        report = self.report([visibility.Post(nid(998), other, days_before(-2)),
                              visibility.Post(nid(997), other, days_before(1))])
        block = report["各轮"]["first"]
        self.assertEqual((block["查之后才发的帖子（不算）"],
                          block["按帖龄"]["3天内"]["没查全"]), (1, 1))

    def test_later_round_still_counts_the_newer_post(self):
        for page in (1, 2, 3):
            self.keep("first", page, range(page * 100, page * 100 + 20))
            self.state.keep_page("repeat2", self.digest, page, {
                "notes": [{"note_id": nid(i), "ad": False}
                          for i in range(page * 100, page * 100 + 20)],
                "items": 20, "search_id": "s"}, AT + 3 * 86400)
        rounds = self.report([visibility.Post(nid(999), self.word, days_before(-2))])["各轮"]
        self.assertEqual(rounds["first"]["按帖龄"]["3天内"]["篇数"], 0)
        self.assertEqual(rounds["repeat2"]["按帖龄"]["3天内"]["没搜到"], 1)

    def test_unreadable_page_never_reads_as_not_found(self):
        """认不出 ID 的页：报告不许说「没搜到」「查全了」，要明说没读懂。"""
        odd = [{"model_type": "note", "note": {"noteId": nid(n)}} for n in range(100, 120)]
        result, _logs = run("first", [(self.word, self.digest)], self.state, Sender([ok(odd)]))
        self.assertFalse(result.clean)
        report = self.report([self.post(105, 2)])
        self.assertEqual(report["回答"]["没搜到的"], 0)
        self.assertEqual(report["读不懂的页"],
                         {"页数": 1, "条目类型": {"note": 20}, "条目键名": ["model_type", "note"]})
        text = visibility.render_report(report)
        self.assertIn("认不出来", text)
        self.assertIn("不能当成「没搜到」", text)
        self.assertIn("first：读不懂的页：1 次", text)
        # 重新拿到之后警告消失，失败记录还在。
        run("first", [(self.word, self.digest)], self.state, Sender(default=by_page))
        report = self.report([self.post(105, 2)])
        self.assertEqual(report["读不懂的页"]["页数"], 0)
        self.assertNotIn("不能当成", visibility.render_report(report))
        self.assertEqual(report["回答"]["搜到的"], 1)

    def test_our_post_in_a_paid_slot_is_not_an_organic_rank(self):
        self.keep("first", 1, [100, 101, 102], ads=(100,))
        self.keep("first", 2, [])
        day3 = self.report([self.post(100, 1), self.post(102, 1)])["各轮"]["first"]["按帖龄"]["3天内"]
        self.assertEqual((day3["付费位"], day3["前20"]), (1, 1))
        found = visibility.listing(visibility.ROUNDS["first"], self.digest, self.state)
        self.assertEqual(found.rank(nid(102)), 2)          # 广告位不占名次
        self.assertTrue(found.ended and found.complete)

    def test_repeats_across_pages_do_not_push_ranks_down(self):
        self.keep("first", 1, [100, 101])
        self.keep("first", 2, [101, 102])
        self.keep("first", 3, [])
        found = visibility.listing(visibility.ROUNDS["first"], self.digest, self.state)
        self.assertEqual(found.organic, [nid(100), nid(101), nid(102)])

    def test_posts_split_by_age_and_comments(self):
        for page in (1, 2, 3):
            self.keep("first", page, range(page * 100, page * 100 + 20))
        # 发布时间空的帖子读表那一步就不收了；这一档只是给直接调 build_report 的人兜底。
        posts = [self.post(100, 1, comments=50), self.post(250, 5, comments=3),
                 self.post(777, 30), self.post(888)]
        block = self.report(posts)["各轮"]["first"]
        self.assertEqual(block["按帖龄"]["3天内"]["前20"], 1)
        self.assertEqual(block["按帖龄"]["4到7天"]["没搜到"], 1)
        self.assertEqual(block["按帖龄"]["更早"]["没搜到"], 1)
        self.assertEqual(block["按帖龄"]["发布时间空"]["篇数"], 1)
        self.assertEqual(block["按评论数"]["评论数到 20 条"]["前20"], 1)
        self.assertEqual(block["按评论数"]["评论数不到 20 条"]["篇数"], 1)
        self.assertEqual(block["按评论数"]["评论数没量到"]["篇数"], 2)
        self.assertEqual(block["每页笔记数"], {"最少": 20, "中位": 20, "最多": 20})

    def test_stability_compares_repeats_with_the_first_round(self):
        for page in (1, 2, 3):
            self.keep("first", page, range(page * 100, page * 100 + 20))
        self.keep("repeat1", 1, [*range(500, 519), 300])      # 300：第 41 位 → 第 20 位
        self.keep("repeat1", 2, range(100, 120))              # 100：第 1 位 → 第 21 位
        self.keep("repeat1", 3, range(600, 620))              # 200：搜到 → 没搜到
        tally = self.report([self.post(100, 1), self.post(200, 1), self.post(300, 1),
                             self.post(999, 1)])["名次稳不稳"]["repeat1"]
        self.assertEqual(tally, {"对得上的篇数": 4, "档位一样": 1, "档位变了": 3,
                                 "名次差超过20": 1, "一次搜到一次没搜到": 1})

    def test_report_is_counts_only(self):
        for page in (1, 2, 3):
            self.keep("first", page, range(page * 100, page * 100 + 20))
        self.state.note_failure("first", self.digest, 2, "unknown", 400, AT)
        self.state.data["balance"].append({"round": "first", "requests": 3,
                                           "drop_usd": 0.03, "at": int(AT)})
        report = self.report([self.post(100, 1), self.post(999, 1)])
        text_path, json_path = visibility.write_report(pathlib.Path(self.dir.name), report)
        for path in (text_path, json_path):
            content = path.read_text(encoding="utf-8")
            for forbidden in (self.word, self.digest, nid(100), nid(999), "http", "xhslink"):
                self.assertNotIn(forbidden, content)
        text = text_path.read_text(encoding="utf-8")
        self.assertIn("K01", text)
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里搜到 1 篇，没搜到 1 篇", text)
        self.assertIn("first：unknown（HTTP 400）：1 次", text)
        self.assertIn("余额少了 $0.03", text)
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["花费"]["次数上限"], 280)

    def test_report_before_any_page_says_so(self):
        text = visibility.render_report(self.report([self.post(100, 1)]))
        self.assertIn("还没有「发布 3 天内」的帖子有结果", text)

    def test_depth_round_lists_only_the_keywords_it_searched(self):
        self.state.register(["词乙", "词丙"])
        for page in range(1, 11):
            self.keep("depth", page, range(page * 100, page * 100 + 20))
        block = self.report([self.post(905, 1)])["各轮"]["depth"]
        self.assertEqual([row["词"] for row in block["按词"]], ["K01", "K02"])
        self.assertEqual(block["按词"][0]["看了前几位"], 200)
        self.assertEqual(block["按帖龄"]["3天内"]["60以后"], 1)
        self.assertEqual(block["页"], {"计划": 20, "拿到": 10, "还差": 10, "失败过": 0})


# ---------- 命令行 ----------


class TrialTable:
    def __init__(self, records, *, with_column=True, table_id="tblTRIAL"):
        settings = Settings()
        self.table_id = table_id
        self._meta = {name: {"type": 1, "options": None}
                      for name in settings.fields.must_read()}
        if with_column:
            self._meta[settings.fields.search_keywords] = {"type": 1, "options": None}
        self._records = records
        self.writes = 0

    def fields_meta(self):
        return self._meta

    def search(self, field_names, **_kwargs):
        return self._records

    def __getattr__(self, name):
        if name.startswith(("batch_", "create_", "delete_", "update_")):
            raise AssertionError(f"试跑不该往飞书写：{name}")
        raise AttributeError(name)


def trial_record(n: int, keyword, *, age_days=2.0) -> dict:
    f = Settings().fields
    return {"record_id": f"rec{n}", "fields": {
        f.link: f"https://www.xiaohongshu.com/explore/{nid(n)}",
        f.publish_time: int((datetime.now(timezone.utc).timestamp() - age_days * 86400) * 1000),
        f.search_keywords: keyword}}


class TestCommand(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.table = TrialTable([trial_record(100, "某某品牌"), trial_record(999, "某某品牌"),
                                 trial_record(300, "第二个词", age_days=5),
                                 {"record_id": "blank", "fields": {}}])
        patches = [
            mock.patch.object(cli, "_search_trial_tables", lambda selected: [("甲表", self.table)]),
            mock.patch.object(cli, "_tikhub_balance_usd", lambda *_a: None),
            mock.patch.dict(cli.os.environ, {"TIKHUB_API_KEY": KEY}),
            mock.patch("time.sleep", lambda _s: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def invoke(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = cli.cmd_search_trial([*args, "--state-dir", self.dir.name])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_dry_run_sends_nothing_writes_nothing_and_prices_the_round(self):
        def explode(*_a, **_k):
            raise AssertionError("--dry-run 不该发请求")
        with mock.patch.object(visibility, "_send_once", explode), \
                mock.patch.object(transport, "request", explode):
            code, out = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("只演练，不发付费请求", out)
        self.assertIn("词：2 个", out)
        self.assertIn("还要发最多 6 次，约 ¥0.43", out)
        self.assertIn("上限 280 次 / ¥20", out)
        self.assertNotIn("某某品牌", out)
        self.assertEqual(list(pathlib.Path(self.dir.name).iterdir()), [])

    def test_real_run_fetches_reports_and_resumes_for_free(self):
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(len(sender.requests), 6)
        self.assertIn("新拿到 6 页", out)
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里搜到 1 篇，没搜到 1 篇", out)
        for forbidden in ("某某品牌", "第二个词", nid(100), "tikhub.", KEY):
            self.assertNotIn(forbidden, out)
        names = sorted(p.name for p in pathlib.Path(self.dir.name).iterdir())
        self.assertEqual(names, ["report.json", "report.md", "state.json", "trial.lock"])
        with mock.patch.object(visibility, "_send_once", Sender()):       # 再发就炸
            code, out = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("新拿到 0 页，沿用 6 页", out)

    def test_show_keywords_prints_to_the_screen_only(self):
        with mock.patch.object(visibility, "_send_once", Sender(default=by_page)):
            _code, out = self.invoke("--show-keywords")
        self.assertIn("K01 = 某某品牌", out)
        report = pathlib.Path(self.dir.name, "report.md").read_text(encoding="utf-8")
        self.assertNotIn("某某品牌", report)

    def test_failed_pages_make_the_exit_code_non_zero(self):
        bad = transport.Response(400, "application/json",
                                 json.dumps({"detail": {"code": 400, "message": "x"}}))
        with mock.patch.object(visibility, "_send_once", Sender([bad], default=by_page)):
            code, out = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("没拿到 1 页", out)

    def test_first_page_without_a_session_exits_non_zero(self):
        sender = Sender([ok(page_of(100), session=False)], default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("没拿到 1 页", out)
        self.assertIn("还差 3 页", out)
        self.assertIn("没查全 2 篇", out)
        self.assertNotIn("没搜到 1 篇", out)
        with mock.patch.object(visibility, "_send_once", sender):       # 再跑补得上
            code, out = self.invoke()
        self.assertEqual(code, 0)
        self.assertIn("新拿到 3 页，沿用 3 页", out)

    def test_report_only_leaves_out_a_post_added_after_the_search(self):
        with mock.patch.object(visibility, "_send_once", Sender(default=by_page)):
            self.invoke()
        self.table._records.append(trial_record(998, "某某品牌", age_days=-1))
        code, out = self.invoke("--report-only")
        self.assertEqual(code, 0)
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里搜到 1 篇，没搜到 1 篇。", out)
        self.assertIn("有 1 篇帖子是这一轮查完之后才发的", out)

    def test_unreadable_first_page_exits_non_zero_after_one_call(self):
        odd = [{"model_type": "note", "note": {"noteId": nid(n)}} for n in range(100, 120)]
        sender = Sender([ok(odd)])
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual((code, len(sender.requests)), (1, 1))
        self.assertIn("中途停了", out)
        self.assertIn("不能当成「没搜到」", out)
        self.assertNotIn("没搜到 1 篇", out)

    def test_ledger_is_read_only_after_the_lock_is_held(self):
        """另一个试跑在我们启动之后、拿到锁之前跑完了：我们得用它存完盘的账本，
        不能拿更早读的那份旧的——否则它的页再付一遍钱，它记的账还被盖掉。"""
        state_path = pathlib.Path(self.dir.name, "state.json")
        order = []
        real_acquire, real_load = cli.runlock.acquire, visibility.State.load

        def acquire(*a, **k):
            # 我们等到锁的那一刻之前，「另一个试跑」把 6 页拿完、存了盘、放了锁。
            other = real_load(state_path)
            other.register(["某某品牌", "第二个词"])
            pairs = [(w, visibility.keyword_hash(w)) for w in ("某某品牌", "第二个词")]
            run("first", pairs, other, Sender(default=by_page))
            order.append("lock")
            return real_acquire(*a, **k)

        def load(path):
            order.append("load")
            return real_load(path)

        def read_table(selected):
            order.append("read-table")
            return [("甲表", self.table)]

        sender = Sender()                              # 我们自己再发一次就炸
        with mock.patch.object(cli.runlock, "acquire", acquire), \
                mock.patch.object(visibility.State, "load", staticmethod(load)), \
                mock.patch.object(cli, "_search_trial_tables", read_table), \
                mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual(order, ["lock", "load", "read-table"])
        self.assertEqual(code, 0)
        self.assertIn("新拿到 0 页，沿用 6 页", out)
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual((saved["calls"], len(saved["pages"])), (6, 6))

    def test_second_run_is_refused_before_it_reads_the_ledger(self):
        lease = cli.runlock.acquire("别的试跑", path=str(pathlib.Path(self.dir.name, "trial.lock")))
        self.addCleanup(lease.release)
        with mock.patch.object(visibility.State, "load",
                               side_effect=AssertionError("没拿到锁就读了账本")), \
                mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke()
        self.assertIn("另一个试跑正在跑", str(code))

    def test_lock_is_released_when_the_run_refuses(self):
        self.table._records = [trial_record(n, f"词{n}") for n in range(1, 17)]
        with mock.patch.object(visibility, "_send_once", Sender()):
            self.invoke()
        lease = cli.runlock.acquire("再来", path=str(pathlib.Path(self.dir.name, "trial.lock")))
        lease.release()

    def test_report_only_sends_nothing(self):
        with mock.patch.object(visibility, "_send_once", Sender(default=by_page)):
            self.invoke()
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, out = self.invoke("--report-only")
        self.assertEqual(code, 0)
        self.assertIn("搜索可见试跑报告", out)

    def test_sixteen_keywords_refuse_before_spending(self):
        self.table._records = [trial_record(n, f"词{n}") for n in range(1, 17)]
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke()
        self.assertIn("最多 15 个", str(code))
        self.assertIn("一分钱没花", str(code))
        self.assertFalse(pathlib.Path(self.dir.name, "state.json").exists())

    def test_missing_key_refuses_before_reading_anything(self):
        with mock.patch.dict(cli.os.environ, {"TIKHUB_API_KEY": ""}), \
                mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke()
        self.assertIn("TIKHUB_API_KEY", str(code))

    def test_table_without_the_column_or_keywords_spends_nothing(self):
        self.table = TrialTable([trial_record(1, "词")], with_column=False)
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, out = self.invoke()
        self.assertIn("没有可查的词", str(code))
        self.assertIn("还没有「搜索关键词」这一列", out)

    def test_state_dir_inside_the_repo_is_refused(self):
        repo = pathlib.Path(cli.__file__).resolve().parent
        with self.assertRaises(SystemExit) as caught:
            cli.cmd_search_trial(["--state-dir", str(repo / "tmp-trial")])
        self.assertIn("不能放在仓库目录里", str(caught.exception))
        self.assertFalse((repo / "tmp-trial").exists())

    def test_unknown_flags_and_rounds_are_refused(self):
        for args in (["--dryrun"], ["--round", "everything"], ["--round"],
                     ["--dry-run", "--report-only"]):
            with self.assertRaises(SystemExit) as caught:
                cli._search_trial_options(args)
            self.assertNotEqual(caught.exception.code, 0)
        self.assertEqual(cli._search_trial_options(["--round", "depth"])["round"], "depth")
        self.assertFalse(cli._search_trial_options([])["dry_run"])

    def test_main_routes_the_command(self):
        with mock.patch.object(cli, "load_env_or_exit", lambda: None), \
                mock.patch.object(cli, "cmd_search_trial", return_value=0) as command:
            self.assertEqual(cli.main(["cli.py", "search-trial", "--dry-run", "--table", "A"]), 0)
        command.assert_called_once_with(["--dry-run"], ["A"])


class TestTrialTables(unittest.TestCase):
    """读哪几张表：只有开了开关的；没有登记表就必须点名。"""

    class T:
        def __init__(self, table_id):
            self.table_id = table_id

    def pick(self, selected, rows, tables):
        with mock.patch.object(cli, "_tables", lambda _s: tables), \
                mock.patch.dict(cli._REGISTRY_ROWS, rows, clear=True):
            return cli._search_trial_tables(selected)

    def test_only_switched_on_tables_are_read(self):
        a, b = self.T("tblA"), self.T("tblB")
        rows = {"tblA": registry.Entry(label="A", search_check=True),
                "tblB": registry.Entry(label="B")}
        self.assertEqual(self.pick(None, rows, [("A", a), ("B", b)]), [("A", a)])

    def test_naming_a_table_that_is_off_is_refused(self):
        rows = {"tblB": registry.Entry(label="B")}
        with self.assertRaises(SystemExit) as caught:
            self.pick(["B"], rows, [("B", self.T("tblB"))])
        self.assertIn("没开「搜索检查」", str(caught.exception))

    def test_no_table_switched_on_is_refused(self):
        rows = {"tblB": registry.Entry(label="B")}
        with self.assertRaises(SystemExit) as caught:
            self.pick(None, rows, [("B", self.T("tblB"))])
        self.assertIn("没有一张表开了", str(caught.exception))

    def test_without_a_registry_tables_must_be_named(self):
        with self.assertRaises(SystemExit):
            self.pick(None, {}, [("A", self.T("tblA"))])
        a = self.T("tblA")
        self.assertEqual(self.pick(["A"], {}, [("A", a)]), [("A", a)])


class TestNothingScheduled(unittest.TestCase):
    """合并之后没有任何东西会自己跑起来。"""

    def test_no_workflow_or_deploy_file_mentions_the_trial(self):
        repo = pathlib.Path(cli.__file__).resolve().parent
        for path in [*(repo / ".github" / "workflows").glob("*.yml"), repo / "railpack.json"]:
            self.assertNotIn("search-trial", path.read_text(encoding="utf-8"), path.name)

    def test_only_the_trial_imports_the_search_module(self):
        repo = pathlib.Path(cli.__file__).resolve().parent
        users = [p.name for p in (repo / "xhsearch").glob("*.py")
                 if p.name != "visibility.py" and any(
                     line.lstrip().startswith(("import ", "from ")) and "visibility" in line
                     for line in p.read_text(encoding="utf-8").splitlines())]
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
