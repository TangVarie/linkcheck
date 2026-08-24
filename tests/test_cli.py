"""cli 层的离线测试：doctor 的期望 schema 必须和代码实际读写的列对得上。

「表面对了，内在配置没对」的体检清单如果本身漏了列，就等于没体检——
这里钉住：代码要读的每一列、要写的每个选择值，都在 doctor 的清单里。
"""

import unittest
from unittest import mock

import cli
from xhsearch import feishu
from xhsearch.config import Settings


class TestDoctorSchema(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.schema = {name: (allowed, label, options, note)
                       for name, allowed, label, options, note
                       in cli._expected_schema(self.settings)}

    def test_covers_every_column_the_code_reads(self):
        for column in self.settings.fields.must_read():
            self.assertIn(column, self.schema, f"doctor 的体检清单漏了要读的列「{column}」")

    def test_covers_every_machine_written_column(self):
        f = self.settings.fields
        for column in (f.platform, f.comment_count, f.previous_comment_count,
                       f.like_count, f.previous_like_count,
                       f.collect_count, f.previous_collect_count,
                       f.pinned_status, f.comment_status, f.comment_digest,
                       f.traffic_status, f.refresh_status,
                       f.failure_reason, f.last_updated, f.alive_confirmed,
                       f.consecutive_failures):
            self.assertIn(column, self.schema, f"doctor 的体检清单漏了要写的列「{column}」")

    def test_select_columns_require_every_machine_value(self):
        """机器往选择列里写的每一个值都必须在必备选项清单里——
        漏一个，那个值就会被 known_options 静默拦下，判定等于没生效。"""
        f = self.settings.fields
        _, _, traffic_required, _ = self.schema[f.traffic_status]
        for tag in self.settings.tags.machine_written():
            self.assertIn(tag, traffic_required)
        # 退役标签（已失效）只留在 merge 的管辖范围里用于摘掉旧值，
        # 不该被 doctor 要求建选项——新表根本不需要它。
        for tag in self.settings.tags.retired:
            self.assertNotIn(tag, traffic_required)
            self.assertIn(tag, self.settings.tags.namespace())
        _, _, status_required, _ = self.schema[f.comment_status]
        for value in self.settings.comment_status.machine_written():
            self.assertIn(value, status_required)
        _, _, pin_required, _ = self.schema[f.pinned_status]
        for value in self.settings.pin_status.machine_written():
            self.assertIn(value, pin_required)

    def test_column_types_match_write_semantics(self):
        """流量状态按多选列表合并写入（必须类型码 4）；评论状态/置顶状态
        按单选字符串覆盖写入（必须类型码 3）。类型和写法错配 = 整行写回失败。"""
        f = self.settings.fields
        self.assertEqual(self.schema[f.traffic_status][0], (4,))
        self.assertEqual(self.schema[f.comment_status][0], (3,))
        self.assertEqual(self.schema[f.pinned_status][0], (3,))


class TestTablesFromEnv(unittest.TestCase):
    """FEISHU_TABLES 解析：这是多表部署的唯一配置入口，
    解析错一项就是少刷一整张表（静默）或整个进程起不来（吵闹）——
    必须吵闹，且报错要说得清哪一项、该怎么写。"""

    def test_single_table_fallback(self):
        entries = cli._tables_from_env(
            {"FEISHU_APP_TOKEN": "bascnA", "FEISHU_TABLE_ID": "tblX"})
        self.assertEqual(entries, [("tblX", "bascnA", "tblX")])

    def test_multi_with_labels_and_both_forms(self):
        spec = ("OKMAN一期=bascnA:tbl1; "
                "OKMAN二期=https://xx.feishu.cn/base/bascnA?table=tbl2&view=vewZ;"
                "bascnB:tbl3")
        entries = cli._tables_from_env({"FEISHU_TABLES": spec})
        self.assertEqual(entries, [
            ("OKMAN一期", "bascnA", "tbl1"),
            ("OKMAN二期", "bascnA", "tbl2"),
            ("tbl3", "bascnB", "tbl3"),      # 不带标签时标签取 table_id
        ])

    def test_tables_wins_over_single_vars(self):
        entries = cli._tables_from_env({
            "FEISHU_TABLES": "甲=bascnA:tbl1",
            "FEISHU_APP_TOKEN": "bascnZ", "FEISHU_TABLE_ID": "tblZ"})
        self.assertEqual(entries, [("甲", "bascnA", "tbl1")])

    def test_newline_and_chinese_semicolon_separators(self):
        entries = cli._tables_from_env(
            {"FEISHU_TABLES": "甲=bascnA:tbl1\n乙=bascnA:tbl2；丙=bascnB:tbl3"})
        self.assertEqual([e[0] for e in entries], ["甲", "乙", "丙"])

    def test_nothing_configured_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env({})

    def test_garbage_entry_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env({"FEISHU_TABLES": "甲=看不懂的东西"})

    def test_url_without_table_param_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=https://xx.feishu.cn/base/bascnA"})

    def test_duplicate_table_exits(self):
        """同一张表配两遍会被两轮 cron 各刷一次——纯白花钱，必须拦。"""
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=bascnA:tbl1; 乙=bascnA:tbl1"})

    def test_duplicate_label_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=bascnA:tbl1; 甲=bascnB:tbl2"})

    def test_wiki_link_is_parsed_with_pending_marker(self):
        """/wiki/ 链接这一步只抠 node_token，真正换成 app_token 要等
        _resolve_wiki_entries 拿到凭据之后再调接口，这里先占位。"""
        entries = cli._tables_from_env(
            {"FEISHU_TABLES": "企业C=https://xx.feishu.cn/wiki/wikcnA?table=tbl9&view=vewZ"})
        self.assertEqual(entries, [("企业C", cli._WIKI_PENDING_PREFIX + "wikcnA", "tbl9")])


class TestResolveWikiEntries(unittest.TestCase):
    """_tables_from_env 占位的 /wiki/ 条目，要在这一步真正换成 app_token。"""

    def test_wiki_entries_are_resolved_via_feishu(self):
        entries = [("企业C", cli._WIKI_PENDING_PREFIX + "wikcnA", "tbl9"),
                   ("甲", "bascnA", "tbl1")]
        with mock.patch.object(cli.feishu, "resolve_wiki_node",
                               return_value="bascnResolved") as resolver:
            resolved = cli._resolve_wiki_entries("app-id", "app-secret", entries)
        resolver.assert_called_once_with("app-id", "app-secret", "wikcnA")
        self.assertEqual(resolved, [("企业C", "bascnResolved", "tbl9"),
                                    ("甲", "bascnA", "tbl1")])

    def test_resolve_failure_exits_with_label_in_message(self):
        entries = [("企业C", cli._WIKI_PENDING_PREFIX + "wikcnA", "tbl9")]
        with mock.patch.object(cli.feishu, "resolve_wiki_node",
                               side_effect=feishu.FeishuError(99991672, "无权限")):
            with self.assertRaises(SystemExit) as ctx:
                cli._resolve_wiki_entries("app-id", "app-secret", entries)
        self.assertIn("企业C", str(ctx.exception))


class TestMainArgs(unittest.TestCase):
    def test_empty_table_filter_exits(self):
        """「--table ,」解析出空清单，和「没传 --table」在下游没法区分，
        会静默变成全表都跑——必须当场拒绝。"""
        with self.assertRaises(SystemExit):
            cli.main(["cli.py", "sweep", "--table", ","])


class TestLoadDotenv(unittest.TestCase):
    """本地跑 doctor/row 的前提：.env 真的会被读进环境变量。
    没有这个加载器，文档里「填好 .env 就能跑」在本地是空头支票。"""

    def test_reads_env_file_without_overriding_real_env(self):
        import os
        import tempfile
        from pathlib import Path

        from xhsearch.envfile import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "# 注释行\n"
                "TEST_ENVFILE_A=hello\n"
                'TEST_ENVFILE_B="quoted"\n'
                "TEST_ENVFILE_EXISTING=from-file\n"
                "TEST_ENVFILE_EMPTY=\n"
                "没有等号的行\n",
                encoding="utf-8")
            os.environ["TEST_ENVFILE_EXISTING"] = "from-real-env"
            try:
                load_dotenv(Path(tmp))
                self.assertEqual(os.environ["TEST_ENVFILE_A"], "hello")
                self.assertEqual(os.environ["TEST_ENVFILE_B"], "quoted")
                # 已存在的环境变量优先——.env 只补缺不覆盖
                self.assertEqual(os.environ["TEST_ENVFILE_EXISTING"], "from-real-env")
                # 空值不写入，免得把「没填」变成「填了空串」
                self.assertNotIn("TEST_ENVFILE_EMPTY", os.environ)
            finally:
                for name in ("TEST_ENVFILE_A", "TEST_ENVFILE_B",
                             "TEST_ENVFILE_EXISTING", "TEST_ENVFILE_EMPTY"):
                    os.environ.pop(name, None)

    def test_missing_file_is_silently_skipped(self):
        import tempfile
        from pathlib import Path

        from xhsearch.envfile import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            load_dotenv(Path(tmp))   # 没有 .env：不抛异常即通过


class TestOptionsFromMeta(unittest.TestCase):
    """None = 查不到别过滤；[] = 不是选择列全拦。写反任何一个分支，
    要么未建选项写进表（整批回滚），要么机器值全部静默丢失。"""

    def test_unreadable_meta_means_no_filter(self):
        self.assertIsNone(cli._options_from_meta(None, "流量状态"))

    def test_missing_column_means_no_filter(self):
        self.assertIsNone(cli._options_from_meta({}, "流量状态"))

    def test_non_select_column_blocks_everything(self):
        meta = {"流量状态": {"type": 1, "ui_type": "Text", "options": None}}
        self.assertEqual(cli._options_from_meta(meta, "流量状态"), [])

    def test_select_column_returns_its_options(self):
        meta = {"流量状态": {"type": 4, "ui_type": "MultiSelect", "options": ["爆贴"]}}
        self.assertEqual(cli._options_from_meta(meta, "流量状态"), ["爆贴"])


class TestSchemaProblems(unittest.TestCase):
    """doctor 的判定逻辑本体：喂典型翻车样本，核对报没报、报得对不对。"""

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields

    def _healthy_meta(self) -> dict:
        meta = {}
        for name, allowed, _label, options, _note in cli._expected_schema(self.settings):
            field_type = allowed[0]
            meta[name] = {
                "type": field_type,
                "ui_type": "",
                "options": list(options or []) if field_type in (3, 4) else None,
            }
        return meta

    def test_healthy_table_has_no_problems(self):
        self.assertEqual(cli._schema_problems(self.settings, self._healthy_meta()), [])

    def test_multiselect_comment_status_is_flagged(self):
        """评论状态现在是单选覆盖写入：建成多选（旧口径的类型）要被点名。"""
        meta = self._healthy_meta()
        meta[self.f.comment_status]["type"] = 4
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.comment_status in p and "类型" in p for p in problems))

    def test_system_modified_time_is_flagged(self):
        """「最近检查时间」建成系统的「最后更新时间」类型：机器写不进去，
        且任何编辑都会刷新它——doctor 必须点名。"""
        meta = self._healthy_meta()
        meta[self.f.last_updated] = {"type": 1002, "ui_type": "ModifiedTime",
                                     "options": None}
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.last_updated in p for p in problems))

    def test_missing_machine_option_wording_depends_on_filtering(self):
        """流量状态走 merge 过滤，缺选项=安全跳过；巡查状态是直写，
        缺选项=可能整行写回失败。两种后果的文案必须如实区分。"""
        meta = self._healthy_meta()
        meta[self.f.traffic_status]["options"].remove("大爆")
        meta[self.f.refresh_status]["options"].remove("跳过")
        problems = cli._schema_problems(self.settings, meta)
        traffic = next(p for p in problems if self.f.traffic_status in p)
        status = next(p for p in problems if self.f.refresh_status in p)
        self.assertIn("跳过（不会误写", traffic)
        self.assertIn("写回失败", status)

    def test_select_without_options_key_still_reports_missing(self):
        """类型已确认是多选、但 API 没带 options 键（零选项的另一种形态）：
        必须按空清单报缺选项，不能当成非选择列放行——运行时会全拦。"""
        meta = self._healthy_meta()
        meta[self.f.comment_status]["options"] = None
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.comment_status in p and "缺这些选项" in p
                            for p in problems))

    def test_exotic_number_subtype_is_flagged(self):
        """评分字段和普通数字共用类型码 2，但封顶 5 星——光看类型码抓不到。"""
        meta = self._healthy_meta()
        meta[self.f.like_count]["ui_type"] = "Rating"
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.like_count in p and "评分" in p for p in problems))

    def test_missing_required_column_is_called_out_separately(self):
        meta = self._healthy_meta()
        del meta[self.f.link]
        del meta[self.f.comment_digest]
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any("必备列" in p and self.f.link in p for p in problems))
        self.assertTrue(any("自动跳过" in p and self.f.comment_digest in p for p in problems))

    def test_missing_timestamp_column_is_required(self):
        """「最近检查时间」缺失时 sweep 会失控全表重刷——必须按必备列报，
        不能混进「会被自动跳过」的机器列清单里轻描淡写。"""
        meta = self._healthy_meta()
        del meta[self.f.last_updated]
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any("必备列" in p and self.f.last_updated in p for p in problems))


if __name__ == "__main__":
    unittest.main()
