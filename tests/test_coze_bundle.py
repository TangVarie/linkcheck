"""扣子打包产物的冒烟测试。

打包脚本要剥掉包内相对 import 和重复的 import，很容易多剥一个——语法照样通过，
但一跑就 NameError。而扣子那边没有本地调试，报错只能在网页运行记录里看，
排查成本极高。所以这里把整包 exec 起来，真的调几个函数。
"""

import ast
import sys
import types
import unittest

from tools.build_coze_node import build


class _FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class _FakeRequestsAsync(types.ModuleType):
    """扣子内置 requests_async 的占位实现，本地没有这个包。"""

    async def post(self, *args, **kwargs):
        return _FakeResponse()

    async def get(self, *args, **kwargs):
        return _FakeResponse()


class TestCozeBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = build()
        sys.modules.setdefault("requests_async", _FakeRequestsAsync("requests_async"))
        cls.namespace: dict = {}
        exec(compile(cls.source, "coze_node.py", "exec"), cls.namespace)

    def test_parses(self):
        ast.parse(self.source)

    def test_no_relative_imports_survive(self):
        for line in self.source.splitlines():
            self.assertFalse(line.strip().startswith("from ."), f"漏剥了相对 import：{line}")

    def test_core_symbols_present(self):
        for name in [
            "Settings", "Row", "parse", "plan_calls", "read_comment_page", "merge_detail",
            "decide", "decide_pin", "Pin", "gone_verdict", "suspect_verdict",
            "merge", "format_digest", "format_pinned", "parse_response", "build_body",
            "headers", "endpoint", "Failure", "FATAL", "Err", "Ok", "main",
            # 双通道：这几个漏剥一个，扣子那边会直接 NameError，
            # 而且只能在网页运行记录里看到，排查成本极高。
            "get_provider", "usable_order", "FAILOVER_KINDS", "Channels",
            "channel_call", "TIKHUB_PATHS", "REGISTRY",
        ]:
            self.assertIn(name, self.namespace, f"打包产物里缺 {name}")

    def test_both_channels_are_wired_inside_bundle(self):
        """打包产物里两家都要能组包——这一条是 providers 层被剥坏的第一现场。"""
        ns = self.namespace
        tik = ns["get_provider"]("tikhub").build(
            "k", "xhs", "comments", {"note_id": "n" * 24, "sort": "default"})
        self.assertEqual(tik.method, "GET")
        self.assertIn("api.tikhub.dev", tik.url)
        self.assertIn("Mozilla/", tik.headers["User-Agent"])

        sdx = ns["get_provider"]("socialdatax").build(
            "k", "xhs", "comments", {"note_id": "n" * 24, "sort": "default"})
        self.assertEqual(sdx.method, "POST")

    def test_tikhub_normalisation_works_inside_bundle(self):
        """置顶标记藏在 show_tags_v2 里。归一化被剥坏的话这里立刻炸。"""
        import json as _json

        ns = self.namespace
        body = _json.dumps({"code": 200, "data": {"code": 0, "success": True, "data": {
            "comments": [{"content": "戳主页", "like_count": 9, "ip_location": "Shanghai",
                          "show_tags_v2": [{"type": "user_top"}],
                          "user": {"nickname": "官号"}}],
            "comment_count": 88, "comment_count_l1": 80,
            "all_sort_strategies": [{"type": "default"}], "user_id": "u1",
        }}}, ensure_ascii=False)
        res = ns["get_provider"]("tikhub").parse(
            "xhs", "comments", 200, "application/json", body, "rid", "k")
        snapshot = ns["read_comment_page"]("xhs", res.data)
        self.assertEqual(snapshot.comment_count, 88)
        self.assertIsNotNone(snapshot.pinned)
        self.assertEqual(snapshot.comments[0].ip_location, "上海")

    def test_failover_never_fires_on_gone(self):
        ns = self.namespace
        self.assertNotIn(ns["Failure"].GONE, ns["FAILOVER_KINDS"])

    def test_link_parsing_works_inside_bundle(self):
        parsed = self.namespace["parse"]("https://www.xiaohongshu.com/explore/" + "b" * 24)
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "b" * 24)

    def test_full_decision_path_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        snapshot = ns["read_comment_page"]("xhs", {
            "items": [{"content": "戳主页领券", "is_pinned": True, "is_author_comment": True,
                       "like_count": 9, "ip_location": "上海", "author": {"name": "官号"}}],
            "comment_count": 88,
            "points": {"cost": 10, "balance": 500},
        })
        verdict = ns["decide"](snapshot, settings, previous_comment_count=10,
                               age_hours=20, seed_keywords=["戳主页领券"])
        self.assertIn("爆贴", verdict.tags)          # 88 条落在 50–99 档
        self.assertNotIn("大爆", verdict.tags)       # 档位互斥
        self.assertIs(verdict.pin, ns["Pin"].PINNED)  # 自家帖子：有置顶即成功
        self.assertEqual(verdict.seed_hit.keyword, "戳主页领券")   # 蓝词命中
        self.assertIn("置顶", ns["format_digest"](snapshot, settings.digest))

    def test_heat_tiers_match_the_agreed_thresholds(self):
        """≥20 评估中，≥50 爆贴，≥100 大爆。改动阈值会静默改变全表结论，
        所以在打包产物里也钉一遍。"""
        ns = self.namespace
        settings = ns["Settings"]()
        for count, expected in [(19, None), (20, "评估中"), (50, "爆贴"), (100, "大爆")]:
            snap = ns["read_comment_page"]("xhs", {"items": [], "comment_count": count})
            verdict = ns["decide"](snap, settings, previous_comment_count=None, age_hours=10)
            heat = {t for t in verdict.tags if settings.tags.rank(t) >= 0}
            self.assertEqual(heat, {expected} if expected else set(), f"评论数 {count}")

    def test_tag_merge_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        merged = ns["merge"](["已复盘", "风控中"], {"爆贴"}, settings.tags.namespace())
        self.assertIn("已复盘", merged.final)
        self.assertIn("爆贴", merged.final)
        self.assertNotIn("风控中", merged.final)

    def test_protocol_parsing_works_inside_bundle(self):
        ns = self.namespace
        result = ns["parse_response"](
            401, "application/json", '{"code":1401,"message":"API Key 无效或已失效。"}')
        self.assertIs(result.kind, ns["Failure"].AUTH)
        self.assertIn(result.kind, ns["FATAL"])

    def test_http_200_business_error_is_caught_inside_bundle(self):
        """业务错误走 HTTP 200 + code。这条在扣子里错了，
        每一个「笔记已删除」都会被当成成功。"""
        ns = self.namespace
        result = ns["parse_response"](200, "application/json",
                                      '{"code":1008,"message":"当前作品已删除。"}')
        self.assertIs(result.kind, ns["Failure"].GONE)
        self.assertTrue(result.definitive)

    def test_rest_endpoints_inside_bundle(self):
        ns = self.namespace
        self.assertTrue(ns["endpoint"]("xhs", "comments").endswith("/xhs/note/comment/list"))

    def test_call_planning_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        row = ns["Row"](record_id="r", link_cell="https://www.douyin.com/video/7123456789012345678")
        plan = ns["plan_calls"](row, settings)
        self.assertEqual([c.purpose for c in plan], ["comments", "detail"])

    def test_soft_deadline_is_under_coze_hard_limit(self):
        # 扣子代码节点硬上限 60 秒，超时会把已经算完的几十行结果一起丢掉
        self.assertLess(self.namespace["SOFT_DEADLINE"], 60)


class TestCozeOrchestration(unittest.TestCase):
    """FOOTER 里的异步编排层（读表 / 通道调度 / 单行处理）。

    这一段是扣子上真正执行的编排代码，之前从未被任何测试跑过——
    「读表不翻页导致 40 行以外永远刷不到」这种缺陷就是在这里潜伏的。
    """

    @classmethod
    def setUpClass(cls):
        cls.namespace = TestCozeBundle.namespace if TestCozeBundle.__dict__.get(
            "namespace") else None
        if not cls.namespace:
            from tools.build_coze_node import build
            cls.namespace = {}
            sys.modules.setdefault("requests_async", _FakeRequestsAsync("requests_async"))
            exec(compile(build(), "coze_node.py", "exec"), cls.namespace)
        cls.mod = sys.modules["requests_async"]

    def tearDown(self):
        for name in ("post", "get"):
            if name in self.mod.__dict__:
                delattr(self.mod, name)

    def test_feishu_search_paginates_to_the_end(self):
        """只取一页的话，第 41 条以后的记录永远轮不到刷新且无任何报错——
        「600 行按每轮 40 行排干」全靠这里拿到全量。"""
        import asyncio
        import json as _json

        ns = self.namespace
        pages = [
            {"code": 0, "data": {"items": [{"record_id": "r1"}],
                                 "has_more": True, "page_token": "tok+1"}},
            {"code": 0, "data": {"items": [{"record_id": "r2"}], "has_more": False}},
        ]
        calls: list = []

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            calls.append(url)
            return _FakeResponse(text=_json.dumps(pages[len(calls) - 1]))

        self.mod.post = fake_post
        records, complete = asyncio.run(
            ns["feishu_search"]("tok", "app", "tbl", ["链接"], None,
                                sort_field="最后更新时间"))
        self.assertEqual([r["record_id"] for r in records], ["r1", "r2"])
        self.assertTrue(complete)
        self.assertEqual(len(calls), 2)
        self.assertIn("page_token=tok%2B1", calls[1])   # 不透明 token 必须编码

    def test_feishu_search_scans_stalest_first(self):
        """部分扫描不饿死任何行的关键：按「最后更新时间」升序扫，
        被刷过的行自动沉到队尾，游标藏在数据自己身上。"""
        import asyncio
        import json as _json

        ns = self.namespace
        bodies: list = []

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            bodies.append(json)
            return _FakeResponse(text=_json.dumps(
                {"code": 0, "data": {"items": [], "has_more": False}}))

        self.mod.post = fake_post
        asyncio.run(ns["feishu_search"]("tok", "app", "tbl", ["链接"], None,
                                        sort_field="最后更新时间"))
        self.assertEqual(bodies[0]["sort"],
                         [{"field_name": "最后更新时间", "desc": False}])

    def test_feishu_search_partial_scan_is_flagged(self):
        import asyncio
        import time as _time

        ns = self.namespace

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            raise AssertionError("截止已过，不该再发请求")

        self.mod.post = fake_post
        records, complete = asyncio.run(
            ns["feishu_search"]("tok", "app", "tbl", ["链接"], None,
                                deadline=_time.monotonic() - 1))
        self.assertEqual(records, [])
        self.assertFalse(complete)      # main 靠这个标志提示「读表未扫完」

    def test_channel_call_routes_douyin_url_to_socialdatax(self):
        """抖音短链（提不出数字 ID）：TikHub 端点只收 aweme_id，
        必须直接让给吃链接的 SocialDataX，一次 GET 都不该发。"""
        import asyncio
        import json as _json
        import time as _time

        ns = self.namespace
        posts: list = []

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            posts.append(url)
            return _FakeResponse(text=_json.dumps(
                {"items": [], "comment_count": 3, "points": {}}))

        async def fake_get(url, headers=None, timeout=None):
            raise AssertionError("TikHub 不吃链接，不该打它")

        self.mod.post = fake_post
        self.mod.get = fake_get
        call = ns["ToolCall"]("douyin", "comments", {"url": "https://v.douyin.com/x"})

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["channel_call"](
                {"tikhub": "t", "socialdatax": "s"}, ns["Settings"](), call,
                sem, _time.monotonic() + 60, set())

        result, billed = asyncio.run(run())
        self.assertIsInstance(result, ns["Ok"])
        self.assertEqual(billed, {"socialdatax": 1})
        self.assertEqual(len(posts), 1)

    def test_channel_call_fails_cleanly_when_only_tikhub_configured(self):
        import asyncio
        import time as _time

        ns = self.namespace
        call = ns["ToolCall"]("douyin", "comments", {"url": "https://v.douyin.com/x"})

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["channel_call"](
                {"tikhub": "t"}, ns["Settings"](), call,
                sem, _time.monotonic() + 60, set())

        result, billed = asyncio.run(run())
        self.assertIsInstance(result, ns["Err"])
        self.assertEqual(result.code, "unsupported_link")
        self.assertNotIn(result.kind, ns["FATAL"])       # 行级问题，不能中止整批
        self.assertEqual(billed, {})

    def test_process_past_deadline_writes_nothing(self):
        """软截止后轮到的行：完全不写回。写「刷新失败」会顶掉最后更新时间、
        清掉排队勾、污染定罪计数——断点续跑就毁了。"""
        import asyncio
        import time as _time
        from datetime import datetime, timezone

        ns = self.namespace
        row = ns["Row"](record_id="r",
                        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24)

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["_process"](
                row, {"socialdatax": "s"}, ns["Settings"](),
                datetime.now(timezone.utc), sem, _time.monotonic() - 1, set(),
                None, None, wanted=False)

        fields, fen, balance, status, tally = asyncio.run(run())
        self.assertIsNone(fields)
        self.assertEqual(status, "")
        self.assertEqual(tally, {})

    def test_process_generic_failure_leaves_strike_counter_alone(self):
        """网络失败对「内容在不在」没有证据力，不能给两击定罪的计数器 +1。"""
        import asyncio
        import time as _time
        from datetime import datetime, timezone

        ns = self.namespace

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            raise RuntimeError("boom")

        async def fake_get(url, headers=None, timeout=None):
            raise RuntimeError("boom")

        self.mod.post = fake_post
        self.mod.get = fake_get
        row = ns["Row"](record_id="r",
                        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
                        consecutive_failures=2)

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["_process"](
                row, {"socialdatax": "s"}, ns["Settings"](),
                datetime.now(timezone.utc), sem, _time.monotonic() + 60, set(),
                None, None, wanted=True)

        fields, fen, balance, status, tally = asyncio.run(run())
        self.assertEqual(status, "刷新失败")
        settings = ns["Settings"]()
        self.assertNotIn(settings.fields.consecutive_failures, fields)

    def test_process_fatal_channel_outage_is_marked_not_silent(self):
        """Key 失效/余额耗尽把通道全打死：不能返回和普通顺延一样的空状态——
        那会让 main 报 ok:True，半夜的故障没人知道。"""
        import asyncio
        import json as _json
        import time as _time
        from datetime import datetime, timezone

        ns = self.namespace

        async def fake_post(url, headers=None, json=None, timeout=None, data=None):
            return _FakeResponse(status_code=401, text=_json.dumps(
                {"code": 1401, "message": "API Key 无效或已失效。"}))

        self.mod.post = fake_post
        row = ns["Row"](record_id="r",
                        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24)

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["_process"](
                row, {"socialdatax": "s"}, ns["Settings"](),
                datetime.now(timezone.utc), sem, _time.monotonic() + 60, set(),
                None, None, wanted=True)

        fields, fen, balance, status, tally = asyncio.run(run())
        self.assertIsNone(fields)                 # 不写回（不是这一行的错）
        self.assertEqual(status, "fatal")         # 但要向 main 报故障

    def test_process_unsupported_link_is_skipped_not_failed(self):
        """只配 TikHub 的抖音短链行：按「跳过」处理，不进熔断分母。"""
        import asyncio
        import time as _time
        from datetime import datetime, timezone

        ns = self.namespace
        row = ns["Row"](record_id="r",
                        link_cell="7.86 复制打开抖音 https://v.douyin.com/iRxYzAb/",
                        publish_time_ms=None)

        async def run():
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(1)
            return await ns["_process"](
                row, {"tikhub": "t"}, ns["Settings"](),
                datetime.now(timezone.utc), sem, _time.monotonic() + 60, set(),
                None, None, wanted=True)

        fields, fen, balance, status, tally = asyncio.run(run())
        self.assertEqual(status, "跳过")
        settings = ns["Settings"]()
        self.assertIn("不支持这种链接形态",
                      fields[settings.fields.failure_reason])
        self.assertEqual(fen, 0.0)                # 一分钱没花

    def test_render_filters_unknown_options_and_keeps_old_tier(self):
        """「大爆」没建选项：既不能写进去（整批回滚），也不能顺手摘掉「爆贴」。"""
        from datetime import datetime, timezone

        ns = self.namespace
        settings = ns["Settings"]()
        verdict = ns["Verdict"](tags={"大爆"}, notes=[], pin=ns["Pin"].UNSUPPORTED)
        row = ns["Row"](record_id="r", link_cell="x", current_tags=["爆贴", "已复盘"])
        fields = ns["_render"](
            row, verdict, None, settings, datetime.now(timezone.utc), "正常",
            ["评估中", "爆贴", "风控中", "已失效"], None)
        # 保留旧档位后合并结果与现值相同 → 不写这一列（保持原样的最强形式）
        self.assertNotIn(settings.fields.traffic_status, fields)
        self.assertIn("还没建选项", fields[settings.fields.failure_reason])


if __name__ == "__main__":
    unittest.main()
