"""建列 / 补选项 / 新增记录。全部离线，不发任何请求。

这个文件盯的是三条纪律：
* 建列是**纯追加**，不碰已有列
* 补选项**只增不减**，而且现有选项的 id 要原样带回去（飞书按 id 认选项，
  只按名字重建一遍会把用到旧选项的单元格连值一起删掉）
* batch_create 的幂等键是**必填**，不然一次超时重试就多出一批重复行
"""

import json
import unittest
import uuid
from unittest import mock

from xhsearch import feishu, transport


def ok(payload=None):
    return transport.Response(status=200, content_type="application/json",
                              body=json.dumps({"code": 0, "data": payload or {}}))


def table():
    return feishu.Bitable(app_id="cli_x", app_secret="s",
                          app_token="bascnA", table_id="tblB")


def option(name, oid, color=0):
    return {"id": oid, "name": name, "color": color}


class Captured:
    """记下发出去的请求，按顺序回预设的响应。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args, **kwargs):
        # transport.post_with_retry(url, headers, body, ...) /
        # transport.request_with_retry(method, url, headers, body, ...)
        if args and args[0] in ("PUT", "DELETE", "GET", "POST"):
            method, url, _headers, body = args[0], args[1], args[2], args[3]
        else:
            method, url, _headers, body = "POST", args[0], args[1], args[2]
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body) if body else None})
        return self.responses.pop(0) if self.responses else ok()


class TestCreateField(unittest.TestCase):
    def test_posts_the_body_and_returns_the_field_id(self):
        cap = Captured(ok({"field": {"field_id": "fld123"}}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            got = table().create_field({"field_name": "负面状态", "type": 3})
        self.assertEqual(got, "fld123")
        self.assertTrue(cap.calls[0]["url"].endswith("/tables/tblB/fields"))
        self.assertEqual(cap.calls[0]["body"]["field_name"], "负面状态")

    def test_business_error_raises(self):
        bad = transport.Response(status=200, content_type="application/json",
                                 body=json.dumps({"code": 1254302, "msg": "no"}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", Captured(bad)):
            with self.assertRaises(feishu.FeishuError) as ctx:
                table().create_field({"field_name": "x", "type": 1})
        self.assertIn("高级权限", ctx.exception.hint)


class TestAddFieldOptions(unittest.TestCase):
    """`PUT fields` 对 property 是整体覆盖，飞书按 id 认选项。
    这一组用例全是在防「补一个选项，结果清空了整列」。"""

    def _raw(self, options, type_code=4):
        return {"流量状态": {"field_id": "fld1", "field_name": "流量状态",
                             "type": type_code,
                             "property": {"options": options}}}

    def test_existing_options_are_sent_back_with_their_ids(self):
        cap = Captured(ok())
        raw = self._raw([option("爆贴", "optA"), option("大爆", "optB", 3)])
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=raw), \
             mock.patch.object(transport, "request_with_retry", cap):
            added = table().add_field_options("流量状态", ["风控中"])
        self.assertEqual(added, ["风控中"])
        sent = cap.calls[0]["body"]["property"]["options"]
        self.assertEqual(sent[0], {"id": "optA", "name": "爆贴", "color": 0},
                         "旧选项必须原样带回去，id 和 color 都要在")
        self.assertEqual(sent[1], {"id": "optB", "name": "大爆", "color": 3})
        self.assertEqual(sent[2], {"name": "风控中"}, "新选项只给 name，id 由飞书分配")

    def test_it_is_a_put_on_the_field_id(self):
        cap = Captured(ok())
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw",
                               return_value=self._raw([option("爆贴", "optA")])), \
             mock.patch.object(transport, "request_with_retry", cap):
            table().add_field_options("流量状态", ["风控中"])
        self.assertEqual(cap.calls[0]["method"], "PUT")
        self.assertTrue(cap.calls[0]["url"].endswith("/fields/fld1"))

    def test_field_name_and_type_are_resent(self):
        """PUT 是整体更新，只发 property 会把列名和类型打没。"""
        cap = Captured(ok())
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw",
                               return_value=self._raw([option("爆贴", "optA")])), \
             mock.patch.object(transport, "request_with_retry", cap):
            table().add_field_options("流量状态", ["风控中"])
        body = cap.calls[0]["body"]
        self.assertEqual(body["field_name"], "流量状态")
        self.assertEqual(body["type"], 4)

    def test_nothing_to_add_sends_no_request(self):
        cap = Captured()
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw",
                               return_value=self._raw([option("爆贴", "optA")])), \
             mock.patch.object(transport, "request_with_retry", cap):
            self.assertEqual(table().add_field_options("流量状态", ["爆贴"]), [])
        self.assertEqual(cap.calls, [])

    def test_unknown_options_created_by_ops_are_preserved(self):
        """运营自己建的「爆帖预备」不在我们的清单里，绝不能被顺手删掉。"""
        cap = Captured(ok())
        raw = self._raw([option("爆帖预备", "optX"), option("已复盘", "optY")])
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=raw), \
             mock.patch.object(transport, "request_with_retry", cap):
            table().add_field_options("流量状态", ["风控中"])
        names = [o["name"] for o in cap.calls[0]["body"]["property"]["options"]]
        self.assertEqual(names, ["爆帖预备", "已复盘", "风控中"])

    def test_duplicate_requests_are_collapsed(self):
        cap = Captured(ok())
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw",
                               return_value=self._raw([])), \
             mock.patch.object(transport, "request_with_retry", cap):
            added = table().add_field_options("流量状态", ["风控中", "风控中", ""])
        self.assertEqual(added, ["风控中"])

    def test_refuses_when_the_current_options_cannot_be_read(self):
        """读不到现值就没有安全的写法——宁可不做。"""
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value=None):
            with self.assertRaises(feishu.FeishuError) as ctx:
                table().add_field_options("流量状态", ["风控中"])
        self.assertIn("读-改-写", str(ctx.exception))

    def test_refuses_on_a_missing_column(self):
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw", return_value={}):
            with self.assertRaises(feishu.FeishuError):
                table().add_field_options("流量状态", ["风控中"])

    def test_refuses_on_a_non_select_column(self):
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(feishu.Bitable, "fields_meta_raw",
                               return_value=self._raw([], type_code=1)):
            with self.assertRaises(feishu.FeishuError) as ctx:
                table().add_field_options("流量状态", ["风控中"])
        self.assertIn("不是单选/多选", str(ctx.exception))


UUID_A = "3f8a1c2e-5b64-4d7f-9a01-2c3d4e5f6071"


class TestIdempotencyKey(unittest.TestCase):
    """飞书的 client_token 只吃**标准 UUID**，格式不对整条 batch_create 就报
    `Invalid client token, make sure that it complies with the specification.`
    """

    def canonical_v4(self, token: str) -> None:
        """规范形式 8-4-4-4-12，**且版本位必须是 4**。

        飞书文档对 client_token 的原话是「格式为标准的 uuidv4」，
        示例 `fe599b60-450f-46ff-b2ef-9f6675625b97`；不合规是错误码 1254037
        `Invalid client token, make sure that it complies with the specification.`

        两个都要判，缺一条这测试就还是没牙：
        · 只判「解析得过」——`secrets.token_hex(16)` 那种没有连字符的
          32 位十六进制串 Python 照收，飞书不收；
        · 只判「规范形式」——UUIDv5 也是规范形式，但版本位是 5，飞书照样拒。
          **线上就是这么第二次红的。**
        """
        parsed = uuid.UUID(token)
        self.assertEqual(str(parsed), token, f"不是规范 UUID：{token!r}")
        self.assertEqual(parsed.version, 4,
                         f"飞书要 uuidv4，这个是 v{parsed.version}：{token!r}")

    def test_a_seed_becomes_a_canonical_uuid(self):
        raw = ("add-https://piqijafyg8a.feishu.cn/base/S72ObviZAa7P0AsueX1cpsYRnN6"
               "?table=tblXXXX-Hatherine 素人执行表单")
        self.canonical_v4(feishu.idempotency_key(raw))

    def test_the_same_seed_always_gives_the_same_uuid(self):
        """幂等键的全部意义在这里：连点两次「加」不该多出两行。"""
        self.assertEqual(feishu.idempotency_key("add-a:b-甲"),
                         feishu.idempotency_key("add-a:b-甲"))
        self.assertNotEqual(feishu.idempotency_key("add-a:b-甲"),
                            feishu.idempotency_key("add-a:b-乙"))

    def test_no_seed_gives_a_random_but_canonical_uuid(self):
        first, second = feishu.idempotency_key(), feishu.idempotency_key()
        self.canonical_v4(first)
        self.canonical_v4(second)
        self.assertNotEqual(first, second)


class TestBatchCreate(unittest.TestCase):
    def test_client_token_is_required(self):
        """transport 对超时是自动重试的，没有幂等键就会多出重复行。"""
        with self.assertRaises(TypeError):
            table().batch_create([{"fields": {}}])          # 位置参数都给不了
        with mock.patch.object(feishu.Bitable, "token", return_value="t"):
            with self.assertRaises(ValueError):
                table().batch_create([{"fields": {}}], client_token="")

    def test_a_key_that_is_not_a_uuid_is_refused_here_not_by_feishu(self):
        """和「忘了传」同一类事，同样应该在本地就炸。

        送出去只会换回一个飞书的 request_id——那次线上就是这么坏的：
        前端拼的 `add-<飞书URL>-<中文项目名>` 一路透传到飞书。
        """
        with mock.patch.object(feishu.Bitable, "token", return_value="t"):
            for bad in ("add-https://x.feishu.cn/base/b?table=t-甲",
                        "beee1b7a4d022a9f25a3b0bca0fd72c6",   # 没有连字符
                        "9a484430-9325-5b53-a1b6-962777ae53b4",  # 合法但是 v5
                        "uuid-1"):
                with self.assertRaises(ValueError, msg=bad) as ctx:
                    table().batch_create([{"fields": {}}], client_token=bad)
                self.assertIn("client_token", str(ctx.exception))

    def test_token_goes_on_the_url(self):
        cap = Captured(ok({"records": [{"record_id": "rec1"}]}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            got = table().batch_create([{"fields": {"名字": "甲"}}],
                                       client_token=UUID_A)
        self.assertEqual(got, ["rec1"])
        self.assertIn("client_token=" + UUID_A, cap.calls[0]["url"])

    def test_retrying_the_same_batch_reuses_the_key(self):
        """同一批内容重发必须用同一个键，否则飞书当成新的一批。"""
        keys = []
        for _ in range(2):
            cap = Captured(ok({"records": [{"record_id": "rec1"}]}))
            with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
                 mock.patch.object(transport, "post_with_retry", cap):
                table().batch_create([{"fields": {}}], client_token=UUID_A)
            keys.append(cap.calls[0]["url"])
        self.assertEqual(keys[0], keys[1])

    def test_chunks_get_distinct_keys(self):
        """整批共用一个键的话，第二片会被当成第一片的重发而整片丢掉。"""
        records = [{"fields": {"i": i}} for i in range(feishu.BATCH_CREATE_SIZE + 5)]
        cap = Captured(ok({"records": []}), ok({"records": []}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            table().batch_create(records, client_token=UUID_A)
        self.assertEqual(len(cap.calls), 2)
        self.assertNotEqual(cap.calls[0]["url"], cap.calls[1]["url"])

    def test_every_chunk_key_is_itself_a_canonical_uuid(self):
        """互不相同还不够——第二片原来是 `<uuid>-1000`，拼完就不是 UUID 了。
        分片只在超过 BATCH_CREATE_SIZE 时才走到，所以这条一直潜伏着。"""
        records = [{"fields": {"i": i}} for i in range(feishu.BATCH_CREATE_SIZE + 5)]
        cap = Captured(ok({"records": []}), ok({"records": []}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            table().batch_create(records, client_token=UUID_A)
        seen = set()
        for call in cap.calls:
            token = call["url"].split("client_token=")[1].split("&")[0]
            parsed = uuid.UUID(token)
            self.assertEqual(str(parsed), token, f"分片的键不是规范 UUID：{token!r}")
            self.assertEqual(parsed.version, 4, f"分片的键不是 v4：{token!r}")
            seen.add(token)
        self.assertEqual(len(seen), len(cap.calls))


class TestFieldsMetaRaw(unittest.TestCase):
    def test_raw_keeps_ids_while_summary_drops_them(self):
        items = [{"field_name": "流量状态", "type": 4, "ui_type": "MultiSelect",
                  "field_id": "fld1",
                  "property": {"options": [option("爆贴", "optA")]}}]
        with mock.patch.object(feishu.Bitable, "_fetch_fields", return_value=items):
            raw = table().fields_meta_raw()
            summary = table().fields_meta()
        self.assertEqual(raw["流量状态"]["field_id"], "fld1")
        self.assertEqual(raw["流量状态"]["property"]["options"][0]["id"], "optA")
        self.assertEqual(summary["流量状态"]["options"], ["爆贴"],
                         "摘要视图只留名字——所以补选项不能用它")

    def test_both_views_are_none_when_unreadable(self):
        with mock.patch.object(feishu.Bitable, "_fetch_fields", return_value=None):
            self.assertIsNone(table().fields_meta_raw())
            self.assertIsNone(table().fields_meta())


class TestWorkspace(unittest.TestCase):
    def test_create_base_returns_the_tokens(self):
        cap = Captured(ok({"app": {"app_token": "bascnNEW", "url": "https://x",
                                   "default_table_id": "tblNEW"}}))
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            got = feishu.Workspace("cli_x", "s").create_base("OKMAN四期")
        self.assertEqual(got["app_token"], "bascnNEW")
        self.assertTrue(cap.calls[0]["url"].endswith("/bitable/v1/apps"))
        self.assertEqual(cap.calls[0]["body"], {"name": "OKMAN四期"})

    def test_create_table_sends_all_fields_at_once(self):
        """一次带上列，比先建空表再逐列 POST 少 20 次请求，
        也少一个「建到一半失败留下残表」的中间状态。"""
        cap = Captured(ok({"table_id": "tblNEW"}))
        fields = [{"field_name": "反馈链接", "type": 1},
                  {"field_name": "是否巡查", "type": 7}]
        with mock.patch.object(feishu.Bitable, "token", return_value="t"), \
             mock.patch.object(transport, "post_with_retry", cap):
            got = feishu.Workspace("cli_x", "s").create_table("bascnNEW", "监控", fields)
        self.assertEqual(got, "tblNEW")
        self.assertEqual(cap.calls[0]["body"]["table"]["fields"], fields)


if __name__ == "__main__":
    unittest.main()
