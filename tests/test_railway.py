"""Railway 日志客户端的单测。全部离线，不发任何请求。

重点在几件容易出错的事：GraphQL 的错误是 HTTP 200 + errors 数组
（只看状态码会把「字段名写错」当成「真的没有日志」）、
脱敏必须真的把 Key 抹掉、以及「有 run_start 没 run_end」要能认出来。
"""

import json
import unittest

from xhsearch import railway, transport


def resp(status=200, payload=None, body=None):
    return transport.Response(
        status=status, content_type="application/json",
        body=body if body is not None else json.dumps(payload or {}))


def log_item(message, ts="2026-08-26T12:00:00Z", severity="info",
             service="svc1", deployment="dep1"):
    return {"timestamp": ts, "message": message, "severity": severity,
            "tags": {"serviceId": service, "deploymentId": deployment}}


def envelope(items):
    return {"data": {"environmentLogs": items}}


def cfg(**kwargs):
    base = dict(token="tok-1234567890", environment_id="env-1")
    base.update(kwargs)
    return railway.RailwayConfig(**base)


def sender(response):
    captured = {}

    def post(url, headers, body, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return response
    return post, captured


class TestConfig(unittest.TestCase):
    def test_reports_exactly_which_variables_are_missing(self):
        """「日志不可用」这种话没用。要说清差哪个变量。"""
        config = railway.RailwayConfig.from_env({})
        gaps = config.missing()
        self.assertIn("RAILWAY_API_TOKEN", " ".join(gaps))
        self.assertIn("RAILWAY_ENVIRONMENT_ID", gaps)
        self.assertFalse(config.enabled)

    def test_enabled_when_both_present(self):
        config = railway.RailwayConfig.from_env(
            {"RAILWAY_API_TOKEN": "t", "RAILWAY_ENVIRONMENT_ID": "e"})
        self.assertTrue(config.enabled)
        self.assertEqual(config.missing(), [])

    def test_auto_injected_service_id_is_ignored(self):
        """RAILWAY_SERVICE_ID 是 Railway 自动注入的「本服务 ID」——在面板上
        就是面板自己。认了它，过滤出来的只有面板的访问日志，运行历史永远空。"""
        config = railway.RailwayConfig.from_env({
            "RAILWAY_SERVICE_ID": "panel-self", "RAILWAY_CRON_SERVICE_ID": "cron-1"})
        self.assertEqual(config.service_ids, ("cron-1",))
        alone = railway.RailwayConfig.from_env({"RAILWAY_SERVICE_ID": "panel-self"})
        self.assertEqual(alone.service_ids, ())

    def test_several_service_ids_split_and_deduped(self):
        config = railway.RailwayConfig.from_env(
            {"RAILWAY_CRON_SERVICE_ID": " q-1, s-2，q-1  s-3 "})
        self.assertEqual(config.service_ids, ("q-1", "s-2", "s-3"))

    def test_fetch_without_config_raises_not_configured(self):
        with self.assertRaises(railway.NotConfigured):
            railway.fetch_logs(railway.RailwayConfig(token="", environment_id=""))


class TestTokenScope(unittest.TestCase):
    """同一套 GraphQL 上有 `variables` 查询——一个能读日志的 account token
    同样能读回项目的全部环境变量（TikHub Key、飞书 Secret 都在里面）。
    所以范围更小的 project token 必须优先，而且它走另一个请求头。
    """

    def test_project_token_wins_over_account_token(self):
        config = railway.RailwayConfig.from_env(
            {"RAILWAY_PROJECT_TOKEN": "proj-1", "RAILWAY_API_TOKEN": "acct-1"})
        self.assertEqual(config.token, "proj-1")
        self.assertTrue(config.project_scoped)

    def test_project_token_uses_its_own_header(self):
        config = railway.RailwayConfig.from_env({"RAILWAY_PROJECT_TOKEN": "proj-1"})
        headers = config.headers()
        self.assertEqual(headers["Project-Access-Token"], "proj-1")
        self.assertNotIn("Authorization", headers)

    def test_account_token_uses_bearer(self):
        config = railway.RailwayConfig.from_env({"RAILWAY_API_TOKEN": "acct-1"})
        self.assertEqual(config.headers()["Authorization"], "Bearer acct-1")
        self.assertNotIn("Project-Access-Token", config.headers())

    def test_user_agent_is_not_urllibs_default(self):
        """backboard 前面的 Cloudflare 把 `Python-urllib/3.x` 直接 403（1010）。"""
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(), post=post)
        agent = seen["headers"].get("User-Agent", "")
        self.assertTrue(agent)
        self.assertNotIn("Python-urllib", agent)

    def test_the_right_header_actually_goes_out(self):
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(
            railway.RailwayConfig(token="proj-1", environment_id="env-1",
                                  project_scoped=True), post=post)
        self.assertEqual(seen["headers"]["Project-Access-Token"], "proj-1")


class TestRequestShape(unittest.TestCase):
    def test_sends_bearer_token_and_environment_id(self):
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(), post=post)
        self.assertEqual(seen["url"], railway.ENDPOINT)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok-1234567890")
        self.assertEqual(seen["body"]["variables"]["environmentId"], "env-1")

    def test_service_filter_is_added_when_known(self):
        """面板服务自己的访问日志混进来毫无价值，还会把巡检输出挤出翻页窗口。"""
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(service_id="svc-9"), post=post)
        self.assertIn("@service:svc-9", seen["body"]["variables"]["filter"])

    def test_caller_filter_is_kept_alongside_the_service_filter(self):
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(service_id="svc-9"), filter_expr="风控中", post=post)
        filt = seen["body"]["variables"]["filter"]
        self.assertIn("@service:svc-9", filt)
        self.assertIn("风控中", filt)

    def test_each_service_is_queried_separately_and_merged(self):
        """队列和整轮拆成两个服务：各查各的（各有自己的 beforeLimit 窗口），
        合并后新的在前。拼成一条 OR 过滤的话，整轮那几行会被队列挤出窗口。"""
        filters = []
        answers = {
            "q-1": [log_item("q-old", ts="2026-10-23T01:00:00Z", service="q-1"),
                    log_item("q-new", ts="2026-10-23T09:00:00Z", service="q-1")],
            "s-2": [log_item("s-mid", ts="2026-10-23T08:05:00Z", service="s-2"),
                    log_item("s-bad", ts="不是时间", service="s-2")],
        }

        def post(url, headers, body, **kwargs):
            variables = json.loads(body)["variables"]
            filters.append(variables["filter"])
            service = variables["filter"].split()[0].split(":", 1)[1]
            return resp(payload=envelope(answers[service]))

        lines = railway.fetch_logs(cfg(service_id="q-1,s-2"), filter_expr="风控中",
                                   post=post)
        self.assertEqual(filters, ["@service:q-1 风控中", "@service:s-2 风控中"])
        self.assertEqual([l.message for l in lines],
                         ["q-new", "s-mid", "q-old", "s-bad"])

    def test_one_service_failing_fails_the_fetch(self):
        """一个服务读不到就整批报错（面板会保留上一批），不悄悄只给一半。"""
        calls = []

        def post(url, headers, body, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                return resp(payload={"errors": [{"message": "boom"}]})
            return resp(payload=envelope([log_item("ok")]))

        with self.assertRaises(railway.RailwayError):
            railway.fetch_logs(cfg(service_id="a b"), post=post)

    def test_limit_is_clamped(self):
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(), limit=999999, post=post)
        self.assertEqual(seen["body"]["variables"]["beforeLimit"], railway.MAX_LIMIT)
        railway.fetch_logs(cfg(), limit=-5, post=post)
        self.assertEqual(seen["body"]["variables"]["beforeLimit"], 1)


class TestVerifiedSignature(unittest.TestCase):
    """签名对着 backboard 做过 introspection（2026-08-26）：

        environmentLogs(afterDate, afterLimit, anchorDate, beforeDate,
                        beforeLimit, environmentId, filter)

    这几条测试钉住两个照着 deploymentLogs 想当然就会踩的坑。
    """

    def test_uses_before_limit_not_limit(self):
        """environmentLogs **没有** limit 参数（有的是 deploymentLogs）。
        传了的话 GraphQL 在校验期整个查询报错，一条都拿不到。"""
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(), limit=42, post=post)
        self.assertEqual(seen["body"]["variables"]["beforeLimit"], 42)
        self.assertNotIn("limit", seen["body"]["variables"])
        self.assertNotIn("$limit", seen["body"]["query"])

    def test_service_filter_is_not_a_graphql_argument(self):
        """environmentLogs 没有 serviceId **参数**，只能走 filter 字符串。

        （`serviceId` 作为 `tags {}` 里的**返回字段**是存在的，
        所以这里只看 `environmentLogs(...)` 括号里那一段。）
        """
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(service_id="svc-9"), post=post)
        query = seen["body"]["query"]
        call = query[query.index("environmentLogs(", query.index("{")):]
        args = call[call.index("(") + 1:call.index(")")]
        self.assertNotIn("serviceId", args)
        self.assertNotIn("limit:", args)
        self.assertIn("@service:svc-9", seen["body"]["variables"]["filter"])

    def test_only_requests_fields_that_exist_on_the_log_type(self):
        """Log 只有 attributes / message / severity / tags / timestamp；
        LogTags 只有 deploymentId / deploymentInstanceId / environmentId /
        projectId / serviceId / snapshotId。多问一个字段整条查询就报错。"""
        post, seen = sender(resp(payload=envelope([])))
        railway.fetch_logs(cfg(), post=post)
        query = seen["body"]["query"]
        for field in ("timestamp", "message", "severity", "tags",
                      "serviceId", "deploymentId", "attributes", "key", "value"):
            self.assertIn(field, query)
        for absent in ("level", "text", "podName", "$limit"):
            self.assertNotIn(absent, query)


class TestErrorHandling(unittest.TestCase):
    def test_graphql_errors_come_back_as_http_200(self):
        """只看状态码的客户端会把「字段名写错」当成「真的没有日志」——
        两种情况都是空列表，最难查。"""
        post, _ = sender(resp(payload={"errors": [{"message": "Cannot query field x"}]}))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("Cannot query field x", str(ctx.exception))

    def test_401_says_what_to_do(self):
        post, _ = sender(resp(status=401, payload={}))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("token", str(ctx.exception))
        # 不再把人往「能读回全部密钥」的 account token 上推。
        self.assertNotIn("account", str(ctx.exception))

    def test_error_bodies_are_redacted_before_truncation(self):
        """先截再抹的话，跨在截断处的密钥只会被抹掉首尾，中间一截照样送到浏览器。"""
        secret = "sk-0123456789abcdefghijklmnopqrstuvwx"
        # 每一种都让密钥跨在截断处：截断后只剩前 19-20 个字符。
        cases = [
            resp(status=401, body="x" * 100 + secret),
            resp(status=502, body="<" + "x" * 180 + secret),
            resp(payload={"errors": [{"message": "y" * 280 + secret}]}),
        ]
        for response in cases:
            post, _ = sender(response)
            with self.assertRaises(railway.RailwayError) as ctx:
                railway.fetch_logs(cfg(), secrets=[secret], post=post)
            # 首 8 个字符之后的那一截：先截再抹时它会原样漏出去。
            self.assertNotIn(secret[8:16], str(ctx.exception))

    def test_cloudflare_1010_is_not_blamed_on_the_token(self):
        """Cloudflare 按 UA 拦截是 403 + `error code: 1010`，和 token 无关。"""
        post, _ = sender(resp(status=403, body="error code: 1010"))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("1010", str(ctx.exception))
        self.assertIn("不是 token 的问题", str(ctx.exception))

    def test_not_authorized_explains_the_likely_causes(self):
        """token 错了或者读不了这个环境，Railway 都是 HTTP 200 + Not Authorized。"""
        post, _ = sender(resp(payload={"errors": [{"message": "Not Authorized"}]}))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("Not Authorized", str(ctx.exception))
        self.assertIn("RAILWAY_ENVIRONMENT_ID", str(ctx.exception))

    def test_429_points_at_the_cache_setting(self):
        post, _ = sender(resp(status=429, payload={}))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("PANEL_CACHE_SECONDS", str(ctx.exception))

    def test_non_json_body_is_reported_not_swallowed(self):
        post, _ = sender(resp(status=502, body="<html>bad gateway</html>"))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), post=post)
        self.assertIn("502", str(ctx.exception))

    def test_malformed_items_are_skipped_not_fatal(self):
        post, _ = sender(resp(payload={"data": {"environmentLogs": [
            "not a dict", log_item("好的一行"), None]}}))
        lines = railway.fetch_logs(cfg(), post=post)
        self.assertEqual([line.message for line in lines], ["好的一行"])

    def test_missing_data_key_is_an_empty_list(self):
        post, _ = sender(resp(payload={"data": {}}))
        self.assertEqual(railway.fetch_logs(cfg(), post=post), [])


class TestRedaction(unittest.TestCase):
    def test_keys_are_removed_before_the_text_reaches_the_caller(self):
        secret = "sk-live-abcdefghijklmnop"
        post, _ = sender(resp(payload=envelope(
            [log_item(f"auth failed for {secret}")])))
        lines = railway.fetch_logs(cfg(), secrets=[secret], post=post)
        self.assertNotIn(secret, lines[0].message)
        self.assertIn("***", lines[0].message)

    def test_partial_leaks_are_also_masked(self):
        """上游的错误话术常常只回显首尾片段。"""
        secret = "sk-live-abcdefghijklmnop"
        masked = railway.redact(f"key {secret[:8]}... tail {secret[-8:]}", [secret])
        self.assertNotIn(secret[:8], masked)
        self.assertNotIn(secret[-8:], masked)

    def test_short_values_are_not_treated_as_secrets(self):
        """一个三字符的「密钥」会把正常文本打成马赛克。"""
        self.assertEqual(railway.redact("正常的一行日志 abc", ["abc"]),
                         "正常的一行日志 abc")

    def test_errors_are_redacted_too(self):
        secret = "sk-live-abcdefghijklmnop"
        post, _ = sender(resp(payload={"errors": [{"message": f"bad token {secret}"}]}))
        with self.assertRaises(railway.RailwayError) as ctx:
            railway.fetch_logs(cfg(), secrets=[secret], post=post)
        self.assertNotIn(secret, str(ctx.exception))


class TestTimestamps(unittest.TestCase):
    def test_rfc3339_with_z(self):
        moment = railway.parse_timestamp("2026-08-26T12:00:00Z")
        self.assertEqual(moment.year, 2026)
        self.assertIsNotNone(moment.tzinfo)

    def test_extra_fractional_digits_do_not_crash(self):
        """有些实现给的小数秒超过 6 位，fromisoformat 在 3.11 上会拒绝。"""
        self.assertIsNotNone(railway.parse_timestamp("2026-08-26T12:00:00.123456789Z"))

    def test_garbage_returns_none_instead_of_raising(self):
        """一行时间戳格式变了，不该让整个日志面板打不开。"""
        for junk in ("", "not-a-time", "2026-13-45T99:99:99Z"):
            self.assertIsNone(railway.parse_timestamp(junk))


class TestEventDetection(unittest.TestCase):
    def test_structured_events_are_parsed(self):
        payload = {"run_id": "r1", "event": "row", "status": "正常"}
        post, _ = sender(resp(payload=envelope([log_item(json.dumps(payload))])))
        lines = railway.fetch_logs(cfg(), post=post)
        self.assertEqual(lines[0].event["status"], "正常")

    def test_plain_text_is_not_an_event(self):
        post, _ = sender(resp(payload=envelope([log_item("⏱ sweep 开跑：…")])))
        self.assertIsNone(railway.fetch_logs(cfg(), post=post)[0].event)

    def test_json_without_run_id_is_not_ours(self):
        """上游偶尔会回显 JSON 片段。收进来就是凭空多出来的轮次。"""
        post, _ = sender(resp(payload=envelope(
            [log_item('{"level":"info","msg":"something else"}')])))
        self.assertIsNone(railway.fetch_logs(cfg(), post=post)[0].event)

    def test_broken_json_does_not_crash(self):
        post, _ = sender(resp(payload=envelope([log_item('{"run_id": "r1", broken')])))
        self.assertIsNone(railway.fetch_logs(cfg(), post=post)[0].event)


def structured(attrs, message="", **kwargs):
    """Railway 把 JSON 日志拆开之后的样子：正文空、字段全在 attributes 里。"""
    item = log_item(message, **kwargs)
    item["attributes"] = [{"key": k, "value": v} for k, v in attrs]
    return item


class TestRailwayStructuredLogs(unittest.TestCase):
    """Railway 会把一行 JSON 解析成结构化日志：message 字段当正文，其余字段变
    attributes。RUN_LOG_JSON 的事件没有 message 字段，所以正文是空的。
    只读正文的话，运行历史永远是「暂时没有可解析的运行记录」（线上踩过）。"""

    def fetch(self, items, secrets=()):
        post, _ = sender(resp(payload=envelope(items)))
        return railway.fetch_logs(cfg(), secrets=secrets, post=post)

    def test_json_encoded_attribute_values_rebuild_a_run(self):
        lines = self.fetch([
            structured([("run_id", '"1730000000-7"'), ("event", '"run_start"'),
                        ("ts", "1000.5"), ("mode", '"queue"'),
                        ("tables", '["A","B"]')], ts="2026-10-23T09:20:01Z"),
            structured([("run_id", '"1730000000-7"'), ("event", '"table"'),
                        ("ts", "1001"), ("table", '"A"'), ("rows", "2"),
                        ("counts", '{"正常":2}'), ("points_balance", "null")],
                       ts="2026-10-23T09:20:02Z"),
            structured([("run_id", '"1730000000-7"'), ("event", '"run_end"'),
                        ("ts", "1002"), ("mode", '"queue"'), ("exit_code", "0"),
                        ("error", '""'), ("rows", "2")], ts="2026-10-23T09:20:03Z"),
        ])
        runs = railway.build_runs(lines)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.run_id, run.mode, run.exit_code, run.rows),
                         ("1730000000-7", "queue", 0, 2))
        self.assertTrue(run.finished)
        self.assertEqual(run.tables[0].counts, {"正常": 2})
        self.assertIsNone(run.tables[0].points_balance)
        # 原始日志那一栏不是一片空白。
        self.assertIn("run_start", lines[0].message)

    def test_plain_string_attribute_values_work_too(self):
        """value 的编码没有文档：字符串不带引号也要认。"""
        lines = self.fetch([
            structured([("run_id", "1730000000-8"), ("event", "run_end"),
                        ("ts", "1002"), ("mode", "sweep"), ("exit_code", "0")]),
        ])
        self.assertEqual(lines[0].event["run_id"], "1730000000-8")
        self.assertEqual(lines[0].event["mode"], "sweep")
        self.assertEqual(lines[0].event["exit_code"], 0)

    def test_flattened_nested_keys_are_rebuilt(self):
        lines = self.fetch([
            structured([("run_id", "r"), ("event", "table"), ("table", "A"),
                        ("counts.正常", "3"), ("counts.风控中", "1"),
                        ("used_providers.tikhub", "4")]),
        ])
        self.assertEqual(lines[0].event["counts"], {"正常": 3, "风控中": 1})
        self.assertEqual(lines[0].event["used_providers"], {"tikhub": 4})

    def test_attribute_values_are_redacted(self):
        """run_end 的 error 会原样显示在面板上。"""
        secret = "sk-0123456789abcdefghijklmnopqrstuvwx"
        lines = self.fetch([
            structured([("run_id", "r"), ("event", "run_end"),
                        ("error", f'"上游回显 {secret}"')]),
        ], secrets=[secret])
        self.assertNotIn(secret[8:16], json.dumps(lines[0].event, ensure_ascii=False))
        self.assertNotIn(secret[8:16], lines[0].message)

    def test_attributes_without_run_id_are_not_ours(self):
        """Railway 自己也会给普通日志挂 attributes（比如 level）。"""
        lines = self.fetch([structured([("level", "error")], message="Traceback ...")])
        self.assertIsNone(lines[0].event)
        self.assertEqual(lines[0].message, "Traceback ...")

    def test_unparsed_json_message_still_wins(self):
        """不经过 Railway 解析的部署（VPS、GitHub Actions 的日志）照旧从正文读。"""
        payload = {"run_id": "r1", "event": "row", "status": "正常"}
        lines = self.fetch([structured([("level", "info")], message=json.dumps(payload))])
        self.assertEqual(lines[0].event["status"], "正常")


def ev(run_id, event, ts, **extra):
    return railway.LogLine(event={"run_id": run_id, "event": event, "ts": ts, **extra})


class TestBuildRuns(unittest.TestCase):
    def test_a_complete_run_is_reassembled(self):
        lines = [
            ev("r1", "run_start", 1000, mode="sweep", tables=["A", "B"]),
            ev("r1", "table", 1010, table="A", rows=5, cost_yuan=0.36,
               counts={"正常": 5}, used_providers={"tikhub": 5}, failovers=0),
            ev("r1", "row", 1011, record_id="rec1", status="正常"),
            ev("r1", "table", 1020, table="B", rows=2, cost_yuan=0.14,
               counts={"正常": 2}),
            ev("r1", "run_end", 1030, mode="sweep", exit_code=0, rows=7,
               cost_yuan=0.50),
        ]
        runs = railway.build_runs(lines)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.mode, "sweep")
        self.assertEqual(run.rows, 7)
        self.assertAlmostEqual(run.cost_yuan, 0.50)
        self.assertEqual([t.label for t in run.tables], ["A", "B"])
        self.assertTrue(run.finished)
        self.assertTrue(run.ok)

    def test_the_balance_reaches_the_run_from_the_table_event(self):
        """「跑完剩」那一列的值。

        **踩过的坑**：`points_balance` 是 runner 在**表级**事件里发的
        （runner.py 的 EVENT_TABLE），而这里原来只从 `run_end` 里读，
        `cli.py` 发 `run_end` 时又没带它——三处对不上，值根本传不过来，
        「跑完剩」永远显示「—」。而余额对账（docs/待验证清单.md 17c）
        整个靠这一列，等于那条验收做不了。
        """
        lines = [
            ev("r1", "run_start", 1000, mode="sweep"),
            ev("r1", "table", 1010, table="A", rows=5, cost_yuan=0.36,
               points_balance=4200),
            ev("r1", "run_end", 1030, mode="sweep", exit_code=0, rows=5,
               cost_yuan=0.36),
        ]
        run = railway.build_runs(lines)[0]
        self.assertEqual(run.points_balance, 4200)
        self.assertAlmostEqual(run.points_yuan, 42.0)

    def test_run_end_wins_over_the_table_events(self):
        """收尾事件带了就以它为准——它是这一轮**最后**看到的余额。"""
        lines = [
            ev("r1", "run_start", 1000),
            ev("r1", "table", 1010, table="A", points_balance=4200),
            ev("r1", "run_end", 1030, exit_code=0, points_balance=4100),
        ]
        self.assertEqual(railway.build_runs(lines)[0].points_balance, 4100)

    def test_the_lowest_balance_wins_across_tables(self):
        """一轮刷几张表，每张报一次余额。取最小的那个——和 runner 内部
        合并同一张表的多次读数时一样，宁可低报。"""
        lines = [
            ev("r1", "run_start", 1000),
            ev("r1", "table", 1010, table="A", points_balance=4200),
            ev("r1", "table", 1020, table="B", points_balance=3900),
            ev("r1", "run_end", 1030, exit_code=0),
        ]
        self.assertEqual(railway.build_runs(lines)[0].points_balance, 3900)

    def test_a_run_with_no_balance_anywhere_stays_none(self):
        """全走 TikHub 的轮子读不到积分余额。那要显示「—」，不是 ¥0。"""
        lines = [ev("r1", "run_start", 1000),
                 ev("r1", "table", 1010, table="A"),
                 ev("r1", "run_end", 1030, exit_code=0)]
        self.assertIsNone(railway.build_runs(lines)[0].points_balance)

    def test_row_events_are_not_expanded(self):
        """一轮几百行，全塞进内存只为了在页面上显示一个数字。"""
        lines = [ev("r1", "run_start", 1000)]
        lines += [ev("r1", "row", 1000 + i, record_id=f"rec{i}") for i in range(500)]
        lines.append(ev("r1", "run_end", 2000, exit_code=0, rows=500))
        run = railway.build_runs(lines)[0]
        self.assertEqual(run.tables, [])
        self.assertEqual(run.rows, 500)

    def test_a_run_without_an_end_is_not_ok(self):
        """有 run_start 没 run_end = 被容器杀掉了（redeploy / 回收 / OOM），
        已经付过钱的结果多半丢了。这种轮子必须看得见。"""
        lines = [ev("r1", "run_start", 1000, mode="queue"),
                 ev("r1", "table", 1010, table="A", rows=3, cost_yuan=0.2)]
        run = railway.build_runs(lines)[0]
        self.assertFalse(run.finished)
        self.assertFalse(run.ok)
        # 汇总从表级事件凑出来，比一片空白有用
        self.assertEqual(run.rows, 3)
        self.assertAlmostEqual(run.cost_yuan, 0.2)

    def test_breaker_and_failovers_bubble_up_from_tables(self):
        lines = [
            ev("r1", "run_start", 1000),
            ev("r1", "table", 1010, table="A", breaker_tripped=True, failovers=2),
            ev("r1", "table", 1020, table="B", failovers=1),
            ev("r1", "run_end", 1030, exit_code=0),
        ]
        run = railway.build_runs(lines)[0]
        self.assertTrue(run.breaker_tripped)
        self.assertEqual(run.failovers, 3)
        self.assertFalse(run.ok, "熔断了就不该显示成「没事」")

    def test_nonzero_exit_is_not_ok(self):
        lines = [ev("r1", "run_start", 1000), ev("r1", "run_end", 1010, exit_code=1)]
        self.assertFalse(railway.build_runs(lines)[0].ok)

    def test_runs_are_newest_first_and_capped(self):
        lines = []
        for i in range(80):
            lines.append(ev(f"r{i}", "run_start", 1000 + i))
            lines.append(ev(f"r{i}", "run_end", 1001 + i, exit_code=0))
        runs = railway.build_runs(lines, limit=10)
        self.assertEqual(len(runs), 10)
        self.assertEqual(runs[0].run_id, "r79")

    def test_non_event_lines_are_ignored(self):
        lines = [railway.LogLine(message="⏱ sweep 开跑"),
                 ev("r1", "run_start", 1000),
                 ev("r1", "run_end", 1010, exit_code=0)]
        self.assertEqual(len(railway.build_runs(lines)), 1)

    def test_an_event_without_a_run_id_is_dropped(self):
        self.assertEqual(railway.build_runs(
            [railway.LogLine(event={"event": "run_end"})]), [])

    def test_garbage_numbers_do_not_crash(self):
        lines = [ev("r1", "run_end", "not-a-number", rows="lots",
                    cost_yuan=None, exit_code="x")]
        run = railway.build_runs(lines)[0]
        self.assertEqual((run.rows, run.cost_yuan, run.exit_code), (0, 0.0, 0))


if __name__ == "__main__":
    unittest.main()
