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

    def test_ad_type_names_are_matched_by_word_not_substring(self):
        self.assertEqual([providers.is_ad_type(k) for k in
                          ("ads", "AD_card", "native-advert", "note", "hot_query", "load", None)],
                         [True, True, True, False, False, False, False])

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
        # 建成空的多选（选项填的时候自己长出来），不用先定选项。
        self.assertEqual(missing[0].body(), {"field_name": "搜索关键词", "type": 4})

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
    """词是表的：一张表的词 = 它所有行里写过的词合起来；帖子 = 这张表所有查得了的行。"""

    def test_multi_select_options_are_taken_whole(self):
        self.assertEqual(visibility.split_keywords(["词甲", "a b", "#词丙"]), ["词甲", "a b", "词丙"])
        # 飞书读回来的多选格子按列类型整理成选项名的列表；选项里的逗号不拆。
        cell = visibility.keyword_cell(["a, b", "c"], visibility.COLUMN_MULTI)
        self.assertEqual(visibility.split_keywords(cell), ["a, b", "c"])

    def test_text_cell_with_a_mention_is_never_read_as_a_multi_select(self):
        """文本格子也是一个数组，@人 的分段带着 name：不能把人名当选项拿去搜。"""
        cell = [{"type": "text", "text": "词甲，词乙 "},
                {"type": "mention", "mentionType": "User", "name": "某同事",
                 "text": "@某同事", "token": "ou_x"}]
        self.assertEqual(visibility.split_keywords(cell), ["词甲", "词乙"])
        tidy = visibility.keyword_cell(cell, visibility.COLUMN_TEXT)
        self.assertEqual(visibility.split_keywords(tidy), ["词甲", "词乙"])
        info = visibility.intake([[(xhs_row(1), tidy), (xhs_row(2), None)]])
        self.assertEqual(info.keywords, ["词甲", "词乙"])
        # 只有 @人、没有字的格子：一个词都没有。
        self.assertEqual(visibility.split_keywords(cell[1:]), [])

    def test_a_sentence_in_a_text_cell_is_not_split_into_paid_words(self):
        split = visibility.split_keywords
        sentence = "how to pick a sunscreen for sensitive skin in summer"
        for cell in (sentence, "这张表先不查，等客户确认了再说。", "备注：词甲、词乙",
                     "词甲、" + "很" * 31, "词甲 还是 词乙？"):
            self.assertEqual(split(cell), [], cell)
            self.assertTrue(visibility.parse_cell(cell).sentence, cell)
        info = visibility.intake([[(xhs_row(1), sentence), (xhs_row(2), "词甲")]])
        self.assertEqual((info.keywords, info.sentence_cells, info.rows_with_keyword),
                         (["词甲"], 1, 1))
        self.assertEqual(len(info.posts), 2)
        # 一句话是唯一的格子：这张表一个词都没有，不查。
        self.assertEqual(visibility.intake([[(xhs_row(1), sentence)]]).keywords, [])

    def test_a_decimal_at_the_start_of_a_word_is_not_list_numbering(self):
        """「3.5mm耳机」开头的 3. 不是编号；削掉就成了另一个词，钱花在别的词上。"""
        self.assertEqual(visibility.split_keywords("3.5mm耳机"), ["3.5mm耳机"])
        self.assertEqual(visibility.split_keywords("2.4G无线鼠标，1.5米数据线"),
                         ["2.4G无线鼠标", "1.5米数据线"])
        self.assertEqual(visibility.split_keywords("1. 词甲\n2.词乙\n3）词丙\n4、词丁"),
                         ["词甲", "词乙", "词丙", "词丁"])

    def test_paid_only_needs_every_page_just_like_not_found(self):
        """页没翻全时，在已翻的页里只出现在付费位的帖子是「没查全」：没翻的页里可能自然排上了。"""
        partial = visibility.Listing(organic=["n-other"], paid={"n-ours"}, pages=1, complete=False)
        self.assertEqual(partial.result("n-ours"), (visibility.INCOMPLETE, None))
        full = visibility.Listing(organic=["n-other"], paid={"n-ours"}, pages=3, complete=True)
        self.assertEqual(full.result("n-ours"), (visibility.PAID, None))
        self.assertEqual(full.result("n-none"), (visibility.NOT_FOUND, None))
        self.assertEqual(partial.result("n-other")[1], 1)

    def test_lone_punctuation_and_numbering_never_become_keywords(self):
        split = visibility.split_keywords
        self.assertEqual(split("词甲 / 词乙"), ["词甲", "词乙"])
        self.assertEqual(split("词甲 - 词乙、-、/、·"), ["词甲 - 词乙"])
        self.assertEqual(split("词甲|词乙｜词丙"), ["词甲", "词乙", "词丙"])
        self.assertEqual(split("#词甲#词乙 ＃词丙"), ["词甲", "词乙", "词丙"])
        self.assertEqual(split("1. 词甲\n2）词乙\n3、词丙"), ["词甲", "词乙", "词丙"])
        self.assertEqual(split(["-", "词甲", " / "]), ["词甲"])
        self.assertEqual(split("618、双11"), ["618", "双11"])

    def test_a_spaced_word_is_one_keyword_however_the_rows_are_filled(self):
        """空格不拆：只写一个带空格的词，和它跟别的词写在一起，得到的是同一个词。"""
        info = visibility.intake([[(xhs_row(1), "cGMP  因子"), (xhs_row(2), "cGMP 因子、词甲"),
                                   (xhs_row(3), ["cGMP 因子"])]])
        self.assertEqual(info.keywords, ["cGMP 因子", "词甲"])

    def test_text_splits_on_the_listed_separators_and_keeps_inner_spaces(self):
        split = visibility.split_keywords
        self.assertEqual(split("快充、小片；充电宝,磁吸，薄;轻\n小"),
                         ["快充", "小片", "充电宝", "磁吸", "薄", "轻", "小"])
        self.assertEqual(split("cGMP 因子、 #快充 "), ["cGMP 因子", "快充"])
        self.assertEqual(split([{"type": "text", "text": "词甲，"}, {"type": "text", "text": "词乙"}]),
                         ["词甲", "词乙"])
        self.assertEqual(split("词甲,"), ["词甲"])

    def test_whitespace_never_splits_a_text_cell(self):
        split = visibility.split_keywords
        self.assertEqual(split("快充  小片\u3000充电宝\t磁吸"), ["快充 小片 充电宝 磁吸"])
        self.assertEqual(split("#快充小片"), ["快充小片"])
        self.assertEqual(split("a b,"), ["a b"])

    def test_empty_cells_and_repeats(self):
        split = visibility.split_keywords
        self.assertEqual([split(None), split(""), split(" # "), split([]), split("，、")],
                         [[], [], [], [], []])
        self.assertEqual(split("SPX、spx、Spx"), ["SPX"])

    def test_keywords_are_the_union_over_the_tables_rows_in_first_seen_order(self):
        same = visibility.intake([[(xhs_row(1), "词甲、词乙"), (xhs_row(2), "词甲、词乙"),
                                   (xhs_row(3), "词甲、词乙")]])
        one = visibility.intake([[(xhs_row(1), None), (xhs_row(2), "词甲、词乙"), (xhs_row(3), "")]])
        mixed = visibility.intake([[(xhs_row(1), ["词甲"]), (xhs_row(2), "词乙；词甲"),
                                    (xhs_row(3), None)]])
        for info in (same, one, mixed):
            self.assertEqual(info.keywords, ["词甲", "词乙"])
            # 每一行都是这张表的帖子（格子空着的也是），每一篇都带着整张表的词。
            self.assertEqual([p.note_id for p in info.posts], [nid(1), nid(2), nid(3)])
            self.assertEqual({p.keywords for p in info.posts}, {("词甲", "词乙")})
        self.assertEqual((same.rows_with_keyword, one.rows_with_keyword, one.rows), (3, 1, 3))

    def test_each_table_has_its_own_set_and_a_shared_word_is_one_keyword(self):
        info = visibility.intake([
            [(xhs_row(1), "SPX、词甲"), (xhs_row(2), None)],
            [(xhs_row(3), "词乙，spx")],
        ])
        self.assertEqual(info.keywords, ["SPX", "词甲", "词乙"])
        self.assertEqual([p.keywords for p in info.posts],
                         [("SPX", "词甲"), ("SPX", "词甲"), ("词乙", "SPX")])
        self.assertEqual((info.tables, info.tables_with_keywords), (2, 2))

    def test_table_without_any_keyword_is_not_looked_at(self):
        resolved = []
        short = rows_mod.Row(record_id="s", link_cell="https://xhslink.cn/o/abcDEF",
                             publish_time_ms=1_700_000_000_000)
        info = visibility.intake([[(xhs_row(1), ""), (short, None)], [(xhs_row(2), "词甲")]],
                                 resolve=lambda url: resolved.append(url) or nid(9))
        self.assertEqual((info.keywords, [p.note_id for p in info.posts]), (["词甲"], [nid(2)]))
        self.assertEqual(info.skipped, {visibility.SKIP_NO_WORDS: 2})
        self.assertEqual((info.tables, info.tables_with_keywords, resolved), (2, 1, []))

    def test_rows_that_cannot_be_checked_are_counted_by_reason(self):
        douyin = rows_mod.Row(record_id="d", link_cell="https://www.douyin.com/video/7412345678901234567")
        info = visibility.intake([[
            (xhs_row(1), "词甲"),
            (douyin, "词甲"),
            (xhs_row(2, refresh_status=rows_mod.STATUS_GONE), None),
            (xhs_row(3, refresh_status=rows_mod.STATUS_SUSPECT), None),
            (xhs_row(4), ["很" * 31]),
        ]])
        self.assertEqual([p.note_id for p in info.posts], [nid(1), nid(4)])
        self.assertEqual(info.skipped, {visibility.SKIP_NOT_XHS: 1, visibility.SKIP_DEAD: 2})
        # 像整句话的词不收，但写它的那一行照样是这张表的帖子。
        self.assertEqual((info.keywords, info.too_long), (["词甲"], 1))

    def test_same_post_twice_is_one_post_and_case_does_not_split_a_keyword(self):
        info = visibility.intake([[(xhs_row(1), "SPX"), (xhs_row(1), "spx"), (xhs_row(2), "Spx")]])
        self.assertEqual(info.keywords, ["SPX"])
        self.assertEqual(len(info.posts), 2)

    def test_short_links_are_expanded_for_free_or_left_pending(self):
        short = rows_mod.Row(record_id="s", link_cell="https://xhslink.cn/o/abcDEF",
                             publish_time_ms=1_700_000_000_000)
        pending = visibility.intake([[(short, "词甲")]])
        self.assertEqual((pending.short_links, pending.posts, pending.keywords),
                         (1, [], ["词甲"]))
        resolved = visibility.intake([[(short, "词甲")]], resolve=lambda url: nid(9))
        self.assertEqual([p.note_id for p in resolved.posts], [nid(9)])
        # 一篇对得了的帖子都没有的表：它的词不占名额、不花钱。
        failed = visibility.intake([[(short, "词甲")]], resolve=lambda url: None)
        self.assertEqual((failed.skipped, failed.keywords), ({visibility.SKIP_NO_ID: 1}, []))

    def test_empty_publish_time_is_not_matched_and_its_short_link_is_not_expanded(self):
        """设计第 9 条：发布时间空着的行对不了。只有这种行的表，它的词也不占名额。"""
        resolved = []
        short = rows_mod.Row(record_id="s", link_cell="https://xhslink.cn/o/abcDEF")
        info = visibility.intake(
            [[(xhs_row(1, publish_time_ms=None), "只有空时间的词"), (short, "只有空时间的词")],
             [(xhs_row(2), "词甲"), (xhs_row(3, publish_time_ms=None), "词甲")]],
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
        self.assertEqual(data.pop("rounds"), {
            "first": {"at": 1_800_000_000, "kw": [self.words[0][1]]}})
        path.write_text(json.dumps(data), encoding="utf-8")
        state = fresh_state(self.dir.name)
        self.assertEqual(state.calls, 3)
        self.assertEqual(state.round_at("first"), 1_800_000_000.0)     # 退回页上的时间

    # —— 一轮的词：定下就不换，只往空位里补 ——

    def test_round_remembers_the_keywords_it_selected_as_digests(self):
        run("depth", self.words[2:3], self.state, Sender(default=by_page))
        saved = json.loads(pathlib.Path(self.dir.name, "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["rounds"]["depth"]["kw"], [self.words[2][1]])
        self.assertNotIn("词3", json.dumps(saved, ensure_ascii=False))

    def test_second_run_with_another_subset_fills_free_slots_and_repays_nothing(self):
        """depth 最多两个词：先只查了 K03，再拿全表跑——K03 留着、K01 补进空位、K02 不查。"""
        first, _logs = run("depth", self.words[2:3], self.state, Sender(default=by_page))
        self.assertEqual(first.requests, 10)
        sender = Sender(default=by_page)
        again, logs = run("depth", self.words, fresh_state(self.dir.name), sender)
        self.assertEqual((again.fetched, again.reused, again.requests), (10, 10, 10))
        state = fresh_state(self.dir.name)
        self.assertEqual(state.calls, 20)                     # 这一轮说好的最多 20 次
        self.assertEqual(state.selection("depth"),
                         ([self.words[2][1], self.words[0][1]], True))
        self.assertTrue(any("另外 13 个词这一轮不查" in line for line in logs))
        self.assertEqual(state.page("depth", self.words[1][1], 1), None)
        # 第三次只点了排不进去的词：一个请求都不发，定下的词也不变。
        third, _logs = run("depth", self.words[1:2], state, Sender())
        self.assertEqual((third.requests, third.fetched, third.todo), (0, 0, 0))
        self.assertEqual(state.selection("depth")[0], [self.words[2][1], self.words[0][1]])
        self.assertEqual(state.calls, 20)

    def test_second_run_of_the_first_round_adds_the_other_tables_keywords(self):
        run("first", self.words[:1], self.state, Sender(default=by_page))
        sender = Sender(default=by_page)
        again, _logs = run("first", self.words[1:3], self.state, sender)
        self.assertEqual((again.requests, again.reused, self.state.calls), (6, 0, 9))
        self.assertEqual(self.state.selection("first")[0], [d for _w, d in self.words[:3]])
        whole, _logs = run("first", self.words[:3], self.state, Sender())    # 再发就炸
        self.assertEqual((whole.reused, whole.requests), (9, 0))

    def test_old_state_file_without_a_selection_resumes_without_paying_again(self):
        run("depth", self.words[2:3], self.state, Sender(default=by_page))
        path = pathlib.Path(self.dir.name, "state.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["rounds"]["depth"]["kw"]                    # 早先的进度文件长这样
        path.write_text(json.dumps(data), encoding="utf-8")
        state = fresh_state(self.dir.name)
        self.assertEqual(state.selection("depth"), ([self.words[2][1]], False))
        again, _logs = run("depth", self.words, state, Sender(default=by_page))
        self.assertEqual((again.fetched, again.reused, again.requests), (10, 10, 10))
        self.assertEqual(state.selection("depth"),
                         ([self.words[2][1], self.words[0][1]], True))

    def test_cap_is_still_checked_first_when_a_round_is_topped_up(self):
        run("depth", self.words[2:3], self.state, Sender(default=by_page))
        self.state.data["calls"] = visibility.MAX_CALLS
        result, _logs = run("depth", self.words, self.state, Sender())       # 再发就炸
        self.assertTrue(result.stopped)
        self.assertEqual((result.requests, self.state.calls), (0, visibility.MAX_CALLS))

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
QUESTION = "发布 3 天内的帖子，在这张表要查的词下面，综合排序前 3 页里看不看得见"


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
        return visibility.Post(nid(n), (self.word,),
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
            "问题": QUESTION,
            "3天内的帖子": 3, "搜到的": 2, "只在付费位的": 0, "没搜到的": 1, "没查全的": 0})
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
                 visibility.Post(nid(999), (other,), days_before(2), 30)]
        report = self.report(posts)
        self.assertEqual(report["帖子"], 2)
        self.assertEqual(report["回答"], {
            "问题": QUESTION,
            "3天内的帖子": 2, "搜到的": 1, "只在付费位的": 0, "没搜到的": 0, "没查全的": 1})
        block = report["各轮"]["first"]
        self.assertEqual(block["按评论数"]["评论数到 20 条"]["没查全"], 1)
        self.assertEqual(block["按词"][1]["按帖龄"]["3天内"]["没查全"], 1)
        text = visibility.render_report(report)
        self.assertIn("至少在一个词下面搜到 1 篇（查全的 1 篇里占 100%），哪个词下面都没搜到 0 篇，没查全（说不准）1 篇", text)
        self.assertIn("- K02：还没查，一页都没拿到\n"
                      "  全部：1 篇，搜到 0 篇；没查全（说不准，不算进比例）1 篇\n"
                      "  按帖龄：3天内 1 篇，搜到 0 篇；没查全（说不准，不算进比例）1 篇", text)

    def test_posts_under_a_keyword_the_cap_stopped_before_are_incomplete(self):
        other = "另一个词"
        self.state.register([other])
        self.state.data["calls"] = visibility.MAX_CALLS - 3
        pairs = [(self.word, self.digest), (other, visibility.keyword_hash(other))]
        sender = Sender(default=by_page)
        result, _logs = run("first", pairs, self.state, sender)
        self.assertEqual((len(sender.requests), bool(result.stopped)), (3, True))
        # 帖龄按这一轮跑的那一刻算（这个词没有自己的第 1 页时间）。
        posts = [visibility.Post(nid(999), (other,), days_before(2)),
                 visibility.Post(nid(998), (other,), days_before(5))]
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
        later = visibility.Post(nid(999), (self.word,), days_before(-2))   # 查完两天后才发
        report = self.report([self.post(105, 2), later])
        self.assertEqual(report["回答"], {
            "问题": QUESTION,
            "3天内的帖子": 1, "搜到的": 1, "只在付费位的": 0, "没搜到的": 0, "没查全的": 0})
        block = report["各轮"]["first"]
        self.assertEqual(block["查之后才发的帖子（不算）"], 1)
        self.assertEqual(sum(b["篇数"] for b in block["按帖龄"].values()), 1)
        self.assertIn("有 1 篇帖子是这一轮查完之后才发的", visibility.render_report(report))
        # 没查到的词也一样：这一轮跑完之后才发的不算。
        other = "另一个词"
        self.state.register([other])
        self.state.mark_round("first", AT, [self.digest, visibility.keyword_hash(other)])
        report = self.report([visibility.Post(nid(998), (other,), days_before(-2)),
                              visibility.Post(nid(997), (other,), days_before(1))])
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
        rounds = self.report([visibility.Post(nid(999), (self.word,), days_before(-2))])["各轮"]
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
        self.assertEqual(tally, {"对得上的（帖子×词）": 4, "档位一样": 1, "档位变了": 3,
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
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里至少在一个词下面搜到 1 篇（50%），"
                      "哪个词下面都没搜到 1 篇", text)
        self.assertIn("first：unknown（HTTP 400）：1 次", text)
        self.assertIn("余额少了 $0.03", text)
        self.assertIn("外部语料抓取也在共用同一个余额", text)
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


class TestTableMatching(unittest.TestCase):
    """2026-11-16 负责人定的：每个词拉前几页，再拿这张表**所有**发布帖去对。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = fresh_state(self.dir.name)
        self.a, self.b, self.c = "词甲", "词乙", "词丙"
        self.state.register([self.a, self.b, self.c])

    def keep(self, word, page, ids, round_name="first", at=AT):
        self.state.keep_page(round_name, visibility.keyword_hash(word), page, {
            "notes": [{"note_id": nid(i), "ad": False} for i in ids],
            "items": len(ids), "search_id": "s"}, at)

    def full(self, word, start, **kwargs):
        for page in (1, 2, 3):
            self.keep(word, page, range(start + page * 100, start + page * 100 + 20), **kwargs)

    def post(self, n, words, age_days=2.0, comments=None):
        return visibility.Post(nid(n), tuple(words), days_before(age_days), comments)

    def first(self, posts):
        report = visibility.build_report(self.state, posts, tz=BJ, now=AT)
        return report, report["各轮"]["first"]

    def test_a_match_seen_days_later_is_not_backdated_to_the_first_search(self):
        """一轮隔了几天才续跑完：第 2 天在词甲下没搜到、第 5 天在词乙下搜到的帖子，
        不能记成「3 天内搜到」；只在词甲那一刻查过的另一篇也说不了「3 天内没搜到」。"""
        day = 86400
        self.full(self.a, 1000, at=AT)                       # 词甲：发布后第 2 天查的
        self.full(self.b, 2000, at=AT + 3 * day)             # 词乙：第 5 天才查
        table = (self.a, self.b)
        late_hit = visibility.Post(nid(2105), table, days_before(2.0), None)   # 只在词乙下出现
        nowhere = visibility.Post(nid(9999), table, days_before(2.0), None)
        early_hit = visibility.Post(nid(1105), table, days_before(2.0), None)  # 第 2 天就在词甲下
        report = visibility.build_report(self.state, [late_hit, nowhere, early_hit], tz=BJ,
                                         now=AT + 3 * day)
        block = report["各轮"]["first"]
        day3, day7 = block["按帖龄"]["3天内"], block["按帖龄"]["4到7天"]
        self.assertEqual((day3["篇数"], day3["前20"], day3["没查全"], day3["没搜到"]), (2, 1, 1, 0))
        self.assertEqual((day7["篇数"], day7["前20"]), (1, 1))
        self.assertEqual((report["回答"]["3天内的帖子"], report["回答"]["搜到的"],
                          report["回答"]["没搜到的"], report["回答"]["没查全的"]), (2, 1, 0, 1))

    def test_every_post_is_checked_against_every_keyword_of_its_table(self):
        self.full(self.a, 1000)                    # 词甲：1100.. 1200.. 1300..
        self.full(self.b, 2000)                    # 词乙：2100.. 2200.. 2300..
        table = (self.a, self.b)
        posts = [self.post(1105, table), self.post(2205, table), self.post(9999, table),
                 self.post(2301, table, age_days=5), self.post(8888, table, age_days=30)]
        report, block = self.first(posts)
        self.assertEqual(report["帖子"], 5)
        self.assertEqual(report["回答"], {
            "问题": QUESTION, "3天内的帖子": 3, "搜到的": 2, "只在付费位的": 0,
            "没搜到的": 1, "没查全的": 0})
        day3 = block["按帖龄"]["3天内"]
        self.assertEqual((day3["篇数"], day3["前20"], day3["21到40"], day3["没搜到"]), (3, 1, 1, 1))
        self.assertEqual(block["按帖龄"]["4到7天"]["41到60"], 1)
        self.assertEqual(block["按帖龄"]["更早"]["没搜到"], 1)
        # 每个词：这张表的 5 篇里，在它下面出现了几篇、排在哪一档。
        rows = {row["词"]: row["帖子"] for row in block["按词"]}
        self.assertEqual((rows["K01"]["篇数"], rows["K01"]["前20"], rows["K01"]["没搜到"]), (5, 1, 4))
        self.assertEqual((rows["K02"]["篇数"], rows["K02"]["21到40"], rows["K02"]["41到60"],
                          rows["K02"]["没搜到"]), (5, 1, 1, 3))
        text = visibility.render_report(report)
        self.assertIn("- K01：这个词翻了 3 页，里面一共 60 篇笔记（广告不算；查全了）\n"
                      "  全部：5 篇，搜到 1 篇（20%）：前20 1\n", text)

    def test_the_report_reads_plainly_when_nothing_is_found(self):
        """2026-11-25 第一轮实测的样子：每页有几条类型是 `ads` 的广告（认不出笔记 ID），
        我们的帖子一篇都没出现。运营看不懂的几处：「写了这个词的表的帖子」、每个词「拿到 3 页」
        和总共 12 页对不上、广告被说成「认不出笔记的」、没有比例。"""
        self.state.mark_round("first", AT, [visibility.keyword_hash(w) for w in (self.a, self.b)])
        for word, start in ((self.a, 1000), (self.b, 2000)):
            for page in (1, 2, 3):
                ids = range(start + page * 100, start + page * 100 + 18)
                self.state.keep_page("first", visibility.keyword_hash(word), page, {
                    "notes": [{"note_id": nid(i), "ad": False} for i in ids],
                    "items": 20, "other_items": 2, "model_types": {"note": 18, "ads": 2},
                    "item_keys": ["ads", "model_type", "note"], "search_id": "s"}, AT)
        table = (self.a, self.b)
        posts = [self.post(9000 + i, table, age_days=age) for i, age in enumerate((1, 2, 5, 30))]
        report, block = self.first(posts)
        text = visibility.render_report(report)
        self.assertIn("至少在一个词下面搜到 0 篇（0%），哪个词下面都没搜到 2 篇。", text)
        self.assertIn("一篇都没搜到：先别急着下结论", text)
        self.assertIn("页：2 个词，每个词翻 3 页，一共计划 6 页；拿到 6", text)
        self.assertIn("结果里有 12 条是广告（类型名是 ads 一类），广告不算名次。"
                      "认不出是哪篇笔记的 12 条，就是这些广告。", text)
        self.assertNotIn("写了这个词的表的帖子", text)
        self.assertIn("每个词都拿这张表的全部帖子去对，不是只对标题、正文里写了这个词的帖子", text)
        self.assertIn("- K01：这个词翻了 3 页，里面一共 54 篇笔记（广告不算；查全了）\n"
                      "  全部：4 篇，搜到 0 篇（0%）\n"
                      "  按帖龄：3天内 2 篇，搜到 0 篇（0%）；4到7天 1 篇，搜到 0 篇（0%）；"
                      "更早 1 篇，搜到 0 篇（0%）", text)
        self.assertIn("- 3天内：2 篇，搜到 0 篇（0%）", text)
        self.assertEqual((block["词数"], block["每个词翻几页"]), (2, 3))

    def test_a_partly_found_group_shows_its_share_without_the_unchecked_posts(self):
        bucket = {**visibility._blank(), "篇数": 5, "前20": 1, "41到60": 1, "没搜到": 1,
                  "付费位": 1, "没查全": 1}
        self.assertEqual(visibility._counts(bucket),
                         "5 篇，搜到 2 篇（查全的 4 篇里占 50%）：前20 1 · 41到60 1；"
                         "只在付费位（广告）1 篇，不算搜到；没查全（说不准，不算进比例）1 篇")
        self.assertEqual(visibility._counts(visibility._blank()), "0 篇")

    def test_best_rank_wins_when_a_post_shows_under_two_keywords(self):
        self.keep(self.a, 1, [*range(100, 150), 7])       # 第 51 位
        self.keep(self.a, 2, [])
        self.keep(self.b, 1, [1, 2, 7])                   # 第 3 位
        self.keep(self.b, 2, [])
        _report, block = self.first([self.post(7, (self.a, self.b))])
        self.assertEqual(block["按帖龄"]["3天内"]["前20"], 1)
        self.assertEqual(block["按帖龄"]["3天内"]["41到60"], 0)

    def test_not_found_needs_every_keyword_complete_else_it_cannot_be_judged(self):
        self.full(self.a, 1000)
        self.keep(self.b, 1, range(2100, 2120))           # 词乙只拿到 1 页
        table = (self.a, self.b, self.c)                  # 词丙一页都没拿到
        self.state.mark_round("first", AT, [visibility.keyword_hash(w) for w in table])
        posts = [self.post(1105, table), self.post(2105, table), self.post(9999, table)]
        report, block = self.first(posts)
        self.assertEqual(report["回答"], {
            "问题": QUESTION, "3天内的帖子": 3, "搜到的": 2, "只在付费位的": 0,
            "没搜到的": 0, "没查全的": 1})
        rows = {row["词"]: row["帖子"] for row in block["按词"]}
        self.assertEqual((rows["K01"]["前20"], rows["K01"]["没搜到"]), (1, 2))
        self.assertEqual((rows["K02"]["前20"], rows["K02"]["没查全"]), (1, 2))
        self.assertEqual(rows["K03"]["没查全"], 3)
        # 补齐之后才说得出「没搜到」。
        self.keep(self.b, 2, [])
        self.keep(self.c, 1, [])
        report, _block = self.first(posts)
        self.assertEqual((report["回答"]["没搜到的"], report["回答"]["没查全的"]), (1, 0))

    def test_keyword_added_to_the_table_after_the_round_makes_unfound_posts_incomplete(self):
        self.full(self.a, 1000)
        self.state.mark_round("first", AT, [visibility.keyword_hash(self.a)])
        table = (self.a, self.b)                          # 词乙是查完之后才加进表的
        posts = [self.post(1105, table), self.post(9999, table)]
        report, block = self.first(posts)
        self.assertEqual(report["回答"], {
            "问题": QUESTION, "3天内的帖子": 2, "搜到的": 1, "只在付费位的": 0,
            "没搜到的": 0, "没查全的": 1})
        self.assertEqual(block["表里有、这一轮没查的词"], 1)
        self.assertEqual([row["词"] for row in block["按词"]], ["K01"])
        self.assertIn("表里现在有 1 个词这一轮没查过", visibility.render_report(report))
        # 表里没多出词：照旧是「没搜到」。
        report, block = self.first([self.post(9999, (self.a,))])
        self.assertEqual((report["回答"]["没搜到的"], block["表里有、这一轮没查的词"]), (1, 0))

    def test_depth_round_that_is_full_does_not_wait_for_the_other_keywords(self):
        for word in (self.a, self.b):
            for page in range(1, 11):
                self.keep(word, page, range(page * 100, page * 100 + 20), round_name="depth")
        self.state.mark_round("depth", AT, [visibility.keyword_hash(w) for w in (self.a, self.b)])
        report = visibility.build_report(
            self.state, [self.post(9999, (self.a, self.b, self.c))], tz=BJ, now=AT)
        block = report["各轮"]["depth"]
        self.assertEqual(block["按帖龄"]["3天内"]["没搜到"], 1)
        self.assertEqual(block["表里有、这一轮没查的词"], 0)

    def test_paid_slot_only_is_not_a_verdict_while_a_keyword_is_incomplete(self):
        digest = visibility.keyword_hash(self.a)
        self.state.keep_page("first", digest, 1, {
            "notes": [{"note_id": nid(1), "ad": False}, {"note_id": nid(7), "ad": True}],
            "items": 2, "search_id": "s"}, AT)
        self.keep(self.a, 2, [])
        table = (self.a, self.b)                          # 词乙选了，一页都没拿到
        self.state.mark_round("first", AT, [visibility.keyword_hash(w) for w in table])
        report, block = self.first([self.post(7, table)])
        self.assertEqual((report["回答"]["只在付费位的"], report["回答"]["没查全的"]), (0, 1))
        rows = {row["词"]: row["帖子"] for row in block["按词"]}
        self.assertEqual((rows["K01"]["付费位"], rows["K02"]["没查全"]), (1, 1))
        # 词乙查全了、也没有：这才是「只在付费位」。
        self.keep(self.b, 1, [])
        report, _block = self.first([self.post(7, table)])
        self.assertEqual((report["回答"]["只在付费位的"], report["回答"]["没查全的"]), (1, 0))

    def test_same_keyword_in_two_tables_is_searched_once_and_matched_per_table(self):
        self.full(self.a, 1000)
        self.full(self.b, 2000)
        self.full(self.c, 3000)
        one, two = (self.a, self.b), (self.a, self.c)     # 两张表都写了词甲
        posts = [self.post(1105, one), self.post(3105, one),     # 3105 在词丙下面，但词丙不是它那张表的
                 self.post(1106, two), self.post(3106, two), self.post(2106, two)]
        report, block = self.first(posts)
        self.assertEqual(report["词"], 3)
        self.assertEqual(block["页"]["计划"], 9)           # 词甲只算一份
        day3 = block["按帖龄"]["3天内"]
        self.assertEqual((day3["篇数"], day3["前20"], day3["没搜到"]), (5, 3, 2))
        rows = {row["词"]: row["帖子"] for row in block["按词"]}
        self.assertEqual((rows["K01"]["篇数"], rows["K01"]["前20"]), (5, 2))   # 两张表的帖子都对
        self.assertEqual((rows["K02"]["篇数"], rows["K02"]["前20"]), (2, 0))
        self.assertEqual((rows["K03"]["篇数"], rows["K03"]["前20"]), (3, 1))

    def test_same_post_in_two_tables_counts_once_with_both_sets(self):
        self.full(self.a, 1000)
        self.full(self.b, 2000)
        report, block = self.first([self.post(2105, (self.a,)), self.post(2105, (self.b,))])
        self.assertEqual(report["帖子"], 1)
        self.assertEqual(block["按帖龄"]["3天内"]["前20"], 1)

    def test_post_published_between_two_keyword_searches_is_only_matched_with_the_later(self):
        self.full(self.a, 1000)
        self.full(self.b, 2000, at=AT + 3 * 86400)        # 词乙是三天后补查的
        table = (self.a, self.b)
        newer = self.post(9999, table, age_days=-1)       # 两次之间发的
        much_newer = self.post(9998, table, age_days=-5)  # 都查完了才发
        _report, block = self.first([newer, much_newer])
        rows = {row["词"]: row["帖子"] for row in block["按词"]}
        self.assertEqual((rows["K01"]["篇数"], rows["K02"]["篇数"], rows["K02"]["没搜到"]), (0, 1, 1))
        self.assertEqual(block["查之后才发的帖子（不算）"], 1)
        self.assertEqual(sum(b["篇数"] for b in block["按帖龄"].values()), 1)

    def test_round_that_searched_none_of_a_tables_keywords_leaves_its_posts_out(self):
        for page in range(1, 11):
            self.keep(self.a, page, range(page * 100, page * 100 + 20), round_name="depth")
        self.state.mark_round("depth", AT, [visibility.keyword_hash(self.a)])
        report = visibility.build_report(
            self.state, [self.post(105, (self.a, self.b)), self.post(999, (self.c,))], tz=BJ, now=AT)
        block = report["各轮"]["depth"]
        self.assertEqual([row["词"] for row in block["按词"]], ["K01"])
        self.assertEqual(sum(b["篇数"] for b in block["按帖龄"].values()), 1)
        self.assertEqual(block["这一轮没查它那张表的词的帖子（不算）"], 1)
        self.assertIn("这一轮一个都没轮到", visibility.render_report(report))

    def test_state_file_from_the_per_post_design_still_loads_and_reports(self):
        """改动之前（一篇帖子配一个词）存下的进度文件：形状没变，页是按词记的，照样能用。"""
        digest = visibility.keyword_hash(self.a)
        old = {"version": 1, "trial": visibility.TRIAL_NAME, "calls": 3, "yuan": 0.216,
               "keywords": {digest: 1},
               "pages": {f"first|{digest}|{page}": {
                   "at": int(AT), "ids": [nid(i) for i in range(page * 100, page * 100 + 20)],
                   "ads": [], "items": 20, "other": 0, "types": {}, "keys": [],
                   "sid": {"search_id": "s"}} for page in (1, 2, 3)},
               "rounds": {"first": {"at": int(AT), "kw": [digest]}},
               "failures": [], "balance": []}
        path = pathlib.Path(self.dir.name, "old", "state.json")
        path.parent.mkdir()
        path.write_text(json.dumps(old), encoding="utf-8")
        state = visibility.State.load(path)
        self.assertEqual((state.calls, state.yuan), (3, 0.216))
        # 那时候只有「填了这个词的那一篇」被对过；现在整张表的帖子都拿去对同一批页。
        posts = [self.post(105, (self.a,)), self.post(205, (self.a,)), self.post(999, (self.a,))]
        report = visibility.build_report(state, posts, tz=BJ, now=AT)
        day3 = report["各轮"]["first"]["按帖龄"]["3天内"]
        self.assertEqual((day3["前20"], day3["21到40"], day3["没搜到"]), (1, 1, 1))
        # 接着跑：拿到过的页不付第二次钱；新词照样受 15 个的上限。
        result, _logs = run("first", [(self.a, digest)], state, Sender())
        self.assertEqual((result.reused, result.requests), (3, 0))
        self.assertIn("最多 15 个", state.register([f"新词{i}" for i in range(15)]))
        self.assertEqual(state.data["keywords"], {digest: 1})


class TestReportSelection(unittest.TestCase):
    """报告里一轮算哪几个词：跑的时候记下的那几个，不是「编号靠前的」。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = fresh_state(self.dir.name)
        self.words = words(3)
        self.state.register([w for w, _d in self.words])
        self.posts = [visibility.Post(nid(900 + i), (word,), days_before(2))
                      for i, (word, _d) in enumerate(self.words, start=1)]
        self.bad = transport.Response(400, "application/json",
                                      json.dumps({"detail": {"code": 400, "message": "x"}}))

    def depth(self, state=None):
        report = visibility.build_report(state or self.state, self.posts, tz=BJ, now=AT)
        return report["各轮"]["depth"], visibility.render_report(report)

    def forget_selection(self) -> visibility.State:
        """把进度文件改回早先的样子：`rounds` 里没有记这一轮选了哪几个词。"""
        self.state.save()
        path = pathlib.Path(self.dir.name, "state.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data["rounds"].values():
            entry.pop("kw", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        return fresh_state(self.dir.name)

    def test_subset_run_reports_only_the_keyword_it_selected(self):
        """K01–K03 都编了号，depth 只选了 K03 并翻满 10 页：报告只列 K03，一页不差。"""
        result, _logs = run("depth", self.words[2:], self.state, Sender(default=by_page))
        self.assertTrue(result.clean)
        block, text = self.depth()
        self.assertEqual([row["词"] for row in block["按词"]], ["K03"])
        self.assertEqual(block["页"], {"计划": 10, "拿到": 10, "还差": 0, "失败过": 0})
        self.assertTrue(block["词是当时记下的"])
        self.assertEqual(sum(b["篇数"] for b in block["按帖龄"].values()), 1)
        section = text.split("（depth）")[1]
        self.assertNotIn("K01", section)
        self.assertNotIn("还没查", section)

    def test_selected_keyword_whose_first_page_failed_stays_in_the_report(self):
        """只选了 K03、它的第 1 页失败：报告列的还是 K03，它的帖子记成「没查全」。"""
        result, _logs = run("depth", self.words[2:], self.state, Sender([self.bad, self.bad]))
        self.assertEqual(result.failed, 1)
        block, _text = self.depth()
        self.assertEqual([row["词"] for row in block["按词"]], ["K03"])
        self.assertEqual(block["页"], {"计划": 10, "拿到": 0, "还差": 10, "失败过": 1})
        self.assertEqual(block["按帖龄"]["3天内"]["没查全"], 1)
        self.assertEqual(block["按帖龄"]["3天内"]["篇数"], 1)

    def test_old_state_file_keeps_the_keyword_whose_first_page_failed(self):
        run("depth", self.words[2:], self.state, Sender([self.bad, self.bad]))
        block, text = self.depth(self.forget_selection())
        self.assertFalse(block["词是当时记下的"])
        # 失败记录认得出 K03；另一个是从前往后补的（旧文件分不清，报告里明说是推的）。
        self.assertEqual([row["词"] for row in block["按词"]], ["K01", "K03"])
        self.assertEqual(block["按词"][1]["按帖龄"]["3天内"]["没查全"], 1)
        self.assertIn("没记当时查的是哪几个词", text)

    def test_old_state_file_with_pages_still_reports_them(self):
        run("depth", self.words[2:], self.state, Sender(default=by_page))
        old = self.forget_selection()
        block, _text = self.depth(old)
        rows = {row["词"]: row for row in block["按词"]}
        self.assertEqual((rows["K03"]["拿到几页"], rows["K03"]["查全了"]), (10, True))
        self.assertEqual(block["页"]["拿到"], 10)
        # 连 rounds 这一项都没有的更老的文件也一样读得出来。
        path = pathlib.Path(self.dir.name, "state.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["rounds"]
        path.write_text(json.dumps(data), encoding="utf-8")
        block, _text = self.depth(fresh_state(self.dir.name))
        self.assertIn("K03", [row["词"] for row in block["按词"]])

    def test_report_after_a_second_subset_lists_the_whole_selection(self):
        run("depth", self.words[2:], self.state, Sender(default=by_page))
        run("depth", self.words, self.state, Sender(default=by_page))
        block, _text = self.depth()
        self.assertEqual([row["词"] for row in block["按词"]], ["K01", "K03"])
        self.assertEqual(block["页"], {"计划": 20, "拿到": 20, "还差": 0, "失败过": 0})

    def test_building_a_report_does_not_touch_the_state(self):
        run("depth", self.words[2:], self.state, Sender(default=by_page))
        before = json.dumps(self.state.data, sort_keys=True)
        self.depth()
        self.assertEqual(json.dumps(self.state.data, sort_keys=True), before)


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
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里至少在一个词下面搜到 1 篇（50%），"
                      "哪个词下面都没搜到 1 篇", out)
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
        # 词是表的：第一个词没查到，但另一个词下面搜到的那一篇照样算搜到；
        # 两个词下面都没有的那一篇，因为有一个词没查全，说不准。
        self.assertIn("至少在一个词下面搜到 1 篇（查全的 1 篇里占 100%），哪个词下面都没搜到 0 篇，没查全（说不准）1 篇", out)
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
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里至少在一个词下面搜到 1 篇（50%），"
                      "哪个词下面都没搜到 1 篇。", out)
        self.assertIn("有 1 篇帖子是这一轮查完之后才发的", out)

    def test_unreadable_first_page_exits_non_zero_after_one_call(self):
        odd = [{"model_type": "note", "note": {"noteId": nid(n)}} for n in range(100, 120)]
        sender = Sender([ok(odd)])
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual((code, len(sender.requests)), (1, 1))
        self.assertIn("中途停了", out)
        self.assertIn("不能当成「没搜到」", out)
        self.assertNotIn("都没搜到 1 篇", out)

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

    def test_table_subset_round_reports_its_own_keyword_and_resumes_for_free(self):
        """先全表跑第一轮（K01、K02），再只点一张只有 K02 的表跑 depth。"""
        with mock.patch.object(visibility, "_send_once", Sender(default=by_page)):
            self.invoke()
        full = self.table
        self.table = TrialTable([trial_record(300, "第二个词", age_days=5)])
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke("--round", "depth")
        self.assertEqual((code, len(sender.requests)), (0, 10))
        self.assertIn("还要发最多 10 次", out)
        report = json.loads(pathlib.Path(self.dir.name, "report.json").read_text(encoding="utf-8"))
        block = report["各轮"]["depth"]
        self.assertEqual([row["词"] for row in block["按词"]], ["K02"])
        self.assertEqual(block["页"], {"计划": 10, "拿到": 10, "还差": 0, "失败过": 0})
        # 演练、只出报告：不发请求、进度文件一个字节都不动。
        self.table = full
        state_path = pathlib.Path(self.dir.name, "state.json")
        before = state_path.read_bytes()
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, out = self.invoke("--round", "depth", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("这一轮查 2 个词", out)
            self.assertIn("已经拿到 10 页，还要发最多 10 次", out)    # K01 补进空位
            code, out = self.invoke("--round", "depth", "--report-only")
            self.assertEqual(code, 0)
        self.assertEqual(state_path.read_bytes(), before)
        again = json.loads(pathlib.Path(self.dir.name, "report.json").read_text(encoding="utf-8"))
        self.assertEqual([row["词"] for row in again["各轮"]["depth"]["按词"]], ["K02"])
        # 拿全表再跑 depth：K02 的 10 页沿用，只付 K01 的 10 页。
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke("--round", "depth")
        self.assertEqual((code, len(sender.requests)), (0, 10))
        self.assertIn("新拿到 10 页，沿用 10 页", out)

    def test_sixteen_keywords_refuse_before_spending(self):
        self.table._records = [trial_record(n, f"词{n}") for n in range(1, 17)]
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke()
        self.assertIn("最多 15 个", str(code))
        self.assertIn("一分钱没花", str(code))
        self.assertFalse(pathlib.Path(self.dir.name, "state.json").exists())

    def test_sixteen_words_in_one_cell_refuse_before_spending(self):
        self.table._records = [trial_record(1, "、".join(f"词{n}" for n in range(1, 17))),
                               trial_record(2, None)]
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke()
        self.assertIn("最多 15 个", str(code))
        self.assertIn("一分钱没花", str(code))
        self.assertFalse(pathlib.Path(self.dir.name, "state.json").exists())

    def test_keywords_on_one_row_are_matched_against_every_row_of_the_table(self):
        """只在一行里写词（多选），别的行空着：整张表的帖子都拿去对。"""
        self.table._meta[Settings().fields.search_keywords] = {"type": 4, "options": None}
        self.table._records = [trial_record(900, ["词甲", "词乙"], age_days=20),
                               trial_record(105, None), trial_record(205, ""),
                               trial_record(999, None)]
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual((code, len(sender.requests)), (0, 6))     # 2 个词 × 3 页
        self.assertIn("读到 4 行，填了「搜索关键词」的 1 行", out)
        self.assertIn("发布 3 天内的帖子 3 篇，综合排序前 3 页里至少在一个词下面搜到 2 篇（67%），"
                      "哪个词下面都没搜到 1 篇", out)
        report = json.loads(pathlib.Path(self.dir.name, "report.json").read_text(encoding="utf-8"))
        self.assertEqual((report["词"], report["帖子"]), (2, 4))
        self.assertEqual(report["读表"]["填了词的行"], 1)
        state = pathlib.Path(self.dir.name, "state.json").read_text(encoding="utf-8")
        for path_text in (out, state, json.dumps(report, ensure_ascii=False)):
            self.assertNotIn("词甲", path_text)
        self.assertNotIn(nid(105), json.dumps(report))

    def test_mention_in_a_text_cell_is_not_searched(self):
        mention = {"type": "mention", "mentionType": "User", "name": "某同事",
                   "text": "@某同事", "token": "ou_x"}
        self.table._records = [
            trial_record(105, [{"type": "text", "text": "词甲，词乙 "}, mention]),
            trial_record(205, [mention]), trial_record(999, None)]
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, _out = self.invoke("--show-keywords")
        self.assertEqual((code, len(sender.requests)), (0, 6))     # 2 个词 × 3 页
        self.assertNotIn("%E6%9F%90%E5%90%8C%E4%BA%8B", "".join(r.url for r in sender.requests))
        self.assertNotIn("某同事", _out)

    def test_a_sentence_in_the_cell_spends_nothing_and_says_so(self):
        self.table._records = [
            trial_record(105, "how to pick a sunscreen for sensitive skin in summer"),
            trial_record(999, "词甲 / -")]
        code, out = self.invoke("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("词：1 个", out)
        self.assertIn("有 1 格「搜索关键词」像一句话", out)
        self.table._records = self.table._records[:1]
        sender = Sender()
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke()
        self.assertEqual(len(sender.requests), 0)
        self.assertIn("一分钱没花", str(code) + out)

    def test_table_with_the_column_but_no_word_spends_nothing(self):
        self.table._records = [trial_record(1, None), trial_record(2, "")]
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, out = self.invoke()
        self.assertIn("没有可查的词", str(code))
        self.assertIn("一个词都没写，这张表不查", out)
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
        """搜索那一层只有两个入口摸得到：命令行，和面板「搜索可见试跑」那一栏背后的
        trial.py。面板自己（panel.py）不直接引它，巡查那几层更不引。"""
        import ast

        repo = pathlib.Path(cli.__file__).resolve().parent

        def imported(path) -> set:
            # 按语法树取，不按行搜：`from . import (a,\n b)` 换了行也算。
            names = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[-1] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.update(a.name for a in node.names)
                    if node.module:
                        names.add(node.module.split(".")[-1])
            return names

        def importers(module: str) -> list:
            return sorted(p.name for p in (repo / "xhsearch").glob("*.py")
                          if p.name != f"{module}.py" and module in imported(p))
        self.assertEqual(importers("visibility"), ["trial.py"])
        self.assertEqual(importers("trial"), ["panel.py"])

    def test_the_cli_reaches_the_trial_only_from_the_trial_command(self):
        """cli.py 里引 trial / visibility 的地方都在 search-trial 那几个函数里——
        sweep / queue / row / estimate / serve 的函数体里没有。"""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(cli))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module == "xhsearch" \
                        and {a.name for a in child.names} & {"trial", "visibility"}:
                    self.assertIn("search_trial", node.name)
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        for node in top:
            self.assertFalse({a.name for a in node.names} & {"trial", "visibility"})

    def test_the_panel_starts_a_paid_run_only_from_the_button_route(self):
        """面板里会花钱的那一下（Trial.start）只有 POST /api/trial/start 的处理函数会调：
        取数的后台线程、每期结算、轮询都不调。"""
        import ast
        import inspect

        callers = set()
        for node in ast.walk(ast.parse(inspect.getsource(panel))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                        and child.func.attr in ("start", "report_only", "dry_run") \
                        and isinstance(child.func.value, ast.Name) \
                        and child.func.value.id == "trial":
                    callers.add(node.name)
        self.assertEqual(callers, {"_trial_action"})


if __name__ == "__main__":
    unittest.main()
