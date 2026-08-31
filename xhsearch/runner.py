"""编排：读表 → 调接口 → 判定 → 写回。

两条触发路径（手动刷单行 / 定时刷全表）走的是同一个 refresh()，只是传进来的
行不同。判定口径因此永远只有一份——否则必然出现「白天手动刷出爆文，夜里
批量跑完又变回去」这种最伤信任的 bug。

这个文件里有三处专门用来防「写坏表」的设计，改动时请先读懂它们：

1. 两击定罪：第一次取不到内容不判失效，只计数。
2. 全局熔断：一批里失效比例过高就整批作废标签写入。
3. 失败不清空：任何失败路径都保留上一次的数据，只更新状态列。
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from . import analyze, feishu, protocol, providers, tags, transport
from .config import Budget, Display, Settings
from .rows import Row, ToolCall, plan_calls, estimate_yuan

STATUS_OK = "正常"
STATUS_SUSPECT = "疑似受限"
STATUS_GONE = "已失效"
STATUS_FAILED = "刷新失败"
STATUS_SKIPPED = "跳过"
STATUS_COOLDOWN = "冷却跳过"
# 到软截止（或整批中止）没轮到的行：**完全不写回**，最后更新时间保持原样，
# 下一轮自然重新捞起——这才是 docstring 承诺的断点续跑。
STATUS_DEFERRED = "留待下一轮"

# 这些状态没有真正打过上游接口，不该参与熔断的失效比例计算。
_NOT_ATTEMPTED = frozenset({STATUS_COOLDOWN, STATUS_SKIPPED, STATUS_DEFERRED})


@dataclass
class TagPlan:
    """写回前重算「流量状态」需要的全部材料。

    存在的理由是 lost update：标签是**读-改-写**的（飞书多选没有原子 append），
    而读发生在整轮开始、写发生在整轮结束，中间可能隔着几分钟。运营在这几分钟里
    手工加的「客户已确认」，会被按旧快照算出来的整列值覆盖掉——
    「人工标签永远不动」这个承诺只在没有并发编辑时成立。

    有了它，write_back 可以在真正写之前重读一次现值，用**新鲜的**人工标签
    重做一次 merge（见 _reconcile_tags）。
    """

    field_name: str
    computed: list[str]
    namespace: list[str]
    known_options: Optional[list[str]]
    exclusive: tuple[tuple[str, ...], ...] = ()
    # 读表那一刻的现值，用来判断「这中间有没有人动过」。
    snapshot_tags: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    record_id: str
    status: str
    fields: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    # 积分只对 SocialDataX 有意义。双通道之后两家单价差 10 倍（抖音），
    # 唯一能跨供应商相加的单位是钱，所以真正的账在 cost_yuan 上。
    credits: int = 0
    cost_yuan: float = 0.0
    # 写回前重算标签的材料；None = 这一行本轮不碰标签列。
    tag_plan: Optional[TagPlan] = None
    # 这一行失败的错误码（GONE 路径才填）。小样本熔断靠它识别
    # 「所有行都栽在同一个错误上」这种上游漂移形态。
    failure_code: str = ""
    # 上游给的是不是**权威**结论（1008「内容已删除」、xhs detail 返回空列表）。
    # 权威信号不参与小样本熔断：那是有契约的死讯，不是启发式误判，
    # 否则一张只有三行、三条都真被删了的表会永远等不到「已失效」。
    failure_definitive: bool = False
    # 真正写进「最近检查时间」的那个时刻（UTC）。None = 这一行本轮不推进时间戳
    # （顺延、冷却、熔断作废）。日志逐行打印的就是它——日志和表对得上，
    # 全靠这里和 fields[last_updated] 是同一个值。
    checked_at: Optional[datetime] = None


@dataclass
class RunReport:
    outcomes: list[Outcome] = field(default_factory=list)
    aborted_reason: str = ""
    # 真正的故障（Key 失效、积分耗尽）才置 True。
    # 「有几行没跑完，留给下一轮」也会填 aborted_reason，但那是正常运行——
    # 两者必须分开，否则定时任务会把正常的分批执行当成失败反复重启。
    fatal: bool = False
    breaker_tripped: bool = False
    # 熔断判定用的样本（真正打过上游的行数 / 其中判了失效或嫌疑的行数），
    # 在单表熔断作废**之前**记录。跨表熔断要把各表样本加总重算比例，
    # 作废会把 GONE/SUSPECT 改成 FAILED，事后从 outcomes 里就数不出来了。
    breaker_attempted: int = 0
    breaker_gone: int = 0
    # 判了失效/嫌疑、**且信号是启发式（非权威）**的行各自栽在哪个错误码上。
    # 小样本熔断看的是「是不是全都栽在同一个码上」——那是上游漂移，
    # 不是内容真没了。权威死讯（1008 等）不进这张表，见 Outcome.failure_definitive。
    breaker_soft_codes: dict[str, int] = field(default_factory=dict)
    points_balance: Optional[int] = None
    # 本轮实际用过的供应商，以及降级次数——写进日志，免得「怎么突然贵/便宜了」查不出来。
    used_providers: dict[str, int] = field(default_factory=dict)
    failovers: int = 0
    # 本轮判定为不可用的平台（COR-004）：{平台: 原因}。
    # 一个平台的通道全倒不该让另一个平台的健康行也停摆。
    dead_platforms: dict[str, str] = field(default_factory=dict)
    # 预算触顶的原因（SUP-001）。非空 = 还有行没轮到，但它们完全没被写过。
    budget_stopped: str = ""

    @property
    def credits(self) -> int:
        return sum(o.credits for o in self.outcomes)

    @property
    def cost_yuan(self) -> float:
        return sum(o.cost_yuan for o in self.outcomes)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    def checked_span(self, display: Display, *, skip: Iterable[str] = ()) -> str:
        """本轮真正盖上「最近检查时间」的时间跨度，按显示时区渲染。

        写在「已写回 N 行」那一行后面，是为了让日志和表能**直接对上**：
        表里那一格显示的是什么，这里就打印什么。跨度而不是单个时刻，
        是因为几百行的一轮会跨好几分钟，而每一行盖的是自己的那个时刻。

        skip 传入**没写进去**的行（batch_update 逐行失败的那些，比如读表之后
        记录被删了）。算错了方向就白改了：报一个表里根本不存在的时刻，
        正是这次要修的「日志和表对不上」本身。同理，「最近检查时间」整列被
        挡下来时（列没建、类型建错）调用方根本不该问这个跨度——一行都没盖上。
        """
        skipped = set(skip)
        stamps = sorted(o.checked_at for o in self.outcomes
                        if o.checked_at and o.record_id not in skipped)
        if not stamps:
            return ""
        if stamps[0] == stamps[-1]:
            return display.stamp(stamps[0])
        return (f"{display.stamp(stamps[0])} ~ "
                f"{display.clock(stamps[-1])}")

    def summary(self) -> str:
        parts = [f"{k} {v}" for k, v in sorted(self.counts().items())]
        line = f"处理 {len(self.outcomes)} 行（{'，'.join(parts) or '无'}），"
        line += f"花费 ≈ ¥{self.cost_yuan:.2f}"
        if self.used_providers:
            via = "、".join(f"{k} {v} 次" for k, v in sorted(self.used_providers.items()))
            line += f"（{via}）"
        if self.failovers:
            line += f"，其中 {self.failovers} 次是主通道失败后降级完成的"
        if self.points_balance is not None:
            line += f"，SocialDataX 余额 {self.points_balance} 积分 ≈ ¥{self.points_balance / 100:.2f}"
        if self.breaker_tripped:
            line += "\n🛑 已熔断：本批失效比例异常偏高，所有流量状态写入已作废"
        if self.budget_stopped:
            line += f"\n💰 预算触顶：{self.budget_stopped}"
        for platform, reason in sorted(self.dead_platforms.items()):
            line += f"\n⚠ {platform} 平台本轮无可用通道：{reason[:150]}"
        if self.aborted_reason:
            line += f"\n⚠ 提前中止：{self.aborted_reason}"
        return line


class _Abort(Exception):
    """这个**平台**必须停下（它的通道全部 key 失效 / 积分耗尽）。

    刻意是平台级而不是整批级：xhs 只配了一个已失效的 TikHub、douyin 配了
    健康的 SocialDataX 时，整批级中止会让抖音那些本来能跑完的行全部顺延，
    一个平台的配置故障拖停另一个平台。

    带上这一行已经花掉的钱：中止的行也可能已经产生过成功（并计费）的调用，
    丢掉这笔账就会少报成本。
    """

    def __init__(self, reason: str, platform: str = "", credits: int = 0, yuan: float = 0.0):
        self.reason = reason
        self.platform = platform
        self.credits = credits
        self.yuan = yuan
        super().__init__(reason)


class RunBudget:
    """一次运行的硬预算，**发请求之前**预留（SUP-001）。

    「预留」而不是「事后统计」是关键：等花完再统计，钱已经花出去了。
    每一行在开跑前先把自己要花的调用数/金额记在账上，记不下就不跑——
    没跑的行完全不写回，queued / 到期状态保持原样，下一轮自然继续。

    线程安全：refresh() 的线程池会并发调用 reserve()。
    """

    def __init__(self, budget: Budget):
        self._budget = budget
        self._lock = threading.Lock()
        self.records = 0
        self.calls = 0
        self.yuan = 0.0
        self.stopped_reason = ""

    @property
    def limits(self) -> Budget:
        return self._budget

    def reserve(self, calls: int, yuan: float) -> bool:
        """给一行预留额度。返回 False = 预算不够，这一行不该跑。"""
        b = self._budget
        with self._lock:
            if b.max_records_per_run and self.records + 1 > b.max_records_per_run:
                self.stopped_reason = (
                    f"已达本轮行数上限 {b.max_records_per_run} 行，剩余行留给下一轮")
                return False
            if b.max_calls_per_run and self.calls + calls > b.max_calls_per_run:
                self.stopped_reason = (
                    f"已达本轮调用次数上限 {b.max_calls_per_run} 次，剩余行留给下一轮")
                return False
            if b.max_yuan_per_run and self.yuan + yuan > b.max_yuan_per_run + 1e-9:
                self.stopped_reason = (
                    f"已达本轮金额上限 ¥{b.max_yuan_per_run:.2f}，剩余行留给下一轮")
                return False
            self.records += 1
            self.calls += calls
            self.yuan += yuan
            return True

    def settle(self, reserved_yuan: float, actual_yuan: float,
               reserved_calls: int, actual_calls: int) -> None:
        """一行跑完后按**实际**花销校正账面。

        预留是按「此刻看起来会走哪家」算的，但一行跑到一半仍可能降级到更贵的
        那家（预留时它还是健康的），或者少发一个请求。不校正的话账面会持续
        偏离真实开销，后面的行就是拿一个假的余量在放行——
        广告出去的硬上限必须真的是上限。
        """
        with self._lock:
            self.yuan += max(0.0, actual_yuan) - max(0.0, reserved_yuan)
            self.calls += max(0, actual_calls) - max(0, reserved_calls)
            if self.yuan < 0:
                self.yuan = 0.0
            if self.calls < 0:
                self.calls = 0


def _looks_alive(snapshot: Optional[analyze.Snapshot]) -> bool:
    """这一轮有没有拿到「这篇内容还活着」的**强**证据——非零互动数。

    只用于内部抗拒矛盾死讯（见下面 GONE 分支）：评论接口刚拿回非零互动数，
    detail 却说笔记不存在，那是上游自相矛盾，这时候才有资格不信 detail。
    「已确认存活」复选框走的是更宽松的 _measured_this_round，不用这个。
    """
    if snapshot is None:
        return False
    return (bool(snapshot.comments) or bool(snapshot.comment_count)
            or bool(snapshot.like_count) or bool(snapshot.collect_count))


def _measured_this_round(snapshot: Optional[analyze.Snapshot]) -> bool:
    """这一轮有没有真的量到至少一个数字——哪怕量到的是 0。

    区分「量到了、就是 0」和「压根没量到」：评论数/点赞数/收藏数任一
    不是 None（哪怕是 0）都算量到了——这本身就证明这篇内容这一轮被
    成功巡查到，不需要数字非零才算存活。压根没量到的是评论页空壳
    （items 为空且 comment_count 是 None）又赶上 detail 兜底也失败的
    轮次：这一轮对存亡什么信息都没拿到，不该拿它去盖章。
    """
    if snapshot is None:
        return False
    return (bool(snapshot.comments)
            or snapshot.comment_count is not None
            or snapshot.like_count is not None
            or snapshot.collect_count is not None)


@dataclass
class _Spend:
    """这一次「取数」总共花掉了什么。跨供应商唯一能相加的单位是钱。

    是累加而不是覆盖：一次降级会真的产生两笔调用，其中失败的那笔也可能已经扣了钱。
    只记成功的那笔，账面就会比账单少，而少报成本的监控系统没有任何价值。
    """

    credits: int = 0
    yuan: float = 0.0
    provider: str = ""
    tally: dict[str, int] = field(default_factory=dict)
    # 实际发出去的 HTTP 请求数（含传输层重试和降级到备胎的那几次）。
    # 预算的调用闸门按它记账，而不是按「计划里有几个调用」。
    requests: int = 0


def _was_billed(provider_name: str, result: protocol.Result) -> bool:
    """这次失败是不是照样扣了钱。

    TikHub 自己的接口文档写着：传入不存在的笔记 ID，接口**正常响应、正常计费**，
    只是 data 里是个空壳。我们把那个空壳翻译成 GONE，但钱是真花了。
    判据是「HTTP 层面成功、业务层面失败」——鉴权失败(401)、参数非法(400)
    这类根本没进业务层的，对方明说不计费。

    是不是收费按供应商定（Provider.bills_failed_lookups），不能一刀切：
    SocialDataX 那边这件事还没验过，凭猜测把账单虚高比少报更糟。
    """
    if not isinstance(result, protocol.Err):
        return False
    if not providers.get_provider(provider_name).bills_failed_lookups:
        return False
    status = result.http_status
    return status is not None and 200 <= status < 300


def _call_once(
    provider_name: str,
    api_key: str,
    call: ToolCall,
    *,
    deadline: Optional[float],
    timeout: float,
) -> tuple[protocol.Result, int]:
    """打一次供应商接口。返回（解析结果, **实际发出的 HTTP 请求数**）。

    请求数要如实返回：传输层的一次重试也是一个真实请求，按「计划调用数」记账
    会让 MAX_CALLS_PER_RUN 名不副实。
    """
    provider = providers.get_provider(provider_name)
    request = provider.build(api_key, call.platform, call.purpose, call.arguments)
    response = transport.request_with_retry(
        request.method,
        request.url,
        request.headers,
        request.body,
        timeout=timeout,
        deadline=deadline,
        # 传输层只按 HTTP 状态判重试。业务错误可能走 HTTP 200 + body 里的字段，
        # 传输层看不见，必须由下面按解析结果处理。
        # 429 刻意**不**在这里重试：限流要按 retry_after 等待，那是 _fetch_one
        # 的职责（它还要照顾软截止）。传输层的退避对它太短。
        should_retry=lambda r: r.status == 0 or r.status >= 500,
    )
    result = provider.parse(
        call.platform, call.purpose,
        response.status, response.content_type, response.body,
        response.request_id, api_key,
    )
    # 把标准的 Retry-After 响应头补进 Err：只有 SocialDataX 会在 body 里给
    # retry_after_seconds，TikHub 只给响应头。不补的话，一个明确告诉我们
    # 「等 30 秒」的 429 会被按默认的 5 秒重试，撞回同一堵墙。
    if (isinstance(result, protocol.Err)
            and result.retry_after_seconds is None
            and response.retry_after is not None):
        result.retry_after_seconds = response.retry_after
    return result, max(1, response.attempts)


def _call(
    call: ToolCall,
    keys: dict[str, str],
    settings: Settings,
    *,
    deadline: Optional[float],
    timeout: float,
    disabled: set[str],
    spend: _Spend,
) -> protocol.Result:
    """双通道：主通道失败就降到备胎，而不是让整批停下。

    AUTH / QUOTA 在单通道时代是「整批立刻停」，因为再打也是白打。有了备胎之后
    它们的正确处置变成「把这家标记为本轮不可用，换一家继续」——只有**所有**
    通道都倒下才轮到 _Abort。这就是双通道真正值钱的地方：
    半夜 TikHub 余额见底不再意味着整晚的监控全丢。
    """
    usable = providers.usable_order(settings.channels, call.platform, keys, disabled)
    # 再按「这家吃不吃这种参数形态」过滤：TikHub 的抖音端点只收数字 aweme_id，
    # 不吃链接（图文、视频共用同一对端点，形态无关——问题只在参数不能是 URL）。
    order = [
        n for n in usable
        if providers.get_provider(n).can_handle(call.platform, call.purpose, call.arguments)
    ]
    if not order:
        if usable:
            # 有通道、但都不吃这种链接形态：这是行级问题（比如抖音短链提不出 ID），
            # 绝不能报成 AUTH——那会 FATAL 掉整批。UNKNOWN = 行级失败，写清原因。
            return protocol.Err(
                protocol.Failure.UNKNOWN, "unsupported_link",
                f"当前可用通道（{'、'.join(usable)}）不支持这种链接形态："
                f"无法从链接中提取{call.platform}作品 ID。"
                "请在表里贴含数字 ID 的完整链接，或配置 SOCIALDATAX_API_KEY"
                "（SocialDataX 直接吃短链和分享文案）",
            )
        return protocol.Err(
            protocol.Failure.AUTH, "no_channel",
            f"{call.platform} 没有可用的数据通道了（key 没配，或本轮全部失效）",
            definitive=True,
        )

    last: protocol.Result = protocol.Err(protocol.Failure.UNKNOWN, "no_attempt", "没有发起任何请求")
    for index, name in enumerate(order):
        result, attempts = _call_once(name, keys[name], call,
                                      deadline=deadline, timeout=timeout)
        spend.requests += attempts

        if isinstance(result, protocol.Ok) or _was_billed(name, result):
            spend.provider = name
            spend.yuan += providers.get_provider(name).yuan_per_call(call.platform, call.purpose)
            spend.credits += (getattr(result, "points_cost", None)
                              or (10 if name == providers.SOCIALDATAX else 0))
            spend.tally[name] = spend.tally.get(name, 0) + 1

        if isinstance(result, protocol.Ok):
            return result

        last = result
        if result.kind in (protocol.Failure.AUTH, protocol.Failure.QUOTA):
            # 这家这一轮别再用了：600 行 × 每行都撞一次同样的 401，
            # 既慢又会把限流打出来。
            disabled.add(name)
        if result.kind not in providers.FAILOVER_KINDS or index == len(order) - 1:
            return result
        # 换下一家。上面已经把这笔（如果被计费了的话）记进账了。

    return last


def _fetch_one(
    row: Row,
    keys: dict[str, str],
    settings: Settings,
    *,
    now: datetime,
    deadline: Optional[float],
    timeout: float,
    disabled: set[str],
    tally: dict[str, int],
    lock: Optional[threading.Lock] = None,
) -> tuple[Optional[analyze.Snapshot], Optional[protocol.Err], int, float, int, int]:
    """跑完一行需要的全部调用。

    返回（快照, 终局错误, 消耗积分, 花费元, 降级次数, 实际发出的请求数）。
    终局错误 code == "deadline" 且无快照 = 这一行根本没开始，留给下一轮。
    """
    calls = plan_calls(row, settings, now)
    if not calls:
        return None, protocol.Err(
            protocol.Failure.UNKNOWN, "bad_link", row.parsed.describe_failure()
        ), 0, 0.0, 0, 0

    snapshot: Optional[analyze.Snapshot] = None
    credits = 0
    yuan = 0.0
    failovers = 0
    requests = 0
    guard = lock or threading.Lock()

    def attempt(c: ToolCall) -> protocol.Result:
        nonlocal credits, yuan, failovers, requests
        spend = _Spend()
        # 「降级」的基准是**此刻真正可用**的第一家，而不是配置里写的主通道：
        # 只配了备胎 key 的部署（完全合法）每一次成功调用都不是降级，
        # 报成降级会让运营按手册去查一个不存在的主通道故障。
        #
        # disabled 必须一起传进来：主通道在本轮早些时候已经被判死之后，
        # 后面每一行都直接走备胎，那不是「这一行发生了降级」。漏传它会让
        # 降级次数被持续高估——而降级率正是判断主通道健康度的那个指标。
        candidates = [
            n for n in providers.usable_order(settings.channels, c.platform, keys, disabled)
            if providers.get_provider(n).can_handle(c.platform, c.purpose, c.arguments)
        ]
        expected = candidates[0] if candidates else ""
        result = _call(c, keys, settings, deadline=deadline, timeout=timeout,
                       disabled=disabled, spend=spend)
        # 多线程共享 tally / 报表计数：加锁——「读-改-写」在线程间会丢更新，
        # 账目哪怕只差一次也会让人怀疑整张账。
        with guard:
            credits += spend.credits
            yuan += spend.yuan
            requests += spend.requests
            for name, count in spend.tally.items():
                tally[name] = tally.get(name, 0) + count
        if spend.provider and expected and spend.provider != expected:
            failovers += 1
        return result

    for call in calls:
        if deadline is not None and time.monotonic() >= deadline:
            if snapshot is None:
                # 一个请求都还没发：整行留给下一轮，绝不写回。
                return None, protocol.Err(
                    protocol.Failure.UNKNOWN, "deadline", "已到软截止，本行留给下一轮"
                ), credits, yuan, failovers, requests
            break  # 评论已到手，detail 放弃：按已有数据完成本行

        result = attempt(call)

        if isinstance(result, protocol.Err) and result.kind is protocol.Failure.RATE_LIMIT:
            # 退避一次再试。等待时间不能越过软截止——设了软截止的运行时里，
            # 一次 30 秒的 retry_after 睡过头会把整轮拖超时。
            # retry_after 已在 protocol.clamp_retry_after 里过滤过负数/NaN/超大值：
            # 直接把上游给的数字传给 time.sleep 会被一个 -1 或 NaN 打挂。
            wait = protocol.clamp_retry_after(result.retry_after_seconds) or 5.0
            if deadline is not None:
                wait = min(wait, max(0.0, deadline - time.monotonic()))
            time.sleep(wait)
            if deadline is not None and time.monotonic() >= deadline:
                # 等待吃光了预算就别再打了：传输层只会回一个合成的截止响应，
                # 被当成普通网络失败写成「刷新失败」——该留给下一轮的行
                # 会因此被顶掉更新时间、清掉排队勾。
                if snapshot is None:
                    return None, protocol.Err(
                        protocol.Failure.UNKNOWN, "deadline",
                        "限流等待中到达软截止，本行留给下一轮"
                    ), credits, yuan, failovers, requests
                break  # 评论已到手：detail 放弃，按已有数据完成本行
            result = attempt(call)

        if isinstance(result, protocol.Err):
            # 重试后的结果也走同一套分类——之前的写法在这里直接 return，
            # 导致「限流 → 重试拿到权威 1008」时死亡信号被降级成一条备注。
            if result.kind in protocol.FATAL:
                # 走到这里说明**这个平台**所有通道都是 AUTH/QUOTA，备胎也没了。
                # 只停这个平台：另一个平台可能配着完全健康的通道。
                raise _Abort(str(result), call.platform, credits, yuan)
            if result.kind is protocol.Failure.RATE_LIMIT:
                # 二连限流：把这一行留给下一轮，别拖垮整批。
                return snapshot, result, credits, yuan, failovers, requests
            if result.kind is protocol.Failure.GONE and not _looks_alive(snapshot):
                # 「内容没了」是行级结论，不管它是从评论接口还是 detail 冒出来的。
                # TikHub 通道上最干净的死亡信号恰恰在 detail 里（小红书 data 为 []、
                # 抖音 aweme_detail 为 null + filter_list 命中），漏掉它
                # 等于把双通道最好的一个能力扔了。
                #
                # 但只在**没有活着的证据**时才认：评论接口刚拿回 200 条评论、
                # detail 却说笔记不存在，那是上游自相矛盾，这时候信 detail
                # 就是拿一次上游抖动去杀一条好帖子。
                return None, result, credits, yuan, failovers, requests
            # 评论接口失败 = 这一行的结论；detail 失败只是少几个数字，
            # 已经拿到的评论数据仍然有效，不该整行判死。
            if call.purpose == "comments":
                return snapshot, result, credits, yuan, failovers, requests
            continue

        assert isinstance(result, protocol.Ok)
        if call.purpose == "comments":
            snapshot = analyze.read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            analyze.merge_detail(snapshot, result.data)

    return snapshot, None, credits, yuan, failovers, requests


def _base_fields(settings: Settings, *, status: str, notes: list[str],
                 checked_at: datetime) -> dict[str, Any]:
    """每条路径都要写的四列。

    checked_at 是**这一行处理完的那一刻**，不是整轮开跑的那一刻。
    一轮 sweep 要跑几分钟，几百行共用开跑时间的话，表里的「最近检查时间」
    会比日志里那一行早好几分钟——运营拿两边对，怎么对都对不上。
    """
    f = settings.fields
    return {
        f.refresh_status: status,
        f.failure_reason: "；".join(n for n in notes if n)[:500],
        f.last_updated: int(checked_at.timestamp() * 1000),
        # 处理完就把「排队刷新」的勾去掉 —— 勾自动消失就是「已完成」的视觉信号，
        # 不需要向运营解释任何东西。
        f.queued: False,
    }


def refresh(
    rows: list[Row],
    api_key: Any,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
    known_options: Optional[list[str]] = None,
    comment_status_options: Optional[list[str]] = None,
    negative_status_options: Optional[list[str]] = None,
    pin_status_options: Optional[list[str]] = None,
    forced: bool = False,
    timeout: float = 30.0,
    progress: Optional[Callable[[str], None]] = None,
    deadline: Optional[float] = None,
    disabled: Optional[set[str]] = None,
    budget: Optional[RunBudget] = None,
    stop: Optional[threading.Event] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> RunReport:
    """刷新一批行。不写回，只算结果——写回由调用方决定时机。

    api_key 可以是一个字符串（只有一家供应商时的老写法，继续支持），
    也可以是 {"tikhub": "...", "socialdatax": "..."}——双通道要用后者。

    forced=True 表示这是人明确要求的刷新（手动触发），会跳过冷却检查。

    到软截止就停止派发新行；没跑到的行**不会**被写回，因此它们的
    「最后更新时间」保持原样，下一轮自然会被重新捞起来。这是断点续跑的全部机制。

    deadline / disabled / budget 是给多表调用方用的：软截止是整次运行的预算
    而不是每张表各一份，deadline 传入绝对的 time.monotonic() 截止点让各表共享；
    disabled 传入同一个集合，「某家 Key 已失效/余额耗尽」的结论就能跨表
    生效，不用每张表都花一次真实请求重新发现；budget 同理，行数/调用/金额
    的硬上限是**整次运行**的，不是每张表各领一份。都不传时行为与单表一致。

    stop 是优雅停机的开关（SIGTERM/SIGINT）：置位后不再派发新行，
    已经在跑的行跑完，结果照常返回给调用方写回。

    now 是**开跑那一刻**，用于筛行（到期、冷却）和算发布时长——整轮一个值，
    行与行之间的判定口径才一致。clock 是**当下**，只用来给每一行盖
    「最近检查时间」的戳；默认就是真实时钟，测试可以注入一个假的。
    两者分开是刻意的：判定要可复现，时间戳要说实话。

    结构化事件不在这里发：熔断（尤其是跨表熔断，发生在调用方那一层）会把
    已失效/疑似受限改写成刷新失败，在这里发等于让告警看到一个比实际落表
    **更吓人**的结论。用 emit_run_events()，在所有熔断都定案之后再发。
    """
    now = now or datetime.now(timezone.utc)
    tick = clock or (lambda: datetime.now(timezone.utc))

    def stamp() -> datetime:
        """这一行的「最近检查时间」。

        兜住时钟回拨：绝不写一个比开跑还早的时刻，否则这一行会显得比
        同一轮里先跑完的行更旧，分层刷新的先后关系就乱了。
        """
        moment = tick()
        return moment if moment >= now else now

    if deadline is None:
        deadline = (
            time.monotonic() + settings.soft_deadline_seconds
            if settings.soft_deadline_seconds
            else None
        )
    report = RunReport()
    say = progress or (lambda _: None)
    f = settings.fields

    keys = providers.credentials(api_key)
    # 本轮已经确认不可用的通道（Key 失效、余额耗尽）。多线程共享，
    # 但只做「加一个字符串」这一种写入，GIL 下天然安全，不值得上锁。
    if disabled is None:
        disabled = set()
    tally: dict[str, int] = {}
    # tally / report 计数的读-改-写要过这把锁（见 _fetch_one.attempt）。
    lock = threading.Lock()
    # 触发 FATAL 的**平台**在这里登记原因；同平台后来的行看到就直接顺延，
    # 不再穿透 pool.map——那样会把已完成但还没取出的结果一起丢掉。
    # 按平台分粒度是刻意的：xhs 的通道全倒，不该让 douyin 的健康行陪葬。
    dead_platforms: dict[str, str] = {}
    if budget is None:
        budget = RunBudget(settings.budget)

    def finish(
        row: Row,
        verdict: analyze.Verdict,
        snapshot,
        *,
        status: str,
        credits: int,
        cost_yuan: float = 0.0,
        touch_tags: bool = True,
    ) -> Outcome:
        """把判定结果落成待写字段。所有路径都汇总到这里，保证列的写法一致。

        touch_tags=False 用于「这一轮没获得关于这篇笔记的任何新信息」的情况
        （链接识别不了、第一次取不到内容）。这时必须完全不碰流量状态——
        否则易变标签会被当成「本轮判定为不需要」而摘掉，等于用一次失败
        抹掉上一次的真实结论。
        """
        checked_at = stamp()
        fields = _base_fields(settings, status=status, notes=verdict.notes,
                              checked_at=checked_at)
        tag_plan: Optional[TagPlan] = None

        if touch_tags:
            merged = tags.merge(
                row.current_tags,
                verdict.tags,
                settings.tags.namespace(),
                known_options=known_options,
                exclusive=(settings.tags.heat_tiers(),),
            )
            # 记下重算所需的材料：写回前会拿**那一刻**的现值再 merge 一次，
            # 这样运行期间运营新加的人工标签不会被旧快照算出的整列值覆盖。
            tag_plan = TagPlan(
                field_name=f.traffic_status,
                computed=sorted(verdict.tags),
                namespace=list(settings.tags.namespace()),
                known_options=None if known_options is None else list(known_options),
                exclusive=(tuple(settings.tags.heat_tiers()),),
                snapshot_tags=list(row.current_tags or []),
            )
            if merged.dropped_unknown:
                fields[f.failure_reason] = (
                    fields[f.failure_reason]
                    + f"；这些标签在「{f.traffic_status}」里还没建选项，已跳过："
                    + "、".join(merged.dropped_unknown)
                )[:500]
            # 无变化就根本不进 payload：省一次写、避开选项冲突、
            # 也避免让人看到这一行被反复改动。
            if merged.changed:
                fields[f.traffic_status] = merged.final

        # 「评论状态」「置顶状态」都是单选列，机器直接覆盖当前值——
        # 单选只显示当前状态（「待评论」这类人工排期旧值一并被结论覆盖）。
        # None = 这一轮判不了（没填关键词/抖音/空壳轮），保持原样。
        # 选项还没建就跳过并提示，别让一个缺选项拖垮整行写回。
        if touch_tags:
            for column, value, options in (
                (f.comment_status,
                 analyze.comment_status_value(verdict, settings),
                 comment_status_options),
                (f.negative_status,
                 analyze.negative_status_value(verdict, settings),
                 negative_status_options),
                (f.pinned_status,
                 analyze.pin_status_value(verdict, row.pin_status, settings),
                 pin_status_options),
            ):
                if value is None:
                    continue
                if options is not None and value not in options:
                    fields[f.failure_reason] = (
                        fields[f.failure_reason]
                        + f"；「{column}」里还没建选项「{value}」，已跳过"
                    )[:500]
                    continue
                fields[column] = value

        if row.parsed.platform:
            fields[f.platform] = "小红书" if row.parsed.platform == "xhs" else "抖音"

        if snapshot is not None:
            if snapshot.comment_count is not None:
                # 先把旧值搬到「上次评论数」，公式列才能算出增量。
                if row.previous_comment_count is not None:
                    fields[f.previous_comment_count] = row.previous_comment_count
                fields[f.comment_count] = snapshot.comment_count
            # 「起量时间」**只写第一次**：它回答「什么时候起来的」，是个历史
            # 事实；后面每一波都覆盖的话，这一列就退化成「最近一次涨得猛的
            # 时间」，而那个问题「评论增量」已经回答了。格子空着才写。
            # 写的时刻和「最近检查时间」是同一个 checked_at——两列必须逐字
            # 对得上，否则运营拿它对运行日志会差出几分钟。
            if verdict.surged and row.surge_time_ms is None:
                fields[f.surge_time] = int(checked_at.timestamp() * 1000)
                # 补一句「这是第一次」，把这一轮和后面几波区分开。
                # ⚠️ 措辞刻意**不说「已记进某某列」**：这句话是在这里定的，
                # 而那一列能不能落表要到 write_back 才知道——表里还没建
                # 「起量时间」时它会被整列挡下（dropped_fields，运行日志会说），
                # 诊断却照样落表。那就成了「表里写着已记录、格子根本不存在」。
                # 同一条纪律在 cli 里已有先例：整列被挡下时连「本轮巡查时间
                # 区间」都不报，因为报一个表里不存在的时刻正是要修的病。
                fields[f.failure_reason] = (
                    fields[f.failure_reason] + "；这是它第一次起量"
                )[:500]
            # 赞藏不再写表（四列已去掉）。snapshot.like_count / collect_count
            # 仍然解析、仍然当「这一轮真的量到了东西」的存活证据用
            # （见 _observed / _measured_this_round），只是不落表。
            # 快照只在看到了评论页（有评论、或至少知道评论数）时更新：
            # 空壳轮写「暂无评论」会把上一轮的真实快照抹掉。
            # 命中关键词的那条评论排最前并带「命中」标记。
            if analyze.saw_comment_page(snapshot):
                fields[f.comment_digest] = analyze.format_digest(
                    snapshot, settings.digest, hit=verdict.seed_hit)

        # 「负面评论快照」跟着「负面状态」一起写：判过就写（命中的几条，
        # 或者「（未命中）」），没判过一个字都不碰。
        # 判过却不写的话，上一轮的负面评论会一直留在格子里，
        # 运营会以为负面还在——那比不写更误导。
        if touch_tags and verdict.negative_checked:
            fields[f.negative_digest] = analyze.format_negative_digest(
                verdict.negative_hits, settings.digest)

        return Outcome(row.record_id, status, fields, "；".join(verdict.notes)[:200],
                       credits, cost_yuan, tag_plan=tag_plan, checked_at=checked_at)

    def work(row: Row) -> Outcome:
        platform = row.parsed.platform or ""
        with lock:
            dead_reason = dead_platforms.get(platform)
        if dead_reason:
            return Outcome(row.record_id, STATUS_DEFERRED, {},
                           f"{platform} 平台本轮无可用通道，留给下一轮", 0)
        if stop is not None and stop.is_set():
            # 收到 SIGTERM：停止派发新行，**不写回任何字段**——
            # 和软截止完全同一套语义，下一轮自然重捞。
            return Outcome(row.record_id, STATUS_DEFERRED, {}, "运行被终止，留给下一轮", 0)
        if deadline is not None and time.monotonic() >= deadline:
            # 到软截止：**不写回任何字段**。最后更新时间保持原样，下一轮自然重捞；
            # 排队勾保留，运营的手动请求不会被静默吞掉。
            return Outcome(row.record_id, STATUS_DEFERRED, {}, "已到软截止，留给下一轮", 0)

        if not row.parsed.usable:
            verdict = analyze.Verdict(notes=[row.parsed.describe_failure()])
            return finish(row, verdict, None, status=STATUS_SKIPPED, credits=0, touch_tags=False)

        if not forced and row.in_cooldown(settings, now):
            return Outcome(row.record_id, STATUS_COOLDOWN, {},
                           f"{settings.safety.cooldown_seconds} 秒内刚刷过，跳过（不计费）", 0)

        # —— 预算：在发请求**之前**预留（SUP-001）——
        # 预留必须是**悲观**的：预留时主通道是健康的，可跑到一半它倒了、
        # 这一行就走了贵十几倍的备胎。等 settle() 事后校正时钱已经花出去，
        # MAX_YUAN_PER_RUN 在这一行上已经被越过了。所以按「可能走到的最贵
        # 那家」预留，跑完再 settle 退还差额——上限不被突破，吞吐也不长期受压。
        planned = plan_calls(row, settings, now)
        reserved_yuan = estimate_yuan([row], settings, now, keys=keys,
                                      disabled=disabled, worst_case=True)
        # 调用数同理：一个计划中的调用最多会打 len(order) 家。传输层的重试
        # （最多 3 次，且只在网络/5xx 上发生）不预留，但会在 settle 里如实计入。
        reserved_calls = len(planned) * max(1, len(
            providers.usable_order(settings.channels, platform, keys, disabled)))
        if not budget.reserve(reserved_calls, reserved_yuan):
            return Outcome(row.record_id, STATUS_DEFERRED, {},
                           budget.stopped_reason or "本轮预算已用完，留给下一轮", 0)

        try:
            snapshot, error, credits, cost_yuan, failovers, made_requests = _fetch_one(
                row, keys, settings, now=now, deadline=deadline, timeout=timeout,
                disabled=disabled, tally=tally, lock=lock,
            )
        except _Abort as exc:
            # 中止的行也可能已经花过钱：按实际花销校正账面再走。
            # 请求数按预留值保守留着——中止路径拿不到真实计数，宁可多算。
            budget.settle(reserved_yuan, exc.yuan, reserved_calls, reserved_calls)
            with lock:
                dead_platforms.setdefault(exc.platform or platform, exc.reason)
            # 这一行没有完整结论，不写回；但已经花掉的钱要入账。
            return Outcome(row.record_id, STATUS_DEFERRED, {},
                           f"{exc.platform or platform} 平台中止：{exc.reason[:150]}",
                           exc.credits, exc.yuan)
        # 按**实际**值校正账面：金额是真花掉的，请求数是真发出去的
        # （含传输层重试和降级到备胎的那几次）。悲观预留的差额在这里退还，
        # 后面的行才是拿真实余量在放行。
        budget.settle(reserved_yuan, cost_yuan, reserved_calls, made_requests)
        with lock:
            report.failovers += failovers

        # —— 软截止在这一行开始前就到了：不写回，留给下一轮 ——
        if snapshot is None and error is not None and error.code == "deadline":
            return Outcome(row.record_id, STATUS_DEFERRED, {},
                           "已到软截止，留给下一轮", credits, cost_yuan)

        # —— 链接形态不被当前配置的通道支持（比如只配 TikHub 的抖音短链）——
        # 这是链接/配置问题，不是上游观测：按「跳过」处理（和坏链接同类），
        # 不进熔断的失效比例分母，也不碰任何标签和定罪计数。
        if snapshot is None and error is not None and error.code == "unsupported_link":
            verdict = analyze.Verdict(notes=[error.operator_text()])
            return finish(row, verdict, None, status=STATUS_SKIPPED,
                          credits=credits, cost_yuan=cost_yuan, touch_tags=False)

        # —— 取不到内容：两击定罪 ——
        if snapshot is None and error is not None and error.kind is protocol.Failure.GONE:
            strikes = (row.consecutive_failures or 0) + 1
            # 上游给出权威结论时（错误码 1008「内容已删除」，规范明写「不要重试」）
            # 不必等第二次——它已经确定了，再等一轮只是让运营晚一天看到。
            if error.definitive or strikes >= settings.safety.strikes_before_gone:
                verdict = analyze.gone_verdict(settings, error.operator_text(),
                                               current_tags=row.current_tags)
                outcome = finish(row, verdict, None, status=STATUS_GONE,
                                 credits=credits, cost_yuan=cost_yuan)
                outcome.fields[f.alive_confirmed] = False
            else:
                verdict = analyze.suspect_verdict(settings, strikes, error.operator_text())
                outcome = finish(row, verdict, None, status=STATUS_SUSPECT,
                                 credits=credits, cost_yuan=cost_yuan, touch_tags=False)
            outcome.fields[f.consecutive_failures] = strikes
            # 记下失败签名：小样本熔断靠「是不是全都栽在同一个码上」识别上游漂移。
            outcome.failure_code = f"{error.code}"
            outcome.failure_definitive = bool(error.definitive)
            return outcome

        # —— 取不到内容且不是「确认不存在」：只记，绝不打标签 ——
        if snapshot is None:
            reason = error.operator_text() if error else "没有拿到任何数据"
            checked_at = stamp()
            outcome = Outcome(
                row.record_id,
                STATUS_FAILED,
                _base_fields(settings, status=STATUS_FAILED, notes=[reason],
                             checked_at=checked_at),
                reason,
                credits,
                cost_yuan,
                checked_at=checked_at,
            )
            # ⚠️ 刻意不动「连续失败次数」：超时、5xx、限流这类失败对
            # 「内容还在不在」没有任何证据力。把它们也计进去，等于让一次
            # 网络抖动为下一次非权威 GONE 嫌疑「预热」定罪计数——
            # 单次嫌疑就能把活帖标成已失效，正是两击定罪要防的误伤。
            # 这个计数器只由 GONE 路径累加、由成功路径清零。
            return outcome

        verdict = analyze.decide(
            snapshot,
            settings,
            previous_comment_count=row.previous_comment_count,
            age_hours=row.age_hours(now),
            seed_keywords=row.seed_keywords,                  # 评论关键词组，任一命中即算命中
            negative_keywords=row.negative_keywords,          # 负面/竞品词，复用同一份评论页
            current_tags=row.current_tags,                    # 热度档位的棘轮要看现有档位
            current_pin_status=row.pin_status,                # 区分「掉了」和「从来没有」
        )
        if error is not None:
            verdict.notes.append(f"（detail 未取到：{error.operator_text()[:120]}）")
        if snapshot.points_balance is not None:
            with lock:
                # 取**最小**值而不是「最后一个线程写的那个」：余额随消费单调下降，
                # 最小值就是这一轮结束时的真实余额。裸赋值的结果由线程完成顺序
                # 决定，可能显示一个较高的旧余额——一个会高报余额的资金监控
                # 比没有监控更危险。
                report.points_balance = (
                    snapshot.points_balance if report.points_balance is None
                    else min(report.points_balance, snapshot.points_balance))

        outcome = finish(row, verdict, snapshot, status=STATUS_OK,
                         credits=credits, cost_yuan=cost_yuan)
        outcome.fields[f.consecutive_failures] = 0
        # 「已确认存活」只要这一轮真的量到了数字（哪怕是 0）就打勾——
        # 巡查状态=正常本身就是这篇内容还在的证明，不需要互动数字非零。
        # 评论页空壳 + detail 兜底也失败的轮次对存亡零信息，复选框不动。
        if _measured_this_round(snapshot):
            outcome.fields[f.alive_confirmed] = True
        return outcome

    def guarded(row: Row) -> Outcome:
        """行级异常隔离（ROB-003）。

        `work` 里除了 _Abort 之外的任何异常，原来会经 pool.map 重新抛出，
        refresh() 直接不返回 report——这张表所有**已经花过钱**的行一条都不写回，
        多表时还会阻断后续的表。一条脏数据、一次上游形状漂移就能造成这个后果。

        所以这里兜住 Exception（不兜 BaseException：KeyboardInterrupt /
        SystemExit 该照常穿透），把这一行记成「刷新失败」并带上 traceback 摘要。
        与其它 STATUS_FAILED 路径一致：写状态和诊断、推进最近检查时间、
        清排队勾——让它按正常节奏重试，而不是每一轮都在同一个 bug 上重复花钱。
        """
        try:
            return work(row)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            say(f"  ⚠ {row.record_id} 处理时抛异常：{detail}")
            say("    " + traceback.format_exc().strip().replace("\n", "\n    "))
            reason = f"内部错误，本行已跳过：{detail}"[:300]
            checked_at = stamp()
            return Outcome(
                row.record_id,
                STATUS_FAILED,
                _base_fields(settings, status=STATUS_FAILED, notes=[reason],
                             checked_at=checked_at),
                reason,
                checked_at=checked_at,
            )

    pending = list(rows)
    with ThreadPoolExecutor(max_workers=max(1, settings.max_concurrency)) as pool:
        for outcome in pool.map(guarded, pending):
            report.outcomes.append(outcome)
            # 行首的时间是**这一行的**「最近检查时间」，和写进表里的是同一个值。
            # pool.map 按提交顺序产出，日志行的打印时刻可能比实际处理晚好几秒
            # （前一行卡住时后几行会一起冲出来），所以不能拿日志自己的时间戳去
            # 对表——必须把落表的那个值直接打出来。顺延/冷却的行没有时间戳，
            # 留空占位，一眼就能看出「这一行本轮没动过时间」。
            when = (settings.display.clock(outcome.checked_at)
                    if outcome.checked_at else "  --  ")
            say(f"  [{when}] {outcome.record_id} → {outcome.status} "
                f"{outcome.reason}".rstrip())

    report.dead_platforms = dict(dead_platforms)
    report.budget_stopped = budget.stopped_reason
    if dead_platforms:
        report.aborted_reason = "；".join(
            f"{platform}：{reason}" for platform, reason in sorted(dead_platforms.items()))
        report.fatal = True
    else:
        deferred = sum(1 for o in report.outcomes if o.status == STATUS_DEFERRED)
        if deferred:
            cause = budget.stopped_reason or "到达软截止"
            report.aborted_reason = f"{deferred} 行未处理（{cause}），留给下一轮"

    report.used_providers = dict(tally)
    _apply_circuit_breaker(report, settings)
    return report


# 结构化事件的四种类型。面板靠它们把一轮跑成什么样从日志里还原出来，
# 所以每条都带 event 字段——少了它，消费方只能靠「有没有 record_id」猜。
EVENT_RUN_START = "run_start"
EVENT_TABLE = "table"
EVENT_ROW = "row"
EVENT_RUN_END = "run_end"


def emit(sink: Optional[Callable[[dict[str, Any]], None]], event: str,
         **payload: Any) -> None:
    """发一条结构化事件。**任何异常都吞掉。**

    日志出问题绝不能影响业务流程——一轮已经付过钱的结果，不该因为
    序列化一个奇怪的值失败就丢掉。
    """
    if sink is None:
        return
    try:
        sink({"event": event, **payload})
    except Exception:  # noqa: BLE001
        pass


def emit_run_events(report: RunReport, sink: Optional[Callable[[dict[str, Any]], None]],
                    **context: Any) -> None:
    """把这份 report 发成结构化事件：一条表级汇总 + 每行一条。

    **必须在所有熔断都定案之后调用**（单表熔断在 refresh 末尾、跨表熔断在
    调用方那一层）。在行跑完时就发的话，一旦事后触发熔断，
    原本记成「已失效」的行会被改写成「刷新失败」再落表——
    看板和告警消费到的就是一个比表里更吓人的结论，
    而这恰恰发生在上游故障、最不该误报的时候。

    表级汇总（EVENT_TABLE）是给面板用的：从容器日志里还原「这一轮哪张表
    处理了多少行、花了多少、有没有熔断、降级几次」，不用把几百条行级事件
    都拉回来自己加总。行级事件仍然照发，排查具体某一行时要用。
    """
    if sink is None:
        return
    first = min((o.checked_at for o in report.outcomes if o.checked_at), default=None)
    last = max((o.checked_at for o in report.outcomes if o.checked_at), default=None)
    emit(sink, EVENT_TABLE, **{
        **context,
        "rows": len(report.outcomes),
        "counts": report.counts(),
        "cost_yuan": round(report.cost_yuan, 6),
        "credits": report.credits,
        "used_providers": dict(report.used_providers),
        "failovers": report.failovers,
        "breaker_tripped": report.breaker_tripped,
        "breaker_attempted": report.breaker_attempted,
        "breaker_gone": report.breaker_gone,
        "budget_stopped": report.budget_stopped,
        "dead_platforms": dict(report.dead_platforms),
        "aborted_reason": report.aborted_reason,
        "fatal": report.fatal,
        "points_balance": report.points_balance,
        "checked_from": first.isoformat() if first else None,
        "checked_to": last.isoformat() if last else None,
    })
    for outcome in report.outcomes:
        emit(sink, EVENT_ROW, **{
            **context,
            "record_id": outcome.record_id,
            "status": outcome.status,
            "reason": outcome.reason,
            "cost_yuan": round(outcome.cost_yuan, 6),
            "credits": outcome.credits,
            "failure_code": outcome.failure_code,
            "breaker_tripped": report.breaker_tripped,
        })


def _apply_circuit_breaker(report: RunReport, settings: Settings) -> None:
    """一批里失效比例异常偏高 → 判定为上游故障，撤销所有标签写入。

    几百条笔记不可能在同一小时里被集体删除。真发生这种事，一定是上游挂了或者
    错误话术改版了，而不是内容真出事。宁可这一轮什么都不写，也不能把整张表刷红。
    """
    # 分母只算真正打过上游的行：冷却/坏链接/软截止顺延的行没有产生任何
    # 「取不到内容」的观测，混进分母会稀释失效比例，让该熔的批熔不了。
    attempted = [o for o in report.outcomes if o.status not in _NOT_ATTEMPTED]
    total = len(attempted)
    suspects = [o for o in attempted if o.status in (STATUS_GONE, STATUS_SUSPECT)]
    gone = len(suspects)
    soft_codes: dict[str, int] = {}
    for outcome in suspects:
        if outcome.failure_definitive:
            continue
        key = outcome.failure_code or "unknown"
        soft_codes[key] = soft_codes.get(key, 0) + 1
    # 样本先记在 report 上再决定熔不熔：跨表熔断要用作废前的原始计数。
    report.breaker_attempted, report.breaker_gone = total, gone
    report.breaker_soft_codes = soft_codes
    if total >= settings.safety.breaker_min_sample:
        if gone / total > settings.safety.breaker_gone_ratio:
            _void_gone_writes(report, settings, gone, total)
        return
    if _uniform_failure(total, soft_codes, settings):
        _void_gone_writes(report, settings, gone, total,
                          extra="、且全部栽在同一个非权威错误码上")


def _uniform_failure(total: int, soft_codes: dict[str, int], settings: Settings) -> bool:
    """小样本的第二道熔断（ROB-010）。

    比例闸门要 10 行样本才生效，而 queue 模式一批常常只有三五行——
    上游把所有笔记都译成 empty_shell 的那种漂移，在小批次上完全没有保护，
    两轮之后一批活帖就全被标成「已失效」。

    判据有三条，缺一不可：
    1. 打过上游的行数够小样本线（默认 3）；
    2. **每一行**都判了失效/嫌疑；
    3. 全部栽在**同一个非权威错误码**上。

    第 3 条里的「非权威」是这道闸不误伤真实死讯的关键：真被删掉的内容拿到的是
    有契约的权威码（SocialDataX 1008、TikHub 小红书 detail 空列表 / 抖音
    filter_list 命中），它们不进这张表，所以「一张只有三行、三条都真被删了」
    的表照常能判「已失效」。被挡住的是启发式信号（empty_shell、not_found）
    整批一致命中——那是上游改了话术或 schema，不是内容集体消失。
    """
    if total < settings.safety.breaker_uniform_min_sample:
        return False
    return len(soft_codes) == 1 and sum(soft_codes.values()) == total


def apply_cross_run_breaker(reports: list[RunReport], settings: Settings) -> bool:
    """跨表熔断：把同一轮里多张表的观测合起来再算一次失效比例。

    多表部署下每张表可能只有三五行，单表永远凑不满 breaker_min_sample——
    但上游故障是通道级的，跟行分在哪张表没关系，样本理应全局算。
    这里用各 report 在单表熔断**之前**记下的原始计数（见 breaker_attempted），
    已经自己熔断过的表不重复作废（诊断信息会追加两遍），
    但它的样本照常计入全局比例。触发时返回 True，调用方据此提示。
    """
    total = sum(r.breaker_attempted for r in reports)
    gone = sum(r.breaker_gone for r in reports)
    soft_codes: dict[str, int] = {}
    for report in reports:
        for code, count in report.breaker_soft_codes.items():
            soft_codes[code] = soft_codes.get(code, 0) + count

    uniform = False
    if total < settings.safety.breaker_min_sample:
        uniform = _uniform_failure(total, soft_codes, settings)
        if not uniform:
            return False
    elif gone / total <= settings.safety.breaker_gone_ratio:
        return False

    for report in reports:
        if not report.breaker_tripped:
            _void_gone_writes(
                report, settings, gone, total,
                extra="、且全部栽在同一个非权威错误码上" if uniform else "")
    return True


def _void_gone_writes(report: RunReport, settings: Settings, gone: int, total: int,
                      *, extra: str = "") -> None:
    """熔断的执行动作：撤销这份 report 里所有失效判定的写入。

    gone/total 是触发熔断的样本（单表熔断是本表的，跨表熔断是全局合计的），
    只用于诊断文案，让运营看到判定被作废的依据。

    ⚠️ 「作废」必须**彻底**：被保护的行不能只撤掉标签，却仍然带着
    「最近检查时间=现在」和「排队刷新=False」落表——那等于告诉调度器
    「这一行本轮处理完了」，运营的手动请求被吞掉，sweep 还要再等 8–72 小时
    才会复查。承诺的是「宁可这一轮什么都不写」，就要真的什么都不写：
    只留状态和诊断两列。
    """
    report.breaker_tripped = True
    f = settings.fields
    for outcome in report.outcomes:
        outcome.fields.pop(f.traffic_status, None)
        if outcome.status in (STATUS_GONE, STATUS_SUSPECT):
            outcome.status = STATUS_FAILED
            # 判定作废，计数增量也要一并撤销——否则熔断轮照样给每行 +1，
            # 上游故障一恢复，下一次单个非权威 GONE 就能一击定罪。
            outcome.fields.pop(f.consecutive_failures, None)
            # 「已确认存活=取消」同理作废：上游故障轮不能把一批活帖的勾全摘掉。
            outcome.fields.pop(f.alive_confirmed, None)
            # 不推进最近检查时间、不清排队勾：这一行**没有**得到有效结论。
            outcome.fields.pop(f.last_updated, None)
            outcome.fields.pop(f.queued, None)
            # 时间戳撤了，报告里也不能再声称盖过章——收尾那行打印的
            # 「巡查时间 x ~ y」区间必须只包含真正落表的那些行。
            outcome.checked_at = None
            # 标签重算材料也要撤销：这一行本轮不碰标签列了。
            outcome.tag_plan = None
            # 追加而不是覆盖：原始错误文案里带着 request_id，是找厂商排查的唯一凭据。
            original = str(outcome.fields.get(f.failure_reason) or "")
            note = (
                f"本批 {gone}/{total} 行都取不到内容{extra}，疑似上游故障而非内容失效，"
                "本轮不改流量状态、不推进检查时间，请稍后复查"
            )
            outcome.fields[f.refresh_status] = STATUS_FAILED
            outcome.fields[f.failure_reason] = (
                f"{original}；{note}" if original else note
            )[:500]


# ---------- 与飞书表的对接 ----------


def row_from_record(record: dict[str, Any], settings: Settings) -> Row:
    """一条飞书 record → 一个 Row。**纯函数，不发请求。**

    抽出来是为了给第二个读侧用：监控面板要按更宽的列做一次 search
    （它还要 巡查状态/负面状态/诊断信息 这些判定链路用不到的列），
    但「这一格怎么读成 Row 的哪个字段」必须和 load_rows 逐字相同——
    两处各写一遍的话，面板算出来的「到期几行、预计多少钱」迟早和
    真正开跑时不是一个数，而那正是最难发现的一类不一致。
    """
    f = settings.fields
    cells = record.get("fields") or {}
    return Row(
        record_id=record.get("record_id") or "",
        link_cell=feishu.read_text(cells.get(f.link)),
        publish_time_ms=feishu.read_timestamp_ms(cells.get(f.publish_time)),
        seed_keywords=feishu.read_keywords(cells.get(f.seed_keywords)),
        negative_keywords=feishu.read_keywords(cells.get(f.negative_keywords)),
        current_tags=feishu.read_multi_select(cells.get(f.traffic_status)),
        previous_comment_count=feishu.read_int(cells.get(f.comment_count)),
        last_updated_ms=feishu.read_timestamp_ms(cells.get(f.last_updated)),
        consecutive_failures=feishu.read_int(cells.get(f.consecutive_failures)) or 0,
        pin_status=feishu.read_text(cells.get(f.pinned_status)),
        surge_time_ms=feishu.read_timestamp_ms(cells.get(f.surge_time)),
        queued=feishu.read_bool(cells.get(f.queued)),
    )


def load_rows(
    table: feishu.Bitable,
    settings: Settings,
    *,
    only_record_ids: Optional[list[str]] = None,
    only_due: bool = True,
    only_queued: bool = False,
    now: Optional[datetime] = None,
    max_records: Optional[int] = None,
    known_fields: Optional[set[str]] = None,
) -> list[Row]:
    """从表里读出待刷新的行。

    分层刷新在这里落地，而且只是一个过滤条件，不是一套调度代码。

    known_fields 传入表里实际存在的列名时，search 只请求存在的列——
    按名字请求不存在的列（比如还没建的「点赞数」）会让整个 search 直接
    报 1254045，一行都读不到，写侧的列过滤根本轮不到出场。
    """
    f = settings.fields
    now = now or datetime.now(timezone.utc)

    if known_fields is not None and only_queued and f.queued not in known_fields:
        # 「排队刷新」列还没建：queue 模式无事可做。绝不能退化成无过滤
        # 全表刷新——那会花掉一整轮 sweep 的钱。
        return []

    if known_fields is not None and only_due and f.last_updated not in known_fields:
        # 「最近检查时间」列还没建：分层刷新没有依据，每一行都会被判成
        # 「该刷了」，一轮 sweep 就是全表重刷；写回侧又会把时间戳挡掉
        # （列不存在），下一轮照样全刷——烧钱死循环。拒跑，调用方提示建列。
        return []

    filter_spec: Optional[dict[str, Any]] = None
    if only_record_ids is None:
        conditions = [{"field_name": f.monitoring, "operator": "is", "value": ["true"]}]
        if only_queued:
            conditions.append({"field_name": f.queued, "operator": "is", "value": ["true"]})
        filter_spec = {"conjunction": "and", "conditions": conditions}

    wanted_fields = f.must_read()
    if known_fields is not None:
        wanted_fields = [c for c in wanted_fields if c in known_fields]

    if only_record_ids:
        # 定点读（SUP-003）：原来是「把表里所有 monitoring 行都读回来再本地筛
        # record_id」，多表时每张表都全量扫一遍。手动刷一行不该是 O(全表×表数)。
        records = table.batch_get(only_record_ids)
    else:
        records = table.search(wanted_fields, filter_spec=filter_spec, max_records=max_records)

    wanted = set(only_record_ids or [])
    result: list[Row] = []
    for record in records:
        record_id = record.get("record_id") or ""
        if wanted and record_id not in wanted:
            continue
        row = row_from_record(record, settings)
        # 手动触发时无视分层节流——人明确要求刷新，就该刷。
        if wanted or row.queued or not only_due or row.is_due(settings, now):
            result.append(row)
    return result


def _reconcile_tags(
    table: feishu.Bitable,
    report: RunReport,
    *,
    say: Callable[[str], None] = lambda _: None,
) -> None:
    """写回**之前**重读一次流量状态，用新鲜的人工标签重做 merge（COR-002）。

    为什么必须有这一步：飞书多选字段没有原子 append，写入是整列覆盖，
    所以每次都得读-改-写。而这一轮的「读」发生在开跑时、「写」发生在收尾时，
    中间隔着几分钟的付费调用。运营在这几分钟里手工加的「客户已确认」，
    会被按开跑那一刻的旧快照算出来的整列值直接抹掉——
    「人工标签永远不动」这个承诺原本只在没有并发编辑时成立。

    重读失败（网络、权限、接口不支持）时**不写这一列**，而不是拿旧快照赌一把：
    宁可这一轮少打一个机器标签（下一轮自动补上），也不能覆盖掉人手打的标签。
    """
    targets = [o for o in report.outcomes
               if o.tag_plan is not None and o.tag_plan.field_name in o.fields]
    if not targets:
        return

    try:
        fresh = {r.get("record_id"): (r.get("fields") or {})
                 for r in table.batch_get([o.record_id for o in targets])}
    except Exception as exc:  # noqa: BLE001 —— 重读失败不该炸穿写回
        say(f"⚠ 写回前重读「流量状态」失败（{type(exc).__name__}: {exc}）："
            "本轮不改这一列，避免覆盖运营刚打的人工标签；下一轮会自动补上")
        for outcome in targets:
            outcome.fields.pop(outcome.tag_plan.field_name, None)
        return

    changed_rows = 0
    for outcome in targets:
        plan = outcome.tag_plan
        cells = fresh.get(outcome.record_id)
        if cells is None:
            # 这一行在我们跑的这几分钟里被删了：整行交给 batch_update 去报
            # 1254043 并被隔离，不必在这里特殊处理。
            continue
        current = feishu.read_multi_select(cells.get(plan.field_name))
        if sorted(current) == sorted(plan.snapshot_tags):
            continue   # 没人动过，开跑时算出来的值仍然成立
        merged = tags.merge(
            current,
            plan.computed,
            plan.namespace,
            known_options=plan.known_options,
            exclusive=plan.exclusive,
        )
        changed_rows += 1
        if merged.changed:
            outcome.fields[plan.field_name] = merged.final
        else:
            # 拿新鲜现值重算后无需改动：那就一列都别写。
            outcome.fields.pop(plan.field_name, None)
    if changed_rows:
        say(f"ℹ 写回前发现 {changed_rows} 行的「流量状态」在本轮运行期间被改过，"
            "已按最新现值重算标签（人工标签原样保留）")


def write_back(
    table: feishu.Bitable,
    report: RunReport,
    *,
    errors: Optional[list] = None,
    known_fields: Optional[set[str]] = None,
    field_types: Optional[dict[str, Any]] = None,
    dropped_fields: Optional[set[str]] = None,
    mistyped_fields: Optional[set[str]] = None,
    say: Callable[[str], None] = lambda _: None,
) -> int:
    """写回。errors 传一个列表进来可以收集失败的行（(record_id, FeishuError)），
    不传则在所有行都尝试过之后抛汇总异常——见 feishu.Bitable.batch_update。

    写回前有两道按列的闸，理由是同一个：batch_update 全成功或全失败，
    一列不对就能让整表已经付过钱的结果全部落空。

    known_fields 传入表里实际存在的列名（table.field_names()）时，会把表里
    还没建的机器列挡下来（记进 dropped_fields 供调用方提示）——按名字写
    不存在的列是表级错误（1254045）。None = 不过滤，宁可试着写。

    field_types 传入列名 → 字段类型码（table.fields_meta() 里的 "type"）时，
    会把类型和值形状对不上的列挡下来（记进 mistyped_fields）——比如
    「流量状态」被建成单选而机器写列表，飞书回 1254063，整表写回全灭。
    None = 不过滤（旧行为）。
    """
    _reconcile_tags(table, report, say=say)
    updates = []
    for o in report.outcomes:
        fields = o.fields
        if not fields:
            continue
        if known_fields is not None:
            missing = set(fields) - known_fields
            if missing:
                if dropped_fields is not None:
                    dropped_fields |= missing
                fields = {k: v for k, v in fields.items() if k in known_fields}
        if field_types is not None:
            bad = {k for k, v in fields.items()
                   if k in field_types and not feishu.value_fits(field_types[k], v)}
            if bad:
                if mistyped_fields is not None:
                    mistyped_fields |= bad
                fields = {k: v for k, v in fields.items() if k not in bad}
        if fields:
            updates.append({"record_id": o.record_id, "fields": fields})
    return table.batch_update(updates, errors=errors) if updates else 0
