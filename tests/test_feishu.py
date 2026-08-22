"""飞书读写层的测试。全部离线：用假的 transport 顶掉网络。

这一层是生产表唯一的写入路径，此前零测试覆盖。重点钉四件事：
读记录要翻页翻到底、写回要把坏行隔离出来而不是整批陪葬、
取 token 要能扛一次网络抖动、错误信息要带得上排查线索。
"""

import json
import unittest
from unittest import mock

from xhsearch import feishu, transport


def ok(payload: dict) -> transport.Response:
    return transport.Response(200, "application/json",
                              json.dumps({"code": 0, "data": payload}, ensure_ascii=False),
                              "req-ok")


def feishu_err(code: int, msg: str) -> transport.Response:
    return transport.Response(200, "application/json",
                              json.dumps({"code": code, "msg": msg}, ensure_ascii=False),
                              "req-err")


def make_table() -> feishu.Bitable:
    table = feishu.Bitable("app-id", "app-secret", "app-token", "table-id")
    # 测试不走真鉴权，直接塞一个没过期的 token。
    table._token = feishu._Token("test-token", float("inf"))
    return table


class TestSearchPagination(unittest.TestCase):
    def test_follows_page_token_to_the_end(self):
        pages = [
            ok({"items": [{"record_id": "rec1"}],
                "has_more": True, "page_token": "tok+special="}),
            ok({"items": [{"record_id": "rec2"}], "has_more": False}),
        ]
        urls: list[str] = []

        def fake_post(url, headers, body, timeout=30.0):
            urls.append(url)
            return pages[len(urls) - 1]

        with mock.patch.object(transport, "post", side_effect=fake_post), \
             mock.patch("time.sleep"):
            records = make_table().search(["链接"])

        self.assertEqual([r["record_id"] for r in records], ["rec1", "rec2"])
        self.assertEqual(len(urls), 2)
        # page_token 是服务端的不透明字符串，必须 URL 编码后再拼。
        self.assertIn("page_token=tok%2Bspecial%3D", urls[1])


class TestBatchUpdateIsolation(unittest.TestCase):
    """batch_update 是全成功或全失败：一个坏行不该让同分片的好行陪葬，
    更不该让后续分片全部丢掉。"""

    def run_update(self, updates, errors=None):
        def fake_post(url, headers, body, timeout=30.0):
            payload = json.loads(body)
            records = payload.get("records") or []
            if any(r.get("record_id") == "bad" for r in records):
                return feishu_err(1254005, "RecordIdNotFound")
            return ok({})

        with mock.patch.object(transport, "post", side_effect=fake_post), \
             mock.patch("time.sleep"):
            return make_table().batch_update(updates, errors=errors)

    def test_bad_row_is_isolated_and_good_rows_are_written(self):
        updates = [{"record_id": rid, "fields": {"x": 1}} for rid in ("a", "bad", "c", "d")]
        errors: list = []
        written = self.run_update(updates, errors)
        self.assertEqual(written, 3)
        self.assertEqual([rid for rid, _ in errors], ["bad"])
        self.assertEqual(errors[0][1].code, 1254005)

    def test_without_collector_raises_summary_after_trying_everything(self):
        updates = [{"record_id": rid, "fields": {"x": 1}} for rid in ("a", "bad", "c")]
        with self.assertRaises(feishu.FeishuError) as ctx:
            self.run_update(updates)
        self.assertIn("1 行写回失败", str(ctx.exception))
        self.assertIn("2 行已写回", str(ctx.exception))
        self.assertIn("bad", str(ctx.exception))

    def test_table_level_error_is_not_bisected(self):
        """表级错误（权限/列名）每个子分片都会同样失败：二分只会把一次失败
        放大成上千次请求。必须立刻抛出，而不是逐行捶打飞书接口。"""
        calls = {"n": 0}

        def fake_post(url, headers, body, timeout=30.0):
            calls["n"] += 1
            return feishu_err(91403, "Forbidden")

        updates = [{"record_id": rid, "fields": {"x": 1}} for rid in ("a", "b", "c", "d")]
        with mock.patch.object(transport, "post", side_effect=fake_post), \
             mock.patch("time.sleep"):
            with self.assertRaises(feishu.FeishuError) as ctx:
                make_table().batch_update(updates, errors=[])
        self.assertEqual(calls["n"], 1)          # 一次失败就收手，不二分
        self.assertEqual(ctx.exception.code, 91403)

    def test_all_good_rows_write_in_one_call(self):
        calls = {"n": 0}

        def fake_post(url, headers, body, timeout=30.0):
            calls["n"] += 1
            return ok({})

        with mock.patch.object(transport, "post", side_effect=fake_post):
            written = make_table().batch_update(
                [{"record_id": "a", "fields": {}}, {"record_id": "b", "fields": {}}])
        self.assertEqual(written, 2)
        self.assertEqual(calls["n"], 1)   # 没有失败就不该有二分开销


class TestTokenRetry(unittest.TestCase):
    def test_one_network_blip_does_not_kill_the_run(self):
        responses = [
            transport.Response(0, "", "网络错误：timed out"),
            transport.Response(200, "application/json",
                               json.dumps({"code": 0, "tenant_access_token": "tok-1",
                                           "expire": 7200})),
        ]
        with mock.patch.object(transport, "post", side_effect=responses), \
             mock.patch("time.sleep"):
            table = feishu.Bitable("a", "b", "c", "d")
            self.assertEqual(table.token(), "tok-1")


class TestErrorContext(unittest.TestCase):
    def test_non_json_response_keeps_status_and_request_id(self):
        """网关吐 HTML 时，「响应不是 JSON：None」等于把排查线索全丢了。"""
        bad = transport.Response(502, "text/html", "<html>bad gateway</html>", "rid-42")
        with mock.patch.object(transport, "post", return_value=bad), \
             mock.patch("time.sleep"):
            with self.assertRaises(feishu.FeishuError) as ctx:
                make_table().search(["链接"])
        text = str(ctx.exception)
        self.assertIn("502", text)
        self.assertIn("bad gateway", text)
        self.assertIn("rid-42", text)


class TestReadKeywords(unittest.TestCase):
    def test_multi_select_list(self):
        self.assertEqual(feishu.read_keywords(["西地那非口溶膜", "cGMP因子"]),
                         ["西地那非口溶膜", "cGMP因子"])

    def test_rich_text_list_is_not_swallowed(self):
        """单行文本列的返回形态是富文本分段（带 text 的对象）。
        按多选取会全部丢掉——关键词静默消失，蓝词命中永远不更新。"""
        value = [{"text": "西地那非口溶膜、cGMP因子", "type": "text"}]
        self.assertEqual(feishu.read_keywords(value),
                         ["西地那非口溶膜", "cGMP因子"])

    def test_plain_text_split_and_dedup(self):
        self.assertEqual(feishu.read_keywords("甲，乙、甲; 丙\n丁"),
                         ["甲", "乙", "丙", "丁"])

    def test_space_inside_keyword_survives(self):
        self.assertEqual(feishu.read_keywords("cGMP 因子、快充口溶膜"),
                         ["cGMP 因子", "快充口溶膜"])

    def test_empty(self):
        self.assertEqual(feishu.read_keywords(None), [])
        self.assertEqual(feishu.read_keywords(""), [])


class TestLoadRowsFieldFiltering(unittest.TestCase):
    """search 按名字请求列：请求一个还没建的列会让整个 search 报 1254045，
    一行都读不到——所以读侧也要按表里实际存在的列过滤。"""

    class _StubTable:
        def __init__(self):
            self.requested_fields = None

        def search(self, field_names, *, filter_spec=None, max_records=None):
            self.requested_fields = list(field_names)
            return []

    def test_missing_columns_are_not_requested(self):
        from xhsearch import runner
        from xhsearch.config import Settings

        settings = Settings()
        f = settings.fields
        table = self._StubTable()
        known = {f.link, f.publish_time, f.monitoring, f.queued,
                 f.traffic_status, f.comment_count, f.last_updated,
                 f.consecutive_failures, f.comment_status}   # 缺 点赞数/收藏数/蓝词字段
        runner.load_rows(table, settings, known_fields=known)
        self.assertNotIn(f.like_count, table.requested_fields)
        self.assertNotIn(f.collect_count, table.requested_fields)
        self.assertNotIn(f.seed_keywords, table.requested_fields)
        self.assertIn(f.link, table.requested_fields)

    def test_queue_mode_without_queued_column_does_nothing(self):
        """「排队刷新」列没建时 queue 模式必须空转——退化成无过滤全表刷新
        会花掉一整轮 sweep 的钱。"""
        from xhsearch import runner
        from xhsearch.config import Settings

        settings = Settings()
        table = self._StubTable()
        rows = runner.load_rows(table, settings, only_queued=True,
                                known_fields={settings.fields.link})
        self.assertEqual(rows, [])
        self.assertIsNone(table.requested_fields)   # 连 search 都没发


class TestFieldOptionsPagination(unittest.TestCase):
    def test_field_on_second_page_is_found(self):
        pages = [
            ok({"items": [{"field_name": "别的列"}],
                "has_more": True, "page_token": "t2"}),
            ok({"items": [{"field_name": "流量状态",
                           "property": {"options": [{"name": "爆贴"}, {"name": "大爆"}]}}],
                "has_more": False}),
        ]
        urls: list[str] = []

        def fake_get(url, headers, timeout=30.0):
            urls.append(url)
            return pages[len(urls) - 1]

        with mock.patch.object(transport, "get", side_effect=fake_get):
            options = make_table().list_field_options("流量状态")

        self.assertEqual(options, ["爆贴", "大爆"])
        # 列字段接口的上限是 100，超了会被拒。
        self.assertIn("page_size=100", urls[0])

    def test_unreadable_returns_none_not_empty(self):
        """None = 别过滤（宁可试着写）；空列表 = 全拦下。两者不能混。"""
        with mock.patch.object(transport, "get",
                               return_value=transport.Response(403, "", "denied")):
            self.assertIsNone(make_table().list_field_options("流量状态"))


if __name__ == "__main__":
    unittest.main()
