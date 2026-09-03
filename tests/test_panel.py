"""监控面板的单测。全部离线，不起真服务、不发任何请求。

这个文件里最重要的不是「功能对不对」，是**几条不变量**：
面板不发付费请求、不写业务表、不把密钥或个人信息漏到前端、
口令没配就拒绝启动。那几条一旦破了，破法都是安静的。
"""

import json
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import feishu, panel, panel_view, railway, summary
from xhsearch.config import Settings

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
GOOD_PASSWORD = "a-very-long-passphrase"


def config(**kwargs):
    base = dict(password=GOOD_PASSWORD, secret=b"secret-bytes", port=8080)
    base.update(kwargs)
    return panel.PanelConfig(**base)


class FakeTable:
    """只实现面板真正会调的两个方法。多一个都不给——
    面板要是哪天开始调别的东西，这里会直接 AttributeError。"""

    def __init__(self, meta, records, *, app_token="bascnAAA", table_id="tblBBB",
                 meta_error=None, search_error=None):
        self.app_token = app_token
        self.table_id = table_id
        self._meta = meta
        self._records = records
        self._meta_error = meta_error
        self._search_error = search_error
        self.searched_fields = None
        self.searched_filter = None

    def fields_meta(self):
        if self._meta_error:
            raise self._meta_error
        return self._meta

    def search(self, field_names, *, filter_spec=None, **_kwargs):
        if self._search_error:
            raise self._search_error
        self.searched_fields = list(field_names)
        self.searched_filter = filter_spec
        return self._records


def healthy_meta(settings=None):
    from xhsearch import schema
    settings = settings or Settings()
    meta = {}
    for name, allowed, _label, options, _note in schema.expected_schema(settings):
        meta[name] = {"type": allowed[0], "ui_type": "",
                      "options": list(options) if options else None}
    return meta


# ---------------------------------------------------------------- 不变量

def _called_names(module) -> set:
    """模块里所有被调用的名字（含 a.b.c 这种点号形式）。

    按 AST 走，不是字符串搜——注释和文档字符串里提到某个函数名是正常的
    （这个仓库的文档就写在代码里），把它们算成「调用了它」会逼着人
    为了过测试而把话说得含糊。
    """
    import ast
    import inspect

    called = set()

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                called.add(name)
                called.add(name.rsplit(".", 1)[-1])
    return called


def _imported_modules(module) -> set:
    import ast
    import inspect

    names = set()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            if node.module:
                names.add(node.module.split(".")[0])
    return names


class TestNeverSpends(unittest.TestCase):
    def test_panel_calls_nothing_that_spends_money(self):
        """面板永远不发付费请求。按 AST 逐个调用节点查，不是字符串搜。

        只认**带点号的全名**和几个不会撞车的裸名字：光看裸名字的话，
        `Cache.refresh`（重新读一遍飞书）会被当成 `runner.refresh`（真花钱）。
        「面板压根不 import 那两层」由下面两条测试保证，两条合起来才严密。
        """
        called = _called_names(panel)
        for name in called:
            self.assertFalse(
                name.startswith(("providers.", "runner.", "protocol.", "transport.")),
                f"panel.py 调了 {name}——面板不许碰会发请求的那几层")
        for forbidden in ("plan_calls", "estimate_yuan", "_fetch_one",
                          "_call_once", "post_with_retry", "get_with_retry",
                          "get_provider", "write_back"):
            self.assertNotIn(forbidden, called,
                             f"panel.py 调了 {forbidden}——面板不许成为付费执行者")

    def test_panel_does_not_import_the_provider_layer(self):
        self.assertNotIn("providers", _imported_modules(panel))
        self.assertFalse(hasattr(panel, "providers"),
                         "panel 模块不该持有 providers 的引用")

    def test_panel_does_not_import_runner_either(self):
        """连 runner 都不引：面板要的 row_from_record 是 summary 那一层的事，
        面板自己碰不到编排层，也就没有「不小心调了 refresh」的可能。"""
        self.assertNotIn("runner", _imported_modules(panel))

    def test_collect_only_reads(self):
        """跑一趟完整的 collect，确认它只调了 fields_meta 和 search。"""
        table = FakeTable(healthy_meta(), [])
        with mock.patch.object(FakeTable, "search",
                               side_effect=FakeTable.search,
                               autospec=True) as spy:
            panel.collect([("A", table)], Settings(), {"tikhub": "k"}, now=NOW)
        self.assertEqual(spy.call_count, 1)


class TestBusinessTableWrites(unittest.TestCase):
    """面板对**业务表数据**只写一列：`排队刷新`。

    白名单而不是黑名单：黑名单要穷举「不许写什么」，漏一个就是默许；
    白名单漏一个只是少个功能。
    """

    def test_the_whitelist_has_exactly_one_column(self):
        self.assertEqual(panel.BUSINESS_WRITE_WHITELIST, ("排队刷新",))

    def test_the_whitelist_matches_the_configured_column_name(self):
        """有人改了 config 里的列名却忘了改白名单，那一天要挡住。"""
        self.assertIn(Settings().fields.queued, panel.BUSINESS_WRITE_WHITELIST)

    def test_conclusion_columns_are_never_writable(self):
        """写机器的结论列等于伪造监控结果——面板没量过任何东西。"""
        f = Settings().fields
        for column in (f.traffic_status, f.refresh_status, f.comment_status,
                       f.negative_status, f.pinned_status, f.failure_reason,
                       f.last_updated, f.comment_count, f.consecutive_failures,
                       f.alive_confirmed, f.comment_digest, f.negative_digest):
            self.assertNotIn(column, panel.BUSINESS_WRITE_WHITELIST)

    def test_operator_input_columns_are_never_writable(self):
        """那些是人的判断，面板只显示不代填。"""
        f = Settings().fields
        for column in (f.seed_keywords, f.negative_keywords, f.publish_time,
                       f.link, f.monitoring):
            self.assertNotIn(column, panel.BUSINESS_WRITE_WHITELIST)

    def test_the_queue_path_refuses_a_column_off_the_whitelist(self):
        settings = Settings()
        settings.fields.queued = "我不在白名单里"
        cfg = config()
        cfg.app_id, cfg.app_secret = "cli_x", "s"
        with self.assertRaises(RuntimeError):
            panel.Queueing(cfg, settings, log=lambda *a: None).queue(
                "bascnA", "tblB", ["rec1"])


class TestConfigRefusesToStartUnsafe(unittest.TestCase):
    def test_missing_password_refuses(self):
        with self.assertRaises(panel.ConfigError) as ctx:
            panel.PanelConfig.from_env({})
        self.assertIn("PANEL_PASSWORD", str(ctx.exception))

    def test_short_password_refuses(self):
        with self.assertRaises(panel.ConfigError) as ctx:
            panel.PanelConfig.from_env({"PANEL_PASSWORD": "short"})
        self.assertIn("至少", str(ctx.exception))

    def test_secret_is_random_when_unset(self):
        a = panel.PanelConfig.from_env({"PANEL_PASSWORD": GOOD_PASSWORD})
        b = panel.PanelConfig.from_env({"PANEL_PASSWORD": GOOD_PASSWORD})
        self.assertNotEqual(a.secret, b.secret)
        self.assertGreaterEqual(len(a.secret), 32)

    def test_bad_numbers_refuse_rather_than_clamp(self):
        for env in ({"PANEL_CACHE_SECONDS": "0"}, {"PANEL_CACHE_SECONDS": "abc"},
                    {"PORT": "0"}, {"PORT": "99999"}, {"PANEL_SHOW_DIGEST": "maybe"}):
            env = {"PANEL_PASSWORD": GOOD_PASSWORD, **env}
            with self.assertRaises(panel.ConfigError):
                panel.PanelConfig.from_env(env)

    def test_new_table_collaborators_come_from_the_environment(self):
        """建完表给谁开权限：逗号 / 分号分隔都认，两头空白去掉，空就是空元组。"""
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": GOOD_PASSWORD,
            "FEISHU_TABLE_MANAGERS": "ziao@example.com, ou_abc；ou_def",
            "FEISHU_TABLE_EDITOR_CHATS": " oc_1 ; oc_2 ",
            "FEISHU_TABLE_OWNER": " ziao@example.com "})
        self.assertEqual(cfg.table_managers, ("ziao@example.com", "ou_abc", "ou_def"))
        self.assertEqual(cfg.table_editor_chats, ("oc_1", "oc_2"))
        self.assertEqual(cfg.table_owner, "ziao@example.com")
        bare = panel.PanelConfig.from_env({"PANEL_PASSWORD": GOOD_PASSWORD})
        self.assertEqual((bare.table_managers, bare.table_editor_chats, bare.table_owner),
                         ((), (), ""))

    def test_a_formatted_phone_number_stays_one_manager(self):
        """「+86 138-0000-0000」按空白切会变成两个人，谁都对不上。"""
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": GOOD_PASSWORD,
            "FEISHU_TABLE_MANAGERS": "+86 138-0000-0000, 139 0000 0000"})
        self.assertEqual(cfg.table_managers, ("+86 138-0000-0000", "139 0000 0000"))
        from xhsearch import provision
        self.assertEqual([provision.member_type(m) for m in cfg.table_managers],
                         ["mobile", "mobile"])


class TestSessions(unittest.TestCase):
    def test_roundtrip(self):
        cfg = config()
        self.assertTrue(panel.valid_session(cfg, panel.issue_session(cfg)))

    def test_expired_token_rejected(self):
        # now=0 是 epoch，是个合法时刻。曾经这里写的是 `now or time.time()`，
        # 0 被当成「没传」，于是这个「1970 年签发的 token」反而是有效的。
        cfg = config()
        token = panel.issue_session(cfg, now=0)
        self.assertFalse(panel.valid_session(cfg, token))

    def test_zero_is_a_real_timestamp_not_a_missing_one(self):
        cfg = config()
        token = panel.issue_session(cfg, now=0)
        self.assertTrue(token.startswith(str(panel.SESSION_TTL_SECONDS) + "."))
        self.assertTrue(panel.valid_session(cfg, token, now=0))

    def test_forged_signature_rejected(self):
        cfg = config()
        expiry = int(NOW.timestamp()) + 9999
        self.assertFalse(panel.valid_session(cfg, f"{expiry}.deadbeef"))

    def test_token_from_another_secret_rejected(self):
        token = panel.issue_session(config(secret=b"other"))
        self.assertFalse(panel.valid_session(config(), token))

    def test_garbage_rejected_without_raising(self):
        cfg = config()
        for junk in ("", "x", "....", "abc.def", "9999999999999999999999.x"):
            self.assertFalse(panel.valid_session(cfg, junk))

    def test_csrf_token_is_derived_not_the_session(self):
        cfg = config()
        session = panel.issue_session(cfg)
        derived = panel.csrf_token(cfg, session)
        self.assertNotEqual(derived, session)
        self.assertNotIn(session, derived)
        self.assertEqual(derived, panel.csrf_token(cfg, session))
        self.assertNotEqual(derived, panel.csrf_token(config(secret=b"other"), session))
        self.assertEqual(panel.csrf_token(cfg, ""), "")

    def test_password_check_is_exact(self):
        cfg = config()
        self.assertTrue(panel.check_password(cfg, GOOD_PASSWORD))
        self.assertFalse(panel.check_password(cfg, GOOD_PASSWORD + " "))
        self.assertFalse(panel.check_password(cfg, ""))


class TestLoginThrottle(unittest.TestCase):
    def test_blocks_after_the_limit(self):
        throttle = panel.LoginThrottle(max_failures=3, window=60)
        for _ in range(3):
            self.assertFalse(throttle.blocked("1.2.3.4"))
            throttle.record_failure("1.2.3.4")
        self.assertTrue(throttle.blocked("1.2.3.4"))

    def test_one_source_does_not_lock_out_everyone(self):
        """全局锁定意味着任何人都能让整个团队登不进来。"""
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        for _ in range(5):
            throttle.record_failure("attacker")
        self.assertTrue(throttle.blocked("attacker"))
        self.assertFalse(throttle.blocked("colleague"))

    def test_window_expires(self):
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        throttle.record_failure("ip", now=1000)
        throttle.record_failure("ip", now=1000)
        self.assertTrue(throttle.blocked("ip", now=1010))
        self.assertFalse(throttle.blocked("ip", now=1100))

    def test_bucket_count_is_bounded(self):
        """X-Forwarded-For 可以伪造。伪造只能绕过限速，
        不能把限速本身变成内存耗尽。"""
        throttle = panel.LoginThrottle()
        for i in range(5000):
            throttle.record_failure(f"ip-{i}")
        self.assertLessEqual(len(throttle._buckets), 4096)

    def test_success_clears_the_bucket(self):
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        throttle.record_failure("ip")
        throttle.clear("ip")
        self.assertFalse(throttle.blocked("ip"))


# ---------------------------------------------------------------- 取数

class TestCollect(unittest.TestCase):
    def test_only_asks_for_columns_that_exist(self):
        """按名字请求不存在的列会让整个 search 报 1254045，一行都读不回来。"""
        settings = Settings()
        meta = healthy_meta(settings)
        del meta[settings.fields.negative_status]
        table = FakeTable(meta, [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertNotIn(settings.fields.negative_status, table.searched_fields)
        self.assertIn(settings.fields.link, table.searched_fields)

    def test_filters_to_monitored_rows(self):
        settings = Settings()
        table = FakeTable(healthy_meta(settings), [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertEqual(table.searched_filter["conditions"][0]["field_name"],
                         settings.fields.monitoring)

    def test_missing_monitoring_column_is_flagged_not_silently_ignored(self):
        """没法过滤在管的行，下面每个数字都包含了本该排除的行——
        必须说出来，而不是给一堆看起来正常的数字。"""
        settings = Settings()
        meta = healthy_meta(settings)
        del meta[settings.fields.monitoring]
        overview = panel.collect([("A", FakeTable(meta, []))], settings, {}, now=NOW)
        self.assertTrue(any("无法只统计在管的行" in p
                            for p in overview.projects[0].health))

    def test_unreadable_metadata_explains_the_usual_cause(self):
        overview = panel.collect([("A", FakeTable(None, []))], Settings(), {}, now=NOW)
        self.assertIn("添加文档应用", overview.projects[0].error)

    def test_empty_metadata_is_treated_the_same_as_unreadable(self):
        """多维表格的主字段不可删，健康的表不可能一列都没有。"""
        overview = panel.collect([("A", FakeTable({}, []))], Settings(), {}, now=NOW)
        self.assertIn("添加文档应用", overview.projects[0].error)

    def test_one_broken_table_does_not_break_the_others(self):
        settings = Settings()
        broken = FakeTable(healthy_meta(settings), [],
                           search_error=RuntimeError("权限被收回"))
        good = FakeTable(healthy_meta(settings), [], app_token="t2", table_id="tb2")
        overview = panel.collect([("坏", broken), ("好", good)], settings, {}, now=NOW)
        self.assertIn("权限被收回", overview.projects[0].error)
        self.assertEqual(overview.projects[1].error, "")

    def test_digest_columns_not_requested_by_default(self):
        settings = Settings()
        table = FakeTable(healthy_meta(settings), [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertNotIn(settings.fields.comment_digest, table.searched_fields)

    # —— 「哪一条」用哪一列 ——

    def _with_content_column(self, name="笔记内容", *, text="露营装备清单"):
        settings = Settings()
        meta = healthy_meta(settings)
        meta[name] = {"type": "text", "ui_type": "", "options": None}
        record = {"record_id": "r1", "fields": {
            settings.fields.link: "https://www.xiaohongshu.com/explore/"
                                  "65a1b2c3d4e5f60718293a4b",
            settings.fields.traffic_status: ["风控中"],
            name: text}}
        return settings, FakeTable(meta, [record])

    def test_the_content_column_is_found_without_any_configuration(self):
        """运营不该为了看见「哪一条」再去配一个环境变量——
        字段元数据本来就读了，列名是白拿的。"""
        settings, table = self._with_content_column()
        overview = panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertIn("笔记内容", table.searched_fields)
        self.assertEqual(overview.projects[0].todos[0].label, "露营装备清单")

    def test_a_configured_column_overrides_the_automatic_pick(self):
        settings = Settings()
        meta = healthy_meta(settings)
        for name in ("笔记内容", "我自己的文案列"):
            meta[name] = {"type": "text", "ui_type": "", "options": None}
        record = {"record_id": "r1", "fields": {
            settings.fields.traffic_status: ["风控中"],
            "笔记内容": "自动认出来的", "我自己的文案列": "我配的那一列"}}
        overview = panel.collect([("A", FakeTable(meta, [record]))], settings, {},
                                 now=NOW, label_column="我自己的文案列")
        self.assertEqual(overview.projects[0].todos[0].label, "我配的那一列")

    def test_no_content_column_anywhere_is_said_out_loud(self):
        """整栏空掉，运营就只能一条条点开链接看——正是这个面板要消灭的动作。
        所以要在项目卡上说清楚试过哪些名字、怎么补。"""
        settings = Settings()
        overview = panel.collect([("A", FakeTable(healthy_meta(settings), []))],
                                 settings, {}, now=NOW)
        notes = " ".join(overview.projects[0].health)
        self.assertIn("笔记内容", notes)
        self.assertIn("PANEL_LABEL_COLUMN", notes)

    def test_a_configured_column_that_the_table_lacks_is_called_out(self):
        """这条提示是「列名写错了」时唯一的线索。它自己坏掉的话没人会发现。"""
        settings = Settings()
        overview = panel.collect([("A", FakeTable(healthy_meta(settings), []))],
                                 settings, {}, now=NOW, label_column="打错的列名")
        notes = " ".join(overview.projects[0].health)
        self.assertIn("打错的列名", notes)
        self.assertIn("PANEL_LABEL_COLUMN", notes)

    def test_a_column_that_exists_raises_no_complaint(self):
        settings, table = self._with_content_column()
        overview = panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertNotIn("PANEL_LABEL_COLUMN", " ".join(overview.projects[0].health))


class TestCheckSeesDisabledEntries(unittest.TestCase):
    """体检查重必须**连停用的行一起看**。

    踩过的坑：查重用的清单过滤了 `usable`，而 `usable = enabled and …`——
    于是把一张表停用之后再去体检它，会报「配置齐了，可以直接入册」，
    加进去就是同一张表两行。等两行都启用了才被 `find_duplicates` 抓到，
    而在那之前，**一轮里这张表会被刷两遍 = 付两次钱**。
    """

    class _Entry:
        def __init__(self, label, table_id, enabled):
            self.label, self.table_id, self.enabled = label, table_id, enabled
            self.app_token, self.problem = "bascnA", ""

        @property
        def usable(self):
            return self.enabled and not self.problem and bool(self.table_id)

    def _known(self, *entries):
        projects = panel.Projects.__new__(panel.Projects)
        projects.settings = Settings()
        projects.list = lambda: list(entries)                   # noqa: ARG005
        captured = {}

        def fake_check(_table, _settings, *, label, target, known_tables):
            captured["known"] = list(known_tables)
            return None

        with mock.patch.object(panel.Projects, "_bitable", lambda *a, **k: None), \
             mock.patch("xhsearch.provision.check", fake_check):
            panel.Projects.check(projects, "新项目", "bascnA:tblB")
        return captured["known"]

    def test_a_disabled_table_still_counts_as_known(self):
        known = self._known(self._Entry("停用的", "tblB", enabled=False))
        self.assertIn("tblB", [t for _l, _a, t in known],
                      "停用的表没进查重清单，再体检会报「可以入册」，加进去就是两行")

    def test_an_enabled_table_counts_too(self):
        known = self._known(self._Entry("在跑的", "tblB", enabled=True))
        self.assertIn("tblB", [t for _l, _a, t in known])

    def test_a_row_with_no_table_id_is_skipped(self):
        """链接没解析出来的行没有 table_id，拿它查重只会误报。"""
        known = self._known(self._Entry("坏行", "", enabled=True))
        self.assertEqual(known, [])


class TestCache(unittest.TestCase):
    def test_keeps_the_last_good_snapshot_when_a_refresh_fails(self):
        """一次网络抖动不该让整个面板变成白纸。"""
        results = [summary.Overview(projects=[]), RuntimeError("网络抖了一下")]

        def produce():
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        cache = panel.Cache(produce, ttl=999)
        cache.refresh()
        first, error, _ = cache.snapshot()
        self.assertIsNotNone(first)
        self.assertEqual(error, "")
        cache.refresh()
        second, error, _ = cache.snapshot()
        self.assertIs(second, first)
        self.assertIn("网络抖了一下", error)

    def test_first_failure_leaves_no_snapshot_but_records_why(self):
        cache = panel.Cache(lambda: (_ for _ in ()).throw(RuntimeError("挂了")), ttl=999)
        cache.refresh()
        value, error, _ = cache.snapshot()
        self.assertIsNone(value)
        self.assertIn("挂了", error)


class TestFeishuBase(unittest.TestCase):
    def test_explicit_domain_wins(self):
        self.assertEqual(panel._feishu_base({"FEISHU_DOMAIN": "https://acme.feishu.cn"}),
                         "https://acme.feishu.cn")

    def test_bare_domain_gets_https(self):
        self.assertEqual(panel._feishu_base({"FEISHU_DOMAIN": "acme.feishu.cn"}),
                         "https://acme.feishu.cn")

    def test_scraped_from_tables_spec(self):
        spec = "A=bascnX:tblY; B=https://acme.feishu.cn/base/bascnZ?table=tblW"
        self.assertEqual(panel._feishu_base({"FEISHU_TABLES": spec}),
                         "https://acme.feishu.cn")

    def test_falls_back_to_the_generic_domain(self):
        self.assertEqual(panel._feishu_base({}), "https://feishu.cn")


# ---------------------------------------------------------------- 渲染

class TestRendering(unittest.TestCase):
    def _page(self, snap, **kwargs):
        overview = summary.Overview(projects=[snap], generated_at=NOW)
        return panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(**kwargs), csrf="tok")

    def _snap(self, **kwargs):
        base = dict(label="项目A", app_token="t", table_id="tb")
        base.update(kwargs)
        return summary.ProjectSnapshot(**base)

    def test_escapes_text_that_came_from_feishu(self):
        """诊断信息、项目名、评论正文都是人写的字。直接拼进 HTML 就是存储型 XSS。"""
        todo = summary.TodoRow(record_id="r1", project="<img src=x onerror=alert(1)>",
                               record_url="https://x/base/t?table=tb&record=r1",
                               link_cell="", reasons=["风控中"],
                               diagnosis="</td><script>alert(1)</script>")
        page = self._page(self._snap(todos=[todo]))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src=x onerror", page)
        self.assertIn("&lt;script&gt;", page)

    def test_the_which_one_cell_is_clipped_but_keeps_the_full_text_on_hover(self):
        """这一栏太长会把「为什么」和「诊断信息」挤没；太短就定位不了。
        截断的那一份给眼睛，完整的那一份挂 title 给鼠标。"""
        long = "露营装备清单｜新手最容易踩的五个坑，第三个我自己也中招了"
        todo = summary.TodoRow(record_id="r1", project="A", link_cell="",
                               record_url="https://x/base/t?table=tb&record=r1",
                               label=long, reasons=["风控中"])
        page = self._page(self._snap(todos=[todo]))
        cell = re.search(r"<td class=which title='([^']*)'>([^<]*)</td>", page)
        self.assertIsNotNone(cell, "待办表里没有「哪一条」那一栏")
        self.assertEqual(cell.group(1), long)
        self.assertTrue(cell.group(2).endswith("…"))
        self.assertLess(len(cell.group(2)), len(long))
        self.assertTrue(long.startswith(cell.group(2)[:-1]))

    def test_an_empty_label_renders_a_dash_not_a_blank_cell(self):
        """空单元格和「读到了但是空的」在表格里长得一样，会被当成渲染坏了。"""
        todo = summary.TodoRow(record_id="r1", project="A", link_cell="",
                               record_url="https://x/base/t?table=tb&record=r1",
                               label="", reasons=["风控中"])
        page = self._page(self._snap(todos=[todo]))
        self.assertIn("<td class=which title=''>—</td>", page)

    def test_the_commit_comes_from_the_variable_railway_injects(self):
        """Railway 自己注入 RAILWAY_GIT_COMMIT_SHA，不用额外配。取前 7 位。"""
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": "a-long-enough-password",
            "RAILWAY_GIT_COMMIT_SHA": "ea69577b2eb12ebfab5e2b9f9a683d37cfa8ddb1"})
        self.assertEqual(cfg.commit, "ea69577")

    def test_the_footer_says_which_commit_is_running(self):
        """「这个修复上线了吗」不该靠问。对着 GitHub 上的短号一眼能对。"""
        page = self._page(self._snap(), commit="abc1234")
        self.assertIn("版本 abc1234", page)

    def test_no_commit_means_no_version_line_at_all(self):
        """本地跑、别的平台跑都读不到它——那就整块不显示，
        而不是显示一个「版本 」或者硬编一个假值。"""
        page = self._page(self._snap())
        self.assertNotIn("版本", page)

    def test_no_external_resources(self):
        page = self._page(self._snap())
        for pattern in (r'src=["\']https?://', r'href=["\']https?://(?!\w+\.feishu)',
                        r'@import', r'//cdn\.'):
            self.assertIsNone(re.search(pattern, page),
                              f"页面里出现了外部资源：{pattern}")

    def test_empty_todo_list_says_so_plainly(self):
        page = self._page(self._snap())
        self.assertIn("没有需要处理的行", page)

    def test_deep_link_is_present_for_each_todo(self):
        todo = summary.TodoRow(record_id="rec1", project="A",
                               record_url="https://acme.feishu.cn/base/t?table=tb&record=rec1",
                               link_cell="", reasons=["有负面"])
        page = self._page(self._snap(todos=[todo]))
        self.assertIn("record=rec1", page)
        self.assertIn("去这一行", page)

    def test_generic_domain_warning_shown_when_unset(self):
        page = self._page(self._snap())
        self.assertIn("FEISHU_DOMAIN", page)

    def test_no_warning_when_domain_is_configured(self):
        page = self._page(self._snap(), feishu_base="https://acme.feishu.cn")
        self.assertNotIn("FEISHU_DOMAIN", page)

    def test_health_problems_are_shown_verbatim(self):
        page = self._page(self._snap(health=["「流量状态」缺这些选项：观察中。"]))
        self.assertIn("缺这些选项", page)

    def test_stale_snapshot_is_labelled(self):
        overview = summary.Overview(projects=[self._snap()], generated_at=NOW)
        page = panel_view.overview_page(
            overview=overview, error="FeishuError: 权限被收回",
            fetched_at=NOW.timestamp(), config=config(), csrf="tok")
        self.assertIn("上一次取数失败", page)
        self.assertIn("权限被收回", page)

    def test_cold_start_page_does_not_crash(self):
        page = panel_view.overview_page(
            overview=None, error="", fetched_at=0.0, config=config(), csrf="")
        self.assertIn("正在第一次取数", page)

    def test_login_page_escapes_its_message(self):
        page = panel_view.login_page("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_page_never_contains_the_password_or_secret(self):
        """密钥绝不下发前端。"""
        page = self._page(self._snap())
        self.assertNotIn(GOOD_PASSWORD, page)
        self.assertNotIn("secret-bytes", page)


class TestJsonPayload(unittest.TestCase):
    def test_todo_json_is_serialisable_and_carries_no_secrets(self):
        todo = summary.TodoRow(record_id="r1", project="A", record_url="u",
                               link_cell="https://xhs/x", reasons=["风控中"])
        blob = json.dumps(panel._todo_json(todo), ensure_ascii=False)
        self.assertIn("风控中", blob)

    def test_project_json_round_trips(self):
        snap = summary.ProjectSnapshot(label="A", app_token="t", table_id="tb",
                                       total_rows=3, due_yuan=1.23456)
        payload = panel._project_json(snap)
        self.assertEqual(payload["due_yuan"], 1.23)
        json.dumps(payload, ensure_ascii=False)


class TestLogSanitising(unittest.TestCase):
    def test_control_characters_are_scrubbed(self):
        """Python 3.12 才在 BaseHTTPRequestHandler 里做这件事，
        而这个项目钉在 3.11——不自己做，远端能往终端里塞转义序列。"""
        dirty = "GET /\x1b[31mred\x00\x07 HTTP/1.1"
        clean = panel._sanitize_for_log(dirty)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\x00", clean)
        self.assertNotIn("\x07", clean)

    def test_长度有上界(self):
        self.assertLessEqual(len(panel._sanitize_for_log("x" * 5000)), 200)


# ---------------------------------------------------------------- 真起服务

class TestOverHttp(unittest.TestCase):
    """真起一个服务、真发 HTTP 请求。

    鉴权、CSRF、Cookie 属性这几件事，单看函数是测不出来的——
    它们的 bug 全都长在「handler 到底往响应里写了什么」上。
    """

    @classmethod
    def setUpClass(cls):
        from xhsearch import schema as _schema

        # 访问日志会把测试输出淹掉。这里静音，但**不改生产行为**——
        # 日志的清洗逻辑另有单测（TestLogSanitising）盯着。
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)

        settings = Settings()

        class Table:
            app_token, table_id = "bascnAAA", "tblBBB"

            def fields_meta(self):
                return {n: {"type": a[0], "ui_type": "",
                            "options": list(o) if o else None}
                        for n, a, _l, o, _x in _schema.expected_schema(settings)}

            def search(self, fields, *, filter_spec=None, **kwargs):
                return [{"record_id": "r1", "fields": {
                    settings.fields.link: "https://www.xiaohongshu.com/explore/aa",
                    settings.fields.traffic_status: ["风控中"],
                    settings.fields.failure_reason: "1006 内容被限制",
                }}]

        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cache = panel.Cache(
            lambda: panel.collect([("A", Table())], settings, {}, now=NOW), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def _call(self, path, *, data=None, headers=None, method=None,
              follow=True, host="localhost"):
        import urllib.error
        import urllib.request

        url = f"http://{host}:{self.port}{path}"
        request = urllib.request.Request(url, data=data, headers=headers or {},
                                         method=method)
        if follow:
            opener = urllib.request.build_opener()
        else:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(request, timeout=5)
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), exc.headers

    def _login(self, host="localhost", headers=None):
        status, _body, resp_headers = self._call(
            "/login", data=b"password=" + GOOD_PASSWORD.encode(),
            headers=headers, follow=False, host=host)
        cookie = resp_headers.get("Set-Cookie") or ""
        token = cookie.split("=", 1)[1].split(";")[0] if "=" in cookie else ""
        return status, cookie, token

    def _authed(self, token):
        return {"Cookie": f"{panel.COOKIE_NAME}={token}"}

    # —— 无需登录 ——
    def test_healthz_needs_no_password(self):
        status, body, _ = self._call("/healthz")
        self.assertEqual((status, body), (200, "ok"))

    def test_root_without_session_is_the_login_page(self):
        status, body, _ = self._call("/")
        self.assertEqual(status, 200)
        self.assertIn("口令", body)
        self.assertNotIn("要人管的行", body)

    def test_api_without_session_is_401(self):
        self.assertEqual(self._call("/api/overview")[0], 401)

    def test_unknown_route_is_404(self):
        self.assertEqual(self._call("/nope")[0], 404)

    def test_security_headers_are_present(self):
        _s, _b, headers = self._call("/healthz")
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_csp_lets_the_page_call_its_own_api(self):
        """**页面里每一个 fetch() 都必须被 CSP 放行。**

        CSP 只有真浏览器认——这个仓库的测试全走 urllib，截图走 file://，
        两者都看不见这个头。所以拦不住它的唯一办法是把「脚本里 fetch 了什么」
        和「CSP 允许连什么」放在一起判，而不是只断言这个头存在。

        踩过的坑：`default-src 'none'` 且没写 `connect-src`，于是 fetch 全部
        被浏览器拦下，页面上是 `TypeError: Failed to fetch`——项目列表、加表、
        体检、新建、重新取数、勾排队刷新，一个都点不动。服务端一切正常，
        请求根本没发出去。
        """
        _s, _b, headers = self._call("/healthz")
        csp = headers.get("Content-Security-Policy", "")
        directives = {}
        for chunk in csp.split(";"):
            parts = chunk.split()
            if parts:
                directives[parts[0].lower()] = parts[1:]

        # 脚本里真的 fetch 了哪些地址——从源码里抠，别手写清单，
        # 否则将来新加一个 fetch 这条测试就又变成摆设了。
        targets = re.findall(r'fetch\(\s*"([^"]+)"', panel_view._SCRIPT)
        self.assertTrue(targets, "脚本里一个 fetch 都没找到，这条测试失去意义")
        self.assertTrue(all(t.startswith("/") for t in targets),
                        f"出现了非同源的 fetch：{targets}")

        governing = directives.get("connect-src", directives.get("default-src"))
        self.assertIsNotNone(governing, f"CSP 里既没有 connect-src 也没有 default-src：{csp}")
        self.assertIn(
            "'self'", governing,
            f"CSP 不允许页面连自己的接口，{len(targets)} 个 fetch 会被浏览器全部拦下"
            f"（管这件事的是 {'connect-src' if 'connect-src' in directives else 'default-src'}"
            f"：{' '.join(governing)}）")

    def test_csp_still_refuses_everything_it_used_to(self):
        """放开 connect-src 不等于放开别的。外部脚本/样式/图片仍然不许。"""
        _s, _b, headers = self._call("/healthz")
        csp = headers.get("Content-Security-Policy", "")
        for directive in ("default-src 'none'", "base-uri 'none'",
                          "frame-ancestors 'none'", "form-action 'self'"):
            self.assertIn(directive, csp)
        self.assertNotIn("connect-src *", csp)
        self.assertNotIn("'unsafe-eval'", csp)

    # —— 登录 ——
    def test_wrong_password_is_401_and_grants_nothing(self):
        status, body, headers = self._call("/login", data=b"password=nope")
        self.assertEqual(status, 401)
        self.assertIn("口令不对", body)
        self.assertNotIn("Set-Cookie", headers)

    def test_oversized_body_is_rejected_before_reading(self):
        status, _b, _h = self._call("/login", data=b"password=" + b"x" * 70000)
        self.assertEqual(status, 413)

    def test_missing_content_length_is_411(self):
        # urllib 总会带 Content-Length，所以直接走裸 socket。
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"POST /login HTTP/1.0\r\nHost: localhost\r\n\r\n")
            self.assertIn(b"411", sock.recv(64))

    def test_login_then_see_the_panel(self):
        status, _cookie, token = self._login()
        self.assertEqual(status, 303)
        body = self._call("/", headers=self._authed(token))[1]
        self.assertIn("要人管的行", body)
        self.assertIn("1006 内容被限制", body)
        self.assertIn("record=r1", body)

    def test_password_never_appears_in_any_response(self):
        _s, _c, token = self._login()
        for path in ("/", "/api/overview"):
            self.assertNotIn(GOOD_PASSWORD, self._call(path, headers=self._authed(token))[1])

    # —— Cookie 属性 ——
    def test_plain_localhost_omits_secure_so_local_dev_works(self):
        _s, cookie, _t = self._login()
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_https_forwarded_gets_secure(self):
        _s, cookie, _t = self._login(headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", cookie)

    def test_unknown_host_gets_secure(self):
        """判断不出来是不是明文时，从严——失败方向必须是「更严」。"""
        _s, cookie, _t = self._login(host="127.0.0.1",
                                     headers={"Host": "panel.up.railway.app"})
        self.assertIn("Secure", cookie)

    # —— CSRF ——
    def test_post_without_the_header_is_refused(self):
        _s, _c, token = self._login()
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=self._authed(token))
        self.assertEqual(status, 403)

    def test_post_with_a_wrong_header_is_refused(self):
        _s, _c, token = self._login()
        headers = {**self._authed(token), panel.CSRF_HEADER: "not-the-token"}
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=headers)
        self.assertEqual(status, 403)

    def test_post_with_the_matching_header_works(self):
        _s, _c, token = self._login()
        csrf = panel.csrf_token(self.config, token)
        headers = {**self._authed(token), panel.CSRF_HEADER: csrf}
        status, body, _h = self._call("/api/refresh", data=b"", method="POST",
                                      headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("风控中", body)

    def test_session_value_is_not_accepted_as_the_csrf_token(self):
        """CSRF token 是派生值，不是会话本身——会话原值不该能当它用。
        （否则页面里那个 data-csrf 就等于把 HttpOnly 的 Cookie 抄进了 DOM。）"""
        _s, _c, token = self._login()
        headers = {**self._authed(token), panel.CSRF_HEADER: token}
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=headers)
        self.assertEqual(status, 403)

    def test_page_does_not_leak_the_session_value(self):
        _s, _c, token = self._login()
        body = self._call("/", headers=self._authed(token))[1]
        self.assertNotIn(token, body)
        self.assertIn(panel.csrf_token(self.config, token), body)

    def test_forged_cookie_does_not_get_in(self):
        headers = {"Cookie": f"{panel.COOKIE_NAME}=99999999999.deadbeef"}
        self.assertEqual(self._call("/api/overview", headers=headers)[0], 401)


# ---------------------------------------------------------------- 日志

class FakeRailwayConfig:
    def __init__(self, enabled=True, missing=()):
        self._enabled = enabled
        self._missing = list(missing)
        self.token = "tok-1234567890" if enabled else ""
        self.environment_id = "env-1" if enabled else ""
        self.service_id = ""

    @property
    def enabled(self):
        return self._enabled

    def missing(self):
        return list(self._missing)


class TestLogFeed(unittest.TestCase):
    def _feed(self, fetch, *, enabled=True, missing=(), ttl=999):
        cfg = config()
        cfg.railway = FakeRailwayConfig(enabled=enabled, missing=missing)
        cfg.secrets = ("sk-live-abcdefghijklmnop",)
        return panel.LogFeed(cfg, ttl, fetch=fetch)

    def test_disabled_says_exactly_which_variable_is_missing(self):
        """「日志不可用」这种话没用。"""
        feed = self._feed(lambda *a, **k: [], enabled=False,
                          missing=["RAILWAY_API_TOKEN"])
        lines, message = feed.get()
        self.assertEqual(lines, [])
        self.assertIn("RAILWAY_API_TOKEN", message)

    def test_secrets_are_handed_to_the_fetcher(self):
        seen = {}

        def fetch(cfg, *, secrets=()):
            seen["secrets"] = secrets
            return []
        self._feed(fetch).get()
        self.assertIn("sk-live-abcdefghijklmnop", seen["secrets"])

    def test_result_is_cached_within_the_ttl(self):
        calls = []

        def fetch(cfg, *, secrets=()):
            calls.append(1)
            return [railway.LogLine(message="x")]
        feed = self._feed(fetch)
        feed.get()
        feed.get()
        self.assertEqual(len(calls), 1)

    def test_force_bypasses_the_cache(self):
        calls = []

        def fetch(cfg, *, secrets=()):
            calls.append(1)
            return []
        feed = self._feed(fetch)
        feed.get()
        feed.get(force=True)
        self.assertEqual(len(calls), 2)

    def test_a_railway_failure_keeps_the_last_batch(self):
        """日志少一次刷新不该让页面上那一块变空白。"""
        state = {"fail": False}

        def fetch(cfg, *, secrets=()):
            if state["fail"]:
                raise railway.RailwayError("429 限流")
            return [railway.LogLine(message="上一批")]
        feed = self._feed(fetch, ttl=0)
        feed.get()
        state["fail"] = True
        lines, error = feed.get()
        self.assertEqual([l.message for l in lines], ["上一批"])
        self.assertIn("429", error)

    def test_an_unexpected_exception_is_contained(self):
        def fetch(cfg, *, secrets=()):
            raise ValueError("没想到的错")
        lines, error = self._feed(fetch).get()
        self.assertEqual(lines, [])
        self.assertIn("没想到的错", error)


class TestSecretsList(unittest.TestCase):
    def test_collects_every_secret_that_could_show_up_in_a_log(self):
        env = {"TIKHUB_API_KEY": "tk", "SOCIALDATAX_API_KEY": "sd",
               "FEISHU_APP_SECRET": "fs", "RAILWAY_API_TOKEN": "rw",
               "PANEL_PASSWORD": "pw"}
        found = panel._secrets(env)
        for value in env.values():
            self.assertIn(value, found)

    def test_empty_values_are_dropped(self):
        self.assertEqual(panel._secrets({"TIKHUB_API_KEY": "  "}), ())


class TestRunHistoryRendering(unittest.TestCase):
    def _page(self, runs, log_error=""):
        overview = summary.Overview(
            projects=[summary.ProjectSnapshot(label="A", app_token="t",
                                              table_id="tb")],
            generated_at=NOW)
        return panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(), csrf="tok", runs=runs, log_error=log_error)

    def _run(self, **kwargs):
        base = dict(run_id="r1", mode="sweep", started_at=NOW.timestamp(),
                    ended_at=NOW.timestamp() + 30, exit_code=0, rows=7,
                    cost_yuan=0.5)
        base.update(kwargs)
        return railway.Run(**base)

    def test_no_runs_explains_what_is_needed(self):
        page = self._page([])
        self.assertIn("RUN_LOG_JSON", page)

    def test_a_normal_run_reads_as_normal(self):
        page = self._page([self._run()])
        self.assertIn("正常", page)
        self.assertIn("¥0.50", page)

    def test_an_unfinished_run_is_called_out(self):
        """有 run_start 没 run_end = 被容器杀掉了，那几轮的钱多半白花了。"""
        page = self._page([self._run(ended_at=None, exit_code=None)])
        self.assertIn("没跑完", page)
        self.assertIn("被容器杀掉", page)

    def test_a_tripped_breaker_is_shown(self):
        run = self._run()
        run.tables = [railway.TableRun(label="A", breaker_tripped=True)]
        self.assertIn("已熔断", self._page([run]))

    def test_failovers_are_surfaced(self):
        run = self._run()
        run.tables = [railway.TableRun(label="A", failovers=4)]
        self.assertIn("降级 4 次", self._page([run]))

    def test_log_error_is_shown_without_hiding_the_runs(self):
        page = self._page([self._run()], log_error="429 限流")
        self.assertIn("429 限流", page)
        self.assertIn("¥0.50", page)

    def test_run_fields_are_escaped(self):
        page = self._page([self._run(error="<script>alert(1)</script>")])
        self.assertNotIn("<script>alert(1)</script>", page)


class TestUnknownEstimateIsNotZero(unittest.TestCase):
    """缺「最近检查时间」列时，`load_rows` 直接 return []，于是「到期待刷」
    和「预计花费」都是 0——而真相是**算不出来**。

    这个 0 出现的时机恰恰最危险：把那一列建出来之后全表都会判到期，
    而人刚刚才看着 0 放下心来。
    """

    def _snap(self, drop_last_updated):
        from xhsearch import schema as schema_mod
        settings = Settings()
        meta = {}
        for name, allowed, _l, options, _n in schema_mod.expected_schema(settings):
            if drop_last_updated and name == settings.fields.last_updated:
                continue
            meta[name] = {"type": allowed[0], "ui_type": "",
                          "options": list(options) if options else None}

        class Table:
            app_token, table_id = "t", "tb"

            def fields_meta(self):
                return meta

            def search(self, fields, *, filter_spec=None, **kwargs):
                return [{"record_id": "r1", "fields": {
                    settings.fields.link: "https://www.xiaohongshu.com/explore/aa"}}]

        return panel.collect([("A", Table())], settings, {}, now=NOW).projects[0]

    def test_missing_column_blocks_the_estimate(self):
        snap = self._snap(drop_last_updated=True)
        self.assertTrue(snap.estimate_blocked)
        self.assertIn("算不出来", snap.estimate_blocked)

    def test_a_healthy_table_estimates_normally(self):
        self.assertEqual(self._snap(drop_last_updated=False).estimate_blocked, "")

    def test_the_card_says_so_instead_of_showing_a_number(self):
        snap = self._snap(drop_last_updated=True)
        page = panel_view.overview_page(
            overview=summary.Overview(projects=[snap], generated_at=NOW),
            error="", fetched_at=NOW.timestamp(), config=config(), csrf="t")
        self.assertIn("无法估算", page)
        self.assertNotIn("≈ ¥0.00", page)

    def test_the_top_bar_flags_that_the_total_is_only_a_lower_bound(self):
        blocked = summary.ProjectSnapshot(label="A", app_token="t", table_id="tb",
                                          estimate_blocked="缺列")
        fine = summary.ProjectSnapshot(label="B", app_token="t2", table_id="tb2",
                                       due_yuan=3.0)
        overview = summary.Overview(projects=[blocked, fine], generated_at=NOW)
        self.assertEqual([p.label for p in overview.unestimatable], ["A"])
        page = panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(), csrf="t")
        self.assertIn("算不出", page)


class TestProjectRoutes(unittest.TestCase):
    """项目页的写路径。它们改的是**注册表**（这套东西自己的配置存储），
    以及业务表的**结构**（只追加）——业务表的数据一个字都不碰。
    """

    def _projects(self, **overrides):
        cfg = config()
        cfg.registry_target = "bascnREG:tblREG"
        cfg.app_id = "cli_x"
        cfg.app_secret = "s"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return panel.Projects(cfg, Settings(), log=lambda *a: None), cfg

    def test_disabled_until_the_registry_is_configured(self):
        cfg = config()
        cfg.app_id = "cli_x"
        projects = panel.Projects(cfg, Settings())
        self.assertFalse(projects.enabled)
        self.assertIn("init-registry", projects.why_disabled())

    def test_missing_app_id_is_reported_distinctly(self):
        projects = panel.Projects(config(), Settings())
        self.assertIn("FEISHU_APP_ID", projects.why_disabled())

    def test_enabled_when_both_are_set(self):
        projects, _ = self._projects()
        self.assertTrue(projects.enabled)

    def test_build_refuses_when_not_a_collaborator(self):
        """读不到字段列表就不该乱建列——先把提示给对。"""
        projects, _ = self._projects()
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=None):
            with self.assertRaises(panel.feishu.FeishuError) as ctx:
                projects.build("bascnA", "tblB")
        self.assertIn("添加文档应用", str(ctx.exception))

    def test_build_passes_the_option_switch_through(self):
        from xhsearch import provision as provision_mod
        for allowed in (False, True):
            projects, _ = self._projects(allow_option_patch=allowed)
            with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                                   return_value={"x": {"type": 1, "ui_type": "",
                                                       "options": None}}), \
                 mock.patch.object(provision_mod, "build_missing") as built:
                projects.build("bascnA", "tblB")
            self.assertEqual(built.call_args.kwargs["allow_option_patch"], allowed)

    def test_remove_only_touches_the_registry_row(self):
        from xhsearch import registry as registry_mod
        projects, _ = self._projects()
        with mock.patch.object(registry_mod, "remove") as removed:
            projects.remove("rec1")
        self.assertEqual(removed.call_args[0][1], "rec1")


class FakeStore:
    """内存版的面板设置表。"""

    def __init__(self, **data):
        self.data = dict(data)

    def get_json(self, key, default):
        return self.data.get(key, default)

    def set_json(self, key, value):
        self.data[key] = value


class TestShareSettings(unittest.TestCase):
    """新建表给谁开权限：可管理只来自后台环境变量；可编辑的群在面板上选
    （存注册表 base 里），环境变量里配的群也认。"""

    def _projects(self, store=None, **overrides):
        from xhsearch import panel_settings
        cfg = config()
        cfg.registry_target = "bascnREG:tblREG"
        cfg.app_id = "cli_x"
        cfg.app_secret = "s"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        projects = panel.Projects(cfg, Settings(), log=lambda *a: None)
        projects._settings_store = store or FakeStore()
        return projects, panel_settings

    def test_state_merges_env_and_panel_and_says_which_is_which(self):
        store = FakeStore(**{"share.editor_chats": [{"chat_id": "oc_1", "name": "运营群"}]})
        projects, _ = self._projects(store, table_managers=("13800008888",),
                                     table_editor_chats=("oc_env",), table_owner="")
        state = projects.share_state()
        self.assertEqual([(m["id"], m["source"]) for m in state["managers"]],
                         [("13800008888", "env")])
        self.assertEqual([(c["chat_id"], c["name"], c["source"]) for c in state["editor_chats"]],
                         [("oc_env", "", "env"), ("oc_1", "运营群", "panel")])
        plan = projects.share_plan()
        self.assertEqual(plan.managers, ("13800008888",))
        self.assertEqual(plan.editor_chats, ("oc_env", "oc_1"))
        self.assertEqual(plan.label("oc_1"), "运营群")
        self.assertEqual(plan.label("oc_env"), "oc_env")

    def test_managers_come_only_from_the_backend(self):
        """面板口令是运营共用的：面板上要是能加可管理的人，谁拿到口令谁就能给
        自己开所有新表的管理权。所以设置表里就算有人塞了一份，也不认。"""
        store = FakeStore(**{"share.managers": [{"id": "ou_evil", "label": "x"}]})
        projects, _ = self._projects(store, table_managers=())
        self.assertEqual(projects.share_state()["managers"], [])
        self.assertEqual(projects.share_plan().managers, ())
        self.assertFalse(hasattr(projects, "add_manager"))
        self.assertFalse(hasattr(projects, "remove_manager"))

    def test_chats_are_replaced_as_a_whole_and_must_be_oc_ids(self):
        projects, ps = self._projects()
        state = projects.set_editor_chats([{"chat_id": "oc_1", "name": "运营群"},
                                           {"chat_id": "oc_1", "name": "运营群"},
                                           "garbage"])
        self.assertEqual(projects._settings_store.data[ps.KEY_EDITOR_CHATS],
                         [{"chat_id": "oc_1", "name": "运营群"}])
        self.assertEqual(state["editor_chats"][0]["name"], "运营群")
        with self.assertRaises(ValueError):
            projects.set_editor_chats([{"chat_id": "运营群"}])
        projects.set_editor_chats([])
        self.assertEqual(projects._settings_store.data[ps.KEY_EDITOR_CHATS], [])

    def test_apply_uses_the_merged_plan_on_an_existing_table(self):
        from xhsearch import provision as provision_mod
        from xhsearch import registry as registry_mod
        store = FakeStore(**{"share.editor_chats": [{"chat_id": "oc_1", "name": "运营群"}]})
        projects, _ = self._projects(store, table_managers=("boss@x.com",))
        known = [registry_mod.Entry(label="旧表", target="bascnOLD:tblA",
                                    app_token="bascnOLD", table_id="tblA")]
        with mock.patch.object(panel.Projects, "list", return_value=known), \
             mock.patch.object(provision_mod, "share_table",
                               return_value=provision_mod.ShareResult(granted=["x"])) as shared:
            result = projects.apply_share("bascnOLD")
        self.assertEqual(result.granted, ["x"])
        _workspace, app_token, plan = shared.call_args[0]
        self.assertEqual(app_token, "bascnOLD")
        self.assertEqual(plan.managers, ("boss@x.com",))
        self.assertEqual(plan.editor_chats, ("oc_1",))
        with self.assertRaises(ValueError):
            projects.apply_share("")

    def test_apply_refuses_a_table_that_is_not_in_the_registry(self):
        """面板口令是运营共用的：不查清单的话，知道任何一个应用能管的 base 的
        app_token，就能把配好的群和人加到那个 base 上去。"""
        from xhsearch import provision as provision_mod
        from xhsearch import registry as registry_mod
        projects, _ = self._projects(table_managers=("boss@x.com",))
        known = [registry_mod.Entry(label="旧表", target="bascnOLD:tblA",
                                    app_token="bascnOLD", table_id="tblA")]
        with mock.patch.object(panel.Projects, "list", return_value=known), \
             mock.patch.object(provision_mod, "share_table") as shared:
            with self.assertRaises(ValueError) as ctx:
                projects.apply_share("bascnSOMEONE_ELSES")
        shared.assert_not_called()
        self.assertIn("不在监控清单里", str(ctx.exception))

    def test_the_store_lives_in_the_registry_base(self):
        projects = panel.Projects(self._projects()[0].config, Settings(), log=lambda *a: None)
        self.assertEqual(projects._store().app_token, "bascnREG")


class TestProjectRoutesOverHttp(unittest.TestCase):
    """路由层：鉴权、CSRF、错误翻译。"""

    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)

        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cls.config.registry_target = "bascnREG:tblREG"
        cls.config.app_id = "cli_x"
        cls.config.app_secret = "s"
        cls.actions = mock.Mock(spec=panel.Projects)
        cls.actions.enabled = True
        cls.actions.why_disabled.return_value = ""
        cls.actions.list.return_value = []
        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, projects=cls.actions,
                                        host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.actions.reset_mock()
        self.actions.enabled = True
        self.actions.list.return_value = []

    def _call(self, path, *, data=None, headers=None, method=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{self.port}{path}",
                                     data=data, headers=headers or {}, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _login(self):
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(f"http://localhost:{self.port}/login",
                                     data=b"password=" + GOOD_PASSWORD.encode())
        try:
            resp = opener.open(req, timeout=5)
            cookie = resp.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie") or ""
        token = cookie.split("=", 1)[1].split(";")[0]
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token),
                "Content-Type": "application/json"}

    def test_listing_needs_a_session(self):
        self.assertEqual(self._call("/api/projects")[0], 401)

    def test_writes_need_a_session(self):
        status, _ = self._call("/api/projects/remove", data=b"{}", method="POST")
        self.assertEqual(status, 401)

    def test_writes_need_the_csrf_header(self):
        headers = self._login()
        headers.pop(panel.CSRF_HEADER)
        status, _ = self._call("/api/projects/remove", data=b'{"record_id":"r"}',
                               headers=headers, method="POST")
        self.assertEqual(status, 403)

    def test_a_valid_remove_reaches_the_action_layer(self):
        status, _ = self._call("/api/projects/remove", data=b'{"record_id":"rec9"}',
                               headers=self._login(), method="POST")
        self.assertEqual(status, 200)
        self.actions.remove.assert_called_once_with("rec9")

    def test_create_passes_the_template_and_defaults_to_full(self):
        self.actions.create.return_value = {"target": "b:t", "columns": 37}
        status, body = self._call(
            "/api/projects/create", data=json.dumps({"name": "甲"}).encode(),
            headers=self._login(), method="POST")
        self.assertEqual(status, 200)
        self.actions.create.assert_called_with("甲", template="full")
        self.assertEqual(json.loads(body)["created"]["columns"], 37)
        self._call("/api/projects/create",
                   data=json.dumps({"name": "乙", "template": "monitor"}).encode(),
                   headers=self._login(), method="POST")
        self.actions.create.assert_called_with("乙", template="monitor")

    def test_listing_chats_is_read_only_and_needs_a_session(self):
        self.assertEqual(self._call("/api/chats")[0], 401)
        self.actions.list_chats.return_value = [{"chat_id": "oc_1", "name": "运营群"}]
        status, body = self._call("/api/chats", headers=self._login())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["chats"][0]["chat_id"], "oc_1")

    def test_share_state_needs_a_session_and_is_read_only(self):
        self.assertEqual(self._call("/api/share")[0], 401)
        self.actions.share_state.return_value = {"managers": [], "editor_chats": [], "owner": ""}
        status, body = self._call("/api/share", headers=self._login())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["share"]["managers"], [])

    def test_share_writes_reach_the_action_layer(self):
        self.actions.set_editor_chats.return_value = {"ok": 1}
        headers = self._login()
        status, body = self._call("/api/projects/share_chats",
                                  data=json.dumps({"chats": [{"chat_id": "oc_1", "name": "群"}]}).encode(),
                                  headers=headers, method="POST")
        self.assertEqual(status, 200)
        self.actions.set_editor_chats.assert_called_with([{"chat_id": "oc_1", "name": "群"}])
        status, _ = self._call("/api/projects/share_chats", data=b'{"chats": "x"}',
                               headers=headers, method="POST")
        self.assertEqual(status, 400)

    def test_there_is_no_route_to_add_a_manager(self):
        """可管理的人只在后台环境变量里定：面板上没有这条路，连动作都没有。"""
        status, _ = self._call("/api/projects/share_manager",
                               data=json.dumps({"who": "13800000000"}).encode(),
                               headers=self._login(), method="POST")
        self.assertEqual(status, 404)

    def test_share_apply_reports_granted_and_failures(self):
        from xhsearch import provision as provision_mod
        self.actions.apply_share.return_value = provision_mod.ShareResult(
            granted=["群 运营群 可编辑"], failures=["给「x」开可管理失败：nope"])
        status, body = self._call("/api/projects/share_apply",
                                  data=json.dumps({"app_token": "bascnOLD"}).encode(),
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 200)
        self.actions.apply_share.assert_called_with("bascnOLD")
        payload = json.loads(body)
        self.assertEqual(payload["granted"], ["群 运营群 可编辑"])
        self.assertFalse(payload["ok"])

    def test_listing_chats_translates_the_missing_scope(self):
        self.actions.list_chats.side_effect = feishu.FeishuError(99991672, "no scope")
        status, body = self._call("/api/chats", headers=self._login())
        self.assertEqual(status, 400)
        self.assertIn("im:chat:readonly", body)

    def test_create_refuses_an_unknown_template(self):
        status, body = self._call(
            "/api/projects/create",
            data=json.dumps({"name": "甲", "template": "everything"}).encode(),
            headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("template", body)
        self.actions.create.assert_not_called()

    def test_a_bad_link_becomes_a_400_with_the_reason(self):
        from xhsearch import tablespec
        self.actions.check.side_effect = tablespec.BadTarget("这一项看不懂")
        status, body = self._call("/api/projects/check",
                                  data=b'{"label":"x","target":"junk"}',
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("看不懂", body)

    def test_a_feishu_error_carries_its_remedy(self):
        """面板要把「怎么修」摆在错误旁边，而不是让人从报错文本里自己找。"""
        self.actions.build.side_effect = panel.feishu.FeishuError(
            1254302, "no permission", "这张表开了「高级权限」")
        status, body = self._call("/api/projects/build",
                                  data=b'{"app_token":"a","table_id":"t"}',
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("高级权限", body)

    def test_a_non_json_body_is_rejected_cleanly(self):
        status, body = self._call("/api/projects/add", data=b"not json",
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body)

    def test_an_unknown_action_is_404(self):
        status, _ = self._call("/api/projects/frobnicate", data=b"{}",
                               headers=self._login(), method="POST")
        self.assertEqual(status, 404)

    def test_oversized_body_is_still_capped(self):
        status, _ = self._call("/api/projects/add", data=b"{" + b"x" * 70000,
                               headers=self._login(), method="POST")
        self.assertEqual(status, 413)

    def _token_of(self, body: bytes) -> str:
        self._call("/api/projects/add", data=body,
                   headers=self._login(), method="POST")
        return self.actions.add.call_args.kwargs["client_token"]

    def assertCanonicalUuid(self, token: str) -> None:
        """规范形式 8-4-4-4-12，**而且版本位是 4**。

        飞书文档的原话是「格式为标准的 uuidv4」，不合规报 1254037。
        判据放松任何一格，这条测试就又没有牙：解析器收没有连字符的
        十六进制串，规范形式又收 UUIDv5——两次线上失败正好各占一样。
        """
        parsed = uuid.UUID(token)
        self.assertEqual(str(parsed), token, f"不是规范 UUID：{token!r}")
        self.assertEqual(parsed.version, 4,
                         f"飞书要 uuidv4，这个是 v{parsed.version}：{token!r}")

    def test_add_generates_an_idempotency_key_when_absent(self):
        """**必须是合法 UUID**，不是「非空就行」。

        飞书的 client_token 只吃标准 UUID，格式不对整条 batch_create 就报
        `Invalid client token, make sure that it complies with the specification.`
        原来这条只断言非空——空串会红，而线上真正送过去的那个
        `add-https://…?table=…-中文` 全绿。它就是这次漏网的直接原因。
        """
        self.assertCanonicalUuid(self._token_of(b'{"label":"x","target":"a:b"}'))

    def test_the_key_the_browser_sends_is_normalised_not_passed_through(self):
        """前端那个键是按 (target, label) 拼的，带 `://`、`?`、空格、中文。

        它是**幂等语义的种子**，不是 token 本身——原样透传给飞书就是这次的错。
        """
        raw = ("add-https://piqijafyg8a.feishu.cn/base/S72ObviZAa7P0AsueX1cpsYRnN6"
               "?table=tblXXXX-Hatherine 素人执行表单")
        token = self._token_of(json.dumps(
            {"label": "Hatherine 素人执行表单", "target": "a:b",
             "client_token": raw}).encode())
        self.assertCanonicalUuid(token)
        self.assertNotEqual(token, raw)

    def test_the_same_add_twice_keeps_the_same_key(self):
        """连点两次「加」不该多出两行——归一化不能把幂等性弄丢。"""
        raw = json.dumps({"label": "甲", "target": "a:b",
                          "client_token": "add-a:b-甲"}).encode()
        other = json.dumps({"label": "乙", "target": "a:b",
                            "client_token": "add-a:b-乙"}).encode()
        self.assertEqual(self._token_of(raw), self._token_of(raw))
        self.assertNotEqual(self._token_of(raw), self._token_of(other))

    def test_disabled_projects_explain_why(self):
        self.actions.enabled = False
        self.actions.why_disabled.return_value = "还没配 FEISHU_REGISTRY"
        status, body = self._call("/api/projects/add", data=b"{}",
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("FEISHU_REGISTRY", body)


class TestQueueing(unittest.TestCase):
    """勾「排队刷新」。面板自己一个付费请求都不发——勾上之后 cron 五分钟内
    接手，和运营手工勾同一条路，同样受 MAX_RECORDS_PER_RUN 约束。"""

    def _queueing(self):
        cfg = config()
        cfg.app_id, cfg.app_secret = "cli_x", "s"
        return panel.Queueing(cfg, Settings(), log=lambda *a: None)

    def _meta(self, queued_type=7, present=True):
        f = Settings().fields
        meta = {f.link: {"type": 1, "ui_type": "", "options": None}}
        if present:
            meta[f.queued] = {"type": queued_type, "ui_type": "", "options": None}
        return meta

    def test_writes_only_the_queued_column(self):
        captured = {}

        def batch_update(self, updates, **kwargs):
            captured["updates"] = updates
            return len(updates)

        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=self._meta()), \
             mock.patch.object(panel.feishu.Bitable, "batch_update", batch_update):
            result = self._queueing().queue("bascnA", "tblB", ["r1", "r2"])
        self.assertEqual(result.queued, 2)
        for update in captured["updates"]:
            self.assertEqual(list(update["fields"]), [Settings().fields.queued])
            self.assertIs(update["fields"][Settings().fields.queued], True)

    def test_refuses_when_the_column_is_missing(self):
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=self._meta(present=False)):
            with self.assertRaises(panel.feishu.FeishuError) as ctx:
                self._queueing().queue("bascnA", "tblB", ["r1"])
        self.assertIn("先在面板上把它建出来", str(ctx.exception))

    def test_refuses_when_the_column_is_not_a_checkbox(self):
        """勾上去会写失败，不如提前说清。"""
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=self._meta(queued_type=1)):
            with self.assertRaises(panel.feishu.FeishuError) as ctx:
                self._queueing().queue("bascnA", "tblB", ["r1"])
        self.assertIn("不是复选框", str(ctx.exception))

    def test_refuses_when_not_a_collaborator(self):
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=None):
            with self.assertRaises(panel.feishu.FeishuError) as ctx:
                self._queueing().queue("bascnA", "tblB", ["r1"])
        self.assertIn("添加文档应用", str(ctx.exception))

    def test_an_empty_list_touches_nothing(self):
        with mock.patch.object(panel.feishu.Bitable, "fields_meta") as meta:
            self.assertEqual(self._queueing().queue("a", "b", []).queued, 0)
        meta.assert_not_called()

    def test_row_level_failures_are_reported_not_swallowed(self):
        def batch_update(self, updates, *, errors=None, **kwargs):
            errors.append(("r2", panel.feishu.FeishuError(1254043, "行没了")))
            return 1
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=self._meta()), \
             mock.patch.object(panel.feishu.Bitable, "batch_update", batch_update):
            result = self._queueing().queue("bascnA", "tblB", ["r1", "r2"])
        self.assertEqual(result.queued, 1)
        self.assertIn("行没了", result.failures[0])
        # 前端只给真勾上的行标「排队中」：失败的那行不能在里面
        self.assertEqual(result.record_ids, ["r1"])


class TestQueueRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cls.config.app_id, cls.config.app_secret = "cli_x", "s"
        cls.queueing = mock.Mock(spec=panel.Queueing)
        cls.queueing.queue.return_value = panel.QueueResult(queued=1)
        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, queueing=cls.queueing,
                                        host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.queueing.reset_mock()
        self.queueing.queue.return_value = panel.QueueResult(queued=1)

    def _call(self, data, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{self.port}/api/queue",
                                     data=data, headers=headers or {}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def _auth(self):
        import urllib.error
        import urllib.request

        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NR)
        req = urllib.request.Request(f"http://localhost:{self.port}/login",
                                     data=b"password=" + GOOD_PASSWORD.encode())
        try:
            cookie = opener.open(req, timeout=5).headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie")
        token = cookie.split("=", 1)[1].split(";")[0]
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token)}

    def test_needs_a_session_and_a_csrf_header(self):
        self.assertEqual(self._call(b'{"rows":[]}')[0], 401)
        auth = self._auth()
        auth.pop(panel.CSRF_HEADER)
        self.assertEqual(self._call(b'{"rows":[]}', auth)[0], 403)

    def test_groups_rows_by_table(self):
        """飞书官方建议同一张表同一时刻只做一个写操作。"""
        body = ('{"rows":[{"app_token":"a","table_id":"t1","record_id":"r1"},'
                '{"app_token":"a","table_id":"t1","record_id":"r2"},'
                '{"app_token":"b","table_id":"t2","record_id":"r3"}]}')
        status, _ = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(self.queueing.queue.call_count, 2)
        first = self.queueing.queue.call_args_list[0][0]
        self.assertEqual(first[2], ["r1", "r2"])

    def test_a_huge_selection_is_refused(self):
        """一次「全选」勾几千行 = 下一轮直接顶穿预算。"""
        rows = [{"app_token": "a", "table_id": "t", "record_id": f"r{i}"}
                for i in range(panel.MAX_QUEUE_ROWS + 1)]
        status, body = self._call(json.dumps({"rows": rows}).encode(), self._auth())
        self.assertEqual(status, 400)
        self.assertIn(str(panel.MAX_QUEUE_ROWS), body)
        self.queueing.queue.assert_not_called()

    def test_one_broken_table_does_not_lose_the_others(self):
        self.queueing.queue.side_effect = [
            panel.feishu.FeishuError(-1, "权限没了"),
            panel.QueueResult(queued=2)]
        body = ('{"rows":[{"app_token":"a","table_id":"t1","record_id":"r1"},'
                '{"app_token":"b","table_id":"t2","record_id":"r2"}]}')
        status, out = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200)
        self.assertIn("权限没了", out)
        self.assertIn('"queued": 2', out)

    def test_malformed_rows_are_skipped_not_fatal(self):
        body = ('{"rows":["junk",{"app_token":"","table_id":"t","record_id":"r"},'
                '{"app_token":"a","table_id":"t","record_id":"ok"}]}')
        status, _ = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(self.queueing.queue.call_args[0][2], ["ok"])

    def test_a_bad_body_is_a_400(self):
        self.assertEqual(self._call(b"not json", self._auth())[0], 400)
        self.assertEqual(self._call(b'{"nope":1}', self._auth())[0], 400)


if __name__ == "__main__":
    unittest.main()


class TestCacheRefreshWaitsForTheOneInFlight(unittest.TestCase):
    """停用一张表之后紧接着 refresh()，撞上后台正在跑的那趟（它读注册表时
    那张表还是启用的）。原来的写法是「有人在跑就直接返回」——空手而归，
    页面重载后那张表照样在，看着就像停用没生效。"""

    def _blocking_cache(self):
        import threading
        import time
        entered, release, calls = threading.Event(), threading.Event(), []

        def produce():
            calls.append(time.monotonic())
            if len(calls) == 1:
                entered.set()
                release.wait(5)
            return summary.Overview(projects=[])

        return panel.Cache(produce, ttl=999), entered, release, calls

    def test_a_refresh_asked_mid_flight_still_gets_a_fresh_snapshot(self):
        import threading
        import time
        cache, entered, release, calls = self._blocking_cache()
        first = threading.Thread(target=cache.refresh)
        first.start()
        self.assertTrue(entered.wait(5))
        second = threading.Thread(target=cache.refresh)
        second.start()
        time.sleep(0.05)
        self.assertTrue(second.is_alive(), "第二个调用者不该空手而归")
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual(len(calls), 2,
                         "第一趟跑完必须再补一趟——它开跑时读到的世界可能已经变了")

    def test_everyone_who_waited_shares_one_follow_up_refresh(self):
        """五个人同时点「重新取数」撞上正在跑的那趟：只补跑一趟，不是五趟。"""
        import threading
        import time
        cache, entered, release, calls = self._blocking_cache()
        first = threading.Thread(target=cache.refresh)
        first.start()
        self.assertTrue(entered.wait(5))
        waiters = [threading.Thread(target=cache.refresh) for _ in range(5)]
        for w in waiters:
            w.start()
        time.sleep(0.1)
        release.set()
        first.join(5)
        for w in waiters:
            w.join(5)
        self.assertEqual(len(calls), 2)


class TestQueueRefusesTablesNotUnderWatch(unittest.TestCase):
    """勾「排队刷新」只给还在监控中的表。给停用/已移除/配置有误的表勾，
    勾会写进飞书、面板回「等 cron 接手」，然后永远没人来接——那句话是假的，
    而且那些勾会悬着，哪天一启用就集中花一笔钱。"""

    @classmethod
    def setUpClass(cls):
        from xhsearch import registry as registry_mod
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cls.config.app_id, cls.config.app_secret = "cli_x", "s"
        cls.queueing = mock.Mock(spec=panel.Queueing)
        cls.queueing.queue.return_value = panel.QueueResult(queued=1)

        def entry(label, table_id, enabled, problem=""):
            e = registry_mod.Entry(record_id="r-" + table_id, label=label,
                                   target="bascnA:" + table_id, enabled=enabled,
                                   app_token="bascnA", table_id=table_id)
            e.problem = problem
            return e
        cls.projects = mock.Mock()
        cls.projects.enabled = True
        cls.entries = [entry("在管", "tblON", True),
                       entry("停了", "tblOFF", False),
                       entry("坏的", "tblBAD", True, "「表格链接」是空的")]
        cls.projects.list.return_value = cls.entries
        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, queueing=cls.queueing,
                                        projects=cls.projects, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.queueing.reset_mock()
        self.queueing.queue.side_effect = None
        self.queueing.queue.return_value = panel.QueueResult(queued=1, record_ids=["r1"])
        self.projects.list.side_effect = None
        self.projects.list.return_value = self.entries

    def _call(self, data, headers=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{self.port}/api/queue",
                                     data=data, headers=headers or {}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def _auth(self):
        import urllib.error
        import urllib.request

        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NR)
        req = urllib.request.Request(f"http://localhost:{self.port}/login",
                                     data=b"password=" + GOOD_PASSWORD.encode())
        try:
            cookie = opener.open(req, timeout=5).headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie")
        token = cookie.split("=", 1)[1].split(";")[0]
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token)}

    def test_only_the_table_still_under_watch_gets_written(self):
        body = ('{"rows":['
                '{"app_token":"bascnA","table_id":"tblON","record_id":"r1"},'
                '{"app_token":"bascnA","table_id":"tblOFF","record_id":"r2"},'
                '{"app_token":"bascnA","table_id":"tblOFF","record_id":"r3"},'
                '{"app_token":"bascnA","table_id":"tblBAD","record_id":"r4"},'
                '{"app_token":"bascnA","table_id":"tblGONE","record_id":"r5"}]}')
        status, text = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200, text)
        payload = json.loads(text)
        # 真正写进飞书的只有还在监控中的那张
        self.assertEqual(self.queueing.queue.call_count, 1)
        self.assertEqual(self.queueing.queue.call_args[0][1], "tblON")
        self.assertEqual(payload["queued"], 1)
        # 前端只给这些 record_id 标「排队中」——被拒的表一行都不在里面
        self.assertEqual(payload["queued_records"], ["r1"])
        # 拒掉的三张各说清楚为什么、几行没勾
        joined = "\n".join(payload["skipped"])
        self.assertIn("「停了」已停用", joined)
        self.assertIn("这 2 行没勾", joined)
        self.assertIn("「坏的」配置有误", joined)
        self.assertIn("不在监控清单里", joined)
        self.assertNotIn("失败", joined, "拒勾是决定，不是写入失败，别混进 failures 的口径")

    def test_a_disabled_duplicate_row_does_not_shadow_the_live_one(self):
        """注册表允许同一张表「一行在用 + 一行停用的历史」（查重只看在用的行）。
        按 table_id 归并时在用的那行必须优先——不然一行历史就能把正在巡查的
        表拒成「已停用」。"""
        from xhsearch import registry as registry_mod
        live = self.entries[0]
        stale = registry_mod.Entry(record_id="r-old", label="在管（旧）",
                                   target="bascnA:tblON", enabled=False,
                                   app_token="bascnA", table_id="tblON")
        self.projects.list.return_value = [live, stale]      # 停用的历史排在后面
        body = '{"rows":[{"app_token":"bascnA","table_id":"tblON","record_id":"r1"}]}'
        status, text = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200, text)
        self.assertEqual(self.queueing.queue.call_count, 1)
        self.assertEqual(json.loads(text)["skipped"], [])

    def test_rows_of_a_table_whose_write_blew_up_are_not_called_queued(self):
        """整表写炸的那几行只出现在 failures 里，绝不能出现在 queued_records 里
        ——前端按后者标「排队中」，标了就是替 cron 做一个它不会兑现的承诺。"""
        self.queueing.queue.side_effect = panel.feishu.FeishuError(99991663, "token 过期")
        body = '{"rows":[{"app_token":"bascnA","table_id":"tblON","record_id":"r1"}]}'
        status, text = self._call(body.encode(), self._auth())
        self.assertEqual(status, 200, text)
        payload = json.loads(text)
        self.assertEqual(payload["queued_records"], [])
        self.assertEqual(payload["queued"], 0)
        self.assertTrue(payload["failures"] and "token 过期" in payload["failures"][0])

    def test_an_unreadable_registry_refuses_everything(self):
        """确认不了这些表还在不在监控中，就一行都不勾——勾错了要悬到下次
        启用才发现，重试只是再点一下。"""
        self.projects.list.side_effect = RuntimeError("网络挂了")
        body = '{"rows":[{"app_token":"bascnA","table_id":"tblON","record_id":"r1"}]}'
        status, text = self._call(body.encode(), self._auth())
        self.assertEqual(status, 400)
        self.assertIn("注册表", json.loads(text)["error"])
        self.queueing.queue.assert_not_called()


class TestDisabledTablesAreSaidOutLoud(unittest.TestCase):
    """停用的表是从页面上整体消失的。不说一句，「停用了」和「读不到了」
    在运营眼里长得一模一样——而两者要做的事完全不同。"""

    def _page(self, overview):
        from xhsearch import panel_view
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": "a-long-enough-password",
            "FEISHU_DOMAIN": "https://x.feishu.cn"})
        return panel_view.overview_page(overview=overview, error="",
                                        fetched_at=0.0, config=cfg, csrf="tok")

    def _snap(self, **kwargs):
        base = dict(label="项目A", app_token="t", table_id="tb")
        base.update(kwargs)
        return summary.ProjectSnapshot(**base)

    def test_disabled_tables_are_listed_next_to_the_live_ones(self):
        page = self._page(summary.Overview(projects=[self._snap()],
                                           disabled_tables=["旧项目"]))
        self.assertIn("张表已停用：旧项目", page)
        self.assertIn("点「启用」", page)

    def test_all_disabled_is_not_reported_as_no_tables(self):
        """表都在，只是全停了。说「还没有一张可用的表」会把人引去加表。"""
        page = self._page(summary.Overview(projects=[], disabled_tables=["甲", "乙"]))
        self.assertIn("2 张表全都停用了", page)
        self.assertIn("甲、乙", page)
        self.assertNotIn("还没有一张可用的表", page)

    def test_truly_empty_registry_keeps_the_add_a_table_hint(self):
        page = self._page(summary.Overview(projects=[]))
        self.assertIn("还没有一张可用的表", page)
        # 查的是那两句说明，不是「已停用」四个字——项目清单的 JS 里本来就有
        # 一枚「已停用」芯片的文案，页面字节里永远含它。
        self.assertNotIn("张表已停用", page)
        self.assertNotIn("全都停用了", page)

    def _todo_table(self, todo):
        """只看待办那张表。「排队中」四个字页面字节里永远有——前端就地补标记
        的脚本里写着它——所以要查的是服务端有没有把它渲染进那一行。"""
        page = self._page(summary.Overview(projects=[self._snap(todos=[todo])]))
        start = page.index("<table class=todos>")
        return page[start:page.index("</table>", start)]

    def test_a_queued_todo_shows_the_pending_marker(self):
        todo = summary.TodoRow(record_id="r1", project="A", link_cell="",
                               record_url="https://x/base/t?table=tb&record=r1",
                               label="x", reasons=["刷新失败"], queued=True)
        self.assertIn("<span class=chip>排队中</span>", self._todo_table(todo))

    def test_an_unqueued_todo_does_not(self):
        todo = summary.TodoRow(record_id="r1", project="A", link_cell="",
                               record_url="https://x/base/t?table=tb&record=r1",
                               label="x", reasons=["刷新失败"])
        self.assertNotIn("排队中", self._todo_table(todo))


class TestTodosAreSplitByProject(unittest.TestCase):
    """待办按项目分 tab。表一多，每个项目那几行告警就被别的项目的几十行
    淹掉，运营找自己那几行要滚很久——所以每个项目一页，tab 上带条数。"""

    def _page(self, snaps):
        from xhsearch import panel_view
        cfg = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": "a-long-enough-password",
            "FEISHU_DOMAIN": "https://x.feishu.cn"})
        return panel_view.overview_page(
            overview=summary.Overview(projects=snaps), error="",
            fetched_at=0.0, config=cfg, csrf="tok")

    def _snap(self, label, table_id, reasons_per_row):
        todos = [summary.TodoRow(record_id=f"{table_id}-{i}", project=label,
                                 link_cell="", label=f"笔记{i}",
                                 record_url=f"https://x/base/t?table={table_id}&record={table_id}-{i}",
                                 app_token="t", table_id=table_id, reasons=list(reasons))
                 for i, reasons in enumerate(reasons_per_row)]
        return summary.ProjectSnapshot(label=label, app_token="t", table_id=table_id,
                                       total_rows=len(todos), todos=todos)

    def _todos_block(self, page):
        start = page.index("<div id=todos>")
        return page[start:page.index("<h2 id=s-proj>", start)]

    def _tabs(self, page):
        return re.findall(
            r"<button type=button role=tab class='tab( on)?' aria-selected=(true|false) "
            r"data-tab='(p\d+)' data-project='([^']*)' aria-controls='todos-\3'>"
            r"\4 <span class='(cnt[^']*)'>(\d+)</span></button>", page)

    def _panels(self, page):
        return re.findall(
            r"<div class=tabpanel id='todos-(p\d+)' role=tabpanel data-tab='\1' "
            r"data-project='([^']*)'( hidden)?>", page)

    def test_one_tab_and_one_panel_per_project_with_todos(self):
        page = self._page([self._snap("甲", "ta", [["有负面"], ["置顶掉了"]]),
                           self._snap("乙", "tb", [["风控中"]])])
        tabs = self._tabs(page)
        self.assertEqual([(t[3], t[5]) for t in tabs], [("甲", "2"), ("乙", "1")])
        panels = self._panels(page)
        self.assertEqual([(p[1], bool(p[2])) for p in panels], [("甲", False), ("乙", True)],
                         "第一页展开、其余 hidden——不靠 JS 也先能看到一页")
        self.assertEqual([(bool(t[0]), t[1]) for t in tabs],
                         [(True, "true"), (False, "false")])

    def test_rows_land_in_their_own_project_page(self):
        page = self._page([self._snap("甲", "ta", [["有负面"]]),
                           self._snap("乙", "tb", [["风控中"], ["刷新失败"]])])
        block = self._todos_block(page)
        pages = re.split(r"<div class=tabpanel ", block)[1:]
        self.assertEqual(len(pages), 2)
        self.assertIn("data-project='甲'", pages[0])
        self.assertEqual(pages[0].count("data-tbl='ta'"), 1)
        self.assertEqual(pages[0].count("data-tbl='tb'"), 0)
        self.assertIn("data-project='乙'", pages[1])
        self.assertEqual(pages[1].count("data-tbl='tb'"), 2)
        self.assertEqual(pages[1].count("data-tbl='ta'"), 0)

    def test_tab_order_follows_the_registry_not_severity(self):
        """位置每次刷新都跳的话运营找不到自己的项目。跨表待办本身是按严重度
        排的（乙的风控排在甲的负面前面），tab 顺序不能跟着它。"""
        page = self._page([self._snap("甲", "ta", [["有负面"]]),
                           self._snap("乙", "tb", [["风控中"]])])
        self.assertEqual([t[3] for t in self._tabs(page)], ["甲", "乙"])

    def test_projects_without_todos_get_no_tab(self):
        """没事的项目在下面项目卡里本来就是绿的，开一个空页只会让人点进去
        看一张空表。"""
        page = self._page([self._snap("甲", "ta", []),
                           self._snap("乙", "tb", [["风控中"]]),
                           self._snap("丙", "tc", [])])
        self.assertEqual([t[3] for t in self._tabs(page)], ["乙"])

    def test_the_count_is_coloured_by_the_worst_reason_on_that_page(self):
        page = self._page([self._snap("红", "ta", [["有负面"], ["刷新失败"]]),
                           self._snap("黄", "tb", [["置顶掉了"], ["有负面"]]),
                           self._snap("素", "tc", [["置顶掉了"]])])
        by_label = {t[3]: t[4] for t in self._tabs(page)}
        self.assertEqual(by_label, {"红": "cnt r", "黄": "cnt a", "素": "cnt"})

    def test_the_project_column_is_gone_from_the_todo_pages(self):
        """整页都是同一个项目，那一栏只会占宽度。（归档区仍是拉平的一张表，
        它保留「项目」列——所以只查待办页里的表。）"""
        page = self._page([self._snap("甲", "ta", [["有负面"]])])
        block = self._todos_block(page)
        self.assertNotIn("<th>项目</th>", block)
        self.assertIn("<th>哪一条</th>", block)
        self.assertIn("class=todoAll title='全选这一页'", block)

    def test_every_page_gets_its_own_select_all(self):
        page = self._page([self._snap("甲", "ta", [["有负面"]]),
                           self._snap("乙", "tb", [["风控中"]])])
        self.assertEqual(self._todos_block(page).count("class=todoAll"), 2)

    def test_project_labels_are_escaped_in_the_tab_attributes(self):
        evil = "甲' onclick='alert(1)"
        page = self._page([self._snap(evil, "ta", [["有负面"]])])
        self.assertNotIn("onclick='alert(1)", page)
        self.assertIn("data-project='甲&#x27; onclick=&#x27;alert(1)'", page)

    def test_the_lede_explains_the_tabs(self):
        page = self._page([self._snap("甲", "ta", [["有负面"]])])
        self.assertIn("每个项目一个标签", page)
        self.assertIn("aria-label='按项目分开看'", page)
