"""表清单存取。全部离线，不发任何请求。

这一层最要紧的三件事：
* **一行填错只影响它自己**（和环境变量那条路相反——那边填错就该整个起不来）
* **读不到时绝不静默降级成零张表**：那种失败退出码是 0、日志一切正常，
  而实际一行都没刷，最难发现
* **认不出是注册表就绝不写**：FEISHU_REGISTRY 填成业务表的话，
  机器会往运营的生产表里凭空造出一批行
"""

import unittest
from unittest import mock

from xhsearch import registry


def meta_for(columns):
    return {name: {"type": 1, "ui_type": "", "options": None} for name in columns}


FULL = (registry.COL_LABEL, registry.COL_TARGET, registry.COL_ENABLED,
        registry.COL_NOTE, registry.COL_STATUS, registry.COL_FIRST_RUN)


_UNSET = object()


class FakeTable:
    app_token, table_id = "bascnREG", "tblREG"

    def __init__(self, records, columns=FULL, meta=_UNSET):
        self._records = records
        # meta=None 是个有意义的取值（读不到元数据），不能和「没传」混为一谈。
        self._meta = meta_for(columns) if meta is _UNSET else meta
        self.created = []
        self.updated = []
        self.deleted = []

    def fields_meta(self):
        return self._meta

    def search(self, field_names, **kwargs):
        return self._records

    def batch_create(self, records, *, client_token):
        self.created.append((records, client_token))
        return ["recNEW"]

    def batch_update(self, updates, **kwargs):
        self.updated.extend(updates)
        return len(updates)

    def delete_record(self, record_id):
        self.deleted.append(record_id)


def row(record_id="rec1", label="OKMAN一期", target="bascnA:tbl1",
        enabled=True, **extra):
    fields = {registry.COL_LABEL: label, registry.COL_TARGET: target,
              registry.COL_ENABLED: enabled}
    fields.update(extra)
    return {"record_id": record_id, "fields": fields}


class TestRecogniseRegistry(unittest.TestCase):
    def test_a_real_registry_is_recognised(self):
        self.assertTrue(registry.looks_like_registry(meta_for(FULL)))

    def test_a_business_table_is_not(self):
        """业务表的列名和注册表刻意零重叠，就是为了这一刻。"""
        business = meta_for(("反馈链接", "发布时间", "流量状态", "是否巡查"))
        self.assertFalse(registry.looks_like_registry(business))

    def test_missing_one_signature_column_is_not_enough(self):
        self.assertFalse(registry.looks_like_registry(
            meta_for((registry.COL_LABEL, registry.COL_TARGET))))

    def test_none_and_empty(self):
        self.assertFalse(registry.looks_like_registry(None))
        self.assertFalse(registry.looks_like_registry({}))


class TestRead(unittest.TestCase):
    def test_parses_rows_into_targets(self):
        entries = registry.read(FakeTable([row()]))
        self.assertEqual(entries[0].as_tuple(), ("OKMAN一期", "bascnA", "tbl1"))
        self.assertTrue(entries[0].usable)

    def test_full_urls_work_too(self):
        entries = registry.read(FakeTable([row(
            target="https://x.feishu.cn/base/bascnZ?table=tbl9")]))
        self.assertEqual(entries[0].as_tuple()[1:], ("bascnZ", "tbl9"))

    def test_a_broken_row_does_not_take_down_the_others(self):
        """注册表是运营填的，一行填错不该让其余项目停摆。"""
        entries = registry.read(FakeTable([
            row("rec1", "好的", "bascnA:tbl1"),
            row("rec2", "坏的", "看不懂的东西"),
            row("rec3", "也好的", "bascnB:tbl2"),
        ]))
        self.assertEqual([e.usable for e in entries], [True, False, True])
        self.assertIn("看不懂", entries[1].problem)
        self.assertEqual(len(registry.to_tuples(entries)), 2)

    def test_a_dangerous_token_is_a_row_problem_not_a_crash(self):
        entries = registry.read(FakeTable([row(target="../../auth:tbl1")]))
        self.assertFalse(entries[0].usable)
        self.assertIn("字母和数字", entries[0].problem)

    def test_empty_target_is_flagged(self):
        entries = registry.read(FakeTable([row(target="")]))
        self.assertIn("是空的", entries[0].problem)

    def test_disabled_rows_are_excluded_but_kept_visible(self):
        entries = registry.read(FakeTable([
            row("rec1", "在用", "bascnA:tbl1"),
            row("rec2", "停了", "bascnB:tbl2", enabled=False),
        ]))
        self.assertEqual(len(entries), 2)
        self.assertEqual(registry.to_tuples(entries), [("在用", "bascnA", "tbl1")])

    def test_a_missing_enabled_cell_counts_as_enabled(self):
        """新加的一行忘了勾就静默不刷，是个很难发现的坑。"""
        record = {"record_id": "rec1", "fields": {
            registry.COL_LABEL: "甲", registry.COL_TARGET: "bascnA:tbl1"}}
        self.assertTrue(registry.read(FakeTable([record]))[0].usable)

    def test_label_falls_back_to_the_parsed_one(self):
        entries = registry.read(FakeTable([row(label="", target="bascnA:tbl7")]))
        self.assertEqual(entries[0].label, "tbl7")

    def test_duplicate_live_tables_are_flagged_on_both(self):
        """同一张表登记两遍 = 一轮内付两次钱。"""
        entries = registry.read(FakeTable([
            row("rec1", "甲", "https://x.feishu.cn/base/bascnA?table=tblSAME"),
            row("rec2", "乙", "https://x.feishu.cn/wiki/wikcnB?table=tblSAME"),
        ]))
        self.assertTrue(all(e.problem for e in entries))
        self.assertEqual(registry.to_tuples(entries), [])

    def test_a_disabled_duplicate_does_not_poison_the_live_one(self):
        """停用的那些留着当历史，不该因为和在用的撞了就把在用的也拖下水。"""
        entries = registry.read(FakeTable([
            row("rec1", "在用", "bascnA:tblSAME"),
            row("rec2", "旧的", "bascnA:tblSAME", enabled=False),
        ]))
        self.assertEqual(registry.to_tuples(entries), [("在用", "bascnA", "tblSAME")])

    def test_unreadable_metadata_raises_rather_than_returning_empty(self):
        with self.assertRaises(registry.RegistryError) as ctx:
            registry.read(FakeTable([], meta=None))
        self.assertIn("添加文档应用", str(ctx.exception))

    def test_pointing_at_a_business_table_raises(self):
        business = meta_for(("反馈链接", "流量状态", "是否巡查"))
        with self.assertRaises(registry.RegistryError) as ctx:
            registry.read(FakeTable([], meta=business))
        self.assertIn("不像注册表", str(ctx.exception))


class TestWrites(unittest.TestCase):
    def test_add_defaults_to_disabled(self):
        """新加的表体检多半还没过。默认启用的话，一张缺「最近检查时间」列的表
        会在下一轮 sweep 全表判到期。"""
        table = FakeTable([])
        registry.add(table, label="途鸽三期", target="bascnA:tbl1",
                     client_token="uuid-1")
        fields = table.created[0][0][0]["fields"]
        self.assertIs(fields[registry.COL_ENABLED], False)

    def test_add_passes_the_idempotency_key(self):
        table = FakeTable([])
        registry.add(table, label="甲", target="bascnA:tbl1", client_token="uuid-7")
        self.assertEqual(table.created[0][1], "uuid-7")

    def test_add_only_writes_columns_that_exist(self):
        table = FakeTable([], columns=(registry.COL_LABEL, registry.COL_TARGET,
                                       registry.COL_ENABLED))
        registry.add(table, label="甲", target="bascnA:tbl1", note="备注没了",
                     client_token="u")
        self.assertNotIn(registry.COL_NOTE, table.created[0][0][0]["fields"])

    def test_add_refuses_a_table_that_is_not_a_registry(self):
        table = FakeTable([], meta=meta_for(("反馈链接", "流量状态")))
        with self.assertRaises(registry.RegistryError):
            registry.add(table, label="甲", target="bascnA:tbl1", client_token="u")
        self.assertEqual(table.created, [], "认不出来就一个字都不写")

    def test_set_enabled_touches_only_that_column(self):
        table = FakeTable([])
        registry.set_enabled(table, "rec1", False)
        self.assertEqual(table.updated,
                         [{"record_id": "rec1",
                           "fields": {registry.COL_ENABLED: False}}])

    def test_remove_deletes_the_registry_row_only(self):
        """停止监控和删除数据是两回事。"""
        table = FakeTable([])
        registry.remove(table, "rec1")
        self.assertEqual(table.deleted, ["rec1"])


class TestCliFallback(unittest.TestCase):
    """读不到注册表时的行为。**绝不静默降级成零张表。**"""

    def setUp(self):
        import cli
        self.cli = cli

    def _entries(self, env, read_result):
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(registry, "read", side_effect=read_result), \
             mock.patch.object(self.cli, "_registry_table",
                               return_value=FakeTable([])):
            return self.cli._entries("cli_x", "s")

    def test_falls_back_to_env_and_says_the_list_may_be_stale(self):
        env = {"FEISHU_REGISTRY": "bascnREG:tblREG",
               "FEISHU_TABLES": "甲=bascnA:tbl1"}
        with mock.patch("builtins.print") as printed:
            got = self._entries(env, registry.RegistryError("网络挂了"))
        self.assertEqual([t.as_tuple() for t in got], [("甲", "bascnA", "tbl1")])
        said = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("可能是旧的", said)

    def test_no_fallback_means_refuse_to_run(self):
        env = {"FEISHU_REGISTRY": "bascnREG:tblREG"}
        with self.assertRaises(SystemExit) as ctx:
            self._entries(env, registry.RegistryError("网络挂了"))
        self.assertIn("零张表", str(ctx.exception))

    def test_an_all_broken_registry_refuses_rather_than_running_empty(self):
        env = {"FEISHU_REGISTRY": "bascnREG:tblREG"}
        broken = [registry.Entry(record_id="r1", label="甲", problem="看不懂")]
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(registry, "read", return_value=broken), \
             mock.patch.object(self.cli, "_registry_table",
                               return_value=FakeTable([])), \
             mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                self.cli._entries("cli_x", "s")

    def test_without_the_variable_it_is_the_old_env_path(self):
        with mock.patch.dict("os.environ",
                             {"FEISHU_TABLES": "甲=bascnA:tbl1"}, clear=True):
            self.assertEqual(
                [t.as_tuple() for t in self.cli._entries("cli_x", "s")],
                [("甲", "bascnA", "tbl1")])


if __name__ == "__main__":
    unittest.main()
