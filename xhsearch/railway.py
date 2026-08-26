"""Railway 日志：GraphQL 客户端 + 把结构化事件还原成运行历史。

面板要回答「上一轮到底发生了什么」，而巡检的日志在 Railway 那边。
这一层把两边接起来：拉 `environmentLogs`，从里面挑出
`RUN_LOG_JSON=1` 打到 stderr 的那些 JSON 行，重建成一轮一轮的摘要。

## 为什么用 environmentLogs 而不是 deploymentLogs

Railway 官方文档没写清 cron service 每次触发是**新建一个 deployment**
还是**复用同一个**。`environmentLogs` 是环境级的，返回的 `tags` 里带
`serviceId` / `deploymentId`——两种情况它都成立，而 `deploymentLogs`
要先知道 deployment id，猜错就一条也拉不到。

## 日志保留期是硬上限

Hobby 7 天 / Pro 30 天。**面板不另存一份**，所以「运行历史」最多往回看那么久。
要看月度趋势得另想办法（比如让 cron 把每轮摘要写进一张飞书表），
那是另一件事、另一笔工。

## 脱敏不是可选项

日志里可能出现 Key（上游报错话术会回显参数、我们自己的错误里也带过片段）。
这些文本要送到浏览器，所以 `redact()` 必须在返回之前跑，
而且是**先脱敏再截断**——反过来的话 Key 横跨截断边界时会漏半截出去。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from . import transport

ENDPOINT = "https://backboard.railway.com/graphql/v2"

# 一次最多拉这么多行。Railway 的限流是 Hobby 1000 次/小时、10 次/秒，
# 面板走 TTL 缓存，一小时几十次，余量两个数量级——瓶颈是响应体大小不是次数。
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000

_QUERY = """query environmentLogs($environmentId: String!, $filter: String, $limit: Int) {
  environmentLogs(environmentId: $environmentId, filter: $filter, limit: $limit) {
    timestamp
    message
    severity
    tags { serviceId deploymentId }
  }
}"""


class RailwayError(RuntimeError):
    pass


class NotConfigured(RailwayError):
    """没配 Railway 凭据。这**不是错误**，只是这块功能没开。"""


@dataclass
class RailwayConfig:
    token: str
    environment_id: str
    service_id: str = ""
    timeout: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.environment_id)

    @staticmethod
    def from_env(environ: dict) -> "RailwayConfig":
        return RailwayConfig(
            token=(environ.get("RAILWAY_API_TOKEN") or "").strip(),
            environment_id=(environ.get("RAILWAY_ENVIRONMENT_ID") or "").strip(),
            service_id=(environ.get("RAILWAY_SERVICE_ID") or
                        environ.get("RAILWAY_CRON_SERVICE_ID") or "").strip(),
        )

    def missing(self) -> list[str]:
        """还差哪些变量。面板要照原样说给人听，而不是只显示「日志不可用」。"""
        gaps = []
        if not self.token:
            gaps.append("RAILWAY_API_TOKEN")
        if not self.environment_id:
            gaps.append("RAILWAY_ENVIRONMENT_ID")
        return gaps


@dataclass
class LogLine:
    timestamp: str = ""
    message: str = ""
    severity: str = ""
    service_id: str = ""
    deployment_id: str = ""
    # 这一行如果是结构化事件，解析出来的 dict 放这里；不是就是 None。
    event: Optional[dict] = None

    @property
    def moment(self) -> Optional[datetime]:
        return parse_timestamp(self.timestamp)


def parse_timestamp(raw: str) -> Optional[datetime]:
    """Railway 给的是 RFC3339。**解析失败返回 None，绝不抛异常**——
    一行时间戳格式变了不该让整个日志面板打不开。"""
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    # 有些实现给的小数秒超过 6 位，fromisoformat 在 3.11 上会拒绝。
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        text = f"{head}.{digits[:6]}{rest[len(digits):]}" if digits else head + rest[len(digits):]
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def redact(text: str, secrets: Iterable[str]) -> str:
    """把已知密钥从文本里抹掉。

    和 `providers._redact` 同一个思路，但这里要对**一组**密钥各抹一次：
    送到浏览器的日志可能同时带上游 Key、飞书 Secret 和 Railway token。
    太短的值不替换——一个三字符的「密钥」会把正常文本打成马赛克。
    """
    for secret in secrets:
        secret = (secret or "").strip()
        if len(secret) < 8:
            continue
        text = text.replace(secret, "***")
        # 只泄露首尾片段也算泄露：上游的错误话术常常回显 `sk-abcd…wxyz`。
        text = text.replace(secret[:8], "***").replace(secret[-8:], "***")
    return text


def fetch_logs(
    config: RailwayConfig,
    *,
    limit: int = DEFAULT_LIMIT,
    filter_expr: str = "",
    secrets: Iterable[str] = (),
    post: Optional[Callable[..., transport.Response]] = None,
) -> list[LogLine]:
    """拉一批环境日志。已脱敏、已尽力解析出结构化事件。

    `post` 只为测试注入，生产走 `transport.post_with_retry`
    （重试、full jitter 退避、响应体上限都在那一层）。
    """
    if not config.enabled:
        raise NotConfigured("、".join(config.missing()) + " 没配")
    limit = max(1, min(int(limit), MAX_LIMIT))
    variables: dict[str, Any] = {
        "environmentId": config.environment_id,
        "limit": limit,
    }
    expr = filter_expr.strip()
    if config.service_id:
        # 只要这个服务的日志。面板服务自己的访问日志混进来毫无价值，
        # 还会把真正要看的巡检输出挤出翻页窗口。
        expr = f"@service:{config.service_id} {expr}".strip()
    variables["filter"] = expr

    sender = post or transport.post_with_retry
    resp = sender(
        ENDPOINT,
        {"Authorization": f"Bearer {config.token}",
         "Content-Type": "application/json"},
        json.dumps({"query": _QUERY, "variables": variables}),
        timeout=config.timeout,
        should_retry=lambda r: r.status == 0 or r.status >= 500 or r.status == 429,
    )
    return _parse_response(resp, secrets)


def _parse_response(resp: transport.Response, secrets: Iterable[str]) -> list[LogLine]:
    if resp.status == 401 or resp.status == 403:
        raise RailwayError("Railway 拒绝了这个 token（401/403）。"
                           "去 railway.com 重新生成一个 account 或 workspace token")
    if resp.status == 429:
        raise RailwayError("Railway 限流了（429）。Hobby 是 1000 次/小时，"
                           "把 PANEL_CACHE_SECONDS 调大一点")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RailwayError(
            f"Railway 返回的不是 JSON（HTTP {resp.status}）："
            f"{redact(resp.body[:200], secrets)}")
    if payload.get("errors"):
        # GraphQL 的错误是 HTTP 200 + errors 数组。只看状态码会把每一个
        # 「字段名写错了」当成成功，然后拿到一个空列表——和「真的没有日志」
        # 长得一模一样，最难查。
        first = payload["errors"][0] if payload["errors"] else {}
        raise RailwayError("Railway GraphQL 报错："
                           + redact(str(first.get("message") or first)[:300], secrets))
    raw = ((payload.get("data") or {}).get("environmentLogs")) or []
    lines: list[LogLine] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags") or {}
        message = redact(str(item.get("message") or ""), secrets)
        lines.append(LogLine(
            timestamp=str(item.get("timestamp") or ""),
            message=message,
            severity=str(item.get("severity") or ""),
            service_id=str(tags.get("serviceId") or "") if isinstance(tags, dict) else "",
            deployment_id=str(tags.get("deploymentId") or "") if isinstance(tags, dict) else "",
            event=_as_event(message),
        ))
    return lines


def _as_event(message: str) -> Optional[dict]:
    """这一行是不是我们打的结构化事件。

    判据是「能解析成 JSON 对象 **且** 带 run_id 和 event」——只看
    「是不是 JSON」会把上游偶尔回显的 JSON 片段也收进来，
    那些没有 run_id，混进运行历史就是凭空多出来的轮次。
    """
    text = message.strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if "run_id" not in parsed or "event" not in parsed:
        return None
    return parsed


# ---------- 把事件重建成一轮一轮 ----------

@dataclass
class TableRun:
    label: str = ""
    rows: int = 0
    cost_yuan: float = 0.0
    counts: dict = field(default_factory=dict)
    used_providers: dict = field(default_factory=dict)
    failovers: int = 0
    breaker_tripped: bool = False
    budget_stopped: str = ""
    aborted_reason: str = ""
    fatal: bool = False


@dataclass
class Run:
    run_id: str = ""
    mode: str = ""
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    error: str = ""
    tables: list[TableRun] = field(default_factory=list)
    rows: int = 0
    cost_yuan: float = 0.0
    channels_dead: bool = False
    stopped: bool = False
    budget_stopped: str = ""

    @property
    def finished(self) -> bool:
        return self.ended_at is not None

    @property
    def breaker_tripped(self) -> bool:
        return any(t.breaker_tripped for t in self.tables)

    @property
    def failovers(self) -> int:
        return sum(t.failovers for t in self.tables)

    @property
    def ok(self) -> bool:
        """这一轮算不算「没事」。

        没收尾的轮子（有 run_start 没 run_end）一律算有事——那是被容器
        杀掉的形态（redeploy、回收、OOM），已经付过钱的结果多半丢了。
        """
        return (self.finished and not self.error and self.exit_code == 0
                and not self.channels_dead and not self.breaker_tripped)


def build_runs(lines: Iterable[LogLine], *, limit: int = 50) -> list[Run]:
    """把结构化事件重建成运行历史，最近的排前面。

    行级事件（EVENT_ROW）在这里**故意不展开**：一轮几百行，全塞进内存
    只为了在页面上显示一个数字。表级汇总里已经有 counts 了。
    """
    runs: dict[str, Run] = {}
    for line in lines:
        event = line.event
        if not event:
            continue
        run_id = str(event.get("run_id") or "")
        if not run_id:
            continue
        run = runs.setdefault(run_id, Run(run_id=run_id))
        kind = event.get("event")
        stamp = event.get("ts")
        if kind == "run_start":
            run.mode = str(event.get("mode") or run.mode)
            run.started_at = _as_float(stamp, run.started_at)
        elif kind == "table":
            run.mode = str(event.get("mode") or run.mode)
            run.tables.append(TableRun(
                label=str(event.get("table") or ""),
                rows=_as_int(event.get("rows")),
                cost_yuan=_as_float(event.get("cost_yuan"), 0.0) or 0.0,
                counts=event.get("counts") or {},
                used_providers=event.get("used_providers") or {},
                failovers=_as_int(event.get("failovers")),
                breaker_tripped=bool(event.get("breaker_tripped")),
                budget_stopped=str(event.get("budget_stopped") or ""),
                aborted_reason=str(event.get("aborted_reason") or ""),
                fatal=bool(event.get("fatal")),
            ))
        elif kind == "run_end":
            run.mode = str(event.get("mode") or run.mode)
            run.ended_at = _as_float(stamp, run.ended_at)
            run.exit_code = _as_int(event.get("exit_code"))
            run.error = str(event.get("error") or "")
            run.rows = _as_int(event.get("rows"))
            run.cost_yuan = _as_float(event.get("cost_yuan"), 0.0) or 0.0
            run.channels_dead = bool(event.get("channels_dead"))
            run.stopped = bool(event.get("stopped"))
            run.budget_stopped = str(event.get("budget_stopped") or "")
        # EVENT_ROW 有意忽略，见 docstring。
        if run.started_at is None and stamp is not None:
            run.started_at = _as_float(stamp, None)

    for run in runs.values():
        # 没有 run_end 的轮子，汇总从表级事件里凑——那正是被杀掉的形态，
        # 显示「处理了多少行、花了多少」比显示一片空白有用得多。
        if not run.finished and run.tables:
            run.rows = run.rows or sum(t.rows for t in run.tables)
            run.cost_yuan = run.cost_yuan or sum(t.cost_yuan for t in run.tables)

    ordered = sorted(runs.values(),
                     key=lambda r: (r.started_at or 0.0, r.run_id), reverse=True)
    return ordered[:limit]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default
