"""飞书多维表格读写。

用企业自建应用 + tenant_access_token，不用扣子的官方飞书插件——后者走的是
用户级 OAuth，绑在某个具体人的飞书身份上，无人值守的定时任务里那个人离职、
改密码或撤销授权，整条链路就断了，而且断得很安静。

上线前必做的两件事，漏了必报错：
1. 应用要开 bitable:app 权限
2. 要在**那张多维表格**里「添加文档应用」把这个应用加成协作者
   （只发布应用不加协作者 → 99991672 权限错误）
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from . import transport

BASE = "https://open.feishu.cn/open-apis"

# 官方文档（app-table-record/batch_update）写明单次最多 500 条记录。
# 早期注释说「文档上限 1000，这里取一半」是过时信息：500 就是硬上限本身。
BATCH_SIZE = 500

# 值得二分定位的**行级**错误码：这一行的问题不影响别的行，隔离出来其余照写。
# 表级错误（权限 91403/99991672、列名 1254045、token 失效、网络中断）会让每个
# 子分片同样失败——对 500 行的分片，二分会把一次失败放大成近千次请求，
# 既拖垮任务超时又在捶打飞书接口，必须立刻向上抛。
#
# ⚠️ 这张表按官方 batch_update 文档校对过一次。改之前先去查文档，别猜：
# 1254291 曾被当成「多选选项不存在」而进了这个集合，那是**写冲突**——
# 二分会把一次冲突放大成 2n-1 次立即重试，恰好在冲突最严重的时候
# 用最大的请求量去捶打同一张表。
_ROW_LEVEL_CODES = frozenset({
    1254043,   # RecordIdNotFound：record_id 不存在（写回前行刚被删）—— 官方码
    1254005,   # 历史上见过的同义码，保留：它同样只影响这一行
    1254060,   # 字段值类型不对（某一行的坏值）
})

# 写冲突：同一张表被并发写、或请求过快。这是**整块**的问题，不是某一行的问题。
# 正确处置是整块退避后重发（带 jitter），而不是拆分——拆分只会制造更多并发。
WRITE_CONFLICT_CODE = 1254291
# 冲突重试次数与基准退避。总等待上限 ≈ base*(1+2+4)，够穿过一次人工批量编辑。
_CONFLICT_ATTEMPTS = 4
_CONFLICT_BASE_DELAY = 1.0

# 分页防循环：上游/代理反复返回同一个 page_token 时，原来的 while True
# 会把进程永远挂在这里。页数、总行数两道闸都要有。
#
# ⚠️ 两道闸必须对得上。页数闸写死 200 页时，默认 page_size=200 只能翻到
# 40,000 行——一张 45,000 行的合法大表会被页数闸拦下，而它明明没超过行数闸。
# 所以页数闸按「行数硬上限 ÷ 实际页大小」推出来，行数闸才是那个说了算的。
MIN_PAGES = 200
MAX_RECORDS_HARD_CAP = 50_000


def _page_limit(page_size: int) -> int:
    """翻多少页才算异常。留一点余量，容忍上游返回不满页。"""
    return max(MIN_PAGES, -(-MAX_RECORDS_HARD_CAP // max(1, page_size)) + 10)

# batch_get 官方单次上限 100 条 record_id。
BATCH_GET_SIZE = 100


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, hint: str = ""):
        self.code = code
        self.msg = msg
        super().__init__(f"飞书接口报错 [{code}] {msg}" + (f"\n → {hint}" if hint else ""))


# 高频踩坑的错误码，直接把解法写进异常里，省得回头翻文档。
_HINTS = {
    10003: "参数不合法：多半是 app_id / app_secret 填错或填串了（实测过的返回）",
    99991663: "app_id / app_secret 不对，或应用还没发布",
    99991672: "应用没有这张多维表格的权限：去表格右上角「…」→「添加文档应用」把应用加成协作者",
    1254005: "record_id 不存在，可能这行已被删除",
    1254043: "record_id 不存在（RecordIdNotFound），可能这行在写回前刚被删除",
    1254045: "字段名对不上：config.py 里的列名必须和表头逐字相同（含空格、括号全半角）",
    1254060: "字段值类型不对：数字列不能写字符串，日期列要写毫秒时间戳",
    1254063: "多选列的值转换失败（MultiSelectFieldConvFail）：某一列的**类型**和"
             "机器写进去的值形状对不上——最常见的是「流量状态」被建成了单选"
             "（机器按多选写列表），或者「评论状态」「负面状态」「置顶状态」"
             "被建成了多选（机器写字符串）。跑 "
             "`python3 cli.py doctor --table 表名` 会逐列指出是哪一列。"
             "这是**整表**的配置问题，不是某一行的问题，所以不二分",
    1254291: "写冲突：同一张表正在被并发写入或请求过快。"
             "检查是不是有两个调度器（queue/sweep/手动）同时在跑——"
             "所有付费入口必须共享同一个运行租约（见 xhsearch/runlock.py）",
    91403: "没有权限，检查应用权限范围和文档协作者设置",
}


def parse_code(raw: Any) -> int:
    """把响应里的 code 转成整数。转不了返回 -1，**绝不抛 ValueError**。

    上游/代理返回 `{"code":"oops"}` 时，裸的 int() 会抛 ValueError——
    那个异常不是 FeishuError，会绕过 CLI 的「按表隔离」直接把整轮打挂。
    """
    if isinstance(raw, bool):
        return -1
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1


def _raise_business_error(payload: dict[str, Any], resp: transport.Response) -> None:
    code = parse_code(payload.get("code"))
    msg = str(payload.get("msg", ""))
    if code == -1:
        # 保住原始 code 和 request_id：没有它们，厂商那边什么都查不了。
        msg = f"响应里的 code 不是数字（原文 {payload.get('code')!r}）：{msg}"
    request_id = str(payload.get("request_id") or resp.request_id or "")
    if request_id:
        msg = f"{msg}（request_id={request_id}）"
    raise FeishuError(code, msg, _HINTS.get(code, ""))


def _check(resp: transport.Response) -> dict[str, Any]:
    payload = resp.json()
    if not isinstance(payload, dict):
        # 非 JSON（网关错误页、连接中断）：把 HTTP 状态、body 片段和 request_id
        # 都带上——只报「响应不是 JSON：None」等于把排查线索全丢了。
        detail = f"HTTP {resp.status} {resp.body[:200]}".strip()
        if resp.request_id:
            detail += f"（request_id={resp.request_id}）"
        raise FeishuError(-1, f"响应不是 JSON：{detail}")
    code = payload.get("code")
    if code not in (0, None):
        _raise_business_error(payload, resp)
    return payload.get("data") or {}


@dataclass
class _Token:
    value: str
    expires_at: float


class Bitable:
    """一张多维表格的读写客户端。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        app_token: str,
        table_id: str,
        *,
        timeout: float = 30.0,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self.timeout = timeout
        self._token: Optional[_Token] = None

    # ---------- 鉴权 ----------

    def token(self) -> str:
        """tenant_access_token，有效期 7200 秒，提前 5 分钟续。

        飞书对重复请求会返回同一个未过期的 token，所以这里不需要跨进程缓存，
        每次冷启动重新取一次是安全的。
        """
        now = time.monotonic()
        if self._token and self._token.expires_at > now:
            return self._token.value

        # 带重试：token 是整轮的第一步，一次网络抖动就断送整轮太不值。
        resp = transport.post_with_retry(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}),
            timeout=self.timeout,
            should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
        )
        payload = resp.json()
        if not isinstance(payload, dict):
            raise FeishuError(-1, f"取 token 失败：HTTP {resp.status} {resp.body[:200]}")
        code = payload.get("code")
        if code not in (0, None):
            _raise_business_error(payload, resp)

        value = payload.get("tenant_access_token")
        if not value:
            raise FeishuError(-1, f"响应里没有 tenant_access_token：{resp.body[:200]}")
        expire = int(payload.get("expire", 7200))
        self._token = _Token(value, now + max(expire - 300, 60))
        return value

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _url(self, suffix: str) -> str:
        return f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/{suffix}"

    # ---------- 读 ----------

    def search(
        self,
        field_names: Iterable[str],
        *,
        filter_spec: Optional[dict[str, Any]] = None,
        page_size: int = 200,
        max_records: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """按条件拉记录，自动翻页。

        field_names 一定要把「流量状态」和「评论数」带上——前者是合并多选的前提，
        后者是判定掉量的基线。少读一个就会写错。
        """
        collected: list[dict[str, Any]] = []
        page_token = ""
        fields = list(field_names)
        seen_tokens: set[str] = set()
        page_limit = _page_limit(min(page_size, 500))

        for page in range(page_limit):
            url = self._url(f"records/search?page_size={min(page_size, 500)}")
            if page_token:
                # page_token 是服务端生成的不透明字符串，可能带 +/= 等字符，必须编码。
                url += f"&page_token={urllib.parse.quote(page_token, safe='')}"
            body: dict[str, Any] = {"field_names": fields, "automatic_fields": False}
            if filter_spec:
                body["filter"] = filter_spec

            resp = transport.post_with_retry(
                url,
                self._headers(),
                json.dumps(body, ensure_ascii=False),
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            data = _check(resp)
            collected.extend(data.get("items") or [])

            if max_records is not None and len(collected) >= max_records:
                return collected[:max_records]
            if len(collected) > MAX_RECORDS_HARD_CAP:
                raise FeishuError(
                    -1,
                    f"一张表读回了超过 {MAX_RECORDS_HARD_CAP} 行仍未翻完，已中止。"
                    "要么表真的这么大（那就传 max_records 分批处理），"
                    "要么上游的分页出了问题",
                )
            if not data.get("has_more"):
                return collected
            page_token = data.get("page_token") or ""
            if not page_token:
                return collected
            if page_token in seen_tokens:
                # 上游/代理反复返回同一个 page_token + has_more=true：
                # 原来的 while True 会在这里无限翻页，把整个进程挂住——
                # Railway 的后续 cron 会因为「上一轮还在跑」被永远跳过。
                raise FeishuError(
                    -1,
                    f"分页出现重复的 page_token（第 {page + 1} 页），"
                    "上游分页状态异常，已中止本次读取",
                )
            seen_tokens.add(page_token)

        raise FeishuError(
            -1, f"分页超过 {page_limit} 页仍未结束，已中止本次读取（疑似上游分页异常）")

    def batch_get(self, record_ids: Iterable[str]) -> list[dict[str, Any]]:
        """按 record_id 定点读回若干行。官方单次上限 100 条，这里自动分批。

        两个用处：
        1. `row` 模式只刷指定几行——原来是把全表读回来再本地筛 ID，
           多表部署下每张表都要全量扫一遍。
        2. 写回前重读「流量状态」做乐观并发控制（见 runner._reconcile_tags）。

        返回的形状和 search() 一致（[{record_id, fields}, ...]）。
        表里已经不存在的 record_id 不会出现在结果里——调用方据此判断
        「这一行在我们跑的这几分钟里被删了」。
        """
        ids = [str(r) for r in record_ids if r]
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), BATCH_GET_SIZE):
            chunk = ids[start : start + BATCH_GET_SIZE]
            resp = transport.post_with_retry(
                self._url("records/batch_get"),
                self._headers(),
                json.dumps({"record_ids": chunk, "automatic_fields": False},
                           ensure_ascii=False),
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            data = _check(resp)
            out.extend(data.get("records") or data.get("items") or [])
        return out

    # ---------- 写 ----------

    def batch_update(
        self,
        updates: list[dict[str, Any]],
        *,
        errors: Optional[list[tuple[str, FeishuError]]] = None,
    ) -> int:
        """批量更新。updates 形如 [{"record_id": "...", "fields": {...}}, ...]

        串行发，不并行：飞书官方建议同一时刻对同一篇多维表格只做一个写操作。
        并行写会拿到写冲突（1254291），还可能写坏。

        飞书的批量写是全成功或全失败，失败分三类，处置完全不同：

        * **行级错误**（_ROW_LEVEL_CODES，比如写回前刚被删掉的 1254043）：
          二分定位，把坏行隔离出来单独失败，好行照写——一个坏行只该损失
          它自己，不该作废同分片的 499 个好行，更不该丢掉后续分片。
        * **写冲突**（1254291）：整块退避后重发，**绝不二分**。冲突是整块的
          问题，拆分只会在冲突最严重的时候用 2n-1 次立即重试去捶打同一张表。
        * **表级错误**（权限、列名、token）：立刻抛出。那种错误每个子分片都会
          同样失败，二分只是把一次失败放大成上千次请求。

        行级坏行收集进 errors（(record_id, FeishuError) 列表）；不传 errors
        时，所有分片都尝试完之后才抛一个汇总异常。
        """
        collected: list[tuple[str, FeishuError]] = []
        written = 0
        for start in range(0, len(updates), BATCH_SIZE):
            written += self._submit(updates[start : start + BATCH_SIZE], collected)
        if collected:
            if errors is not None:
                errors.extend(collected)
            else:
                first_id, first_exc = collected[0]
                raise FeishuError(
                    first_exc.code,
                    f"{len(collected)} 行写回失败（其余 {written} 行已写回）。"
                    f"第一个失败行 {first_id}：{first_exc.msg}",
                    _HINTS.get(first_exc.code, ""),
                )
        return written

    def _post_chunk(self, chunk: list[dict[str, Any]]) -> None:
        """把一个分片发出去。写冲突（1254291）在这里整块退避重试。

        重试次数固定、退避带 full jitter：多个写入方同时撞上同一张表时，
        固定退避会让它们在同一毫秒一起回来，冲突永远解不开。
        """
        last: Optional[FeishuError] = None
        for attempt in range(_CONFLICT_ATTEMPTS):
            resp = transport.post_with_retry(
                self._url("records/batch_update"),
                self._headers(),
                json.dumps({"records": chunk}, ensure_ascii=False),
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            try:
                _check(resp)
                return
            except FeishuError as exc:
                if exc.code != WRITE_CONFLICT_CODE:
                    raise
                last = exc
                if attempt == _CONFLICT_ATTEMPTS - 1:
                    break
                time.sleep(transport.backoff_delay(
                    attempt, _CONFLICT_BASE_DELAY, resp.retry_after))
        assert last is not None
        raise last

    def _submit(self, chunk: list[dict[str, Any]], errors: list[tuple[str, FeishuError]]) -> int:
        try:
            self._post_chunk(chunk)
            return len(chunk)
        except FeishuError as exc:
            if exc.code not in _ROW_LEVEL_CODES:
                # 表级错误和写冲突：二分只会放大失败，直接向上抛。
                # 写冲突已经在 _post_chunk 里退避重试过 _CONFLICT_ATTEMPTS 次。
                raise
            if len(chunk) == 1:
                errors.append((str(chunk[0].get("record_id") or ""), exc))
                return 0
            mid = len(chunk) // 2
            return self._submit(chunk[:mid], errors) + self._submit(chunk[mid:], errors)

    def fields_meta(self) -> Optional[dict[str, dict]]:
        """这张表全部字段的元数据：列名 → {"type", "ui_type", "options"}。

        读不到（权限/网络）返回 None。options 的三种取值必须区分：
        列表（可能为空）= 这是个选择类字段，列出已建的选项名；
        None = 这个字段没有「选项」概念（文本/数字/日期……）。

        doctor 用它做全量体检（列在不在、类型对不对、选项建没建），
        跑批入口用它一次拿全 列名清单 + 两个选择列的选项，省两次分页请求。
        """
        page_token = ""
        meta: dict[str, dict] = {}
        seen_tokens: set[str] = set()
        for _ in range(_page_limit(100)):
            # 列出字段接口的 page_size 上限是 100（比记录接口低），超了会被拒。
            url = self._url("fields?page_size=100")
            if page_token:
                url += f"&page_token={urllib.parse.quote(page_token, safe='')}"
            # 和记录接口用同一套重试（0/429/5xx + full jitter）。原来只在
            # status==0 时固定睡 1 秒重试一次：飞书一次 429 或 502 就让整份
            # 元数据变成 None（=不过滤），未建选项被放行就是行级写回失败。
            resp = transport.request_with_retry(
                "GET", url, self._headers(), "",
                timeout=self.timeout,
                should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
            )
            payload = resp.json()
            if not isinstance(payload, dict) or payload.get("code") not in (0, None):
                return None
            data = payload.get("data") or {}
            for field in data.get("items") or []:
                name = field.get("field_name")
                if not name:
                    continue
                prop = field.get("property") if isinstance(field.get("property"), dict) else {}
                options: Optional[list[str]] = None
                if isinstance(prop, dict) and "options" in prop:
                    # 空列表要保留：「建了选择列但一个选项都没配」和
                    # 「不是选择列」是两回事。
                    options = [o["name"] for o in (prop.get("options") or [])
                               if isinstance(o, dict) and o.get("name")]
                meta[str(name)] = {
                    "type": field.get("type"),
                    "ui_type": str(field.get("ui_type") or ""),
                    "options": options,
                }
            if not data.get("has_more"):
                return meta
            page_token = data.get("page_token") or ""
            if not page_token or page_token in seen_tokens:
                # 重复 token = 上游分页状态异常。返回 None（读不到）而不是
                # 死循环：调用方会把「元数据读不到」当成拒跑的理由。
                return meta if not page_token else None
            seen_tokens.add(page_token)
        return None

    def field_names(self) -> Optional[set[str]]:
        """这张表实际存在的全部列名；读不到返回 None（= 不过滤，宁可试着写）。

        用来在写回前挡掉表里还没建的机器列——按名字写一个不存在的列是
        **表级错误**（1254045），会让整批写回失败。挡下来的列在日志里提示，
        运营按提示建好列，下一轮自然补上。
        """
        meta = self.fields_meta()
        return None if meta is None else set(meta)

    def list_field_options(self, field_name: str) -> Optional[list[str]]:
        """读某个单选/多选字段已配置的选项，读不到返回 None。

        用来在写回前过滤掉字段里不存在的标签——batch_update 是全成功或全失败，
        一个没建过的选项名可能让整批几百行一起回滚。

        返回 None 表示「查不到，别过滤」而不是「没有选项」，两者必须区分：
        前者应放行（宁可试着写），后者应全部拦下。
        """
        meta = self.fields_meta()
        if meta is None or field_name not in meta:
            return None
        options = meta[field_name]["options"]
        # 沿用旧行为：列存在但不是选择类字段时返回 []（全拦）——
        # 往文本列里写多选列表本来就写不进去。
        return options if options is not None else []


# ---------- 单元格取值 / 赋值 ----------


# 机器写得进去的字段类型码。本项目写回构造的每一个值都落在这六种类型里
# （逐列的期望类型见 cli._expected_schema）。一列被建成别的类型
# ——人员、附件、超链接、公式、创建时间、最后更新时间——说明那一列
# 压根不是给机器写的，硬写必然失败。
_WRITABLE_TYPES = frozenset({1, 2, 3, 4, 5, 7})


def value_fits(field_type: Any, value: Any) -> bool:
    """这个值写进这个类型的列，形状对不对。

    存在的理由是一次线上事故：一张表里有一列的类型和机器写的值对不上，
    飞书回 1254063（MultiSelectFieldConvFail）。batch_update 是全成功或
    全失败，而这是**整表**的配置问题——4 行付了钱、0 行落表。写回前先按
    类型筛一遍，坏的那一列被单独摘掉，其余列照写。

    判断只看形状，不看内容（选项名是否已建由另一条路径过滤）：
    多选要列表、单选/文本要字符串、数字/日期要数（bool 不算数，
    Python 里 bool 是 int 的子类，但飞书的数字列不收 True）、
    复选框要布尔。类型不在可写清单里一律返回 False——判不了的时候
    宁可少写一列，也不要赌上整张表的写回。
    """
    code = parse_code(field_type)
    if code not in _WRITABLE_TYPES:
        return False
    if code == 4:
        return isinstance(value, list)
    if code == 7:
        return isinstance(value, bool)
    if code in (2, 5):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def read_text(value: Any) -> str:
    """多维表格的文本列读出来可能是字符串，也可能是富文本分段数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("name") or ""))
        return "".join(parts)
    if isinstance(value, dict):
        # 链接列形如 {"link": "...", "text": "..."}
        return str(value.get("link") or value.get("text") or "")
    return str(value)


def read_keywords(value: Any) -> list[str]:
    """读一组关键词：多选列直接取选项名；文本列按 顿号/逗号/分号/换行 拆分。

    刻意不按空格拆——「cGMP 因子」这种带空格的词组会被拆碎。
    单个词里的前后空白剥掉，空项丢弃，保序去重。
    """
    # 只有列表才可能是多选列（字符串或带 name 的对象）；纯字符串是文本列，
    # 必须走拆分（read_multi_select 会把整串包成单元素列表，等于不拆）。
    # 列表按多选取不到时再当富文本分段拼成整段文本去拆——
    # 否则单行文本列的关键词会静默消失。
    raw = read_multi_select(value) if isinstance(value, list) else []
    if not raw:
        raw = re.split(r"[，,、;；\n]+", read_text(value))
    seen: set[str] = set()
    out: list[str] = []
    for word in raw:
        word = (word or "").strip()
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def read_multi_select(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("name"):
                out.append(str(item["name"]))
        return out
    return []


def read_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def read_bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "1", "是")


# 时间戳的合法区间（毫秒）。2000-01-01 ~ 2100-01-01。
# 超出这个范围的值一律当成「这一格是脏数据」而不是「一个很久以前/以后的时间」：
# 后面 datetime.fromtimestamp() 对超大值会抛 OverflowError/OSError，
# 那个异常发生在选行阶段，会把整张表打挂。
MIN_TIMESTAMP_MS = 946_684_800_000
MAX_TIMESTAMP_MS = 4_102_444_800_000


def read_timestamp_ms(value: Any) -> Optional[int]:
    """日期列读出来是毫秒时间戳。超出合法区间的脏值返回 None（=这一格没有时间）。"""
    number = read_int(value)
    if number is None:
        return None
    # 有些表把日期存成秒，宽松兼容一下。
    number = number * 1000 if abs(number) < 10_000_000_000 else number
    if not MIN_TIMESTAMP_MS <= number <= MAX_TIMESTAMP_MS:
        return None
    return number
