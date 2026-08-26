"""建列请求体 + 现有表和期望的差异。全部离线，纯函数。

`expected_schema` 是「这张表应该长什么样」的唯一真相，体检和建表共用它。
这些用例盯的是：翻译成飞书请求体时别丢东西，以及三类差异
（缺列 / 缺选项 / 类型建错）的处置不能混——前两类面板能做，第三类只能报。
"""

import unittest

from xhsearch import schema
from xhsearch.config import Settings


def healthy_meta(settings=None, drop=(), retype=None, drop_options=None):
    settings = settings or Settings()
    meta = {}
    for name, allowed, _label, options, _note in schema.expected_schema(settings):
        meta[name] = {"type": allowed[0], "ui_type": "",
                      "options": list(options) if options else None}
    for name in drop:
        meta.pop(name, None)
    for name, code in (retype or {}).items():
        meta[name]["type"] = code
    for name, keep in (drop_options or {}).items():
        meta[name]["options"] = list(keep)
    return meta


class TestCreateFieldBody(unittest.TestCase):
    def test_text_field_needs_no_property(self):
        self.assertEqual(schema.create_field_body("反馈链接", 1),
                         {"field_name": "反馈链接", "type": 1})

    def test_number_field_asks_for_integers(self):
        body = schema.create_field_body("实时数据.评论数", 2)
        self.assertEqual(body["property"], {"formatter": "0"})

    def test_date_field_keeps_minutes(self):
        """「最近检查时间」要能和日志逐字对上，只到天不够。"""
        body = schema.create_field_body("最近检查时间", 5)
        self.assertIn("HH:mm", body["property"]["date_formatter"])

    def test_checkbox_is_bare(self):
        self.assertNotIn("property", schema.create_field_body("是否巡查", 7))

    def test_select_fields_are_built_with_their_options(self):
        """新建的列没有存量数据，带选项一次建完是纯追加、零风险——
        比「先建空列再补选项」少一整类问题。"""
        body = schema.create_field_body("流量状态", 4, ["观察中", "爆贴"])
        self.assertEqual(body["type"], 4)
        self.assertEqual(body["property"]["options"],
                         [{"name": "观察中"}, {"name": "爆贴"}])

    def test_select_without_options_omits_them(self):
        body = schema.create_field_body("巡查状态", 3, [])
        self.assertNotIn("options", body.get("property", {}))


class TestDiff(unittest.TestCase):
    def test_a_healthy_table_is_clean(self):
        d = schema.diff(Settings(), healthy_meta())
        self.assertTrue(d.clean)
        self.assertFalse(d.auto_fixable)

    def test_missing_columns_are_listed_with_buildable_bodies(self):
        settings = Settings()
        f = settings.fields
        meta = healthy_meta(settings, drop=[f.negative_status, f.comment_digest])
        d = schema.diff(settings, meta)
        names = [c.name for c in d.missing_columns]
        self.assertEqual(sorted(names), sorted([f.negative_status, f.comment_digest]))
        self.assertTrue(d.auto_fixable)
        # 缺的选择列要连选项一起建出来
        negative = next(c for c in d.missing_columns if c.name == f.negative_status)
        options = negative.body()["property"]["options"]
        self.assertEqual([o["name"] for o in options],
                         settings.negative_status.machine_written())

    def test_wrong_type_is_reported_not_queued_for_building(self):
        """改类型会转换/丢已有数据，绝不自动做。"""
        settings = Settings()
        meta = healthy_meta(settings, retype={settings.fields.traffic_status: 3})
        d = schema.diff(settings, meta)
        self.assertEqual(d.missing_columns, [])
        self.assertEqual(len(d.wrong_types), 1)
        self.assertIn("流量状态", d.wrong_types[0].describe())
        self.assertFalse(d.auto_fixable, "类型建错不该让「一键建齐」亮起来")

    def test_exotic_ui_type_counts_as_wrong_type(self):
        """评分/进度和普通数字共用类型码，但写入行为不同。"""
        settings = Settings()
        meta = healthy_meta(settings)
        meta[settings.fields.comment_count]["ui_type"] = "Rating"
        d = schema.diff(settings, meta)
        self.assertEqual(len(d.wrong_types), 1)
        self.assertIn("评分", d.wrong_types[0].actual)

    def test_missing_options_are_separate_from_missing_columns(self):
        settings = Settings()
        f = settings.fields
        meta = healthy_meta(settings, drop_options={f.traffic_status: ["爆贴"]})
        d = schema.diff(settings, meta)
        self.assertEqual(d.missing_columns, [])
        self.assertEqual(len(d.missing_options), 1)
        gap = d.missing_options[0]
        self.assertEqual(gap.column, f.traffic_status)
        self.assertNotIn("爆贴", gap.missing)
        self.assertIn("风控中", gap.missing)
        self.assertEqual(gap.existing, ["爆贴"],
                         "补选项要靠现值做并集，所以现值必须带出来")

    def test_a_wrongly_typed_select_is_not_also_reported_as_missing_options(self):
        """一个毛病只该占一个位置，否则运营看到两条以为是两件事。"""
        settings = Settings()
        meta = healthy_meta(settings, retype={settings.fields.traffic_status: 1})
        d = schema.diff(settings, meta)
        self.assertEqual(len(d.wrong_types), 1)
        self.assertEqual(d.missing_options, [])

    def test_empty_meta_means_everything_is_missing(self):
        settings = Settings()
        d = schema.diff(settings, {})
        self.assertEqual(len(d.missing_columns),
                         len(schema.expected_schema(settings)))

    def test_none_meta_does_not_crash(self):
        self.assertEqual(len(schema.diff(Settings(), None).missing_columns),
                         len(schema.expected_schema(Settings())))


class TestDiffAgreesWithDoctor(unittest.TestCase):
    """`diff` 和 `schema_problems` 看的是同一批东西，产出不同用途。
    两边对「这张表有没有问题」的判断必须一致——不然面板说绿、doctor 说红。"""

    def _cases(self):
        settings = Settings()
        f = settings.fields
        return [
            ("健康", healthy_meta(settings), True),
            ("缺列", healthy_meta(settings, drop=[f.negative_digest]), False),
            ("类型错", healthy_meta(settings, retype={f.last_updated: 1002}), False),
            ("缺选项", healthy_meta(settings, drop_options={f.pinned_status: []}), False),
        ]

    def test_clean_matches_no_problems(self):
        settings = Settings()
        for name, meta, expected_clean in self._cases():
            with self.subTest(name):
                d = schema.diff(settings, meta)
                problems = schema.schema_problems(settings, meta)
                self.assertEqual(d.clean, expected_clean)
                self.assertEqual(d.clean, not problems,
                                 f"{name}：diff 说 clean={d.clean}，"
                                 f"doctor 说 {len(problems)} 个问题")


if __name__ == "__main__":
    unittest.main()
