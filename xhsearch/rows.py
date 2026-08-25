"""一行的模型：从表里读到什么、要发哪几个请求、最后写回什么。

调用计划是这里最值钱的部分，因为它直接等于钱：

* 小红书：1 次评论调用就拿到 评论数 + 置顶 + 前 N 条。只有需要点赞/收藏
  （爆文的另一个维度）时才追加 1 次 detail，且只对新笔记追加。
* 抖音：评论接口的 comment_count 是 integer|null，为了「评论数」这一列不
  间歇性变空，detail 必须恒定兜底 —— 抖音单篇成本是小红书的两倍。

计费单位是页不是条，页大小服务端定（接口没有 page_size 参数），所以
「只要前 5 条」和「要前 20 条」一个价，整页存下来即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings
from .links import ParsedLink, parse
# 直接导名字并写成一行（理由同 providers.py 顶部的历史说明）。
from .providers import get_provider  # noqa: F401


@dataclass
class ToolCall:
    platform: str            # "xhs" | "douyin"
    purpose: str             # "comments" | "detail"
    arguments: dict[str, Any]


def _utc(ms: Optional[int]) -> Optional[datetime]:
    """毫秒时间戳 → UTC 时间。脏值返回 None，**绝不抛异常**。

    feishu.read_timestamp_ms 已经挡过一次范围，这里是第二道：Row 也可能被
    直接构造（测试、tools、未来的其它读源）。datetime.fromtimestamp 对
    超范围的值抛 OverflowError/OSError，而这个调用发生在**选行阶段**——
    一格脏日期就能让整张表一行都刷不了。
    """
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class Row:
    record_id: str
    link_cell: str
    publish_time_ms: Optional[int] = None
    # 评论关键词组：任一出现在第一页评论里即算命中（不区分大小写）。
    seed_keywords: list[str] = field(default_factory=list)
    # 负面词/竞品词组：查的是**别人**写了什么，和上面正好相反。
    # 两者共用同一份第一页评论，不额外发请求。
    negative_keywords: list[str] = field(default_factory=list)
    current_tags: list[str] = field(default_factory=list)
    previous_comment_count: Optional[int] = None
    last_updated_ms: Optional[int] = None
    consecutive_failures: int = 0
    # 「置顶状态」单选的现值：上一轮是 置顶成功/置顶掉了、这一轮没置顶
    # → 置顶掉了；否则 → 无置顶。「掉了」和「从来没有」的区分全靠它。
    pin_status: str = ""
    queued: bool = False

    _parsed: Optional[ParsedLink] = field(default=None, repr=False, compare=False)

    @property
    def parsed(self) -> ParsedLink:
        if self._parsed is None:
            self._parsed = parse(self.link_cell)
        return self._parsed

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        published = _utc(self.publish_time_ms)
        if published is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - published).total_seconds() / 3600

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        hours = self.age_hours(now)
        return None if hours is None else hours / 24

    def in_cooldown(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """刚刷过就别再刷。

        这是对「有人连点 200 次按钮」的完整回答：连点 200 次 = 1 次真实调用。
        """
        window = settings.safety.cooldown_seconds
        updated = _utc(self.last_updated_ms)
        if not window or updated is None:
            return False
        now = now or datetime.now(timezone.utc)
        elapsed = (now - updated).total_seconds()
        # 负数 = 表里的「最近检查时间」在未来（时钟漂移或人工填错）。
        # 按「不在冷却里」处理：让它至少能被刷一次并把时间戳纠正回来，
        # 否则这一行会被一个未来时间锁死到那个时间点为止。
        return 0 <= elapsed < window

    def is_due(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """按分层策略判断这一行现在该不该刷。

        发布时间未知时按「该刷」处理——宁可多花一毛钱，也别让一行永远不更新
        而没人发现。
        """
        age = self.age_days(now)
        if age is None:
            return True
        interval = settings.refresh.interval_hours_for_age(age)
        if interval is None:
            return False  # 已归档
        updated = _utc(self.last_updated_ms)
        if updated is None:
            return True
        now = now or datetime.now(timezone.utc)
        elapsed_hours = (now - updated).total_seconds() / 3600
        # 未来的更新时间同样按「该刷」处理：否则一个填错的日期能让这一行
        # 长期不到期，而且没有任何人会发现。
        return elapsed_hours < 0 or elapsed_hours >= interval


def plan_calls(row: Row, settings: Settings, now: Optional[datetime] = None) -> list[ToolCall]:
    """算出这一行需要发哪些请求。空列表 = 链接不可用，不该花钱。

    优先用 ID 而不是 URL：小红书分享链接带 `xsec_token`，会过期；ID 不会。
    """
    link = row.parsed
    if not link.usable:
        return []

    calls: list[ToolCall] = []
    age_days = row.age_days(now)

    if link.platform == "xhs":
        target = {"note_id": link.content_id} if link.content_id else {"url": link.url}
        # sort=default 是唯一正确的选择：它对应 App 里默认看到的综合排序，
        # 也是置顶评论最可能出现在第一页的排序。换成按时间倒序
        # 会把老的置顶评论压到最后。
        # 参数名是抽象的——两家叫法不同（sort_type / sort_strategy），
        # 由 providers 层各自翻译。
        calls.append(ToolCall("xhs", "comments", {**target, "sort": "default"}))

        want_detail = settings.detail_within_days > 0 and (
            age_days is None or age_days <= settings.detail_within_days
        )
        if want_detail:
            calls.append(ToolCall("xhs", "detail", dict(target)))

    elif link.platform == "douyin":
        target = {"aweme_id": link.content_id} if link.content_id else {"url": link.url}
        calls.append(ToolCall("douyin", "comments", dict(target)))
        # 恒定追加：抖音评论接口的 comment_count 类型是 integer|null，
        # 不兜底的话「评论数」这列会间歇性变空，比没有更糟。
        calls.append(ToolCall("douyin", "detail", dict(target)))

    return calls


def estimate_credits(rows: list[Row], settings: Settings, now: Optional[datetime] = None) -> int:
    """预估这一批要花多少积分（按 SocialDataX 计价：10 积分/次，1 积分 = 0.01 元）。

    批量跑之前先报数给人看，比事后对账单强。
    走 TikHub 时积分这个单位不成立，用 estimate_yuan() 看钱。
    """
    return sum(len(plan_calls(row, settings, now)) for row in rows) * 10


def estimate_yuan(
    rows: list[Row],
    settings: Settings,
    now: Optional[datetime] = None,
    keys: Optional[dict[str, str]] = None,
    disabled: Optional[set[str]] = None,
    worst_case: bool = False,
) -> float:
    """预估这一批要花多少钱，按每个平台**实际会走的那家**的单价算。

    worst_case=True 时改按**这次调用可能走到的最贵那家**算。两种口径各有各的
    用途，别混：

    * 报给人看的「预计花费」要用乐观口径（默认）——报最贵的会让每次估算
      都虚高，没人会信。
    * **预算闸门的预留必须用悲观口径**。预留时主通道是健康的，可跑到一半
      它倒了、这一行就走了贵十几倍的备胎——等 settle() 事后校正时，钱已经
      花出去了，`MAX_YUAN_PER_RUN` 在这一行上已经被越过。悲观预留 + 事后
      settle 退还差额，既不让上限被突破，也不会长期压低吞吐。

    「实际会走的那家」= 通道顺序里第一家**配了 key、本轮没被判死、且吃这种
    参数形态**的。按配置主通道计价的话，只配了备胎 key 的部署（完全合法）
    会把 SocialDataX 的账按 TikHub 的单价报——抖音差 14 倍，一个天天报错账的
    估算没人会信。keys 不传时退回按「第一家能接的」算。

    disabled 一定要传：主通道在本轮早些时候已经被判死之后，后面每一行**实际**
    走的是备胎。漏传它的后果不只是报表难看——预算闸门就是拿这个数去预留的。
    """
    dead = disabled or set()
    total = 0.0
    for row in rows:
        for call in plan_calls(row, settings, now):
            prices: list[float] = []
            for name in settings.channels.for_platform(call.platform):
                if keys is not None and not keys.get(name):
                    continue
                if name in dead:
                    continue
                try:
                    provider = get_provider(name)
                except ValueError:
                    continue
                if provider.can_handle(call.platform, call.purpose, call.arguments):
                    prices.append(provider.yuan_per_call(call.platform, call.purpose))
                    if not worst_case:
                        break
            # 一家都接不了：这一行实际会以「不支持的链接形态/没通道」失败，不产生费用
            if prices:
                total += max(prices) if worst_case else prices[0]
    return total
