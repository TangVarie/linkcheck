"""跨表聚合层的单测。全部离线，不需要 API Key，不发任何请求。

这一层的每个数字都会被运营拿去做决定，所以口径必须钉死：
「到期」和「卡住了」不是一回事，没填关键词的行必须被数出来，
取消巡查的行一个数字都不该进。
"""

import unittest
from datetime import datetime, timedelta, timezone

from xhsearch import links, runner, summary
from xhsearch.config import Settings

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
XHS = "https://www.xiaohongshu.com/explore/65a1b2c3d4e5f60718293a4b"


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def record(record_id="rec1", *, settings=None, link=XHS, published_hours_ago=10,
           checked_hours_ago=1, tags=(), refresh_status="", negative_status="",
           pin_status="", queued=False, seed=None, negative=None,
           comment_count=None, diagnosis="", digest="", negative_digest="",
           extra=None):
    """造一条飞书 record。只填给到的列——飞书对空单元格根本不返回键，
    测试里也照这个来，否则测不到「这一格没值」的路径。"""
    f = (settings or Settings()).fields
    cells = {}
    if link:
        cells[f.link] = link
    if published_hours_ago is not None:
        cells[f.publish_time] = ms(NOW - timedelta(hours=published_hours_ago))
    if checked_hours_ago is not None:
        cells[f.last_updated] = ms(NOW - timedelta(hours=checked_hours_ago))
    if tags:
        cells[f.traffic_status] = list(tags)
    if refresh_status:
        cells[f.refresh_status] = refresh_status
    if negative_status:
        cells[f.negative_status] = negative_status
    if pin_status:
        cells[f.pinned_status] = pin_status
    if queued:
        cells[f.queued] = True
    if seed:
        cells[f.seed_keywords] = list(seed)
    if negative:
        cells[f.negative_keywords] = list(negative)
    if comment_count is not None:
        cells[f.comment_count] = comment_count
    if diagnosis:
        cells[f.failure_reason] = diagnosis
    if digest:
        cells[f.comment_digest] = digest
    if negative_digest:
        cells[f.negative_digest] = negative_digest
    # 机器不认识的业务列（「笔记内容」之类）。面板要读它，判定链路不碰它。
    cells.update(extra or {})
    return {"record_id": record_id, "fields": cells}


def snapshot(records, settings=None, **kwargs):
    settings = settings or Settings()
    return summary.build_snapshot(
        label="项目A", app_token="bascnAAA", table_id="tblBBB",
        records=records, settings=settings, now=NOW,
        api_keys={"tikhub": "k"}, **kwargs)


class TestCounts(unittest.TestCase):
    def test_empty_table_is_all_zero_and_healthy(self):
        snap = snapshot([])
        self.assertEqual(snap.total_rows, 0)
        self.assertEqual(snap.due_rows, 0)
        self.assertEqual(snap.due_yuan, 0.0)
        self.assertEqual(snap.todos, [])
        self.assertTrue(snap.healthy)

    def test_refresh_status_and_traffic_tags_are_tallied(self):
        snap = snapshot([
            record("r1", refresh_status="正常", tags=["爆贴"]),
            record("r2", refresh_status="正常", tags=["无水花"]),
            record("r3", refresh_status="刷新失败", tags=["风控中", "大爆"]),
        ])
        self.assertEqual(snap.total_rows, 3)
        self.assertEqual(snap.refresh_status_counts, {"正常": 2, "刷新失败": 1})
        self.assertEqual(snap.traffic_tag_counts,
                         {"爆贴": 1, "无水花": 1, "风控中": 1, "大爆": 1})

    def test_a_row_can_carry_several_tags_and_each_is_counted(self):
        snap = snapshot([record("r1", tags=["风控中", "疑似限流", "大爆"])])
        self.assertEqual(snap.traffic_tag_counts,
                         {"风控中": 1, "疑似限流": 1, "大爆": 1})

    def test_negative_and_pin_lost_counted_from_their_own_columns(self):
        settings = Settings()
        snap = snapshot([
            record("r1", negative_status=settings.negative_status.found),
            record("r2", negative_status=settings.negative_status.clean),
            record("r3", pin_status=settings.pin_status.pinned_lost),
            record("r4", pin_status=settings.pin_status.pinned_ok),
        ], settings)
        self.assertEqual(snap.negative_rows, 1)
        self.assertEqual(snap.pin_lost_rows, 1)

    def test_queued_rows_counted(self):
        snap = snapshot([record("r1", queued=True), record("r2")])
        self.assertEqual(snap.queued_rows, 1)


class TestFreshness(unittest.TestCase):
    def test_due_matches_row_is_due_exactly(self):
        """面板报的「到期」必须和 sweep 真正会刷的是同一批，
        否则「预计花费」永远对不上账。"""
        settings = Settings()
        records = [
            record("fresh", published_hours_ago=10, checked_hours_ago=1),
            record("due", published_hours_ago=10, checked_hours_ago=20),
        ]
        snap = snapshot(records, settings)
        expected = [r for r in (runner.row_from_record(rec, settings) for rec in records)
                    if r.is_due(settings, NOW)]
        self.assertEqual(snap.due_rows, len(expected))
        self.assertEqual(snap.due_rows, 1)

    def test_due_is_not_the_same_as_stale(self):
        """刚过期 8 小时的行是「到期」，但不是「卡住了」——
        cron 每 5 分钟一轮，任何时刻都有一批刚到期的行。"""
        snap = snapshot([record("r1", published_hours_ago=10, checked_hours_ago=9)])
        self.assertEqual(snap.due_rows, 1)
        self.assertEqual(snap.stale_rows, 0)

    def test_stale_needs_twice_the_interval(self):
        # 0-2 天档间隔 8 小时，两倍 = 16 小时。
        snap = snapshot([record("r1", published_hours_ago=30, checked_hours_ago=20)])
        self.assertEqual(snap.stale_rows, 1)
        self.assertIn("卡住了", snap.todos[0].reasons)

    def test_never_checked_managed_row_is_stale(self):
        snap = snapshot([record("r1", published_hours_ago=10, checked_hours_ago=None)])
        self.assertEqual(snap.never_checked_rows, 1)
        self.assertEqual(snap.stale_rows, 1)

    def test_archived_rows_are_never_stale(self):
        """归档的行本来就不再自动刷，把它报成「卡住了」是纯噪声。"""
        snap = snapshot([record("r1", published_hours_ago=24 * 400,
                                checked_hours_ago=24 * 300)])
        self.assertEqual(snap.archived_rows, 1)
        self.assertEqual(snap.stale_rows, 0)
        self.assertEqual(snap.due_rows, 0)

    def test_missing_publish_time_is_not_reported_as_stale(self):
        """发布时间读不出来是「那一格有问题」，诊断信息里已经在报了，
        不该在面板上占第二个位置。"""
        snap = snapshot([record("r1", published_hours_ago=None,
                                checked_hours_ago=24 * 30)])
        self.assertEqual(snap.stale_rows, 0)

    def test_oldest_checked_is_the_minimum(self):
        snap = snapshot([
            record("r1", checked_hours_ago=1),
            record("r2", checked_hours_ago=50),
            record("r3", checked_hours_ago=10),
        ])
        self.assertEqual(snap.oldest_checked_ms, ms(NOW - timedelta(hours=50)))


class TestKeywordCoverage(unittest.TestCase):
    def test_counts_and_lists_the_rows_without_keywords(self):
        snap = snapshot([
            record("r1", seed=["西地那非"], negative=["过敏"]),
            record("r2", seed=["西地那非"]),
            record("r3"),
        ])
        self.assertEqual(snap.seed_keyword_rows, 2)
        self.assertEqual(snap.negative_keyword_rows, 1)
        self.assertEqual(snap.rows_without_seed_keyword, ["r3"])
        self.assertEqual(snap.rows_without_negative_keyword, ["r2", "r3"])

    def test_missing_keywords_is_not_a_todo(self):
        """没填词不是「出问题了」，是「还没配」。它属于覆盖度那一行，
        混进待办会把真正的风控行淹掉。"""
        snap = snapshot([record("r1")])
        self.assertEqual(snap.todos, [])


class TestTodos(unittest.TestCase):
    def test_risk_tag_becomes_a_todo(self):
        snap = snapshot([record("r1", tags=["风控中"])])
        self.assertEqual(len(snap.todos), 1)
        self.assertEqual(snap.todos[0].reasons, ["风控中"])

    def test_todo_carries_the_deep_link_to_that_row(self):
        snap = snapshot([record("recXYZ", tags=["风控中"])],
                        feishu_base="https://acme.feishu.cn")
        self.assertEqual(
            snap.todos[0].record_url,
            "https://acme.feishu.cn/base/bascnAAA?table=tblBBB&record=recXYZ")

    def test_todo_carries_diagnosis_verbatim(self):
        snap = snapshot([record("r1", refresh_status="刷新失败",
                                diagnosis="transport: 连接超时")])
        self.assertEqual(snap.todos[0].diagnosis, "transport: 连接超时")

    def test_normal_row_is_not_a_todo(self):
        snap = snapshot([record("r1", refresh_status="正常", tags=["爆贴"])])
        self.assertEqual(snap.todos, [])

    def test_reasons_are_deduped(self):
        snap = snapshot([record("r1", tags=["风控中"], refresh_status="已失效")])
        self.assertEqual(snap.todos[0].reasons, ["风控中", "已失效"])

    def test_todos_sorted_by_severity(self):
        settings = Settings()
        snap = snapshot([
            record("pin", pin_status=settings.pin_status.pinned_lost),
            record("neg", negative_status=settings.negative_status.found),
            record("risk", tags=["风控中"]),
        ], settings)
        self.assertEqual([t.record_id for t in snap.todos], ["risk", "neg", "pin"])

    def test_digest_is_withheld_by_default(self):
        """评论正文、昵称、IP 属地是别人的个人信息，
        默认不因为「顺手」就上一个公网页面。"""
        snap = snapshot([record("r1", tags=["风控中"], digest="张三：这个真好用",
                                negative_digest="李四：踩雷")])
        self.assertEqual(snap.todos[0].digest, "")
        self.assertEqual(snap.todos[0].negative_digest, "")

    def test_digest_shown_when_explicitly_enabled(self):
        snap = snapshot([record("r1", tags=["风控中"], digest="张三：这个真好用")],
                        show_digest=True)
        self.assertEqual(snap.todos[0].digest, "张三：这个真好用")

    def test_todos_are_capped(self):
        snap = snapshot([record(f"r{i}", tags=["风控中"]) for i in range(50)],
                        max_todos=10)
        self.assertEqual(len(snap.todos), 10)


class TestPanelFields(unittest.TestCase):
    def test_digest_columns_only_requested_when_shown(self):
        settings = Settings()
        without = summary.panel_fields(settings)
        self.assertNotIn(settings.fields.comment_digest, without)
        with_digest = summary.panel_fields(settings, show_digest=True)
        self.assertIn(settings.fields.comment_digest, with_digest)
        self.assertIn(settings.fields.negative_digest, with_digest)

    def test_includes_the_columns_the_verdict_path_does_not_need(self):
        """面板要靠这几列回答「这行怎么了」，而 must_read() 里没有它们。"""
        settings = Settings()
        fields = summary.panel_fields(settings)
        for column in (settings.fields.refresh_status, settings.fields.failure_reason,
                       settings.fields.negative_status):
            self.assertIn(column, fields)
            self.assertNotIn(column, settings.fields.must_read())

    def test_no_duplicates(self):
        fields = summary.panel_fields(Settings(), show_digest=True)
        self.assertEqual(len(fields), len(set(fields)))


class TestTextWithoutUrls(unittest.TestCase):
    """「哪一条」的兜底：分享文案抠掉链接之后剩的字。"""

    def test_share_text_keeps_the_title_and_loses_the_link(self):
        cell = ("77 露营装备清单 - 小红书 😆 abcDEF😆 " + XHS +
                " 😆 复制本条信息，打开【小红书】App查看精彩内容！")
        left = links.text_without_urls(cell)
        self.assertIn("露营装备清单", left)
        self.assertNotIn("xiaohongshu.com", left)
        self.assertNotIn("http", left)

    def test_a_bare_link_leaves_nothing(self):
        """整格只有一条链接 → 空串，让 row_label 落到笔记 ID。

        直接把原文前十几个字拿去显示的话，每一行都是
        `https://www.xiaohongsh…`，一模一样，等于这一栏没显示。
        """
        self.assertEqual(links.text_without_urls(XHS), "")

    def test_a_scheme_less_short_link_takes_its_path_with_it(self):
        """只剥域名会在文本里留下一截 `/a/AbC`，比留着整条链接还难认。"""
        self.assertEqual(links.text_without_urls("xhslink.com/a/AbC 露营装备"),
                         "露营装备")

    def test_chinese_glued_to_the_link_survives(self):
        """`_URL_RE` 的 CJK 右边界：不排 CJK 会把「很火」一起吞掉。"""
        self.assertEqual(links.text_without_urls("看这条https://v.douyin.com/xxx很火"),
                         "看这条 很火")


class TestPickLabelColumn(unittest.TestCase):
    def test_candidates_are_tried_in_order(self):
        self.assertEqual(summary.pick_label_column({"文案", "笔记内容", "标题"}),
                         "笔记内容")
        self.assertEqual(summary.pick_label_column({"文案", "标题"}), "文案")

    def test_no_candidate_matches_gives_empty(self):
        self.assertEqual(summary.pick_label_column({"反馈链接", "平台"}), "")

    def test_explicit_config_beats_the_candidates(self):
        self.assertEqual(
            summary.pick_label_column({"笔记内容", "我自己的文案列"}, "我自己的文案列"),
            "我自己的文案列")

    def test_a_configured_column_that_is_not_there_is_not_silently_swapped(self):
        """配错了就该空着、让面板报出来。悄悄换一列 = 配置没生效却没人知道。"""
        self.assertEqual(summary.pick_label_column({"笔记内容"}, "打错的列名"), "")


class TestRowLabel(unittest.TestCase):
    """待办行上「哪一条」的取值。运营拿它去飞书表里 Ctrl+F 定位，
    所以每一档兜底都必须是**这一行独有**的字，不能各行长一样。"""

    def _label(self, **kwargs):
        column = kwargs.pop("label_column", "笔记内容")
        snap = snapshot([record("r1", tags=["风控中"], **kwargs)],
                        label_column=column)
        return snap.todos[0].label

    def test_the_content_column_wins(self):
        label = self._label(extra={"笔记内容": "露营装备清单｜新手别踩这几个坑"})
        self.assertEqual(label, "露营装备清单｜新手别踩这几个坑")

    def test_an_empty_content_cell_falls_back_to_the_share_text(self):
        """列建了但那一格没填 —— 空字符串不是「有值」。"""
        label = self._label(extra={"笔记内容": ""},
                            link="77 露营装备清单 - 小红书 " + XHS)
        self.assertEqual(label, "77 露营装备清单 - 小红书")

    def test_no_content_column_falls_back_to_the_share_text(self):
        label = self._label(label_column="",
                            link="77 露营装备清单 - 小红书 " + XHS)
        self.assertEqual(label, "77 露营装备清单 - 小红书")

    def test_a_bare_link_falls_back_to_the_note_id(self):
        """网址前缀每行都一样，认不出来；笔记 ID 在表里搜得到。"""
        self.assertEqual(self._label(label_column="", link=XHS),
                         "65a1b2c3d4e5f60718293a4b")

    def test_nothing_usable_is_an_empty_label_not_a_crash(self):
        self.assertEqual(self._label(label_column="", link="   "), "")

    def test_rich_text_content_cells_are_read_as_text(self):
        """飞书文本列读出来可能是富文本分段数组。"""
        label = self._label(extra={"笔记内容": [{"type": "text", "text": "露营装备清单"}]})
        self.assertEqual(label, "露营装备清单")


class TestOverview(unittest.TestCase):
    def test_totals_add_up_across_projects(self):
        # r1 距上次检查 20 小时、0-2 天档间隔 8 小时 → 超两倍，算「卡住了」。
        a = snapshot([record("r1", checked_hours_ago=20), record("r2", queued=True)])
        b = summary.build_snapshot(
            label="项目B", app_token="bascnCCC", table_id="tblDDD",
            records=[record("r3", tags=["风控中"])], settings=Settings(),
            now=NOW, api_keys={"tikhub": "k"})
        overview = summary.Overview(projects=[a, b])
        self.assertEqual(overview.total_rows, 3)
        self.assertEqual(overview.queued_rows, 1)
        self.assertEqual(overview.stale_rows, 1)
        # 跨表拉平：A 的「卡住了」+ B 的「风控中」，风控排前面。
        self.assertEqual([t.record_id for t in overview.todos()], ["r3", "r1"])

    def test_todos_are_flattened_and_sorted_across_projects(self):
        settings = Settings()
        a = summary.build_snapshot(
            label="A", app_token="t1", table_id="tb1", settings=settings, now=NOW,
            records=[record("pin", pin_status=settings.pin_status.pinned_lost)])
        b = summary.build_snapshot(
            label="B", app_token="t2", table_id="tb2", settings=settings, now=NOW,
            records=[record("risk", tags=["风控中"])])
        overview = summary.Overview(projects=[a, b])
        self.assertEqual([t.record_id for t in overview.todos()], ["risk", "pin"])

    def test_a_broken_table_does_not_blank_the_others(self):
        good = snapshot([record("r1")])
        bad = summary.ProjectSnapshot(label="坏表", app_token="x", table_id="y",
                                      error="读不到字段元数据")
        overview = summary.Overview(projects=[good, bad])
        self.assertEqual(overview.total_rows, 1)
        self.assertEqual([p.label for p in overview.unhealthy_projects], ["坏表"])


if __name__ == "__main__":
    unittest.main()


class TestQueuedMarker(unittest.TestCase):
    """待办行要带上「排队刷新」此刻勾没勾——这是面板上唯一能分开
    「同样的错」和「还没刷回来」的信号。"""

    def test_a_queued_row_carries_the_flag(self):
        snap = snapshot([record(refresh_status="刷新失败", queued=True)])
        self.assertTrue(snap.todos[0].queued)

    def test_an_unqueued_row_does_not(self):
        snap = snapshot([record(refresh_status="刷新失败")])
        self.assertFalse(snap.todos[0].queued)
