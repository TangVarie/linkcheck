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


def fake_workspace():
    workspace = mock.Mock()
    workspace.create_base.return_value = {
        "app_token": "bascnNEW", "url": "https://x", "default_table_id": "t0"}
    workspace.create_table.return_value = "tblNEW"
    workspace.create_field.return_value = "fldNEW"
    return workspace


class TestCreateMonitoredTable(unittest.TestCase):
    def test_the_monitor_template_builds_every_expected_column_in_one_call(self):
        settings = Settings()
        workspace = fake_workspace()
        got = provision.create_monitored_table(workspace, settings, "途鸽三期",
                                               template="monitor")
        self.assertEqual(got["target"], "bascnNEW:tblNEW")
        fields = workspace.create_table.call_args[0][2]
        self.assertEqual(len(fields), len(schema.expected_schema(settings)))
        self.assertEqual(got["columns"], len(fields))
        traffic = next(f for f in fields
                       if f["field_name"] == settings.fields.traffic_status)
        self.assertEqual([o["name"] for o in traffic["property"]["options"]],
                         settings.tags.machine_written())
        workspace.create_field.assert_not_called()
        self.assertEqual(got["skipped_columns"], [])

    def test_a_failed_base_creation_raises(self):
        workspace = mock.Mock()
        workspace.create_base.return_value = {"app_token": "", "url": "",
                                              "default_table_id": ""}
        with self.assertRaises(feishu.FeishuError):
            provision.create_monitored_table(workspace, Settings(), "甲")

    def test_an_unknown_template_is_refused_before_anything_is_built(self):
        workspace = fake_workspace()
        with self.assertRaises(ValueError):
            provision.create_monitored_table(workspace, Settings(), "甲",
                                             template="whatever")
        workspace.create_base.assert_not_called()


# 西屋表（2026-09 导出）的列，按表里的顺序。模板照它抄，这里照它验。
XIWU_COLUMNS = [
    "素人编号", "发布时间", "反馈链接", "已删除评论留底", "流量状态", "起量时间",
    "笔记状态", "方向", "找人备注", "内容配图", "文案", "随贴评论", "评论的素人编号",
    "评论配图", "评论状态", "发布截图", "相关截图", "蓝词字段", "蓝词图片", "作者",
    "父记录", "负面词", "评论关键词", "是否巡查", "排队刷新", "诊断信息", "置顶状态",
    "已确认存活", "巡查状态", "负面状态", "负面评论快照", "评论区快照",
    "实时数据.评论数", "最近检查时间", "下次检查时间", "连续失败次数", "平台",
    "上次评论数",
]


class TestFullTemplate(unittest.TestCase):
    """「直接新建一张」默认按西屋表的结构连业务列一起建——只建巡查列的话
    建完还得回飞书手工补十几列，等于没省事。"""

    def setUp(self):
        self.settings = Settings()
        self.workspace = fake_workspace()
        self.made = provision.create_monitored_table(
            self.workspace, self.settings, "西屋二期", log=lambda *a: None)
        self.fields = self.workspace.create_table.call_args[0][2]

    def test_full_is_the_default(self):
        self.assertEqual(self.made["template"], "full")

    def test_columns_follow_the_xiwu_table_in_order(self):
        """顺序也要一样：运营看惯了那个顺序，机器列全堆到末尾反而要重新找。
        「父记录」和「下次检查时间」建表时带不了（一个要 table_id，一个引用
        别的列），建完再补，落在末尾。"""
        expected = [c for c in XIWU_COLUMNS if c not in ("父记录", "下次检查时间")]
        self.assertEqual([f["field_name"] for f in self.fields], expected)

    def test_every_xiwu_column_ends_up_built(self):
        self.assertEqual(sorted(self.made["built"]), sorted(XIWU_COLUMNS))
        self.assertEqual(self.made["columns"], len(XIWU_COLUMNS))
        self.assertEqual(self.made["skipped_columns"], [])

    def test_every_monitor_column_is_in_the_full_template(self):
        names = {f["field_name"] for f in self.fields}
        for name, *_ in schema.expected_schema(self.settings):
            self.assertIn(name, names, f"巡查列「{name}」没进标准模板")

    def test_business_columns_get_the_right_types(self):
        by_name = {f["field_name"]: f for f in self.fields}
        self.assertEqual(by_name["素人编号"]["type"], 1)
        self.assertEqual(by_name["文案"]["type"], 1)
        for attachment in ("已删除评论留底", "内容配图", "评论配图", "发布截图",
                           "相关截图", "蓝词图片"):
            self.assertEqual(by_name[attachment]["type"], 17, attachment)
        self.assertEqual(by_name["方向"]["type"], 3)
        self.assertEqual([o["name"] for o in by_name["方向"]["property"]["options"]],
                         ["流量贴", "产品贴"])
        self.assertEqual(by_name["笔记状态"]["type"], 4)
        self.assertIn("已发布", [o["name"] for o in by_name["笔记状态"]["property"]["options"]])
        # 每个项目的词都不一样：建成空的选择列，填的时候飞书自动加选项
        self.assertEqual(by_name["蓝词字段"]["type"], 4)
        self.assertNotIn("property", by_name["蓝词字段"])
        self.assertEqual(by_name["作者"]["type"], 3)
        self.assertNotIn("property", by_name["作者"])

    def test_monitor_columns_keep_their_schema_types_and_options(self):
        """巡查列的类型和选项只有 expected_schema 一份真相，模板不能另写一套。"""
        by_name = {f["field_name"]: f for f in self.fields}
        for name, allowed, _label, options, _note in schema.expected_schema(self.settings):
            self.assertEqual(by_name[name]["type"], allowed[0], name)
            if options:
                got = [o["name"] for o in by_name[name]["property"]["options"]]
                self.assertEqual(got[:len(options)], list(options), name)

    def test_traffic_status_also_gets_the_two_human_options(self):
        """人机共用的列，业务表里还有两个人工选项。新表带上是纯追加。"""
        f = self.settings.fields
        traffic = next(x for x in self.fields if x["field_name"] == f.traffic_status)
        names = [o["name"] for o in traffic["property"]["options"]]
        self.assertIn("观察中", names)
        self.assertIn("爆帖预备", names)
        self.assertEqual(names[:len(self.settings.tags.machine_written())],
                         self.settings.tags.machine_written())

    def _deferred(self):
        calls = self.workspace.create_field.call_args_list
        return {c[0][2]["field_name"]: c for c in calls}

    def test_the_parent_record_link_is_added_after_the_table_exists(self):
        """单向关联到本表：property 要填 table_id，只能建完再补。"""
        call = self._deferred()["父记录"]
        app_token, table_id, body = call[0]
        self.assertEqual((app_token, table_id), ("bascnNEW", "tblNEW"))
        self.assertEqual(body["type"], 18)
        self.assertEqual(body["property"], {"table_id": "tblNEW", "multiple": False})
        self.assertIn("父记录", self.made["built"])

    def test_the_next_check_formula_is_the_operators_own_formula(self):
        """「下次检查时间」是公式列，公式是运营给的原文——按列名引用别的列，
        那些列得先存在，所以也是建完再补。列名从配置取，不写死。"""
        call = self._deferred()["下次检查时间"]
        body = call[0][2]
        self.assertEqual(body["type"], 20)
        self.assertEqual(
            body["property"]["formula_expression"],
            'IF(DATEDIF([发布时间], NOW(), "D") > 30, "", '
            '[最近检查时间] + IF(DATEDIF([发布时间], NOW(), "D") <= 2, 8, '
            'IF(DATEDIF([发布时间], NOW(), "D") <= 7, 24, 72)) / 24)')
        self.assertIn("下次检查时间", self.made["built"])

    def test_the_archive_cutoff_in_the_formula_follows_the_setting(self):
        """归档后 sweep 不再刷这一行，公式再给一个「下次检查」就是在报一个
        不会发生的时间。界线用的是全局的归档天数。"""
        settings = Settings()
        settings.refresh.archive_after_days = 45
        self.assertTrue(provision.next_check_formula(settings)
                        .startswith('IF(DATEDIF([发布时间], NOW(), "D") > 45, "", '))

    def test_deferred_columns_are_added_in_layout_order(self):
        names = [c[0][2]["field_name"] for c in self.workspace.create_field.call_args_list]
        self.assertEqual(names, ["父记录", "下次检查时间"])

    def test_the_formula_follows_renamed_columns(self):
        from dataclasses import replace
        settings = Settings()
        settings.fields = replace(settings.fields, last_updated="上次巡查",
                                  publish_time="发帖日期")
        formula = provision.next_check_formula(settings)
        self.assertIn("[上次巡查] +", formula)
        self.assertIn('DATEDIF([发帖日期], NOW(), "D")', formula)
        self.assertNotIn("最近检查时间", formula)

    def test_a_failed_deferred_column_does_not_fail_the_created_table(self):
        workspace = fake_workspace()
        workspace.create_field.side_effect = [RuntimeError("1254xxx"), "fldOK"]
        made = provision.create_monitored_table(workspace, self.settings, "甲",
                                                log=lambda *a: None)
        self.assertEqual(made["target"], "bascnNEW:tblNEW")
        self.assertEqual(len(made["column_failures"]), 1)
        self.assertIn("父记录", made["column_failures"][0])
        self.assertNotIn("父记录", made["built"])
        self.assertIn("下次检查时间", made["built"])

    def test_a_renamed_monitor_column_follows_the_rename(self):
        """模板按角色名引用巡查列，改列名两边不漂。"""
        from dataclasses import replace
        settings = Settings()
        settings.fields = replace(settings.fields, link="笔记链接")
        fields, _deferred, _skipped = provision.template_fields(settings)
        names = [f["field_name"] for f in fields]
        self.assertIn("笔记链接", names)
        self.assertNotIn("反馈链接", names)
        self.assertEqual(names.index("笔记链接"), XIWU_COLUMNS.index("反馈链接"))


class TestShareTable(unittest.TestCase):
    """建完给谁开权限。不配的话建出来的表只有应用能管——人打开只有「可阅读」，
    连分享范围都动不了。"""

    def _plan(self, **kw):
        return provision.SharePlan(**kw)

    def test_nothing_configured_means_no_calls(self):
        workspace = fake_workspace()
        made = provision.create_monitored_table(workspace, Settings(), "甲",
                                                share=provision.SharePlan(),
                                                log=lambda *a: None)
        workspace.add_member.assert_not_called()
        workspace.transfer_owner.assert_not_called()
        self.assertEqual(made["shared"], [])

    def test_managers_get_full_access_and_chats_get_edit(self):
        workspace = fake_workspace()
        plan = self._plan(managers=("ziao@example.com", "ou_abc"),
                          editor_chats=("oc_group1",))
        result = provision.share_table(workspace, "bascnNEW", plan, log=lambda *a: None)
        self.assertEqual(workspace.add_member.call_args_list, [
            mock.call("bascnNEW", "email", "ziao@example.com", "full_access"),
            mock.call("bascnNEW", "openid", "ou_abc", "full_access"),
            mock.call("bascnNEW", "openchat", "oc_group1", "edit"),
        ])
        self.assertEqual(result.failures, [])
        # 人合成一条、不写是谁；群一条一个、写群名
        self.assertEqual(result.granted,
                         ["可管理 2 人（后台配置，面板上不显示是谁）", "群 oc_group1 可编辑"])
        self.assertNotIn("ziao@example.com", " ".join(result.granted))
        self.assertNotIn("ou_abc", " ".join(result.granted))

    def test_the_owner_is_transferred_last_and_the_app_keeps_full_access(self):
        workspace = fake_workspace()
        plan = self._plan(managers=("a@x.com",), owner="ziao@example.com")
        result = provision.share_table(workspace, "bascnNEW", plan, log=lambda *a: None)
        workspace.transfer_owner.assert_called_once_with(
            "bascnNEW", "email", "ziao@example.com")
        # 先加协作者再转所有权：转的时候应用还是所有者，最不容易出权限问题
        self.assertLess(workspace.mock_calls.index(mock.call.add_member(
            "bascnNEW", "email", "a@x.com", "full_access")),
            workspace.mock_calls.index(mock.call.transfer_owner(
                "bascnNEW", "email", "ziao@example.com")))
        self.assertIn("所有权已转给后台配置的人", result.granted[-1])
        self.assertIn("应用保留可管理", result.granted[-1])
        self.assertNotIn("ziao@example.com", " ".join(result.granted))

    def test_an_unrecognised_id_is_refused_not_guessed(self):
        """宁可拒掉让人改配置，也别猜一种类型送给飞书。失败信息按「第 N 项」
        指路，不把填的东西原样写出来。"""
        workspace = fake_workspace()
        plan = self._plan(managers=("张三",), editor_chats=("运营群",), owner="123")
        result = provision.share_table(workspace, "bascnNEW", plan, log=lambda *a: None)
        workspace.add_member.assert_not_called()
        workspace.transfer_owner.assert_not_called()
        self.assertEqual(len(result.failures), 3)
        self.assertIn("FEISHU_TABLE_MANAGERS 第 1 项", result.failures[0])
        self.assertIn("邮箱", result.failures[0])
        self.assertNotIn("张三", result.failures[0])
        self.assertIn("oc_", result.failures[1])
        self.assertIn("cli.py chats", result.failures[1])
        self.assertIn("FEISHU_TABLE_OWNER", result.failures[2])
        self.assertNotIn("123", result.failures[2])

    def test_one_failure_does_not_stop_the_rest_nor_the_table(self):
        workspace = fake_workspace()
        workspace.add_member.side_effect = [RuntimeError("230001 not in chat"), None]
        plan = self._plan(editor_chats=("oc_bad", "oc_good"))
        made = provision.create_monitored_table(workspace, Settings(), "甲",
                                                share=plan, log=lambda *a: None)
        self.assertEqual(made["target"], "bascnNEW:tblNEW")
        self.assertEqual(len(made["share_failures"]), 1)
        self.assertIn("oc_bad", made["share_failures"][0])
        self.assertIn("在这个群里", made["share_failures"][0],
                      "加群失败最常见的原因要写在失败信息里")
        self.assertEqual(made["shared"], ["群 oc_good 可编辑"])

    def test_every_grant_is_logged(self):
        """给谁开了什么权限，是这套东西改别人可见范围的唯一审计线索。"""
        lines = []
        workspace = fake_workspace()
        provision.share_table(workspace, "bascnNEW",
                              self._plan(managers=("a@x.com",), editor_chats=("oc_1",)),
                              log=lines.append)
        self.assertEqual(len(lines), 2)
        # 人按「第 N 项」记，不记是谁；群记 chat_id / 群名
        self.assertIn("FEISHU_TABLE_MANAGERS 第 1 项", lines[0])
        self.assertIn("可管理", lines[0])
        self.assertNotIn("a@x.com", lines[0])
        self.assertIn("oc_1", lines[1])
        self.assertIn("可编辑", lines[1])

    def test_member_type_inference(self):
        self.assertEqual(provision.member_type("a@b.c"), "email")
        self.assertEqual(provision.member_type(" ou_x "), "openid")
        self.assertEqual(provision.member_type("oc_x"), "openchat")
        self.assertEqual(provision.member_type("13800000000"), "mobile")
        self.assertEqual(provision.member_type("+86 138-0000-0000"), "mobile")
        self.assertEqual(provision.member_type("+852 9123 4567"), "mobile")
        self.assertEqual(provision.member_type("张三"), "")
        self.assertEqual(provision.member_type("123"), "")
        self.assertEqual(provision.member_type(""), "")
        self.assertEqual(provision.normalize_mobile(" +86 138-0000-0000 "), "+8613800000000")

    def test_a_mobile_manager_is_resolved_to_an_open_id_first(self):
        """运营的飞书账号多半是手机号登录的，而协作者接口不收手机号。"""
        workspace = fake_workspace()
        workspace.resolve_open_id.return_value = "ou_ziao"
        plan = self._plan(managers=("138 0000 0000",))
        result = provision.share_table(workspace, "bascnNEW", plan, log=lambda *a: None)
        workspace.resolve_open_id.assert_called_once_with(mobile="13800000000")
        workspace.add_member.assert_called_once_with("bascnNEW", "openid", "ou_ziao", "full_access")
        self.assertEqual(result.failures, [])
        self.assertEqual(result.granted, ["可管理 1 人（后台配置，面板上不显示是谁）"])

    def test_an_unknown_mobile_is_a_failure_with_the_scope_hint(self):
        workspace = fake_workspace()
        workspace.resolve_open_id.return_value = ""
        result = provision.share_table(workspace, "bascnNEW",
                                       self._plan(managers=("13800000000",)),
                                       log=lambda *a: None)
        workspace.add_member.assert_not_called()
        self.assertEqual(len(result.failures), 1)
        self.assertIn("FEISHU_TABLE_MANAGERS 第 1 项", result.failures[0])
        self.assertNotIn("13800000000", result.failures[0], "失败信息也不许写手机号")
        workspace.resolve_open_id.side_effect = RuntimeError("99991672 no scope")
        result = provision.share_table(workspace, "bascnNEW",
                                       self._plan(owner="13800000000"),
                                       log=lambda *a: None)
        workspace.transfer_owner.assert_not_called()
        self.assertIn("contact:user.id:readonly", result.failures[0])
        self.assertNotIn("13800000000", result.failures[0])

    def test_an_upstream_error_that_echoes_the_id_is_redacted(self):
        """**上游的报错会回声。** 飞书对一个不认识的 open_id / 邮箱，msg 里
        常常原样带着送过去的那个值，而这段 msg 会进 failures（面板直接显示）
        和运行日志——上面费劲不写身份，一句 `{exc}` 就全漏回去了。"""
        lines = []
        workspace = fake_workspace()
        workspace.add_member.side_effect = RuntimeError(
            "1254001 invalid member_id: ziao@example.com（request_id=abc）")
        result = provision.share_table(
            workspace, "bascnNEW", self._plan(managers=("ziao@example.com",)),
            log=lines.append)
        everything = " ".join(result.failures + lines)
        self.assertNotIn("ziao@example.com", everything)
        self.assertIn("…", everything)
        # 抹的是身份，不是整条报错——request_id 是找厂商排查的唯一凭据
        self.assertIn("request_id=abc", everything)
        self.assertIn("1254001", everything)

    def test_a_mobile_echo_is_redacted_on_the_lookup_path_too(self):
        workspace = fake_workspace()
        workspace.resolve_open_id.side_effect = RuntimeError(
            "no user for mobile 13800008888")
        result = provision.share_table(
            workspace, "bascnNEW", self._plan(managers=("138 0000 8888",)),
            log=lambda *a: None)
        self.assertNotIn("13800008888", result.failures[0])
        self.assertNotIn("138 0000 8888", result.failures[0])
        self.assertIn("contact:user.id:readonly", result.failures[0])

    def test_the_resolved_open_id_is_redacted_from_an_echo(self):
        """手机号换来的 open_id 同样是身份：报错回声它也要抹。"""
        workspace = fake_workspace()
        workspace.resolve_open_id.return_value = "ou_resolved_abc"
        workspace.add_member.side_effect = RuntimeError("bad member ou_resolved_abc")
        result = provision.share_table(
            workspace, "bascnNEW", self._plan(managers=("13800008888",)),
            log=lambda *a: None)
        self.assertNotIn("ou_resolved_abc", result.failures[0])

    def test_redact_leaves_short_values_alone(self):
        """一两个字符的值抹下去会把整句话打成马赛克。"""
        self.assertEqual(provision.redact("a bad id", "a"), "a bad id")
        self.assertEqual(provision.redact("id=abcd 出错", "abcd"), "id=… 出错")

    def test_people_never_appear_in_results_or_logs_but_group_names_do(self):
        """手机号 / 邮箱 / open_id 是个人信息，面板口令又是运营共用的：
        管理员是谁不进结果、不进日志。群名可以——群本来就是给运营看的。"""
        lines = []
        workspace = fake_workspace()
        workspace.resolve_open_id.return_value = "ou_ziao"
        plan = self._plan(managers=("13800008888", "ziao@example.com"), editor_chats=("oc_1",),
                          owner="ou_boss", labels={"oc_1": "梨响运营群"})
        result = provision.share_table(workspace, "bascnNEW", plan, log=lines.append)
        everything = " ".join(result.granted + result.failures + lines)
        for secret in ("13800008888", "ziao@example.com", "ou_ziao", "ou_boss"):
            self.assertNotIn(secret, everything, f"{secret} 泄露到了结果或日志里")
        self.assertIn("梨响运营群", everything)
        self.assertEqual(result.granted[0], "可管理 2 人（后台配置，面板上不显示是谁）")
        self.assertIn("第 1 项", lines[0])
        self.assertIn("第 2 项", lines[1])


if __name__ == "__main__":
    unittest.main()
