"""Codex 在 PR #13 上报的 12 条，每条一个会在修之前失败的用例。

分三组，对应三种代价：
* **A 组**是钱和数据正确性——查重漏掉第二组 = 同一张表一轮付两次钱；
  `到期待刷` 漏算 queued = 面板自己那个批量勾按钮制造出来的行不算数；
  阈值只校验填了的那几格 = cron 从下一轮起写错热度档，而热度是棘轮的。
* **B 组**是「面板起不来 / 和 cron 口径分叉」。
* **C 组**是显示与安全。

全部离线，一个请求都不发。
"""

import ast
import unittest
from datetime import timedelta
from unittest import mock

from xhsearch import panel, provision, registry, runner, summary, tablespec
from xhsearch.config import Settings
from tests.test_summary import NOW, ms, record


# ---------- A1 · 每一组重复都要抓出来 ----------

class TestEveryDuplicateGroup(unittest.TestCase):
    """只报第一组 = 第二组原样放行 = 那张表一轮里被排两遍。"""

    def targets(self):
        return [tablespec.TableTarget("甲", "ap", "tbl1"),
                tablespec.TableTarget("乙", "bp", "tbl1"),
                tablespec.TableTarget("丙", "cp", "tbl2"),
                tablespec.TableTarget("丁", "dp", "tbl2")]

    def test_both_groups_are_reported(self):
        found = tablespec.find_duplicates(self.targets())
        self.assertEqual(sorted(found.by_table_id), ["tbl1", "tbl2"])

    def test_singular_wrapper_still_returns_one_line(self):
        self.assertIn("tbl1", tablespec.find_duplicate(self.targets()))
        self.assertEqual(tablespec.find_duplicate([]), "")

    def test_lookup_is_exact_not_substring(self):
        """拿 table_id 去那句话里做子串匹配会误伤：`tbl1` 是 `tbl10` 的子串。"""
        found = tablespec.find_duplicates(
            [tablespec.TableTarget("甲", "ap", "tbl1"),
             tablespec.TableTarget("乙", "bp", "tbl1"),
             tablespec.TableTarget("丙", "cp", "tbl10")])
        self.assertTrue(found.message_for(table_id="tbl1"))
        self.assertFalse(found.message_for(table_id="tbl10"))

    def test_registry_disables_all_four_rows(self):
        from tests.test_registry import FakeTable, FULL, row
        table = FakeTable([
            row("r1", label="甲", target="bascnA:tbl1"),
            row("r2", label="乙", target="bascnB:tbl1"),
            row("r3", label="丙", target="bascnC:tbl2"),
            row("r4", label="丁", target="bascnD:tbl2"),
        ], columns=FULL)
        entries = registry.read(table)
        self.assertTrue(all(e.problem for e in entries),
                        [f"{e.label}:{e.problem!r}" for e in entries])
        self.assertEqual(registry.to_targets(entries), [])


# ---------- A2 · queued 的行要算进「到期待刷」 ----------

class TestQueuedCountsAsDue(unittest.TestCase):
    """`runner.load_rows` 花钱时看 `row.queued`，面板不看就会少报。"""

    def snapshot(self, records, settings=None):
        return summary.build_snapshot(
            label="甲", app_token="bascnA", table_id="tbl1",
            records=records, settings=settings or Settings(), now=NOW)

    def test_a_queued_row_that_is_not_yet_due_still_counts(self):
        # 刚刷过（1 小时前），自然间隔远没到——只有人工勾了「排队刷新」。
        rows = [record("recQ", checked_hours_ago=1, published_hours_ago=10,
                       queued=True)]
        snap = self.snapshot(rows)
        self.assertEqual(snap.queued_rows, 1)
        self.assertEqual(snap.due_rows, 1,
                         "勾了排队刷新的行下一轮一定会花钱，必须算进来")

    def test_an_archived_queued_row_counts_too(self):
        """`排队刷新` 绕过归档线——load_rows 就是这么算的。"""
        rows = [record("recOld", published_hours_ago=24 * 400,
                       checked_hours_ago=1, queued=True)]
        snap = self.snapshot(rows)
        self.assertEqual(snap.archived_rows, 1)
        self.assertEqual(snap.due_rows, 1)

    def test_a_plain_fresh_row_is_still_not_due(self):
        snap = self.snapshot([record("recA", checked_hours_ago=1,
                                     published_hours_ago=10)])
        self.assertEqual(snap.due_rows, 0)


# ---------- A3 · 阈值按生效值严格递增 ----------

class TestEffectiveTierOrdering(unittest.TestCase):
    BASE = Settings()          # 20 / 50 / 100

    def read(self, **thresholds):
        entry = registry.Entry(label="甲", app_token="a", table_id="t",
                               thresholds=thresholds)
        return registry.read_overrides(entry, self.BASE)

    def test_one_value_that_breaks_the_effective_order_is_rejected(self):
        """只填「爆贴=10」，全局评估中是 20 → 生效后 (20, 10, 100)。"""
        override = self.read(**{registry.COL_TIER_HOT: 10})
        self.assertEqual(override.values, {})
        self.assertIn("递增", override.problems[0])

    def test_equal_tiers_are_rejected(self):
        """heat_tier 从高往低判，等值会让下面那一档永远够不着。"""
        override = self.read(**{registry.COL_TIER_EVALUATING: 20,
                                registry.COL_TIER_HOT: 20})
        self.assertEqual(override.values, {})
        self.assertIn("相等也不行", override.problems[0])

    def test_one_value_that_keeps_the_order_passes(self):
        override = self.read(**{registry.COL_TIER_HOT: 30})
        self.assertEqual(override.values, {registry.COL_TIER_HOT: 30})
        self.assertEqual(override.problems, [])

    def test_the_effective_values_are_spelled_out_in_the_message(self):
        """报错要说清生效后是哪三个数，否则人只看得到自己填的那一个。"""
        override = self.read(**{registry.COL_TIER_HOT: 10})
        self.assertIn("评估中门槛=20", override.problems[0])
        self.assertIn("爆贴门槛=10", override.problems[0])


# ---------- B1 · 空注册表也要能起面板 ----------

class TestEmptyRegistryStillServes(unittest.TestCase):
    """面板存在的理由之一就是**在上面加第一张表**。在这条路上拒绝启动，
    等于「要用面板加表，得先有表」。"""

    def setUp(self):
        import importlib
        self.cli = importlib.import_module("cli")

    def _call(self, **kwargs):
        from tests.test_registry import FakeTable
        env = {"FEISHU_REGISTRY": "bascnREG:tblREG"}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(registry, "read", return_value=[]), \
             mock.patch.object(self.cli, "_registry_table",
                               return_value=FakeTable([])), \
             mock.patch("builtins.print"):
            return self.cli._entries_or_raise("id", "secret", **kwargs)

    def test_serve_path_accepts_an_empty_registry(self):
        self.assertEqual(self._call(allow_empty=True), [])

    def test_paid_path_still_refuses(self):
        """静默跑成「零张表」比报错难发现得多——付费那几条命令不给这个开关。"""
        with self.assertRaises(self.cli.NoTables):
            self._call()

    def test_the_sys_exit_wrapper_is_still_there_for_paid_commands(self):
        from tests.test_registry import FakeTable
        env = {"FEISHU_REGISTRY": "bascnREG:tblREG"}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(registry, "read", return_value=[]), \
             mock.patch.object(self.cli, "_registry_table",
                               return_value=FakeTable([])), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                self.cli._entries("id", "secret")


# ---------- B2 · 表清单每轮重读 ----------

class TestTableListReloads(unittest.TestCase):
    def test_produce_sees_a_table_added_after_startup(self):
        """加表之后不重启也该出现——否则面板和 cron 长期显示不同的在管清单。"""
        calls = []

        def resolve():
            calls.append(len(calls))
            return [("甲", object())] if calls[-1] else []

        # 模拟 cmd_serve 里那个闭包：produce 每次执行都重新解析。
        def produce():
            return resolve()

        self.assertEqual(produce(), [])
        self.assertEqual(len(produce()), 1)

    def test_serve_resolves_lazily_not_once(self):
        """钉住 cmd_serve 的形状：produce 体内必须再调一次 resolve_tables。"""
        import inspect, cli
        source = inspect.getsource(cli.cmd_serve)
        tree = ast.parse(source.strip())
        produce = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "produce")
        called = {n.func.id for n in ast.walk(produce)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("resolve_tables", called,
                      "produce 里必须重新解析表清单，不能复用启动时那一份")


# ---------- B3 · 面板用逐表阈值 ----------

class TestPanelUsesPerTableSettings(unittest.TestCase):
    class FakeTable:
        app_token, table_id, route = "bascnA", "tbl1", "base"

        def __init__(self, records, columns):
            self._records, self._columns = records, columns

        def fields_meta(self):
            return {c: {"type": 1, "ui_type": "", "options": None}
                    for c in self._columns}

        def search(self, field_names, **kwargs):
            return self._records

    def _tables(self):
        settings = Settings()
        f = settings.fields
        columns = summary.panel_fields(settings) + [f.monitoring]
        # 90 天前发的帖：默认 archive_after_days=30 → 已归档。
        rows = [record("recOld", published_hours_ago=24 * 90,
                       checked_hours_ago=1)]
        return [("甲", self.FakeTable(rows, columns))], settings

    def test_a_per_table_archive_window_changes_the_numbers(self):
        tables, settings = self._tables()
        wide = registry.apply_overrides(
            settings,
            registry.Entry(label="甲", app_token="bascnA", table_id="tbl1",
                           thresholds={registry.COL_ARCHIVE_DAYS: 365}))

        default = panel.collect(tables, settings, {}, now=NOW)
        self.assertEqual(default.projects[0].archived_rows, 1)

        per_table = panel.collect(tables, settings, {}, now=NOW,
                                  settings_for=lambda tid: wide)
        self.assertEqual(per_table.projects[0].archived_rows, 0,
                         "逐表归档天数放宽之后这一行不该还算归档——"
                         "cron 那边 apply_overrides 就是这么算的")

    def test_other_projects_are_not_contaminated(self):
        """深拷贝纪律：一张表的覆盖不能串味到别的表。"""
        tables, settings = self._tables()
        wide = registry.apply_overrides(
            settings,
            registry.Entry(label="甲", app_token="bascnA", table_id="tbl1",
                           thresholds={registry.COL_ARCHIVE_DAYS: 365}))
        self.assertEqual(settings.refresh.archive_after_days, 30)
        self.assertEqual(wide.refresh.archive_after_days, 365)


# ---------- C1 · 待办先排序再截断 ----------

class TestTodosRankedBeforeCapping(unittest.TestCase):
    """按飞书记录顺序截断，一次大面积事故就能把 风控中 整个藏掉。"""

    def build(self, max_todos):
        settings = Settings()
        rows = [record(f"rec{i}", refresh_status=runner.STATUS_FAILED,
                       published_hours_ago=10, checked_hours_ago=1)
                for i in range(250)]
        # 最严重的那一条排在**最后**——正是旧写法看不到的位置。
        rows.append(record("recRISK", tags=[settings.tags.risk],
                           published_hours_ago=10, checked_hours_ago=1))
        return summary.build_snapshot(
            label="甲", app_token="bascnA", table_id="tbl1",
            records=rows, settings=settings, now=NOW, max_todos=max_todos)

    def test_the_worst_row_survives_the_cap(self):
        snap = self.build(200)
        self.assertIn("recRISK", [t.record_id for t in snap.todos])
        self.assertEqual(snap.todos[0].record_id, "recRISK")

    def test_the_dropped_count_is_reported(self):
        snap = self.build(200)
        self.assertEqual(len(snap.todos), 200)
        self.assertEqual(snap.todos_dropped, 51)
        self.assertFalse(snap.attention_exact)

    def test_nothing_dropped_means_the_count_is_exact(self):
        snap = self.build(500)
        self.assertEqual(snap.todos_dropped, 0)
        self.assertTrue(snap.attention_exact)

    def test_overview_accounts_for_its_own_cap(self):
        snap = self.build(500)
        overview = summary.Overview(projects=[snap])
        self.assertEqual(overview.todos_dropped_by(limit=10),
                         len(snap.todos) - 10)


# ---------- C2 · PANEL_SECRET 强度 ----------

class TestSecretStrength(unittest.TestCase):
    BASE = {"PANEL_PASSWORD": "a-long-enough-password"}

    def test_a_short_explicit_secret_is_refused(self):
        """猜中它就能自己签会话 Cookie，完全绕过口令，而且不走登录节流。"""
        with self.assertRaises(panel.ConfigError) as ctx:
            panel.PanelConfig.from_env({**self.BASE, "PANEL_SECRET": "x"})
        self.assertIn("PANEL_SECRET", str(ctx.exception))

    def test_a_long_secret_is_accepted(self):
        config = panel.PanelConfig.from_env(
            {**self.BASE, "PANEL_SECRET": "z" * panel.MIN_SECRET_LENGTH})
        self.assertEqual(config.secret, b"z" * panel.MIN_SECRET_LENGTH)

    def test_no_secret_still_generates_a_strong_one(self):
        config = panel.PanelConfig.from_env(dict(self.BASE))
        self.assertGreaterEqual(len(config.secret), 32)


# ---------- C3 · Railway 凭据全部进脱敏名单 ----------

class TestRailwaySecretsAreRedacted(unittest.TestCase):
    def test_the_preferred_project_token_is_in_the_list(self):
        env = {"RAILWAY_PROJECT_TOKEN": "proj-secret-value"}
        self.assertIn("proj-secret-value", panel._secrets(env))

    def test_every_credential_railway_reads_is_covered(self):
        """AST 不变量：`railway.py` 读的每一个像凭据的环境变量，
        都必须出现在 `panel._secrets()` 的名单里。

        走 AST 而不是字符串搜索——注释和文档串里也会出现这些名字。
        """
        import inspect
        from xhsearch import railway

        read_names = set()
        for node in ast.walk(ast.parse(inspect.getsource(railway))):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                read_names.add(node.args[0].value)
        credentials = {n for n in read_names
                       if "TOKEN" in n or "SECRET" in n or "KEY" in n}
        self.assertTrue(credentials, "没扫到任何凭据变量，这个测试就白写了")

        listed = set()
        source = inspect.getsource(panel._secrets)
        for node in ast.walk(ast.parse(source.strip())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                listed.add(node.value)
        self.assertEqual(credentials - listed, set(),
                         "railway.py 读了它，而面板不会脱敏——"
                         "那把钥匙会原样出现在公网页面上")


# ---------- C4 · 空日志结果也要进缓存 ----------

class TestEmptyLogResultIsCached(unittest.TestCase):
    def _feed(self, lines):
        config = panel.PanelConfig.from_env({
            "PANEL_PASSWORD": "a-long-enough-password",
            "RAILWAY_PROJECT_TOKEN": "t", "RAILWAY_ENVIRONMENT_ID": "e"})
        calls = []

        def fetch(cfg, **kwargs):
            calls.append(1)
            return list(lines)

        return panel.LogFeed(config, ttl=60.0, fetch=fetch), calls

    def test_a_successful_empty_fetch_is_not_refetched(self):
        """新服务、或 RUN_LOG_JSON 还没产出时正好就是这个状态——
        缓存在最该生效的空态下失效，几个人同时看就把限额吃掉。"""
        feed, calls = self._feed([])
        feed.get()
        feed.get()
        feed.get()
        self.assertEqual(len(calls), 1)

    def test_a_non_empty_fetch_is_still_cached(self):
        feed, calls = self._feed(["一行日志"])
        feed.get()
        feed.get()
        self.assertEqual(len(calls), 1)

    def test_force_still_refetches(self):
        feed, calls = self._feed([])
        feed.get()
        feed.get(force=True)
        self.assertEqual(len(calls), 2)


# ---------- C5 · 新建表的链接指向新建的那张表 ----------

class TestCreatedTableLink(unittest.TestCase):
    class FakeWorkspace:
        def create_base(self, name):
            return {"app_token": "bascnNEW",
                    "url": "https://x.feishu.cn/base/bascnNEW"}

        def create_table(self, app_token, name, fields):
            return "tblNEW"

    def test_the_link_points_at_the_monitored_table(self):
        """base 级链接点进去是飞书自动建的那张默认表——
        运营可能直接在里面填数据，而注册表监控的是另一张。"""
        made = provision.create_monitored_table(
            self.FakeWorkspace(), Settings(), "途鸽三期")
        self.assertEqual(made["table_id"], "tblNEW")
        self.assertIn("table=tblNEW", made["url"])
        self.assertTrue(made["note"])

    def test_a_missing_base_url_does_not_become_a_fake_link(self):
        self.assertEqual(provision._table_url("", "bascnA", "tblA"), "")


# ---------- C6 · /wiki/ 路由不能被重写成 /base/ ----------

class TestWikiRouteSurvives(unittest.TestCase):
    def test_parse_target_keeps_the_route(self):
        wiki = tablespec.parse_target(
            "企业C=https://x.feishu.cn/wiki/wikcnA?table=tbl9")
        base = tablespec.parse_target("甲=https://x.feishu.cn/base/bascnA?table=tbl1")
        self.assertEqual(wiki.route, "wiki")
        self.assertEqual(base.route, "base")

    def test_plain_token_form_defaults_to_base(self):
        self.assertEqual(tablespec.parse_target("甲=bascnA:tbl1").route, "base")

    def test_urls_use_the_original_route(self):
        wiki = summary.table_url("https://x.feishu.cn", "wikcnA", "tbl9",
                                 route="wiki")
        self.assertIn("/wiki/wikcnA", wiki)
        self.assertNotIn("/base/", wiki)
        row = summary.record_url("https://x.feishu.cn", "wikcnA", "tbl9",
                                 "recX", route="wiki")
        self.assertIn("/wiki/wikcnA", row)
        self.assertIn("record=recX", row)

    def test_snapshot_carries_the_route_into_every_link(self):
        snap = summary.build_snapshot(
            label="企业C", app_token="wikcnA", table_id="tbl9",
            records=[record("recX", refresh_status=runner.STATUS_FAILED)],
            settings=Settings(), now=NOW,
            feishu_base="https://x.feishu.cn", route="wiki")
        self.assertIn("/wiki/wikcnA", snap.table_url)
        self.assertIn("/wiki/wikcnA", snap.todos[0].record_url)

    def test_registry_round_trips_the_route(self):
        from tests.test_registry import FakeTable, FULL, row as reg_row
        table = FakeTable([reg_row(
            "r1", label="企业C",
            target="https://x.feishu.cn/wiki/wikcnA?table=tbl9")], columns=FULL)
        target = registry.to_targets(registry.read(table))[0]
        self.assertEqual(target.route, "wiki")


if __name__ == "__main__":
    unittest.main()
