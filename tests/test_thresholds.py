"""逐表阈值：读、校验、覆盖、以及改之前算给人看。全部离线，纯函数。

这一层的红线是：**只放纯行级分类的参数**。熔断比例、两击定罪、冷却、
单轮预算有跨表语义（apply_cross_run_breaker 要把各表样本加总重算），
逐表不同会让「这一轮该不该熔」说不清——它们不在这里，也不会加。
"""

import unittest
from datetime import timedelta

from xhsearch import registry, summary
from xhsearch.config import Settings
from tests.test_summary import NOW, record


def entry(**thresholds):
    return registry.Entry(label="项目A", app_token="a", table_id="t",
                          thresholds=thresholds)


class TestReadOverrides(unittest.TestCase):
    def test_empty_means_global_defaults(self):
        override = registry.read_overrides(entry())
        self.assertFalse(override.any)
        self.assertEqual(override.problems, [])

    def test_valid_values_come_through(self):
        override = registry.read_overrides(entry(**{
            registry.COL_TIER_HOT: 30, registry.COL_FLOP_HOURS: 72}))
        self.assertEqual(override.values[registry.COL_TIER_HOT], 30)
        self.assertEqual(override.values[registry.COL_FLOP_HOURS], 72)

    def test_non_ascending_tiers_are_rejected_as_a_group(self):
        """「爆贴」门槛比「大爆」还高的话，热度档永远是错的。"""
        override = registry.read_overrides(entry(**{
            registry.COL_TIER_HOT: 100, registry.COL_TIER_SUPER_HOT: 50}))
        self.assertNotIn(registry.COL_TIER_HOT, override.values)
        self.assertIn("递增", override.problems[0])

    def test_ascending_tiers_pass(self):
        override = registry.read_overrides(entry(**{
            registry.COL_TIER_EVALUATING: 10, registry.COL_TIER_HOT: 30,
            registry.COL_TIER_SUPER_HOT: 90}))
        self.assertEqual(len(override.values), 3)
        self.assertEqual(override.problems, [])

    def test_out_of_range_values_are_reported_not_silently_dropped(self):
        """静默忽略一个配置，比不支持它更糟。"""
        override = registry.read_overrides(entry(**{
            registry.COL_FLOP_HOURS: 0, registry.COL_ARCHIVE_DAYS: 99999}))
        self.assertEqual(override.values, {})
        self.assertEqual(len(override.problems), 2)
        self.assertTrue(all("已忽略" in p for p in override.problems))

    def test_a_bad_group_does_not_kill_the_good_ones(self):
        override = registry.read_overrides(entry(**{
            registry.COL_TIER_HOT: 100, registry.COL_TIER_SUPER_HOT: 50,
            registry.COL_FLOP_HOURS: 72}))
        self.assertEqual(override.values, {registry.COL_FLOP_HOURS: 72})


class TestApplyOverrides(unittest.TestCase):
    def test_no_override_returns_the_base_object(self):
        base = Settings()
        self.assertIs(registry.apply_overrides(base, entry()), base)

    def test_the_base_settings_are_never_mutated(self):
        """就地改会让第一张表的阈值串味到后面所有表——那种 bug 只在多表
        部署上出现，看起来像「判定口径莫名其妙」。"""
        base = Settings()
        registry.apply_overrides(base, entry(**{registry.COL_TIER_HOT: 30}))
        self.assertEqual(base.thresholds.tier_hot, 50)

    def test_each_field_lands_where_it_should(self):
        got = registry.apply_overrides(Settings(), entry(**{
            registry.COL_TIER_EVALUATING: 5, registry.COL_TIER_HOT: 30,
            registry.COL_TIER_SUPER_HOT: 90, registry.COL_FLOP_HOURS: 72,
            registry.COL_ARCHIVE_DAYS: 60}))
        self.assertEqual(got.thresholds.tier_evaluating, 5)
        self.assertEqual(got.thresholds.tier_hot, 30)
        self.assertEqual(got.thresholds.tier_super_hot, 90)
        self.assertEqual(got.thresholds.flop_hours, 72)
        self.assertEqual(got.refresh.archive_after_days, 60)

    def test_untouched_fields_keep_the_global_value(self):
        got = registry.apply_overrides(Settings(), entry(**{
            registry.COL_TIER_HOT: 30}))
        self.assertEqual(got.thresholds.tier_evaluating, 20)
        self.assertEqual(got.thresholds.tier_super_hot, 100)

    def test_cross_table_parameters_are_not_overridable(self):
        """熔断/两击/冷却/预算有跨表语义，逐表不同会让熔断说不清。
        它们连**列**都没有——填不进来，不是靠代码去挡。"""
        for forbidden in ("熔断", "两击", "冷却", "预算", "并发"):
            self.assertFalse(
                any(forbidden in c for c in registry.THRESHOLD_COLUMNS),
                f"{forbidden} 不该出现在逐表阈值里")

    def test_problems_are_logged_not_swallowed(self):
        lines = []
        registry.apply_overrides(
            Settings(), entry(**{registry.COL_FLOP_HOURS: 0}), log=lines.append)
        self.assertTrue(any("已忽略" in line for line in lines))


class TestTierShiftPreview(unittest.TestCase):
    """改阈值唯一的真实副作用：热度档是棘轮（只升不降），改完不回溯。"""

    def _new(self, **thresholds):
        return registry.apply_overrides(Settings(), entry(**thresholds))

    def test_lowering_a_threshold_promotes_rows(self):
        base = Settings()
        recs = [record("r1", comment_count=35, published_hours_ago=100),
                record("r2", comment_count=10, published_hours_ago=100)]
        shift = summary.preview_tier_shift(
            recs, base, self._new(**{registry.COL_TIER_HOT: 30}), now=NOW)
        self.assertEqual(shift.up, 1)
        self.assertIn("升档", shift.describe())

    def test_raising_a_threshold_is_reported_as_blocked_not_as_a_demotion(self):
        """棘轮：算出来更低也不会真降。摆出来，别让人以为改完就回退了。"""
        base = Settings()
        recs = [record("r1", comment_count=60, published_hours_ago=100)]
        shift = summary.preview_tier_shift(
            recs, base, self._new(**{registry.COL_TIER_HOT: 80}), now=NOW)
        self.assertEqual(shift.up, 0)
        self.assertEqual(shift.down_blocked, 1)
        self.assertIn("不会降", shift.describe())

    def test_no_change_says_so_plainly(self):
        base = Settings()
        recs = [record("r1", comment_count=5, published_hours_ago=100)]
        shift = summary.preview_tier_shift(
            recs, base, self._new(**{registry.COL_TIER_SUPER_HOT: 200}), now=NOW)
        self.assertIn("没有行的档位会变", shift.describe())

    def test_rows_without_a_comment_count_are_skipped(self):
        """没量过的行谈不上档位会不会变。"""
        base = Settings()
        shift = summary.preview_tier_shift(
            [record("r1", published_hours_ago=100)], base,
            self._new(**{registry.COL_TIER_HOT: 1}), now=NOW)
        self.assertEqual(shift.changed, 0)

    def test_flop_hours_affects_young_rows(self):
        base = Settings()
        recs = [record("r1", comment_count=3, published_hours_ago=60)]
        # 默认 48 小时 → 已经是「无水花」；改成 72 → 退回「观察中」（降，被棘轮挡）
        shift = summary.preview_tier_shift(
            recs, base, self._new(**{registry.COL_FLOP_HOURS: 72}), now=NOW)
        self.assertEqual(shift.down_blocked, 1)

    def test_examples_are_capped(self):
        base = Settings()
        recs = [record(f"r{i}", comment_count=35, published_hours_ago=100)
                for i in range(20)]
        shift = summary.preview_tier_shift(
            recs, base, self._new(**{registry.COL_TIER_HOT: 30}), now=NOW,
            max_examples=3)
        self.assertEqual(shift.up, 20)
        self.assertEqual(len(shift.examples), 3)

    def test_it_costs_nothing(self):
        """用表里已经有的评论数就能算，一个请求都不发。"""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(summary.preview_tier_shift))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr,
                                 ("search", "refresh", "fetch", "post", "get"))


class TestArchivedTodosAreSeparated(unittest.TestCase):
    """「排队刷新」会绕过归档线，而面板恰好把跨表的老旧异常行摆在一屏、
    天然鼓励全选。归档的行不能混进主列表。"""

    def _overview(self):
        settings = Settings()
        snap = summary.build_snapshot(
            label="A", app_token="t", table_id="tb", settings=settings, now=NOW,
            records=[
                record("live", tags=["风控中"], published_hours_ago=10),
                record("old", tags=["风控中"], published_hours_ago=24 * 400,
                       checked_hours_ago=24 * 300),
            ])
        return summary.Overview(projects=[snap], generated_at=NOW)

    def test_the_main_list_excludes_archived_rows(self):
        overview = self._overview()
        self.assertEqual([t.record_id for t in overview.todos()], ["live"])

    def test_they_are_still_reachable_in_their_own_list(self):
        overview = self._overview()
        self.assertEqual([t.record_id for t in overview.archived_todos()], ["old"])

    def test_they_can_be_asked_for_explicitly(self):
        overview = self._overview()
        self.assertEqual(len(overview.todos(include_archived=True)), 2)

    def test_the_page_folds_them_away_with_the_reason(self):
        from xhsearch import panel, panel_view
        cfg = panel.PanelConfig(password="a" * 12, secret=b"s")
        page = panel_view.overview_page(
            overview=self._overview(), error="", fetched_at=NOW.timestamp(),
            config=cfg, csrf="t")
        self.assertIn("已归档的老帖", page)
        self.assertIn("绕过归档线", page)
        self.assertIn("取消巡查", page)


if __name__ == "__main__":
    unittest.main()
