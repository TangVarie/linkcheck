"""把接口返回的评论页/详情，变成可以直接写进多维表格的值。

全部纯函数，不碰网络也不碰飞书——方便单测，也方便整段粘进扣子代码节点。

两个平台的能力差异必须在这一层显式处理，不能糊过去：

* 小红书评论条目有 is_pinned / is_author_comment，且支持 sort_type=default
* 抖音评论条目**没有 is_pinned**（只有 is_hot / is_folded），也**没有 sort_type**

所以「置顶评论」和「置顶成功」在抖音侧做不到。这里不静默返回空值——
空值会被运营读成「没置顶」，而真相是「这个接口根本看不到置顶」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import DigestFormat, Settings, Thresholds

DOUYIN_PINNED_UNSUPPORTED = "—（抖音不支持置顶监控）"


@dataclass
class CommentView:
    content: str
    like_count: int = 0
    is_pinned: bool = False
    is_author: bool = False
    ip_location: str = ""
    author_name: str = ""

    def one_line(self, fmt: DigestFormat) -> str:
        text = re.sub(r"\s+", " ", self.content or "").strip()
        if len(text) > fmt.per_comment_chars:
            text = text[: fmt.per_comment_chars - 1] + "…"

        marks = []
        if self.is_pinned:
            marks.append("置顶")
        if self.is_author:
            marks.append("作者")
        if fmt.show_like_count and self.like_count:
            marks.append(f"{self.like_count}赞")
        if fmt.show_ip_location and self.ip_location:
            marks.append(self.ip_location)

        prefix = f"[{' · '.join(marks)}] " if marks else ""
        who = f"{self.author_name}: " if self.author_name else ""
        return f"{prefix}{who}{text}"


@dataclass
class Snapshot:
    """一篇笔记这一次刷新拿到的全部事实。"""

    platform: str
    comment_count: Optional[int] = None
    top_level_comment_count: Optional[int] = None
    comments: list[CommentView] = field(default_factory=list)
    like_count: Optional[int] = None
    collect_count: Optional[int] = None
    share_count: Optional[int] = None
    points_balance: Optional[int] = None

    # 上游自己给的审核/封禁标记（小红书 in_censor、抖音 is_prohibited/in_reviewing）。
    # 只有 TikHub 通道拿得到，SocialDataX 那边没有这个字段，所以永远可能是 None。
    # ⚠️ 刻意**不参与打标签**：这两个字段的语义还没在真实被封的帖子上验过，
    # 只见过 false。凭没验过的字段打「风控中」，一旦误报就是运营全线停投——
    # 那正是这个项目最不能犯的错。现在只写进诊断信息给人看。
    # 验过之后再决定要不要提升成判定依据，见 docs/待验证清单.md。
    censored: Optional[bool] = None

    @property
    def pinned(self) -> Optional[CommentView]:
        return next((c for c in self.comments if c.is_pinned), None)

    @property
    def supports_pinned(self) -> bool:
        return self.platform == "xhs"


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) else None


def _author_name(author: Any) -> str:
    if not isinstance(author, dict):
        return ""
    for key in ("name", "nickname", "nick_name"):
        value = author.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def read_comment_page(platform: str, data: dict[str, Any]) -> Snapshot:
    """解析评论接口的一页返回。

    小红书这一次调用就同时给到评论总数、置顶评论和综合排序的前一页评论——
    R1/R2/R3 三个需求一次拿全，不需要再调 detail。
    抖音的 comment_count 类型是 integer|null，拿不到时留 None，由上层决定兜底。
    """
    items = data.get("items")
    comments: list[CommentView] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            comments.append(
                CommentView(
                    content=str(item.get("content") or ""),
                    like_count=item.get("like_count") or 0,
                    # 抖音没有这个字段，get 返回 None → False，语义正确：
                    # 「未标记为置顶」而不是「已知未置顶」，两者的区别由
                    # Snapshot.supports_pinned 承担。
                    is_pinned=bool(item.get("is_pinned")),
                    is_author=bool(item.get("is_author_comment")),
                    ip_location=str(item.get("ip_location") or ""),
                    author_name=_author_name(item.get("author")),
                )
            )

    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    return Snapshot(
        platform=platform,
        comment_count=_int_or_none(data.get("comment_count")),
        top_level_comment_count=_int_or_none(data.get("top_level_comment_count")),
        comments=comments,
        points_balance=points.get("balance"),
    )


def merge_detail(snapshot: Snapshot, data: dict[str, Any]) -> Snapshot:
    """把 detail 接口的互动数并进快照。

    detail 的 comment_count 是 required integer，所以它同时充当抖音
    comment_count 为 null 时的兜底。
    """
    snapshot.like_count = _int_or_none(data.get("like_count"))
    snapshot.collect_count = _int_or_none(data.get("collect_count"))
    snapshot.share_count = _int_or_none(data.get("share_count"))
    if snapshot.comment_count is None:
        snapshot.comment_count = _int_or_none(data.get("comment_count"))
    if isinstance(data.get("_censored"), bool):
        snapshot.censored = data["_censored"]
    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    if points.get("balance") is not None:
        snapshot.points_balance = points["balance"]
    return snapshot


def format_pinned(snapshot: Snapshot) -> str:
    if not snapshot.supports_pinned:
        return DOUYIN_PINNED_UNSUPPORTED
    pinned = snapshot.pinned
    if pinned is None:
        return ""
    who = f"{pinned.author_name}: " if pinned.author_name else ""
    body = re.sub(r"[ \t]+", " ", pinned.content or "").strip()
    return f"{who}{body}"


def format_digest(snapshot: Snapshot, fmt: DigestFormat) -> str:
    """把前 N 条评论排成一格能读的文本。

    置顶评论排在最前面——它在综合排序里通常就在第一位，但接口没有保证，
    这里显式提到最前，免得运营在第 7 行才看到置顶。
    """
    if not snapshot.comments:
        return "（暂无评论）"

    ordered = sorted(snapshot.comments, key=lambda c: not c.is_pinned)
    lines: list[str] = []
    used = 0
    for index, comment in enumerate(ordered[: fmt.max_comments], start=1):
        line = f"{index}. {comment.one_line(fmt)}"
        if used + len(line) + 1 > fmt.total_chars:
            lines.append(f"…（还有 {len(ordered) - index + 1} 条未显示）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _normalize(text: str) -> str:
    """比对文本用的归一化：去空白、去标点、统一大小写。

    表里登记的关键词和评论区里实际打出来的字几乎不可能逐字一致
    （大小写、emoji、被平台吞掉的符号），所以只做宽松包含匹配——
    实际数据里「cGMP因子」和「cgmp因子」就是混着写的。
    """
    return re.sub(r"[\s\W_]+", "", (text or "")).lower()


@dataclass
class SeedHit:
    """关键词命中结果：哪个词、命中在哪条评论。"""

    keyword: str
    comment: str


def match_seed_keywords(snapshot: Snapshot, keywords: list[str]) -> Optional[SeedHit]:
    """评论关键词组 × 第一页评论的包含匹配。

    这查的是**我们自己的种子评论有没有显示出来**（和「蓝词」无关——
    蓝词指评论里变成超链接的词，那是人工在手机端自查的）。
    规则刻意简单：任一关键词（归一化后）出现在任一条评论里就算命中，
    按关键词在表里的顺序取第一个命中的。没有长度门槛、没有辨识度要求——
    「西地那非口溶膜」这类词本身就有辨识度。
    """
    for keyword in keywords:
        needle = _normalize(keyword)
        if not needle:
            continue
        for comment in snapshot.comments:
            if needle in _normalize(comment.content):
                return SeedHit(keyword, comment.content)
    return None


class Pin(Enum):
    """置顶判定结果。

    帖子是我们自己发的，置顶评论必然是我方置顶的——所以不需要拿关键词
    去核对置顶内容，只需要回答「置顶还在不在」。置顶的具体内容由
    「置顶评论」列原样展示。
    """

    UNSUPPORTED = "unsupported"   # 抖音：接口没有 is_pinned，判不了
    PINNED = "pinned"             # 有置顶
    NONE_PINNED = "none_pinned"   # 没有置顶


def decide_pin(snapshot: Snapshot) -> Pin:
    if not snapshot.supports_pinned:
        return Pin.UNSUPPORTED
    return Pin.PINNED if snapshot.pinned is not None else Pin.NONE_PINNED


def comment_status_values(verdict: "Verdict", settings: Settings) -> Optional[set[str]]:
    """算出「评论状态」这一列里机器该写的值——由关键词命中结果驱动。

    命中 = 我们的种子评论显示出来了 → 显示评论；
    配了关键词但一条没中 → 没有显示（「待评论」也会被这个结论替掉——
    待评论本质上就是还没显示）。

    返回 None 表示**这一轮不该碰这一列**，和「写一个空集合」完全不是一回事：
    空集合会把上一轮的结论摘掉，None 是原样保留。没填关键词的行、
    以及本轮没取到评论页内容的行都返回 None。

    和置顶无关：置顶内容由「置顶评论」列单独展示。匹配的是第一页评论，
    两个平台都拿得到，所以抖音行同样能判。
    """
    if not verdict.seed_checked:
        return None
    cs = settings.comment_status
    return {cs.displayed} if verdict.seed_hit is not None else {cs.not_displayed}


@dataclass
class Verdict:
    tags: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    pin: Pin = Pin.UNSUPPORTED
    # 关键词命中结果：seed_checked=True 且 seed_hit=None = 确认未命中；
    # seed_checked=False（没填关键词、或本轮没看到评论页）时
    # 「关键词命中」和「评论状态」两列都不碰。
    seed_hit: Optional[SeedHit] = None
    seed_checked: bool = False


def decide(
    snapshot: Snapshot,
    settings: Settings,
    *,
    previous_comment_count: Optional[int],
    age_hours: Optional[float],
    seed_keywords: Optional[list[str]] = None,
    current_tags: Optional[list[str]] = None,
    previous_pinned: Optional[str] = None,
) -> Verdict:
    """算出这一行本次应有的机器标签和置顶判定。

    只产出 settings.tags.namespace() 里的标签。人工标签由 tags.merge 保护，
    这里完全不需要知道它们的存在。
    """
    t = settings.tags
    th: Thresholds = settings.thresholds
    verdict = Verdict()

    count = snapshot.comment_count

    # —— 热度档位：互斥，取最高，且只升不降 ——
    previous_best = max(
        (tag for tag in (current_tags or []) if t.rank(tag) >= 0),
        key=t.rank,
        default=None,
    )
    if count is not None:
        tier = th.heat_tier(count, t)
        # 棘轮：算上表里已有的档位取最高。评论被删导致数字掉下去，不该让
        # 一条帖子从「大爆」退回「爆贴」——那是风控信号，由风控标签表达。
        best = max(
            (x for x in (tier, previous_best) if x),
            key=t.rank,
            default=None,
        )
        if best:
            verdict.tags.add(best)
            if best == tier:
                verdict.notes.append(f"评论数 {count} → {best}")
            else:
                verdict.notes.append(f"评论数 {count}，但曾达到「{best}」，保留高档位")
    else:
        # 评论数未知（抖音评论接口的 total 是 integer|null，detail 兜底又恰好
        # 失败）——这一轮对热度和风控**没有获得任何新证据**。此时必须把已有的
        # 档位和风控标签原样带上：verdict.tags 留空会让 merge 把它们整体摘掉，
        # 等于用一次上游缺数抹掉「大爆」的棘轮历史和一条在生效的风控告警。
        if previous_best:
            verdict.tags.add(previous_best)
        if t.risk in (current_tags or []):
            verdict.tags.add(t.risk)
        verdict.notes.append("本轮未取到评论数，热度/风控标签保持原样")

    # —— 掉量：评论被平台悄悄批量删除，往往比笔记整个失效早得多 ——
    if (
        count is not None
        and previous_comment_count is not None
        and previous_comment_count >= th.risk_drop_min_baseline
        and count <= previous_comment_count * (1 - th.risk_drop_ratio)
    ):
        verdict.tags.add(t.risk)
        verdict.notes.append(
            f"⚠ 评论数从 {previous_comment_count} 掉到 {count}，"
            f"跌幅超过 {int(th.risk_drop_ratio * 100)}%，疑似限流或删评"
        )

    # 发出去够久了还是零评论，疑似限流。
    if count == 0 and age_hours is not None and age_hours >= th.risk_zero_comment_hours:
        verdict.tags.add(t.risk)
        verdict.notes.append(
            f"⚠ 发布 {age_hours:.0f} 小时仍为 0 评论（阈值 {th.risk_zero_comment_hours} 小时）"
        )

    # —— 置顶 ——
    verdict.pin = decide_pin(snapshot)
    # 之前有过置顶、现在没了 —— 自家帖子的置顶掉了，最该被立刻发现。
    # 「之前有过」看的是上一轮写进「置顶评论」列的内容：非空且不是
    # 抖音的「不支持」占位，就说明上一轮确实看到过置顶。
    if (
        verdict.pin is Pin.NONE_PINNED
        and previous_pinned
        and previous_pinned != DOUYIN_PINNED_UNSUPPORTED
    ):
        verdict.notes.append("⚠ 此前已确认有置顶，本轮置顶已不在")

    # —— 关键词命中：任一关键词出现在第一页任一条评论里即算命中 ——
    # 只在真的看到了评论页（有评论、或至少知道评论数）时才下结论：
    # 空壳轮（items 空 + 评论数也没拿到）写「未命中/没有显示」是拿
    # 上游缺数当证据，会诱导运营去无谓补评论。
    if seed_keywords and (snapshot.comments or snapshot.comment_count is not None):
        verdict.seed_checked = True
        verdict.seed_hit = match_seed_keywords(snapshot, seed_keywords)
        if verdict.seed_hit is None:
            verdict.notes.append(
                f"⚠ 第一页 {len(snapshot.comments)} 条评论未命中任何关键词"
                f"（共 {len(seed_keywords)} 个词）→ 评论没有显示"
            )
    elif seed_keywords:
        verdict.notes.append("本轮未取到评论页内容，关键词命中与评论状态保持原样")

    return verdict


def format_seed_match(verdict: Verdict, snapshot: Snapshot) -> Optional[str]:
    """「关键词命中」列的内容：只标命中的那一条（或明确未命中）。

    None = 这一轮不碰这一列（没填关键词、或没看到评论页）。
    完整的第一页评论在「评论区快照」里，那一列是看评论区氛围的，
    这里不重复也不覆盖。
    """
    if not verdict.seed_checked:
        return None
    if verdict.seed_hit is not None:
        excerpt = re.sub(r"\s+", " ", verdict.seed_hit.comment or "").strip()[:60]
        return f"✅ 命中「{verdict.seed_hit.keyword}」：{excerpt}"
    return f"❌ 未命中（第一页 {len(snapshot.comments)} 条评论）"


def gone_verdict(settings: Settings, reason: str = "",
                 current_tags: Optional[list[str]] = None) -> Verdict:
    """帖子确认取不到时的结论（已经过两击确认，不是第一次失败就走这里）。

    同时打 已失效 和 风控 ——「已失效」说明事实，「风控」是运营真正会去筛的那一列。
    已有的热度档位原样保留：「爆过就是爆过」的棘轮不因帖子死了而清史——
    一条大爆过的帖子被删，恰恰是最需要留着「大爆」标签供复盘的那种。
    """
    verdict = Verdict()
    verdict.tags.add(settings.tags.gone)
    verdict.tags.add(settings.tags.risk)
    t = settings.tags
    best = max(
        (tag for tag in (current_tags or []) if t.rank(tag) >= 0),
        key=t.rank,
        default=None,
    )
    if best:
        verdict.tags.add(best)
    verdict.notes.append(reason or "接口返回内容不存在/已删除/无法访问")
    return verdict


def suspect_verdict(settings: Settings, strikes: int, reason: str = "") -> Verdict:
    """第一次取不到时的结论：只记，不定罪。

    刻意**不打任何标签**——一次网络抖动或上游抽风就把好帖子标成风控，
    运营会全线停投，而这种信任一旦丢了补不回来。
    """
    verdict = Verdict()
    verdict.notes.append(
        (reason or "本轮未取到内容") + f"（第 {strikes} 次，达到 2 次才判定失效，稍后自动复检）"
    )
    return verdict
