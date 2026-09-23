"""截图读数四列的纯函数：公式比对、空格子判定。全部离线。

这几条判断直接决定项目卡上会不会冒出一条「截图读数」提醒——判宽了是假提醒
（教人不再信这张卡），判严了是漏报（捷径没在干活却没人知道）。
"""

import unittest

from xhsearch import readout


class TestColumns(unittest.TestCase):
    def test_the_data_column_comes_before_the_formulas_that_read_it(self):
        self.assertEqual(readout.NAMES, ("数据整理", "曝光量", "阅读量", "互动数"))
        for column in readout.COLUMNS[1:]:
            self.assertEqual(column.type_code, 20)
            self.assertEqual(column.needs, ("数据整理",))
            self.assertIn("[数据整理]", column.formula)

    def test_only_the_data_column_has_a_manual_step(self):
        steps = [c.name for c in readout.COLUMNS if c.manual_step]
        self.assertEqual(steps, ["数据整理"])
        self.assertIn(readout.SCREENSHOT_COLUMN, readout.MANUAL_STEP)


class TestFormulaComparison(unittest.TestCase):
    STANDARD = dict(readout.FORMULAS)["曝光量"]

    def test_whitespace_outside_quotes_does_not_count(self):
        spread = self.STANDARD.replace("(", "(\n    ").replace(", ", " ,  ")
        self.assertFalse(readout.formula_differs(spread, self.STANDARD))

    def test_whitespace_inside_quotes_does(self):
        self.assertTrue(readout.formula_differs(
            self.STANDARD.replace('""', '" "'), self.STANDARD))

    def test_a_different_formula_differs(self):
        self.assertTrue(readout.formula_differs("LEN([数据整理])", self.STANDARD))
        self.assertTrue(readout.formula_differs("", self.STANDARD),
                        "读到了、而且是空的——那就是没写公式")

    def test_unknown_means_no_claim(self):
        self.assertFalse(readout.formula_differs(None, self.STANDARD))
        self.assertFalse(readout.formula_differs(
            "LEN(bitable::$table[tblX].$field[fldY])", self.STANDARD))


class TestBlank(unittest.TestCase):
    def test_what_counts_as_empty(self):
        for value in (None, "", "   ", [], [{"text": "", "type": "text"}],
                      [{"text": "  "}], {"text": ""}):
            with self.subTest(value):
                self.assertTrue(readout.blank(value))

    def test_what_counts_as_filled(self):
        for value in ("曝光量：/；阅读量：/；互动量：/",
                      [{"text": "曝光量：1；阅读量：2；互动量：3", "type": "text"}],
                      [{"file_token": "boxA"}],          # 附件
                      {"value": ["x"], "type": 1},       # 认不出的形状：算有东西
                      0, 12.5):
            with self.subTest(value):
                self.assertFalse(readout.blank(value))


class TestUnreadRows(unittest.TestCase):
    def test_counts_rows_with_screenshots_and_no_text(self):
        shot = [{"file_token": "boxA"}]
        records = [
            {"fields": {"相关截图": shot}},
            {"fields": {"相关截图": shot, "数据整理": ""}},
            {"fields": {"相关截图": shot, "数据整理": "曝光量：1；阅读量：1；互动量：1"}},
            {"fields": {"数据整理": ""}},
            {"fields": {}},
            {},
        ]
        self.assertEqual(readout.unread_rows(records), 2)

    def test_the_message_names_the_count_and_the_usual_causes(self):
        text = readout.unread_message(3)
        self.assertIn("有 3 行", text)
        for cause in ("没挂上", "自动更新", "豆包账号"):
            self.assertIn(cause, text)


if __name__ == "__main__":
    unittest.main()
