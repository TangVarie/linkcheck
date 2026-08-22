#!/usr/bin/env python3
"""把纯逻辑模块打包成一个自包含的文件，用于粘进扣子（Coze）代码节点。

扣子代码节点不能 import 本地模块，只能粘一整段。如果手工复制粘贴，判定口径
迟早会和服务端那份跑偏——白天点按钮和夜里批量跑结论不一致，是这类系统里最
难查也最伤信任的一类 bug。所以这里从同一份源码生成，永远只有一个真相。

    python3 tools/build_coze_node.py > coze_node.py

生成物里的网络层用 requests_async（扣子唯一允许的 HTTP 客户端；它禁用
http.client，所以 urllib 在那边不能用）。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "xhsearch"

# 依赖顺序。纯逻辑模块，全部不碰网络。
MODULES = ["config.py", "links.py", "protocol.py", "providers.py",
           "tags.py", "analyze.py", "rows.py"]

HEADER = '''"""
=============================================================================
  自动生成，请勿直接编辑。
  改动请改 xhsearch/ 下的源码，然后重新跑：
      python3 tools/build_coze_node.py > coze_node.py
=============================================================================

扣子（Coze）代码节点用。把整个文件粘进代码节点即可。

入参（开始节点配置）：
    mode        String   "sweep"（分层巡检）或 "row"（刷指定行）
    record_ids  String   逗号分隔的 record_id；mode=row 时必填
    tikhub_key  String   TikHub API Key（主通道；留空则只走 SocialDataX）
    api_key     String   SocialDataX API Key（备胎；留空则只走 TikHub）
    app_id      String   飞书自建应用 app_id
    app_secret  String   飞书自建应用 app_secret
    app_token   String   多维表格 app_token（URL 里 /base/ 后面那段）
    table_id    String   数据表 id

出参：全部是扁平标量。刻意不返回数组——飞书 HTTP 节点只能整体引用数组，
无法引用数组里的单个元素，返回数组等于在飞书侧不可用。
    ok          Boolean
    processed   Number
    credits     Number    本轮花费的「分」（¥0.01），跨供应商唯一能相加的单位
    balance     Number    SocialDataX 的积分余额；只走 TikHub 时是 0
    message     String

⚠️ tikhub_key 和 api_key 至少要填一个。两个都填就自动降级：
主通道网络故障 / Key 失效 / 余额耗尽时换另一家继续，而不是整轮停摆。

⚠️ 代码节点硬上限 60 秒。SOFT_DEADLINE 设成 45 秒，到点就停止派发新行，
未处理的行不写回「最后更新时间」，下一轮触发自然会重新捞起来——这就是断点续跑
的全部机制，不需要额外的队列。600 行按每轮 40 行算，约 30 分钟排干。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Optional, Protocol, Sequence

import requests_async as requests  # 扣子内置；禁用 http.client，所以不能用 urllib

SOFT_DEADLINE = 45.0
BATCH_LIMIT = 40  # 每轮最多处理多少行，按 60 秒上限反推
'''

FOOTER = '''

# =============================================================================
#  网络层：扣子专用（异步）
# =============================================================================

FEISHU_BASE = "https://open.feishu.cn/open-apis"


async def _post_json(url: str, headers: dict, payload: Any, timeout: float = 25.0):
    response = await requests.post(url, headers=headers, json=payload, timeout=timeout)
    return response


async def feishu_token(app_id: str, app_secret: str) -> str:
    response = await _post_json(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        {"Content-Type": "application/json; charset=utf-8"},
        {"app_id": app_id, "app_secret": app_secret},
    )
    data = json.loads(response.text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"取飞书 token 失败 [{data.get('code')}] {data.get('msg')}")
    return data["tenant_access_token"]


async def feishu_search(token, app_token, table_id, field_names, filter_spec,
                        page_size=200, deadline=None):
    """按条件拉记录，**自动翻页**——语义与服务端 feishu.Bitable.search 一致。

    ⚠️ 这里绝不能只取一页：过滤条件只有「监控中」，到期判断在取回后才做，
    而飞书的返回顺序和是否刷过无关。只取一页的话，每一轮看到的都是同样的
    前 N 条记录，第 N+1 条以后**永远轮不到刷新**且没有任何报错——
    「600 行按每轮 40 行排干」的断点续跑机制全靠这里拿到全量。

    deadline：到点就带着已取到的页返回（部分结果照常处理，剩下的行
    下一轮再捞）。扣子只有 60 秒，飞书响应慢的时候不能让翻页把
    处理和写回的预算吃光。
    """
    items, page_token = [], ""
    base = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    body = {"field_names": list(field_names), "automatic_fields": False}
    if filter_spec:
        body["filter"] = filter_spec
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return items
        url = f"{base}?page_size={page_size}"
        if page_token:
            url += "&page_token=" + _quote(page_token)
        response = await _post_json(url, {"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json; charset=utf-8"}, body)
        data = json.loads(response.text)
        if data.get("code") not in (0, None):
            raise RuntimeError(f"读表失败 [{data.get('code')}] {data.get('msg')}")
        payload = data.get("data") or {}
        items.extend(payload.get("items") or [])
        if not payload.get("has_more"):
            return items
        page_token = payload.get("page_token") or ""
        if not page_token:
            return items


async def feishu_field_options(token, app_token, table_id, field_name):
    """读某个多选字段已建的选项，读不到返回 None（= 不过滤，宁可试着写）。

    这层过滤在扣子路径上尤其重要：batch_update 是全成功或全失败，
    一个没建过的选项名（比如「大爆」没建，而某行评论数刚过 100）会让整批
    几十行一起回滚——而且每一轮都重算出同一个标签、每一轮都整批失败，
    钱照花、结果全丢，直到有人去把选项建好。服务端跑得了 doctor 能提前发现，
    扣子跑不了，只能在这里挡。
    """
    page_token = ""
    try:
        while True:
            url = (f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}"
                   f"/fields?page_size=100")
            if page_token:
                url += "&page_token=" + _quote(page_token)
            response = await requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
            data = json.loads(response.text)
            if data.get("code") not in (0, None):
                return None
            payload = data.get("data") or {}
            for item in payload.get("items") or []:
                if item.get("field_name") == field_name:
                    options = ((item.get("property") or {}).get("options")) or []
                    return [o["name"] for o in options if isinstance(o, dict) and o.get("name")]
            if not payload.get("has_more"):
                return None
            page_token = payload.get("page_token") or ""
            if not page_token:
                return None
    except Exception:                                  # noqa: BLE001
        return None


async def feishu_batch_update(token, app_token, table_id, updates):
    if not updates:
        return 0
    url = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    response = await _post_json(url, {"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=utf-8"},
                                {"records": updates})
    data = json.loads(response.text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"写回失败 [{data.get('code')}] {data.get('msg')}")
    return len(updates)


async def _one_shot(name: str, api_key: str, call: ToolCall, deadline: float):
    """打一次某一家的接口。

    ⚠️ 两家的业务错误都可能走 HTTP 200：SocialDataX 靠 body 里的 `code`，
    TikHub 靠 data 的形状。只看 status_code 的写法会把每一个「笔记已删除」
    当成成功。归一化和分类全在 providers 层，这里只负责发包。
    """
    provider = get_provider(name)
    request = provider.build(api_key, call.platform, call.purpose, call.arguments)
    try:
        if request.method == "GET":
            response = await requests.get(request.url, headers=request.headers, timeout=25.0)
        else:
            response = await requests.post(
                request.url, headers=request.headers,
                data=request.body.encode("utf-8"), timeout=25.0)
    except Exception as exc:                           # noqa: BLE001
        return Err(Failure.TRANSPORT, "network", f"{type(exc).__name__}: {exc}")

    content_type, request_id = "", ""
    try:
        content_type = response.headers.get("content-type", "") or ""
        request_id = response.headers.get("x-request-id", "") or ""
    except Exception:                                  # noqa: BLE001
        pass
    return provider.parse(call.platform, call.purpose,
                          getattr(response, "status_code", 0), content_type,
                          response.text, request_id, api_key)


async def channel_call(keys: dict, settings, call: ToolCall, semaphore, deadline: float,
                       disabled: set):
    """双通道：主通道倒下就换备胎，而不是让整轮停摆。

    返回 (结果, 计费明细 {供应商: 次数})。GONE 不触发降级——那是行级结论不是
    通道故障，换一家只会再花一次钱得到同一个答案。

    ⚠️ 计费明细里包含**失败但照样扣了钱**的调用：TikHub 对不存在的笔记
    「正常响应、正常计费」，只是 data 里是空壳。只记成功的话账面会比账单少。
    判据是「HTTP 成功、业务失败」+ 这家确实收这种钱（bills_failed_lookups）——
    401/400 那种没进业务层的对方明说不计费，SocialDataX 那边则是还没验过。
    """
    async with semaphore:
        if time.monotonic() >= deadline:
            return Err(Failure.TRANSPORT, "deadline", "已到软截止，留给下一轮"), {}

        usable = usable_order(settings.channels, call.platform, keys, disabled)
        # 再按「这家吃不吃这种参数形态」过滤：TikHub 的抖音端点只收数字
        # aweme_id 不收链接（图文/视频共用端点，形态无关），短链行让位给
        # 直接吃链接的 SocialDataX。
        order = [n for n in usable
                 if get_provider(n).can_handle(call.platform, call.purpose, call.arguments)]
        if not order:
            if usable:
                # 行级问题（比如抖音短链提不出 ID），不能报 AUTH——那是 FATAL。
                return Err(Failure.UNKNOWN, "unsupported_link",
                           f"当前可用通道（{'、'.join(usable)}）不支持这种链接形态："
                           "请贴含数字 ID 的完整链接，或配置 SocialDataX Key"), {}
            return Err(Failure.AUTH, "no_channel",
                       f"{call.platform} 没有可用的数据通道了", definitive=True), {}

        billed = {}
        last = Err(Failure.UNKNOWN, "no_attempt", "没有发起任何请求")
        for index, name in enumerate(order):
            result = await _one_shot(name, keys[name], call, deadline)
            charged = not isinstance(result, Err) or (
                get_provider(name).bills_failed_lookups
                and result.http_status is not None
                and 200 <= result.http_status < 300)
            if charged:
                billed[name] = billed.get(name, 0) + 1
            if not isinstance(result, Err):
                return result, billed
            last = result
            if result.kind in (Failure.AUTH, Failure.QUOTA):
                # 整轮拉黑：40 行各撞一次同样的 401 既慢又会把限流打出来。
                disabled.add(name)
            if result.kind not in FAILOVER_KINDS or index == len(order) - 1:
                return result, billed
        return last, billed


# =============================================================================
#  扣子入口
# =============================================================================


async def main(args: Any) -> dict:
    p = args.params
    settings = Settings()
    settings.soft_deadline_seconds = SOFT_DEADLINE
    f = settings.fields
    deadline = time.monotonic() + SOFT_DEADLINE
    now = datetime.now(timezone.utc)

    mode = (p.get("mode") or "sweep").strip()
    wanted = [x.strip() for x in (p.get("record_ids") or "").split(",") if x.strip()]
    if mode == "row" and not wanted:
        # 静默退化成「无过滤全表刷新」比报错贵得多也危险得多。
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0,
                "message": "mode=row 需要在 record_ids 里给出至少一个记录 ID"}

    try:
        token = await feishu_token(p["app_id"], p["app_secret"])
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0, "message": str(exc)}

    # 两个人机共用多选列的已建选项。读不到 = None = 不过滤（宁可试着写）。
    traffic_options = await feishu_field_options(
        token, p["app_token"], p["table_id"], f.traffic_status)
    status_options = await feishu_field_options(
        token, p["app_token"], p["table_id"], f.comment_status)

    filter_spec = None
    if mode != "row":
        filter_spec = {"conjunction": "and", "conditions": [
            {"field_name": f.monitoring, "operator": "is", "value": ["true"]}]}

    try:
        # 翻页预算最多给到软截止的一半：剩下的时间要留给取数和写回。
        records = await feishu_search(token, p["app_token"], p["table_id"],
                                      f.must_read(), filter_spec,
                                      deadline=deadline - SOFT_DEADLINE / 2)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0, "message": str(exc)}

    rows = []
    for record in records:
        record_id = record.get("record_id") or ""
        if wanted and record_id not in wanted:
            continue
        cells = record.get("fields") or {}
        row = Row(
            record_id=record_id,
            link_cell=_cell_text(cells.get(f.link)),
            publish_time_ms=_cell_ms(cells.get(f.publish_time)),
            expected_pinned=_cell_text(cells.get(f.expected_pinned)),
            current_tags=_cell_tags(cells.get(f.traffic_status)),
            previous_comment_count=_cell_int(cells.get(f.comment_count)),
            last_updated_ms=_cell_ms(cells.get(f.last_updated)),
            consecutive_failures=_cell_int(cells.get(f.consecutive_failures)) or 0,
            comment_status=_cell_tags(cells.get(f.comment_status)),
            queued=bool(cells.get(f.queued)),
        )
        if wanted or row.queued or row.is_due(settings, now):
            rows.append(row)
    rows = rows[:BATCH_LIMIT]

    if not rows:
        return {"ok": True, "processed": 0, "credits": 0, "balance": 0, "message": "没有到期的行"}

    keys = {k: v for k, v in {
        "tikhub": (p.get("tikhub_key") or "").strip(),
        "socialdatax": (p.get("api_key") or "").strip(),
    }.items() if v}
    if not keys:
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0,
                "message": "tikhub_key 和 api_key 至少要填一个"}

    semaphore = asyncio.Semaphore(settings.max_concurrency)
    # 本轮已确认不可用的通道。协程之间共享，只做「加一个字符串」这一种写入。
    disabled = set()
    results = await asyncio.gather(
        *[_process(row, keys, settings, now, semaphore, deadline, disabled,
                   traffic_options, status_options, wanted=bool(wanted))
          for row in rows],
        return_exceptions=True,
    )

    # fen 用 float 累加、最后一次取整——按单次调用四舍五入的话，
    # TikHub 抖音 0.72 分/次会被每次凑成 1 分，虚报近四成。
    updates, fen, balance, used = [], 0.0, 0, {}
    entries = []   # (update, 刷新状态) —— 熔断要能找到该撤销的那些行
    attempted = gone = fatal_rows = 0
    for row, result in zip(rows, results):
        if isinstance(result, Exception):
            continue
        fields, spent, bal, status, tally = result
        fen += spent
        balance = bal or balance
        # 熔断的分母只算真正打过上游的行：冷却/软截止顺延的行（fields None）
        # 和坏链接/不支持的链接形态（跳过）没有产生「取不到内容」的观测，
        # 混进去会稀释比例。
        if status in ("正常", "已失效", "疑似受限", "刷新失败"):
            attempted += 1
        if status in ("已失效", "疑似受限"):
            gone += 1
        if status == "fatal":
            fatal_rows += 1
        for name, count in tally.items():
            used[name] = used.get(name, 0) + count
        if fields:
            update = {"record_id": row.record_id, "fields": fields}
            updates.append(update)
            entries.append((update, status))

    # 全局熔断：一批里失效比例异常偏高 → 上游故障，撤销所有标签写入。
    # 判据与服务端 runner._apply_circuit_breaker 一致（含疑似受限、分母为
    # 实际尝试数），熔断后同样改写刷新状态并撤销计数增量——两条运行时对
    # 同一批数据必须给出同一个结论。
    tripped = attempted >= settings.safety.breaker_min_sample and \\
        gone / max(attempted, 1) > settings.safety.breaker_gone_ratio
    if tripped:
        note = (f"本批 {gone}/{attempted} 行都取不到内容，疑似上游故障而非内容失效，"
                "本轮不改流量状态，请稍后复查")
        for update, status in entries:
            update["fields"].pop(f.traffic_status, None)
            if status in ("已失效", "疑似受限"):
                update["fields"][f.refresh_status] = "刷新失败"
                update["fields"].pop(f.consecutive_failures, None)
                original = str(update["fields"].get(f.failure_reason) or "")
                update["fields"][f.failure_reason] = \\
                    (f"{original}；{note}" if original else note)[:500]

    cents = round(fen)
    try:
        written = await feishu_batch_update(token, p["app_token"], p["table_id"], updates)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": cents, "balance": balance,
                "message": f"算完了但写回失败：{exc}"}

    via = "、".join(f"{k} {v} 次" for k, v in sorted(used.items())) or "无调用"
    message = f"刷新 {written} 行，花费 ≈ ¥{fen / 100:.2f}（{via}）"
    if disabled:
        message += f"；⚠ 本轮 {'、'.join(sorted(disabled))} 通道不可用，已降级"
    if tripped:
        message += "；⚠ 本批失效比例异常，已熔断，未改流量状态"
    if fatal_rows:
        # Key 失效 / 余额耗尽把通道全打死了：必须报 ok:False 让下游告警分支
        # 接住——静默返回成功，半夜的故障就没人知道了。已完成的行照常写回。
        message += (f"；❌ 数据通道全部不可用（Key 失效或余额耗尽），"
                    f"{fatal_rows} 行未处理、未写回，修复后下一轮自动重捞")
        return {"ok": False, "processed": written, "credits": cents,
                "balance": balance, "message": message}
    return {"ok": True, "processed": written, "credits": cents,
            "balance": balance, "message": message}


async def _process(row, keys, settings, now, semaphore, deadline, disabled,
                   traffic_options, status_options, *, wanted):
    """单行处理。返回（待写字段|None, 花费的分(float), 余额, 刷新状态, 各通道调用次数）。

    待写字段为 None = 这一行**完全不写回**（冷却、软截止顺延、通道全灭）：
    最后更新时间保持原样，下一轮自然重捞——这是断点续跑的全部机制，
    所以「没轮到」绝不能写成「刷新失败」。
    """
    f = settings.fields
    if time.monotonic() >= deadline:
        return (None, 0.0, 0, "", {})
    if not row.parsed.usable:
        return (_base_fields(settings, "跳过", [row.parsed.describe_failure()], now),
                0.0, 0, "跳过", {})
    if not wanted and row.in_cooldown(settings, now):
        return (None, 0.0, 0, "", {})

    snapshot, error, fen, balance, tally = None, None, 0.0, 0, {}
    for call in plan_calls(row, settings, now):
        result, billed = await channel_call(keys, settings, call, semaphore,
                                            deadline, disabled)
        for name, count in billed.items():
            tally[name] = tally.get(name, 0) + count
            # 不在这里取整：0.72 分/次逐次凑成 1 分，账会虚高近四成。
            fen += count * get_provider(name).yuan_per_call(
                call.platform, call.purpose) * 100
        if isinstance(result, Err):
            if result.code == "deadline":
                if snapshot is None:
                    # 一个请求都没发出去：整行留给下一轮，不写回。
                    return (None, fen, balance, "", tally)
                break  # 评论已到手，detail 放弃，按已有数据完成本行
            if result.code == "unsupported_link":
                # 链接形态不被当前配置的通道支持（比如只配 TikHub 的抖音短链）：
                # 这是链接/配置问题不是上游观测——按「跳过」处理，
                # 不进熔断分母，不碰标签和定罪计数。
                return (_base_fields(settings, "跳过", [result.operator_text()], now),
                        fen, balance, "跳过", tally)
            if result.kind in FATAL:
                # 所有通道都是 AUTH/QUOTA：这是通道故障，不是这一行的结论。
                # 不写回——写「刷新失败」会顶掉最后更新时间、清掉排队勾，
                # 等于替一次 Key 故障惩罚了这一行。状态标 "fatal"，
                # 让 main 能把「Key 失效/余额耗尽」上报成 ok:False 而不是静默成功。
                return (None, fen, balance, "fatal", tally)
            if result.kind is Failure.GONE and not (
                    snapshot and (snapshot.comments or snapshot.comment_count)):
                # 死亡信号可能来自 detail（小红书 data 为 []、抖音 filter_list 命中）。
                # 但评论刚拿回一堆、detail 却说没了，那是上游自相矛盾，不能认。
                error = result
                snapshot = None
                break
            if call.purpose == "comments":
                error = result
                break
            continue
        balance = result.points_balance or balance
        if call.purpose == "comments":
            snapshot = read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            merge_detail(snapshot, result.data)

    if snapshot is None and error is not None and error.kind is Failure.GONE:
        strikes = (row.consecutive_failures or 0) + 1
        # 错误码 1008「内容已删除」是权威结论，规范明写不要重试 —— 直接定罪。
        convicted = error.definitive or strikes >= settings.safety.strikes_before_gone
        verdict = gone_verdict(settings, error.operator_text(),
                               current_tags=row.current_tags) if convicted \\
            else suspect_verdict(settings, strikes, error.operator_text())
        # 第一击不碰流量状态：这一轮没获得关于这篇笔记的任何新信息，
        # 摘掉上一轮的标签等于用一次失败抹掉真实结论。
        fields = _render(row, verdict, None, settings, now,
                         "已失效" if convicted else "疑似受限",
                         traffic_options, status_options, touch_tags=convicted)
        fields[f.consecutive_failures] = strikes
        return (fields, fen, balance, "已失效" if convicted else "疑似受限", tally)

    if snapshot is None:
        reason = error.operator_text() if error else "没有拿到任何数据"
        fields = _base_fields(settings, "刷新失败", [reason], now)
        # ⚠️ 刻意不动「连续失败次数」：超时/5xx/限流对「内容在不在」没有证据力，
        # 计进去会为下一次非权威 GONE 嫌疑预热定罪计数（与服务端 runner 同口径）。
        return (fields, fen, balance, "刷新失败", tally)

    verdict = decide(snapshot, settings,
                     previous_comment_count=row.previous_comment_count,
                     age_hours=row.age_hours(now),
                     expected_pinned=row.expected_pinned,
                     current_tags=row.current_tags,
                     current_comment_status=row.comment_status)
    if snapshot.censored:
        verdict.notes.append("⚠ 上游把这条标成了审核中/受限，请人工确认")
    fields = _render(row, verdict, snapshot, settings, now, "正常",
                     traffic_options, status_options)
    fields[f.consecutive_failures] = 0
    return (fields, fen, balance, "正常", tally)


def _base_fields(settings, status, notes, now):
    f = settings.fields
    return {
        f.refresh_status: status,
        f.failure_reason: "；".join(n for n in notes if n)[:500],
        f.last_updated: int(now.timestamp() * 1000),
        f.queued: False,
    }


def _render(row, verdict, snapshot, settings, now, status,
            traffic_options=None, status_options=None, touch_tags=True):
    f = settings.fields
    fields = _base_fields(settings, status, verdict.notes, now)
    if touch_tags:
        merged = merge(row.current_tags, verdict.tags, settings.tags.namespace(),
                       known_options=traffic_options,
                       exclusive=(settings.tags.heat_tiers(),))
        if merged.dropped_unknown:
            fields[f.failure_reason] = (
                fields[f.failure_reason]
                + f"；这些标签在「{f.traffic_status}」里还没建选项，已跳过："
                + "、".join(merged.dropped_unknown)
            )[:500]
        if merged.changed:
            fields[f.traffic_status] = merged.final
        wanted = comment_status_values(verdict.pin, row.comment_status, settings)
        if wanted is not None:
            merged_status = merge(row.comment_status, wanted,
                                  settings.comment_status.namespace(),
                                  known_options=status_options,
                                  exclusive=(settings.comment_status.namespace(),))
            if merged_status.dropped_unknown:
                fields[f.failure_reason] = (
                    fields[f.failure_reason]
                    + f"；这些值在「{f.comment_status}」里还没建选项，已跳过："
                    + "、".join(merged_status.dropped_unknown)
                )[:500]
            if merged_status.changed:
                fields[f.comment_status] = merged_status.final
    if row.parsed.platform:
        fields[f.platform] = "小红书" if row.parsed.platform == "xhs" else "抖音"
    if snapshot is not None:
        if snapshot.comment_count is not None:
            if row.previous_comment_count is not None:
                fields[f.previous_comment_count] = row.previous_comment_count
            fields[f.comment_count] = snapshot.comment_count
        if snapshot.like_count is not None:
            fields[f.like_count] = snapshot.like_count
        if snapshot.collect_count is not None:
            fields[f.collect_count] = snapshot.collect_count
        fields[f.pinned_comment] = format_pinned(snapshot)
        fields[f.comment_digest] = format_digest(snapshot, settings.digest)
    return fields


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            v if isinstance(v, str) else str((v or {}).get("text") or (v or {}).get("name") or "")
            for v in value
        )
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return str(value)


def _cell_tags(value):
    if isinstance(value, list):
        return [v if isinstance(v, str) else str((v or {}).get("name") or "")
                for v in value if v]
    return [value] if isinstance(value, str) and value else []


def _cell_int(value):
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _cell_ms(value):
    number = _cell_int(value)
    if number is None:
        return None
    return number * 1000 if number < 10_000_000_000 else number
'''

# 打包时要剥掉的行：包内相对 import、future import、以及各模块自己的 docstring 头。
_DROP = re.compile(r"^\s*(from\s+\.\S*\s+import|from\s+__future__\s+import|import\s+re$|"
                   r"import\s+json$|import\s+time$)")


def strip_module(text: str) -> str:
    """去掉模块 docstring 和会重复的 import，保留其余全部代码。"""
    # 去掉开头的模块 docstring
    text = re.sub(r'^\s*""".*?"""\s*', "", text, count=1, flags=re.S)
    kept = []
    for line in text.splitlines():
        if _DROP.match(line):
            continue
        # dataclass / typing / enum 的 import 统一放在头部了
        if re.match(r"^\s*from\s+(dataclasses|typing|enum|datetime)\s+import", line):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def build() -> str:
    parts = [HEADER]
    for name in MODULES:
        source = (PKG / name).read_text(encoding="utf-8")
        parts.append(f"\n\n# {'=' * 77}\n#  来自 xhsearch/{name}\n# {'=' * 77}\n")
        parts.append(strip_module(source))
    parts.append(FOOTER)
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    sys.stdout.write(build())
