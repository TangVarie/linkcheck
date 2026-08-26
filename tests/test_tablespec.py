"""「哪张表」的解析和查重。全部离线，纯函数。

这一层现在有三个来源（环境变量、飞书注册表、面板表单），后两个是运营能填的。
所以两件事必须钉死：**字符集校验**（它拼进带 tenant_access_token 的地址）
和 **查重键是 table_id 单独一维**（否则同一张表用两种链接各登记一次，
一轮内付两次钱）。
"""

import unittest

from xhsearch import tablespec


class TestParseTarget(unittest.TestCase):
    def test_token_pair_form(self):
        t = tablespec.parse_target("OKMAN一期=bascnA:tbl1")
        self.assertEqual(t.as_tuple(), ("OKMAN一期", "bascnA", "tbl1"))

    def test_base_url(self):
        t = tablespec.parse_target(
            "二期=https://xx.feishu.cn/base/bascnA?table=tbl2&view=vewZ")
        self.assertEqual(t.as_tuple(), ("二期", "bascnA", "tbl2"))

    def test_wiki_url_is_treated_the_same(self):
        """实测过：/wiki/ 地址栏那段 token 直接当 app_token 用，接口就认。"""
        t = tablespec.parse_target("C=https://xx.feishu.cn/wiki/wikcnA?table=tbl9")
        self.assertEqual(t.as_tuple(), ("C", "wikcnA", "tbl9"))

    def test_label_defaults_to_table_id(self):
        self.assertEqual(tablespec.parse_target("bascnB:tbl3").label, "tbl3")

    def test_explicit_default_label_wins_over_table_id(self):
        """注册表和面板里标签是单独一格，不在这段文本里。"""
        t = tablespec.parse_target("bascnB:tbl3", default_label="途鸽三期")
        self.assertEqual(t.label, "途鸽三期")

    def test_inline_label_beats_the_default(self):
        t = tablespec.parse_target("甲=bascnB:tbl3", default_label="乙")
        self.assertEqual(t.label, "甲")

    def test_url_without_table_param_is_rejected_with_guidance(self):
        with self.assertRaises(tablespec.BadTarget) as ctx:
            tablespec.parse_target("甲=https://xx.feishu.cn/base/bascnA")
        self.assertIn("?table=", str(ctx.exception))

    def test_garbage_is_rejected(self):
        for junk in ("甲=看不懂的东西", "", "   ", ","):
            with self.assertRaises(tablespec.BadTarget):
                tablespec.parse_target(junk)


class TestTokenCharset(unittest.TestCase):
    """token 会被拼进接口地址，而那个请求带着 tenant_access_token。
    一个含 `/` 或 `..` 的值就能把它改写到别的 open-apis 端点上。"""

    def test_path_traversal_shapes_are_rejected(self):
        for evil in ("../../auth/v3", "a/b", "a?x=1", "a#b", "a%2e%2e",
                     "a b", "a:b", "a\\b"):
            self.assertFalse(tablespec.valid_token(evil), evil)
            with self.assertRaises(tablespec.BadTarget, msg=evil):
                tablespec.parse_target(f"甲={evil}:tbl1")

    def test_a_bad_table_id_is_caught_too(self):
        with self.assertRaises(tablespec.BadTarget):
            tablespec.parse_target("甲=bascnA:../../x")

    def test_real_looking_tokens_pass(self):
        for good in ("bascnABCDEFG1234567", "tblXYZ7890", "wikcnA1b2C3"):
            self.assertTrue(tablespec.valid_token(good), good)

    def test_charset_not_length_is_the_rule(self):
        """长度下限拦不住任何攻击（`..` 才两个字符），只会在飞书改短 token
        格式时误伤合法配置。"""
        self.assertTrue(tablespec.valid_token("a"))
        self.assertFalse(tablespec.valid_token(".."))
        self.assertFalse(tablespec.valid_token("a" * 65))


class TestParseMany(unittest.TestCase):
    def test_semicolons_newlines_and_chinese_semicolons(self):
        spec = "甲=bascnA:tbl1\n乙=bascnA:tbl2；丙=bascnB:tbl3"
        self.assertEqual([t.label for t in tablespec.parse_many(spec)],
                         ["甲", "乙", "丙"])

    def test_blank_chunks_are_skipped(self):
        self.assertEqual(len(tablespec.parse_many("甲=bascnA:tbl1;;\n\n; ,")), 1)

    def test_empty_spec_is_empty_list(self):
        self.assertEqual(tablespec.parse_many(""), [])


class TestFindDuplicate(unittest.TestCase):
    def test_clean_list_has_no_complaint(self):
        targets = tablespec.parse_many("甲=bascnA:tbl1; 乙=bascnB:tbl2")
        self.assertEqual(tablespec.find_duplicate(targets), "")

    def test_same_table_twice(self):
        targets = tablespec.parse_many("甲=bascnA:tbl1; 乙=bascnA:tbl1")
        self.assertIn("tbl1", tablespec.find_duplicate(targets))

    def test_base_and_wiki_links_to_the_same_table_are_caught(self):
        """这是用 (app_token, table_id) 当键挡不住的那一种：两个 app_token
        不同、指的是同一张表 → 一轮内付两次钱。"""
        targets = tablespec.parse_many(
            "甲=https://x.feishu.cn/base/bascnA?table=tblSAME; "
            "乙=https://x.feishu.cn/wiki/wikcnB?table=tblSAME")
        problem = tablespec.find_duplicate(targets)
        self.assertIn("tblSAME", problem)
        self.assertIn("/wiki/", problem)

    def test_duplicate_labels_are_caught(self):
        targets = tablespec.parse_many("甲=bascnA:tbl1; 甲=bascnB:tbl2")
        self.assertIn("标签重复", tablespec.find_duplicate(targets))


class TestCliStillBehaves(unittest.TestCase):
    """cli 那一层的契约没变：解析失败 sys.exit，成功返回三元组。"""

    def test_exits_on_duplicate_across_link_forms(self):
        import cli
        with self.assertRaises(SystemExit):
            cli._tables_from_env({"FEISHU_TABLES":
                "甲=https://x.feishu.cn/base/bascnA?table=tblSAME; "
                "乙=https://x.feishu.cn/wiki/wikcnB?table=tblSAME"})

    def test_single_table_vars_are_validated_too(self):
        import cli
        with self.assertRaises(SystemExit):
            cli._tables_from_env({"FEISHU_APP_TOKEN": "../../x",
                                  "FEISHU_TABLE_ID": "tblX"})


if __name__ == "__main__":
    unittest.main()
