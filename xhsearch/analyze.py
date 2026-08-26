"""把接口返回的评论页/详情，变成可以直接写进多维表格的值。

全部纯函数，不碰网络也不碰飞书——方便单测，离线毫秒级跑完。

两个平台的能力差异必须在这一层显式处理，不能糊过去：

* 小红书评论条目有 is_pinned / is_author_comment，且支持 sort_type=default
* 抖音评论条目**没有 is_pinned**（只有 is_hot / is_folded），也**没有 sort_type**

所以「置顶状态」在抖音侧判不了。这里不静默写「无置顶」——
那会被运营读成「没置顶」，而真相是「这个接口根本看不到置顶」，
抖音行的置顶状态列完全不碰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import DigestFormat, Settings, Thresholds


@dataclass
class CommentView:
    content: str
    like_count: int = 0
    is_pinned: bool = False
    is_author: bool = False
    ip_location: str = ""
    author_name: str = ""

    def one_line(self, fmt: DigestFormat, extra_mark: str = "") -> str:
        text = re.sub(r"\s+", " ", self.content or "").strip()
        if len(text) > fmt.per_comment_chars:
            text = text[: fmt.per_comment_chars - 1] + "…"

        marks = []
        if extra_mark:
            marks.append(extra_mark)
        if self.is_pinned:
            marks.append("置顶")
        if self.is_author:
            marks.append("作者")
        if fmt.show_like_count and self.like_count:
            marks.append(f"{self.like_count}赞")
        if fmt.show_ip_location and self.ip_location:
            marks.append(self.ip_location)

        prefix = f"[{' · '.join(marks)}] " if marks else ""
        # 昵称是个人信息：关掉 show_author_name 后一格都不落库
        # （见 DigestFormat 里的数据最小化说明）。
        who = f"{self.author_name}: " if (fmt.show_author_name and self.author_name) else ""
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
    # 运营定的口径：它返回 True 就打「风控中」——风控中只认两种硬证据
    # （审查标记、链接失效），评论数异常一律走 疑似限流 / 无水花，不进风控。
    # 字段语义还没在真实被封的帖子上验过（只见过 false），所以打标签的同时
    # 仍在诊断信息里请人工确认，见 docs/待验证清单.md。
    censored: Optional[bool] = None

    @property
    def pinned(self) -> Optional[CommentView]:
        return next((c for c in self.comments if c.is_pinned), None)

    @property
    def supports_pinned(self) -> bool:
        return self.platform == "xhs"


# 互动数字的上界。真实世界里没有 10 亿条评论的笔记；出现这种数字一定是
# 上游 schema 漂移（比如把时间戳塞进了 comment_count），不该拿它去算热度。
MAX_COUNT = 1_000_000_000


def _int_or_none(value: Any) -> Optional[int]:
    """把上游给的计数收成一个能用于判定的整数，否则 None（=这一轮没量到）。

    三件事必须挡：
    * bool —— 在 Python 里 True 是 int，`comment_count: true` 会被当成「1 条评论」，
      于是一条爆文被判成「无水花」。
    * 负数 —— 没有意义，而且会让掉量判定得出荒谬结论。
    * 超大值 —— schema 漂移的典型形态。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_COUNT else None


def _author_name(author: Any) -> str:
    if not isinstance(author, dict):
        return ""
    for key in ("name", "nickname", "nick_name"):
        value = author.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def saw_comment_page(snapshot: Snapshot) -> bool:
    """这一轮到底有没有**看到评论的内容**（不是「这条帖子还活着吗」）。

    两个问题必须分开问，答案不一样：
    * 「帖子还活着吗」——detail 兜底回填一个评论数就够证明（见
      runner._observed）。
    * 「评论里说了什么」——只有拿到评论条目、或者确认了评论数就是 0，
      才算看到过。

    曾经这两个问题共用一个判断（`comments or comment_count is not None`），
    于是出现过这一幕：评论接口返回空页、detail 回填「评论数 150」，
    程序据此对一条**一眼都没看到的**评论区下了三个结论——
    「无负面」「评论没有显示」「（暂无评论）」，还把上一轮真实的
    评论快照覆盖掉了。全是拿上游缺数当证据。

    「量到了、就是 0」和「压根没量到」的区别就在这里：前者是结论，
    后者只是没数据。
    """
    return bool(snapshot.comments) or snapshot.comment_count == 0


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


def format_digest(snapshot: Snapshot, fmt: DigestFormat,
                  hit: Optional["SeedHit"] = None) -> str:
    """把前 N 条评论排成一格能读的文本。

    排序：**命中关键词的那条排最前**（带「命中『词』」标记——运营一眼
    看到我们的评论显示出来了、显示的是哪条），然后是置顶评论，其余
    按原序接在后面。置顶在综合排序里通常就在第一位，但接口没有保证，
    这里显式提前，免得在第 7 行才看到。
    """
    if not snapshot.comments:
        return "（暂无评论）"

    hit_comment: Optional[CommentView] = None
    if hit is not None:
        hit_comment = next(
            (c for c in snapshot.comments if c.content == hit.comment), None)

    ordered = sorted(snapshot.comments,
                     key=lambda c: (c is not hit_comment, not c.is_pinned))
    lines: list[str] = []
    used = 0
    for index, comment in enumerate(ordered[: fmt.max_comments], start=1):
        mark = f"命中「{hit.keyword}」" if comment is hit_comment else ""
        line = f"{index}. {comment.one_line(fmt, extra_mark=mark)}"
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


@dataclass
class KeywordHit:
    """一条命中：命中的是哪个词、命中在哪条评论上。

    带的是整个 CommentView 而不只是正文，因为「负面评论快照」要连
    昵称/赞数/IP 一起排版——和「评论区快照」用同一套 one_line()。
    """

    keyword: str
    comment: CommentView


def match_all_keywords(snapshot: Snapshot, keywords: list[str],
                       *, skip_author: bool = False) -> list[KeywordHit]:
    """把关键词组 × 第一页评论的**全部**命中都找出来，按评论原序返回。

    和 match_seed_keywords 的区别只在「找第一个」还是「找全部」：
    种子评论只需要知道显示出来没有，找到一条就够了；负面词要让运营
    看到底下都说了些什么，得把命中的几条都列出来。

    skip_author：跳过作者本人（品牌号）的评论。负面词问的是「**别人**
    在底下说了什么」，自家回复里的「不会过敏」「比竞品A更划算」按字面
    照样命中，会把一条干净的帖子写成「有负面」。种子关键词那边**不能**
    这么跳——自家置顶的引导评论正是它要找的东西，两个方向天生相反。

    只有小红书的评论条目带作者标记（`is_author_comment`），抖音一律
    False，所以抖音行上这个开关等于没开——这是上游能力的边界，不是
    这里可以补的。

    一条评论同时命中多个词时只记第一个命中的词（按关键词在表里的顺序），
    免得同一条评论在快照里出现好几遍。
    """
    hits: list[KeywordHit] = []
    needles = [(k, _normalize(k)) for k in keywords]
    for comment in snapshot.comments:
        if skip_author and comment.is_author:
            continue
        haystack = _normalize(comment.content)
        for keyword, needle in needles:
            if needle and needle in haystack:
                hits.append(KeywordHit(keyword, comment))
                break
    return hits


def format_negative_digest(hits: list[KeywordHit], fmt: DigestFormat) -> str:
    """把命中负面词的那几条评论排成一格能读的文本。

    只放命中的，不放整页——这一列是给运营「一眼看到底下在说什么」的，
    掺进没命中的评论就得自己再找一遍。每条前面标出命中的是哪个词，
    因为「负面词」和「竞品词」混在同一个清单里，处置方式完全不同。
    """
    if not hits:
        # 刻意不留空：空单元格要留给「这一轮压根没查过」。
        # 「查过、没中」和「没查过」在运营那边是两个完全不同的结论。
        return "（未命中）"
    lines: list[str] = []
    used = 0
    for index, hit in enumerate(hits[: fmt.max_comments], start=1):
        line = f"{index}. {hit.comment.one_line(fmt, extra_mark=f'命中「{hit.keyword}」')}"
        if used + len(line) + 1 > fmt.total_chars:
            lines.append(f"…（还有 {len(hits) - index + 1} 条未显示）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


class Pin(Enum):
    """置顶判定结果。

    帖子是我们自己发的，置顶评论必然是我方置顶的——所以不需要核对
    置顶内容，只需要回答「置顶还在不在」，落成「置顶状态」单选列。
    置顶那条的内容在「评论区快照」里照常能看到（置顶排前）。
    """

    UNSUPPORTED = "unsupported"   # 抖音：接口没有 is_pinned，判不了
    PINNED = "pinned"             # 有置顶
    NONE_PINNED = "none_pinned"   # 没有置顶


def decide_pin(snapshot: Snapshot) -> Pin:
    if not snapshot.supports_pinned:
        return Pin.UNSUPPORTED
    return Pin.PINNED if snapshot.pinned is not None else Pin.NONE_PINNED


def comment_status_value(verdict: "Verdict", settings: Settings) -> Optional[str]:
    """「评论状态」单选列该写的值——由关键词命中结果驱动，直接覆盖。

    命中 = 我们的种子评论显示出来了 → 显示评论；
    配了关键词但一条没中 → 没有显示（「待评论」等旧值一并被覆盖——
    单选只显示当前状态）。

    返回 None 表示**这一轮不该碰这一列**：没填关键词的行、以及本轮
    没取到评论页内容的行都保持原样。匹配的是第一页评论，两个平台
    都拿得到，所以抖音行同样能判。
    """
    if not verdict.seed_checked:
        return None
    cs = settings.comment_status
    return cs.displayed if verdict.seed_hit is not None else cs.not_displayed


def negative_status_value(verdict: "Verdict", settings: Settings) -> Optional[str]:
    """「负面状态」单选列该写的值——由负面词/竞品词命中驱动，直接覆盖。

    和 comment_status_value 完全对称，只是方向相反：那边命中是好事，
    这边命中是要立刻去看的事。

    返回 None 表示**这一轮不该碰这一列**：没填负面词的行、以及本轮
    没取到评论页内容的行都保持原样——拿上游缺数当「无负面」的证据，
    等于给运营一个假的安全感，那比不判还糟。
    """
    if not verdict.negative_checked:
        return None
    ns = settings.negative_status
    return ns.found if verdict.negative_hits else ns.clean


def pin_status_value(verdict: "Verdict", current: str, settings: Settings) -> Optional[str]:
    """「置顶状态」单选列该写的值——直接覆盖。

    有置顶 → 置顶成功；没置顶时看这一列自己的历史：此前是 成功/掉了
    → 置顶掉了（曾经置顶过这件事不抹掉），否则 → 无置顶。

    返回 None 表示不碰这一列：抖音（接口没有置顶字段，pin_checked
    恒为 False）和没看到评论页内容的空壳轮都保持原样——拿上游缺数
    当「掉了」的证据会让运营白跑一趟。
    """
    if not verdict.pin_checked:
        return None
    ps = settings.pin_status
    if verdict.pin is Pin.PINNED:
        return ps.pinned_ok
    return ps.pinned_lost if ps.ever_pinned(current) else ps.never_pinned


@dataclass
class Verdict:
    tags: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    pin: Pin = Pin.UNSUPPORTED
    # 本轮有没有资格对置顶下结论：小红书且看到了评论页才为 True。
    # False 时「置顶状态」列不碰（抖音判不了；空壳轮不能拿缺数当证据）。
    pin_checked: bool = False
    # 关键词命中结果：seed_checked=True 且 seed_hit=None = 确认未命中；
    # seed_checked=False（没填关键词、或本轮没看到评论页）时「评论状态」不碰。
    seed_hit: Optional[SeedHit] = None
    seed_checked: bool = False
    # 负面词/竞品词命中结果，语义与上面那对完全对称：
    # negative_checked=False（没填负面词、或本轮没看到评论页）时两个负面列都不碰。
    negative_hits: list["KeywordHit"] = field(default_factory=list)
    negative_checked: bool = False


def decide(
    snapshot: Snapshot,
    settings: Settings,
    *,
    previous_comment_count: Optional[int],
    age_hours: Optional[float],
    seed_keywords: Optional[list[str]] = None,
    negative_keywords: Optional[list[str]] = None,
    current_tags: Optional[list[str]] = None,
    current_pin_status: str = "",
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
        # 发出去够久了还够不上最低热度档 → 无水花。这不是异常，就是没起来——
        # 和「疑似限流」（有量之后异常下跌）必须分开，证据完全不同。
        flopped = (tier is None and age_hours is not None
                   and age_hours >= th.flop_hours)
        # 还在冷启动窗口里 → 观察中。刚发两小时只有几条评论再正常不过，
        # 这时候判「无水花」是误标；但**什么都不写**同样是错的：
        # 空格分不出「还没巡查」「巡查了没结论」「机器坏了」，
        # 于是这一批新帖只能靠人手工去填。这一档就是为了不留空格。
        observing = (tier is None and not flopped and bool(t.observing)
                     and age_hours is not None and age_hours < th.flop_hours)
        if flopped:
            tier = t.flop
        elif observing:
            tier = t.observing
        # 棘轮：算上表里已有的档位取最高。评论被删导致数字掉下去，不该让
        # 一条帖子从「大爆」退回「爆贴」——那种异常由「疑似限流」表达。
        best = max(
            (x for x in (tier, previous_best) if x),
            key=t.rank,
            default=None,
        )
        if best:
            verdict.tags.add(best)
            if best != tier:
                verdict.notes.append(f"评论数 {count}，但曾达到「{best}」，保留高档位")
            elif flopped:
                verdict.notes.append(
                    f"发布 {age_hours:.0f} 小时评论数仍只有 {count}"
                    f"（不足 {th.tier_evaluating} 条）→ {t.flop}")
            elif observing:
                verdict.notes.append(
                    f"发布 {age_hours:.0f} 小时评论数 {count}，还在 {th.flop_hours} 小时"
                    f"冷启动窗口内，暂不下结论 → {t.observing}")
            else:
                verdict.notes.append(f"评论数 {count} → {best}")
        elif age_hours is None:
            # 「发布时间」为空：算不出发布多久，也就分不清「刚发出去」和
            # 「发了两周还是没起来」，热度档位这一轮判不了。这是**表里缺数据**，
            # 不是机器出错——如实说出来，运营把那一格填上下一轮就自动补判。
            verdict.notes.append(
                f"「{settings.fields.publish_time}」是空的，算不出发布多久，"
                "本轮判不了热度档位（把发布时间填上，下一轮自动补判）")
    else:
        # 评论数未知（抖音评论接口的 total 是 integer|null，detail 兜底又恰好
        # 失败）——这一轮对热度和风控**没有获得任何新证据**。此时必须把已有的
        # 档位和状态标签原样带上：verdict.tags 留空会让 merge 把它们整体摘掉，
        # 等于用一次上游缺数抹掉「大爆」的棘轮历史和一条在生效的风控告警。
        if previous_best:
            verdict.tags.add(previous_best)
        for keep in (t.risk, t.throttled):
            if keep in (current_tags or []):
                verdict.tags.add(keep)
        verdict.notes.append("本轮未取到评论数，热度/状态标签保持原样")

    # —— 掉量：评论被平台悄悄批量删除，往往比笔记整个失效早得多 ——
    # 打「疑似限流」而不是「风控中」：风控中只认硬证据（审查标记、链接失效），
    # 数字异常是软证据，口径分开运营才知道该去查什么。
    if (
        count is not None
        and previous_comment_count is not None
        and previous_comment_count >= th.risk_drop_min_baseline
        and count <= previous_comment_count * (1 - th.risk_drop_ratio)
    ):
        verdict.tags.add(t.throttled)
        verdict.notes.append(
            f"⚠ 评论数从 {previous_comment_count} 掉到 {count}，"
            f"跌幅超过 {int(th.risk_drop_ratio * 100)}% → {t.throttled}（也可能是删评/折叠）"
        )

    # —— 审查标记：上游明确说这条在审核/受限，才打「风控中」——
    if snapshot.censored:
        verdict.tags.add(t.risk)
        verdict.notes.append("⚠ 上游把这条标成了审核中/受限 → 风控中，请人工确认")

    # —— 置顶 ——
    verdict.pin = decide_pin(snapshot)
    # 只有小红书且本轮真的看到了评论页（有评论、或至少知道评论数）才有
    # 资格对置顶下结论——空壳轮拿上游缺数当「掉了」的证据会误报；
    # 现有通道上小红书的空壳在上游层就被译成 GONE 到不了这里，
    # 这道闸防的是上游契约漂移。
    verdict.pin_checked = snapshot.supports_pinned and saw_comment_page(snapshot)
    # 掉落的那一轮在诊断信息里额外报一声——自家帖子的置顶掉了，
    # 最该被立刻发现；之后每轮的状态由「置顶状态」列自己持续表达。
    if (
        verdict.pin_checked
        and verdict.pin is Pin.NONE_PINNED
        and current_pin_status == settings.pin_status.pinned_ok
    ):
        verdict.notes.append("⚠ 此前已确认有置顶，本轮置顶已不在")

    # —— 关键词命中：任一关键词出现在第一页任一条评论里即算命中 ——
    # 只在真的看到了评论页（有评论、或至少知道评论数）时才下结论：
    # 空壳轮（items 空 + 评论数也没拿到）写「未命中/没有显示」是拿
    # 上游缺数当证据，会诱导运营去无谓补评论。
    if seed_keywords and saw_comment_page(snapshot):
        verdict.seed_checked = True
        verdict.seed_hit = match_seed_keywords(snapshot, seed_keywords)
        if verdict.seed_hit is None:
            verdict.notes.append(
                f"⚠ 第一页 {len(snapshot.comments)} 条评论未命中任何关键词"
                f"（共 {len(seed_keywords)} 个词）→ 评论没有显示"
            )
    elif seed_keywords:
        verdict.notes.append("本轮未取到评论页内容，关键词命中与评论状态保持原样")

    # —— 负面词/竞品词：**复用上面同一份第一页评论**，不额外发任何请求 ——
    # 判定闸门和种子关键词完全一样：没填词不碰、没看到评论页不碰。
    # 后者尤其重要——拿上游缺数当「无负面」写进表里，等于给运营一个
    # 假的安全感，比不判还糟。
    if negative_keywords and saw_comment_page(snapshot):
        verdict.negative_checked = True
        verdict.negative_hits = match_all_keywords(
            snapshot, negative_keywords, skip_author=True)
        if verdict.negative_hits:
            words = "、".join(dict.fromkeys(h.keyword for h in verdict.negative_hits))
            verdict.notes.append(
                f"⚠ 第一页评论命中 {len(verdict.negative_hits)} 条负面/竞品词（{words}）"
                f"→ {settings.negative_status.found}，详见「{settings.fields.negative_digest}」"
            )
    elif negative_keywords:
        verdict.notes.append("本轮未取到评论页内容，负面词判定保持原样")

    return verdict


def gone_verdict(settings: Settings, reason: str = "",
                 current_tags: Optional[list[str]] = None) -> Verdict:
    """帖子确认取不到时的结论（已经过两击确认，不是第一次失败就走这里）。

    打「风控中」——链接失效是风控中认可的两种硬证据之一（另一种是上游
    审查标记）。事实细节（几次没取到、上游原话）在「巡查状态=已失效」
    和诊断信息里，流量状态这列只留运营真正会去筛的那一个词。
    已有的热度档位原样保留：「爆过就是爆过」的棘轮不因帖子死了而清史——
    一条大爆过的帖子被删，恰恰是最需要留着「大爆」标签供复盘的那种。
    """
    verdict = Verdict()
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
