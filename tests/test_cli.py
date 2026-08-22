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
                       f.pinned_comment, f.comment_status, f.comment_digest,
                       f.seed_match, f.traffic_status, f.refresh_status,
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
        # 「待评论」机器从不写，不该逼运营去建这个选项
        self.assertNotIn(self.settings.comment_status.pending, status_required)

    def test_multiselect_columns_must_be_multiselect(self):
        """两个合并写入的列必须是多选（类型码 4）：建成单选，机器按列表
        写入会让整批写回失败。"""
        f = self.settings.fields
        self.assertEqual(self.schema[f.traffic_status][0], (4,))
        self.assertEqual(self.schema[f.comment_status][0], (4,))


if __name__ == "__main__":
    unittest.main()
