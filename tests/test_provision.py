"""接管一张表：体检 → 建缺的列。全部离线，不发任何请求。

三条纪律的用例：体检不花钱、建列只追加、补选项默认不做。
"""

import unittest
from unittest import mock

from xhsearch import feishu, provision, schema
from xhsearch.config import Settings


def full_meta(settings=None, drop=(), retype=None, drop_options=None):
    settings = settings or Settings()
    meta = {}
    for name, allowed, _l, options, _n in schema.expected_schema(settings):
        if name in drop:
            continue
        meta[name] = {"type": allowed[0], "ui_type": "",
                      "options": list(options) if options else None}
    for name, code in (retype or {}).items():
        meta[name]["type"] = code
    for name, keep in (drop_options or {}).items():
        meta[name]["options"] = list(keep)
    return meta


class FakeTable:
    def __init__(self, meta, rows=1, app_token="bascnA", table_id="tblB",
                 create_error=None, option_error=None):
        self.app_token, self.table_id = app_token, table_id
        self._meta = meta
        self._rows = rows
        self._create_error = create_error
        self._option_error = option_error
        self.created = []
        self.option_calls = []

    def fields_meta(self):
        return self._meta

    def search(self, fields, **kwargs):
        return [{"record_id": f"r{i}", "fields": {}} for i in range(self._rows)]

    def create_field(self, body):
        if self._create_error:
            raise feishu.FeishuError(-1, self._create_error)
        self.created.append(body)
        return "fld1"

    def add_field_options(self, column, wanted):
        if self._option_error:
            raise feishu.FeishuError(-1, self._option_error)
        self.option_calls.append((column, list(wanted)))
        return list(wanted)


class TestCheckIsFree(unittest.TestCase):
    def test_a_healthy_table_is_ready(self):
        c = provision.check(FakeTable(full_meta()), Settings())
        self.assertTrue(c.ready)
        self.assertEqual(c.buildable, [])
        self.assertEqual(c.manual, [])

    def test_it_never_touches_the_paid_path(self):
        """体检只读元数据和一行记录。判定链路一个请求都不发。"""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(provision.check))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute):
                    called.add(target.attr)
        self.assertEqual(called & {"refresh", "plan_calls", "estimate_yuan",
                                   "fetch", "_call_once"}, set())

    def test_unreadable_metadata_gives_the_collaborator_instructions(self):
        """这是接管已有表时唯一必须人做的一步，提示要能直接照着操作。"""
        c = provision.check(FakeTable(None), Settings())
        self.assertFalse(c.reachable)
        self.assertIn("添加文档应用", c.error)
        self.assertIn("高级权限", c.error)

    def test_empty_metadata_is_treated_the_same(self):
        self.assertIn("添加文档应用", provision.check(FakeTable({}), Settings()).error)

    def test_missing_columns_become_buildable(self):
        settings = Settings()
        f = settings.fields
        c = provision.check(
            FakeTable(full_meta(settings, drop=[f.negative_status, f.negative_digest])),
            settings)
        self.assertFalse(c.ready)
        self.assertEqual(sorted(col.name for col in c.buildable),
                         sorted([f.negative_status, f.negative_digest]))

    def test_wrong_types_and_missing_options_go_to_the_manual_list(self):
        """这两类机器不做——改类型会丢数据，补选项会清空整列。"""
        settings = Settings()
        f = settings.fields
        c = provision.check(FakeTable(full_meta(
            settings, retype={f.last_updated: 1002},
            drop_options={f.pinned_status: []})), settings)
        self.assertEqual(c.buildable, [])
        self.assertEqual(len(c.manual), 2)
        self.assertTrue(any("最近检查时间" in m for m in c.manual))
        self.assertTrue(any("置顶状态" in m for m in c.manual))

    def test_a_table_already_watched_is_flagged_as_duplicate(self):
        c = provision.check(FakeTable(full_meta()), Settings(),
                            known_tables=[("OKMAN一期", "bascnZ", "tblB")])
        self.assertIn("已经在监控里了", c.duplicate)
        self.assertFalse(c.ready)

    def test_duplicate_is_keyed_on_table_id_across_link_forms(self):
        """/base/ 和 /wiki/ 两种链接 app_token 不同、指的是同一张表。"""
        c = provision.check(FakeTable(full_meta(), app_token="wikcnDIFFERENT"),
                            Settings(),
                            known_tables=[("旧的", "bascnA", "tblB")])
        self.assertTrue(c.duplicate)

    def test_a_different_table_is_not_a_duplicate(self):
        c = provision.check(FakeTable(full_meta()), Settings(),
                            known_tables=[("别的", "bascnA", "tblOTHER")])
        self.assertEqual(c.duplicate, "")
        self.assertTrue(c.ready)


class TestBuildMissing(unittest.TestCase):
    def _diff(self, **kwargs):
        return schema.diff(Settings(), full_meta(Settings(), **kwargs))

    def test_creates_the_missing_columns(self):
        settings = Settings()
        f = settings.fields
        table = FakeTable(full_meta(settings, drop=[f.negative_status]))
        result = provision.build_missing(
            table, self._diff(drop=[f.negative_status]), log=lambda *a: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.created, [f.negative_status])
        self.assertEqual(table.created[0]["field_name"], f.negative_status)
        self.assertIn("options", table.created[0]["property"],
                      "新建的选择列要连选项一起建")

    def test_option_gaps_are_skipped_by_default(self):
        """补选项对已有列是整体覆盖，未经真机验证之前只给清单。"""
        settings = Settings()
        diff = self._diff(drop_options={settings.fields.traffic_status: ["爆贴"]})
        table = FakeTable(full_meta())
        result = provision.build_missing(table, diff, log=lambda *a: None)
        self.assertEqual(table.option_calls, [], "默认一个补选项请求都不该发")
        self.assertEqual(len(result.skipped_options), 1)
        self.assertIn("缺选项", result.skipped_options[0])

    def test_option_gaps_are_patched_once_explicitly_allowed(self):
        settings = Settings()
        diff = self._diff(drop_options={settings.fields.traffic_status: ["爆贴"]})
        table = FakeTable(full_meta())
        result = provision.build_missing(table, diff, allow_option_patch=True,
                                         log=lambda *a: None)
        self.assertEqual(table.option_calls[0][0], settings.fields.traffic_status)
        self.assertIn(settings.fields.traffic_status, result.options_added)
        self.assertEqual(result.skipped_options, [])

    def test_wrong_types_are_never_touched(self):
        """改类型会转换/丢已有数据，不该由一个网页按钮触发。"""
        settings = Settings()
        diff = self._diff(retype={settings.fields.traffic_status: 3})
        table = FakeTable(full_meta())
        provision.build_missing(table, diff, allow_option_patch=True,
                                log=lambda *a: None)
        self.assertEqual(table.created, [])
        self.assertEqual(table.option_calls, [])

    def test_one_failure_does_not_stop_the_rest(self):
        settings = Settings()
        f = settings.fields
        diff = self._diff(drop=[f.negative_status, f.negative_digest])
        table = FakeTable(full_meta(), create_error="1254302 高级权限")
        result = provision.build_missing(table, diff, log=lambda *a: None)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.failures), 2)
        self.assertIn("高级权限", result.failures[0])

    def test_every_structural_write_is_logged(self):
        """面板能改别人表结构，这是唯一的审计线索。"""
        settings = Settings()
        lines = []
        diff = self._diff(drop=[settings.fields.negative_status])
        provision.build_missing(FakeTable(full_meta()), diff, log=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("bascnA"[-6:], lines[0])
        self.assertIn("tblB", lines[0])
        self.assertIn(settings.fields.negative_status, lines[0])


class TestCreateMonitoredTable(unittest.TestCase):
    def test_builds_every_expected_column_in_one_call(self):
        settings = Settings()
        workspace = mock.Mock()
        workspace.create_base.return_value = {
            "app_token": "bascnNEW", "url": "https://x", "default_table_id": "t0"}
        workspace.create_table.return_value = "tblNEW"
        got = provision.create_monitored_table(workspace, settings, "途鸽三期")
        self.assertEqual(got["target"], "bascnNEW:tblNEW")
        fields = workspace.create_table.call_args[0][2]
        self.assertEqual(len(fields), len(schema.expected_schema(settings)))
        traffic = next(f for f in fields
                       if f["field_name"] == settings.fields.traffic_status)
        self.assertEqual([o["name"] for o in traffic["property"]["options"]],
                         settings.tags.machine_written())

    def test_a_failed_base_creation_raises(self):
        workspace = mock.Mock()
        workspace.create_base.return_value = {"app_token": "", "url": "",
                                              "default_table_id": ""}
        with self.assertRaises(feishu.FeishuError):
            provision.create_monitored_table(workspace, Settings(), "甲")


if __name__ == "__main__":
    unittest.main()
