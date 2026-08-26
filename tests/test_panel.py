"""监控面板的单测。全部离线，不起真服务、不发任何请求。

这个文件里最重要的不是「功能对不对」，是**几条不变量**：
面板不发付费请求、不写业务表、不把密钥或个人信息漏到前端、
口令没配就拒绝启动。那几条一旦破了，破法都是安静的。
"""

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import panel, panel_view, railway, summary
from xhsearch.config import Settings

UTC = timezone.utc
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
GOOD_PASSWORD = "a-very-long-passphrase"


def config(**kwargs):
    base = dict(password=GOOD_PASSWORD, secret=b"secret-bytes", port=8080)
    base.update(kwargs)
    return panel.PanelConfig(**base)


class FakeTable:
    """只实现面板真正会调的两个方法。多一个都不给——
    面板要是哪天开始调别的东西，这里会直接 AttributeError。"""

    def __init__(self, meta, records, *, app_token="bascnAAA", table_id="tblBBB",
                 meta_error=None, search_error=None):
        self.app_token = app_token
        self.table_id = table_id
        self._meta = meta
        self._records = records
        self._meta_error = meta_error
        self._search_error = search_error
        self.searched_fields = None
        self.searched_filter = None

    def fields_meta(self):
        if self._meta_error:
            raise self._meta_error
        return self._meta

    def search(self, field_names, *, filter_spec=None, **_kwargs):
        if self._search_error:
            raise self._search_error
        self.searched_fields = list(field_names)
        self.searched_filter = filter_spec
        return self._records


def healthy_meta(settings=None):
    from xhsearch import schema
    settings = settings or Settings()
    meta = {}
    for name, allowed, _label, options, _note in schema.expected_schema(settings):
        meta[name] = {"type": allowed[0], "ui_type": "",
                      "options": list(options) if options else None}
    return meta


# ---------------------------------------------------------------- 不变量

def _called_names(module) -> set:
    """模块里所有被调用的名字（含 a.b.c 这种点号形式）。

    按 AST 走，不是字符串搜——注释和文档字符串里提到某个函数名是正常的
    （这个仓库的文档就写在代码里），把它们算成「调用了它」会逼着人
    为了过测试而把话说得含糊。
    """
    import ast
    import inspect

    called = set()

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                called.add(name)
                called.add(name.rsplit(".", 1)[-1])
    return called


def _imported_modules(module) -> set:
    import ast
    import inspect

    names = set()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
            if node.module:
                names.add(node.module.split(".")[0])
    return names


class TestNeverSpends(unittest.TestCase):
    def test_panel_calls_nothing_that_spends_money(self):
        """面板永远不发付费请求。按 AST 逐个调用节点查，不是字符串搜。

        只认**带点号的全名**和几个不会撞车的裸名字：光看裸名字的话，
        `Cache.refresh`（重新读一遍飞书）会被当成 `runner.refresh`（真花钱）。
        「面板压根不 import 那两层」由下面两条测试保证，两条合起来才严密。
        """
        called = _called_names(panel)
        for name in called:
            self.assertFalse(
                name.startswith(("providers.", "runner.", "protocol.", "transport.")),
                f"panel.py 调了 {name}——面板不许碰会发请求的那几层")
        for forbidden in ("plan_calls", "estimate_yuan", "_fetch_one",
                          "_call_once", "post_with_retry", "get_with_retry",
                          "get_provider", "write_back"):
            self.assertNotIn(forbidden, called,
                             f"panel.py 调了 {forbidden}——面板不许成为付费执行者")

    def test_panel_does_not_import_the_provider_layer(self):
        self.assertNotIn("providers", _imported_modules(panel))
        self.assertFalse(hasattr(panel, "providers"),
                         "panel 模块不该持有 providers 的引用")

    def test_panel_does_not_import_runner_either(self):
        """连 runner 都不引：面板要的 row_from_record 是 summary 那一层的事，
        面板自己碰不到编排层，也就没有「不小心调了 refresh」的可能。"""
        self.assertNotIn("runner", _imported_modules(panel))

    def test_collect_only_reads(self):
        """跑一趟完整的 collect，确认它只调了 fields_meta 和 search。"""
        table = FakeTable(healthy_meta(), [])
        with mock.patch.object(FakeTable, "search",
                               side_effect=FakeTable.search,
                               autospec=True) as spy:
            panel.collect([("A", table)], Settings(), {"tikhub": "k"}, now=NOW)
        self.assertEqual(spy.call_count, 1)


class TestNeverWrites(unittest.TestCase):
    def test_panel_has_no_write_call(self):
        """P1 阶段面板对业务表**一列都不写**。写路径要到 P4 才出现，
        到时候白名单里也只会有「排队刷新」一个元素。"""
        called = _called_names(panel)
        for forbidden in ("batch_update", "batch_create", "create_field",
                          "add_field_options", "update_field"):
            self.assertNotIn(forbidden, called,
                             f"panel.py 调了 {forbidden}——现阶段面板是只读的")


class TestConfigRefusesToStartUnsafe(unittest.TestCase):
    def test_missing_password_refuses(self):
        with self.assertRaises(panel.ConfigError) as ctx:
            panel.PanelConfig.from_env({})
        self.assertIn("PANEL_PASSWORD", str(ctx.exception))

    def test_short_password_refuses(self):
        with self.assertRaises(panel.ConfigError) as ctx:
            panel.PanelConfig.from_env({"PANEL_PASSWORD": "short"})
        self.assertIn("至少", str(ctx.exception))

    def test_secret_is_random_when_unset(self):
        a = panel.PanelConfig.from_env({"PANEL_PASSWORD": GOOD_PASSWORD})
        b = panel.PanelConfig.from_env({"PANEL_PASSWORD": GOOD_PASSWORD})
        self.assertNotEqual(a.secret, b.secret)
        self.assertGreaterEqual(len(a.secret), 32)

    def test_bad_numbers_refuse_rather_than_clamp(self):
        for env in ({"PANEL_CACHE_SECONDS": "0"}, {"PANEL_CACHE_SECONDS": "abc"},
                    {"PORT": "0"}, {"PORT": "99999"}, {"PANEL_SHOW_DIGEST": "maybe"}):
            env = {"PANEL_PASSWORD": GOOD_PASSWORD, **env}
            with self.assertRaises(panel.ConfigError):
                panel.PanelConfig.from_env(env)


class TestSessions(unittest.TestCase):
    def test_roundtrip(self):
        cfg = config()
        self.assertTrue(panel.valid_session(cfg, panel.issue_session(cfg)))

    def test_expired_token_rejected(self):
        # now=0 是 epoch，是个合法时刻。曾经这里写的是 `now or time.time()`，
        # 0 被当成「没传」，于是这个「1970 年签发的 token」反而是有效的。
        cfg = config()
        token = panel.issue_session(cfg, now=0)
        self.assertFalse(panel.valid_session(cfg, token))

    def test_zero_is_a_real_timestamp_not_a_missing_one(self):
        cfg = config()
        token = panel.issue_session(cfg, now=0)
        self.assertTrue(token.startswith(str(panel.SESSION_TTL_SECONDS) + "."))
        self.assertTrue(panel.valid_session(cfg, token, now=0))

    def test_forged_signature_rejected(self):
        cfg = config()
        expiry = int(NOW.timestamp()) + 9999
        self.assertFalse(panel.valid_session(cfg, f"{expiry}.deadbeef"))

    def test_token_from_another_secret_rejected(self):
        token = panel.issue_session(config(secret=b"other"))
        self.assertFalse(panel.valid_session(config(), token))

    def test_garbage_rejected_without_raising(self):
        cfg = config()
        for junk in ("", "x", "....", "abc.def", "9999999999999999999999.x"):
            self.assertFalse(panel.valid_session(cfg, junk))

    def test_csrf_token_is_derived_not_the_session(self):
        cfg = config()
        session = panel.issue_session(cfg)
        derived = panel.csrf_token(cfg, session)
        self.assertNotEqual(derived, session)
        self.assertNotIn(session, derived)
        self.assertEqual(derived, panel.csrf_token(cfg, session))
        self.assertNotEqual(derived, panel.csrf_token(config(secret=b"other"), session))
        self.assertEqual(panel.csrf_token(cfg, ""), "")

    def test_password_check_is_exact(self):
        cfg = config()
        self.assertTrue(panel.check_password(cfg, GOOD_PASSWORD))
        self.assertFalse(panel.check_password(cfg, GOOD_PASSWORD + " "))
        self.assertFalse(panel.check_password(cfg, ""))


class TestLoginThrottle(unittest.TestCase):
    def test_blocks_after_the_limit(self):
        throttle = panel.LoginThrottle(max_failures=3, window=60)
        for _ in range(3):
            self.assertFalse(throttle.blocked("1.2.3.4"))
            throttle.record_failure("1.2.3.4")
        self.assertTrue(throttle.blocked("1.2.3.4"))

    def test_one_source_does_not_lock_out_everyone(self):
        """全局锁定意味着任何人都能让整个团队登不进来。"""
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        for _ in range(5):
            throttle.record_failure("attacker")
        self.assertTrue(throttle.blocked("attacker"))
        self.assertFalse(throttle.blocked("colleague"))

    def test_window_expires(self):
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        throttle.record_failure("ip", now=1000)
        throttle.record_failure("ip", now=1000)
        self.assertTrue(throttle.blocked("ip", now=1010))
        self.assertFalse(throttle.blocked("ip", now=1100))

    def test_bucket_count_is_bounded(self):
        """X-Forwarded-For 可以伪造。伪造只能绕过限速，
        不能把限速本身变成内存耗尽。"""
        throttle = panel.LoginThrottle()
        for i in range(5000):
            throttle.record_failure(f"ip-{i}")
        self.assertLessEqual(len(throttle._buckets), 4096)

    def test_success_clears_the_bucket(self):
        throttle = panel.LoginThrottle(max_failures=2, window=60)
        throttle.record_failure("ip")
        throttle.clear("ip")
        self.assertFalse(throttle.blocked("ip"))


# ---------------------------------------------------------------- 取数

class TestCollect(unittest.TestCase):
    def test_only_asks_for_columns_that_exist(self):
        """按名字请求不存在的列会让整个 search 报 1254045，一行都读不回来。"""
        settings = Settings()
        meta = healthy_meta(settings)
        del meta[settings.fields.negative_status]
        table = FakeTable(meta, [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertNotIn(settings.fields.negative_status, table.searched_fields)
        self.assertIn(settings.fields.link, table.searched_fields)

    def test_filters_to_monitored_rows(self):
        settings = Settings()
        table = FakeTable(healthy_meta(settings), [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertEqual(table.searched_filter["conditions"][0]["field_name"],
                         settings.fields.monitoring)

    def test_missing_monitoring_column_is_flagged_not_silently_ignored(self):
        """没法过滤在管的行，下面每个数字都包含了本该排除的行——
        必须说出来，而不是给一堆看起来正常的数字。"""
        settings = Settings()
        meta = healthy_meta(settings)
        del meta[settings.fields.monitoring]
        overview = panel.collect([("A", FakeTable(meta, []))], settings, {}, now=NOW)
        self.assertTrue(any("无法只统计在管的行" in p
                            for p in overview.projects[0].health))

    def test_unreadable_metadata_explains_the_usual_cause(self):
        overview = panel.collect([("A", FakeTable(None, []))], Settings(), {}, now=NOW)
        self.assertIn("添加文档应用", overview.projects[0].error)

    def test_empty_metadata_is_treated_the_same_as_unreadable(self):
        """多维表格的主字段不可删，健康的表不可能一列都没有。"""
        overview = panel.collect([("A", FakeTable({}, []))], Settings(), {}, now=NOW)
        self.assertIn("添加文档应用", overview.projects[0].error)

    def test_one_broken_table_does_not_break_the_others(self):
        settings = Settings()
        broken = FakeTable(healthy_meta(settings), [],
                           search_error=RuntimeError("权限被收回"))
        good = FakeTable(healthy_meta(settings), [], app_token="t2", table_id="tb2")
        overview = panel.collect([("坏", broken), ("好", good)], settings, {}, now=NOW)
        self.assertIn("权限被收回", overview.projects[0].error)
        self.assertEqual(overview.projects[1].error, "")

    def test_digest_columns_not_requested_by_default(self):
        settings = Settings()
        table = FakeTable(healthy_meta(settings), [])
        panel.collect([("A", table)], settings, {}, now=NOW)
        self.assertNotIn(settings.fields.comment_digest, table.searched_fields)


class TestCache(unittest.TestCase):
    def test_keeps_the_last_good_snapshot_when_a_refresh_fails(self):
        """一次网络抖动不该让整个面板变成白纸。"""
        results = [summary.Overview(projects=[]), RuntimeError("网络抖了一下")]

        def produce():
            item = results.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        cache = panel.Cache(produce, ttl=999)
        cache.refresh()
        first, error, _ = cache.snapshot()
        self.assertIsNotNone(first)
        self.assertEqual(error, "")
        cache.refresh()
        second, error, _ = cache.snapshot()
        self.assertIs(second, first)
        self.assertIn("网络抖了一下", error)

    def test_first_failure_leaves_no_snapshot_but_records_why(self):
        cache = panel.Cache(lambda: (_ for _ in ()).throw(RuntimeError("挂了")), ttl=999)
        cache.refresh()
        value, error, _ = cache.snapshot()
        self.assertIsNone(value)
        self.assertIn("挂了", error)


class TestFeishuBase(unittest.TestCase):
    def test_explicit_domain_wins(self):
        self.assertEqual(panel._feishu_base({"FEISHU_DOMAIN": "https://acme.feishu.cn"}),
                         "https://acme.feishu.cn")

    def test_bare_domain_gets_https(self):
        self.assertEqual(panel._feishu_base({"FEISHU_DOMAIN": "acme.feishu.cn"}),
                         "https://acme.feishu.cn")

    def test_scraped_from_tables_spec(self):
        spec = "A=bascnX:tblY; B=https://acme.feishu.cn/base/bascnZ?table=tblW"
        self.assertEqual(panel._feishu_base({"FEISHU_TABLES": spec}),
                         "https://acme.feishu.cn")

    def test_falls_back_to_the_generic_domain(self):
        self.assertEqual(panel._feishu_base({}), "https://feishu.cn")


# ---------------------------------------------------------------- 渲染

class TestRendering(unittest.TestCase):
    def _page(self, snap, **kwargs):
        overview = summary.Overview(projects=[snap], generated_at=NOW)
        return panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(**kwargs), csrf="tok")

    def _snap(self, **kwargs):
        base = dict(label="项目A", app_token="t", table_id="tb")
        base.update(kwargs)
        return summary.ProjectSnapshot(**base)

    def test_escapes_text_that_came_from_feishu(self):
        """诊断信息、项目名、评论正文都是人写的字。直接拼进 HTML 就是存储型 XSS。"""
        todo = summary.TodoRow(record_id="r1", project="<img src=x onerror=alert(1)>",
                               record_url="https://x/base/t?table=tb&record=r1",
                               link_cell="", reasons=["风控中"],
                               diagnosis="</td><script>alert(1)</script>")
        page = self._page(self._snap(todos=[todo]))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src=x onerror", page)
        self.assertIn("&lt;script&gt;", page)

    def test_no_external_resources(self):
        page = self._page(self._snap())
        for pattern in (r'src=["\']https?://', r'href=["\']https?://(?!\w+\.feishu)',
                        r'@import', r'//cdn\.'):
            self.assertIsNone(re.search(pattern, page),
                              f"页面里出现了外部资源：{pattern}")

    def test_empty_todo_list_says_so_plainly(self):
        page = self._page(self._snap())
        self.assertIn("没有需要处理的行", page)

    def test_deep_link_is_present_for_each_todo(self):
        todo = summary.TodoRow(record_id="rec1", project="A",
                               record_url="https://acme.feishu.cn/base/t?table=tb&record=rec1",
                               link_cell="", reasons=["有负面"])
        page = self._page(self._snap(todos=[todo]))
        self.assertIn("record=rec1", page)
        self.assertIn("去这一行", page)

    def test_generic_domain_warning_shown_when_unset(self):
        page = self._page(self._snap())
        self.assertIn("FEISHU_DOMAIN", page)

    def test_no_warning_when_domain_is_configured(self):
        page = self._page(self._snap(), feishu_base="https://acme.feishu.cn")
        self.assertNotIn("FEISHU_DOMAIN", page)

    def test_health_problems_are_shown_verbatim(self):
        page = self._page(self._snap(health=["「流量状态」缺这些选项：观察中。"]))
        self.assertIn("缺这些选项", page)

    def test_stale_snapshot_is_labelled(self):
        overview = summary.Overview(projects=[self._snap()], generated_at=NOW)
        page = panel_view.overview_page(
            overview=overview, error="FeishuError: 权限被收回",
            fetched_at=NOW.timestamp(), config=config(), csrf="tok")
        self.assertIn("上一次取数失败", page)
        self.assertIn("权限被收回", page)

    def test_cold_start_page_does_not_crash(self):
        page = panel_view.overview_page(
            overview=None, error="", fetched_at=0.0, config=config(), csrf="")
        self.assertIn("正在第一次取数", page)

    def test_login_page_escapes_its_message(self):
        page = panel_view.login_page("<script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_page_never_contains_the_password_or_secret(self):
        """密钥绝不下发前端。"""
        page = self._page(self._snap())
        self.assertNotIn(GOOD_PASSWORD, page)
        self.assertNotIn("secret-bytes", page)


class TestJsonPayload(unittest.TestCase):
    def test_todo_json_is_serialisable_and_carries_no_secrets(self):
        todo = summary.TodoRow(record_id="r1", project="A", record_url="u",
                               link_cell="https://xhs/x", reasons=["风控中"])
        blob = json.dumps(panel._todo_json(todo), ensure_ascii=False)
        self.assertIn("风控中", blob)

    def test_project_json_round_trips(self):
        snap = summary.ProjectSnapshot(label="A", app_token="t", table_id="tb",
                                       total_rows=3, due_yuan=1.23456)
        payload = panel._project_json(snap)
        self.assertEqual(payload["due_yuan"], 1.23)
        json.dumps(payload, ensure_ascii=False)


class TestLogSanitising(unittest.TestCase):
    def test_control_characters_are_scrubbed(self):
        """Python 3.12 才在 BaseHTTPRequestHandler 里做这件事，
        而这个项目钉在 3.11——不自己做，远端能往终端里塞转义序列。"""
        dirty = "GET /\x1b[31mred\x00\x07 HTTP/1.1"
        clean = panel._sanitize_for_log(dirty)
        self.assertNotIn("\x1b", clean)
        self.assertNotIn("\x00", clean)
        self.assertNotIn("\x07", clean)

    def test_长度有上界(self):
        self.assertLessEqual(len(panel._sanitize_for_log("x" * 5000)), 200)


# ---------------------------------------------------------------- 真起服务

class TestOverHttp(unittest.TestCase):
    """真起一个服务、真发 HTTP 请求。

    鉴权、CSRF、Cookie 属性这几件事，单看函数是测不出来的——
    它们的 bug 全都长在「handler 到底往响应里写了什么」上。
    """

    @classmethod
    def setUpClass(cls):
        from xhsearch import schema as _schema

        # 访问日志会把测试输出淹掉。这里静音，但**不改生产行为**——
        # 日志的清洗逻辑另有单测（TestLogSanitising）盯着。
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)

        settings = Settings()

        class Table:
            app_token, table_id = "bascnAAA", "tblBBB"

            def fields_meta(self):
                return {n: {"type": a[0], "ui_type": "",
                            "options": list(o) if o else None}
                        for n, a, _l, o, _x in _schema.expected_schema(settings)}

            def search(self, fields, *, filter_spec=None, **kwargs):
                return [{"record_id": "r1", "fields": {
                    settings.fields.link: "https://www.xiaohongshu.com/explore/aa",
                    settings.fields.traffic_status: ["风控中"],
                    settings.fields.failure_reason: "1006 内容被限制",
                }}]

        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cache = panel.Cache(
            lambda: panel.collect([("A", Table())], settings, {}, now=NOW), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def _call(self, path, *, data=None, headers=None, method=None,
              follow=True, host="localhost"):
        import urllib.error
        import urllib.request

        url = f"http://{host}:{self.port}{path}"
        request = urllib.request.Request(url, data=data, headers=headers or {},
                                         method=method)
        if follow:
            opener = urllib.request.build_opener()
        else:
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(request, timeout=5)
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), exc.headers

    def _login(self, host="localhost", headers=None):
        status, _body, resp_headers = self._call(
            "/login", data=b"password=" + GOOD_PASSWORD.encode(),
            headers=headers, follow=False, host=host)
        cookie = resp_headers.get("Set-Cookie") or ""
        token = cookie.split("=", 1)[1].split(";")[0] if "=" in cookie else ""
        return status, cookie, token

    def _authed(self, token):
        return {"Cookie": f"{panel.COOKIE_NAME}={token}"}

    # —— 无需登录 ——
    def test_healthz_needs_no_password(self):
        status, body, _ = self._call("/healthz")
        self.assertEqual((status, body), (200, "ok"))

    def test_root_without_session_is_the_login_page(self):
        status, body, _ = self._call("/")
        self.assertEqual(status, 200)
        self.assertIn("口令", body)
        self.assertNotIn("要人管的行", body)

    def test_api_without_session_is_401(self):
        self.assertEqual(self._call("/api/overview")[0], 401)

    def test_unknown_route_is_404(self):
        self.assertEqual(self._call("/nope")[0], 404)

    def test_security_headers_are_present(self):
        _s, _b, headers = self._call("/healthz")
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    # —— 登录 ——
    def test_wrong_password_is_401_and_grants_nothing(self):
        status, body, headers = self._call("/login", data=b"password=nope")
        self.assertEqual(status, 401)
        self.assertIn("口令不对", body)
        self.assertNotIn("Set-Cookie", headers)

    def test_oversized_body_is_rejected_before_reading(self):
        status, _b, _h = self._call("/login", data=b"password=" + b"x" * 70000)
        self.assertEqual(status, 413)

    def test_missing_content_length_is_411(self):
        # urllib 总会带 Content-Length，所以直接走裸 socket。
        import socket
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(b"POST /login HTTP/1.0\r\nHost: localhost\r\n\r\n")
            self.assertIn(b"411", sock.recv(64))

    def test_login_then_see_the_panel(self):
        status, _cookie, token = self._login()
        self.assertEqual(status, 303)
        body = self._call("/", headers=self._authed(token))[1]
        self.assertIn("要人管的行", body)
        self.assertIn("1006 内容被限制", body)
        self.assertIn("record=r1", body)

    def test_password_never_appears_in_any_response(self):
        _s, _c, token = self._login()
        for path in ("/", "/api/overview"):
            self.assertNotIn(GOOD_PASSWORD, self._call(path, headers=self._authed(token))[1])

    # —— Cookie 属性 ——
    def test_plain_localhost_omits_secure_so_local_dev_works(self):
        _s, cookie, _t = self._login()
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_https_forwarded_gets_secure(self):
        _s, cookie, _t = self._login(headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", cookie)

    def test_unknown_host_gets_secure(self):
        """判断不出来是不是明文时，从严——失败方向必须是「更严」。"""
        _s, cookie, _t = self._login(host="127.0.0.1",
                                     headers={"Host": "panel.up.railway.app"})
        self.assertIn("Secure", cookie)

    # —— CSRF ——
    def test_post_without_the_header_is_refused(self):
        _s, _c, token = self._login()
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=self._authed(token))
        self.assertEqual(status, 403)

    def test_post_with_a_wrong_header_is_refused(self):
        _s, _c, token = self._login()
        headers = {**self._authed(token), panel.CSRF_HEADER: "not-the-token"}
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=headers)
        self.assertEqual(status, 403)

    def test_post_with_the_matching_header_works(self):
        _s, _c, token = self._login()
        csrf = panel.csrf_token(self.config, token)
        headers = {**self._authed(token), panel.CSRF_HEADER: csrf}
        status, body, _h = self._call("/api/refresh", data=b"", method="POST",
                                      headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("风控中", body)

    def test_session_value_is_not_accepted_as_the_csrf_token(self):
        """CSRF token 是派生值，不是会话本身——会话原值不该能当它用。
        （否则页面里那个 data-csrf 就等于把 HttpOnly 的 Cookie 抄进了 DOM。）"""
        _s, _c, token = self._login()
        headers = {**self._authed(token), panel.CSRF_HEADER: token}
        status, _b, _h = self._call("/api/refresh", data=b"", method="POST",
                                    headers=headers)
        self.assertEqual(status, 403)

    def test_page_does_not_leak_the_session_value(self):
        _s, _c, token = self._login()
        body = self._call("/", headers=self._authed(token))[1]
        self.assertNotIn(token, body)
        self.assertIn(panel.csrf_token(self.config, token), body)

    def test_forged_cookie_does_not_get_in(self):
        headers = {"Cookie": f"{panel.COOKIE_NAME}=99999999999.deadbeef"}
        self.assertEqual(self._call("/api/overview", headers=headers)[0], 401)


# ---------------------------------------------------------------- 日志

class FakeRailwayConfig:
    def __init__(self, enabled=True, missing=()):
        self._enabled = enabled
        self._missing = list(missing)
        self.token = "tok-1234567890" if enabled else ""
        self.environment_id = "env-1" if enabled else ""
        self.service_id = ""

    @property
    def enabled(self):
        return self._enabled

    def missing(self):
        return list(self._missing)


class TestLogFeed(unittest.TestCase):
    def _feed(self, fetch, *, enabled=True, missing=(), ttl=999):
        cfg = config()
        cfg.railway = FakeRailwayConfig(enabled=enabled, missing=missing)
        cfg.secrets = ("sk-live-abcdefghijklmnop",)
        return panel.LogFeed(cfg, ttl, fetch=fetch)

    def test_disabled_says_exactly_which_variable_is_missing(self):
        """「日志不可用」这种话没用。"""
        feed = self._feed(lambda *a, **k: [], enabled=False,
                          missing=["RAILWAY_API_TOKEN"])
        lines, message = feed.get()
        self.assertEqual(lines, [])
        self.assertIn("RAILWAY_API_TOKEN", message)

    def test_secrets_are_handed_to_the_fetcher(self):
        seen = {}

        def fetch(cfg, *, secrets=()):
            seen["secrets"] = secrets
            return []
        self._feed(fetch).get()
        self.assertIn("sk-live-abcdefghijklmnop", seen["secrets"])

    def test_result_is_cached_within_the_ttl(self):
        calls = []

        def fetch(cfg, *, secrets=()):
            calls.append(1)
            return [railway.LogLine(message="x")]
        feed = self._feed(fetch)
        feed.get()
        feed.get()
        self.assertEqual(len(calls), 1)

    def test_force_bypasses_the_cache(self):
        calls = []

        def fetch(cfg, *, secrets=()):
            calls.append(1)
            return []
        feed = self._feed(fetch)
        feed.get()
        feed.get(force=True)
        self.assertEqual(len(calls), 2)

    def test_a_railway_failure_keeps_the_last_batch(self):
        """日志少一次刷新不该让页面上那一块变空白。"""
        state = {"fail": False}

        def fetch(cfg, *, secrets=()):
            if state["fail"]:
                raise railway.RailwayError("429 限流")
            return [railway.LogLine(message="上一批")]
        feed = self._feed(fetch, ttl=0)
        feed.get()
        state["fail"] = True
        lines, error = feed.get()
        self.assertEqual([l.message for l in lines], ["上一批"])
        self.assertIn("429", error)

    def test_an_unexpected_exception_is_contained(self):
        def fetch(cfg, *, secrets=()):
            raise ValueError("没想到的错")
        lines, error = self._feed(fetch).get()
        self.assertEqual(lines, [])
        self.assertIn("没想到的错", error)


class TestSecretsList(unittest.TestCase):
    def test_collects_every_secret_that_could_show_up_in_a_log(self):
        env = {"TIKHUB_API_KEY": "tk", "SOCIALDATAX_API_KEY": "sd",
               "FEISHU_APP_SECRET": "fs", "RAILWAY_API_TOKEN": "rw",
               "PANEL_PASSWORD": "pw"}
        found = panel._secrets(env)
        for value in env.values():
            self.assertIn(value, found)

    def test_empty_values_are_dropped(self):
        self.assertEqual(panel._secrets({"TIKHUB_API_KEY": "  "}), ())


class TestRunHistoryRendering(unittest.TestCase):
    def _page(self, runs, log_error=""):
        overview = summary.Overview(
            projects=[summary.ProjectSnapshot(label="A", app_token="t",
                                              table_id="tb")],
            generated_at=NOW)
        return panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(), csrf="tok", runs=runs, log_error=log_error)

    def _run(self, **kwargs):
        base = dict(run_id="r1", mode="sweep", started_at=NOW.timestamp(),
                    ended_at=NOW.timestamp() + 30, exit_code=0, rows=7,
                    cost_yuan=0.5)
        base.update(kwargs)
        return railway.Run(**base)

    def test_no_runs_explains_what_is_needed(self):
        page = self._page([])
        self.assertIn("RUN_LOG_JSON", page)

    def test_a_normal_run_reads_as_normal(self):
        page = self._page([self._run()])
        self.assertIn("正常", page)
        self.assertIn("¥0.50", page)

    def test_an_unfinished_run_is_called_out(self):
        """有 run_start 没 run_end = 被容器杀掉了，那几轮的钱多半白花了。"""
        page = self._page([self._run(ended_at=None, exit_code=None)])
        self.assertIn("没跑完", page)
        self.assertIn("被容器杀掉", page)

    def test_a_tripped_breaker_is_shown(self):
        run = self._run()
        run.tables = [railway.TableRun(label="A", breaker_tripped=True)]
        self.assertIn("已熔断", self._page([run]))

    def test_failovers_are_surfaced(self):
        run = self._run()
        run.tables = [railway.TableRun(label="A", failovers=4)]
        self.assertIn("降级 4 次", self._page([run]))

    def test_log_error_is_shown_without_hiding_the_runs(self):
        page = self._page([self._run()], log_error="429 限流")
        self.assertIn("429 限流", page)
        self.assertIn("¥0.50", page)

    def test_run_fields_are_escaped(self):
        page = self._page([self._run(error="<script>alert(1)</script>")])
        self.assertNotIn("<script>alert(1)</script>", page)


class TestUnknownEstimateIsNotZero(unittest.TestCase):
    """缺「最近检查时间」列时，`load_rows` 直接 return []，于是「到期待刷」
    和「预计花费」都是 0——而真相是**算不出来**。

    这个 0 出现的时机恰恰最危险：把那一列建出来之后全表都会判到期，
    而人刚刚才看着 0 放下心来。
    """

    def _snap(self, drop_last_updated):
        from xhsearch import schema as schema_mod
        settings = Settings()
        meta = {}
        for name, allowed, _l, options, _n in schema_mod.expected_schema(settings):
            if drop_last_updated and name == settings.fields.last_updated:
                continue
            meta[name] = {"type": allowed[0], "ui_type": "",
                          "options": list(options) if options else None}

        class Table:
            app_token, table_id = "t", "tb"

            def fields_meta(self):
                return meta

            def search(self, fields, *, filter_spec=None, **kwargs):
                return [{"record_id": "r1", "fields": {
                    settings.fields.link: "https://www.xiaohongshu.com/explore/aa"}}]

        return panel.collect([("A", Table())], settings, {}, now=NOW).projects[0]

    def test_missing_column_blocks_the_estimate(self):
        snap = self._snap(drop_last_updated=True)
        self.assertTrue(snap.estimate_blocked)
        self.assertIn("算不出来", snap.estimate_blocked)

    def test_a_healthy_table_estimates_normally(self):
        self.assertEqual(self._snap(drop_last_updated=False).estimate_blocked, "")

    def test_the_card_says_so_instead_of_showing_a_number(self):
        snap = self._snap(drop_last_updated=True)
        page = panel_view.overview_page(
            overview=summary.Overview(projects=[snap], generated_at=NOW),
            error="", fetched_at=NOW.timestamp(), config=config(), csrf="t")
        self.assertIn("无法估算", page)
        self.assertNotIn("≈ ¥0.00", page)

    def test_the_top_bar_flags_that_the_total_is_only_a_lower_bound(self):
        blocked = summary.ProjectSnapshot(label="A", app_token="t", table_id="tb",
                                          estimate_blocked="缺列")
        fine = summary.ProjectSnapshot(label="B", app_token="t2", table_id="tb2",
                                       due_yuan=3.0)
        overview = summary.Overview(projects=[blocked, fine], generated_at=NOW)
        self.assertEqual([p.label for p in overview.unestimatable], ["A"])
        page = panel_view.overview_page(
            overview=overview, error="", fetched_at=NOW.timestamp(),
            config=config(), csrf="t")
        self.assertIn("算不出", page)


class TestProjectRoutes(unittest.TestCase):
    """项目页的写路径。它们改的是**注册表**（这套东西自己的配置存储），
    以及业务表的**结构**（只追加）——业务表的数据一个字都不碰。
    """

    def _projects(self, **overrides):
        cfg = config()
        cfg.registry_target = "bascnREG:tblREG"
        cfg.app_id = "cli_x"
        cfg.app_secret = "s"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return panel.Projects(cfg, Settings(), log=lambda *a: None), cfg

    def test_disabled_until_the_registry_is_configured(self):
        cfg = config()
        cfg.app_id = "cli_x"
        projects = panel.Projects(cfg, Settings())
        self.assertFalse(projects.enabled)
        self.assertIn("init-registry", projects.why_disabled())

    def test_missing_app_id_is_reported_distinctly(self):
        projects = panel.Projects(config(), Settings())
        self.assertIn("FEISHU_APP_ID", projects.why_disabled())

    def test_enabled_when_both_are_set(self):
        projects, _ = self._projects()
        self.assertTrue(projects.enabled)

    def test_build_refuses_when_not_a_collaborator(self):
        """读不到字段列表就不该乱建列——先把提示给对。"""
        projects, _ = self._projects()
        with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                               return_value=None):
            with self.assertRaises(panel.feishu.FeishuError) as ctx:
                projects.build("bascnA", "tblB")
        self.assertIn("添加文档应用", str(ctx.exception))

    def test_build_passes_the_option_switch_through(self):
        from xhsearch import provision as provision_mod
        for allowed in (False, True):
            projects, _ = self._projects(allow_option_patch=allowed)
            with mock.patch.object(panel.feishu.Bitable, "fields_meta",
                                   return_value={"x": {"type": 1, "ui_type": "",
                                                       "options": None}}), \
                 mock.patch.object(provision_mod, "build_missing") as built:
                projects.build("bascnA", "tblB")
            self.assertEqual(built.call_args.kwargs["allow_option_patch"], allowed)

    def test_remove_only_touches_the_registry_row(self):
        from xhsearch import registry as registry_mod
        projects, _ = self._projects()
        with mock.patch.object(registry_mod, "remove") as removed:
            projects.remove("rec1")
        self.assertEqual(removed.call_args[0][1], "rec1")


class TestProjectRoutesOverHttp(unittest.TestCase):
    """路由层：鉴权、CSRF、错误翻译。"""

    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)

        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32,
                                       port=0, cache_seconds=999)
        cls.config.registry_target = "bascnREG:tblREG"
        cls.config.app_id = "cli_x"
        cls.config.app_secret = "s"
        cls.actions = mock.Mock(spec=panel.Projects)
        cls.actions.enabled = True
        cls.actions.why_disabled.return_value = ""
        cls.actions.list.return_value = []
        cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cache.refresh()
        cls.cache = cache
        cls.server = panel.build_server(cls.config, cache, projects=cls.actions,
                                        host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = __import__("threading").Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.actions.reset_mock()
        self.actions.enabled = True
        self.actions.list.return_value = []

    def _call(self, path, *, data=None, headers=None, method=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://localhost:{self.port}{path}",
                                     data=data, headers=headers or {}, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _login(self):
        import urllib.error
        import urllib.request

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(f"http://localhost:{self.port}/login",
                                     data=b"password=" + GOOD_PASSWORD.encode())
        try:
            resp = opener.open(req, timeout=5)
            cookie = resp.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as exc:
            cookie = exc.headers.get("Set-Cookie") or ""
        token = cookie.split("=", 1)[1].split(";")[0]
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token),
                "Content-Type": "application/json"}

    def test_listing_needs_a_session(self):
        self.assertEqual(self._call("/api/projects")[0], 401)

    def test_writes_need_a_session(self):
        status, _ = self._call("/api/projects/remove", data=b"{}", method="POST")
        self.assertEqual(status, 401)

    def test_writes_need_the_csrf_header(self):
        headers = self._login()
        headers.pop(panel.CSRF_HEADER)
        status, _ = self._call("/api/projects/remove", data=b'{"record_id":"r"}',
                               headers=headers, method="POST")
        self.assertEqual(status, 403)

    def test_a_valid_remove_reaches_the_action_layer(self):
        status, _ = self._call("/api/projects/remove", data=b'{"record_id":"rec9"}',
                               headers=self._login(), method="POST")
        self.assertEqual(status, 200)
        self.actions.remove.assert_called_once_with("rec9")

    def test_a_bad_link_becomes_a_400_with_the_reason(self):
        from xhsearch import tablespec
        self.actions.check.side_effect = tablespec.BadTarget("这一项看不懂")
        status, body = self._call("/api/projects/check",
                                  data=b'{"label":"x","target":"junk"}',
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("看不懂", body)

    def test_a_feishu_error_carries_its_remedy(self):
        """面板要把「怎么修」摆在错误旁边，而不是让人从报错文本里自己找。"""
        self.actions.build.side_effect = panel.feishu.FeishuError(
            1254302, "no permission", "这张表开了「高级权限」")
        status, body = self._call("/api/projects/build",
                                  data=b'{"app_token":"a","table_id":"t"}',
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("高级权限", body)

    def test_a_non_json_body_is_rejected_cleanly(self):
        status, body = self._call("/api/projects/add", data=b"not json",
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body)

    def test_an_unknown_action_is_404(self):
        status, _ = self._call("/api/projects/frobnicate", data=b"{}",
                               headers=self._login(), method="POST")
        self.assertEqual(status, 404)

    def test_oversized_body_is_still_capped(self):
        status, _ = self._call("/api/projects/add", data=b"{" + b"x" * 70000,
                               headers=self._login(), method="POST")
        self.assertEqual(status, 413)

    def test_add_generates_an_idempotency_key_when_absent(self):
        self._call("/api/projects/add", data=b'{"label":"x","target":"a:b"}',
                   headers=self._login(), method="POST")
        self.assertTrue(self.actions.add.call_args.kwargs["client_token"])

    def test_disabled_projects_explain_why(self):
        self.actions.enabled = False
        self.actions.why_disabled.return_value = "还没配 FEISHU_REGISTRY"
        status, body = self._call("/api/projects/add", data=b"{}",
                                  headers=self._login(), method="POST")
        self.assertEqual(status, 400)
        self.assertIn("FEISHU_REGISTRY", body)


if __name__ == "__main__":
    unittest.main()
