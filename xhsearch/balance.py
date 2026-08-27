"""两家数据通道的**余额**。只打官方标明零费用的那两个端点。

## 为什么这件事值得单独一个模块

面板最硬的一条不变量是「**永远不发付费请求**」——它和 cron 是两个容器，
`runlock.py` 是文件锁拦不住跨容器的第二个付费执行者（钱花两份 + 人工标签
被旧快照覆盖 + 飞书写冲突）。余额查询是个请求，所以它必须被单独关起来，
而不是顺手塞进 `providers.py` 里蹭现成的请求管道——那等于把面板接到了
付费端点的同一根线上，以后谁改一行就可能顺着线漏过去。

所以这个模块：

* **不 import `providers` / `runner` / `protocol`**（有测试钉死）
* 只认 `FREE_ENDPOINTS` 里那两条路径，别的一律拼不出来
* 不做重试之外的任何编排，不写任何东西，不碰飞书

## 「免费」不是我说的，是它们自己的机器可读定义说的

| 通道 | 端点 | 出处 | 零费用的证据 |
|---|---|---|---|
| TikHub | `/api/v1/tikhub/user/get_user_info` | `get_all_endpoints_info`（免鉴权） | `endpoint_cost: 0.0` |
| SocialDataX | `/socialdatax/api/v1/points/balance` | `docs/openapi.json` | `x-socialdatax-cost-points: 0`、`"余额查询不扣积分。"` |

对照组（我们真正在花钱的那两个）：TikHub `get_note_comments` 是 `0.01`，
SocialDataX `xhs/note/comment/list` 是 `10` 积分。差别是明摆着的，不是推测。

核对日期 `FREE_CHECKED_ON`。价格表会变——哪天这两个端点开始收费，
这里的注释就是发现它的线索，`tools/probe_endpoint.py` 那套 401/404 判据同样适用。

## 读不到的时候报「读不到」，不报 0

和「缺列时 estimate 报 ¥0.00」「读到 0 行渲染成健康卡片」是同一类错误：
一个 0 在最该警惕的时刻长得像「一切正常」。余额尤其如此——**真的余额为 0**
和**读不到余额**要人做的事完全相反（一个是去充值，一个是去查 Key）。
所以 `Balance.error` 非空时，`amount` 一律是 None，页面上显示的是原因。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from . import transport

# 最后一次对着两家的机器可读定义核对「这两个端点零费用」的日期。
FREE_CHECKED_ON = "2026-08-26"

TIKHUB = "tikhub"
SOCIALDATAX = "socialdatax"

# **唯一允许打的两条路径。** 写成常量而不是拼字符串，是为了让
# 「面板会打哪些端点」这件事能被一个 grep 和一条测试完全回答。
FREE_ENDPOINTS = {
    TIKHUB: "/api/v1/tikhub/user/get_user_info",
    SOCIALDATAX: "/points/balance",
}

# 1 积分 = ¥0.01。和 providers.SOCIALDATAX_YUAN（10 积分/次 = ¥0.10）同源，
# 但这里不 import providers——那是这个模块存在的全部意义。
YUAN_PER_POINT = 0.01

_TIMEOUT = 15.0
_ATTEMPTS = 2


@dataclass
class Balance:
    """一家通道的余额。`error` 非空时 `amount` 一定是 None。"""

    channel: str
    label: str = ""
    # 原始数额，单位见 `unit`。读不到时是 None——**不是 0**。
    amount: Optional[float] = None
    unit: str = ""
    # 折算成人民币。TikHub 那边要乘汇率，所以可能和 amount 不同数量级。
    yuan: Optional[float] = None
    # 换算用的汇率，仅 TikHub 有。写出来是为了让页面上那个 ≈¥ 有据可查。
    rate: Optional[float] = None
    # 赠送额度（TikHub 的 free_credit）。没有这个概念的通道留 None。
    free_credit: Optional[float] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.amount is not None

    def describe(self) -> str:
        if self.error:
            return f"{self.label}：读不到（{self.error}）"
        if self.amount is None:
            return f"{self.label}：读不到"
        if self.unit == "USD":
            return f"{self.label}：${self.amount:.2f} ≈ ¥{self.yuan:.2f}"
        return f"{self.label}：{self.amount:.0f} 积分 ≈ ¥{self.yuan:.2f}"


def _headers(api_key: str) -> dict:
    """两家共用的请求头。**一处定义**——分两处写正是 UA 漏掉的原因。

    `User-Agent` 不是可选的：TikHub 挡在 Cloudflare 后面按 UA 拦截，
    不带它直接 403（Cloudflare 1010），而 403 在页面上长得像「Key 不对」。
    """
    return {"Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": transport.BROWSER_UA}


def _url(base: str, channel: str) -> str:
    """拼地址。路径只能从 `FREE_ENDPOINTS` 里取，拼不出别的来。"""
    return f"{base.rstrip('/')}{FREE_ENDPOINTS[channel]}"


def _payload(response: transport.Response) -> tuple[Optional[dict], str]:
    """把响应变成 (dict, 错误话术)。两者恰有一个非空。"""
    if response.status == 0:
        return None, response.body or "请求没发出去"
    if response.status in (401, 403):
        return None, f"HTTP {response.status}：Key 不对或没权限"
    if not response.ok:
        return None, f"HTTP {response.status}"
    try:
        data = json.loads(response.body)
    except (json.JSONDecodeError, TypeError):
        return None, "返回的不是 JSON"
    if not isinstance(data, dict):
        return None, "返回的不是一个对象"
    return data, ""


def read_tikhub(api_key: str, *, base: str, usd_to_cny: float,
                get=None) -> Balance:
    """TikHub 余额。端点零费用（见模块开头那张表）。

    `balance` 的单位是**美元**——和计价表里的 `endpoint_cost` 同一单位，
    2026-08-26 由使用者对着 TikHub 后台确认过。页面上原值和折算值并排显示：
    汇率是我们自己配的（`USD_TO_CNY`，默认 7.2），折算值随它漂，
    原值不会。

    `free_credit`（赠送额度）**不算进余额**，也不算进「还够跑多久」——
    宁可低报。
    """
    out = Balance(channel=TIKHUB, label="TikHub", unit="USD", rate=usd_to_cny)
    if not api_key:
        out.error = "没配 TIKHUB_API_KEY"
        return out
    fetch = get or _get_with_retry
    response = fetch(_url(base, TIKHUB), _headers(api_key))
    data, problem = _payload(response)
    if problem:
        out.error = problem
        return out
    user = data.get("user_data")
    if not isinstance(user, dict) or user.get("balance") is None:
        out.error = "返回里没有 user_data.balance"
        return out
    try:
        out.amount = float(user["balance"])
    except (TypeError, ValueError):
        out.error = f"balance 不是数字：{user['balance']!r}"
        return out
    out.yuan = out.amount * usd_to_cny
    credit = user.get("free_credit")
    if credit is not None:
        try:
            out.free_credit = float(credit)
        except (TypeError, ValueError):
            pass
    # 账户被停用时余额可能还很好看，但一个请求都发不出去——那比余额低更急。
    if user.get("account_disabled") is True:
        out.error = "账户被停用（余额读到了，但调不动接口）"
        out.amount = None
    return out


def read_socialdatax(api_key: str, *, base: str, get=None) -> Balance:
    """SocialDataX 积分余额。OpenAPI 明写「余额查询不扣积分」。"""
    out = Balance(channel=SOCIALDATAX, label="SocialDataX", unit="积分")
    if not api_key:
        out.error = "没配 SOCIALDATAX_API_KEY"
        return out
    fetch = get or _get_with_retry
    response = fetch(_url(base, SOCIALDATAX), _headers(api_key))
    data, problem = _payload(response)
    if problem:
        out.error = problem
        return out
    # 有的部署把业务体包在 data 里，有的直接平铺。两种都认，认不出就报出来。
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    if body.get("balance") is None:
        out.error = "返回里没有 balance"
        return out
    try:
        out.amount = float(body["balance"])
    except (TypeError, ValueError):
        out.error = f"balance 不是数字：{body['balance']!r}"
        return out
    out.yuan = out.amount * YUAN_PER_POINT
    return out


def _get_with_retry(url: str, headers: dict) -> transport.Response:
    """重试次数刻意比付费路径少：余额读不到只是页面上少个数字，
    不值得为它把上游的限速吃掉（TikHub 这个端点是 1/second）。"""
    return transport.request_with_retry(
        "GET", url, headers, timeout=_TIMEOUT, attempts=_ATTEMPTS)


def read_all(api_keys: dict, *, tikhub_base: str, socialdatax_base: str,
             usd_to_cny: float, get=None) -> list[Balance]:
    """两家都读一遍。**一家读不到不影响另一家**——那两个账户是独立的，
    合并成一个「读余额失败」会让人分不清该去哪家后台看。
    """
    return [
        read_tikhub(api_keys.get(TIKHUB) or "", base=tikhub_base,
                    usd_to_cny=usd_to_cny, get=get),
        read_socialdatax(api_keys.get(SOCIALDATAX) or "",
                         base=socialdatax_base, get=get),
    ]


def runway_days(balances: list, yuan_per_day: float) -> Optional[float]:
    """按最近的实际花速，余额还够跑几天。算不出返回 None。

    只把**读到了的**通道算进去：读不到的那家当 0 会低报，当无穷会高报，
    两个都是错的。页面上另外说明「有一家没算进来」。

    这个数不发任何请求——花速来自 Railway 日志里已有的 run 历史。
    """
    usable = [b.yuan for b in balances if b.ok and b.yuan is not None]
    if not usable or yuan_per_day <= 0:
        return None
    return sum(usable) / yuan_per_day
