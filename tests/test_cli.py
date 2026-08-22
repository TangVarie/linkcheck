"""cli 层的离线测试：doctor 的期望 schema 必须和代码实际读写的列对得上。

「表面对了，内在配置没对」的体检清单如果本身漏了列，就等于没体检——
这里钉住：代码要读的每一列、要写的每个选择值，都在 doctor 的清单里。
"""

import unittest

import cli
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
        for tag in self.settings.tags.namespace():
            self.assertIn(tag, traffic_required)
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
