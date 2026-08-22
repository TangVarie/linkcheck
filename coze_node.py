"""
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



# =============================================================================
#  来自 xhsearch/config.py
# =============================================================================

@dataclass
class FieldNames:
    """多维表格列名。左边是代码里的角色，右边是表头文字。

    默认值按 OKMAN 系列表的实际结构对齐（2026-08 以「OKMAN第一期」为准）：
    巡查三件套（是否巡查/巡查状态/最近检查时间）由本系统接管，
    「实时数据.评论数」是评论数落点。
    表里还没有的列（评论关键词、关键词命中、上次点赞数等）要先在飞书里建好——
    doctor 会列出缺哪些；没建的列会被自动跳过并在日志里提示，不会写坏表。

    ⚠️ 「蓝词字段」机器**完全不读不写**：蓝词（评论里变成超链接的词）
    是人工在手机端自查的，和「评论关键词」（查我们自己的种子评论有没有
    显示出来）是两回事。接口数据里目前拿不到「这个词有没有变成超链接」，
    所以两者没有任何关联（见 docs/待验证清单.md）。
    """

    # —— 人工维护 ——
    link: str = "反馈链接"
    publish_time: str = "发布时间"
    seed_keywords: str = "评论关键词"         # 多选或文本（顿号/逗号分隔）：一组词，
                                              # 任一出现在第一页任一条评论里 = 我们的
                                              # 评论显示出来了。注意不是「蓝词字段」！
    monitoring: str = "是否巡查"              # 复选框；取消勾选即停止刷新
    queued: str = "排队刷新"                  # 复选框；勾上=手动请求刷新，机器处理完自动清掉

    # —— 机器写入 · 运营主视图 ——
    platform: str = "平台"
    comment_count: str = "实时数据.评论数"
    previous_comment_count: str = "上次评论数"
    like_count: str = "点赞数"
    previous_like_count: str = "上次点赞数"
    collect_count: str = "收藏数"
    previous_collect_count: str = "上次收藏数"
    pinned_comment: str = "置顶评论"          # 实际置顶的那条内容（自家帖子，置顶必为我方）
    comment_status: str = "评论状态"          # 由关键词命中驱动：命中=显示评论，
                                              # 未命中=没有显示；没填关键词的行不碰
    comment_digest: str = "评论区快照"
    seed_match: str = "关键词命中"            # 命中了哪个词、命中在哪条评论（单独标明，
                                              # 不覆盖「评论区快照」——快照是看氛围的）
    traffic_status: str = "流量状态"          # 多选，人机共用（无水花等人工值不碰）
    refresh_status: str = "巡查状态"
    failure_reason: str = "诊断信息"
    last_updated: str = "最近检查时间"
    alive_confirmed: str = "已确认存活"       # 复选框：本轮取到数=勾上，确认失效=取消

    # —— 机器写入 · 系统列（建议在运营视图里隐藏）——
    consecutive_failures: str = "连续失败次数"   # 两击定罪的计数器

    def must_read(self) -> list[str]:
        """search 时必须拉回来的列。少读一个就会写错。"""
        return [
            self.link,
            self.publish_time,
            self.seed_keywords,
            self.monitoring,
            self.queued,
            self.traffic_status,          # 合并多选必须先读现值
            self.comment_count,           # 判定掉量必须知道上一次的数
            self.like_count,              # 搬「上次点赞数」要先读现值
            self.collect_count,           # 搬「上次收藏数」同理
            self.last_updated,            # 分层刷新靠它判断到期
            self.consecutive_failures,    # 两击定罪
            self.comment_status,          # 合并写回前要先读现值
            self.pinned_comment,          # 判断置顶是不是刚掉的（和上一轮的置顶内容比）
        ]


@dataclass
class Tags:
    """机器管辖的标签。必须穷举——漏一个，那个标签就再也撤不回来。

    热度三档（评估中 / 爆贴 / 大爆）是**互斥**的：一条帖子同时挂着三个没有意义，
    所以每轮只留最高的那一个。而且只升不降（棘轮）——爆过就是爆过，
    评论被删导致数字掉下去不该让它从「大爆」退回「爆贴」，
    那种情况该由 风控 标签来表达。

    风控 / 已失效反映**当前状态**，每轮重算，恢复正常要能自动摘掉，
    否则表会越来越红，最后没人看。
    """

    evaluating: str = "评估中"
    hot: str = "爆贴"
    super_hot: str = "大爆"
    risk: str = "风控中"
    gone: str = "已失效"

    def heat_tiers(self) -> list[str]:
        """热度档位，由低到高。互斥，同时只留一个。"""
        return [self.evaluating, self.hot, self.super_hot]

    def namespace(self) -> list[str]:
        return [*self.heat_tiers(), self.risk, self.gone]

    def rank(self, tag: str) -> int:
        """热度档位的高低。不是热度标签返回 -1。"""
        tiers = self.heat_tiers()
        return tiers.index(tag) if tag in tiers else -1


@dataclass
class CommentStatus:
    """「评论状态」列的取值：我们的种子评论有没有显示出来。

    判定完全由「评论关键词」的命中结果驱动（和置顶无关——置顶内容
    由「置顶评论」列单独展示）：

        显示评论  —— 任一关键词出现在第一页任一条评论里
        没有显示  —— 配了关键词，但第一页一条都没命中
        待评论    —— 人工排期用的旧值：机器**从不写**，但拿到结论时会
                     替掉它——「待评论」本质上也是「还没显示」，有了
                     自动巡查的结论就不需要人工再标了

    三个值互斥。没填关键词的行机器完全不碰这一列。
    两个平台都能判——匹配的是第一页评论内容，不依赖置顶字段，
    所以抖音行同样有效。
    """

    displayed: str = "显示评论"
    not_displayed: str = "没有显示"
    pending: str = "待评论"

    def namespace(self) -> list[str]:
        """机器管辖的值（含只撤不写的「待评论」）。"""
        return [self.displayed, self.not_displayed, self.pending]

    def machine_written(self) -> list[str]:
        """机器实际会写入的值——doctor 检查选项是否建好用这个清单。"""
        return [self.displayed, self.not_displayed]


@dataclass
class Thresholds:
    """判定口径。

    热度三档互斥，取最高：
        ≥ 20 → 评估中
        ≥ 50 → 爆贴
        ≥ 100 → 大爆
    """

    tier_evaluating: int = 20
    tier_hot: int = 50
    tier_super_hot: int = 100

    # 评论数相对上次下跌超过这个比例，判定疑似风控（限流/删评/折叠）。
    risk_drop_ratio: float = 0.5
    # 上次评论数低于这个值时不做掉量判定——从 3 掉到 1 没有意义。
    risk_drop_min_baseline: int = 20

    # 发布多久之后仍然零评论，判定疑似限流。太短会把正常冷启动误报成风控。
    risk_zero_comment_hours: int = 48

    def heat_tier(self, count: int, tags: "Tags") -> str | None:
        """按评论数算热度档位。返回 None 表示还够不上最低档。"""
        if count >= self.tier_super_hot:
            return tags.super_hot
        if count >= self.tier_hot:
            return tags.hot
        if count >= self.tier_evaluating:
            return tags.evaluating
        return None


@dataclass
class DigestFormat:
    """评论区快照的排版。计费按页不按条，所以能存多少存多少。"""

    max_comments: int = 8
    per_comment_chars: int = 40
    total_chars: int = 700
    show_like_count: bool = True
    show_ip_location: bool = True


@dataclass
class RefreshTiers:
    """分层刷新：越新的帖子刷得越勤。

    这不是一套调度代码，就是筛选时的一个条件：
    「发布时间落在这一层 且 最后更新时间早于 now - interval_hours」= 该刷了。
    """

    tiers: list[tuple[int, int]] = field(
        default_factory=lambda: [
            (2, 8),      # 发布 0-2 天：每 8 小时一次（风控和置顶的关键窗口）
            (7, 24),     # 3-7 天：每天一次
            (30, 72),    # 8-30 天：每 3 天一次
        ]
    )
    archive_after_days: int = 30     # 超过就不再自动刷，只保留手动触发

    def interval_hours_for_age(self, age_days: float) -> int | None:
        """返回该年龄的帖子应有的刷新间隔；None 表示已归档。

        归档界线由 archive_after_days 决定；tiers 只决定归档前的刷新节奏，
        超出最后一档年龄但还没到归档线的，沿用最后一档的间隔。
        （默认两者都是 30 天，行为不变；把 archive_after_days 调大才有差别。）
        """
        if age_days > self.archive_after_days:
            return None
        for max_age_days, interval_hours in self.tiers:
            if age_days <= max_age_days:
                return interval_hours
        return self.tiers[-1][1] if self.tiers else None


@dataclass
class Safety:
    """防误伤参数。这一组的每个默认值都是为了「宁可少报，不可错报」。"""

    # 连续多少次取不到才敢判「已失效」。一次网络抖动就把好帖子标成风控，
    # 运营会全线停投，信任一旦丢了就补不回来。
    strikes_before_gone: int = 2

    # 一批里判定为「失效」的比例超过这个数（且样本够大）就整批作废。
    # 几百条笔记不可能在同一小时里被集体删除 —— 那是上游故障或话术改版。
    breaker_gone_ratio: float = 0.2
    breaker_min_sample: int = 10

    # 同一行在这个时间窗内刚成功刷过就跳过，不花积分。
    # 有人连点 200 次按钮 = 1 次真实调用。
    cooldown_seconds: int = 90


@dataclass
class Channels:
    """双通道：每个平台走哪几家数据供应商，按顺序降级。

    默认两个平台都优先 TikHub，SocialDataX 作备胎。理由（都是实测的，
    完整对比见 docs/供应商对比.md）：

    * 抖音便宜 93%（¥0.10 → ¥0.0072 一次），小红书便宜 28%
    * 两家需要的字段都拿得到——TikHub 的置顶标记藏在 `show_tags_v2` 里，
      名字不叫 is_pinned，但确实有
    * SocialDataX 的错误码是有契约的（1006 封控 / 1008 已删除，
      厂商自己标了「不要重试」），TikHub 没有这张表。所以它更适合当备胎：
      平时不花钱，主通道挂了或余额空了立刻顶上

    降级只在**主通道自己有问题**时发生（网络故障、Key 失效、余额耗尽），
    不会因为「这条笔记没了」而去第二家再花一次钱——那是行级结论，不是故障。

    只想用一家：把列表写成单元素即可，行为和改造前完全一致。
        settings.channels.order = {"xhs": ["socialdatax"], "douyin": ["socialdatax"]}
    """

    order: dict[str, list[str]] = field(default_factory=lambda: {
        "xhs": ["tikhub", "socialdatax"],
        "douyin": ["tikhub", "socialdatax"],
    })

    def for_platform(self, platform: str) -> list[str]:
        return list(self.order.get(platform) or ["socialdatax"])

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for names in self.order.values():
            for name in names:
                if name not in seen:
                    seen.append(name)
        return seen

    def primary(self, platform: str) -> str:
        names = self.for_platform(platform)
        return names[0] if names else "socialdatax"


@dataclass
class Settings:
    fields: FieldNames = field(default_factory=FieldNames)
    tags: Tags = field(default_factory=Tags)
    comment_status: CommentStatus = field(default_factory=CommentStatus)
    thresholds: Thresholds = field(default_factory=Thresholds)
    digest: DigestFormat = field(default_factory=DigestFormat)
    refresh: RefreshTiers = field(default_factory=RefreshTiers)
    safety: Safety = field(default_factory=Safety)
    channels: Channels = field(default_factory=Channels)

    # 小红书笔记发布多少天内额外调一次 detail 拿点赞/收藏。
    # 设为 0 表示完全不调 detail（省一半钱，代价是没有爆文的点赞维度）。
    detail_within_days: int = 7

    # SocialDataX 官方 skill 明文要求最多 3 并发，不要突发请求。留一档余量。
    max_concurrency: int = 2

    # 单次运行的软截止。扣子代码节点硬上限 60 秒，留足写回时间。
    # 独立服务跑批量时设成 0（不限）。
    soft_deadline_seconds: float = 45.0


# =============================================================================
#  来自 xhsearch/links.py
# =============================================================================

Platform = Literal["xhs", "douyin"]

# 小红书笔记 ID 固定 24 位小写十六进制。
_XHS_NOTE_ID = r"[0-9a-f]{24}"

_XHS_DOMAINS = re.compile(
    r"(?:xiaohongshu\.com|xhslink\.com|xhslink\.cn|xhsurl\.com|xhsurl\.cn)",
    re.I,
)
_DOUYIN_DOMAINS = re.compile(
    r"(?:douyin\.com|iesdouyin\.com)",
    re.I,
)

# 按优先级排列：越靠前的形式越明确。
_XHS_ID_PATTERNS = [
    re.compile(rf"/explore/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/discovery/item/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/search_result/({_XHS_NOTE_ID})", re.I),
    # 主页内笔记链接 /user/profile/<uid>/<note_id>
    re.compile(rf"/user/profile/[0-9a-f]{{24}}/({_XHS_NOTE_ID})", re.I),
]

_DOUYIN_ID_PATTERNS = [
    re.compile(r"douyin\.com/video/(\d{6,})", re.I),
    re.compile(r"douyin\.com/note/(\d{6,})", re.I),
    re.compile(r"[?&]modal_id=(\d{6,})", re.I),
    re.compile(r"/share/video/(\d{6,})", re.I),
]

# 从一段分享文案里把 URL 抠出来。中文分享文案常把链接和文字、标点黏在一起，
# 所以右边界除了常见中文标点，还要排掉整个 CJK 区段和左侧括号——
# 「看这条https://v.douyin.com/xxx很火」这种没有空格的写法，
# 不排 CJK 就会把「很火」吞进 URL 里，短链直接 404。
_URL_RE = re.compile(r"https?://[^\s，。、！？；：）】》（《【\"'<>一-鿿]+", re.I)

# 裸 ID：整格就是一个 ID 的情况（运营有时只贴 ID）。
_BARE_XHS_ID = re.compile(rf"^\s*({_XHS_NOTE_ID})\s*$", re.I)
# 抖音 aweme_id 是纯数字，歧义太大（订单号、手机号、其它平台 ID 都长这样），
# 所以只在文本里出现「抖音」二字时才认，且要求是独立的一串数字。
_STANDALONE_DOUYIN_ID = re.compile(r"(?<!\d)(\d{15,25})(?!\d)")


@dataclass(frozen=True)
class ParsedLink:
    """一格链接的解析结果。

    platform 为 None 表示无法判定平台，调用方应把该行标记为「链接无法识别」
    而不是硬猜——猜错会去调错平台的接口，白花积分还写回错数据。
    """

    platform: Optional[Platform]
    content_id: Optional[str]
    url: Optional[str]
    raw: str

    @property
    def usable(self) -> bool:
        return self.platform is not None and (self.content_id or self.url) is not None

    def describe_failure(self) -> str:
        if self.platform is None:
            return "无法判定平台：既没匹配到小红书域名/ID，也没匹配到抖音域名/ID"
        return "已识别平台但没有可用的 ID 或链接"


def _first_url(text: str, domain_re: re.Pattern[str]) -> Optional[str]:
    for candidate in _URL_RE.findall(text):
        if domain_re.search(candidate):
            return candidate.rstrip(".,;")
    return None


def _match_id(text: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return None


def parse(cell: str) -> ParsedLink:
    """把一格原始文本解析成平台 + ID/链接。

    单格里出现多个链接时取第一个能识别的；这是刻意的——一行代表一篇笔记，
    一格塞多个链接本身就是数据录入问题，应该在表里暴露出来而不是在这里猜。
    """
    raw = (cell or "").strip()
    if not raw:
        return ParsedLink(None, None, None, raw)

    # 全角字符会让域名匹配失败，先归一化一遍常见的几个。
    text = raw.replace("：", ":").replace("／", "/").replace("？", "?")

    bare_xhs = _BARE_XHS_ID.match(text)
    if bare_xhs:
        return ParsedLink("xhs", bare_xhs.group(1).lower(), None, raw)

    has_xhs_domain = bool(_XHS_DOMAINS.search(text))
    has_douyin_domain = bool(_DOUYIN_DOMAINS.search(text))

    # 同时命中两个平台的域名 —— 拒绝猜测。
    if has_xhs_domain and has_douyin_domain:
        return ParsedLink(None, None, None, raw)

    if has_xhs_domain:
        note_id = _match_id(text, _XHS_ID_PATTERNS)
        return ParsedLink(
            "xhs",
            note_id.lower() if note_id else None,
            None if note_id else (_first_url(text, _XHS_DOMAINS) or raw),
            raw,
        )

    if has_douyin_domain:
        aweme_id = _match_id(text, _DOUYIN_ID_PATTERNS)
        return ParsedLink(
            "douyin",
            aweme_id,
            None if aweme_id else (_first_url(text, _DOUYIN_DOMAINS) or raw),
            raw,
        )

    # 没有域名。裸抖音 ID 只在文本里明确提到「抖音」时才认。
    if "抖音" in raw:
        bare_douyin = _STANDALONE_DOUYIN_ID.search(text)
        if bare_douyin:
            return ParsedLink("douyin", bare_douyin.group(1), None, raw)

    return ParsedLink(None, None, None, raw)


# =============================================================================
#  来自 xhsearch/protocol.py
# =============================================================================

BASE = "https://mcp.socialdatax.com/socialdatax/api/v1"

# 端点。请求体就是参数本身，不需要 JSON-RPC 信封。
ENDPOINTS = {
    ("xhs", "comments"): "/xhs/note/comment/list",
    ("xhs", "detail"): "/xhs/note/detail",
    ("douyin", "comments"): "/douyin/video/comment/list",
    ("douyin", "detail"): "/douyin/video/detail",
}


def endpoint(platform: str, purpose: str) -> str:
    path = ENDPOINTS.get((platform, purpose))
    if path is None:
        raise ValueError(f"没有 {platform}/{purpose} 的端点")
    return BASE + path


def headers(api_key: str) -> dict[str, str]:
    # 规范明确：Authorization 和 X-API-Key 只能用一种，同时传会被判为配置冲突。
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_body(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False)


class Failure(Enum):
    """错误分类。决定的是「这次失败该怎么办」，而不是「错在哪」。"""

    AUTH = "auth"              # key 缺失/失效 —— 整批停，重试无意义
    QUOTA = "quota"            # 积分不足 —— 整批停，保住已完成的结果
    RATE_LIMIT = "rate_limit"  # 限流 —— 退避后重试同一条
    GONE = "gone"              # 内容不存在/已删除/被限制 —— 行级结论，不是故障
    TRANSPORT = "transport"    # 超时、5xx、服务暂时不可用 —— 重试
    UNKNOWN = "unknown"        # 没见过的错 —— 行级失败，把原文写回表里给人看


# 整批必须立刻停下的错误。
FATAL = frozenset({Failure.AUTH, Failure.QUOTA})
# 值得退避重试的错误。
RETRYABLE = frozenset({Failure.RATE_LIMIT, Failure.TRANSPORT})

# 官方错误码 → 分类。全部来自 OpenAPI 规范的 x-socialdatax-error-contract。
# 第三个元素表示「这个结论是否权威」：权威的可以直接定罪，不用等第二次确认。
_CODES: dict[int, tuple[Failure, bool]] = {
    1400: (Failure.UNKNOWN, True),      # validation_error   请求体参数不正确（HTTP 400）
    1401: (Failure.AUTH, True),         # authentication_error（HTTP 401）
    1429: (Failure.RATE_LIMIT, True),   # rate_limited（HTTP 429）
    1001: (Failure.UNKNOWN, True),      # invalid_argument   ID/链接不符合接口要求
    1002: (Failure.UNKNOWN, True),      # invalid_pagination_state（我们从不翻页，不该出现）
    1003: (Failure.GONE, False),        # not_found          不存在，或公开访问不可见
    1004: (Failure.QUOTA, True),        # insufficient_balance
    1005: (Failure.TRANSPORT, True),    # service_failure    服务暂时不可用
    1006: (Failure.GONE, False),        # content_unavailable 权限/状态/平台限制导致不可读 ← 封控
    1007: (Failure.TRANSPORT, True),    # surface_unavailable 页面暂时不可访问
    1008: (Failure.GONE, True),         # content_deleted    规范原文「不要重试」→ 可直接定罪
}

# 兜底：错误码没命中时，从描述文案里找线索。有了官方错误码表之后，
# 这里只是防御——正常情况下不该走到。
# ⚠️ 顺序即优先级：TRANSPORT 必须排在 GONE 前面——「当前内容暂时不可用」这类
# 瞬时故障文案包含「不可用」三个字，GONE 在前会把一次临时故障计成死亡一击，
# 两击定罪的第一击就这么被白送了。
_MESSAGE_HINTS: list[tuple[tuple[str, ...], Failure]] = [
    (("积分不足", "余额不足", "insufficient"), Failure.QUOTA),
    (("过于频繁", "频率", "rate limit"), Failure.RATE_LIMIT),
    (("暂时不可用", "稍后重试", "service", "网络错误", "超时", "timed out"), Failure.TRANSPORT),
    (("不存在", "已删除", "已下架", "不可用", "违规", "not found", "deleted"), Failure.GONE),
    (("API Key", "鉴权", "unauthorized"), Failure.AUTH),
]


@dataclass
class Ok:
    data: dict[str, Any]
    points_cost: Optional[int] = None
    points_balance: Optional[int] = None
    request_id: str = ""


@dataclass
class Err:
    kind: Failure
    code: str
    message: str
    retry_after_seconds: Optional[float] = None
    http_status: Optional[int] = None
    request_id: str = ""
    # 上游明确给出结论（比如 1008 内容已删除），可以直接定罪不用等第二次确认。
    definitive: bool = False

    def __str__(self) -> str:
        text = f"[{self.kind.value}/{self.code}] {self.message}"
        return f"{text}（request_id={self.request_id}）" if self.request_id else text

    def operator_text(self) -> str:
        """写进表里给运营看的版本。

        必须带 request_id ——那是找厂商排查的唯一凭据，运营看不懂错误内容也没关系，
        直接截图发过去就行。省掉它，一个真实故障可能要多花几天才定位。
        """
        parts = [self.message]
        if self.code and self.code != "unknown":
            parts.append(f"错误码 {self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return f"{parts[0]}（{'，'.join(parts[1:])}）" if len(parts) > 1 else parts[0]


def _classify(code: Any, message: str, http_status: Optional[int]) -> tuple[Failure, bool]:
    try:
        known = _CODES.get(int(code))
    except (TypeError, ValueError):
        known = None
    if known:
        return known

    haystack = f"{code} {message}".lower()
    for needles, kind in _MESSAGE_HINTS:
        if any(n.lower() in haystack for n in needles):
            return kind, False

    if http_status is not None:
        if http_status in (401, 403):
            return Failure.AUTH, True
        if http_status == 402:
            return Failure.QUOTA, True
        if http_status == 404:
            return Failure.GONE, False
        if http_status == 429:
            return Failure.RATE_LIMIT, True
        if http_status >= 500 or http_status == 0:
            # 0 = 传输层的超时/拒连/DNS 失败（transport.py 的约定）。
            # 漏掉这条会把网络故障归成 UNKNOWN——既不重试也不降级，
            # SocialDataX 作主通道时双通道就白配了（TikHub 侧一直有对应规则）。
            return Failure.TRANSPORT, True

    return Failure.UNKNOWN, False


def _err(payload: dict[str, Any], http_status: Optional[int], request_id: str) -> Err:
    code = payload.get("code")
    message = str(payload.get("message") or json.dumps(payload, ensure_ascii=False)[:300])
    kind, definitive = _classify(code, message, http_status)
    # 字段名来自官方规范，不是猜的。429 还会同步返回 Retry-After 响应头。
    retry_after = payload.get("retry_after_seconds")
    return Err(
        kind=kind,
        code=str(code if code is not None else "unknown"),
        message=message,
        retry_after_seconds=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        http_status=http_status,
        request_id=request_id,
        definitive=definitive,
    )


def _ok(data: dict[str, Any], request_id: str) -> Ok:
    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    return Ok(
        data=data,
        points_cost=points.get("cost"),
        points_balance=points.get("balance"),
        request_id=request_id,
    )


def iter_sse_payloads(body: str):
    """从 SSE 响应体里把每个 data: 帧的 JSON 抠出来。

    REST 接口不返回 SSE，这里留着是防御：万一哪天端点换了行为，或者有人把
    这套解析复用到 MCP 端点上（那条路的成功响应确实是 SSE 分帧）。
    按 SSE 规范，一帧可能有多行 data:，需要用换行拼接后再解析。
    """
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
            continue
        if not line.strip() and buffer:
            chunk = "\n".join(buffer)
            buffer = []
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                continue
    if buffer:
        try:
            yield json.loads("\n".join(buffer))
        except json.JSONDecodeError:
            pass


Result = Ok | Err


def parse_response(
    http_status: int,
    content_type: str,
    body: str,
    request_id: str = "",
) -> Result:
    """把一次 HTTP 响应解析成 Ok 或 Err。

    关键：**HTTP 200 不等于成功**。业务错误也走 200，靠 body 里有没有 `code`
    字段来区分。成功响应不带 `code`（规范原文：「成功响应不会返回该字段」）。
    """
    body = body or ""

    # 防御分支：SSE（REST 不会走到，MCP 端点会）
    if "text/event-stream" in (content_type or "").lower():
        for frame in iter_sse_payloads(body):
            if isinstance(frame, dict):
                inner = frame.get("result")
                if isinstance(inner, dict):
                    structured = inner.get("structuredContent")
                    if isinstance(structured, dict):
                        return _ok(structured, request_id)
        return Err(Failure.UNKNOWN, "no_data_frame",
                   f"SSE 响应里没有可用的 data 帧（前 300 字符：{body[:300]}）",
                   http_status=http_status, request_id=request_id)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        kind, definitive = _classify("", body, http_status)
        return Err(kind, f"http_{http_status}", body[:500] or f"HTTP {http_status}",
                   http_status=http_status, request_id=request_id, definitive=definitive)

    if not isinstance(payload, dict):
        return Err(Failure.UNKNOWN, "unexpected_body", body[:500],
                   http_status=http_status, request_id=request_id)

    # 有 code 就是错误 —— 无论 HTTP 状态是不是 200。
    if "code" in payload:
        return _err(payload, http_status, request_id)

    if http_status >= 400:
        kind, definitive = _classify("", body, http_status)
        return Err(kind, f"http_{http_status}", body[:500],
                   http_status=http_status, request_id=request_id, definitive=definitive)

    return _ok(payload, request_id)


# =============================================================================
#  来自 xhsearch/providers.py
# =============================================================================

# 直接导名字，不要 `from . import protocol` —— 打包进扣子时模块会被拼成一个
# 扁平文件，那里根本没有 protocol 这个名字，用 protocol.xxx 会当场 NameError。
# 也必须写成一行：打包脚本是按行剥 import 的，跨行的括号形式会留下半截语法。

SOCIALDATAX = "socialdatax"
TIKHUB = "tikhub"

# TikHub 有两个同功能的域名，**按你的服务器在哪选**（对方文档要求「请勿跨区使用」）：
#   api.tikhub.dev   境内可直连（api.tikhub.io 在大陆被防火墙拦截）
#   api.tikhub.io    主域名，境外服务器（Railway / GitHub Actions）用这个
# 默认取 .dev：它两边都通（境外也实测可用），配错了最多是慢一点，不会不通。
# 跑在境外就设环境变量 TIKHUB_BASE=https://api.tikhub.io。
TIKHUB_BASE = "https://api.tikhub.dev"


def set_tikhub_base(url: str) -> None:
    """改 TikHub 的接入域名。

    做成函数而不是读环境变量，是因为这份代码要整段粘进扣子代码节点——
    那边没有环境变量这回事。宿主（cli.py）负责从环境里读，扣子那边用默认值。
    """
    global TIKHUB_BASE
    cleaned = (url or "").strip().rstrip("/")
    if cleaned:
        TIKHUB_BASE = cleaned

# Cloudflare 会按 UA 拦截，裸的 Python-urllib/3.x 直接 403。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

TIKHUB_PATHS = {
    ("xhs", "comments"): "/api/v1/xiaohongshu/app_v2/get_note_comments",
    # 图文接口对视频笔记同样返回完整互动数据（只是没有播放地址，我们不需要），
    # 所以两种笔记共用一个端点，不必先判类型再选接口。实测已确认。
    ("xhs", "detail"): "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
    ("douyin", "comments"): "/api/v1/douyin/app/v3/fetch_video_comments",
    ("douyin", "detail"): "/api/v1/douyin/app/v3/fetch_one_video",
}

# 单价（元/次）。TikHub 的数字来自它自己公开免鉴权的计价接口
# https://api.tikhub.dev/api/v1/tikhub/user/get_all_endpoints_info
# ⚠️ 小红书那两个端点 allow_discount=0，走量折扣对它们不生效。
_USD_TO_CNY = 7.2
TIKHUB_USD = {"xhs": 0.010, "douyin": 0.001}
SOCIALDATAX_YUAN = 0.10   # 10 积分 × ¥0.01，全平台统一价


_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def _quote(value: Any) -> str:
    """百分号编码。刻意不用 urllib.parse——这份代码要整段粘进扣子代码节点，
    那边禁用了 http.client，围绕 urllib 有多少能用、多少不能用没有文档保证。
    一个六行的编码器换掉一整个不确定性，很划算。"""
    out = []
    for byte in str(value).encode("utf-8"):
        char = chr(byte)
        out.append(char if char in _SAFE else f"%{byte:02X}")
    return "".join(out)


def _query(params: dict[str, Any]) -> str:
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in params.items())


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: str = ""


def _handles_everything(platform: str, purpose: str, arguments: dict[str, Any]) -> bool:
    return True


def _tikhub_can_handle(platform: str, purpose: str, arguments: dict[str, Any]) -> bool:
    """TikHub 吃不吃这种参数形态。

    抖音的图文和视频共用同一对端点（aweme 本来就两种都算），**形态无关**；
    但这对端点只收数字 aweme_id，不收链接——OpenAPI 写明 aweme_id 是「作品id」，
    解析分享链接是另外的端点。把 v.douyin.com 短链塞进 aweme_id 必然失败，
    还会被归一化成 GONE 嫌疑：两轮之后一条活着的内容就被标成「已失效」，钱照扣。
    所以这种行让 TikHub 直接让位给吃链接的 SocialDataX。
    小红书不受影响：share_text 参数长链短链分享文案都吃（实测）。
    """
    if platform == "douyin" and not arguments.get("aweme_id"):
        return False
    return True


@dataclass
class Provider:
    name: str
    label: str
    build: Callable[[str, str, str, dict[str, Any]], Request]
    parse: Callable[..., Result]
    yuan_per_call: Callable[[str, str], float]
    # 「查不到内容」这种业务失败，这家收不收钱。
    #
    # TikHub：**收**。它自己的接口文档写着「传入错误或不存在的笔记ID……
    # 该请求同样会正常计费扣费」，实测余额确实少了 $0.01。
    # SocialDataX：**未知**，只有蒲公英那组工具明文写了失败不扣费。
    # 这里保守当作不收——宁可少报一点点备胎的钱，也不要凭猜测把账单虚高，
    # 一个天天虚报成本的监控没人会信。验完见 docs/待验证清单.md 第 6 项。
    bills_failed_lookups: bool = False
    # 这家能不能处理这种（平台, 用途, 参数）组合。挑通道时先过这一关，
    # 免得把请求发给一个注定报错的端点——白花钱还可能把行判成失效。
    can_handle: Callable[[str, str, dict[str, Any]], bool] = _handles_everything


# =============================================================================
#  SocialDataX：原样透传，因为 analyze 就是照它的字段写的
# =============================================================================


def _sdx_build(api_key: str, platform: str, purpose: str, arguments: dict[str, Any]) -> Request:
    args = dict(arguments)
    # 抽象参数 → SocialDataX 的参数名
    if platform == "xhs" and purpose == "comments":
        args["sort_type"] = args.pop("sort", "default")
    else:
        args.pop("sort", None)
    if "url" in args and platform == "xhs":
        args["note_url"] = args.pop("url")
    return Request(
        "POST",
        endpoint(platform, purpose),
        headers(api_key),
        build_body(args),
    )


def _sdx_parse(platform: str, purpose: str, http_status: int, content_type: str,
               body: str, request_id: str = "", api_key: str = "") -> Result:
    return parse_response(http_status, content_type, body, request_id)


# =============================================================================
#  TikHub：原始上游响应，需要归一化
# =============================================================================


def _tikhub_build(api_key: str, platform: str, purpose: str, arguments: dict[str, Any]) -> Request:
    path = TIKHUB_PATHS.get((platform, purpose))
    if path is None:
        raise ValueError(f"TikHub 没有 {platform}/{purpose} 的端点")

    args = dict(arguments)
    params: dict[str, Any] = {}

    if platform == "xhs":
        if args.get("note_id"):
            params["note_id"] = args["note_id"]
        elif args.get("url"):
            # 分享链接走 share_text，长链短链都吃。
            params["share_text"] = args["url"]
        if purpose == "comments":
            params["index"] = 0
            # 只取第一页，永远不翻页。对方文档说 default 排序「翻页时会丢失或
            # 重复评论」——那是分页场景的问题，对我们不成立。而综合排序才是
            # 运营眼里的评论区，也是置顶评论出现的那一屏。
            params["sort_strategy"] = args.get("sort", "default")
    else:
        if args.get("aweme_id"):
            params["aweme_id"] = args["aweme_id"]
        elif args.get("url"):
            params["aweme_id"] = args["url"]
        if purpose == "comments":
            params["cursor"] = 0
            # 对方文档明写「count 请保持默认，否则会出现 BUG」。
            params["count"] = 20

    return Request(
        "GET",
        f"{TIKHUB_BASE}{path}?{_query(params)}",
        {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
        },
        "",
    )


def _redact(text: str, api_key: str) -> str:
    """把回显的 API Key 从错误文案里抹掉。

    TikHub 的 401 响应原文长这样：
        "无效的API令牌，您提交的API令牌为 <你的key>。..."
    这段文案会被 runner 写进飞书的诊断列。不抹掉就等于把 Key 贴在表里。
    """
    if api_key and len(api_key) >= 6:
        text = text.replace(api_key, "***")
        # 有些实现会截断后再回显，把前后缀也一起挡掉。
        text = text.replace(api_key[:8], "***").replace(api_key[-8:], "***")
    return text


_TIKHUB_HINTS: list[tuple[tuple[str, ...], Failure, bool]] = [
    (("余额", "充值", "insufficient", "balance"), Failure.QUOTA, True),
    (("令牌", "token", "unauthorized", "api key"), Failure.AUTH, True),
    (("频繁", "rate limit", "too many"), Failure.RATE_LIMIT, True),
]


def _tikhub_error(payload: dict[str, Any], http_status: int, api_key: str) -> Err:
    # Cloudflare 的拦截长得完全不像 TikHub 的业务错误：没有 detail 信封，
    # 带的是 error_code / error_name / ray_id，而且 HTTP 是 403。
    # 403 默认会被归成 AUTH，那是错的——这不是 Key 的问题，是这条出口被拦了。
    # 归错的代价很大：AUTH 会把这个通道整轮标死，而它换个网络就好了。
    if payload.get("error_code") or payload.get("ray_id"):
        return Err(
            Failure.TRANSPORT,
            str(payload.get("error_code") or f"http_{http_status}"),
            f"被 Cloudflare 拦截（{payload.get('error_name') or payload.get('title') or '未知原因'}）"
            "：多半是 User-Agent 或出口 IP 的问题，不是 API Key 的问题",
            http_status=http_status,
            definitive=True,
        )

    # 成功和失败是两套信封：成功时 code/message 在顶层，失败时整个塞进 detail。
    envelope = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
    code = envelope.get("code", http_status)
    # 先脱敏再截断（反过来的话 Key 横跨截断边界时会有半截漏出去）。
    message = _redact(
        str(envelope.get("message_zh") or envelope.get("message")
            or json.dumps(payload, ensure_ascii=False)),
        api_key,
    )[:300]
    request_id = str(envelope.get("request_id") or "")

    # TikHub 没有公开的业务错误码表，只能按 HTTP 状态 + 文案归类。
    # 这一点比 SocialDataX 差：那边每个码的语义和「要不要重试」都是写在规范里的。
    haystack = message.lower()
    for needles, kind, definitive in _TIKHUB_HINTS:
        if any(n.lower() in haystack for n in needles):
            return Err(kind, str(code), message, http_status=http_status,
                       request_id=request_id, definitive=definitive)

    if http_status in (401, 403):
        kind, definitive = Failure.AUTH, True
    elif http_status == 402:
        kind, definitive = Failure.QUOTA, True
    elif http_status == 404:
        kind, definitive = Failure.GONE, False
    elif http_status == 429:
        kind, definitive = Failure.RATE_LIMIT, True
    elif http_status >= 500 or http_status == 0:
        kind, definitive = Failure.TRANSPORT, True
    else:
        kind, definitive = Failure.UNKNOWN, False

    return Err(kind, str(code), message, http_status=http_status,
               request_id=request_id, definitive=definitive)


def _tag_types(comment: dict[str, Any]) -> set[str]:
    """把 show_tags / show_tags_v2 里的 type 值收成一个集合。

    置顶 = "user_top"，作者 = "is_author"。实测 v1 的 show_tags 一直是空数组，
    标记全在 v2 里，但两个都收，免得哪天上游改回去。
    """
    out: set[str] = set()
    for key in ("show_tags", "show_tags_v2"):
        for tag in comment.get(key) or []:
            if isinstance(tag, dict) and tag.get("type"):
                out.add(str(tag["type"]))
            elif isinstance(tag, str):
                out.add(tag)
    return out


# TikHub 透传的是小红书 App 原始响应，IP 属地实测是英文（"Shanghai"、"Guizhou"）。
# 「评论区快照」是运营每天要扫的一列，中英混排读起来很别扭，所以在这里翻回中文。
# 查不到的原样保留——宁可显示 "Shanghai" 也别显示空白。
_CN_LOCATION = {
    "beijing": "北京", "tianjin": "天津", "shanghai": "上海", "chongqing": "重庆",
    "hebei": "河北", "shanxi": "山西", "liaoning": "辽宁", "jilin": "吉林",
    "heilongjiang": "黑龙江", "jiangsu": "江苏", "zhejiang": "浙江", "anhui": "安徽",
    "fujian": "福建", "jiangxi": "江西", "shandong": "山东", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "guangdong": "广东", "hainan": "海南",
    "sichuan": "四川", "guizhou": "贵州", "yunnan": "云南", "shaanxi": "陕西",
    "gansu": "甘肃", "qinghai": "青海", "taiwan": "台湾",
    "inner mongolia": "内蒙古", "guangxi": "广西", "tibet": "西藏", "xizang": "西藏",
    "ningxia": "宁夏", "xinjiang": "新疆", "hong kong": "香港", "macao": "澳门",
    "macau": "澳门", "china": "中国",
}


def _cn_location(value: Any) -> str:
    text = str(value or "").strip()
    return _CN_LOCATION.get(text.lower(), text)


def _xhs_comment_items(comments: list[Any]) -> list[dict[str, Any]]:
    items = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        types = _tag_types(c)
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        items.append({
            "content": c.get("content") or "",
            "like_count": c.get("like_count") or 0,
            "is_pinned": "user_top" in types,
            "is_author_comment": "is_author" in types,
            # 实测返回的是英文地名（"Shanghai" / "Guizhou"），翻回中文，
            # 好让两家通道在「评论区快照」这一列里长得一样。
            "ip_location": _cn_location(c.get("ip_location")),
            "author": {"nickname": user.get("nickname") or ""},
        })
    return items


def _tikhub_normalize(platform: str, purpose: str, payload: dict[str, Any],
                      http_status: int, request_id: str, api_key: str) -> Result:
    inner = payload.get("data")
    if not isinstance(inner, dict):
        # 先脱敏再截断：反过来的话，Key 恰好横跨截断边界时会有半截漏出去。
        return Err(Failure.UNKNOWN, "no_data",
                   _redact(json.dumps(payload, ensure_ascii=False), api_key)[:300],
                   http_status=http_status, request_id=request_id)

    if platform == "xhs" and purpose == "comments":
        core = inner.get("data") if isinstance(inner.get("data"), dict) else {}
        comments = core.get("comments") if isinstance(core.get("comments"), list) else []
        # 死亡嫌疑：活笔记一定带着作者 user_id 和三个排序策略；
        # 不存在的笔记这两样都是空的。启发式，所以不 definitive——交给两击定罪。
        alive = bool(core.get("user_id")) or bool(core.get("all_sort_strategies"))
        if not alive and not comments:
            return Err(Failure.GONE, "empty_shell",
                       "评论接口返回了空壳（无作者、无排序策略、无评论），疑似笔记已不存在",
                       http_status=http_status, request_id=request_id, definitive=False)
        return Ok({
            "items": _xhs_comment_items(comments),
            "comment_count": core.get("comment_count"),
            # comment_count 是含楼中楼的总数，和 detail 的 comments_count 一致（实测）；
            # comment_count_l1 只算一级评论。阈值用总数，跟 App 里显示的口径对齐。
            "top_level_comment_count": core.get("comment_count_l1"),
        }, request_id=request_id)

    if platform == "xhs" and purpose == "detail":
        core = inner.get("data")
        note_list = core[0].get("note_list") if isinstance(core, list) and core and isinstance(core[0], dict) else None
        if note_list and not isinstance(note_list, list):
            # 有内容但形状不认识（比如上游把 list 改成了 dict）：这是协议漂移，
            # 不是「笔记没了」。硬下 [0] 会 KeyError 炸穿；按 GONE 定罪更是误杀。
            return Err(Failure.UNKNOWN, "unexpected_shape",
                       _redact(json.dumps(inner, ensure_ascii=False), api_key)[:300],
                       http_status=http_status, request_id=request_id)
        if not note_list:
            # 干净的死亡信号：不存在的笔记，data 直接是 []（实测）。
            return Err(Failure.GONE, "no_note", "详情接口没有返回任何笔记，笔记已不存在或不可见",
                       http_status=http_status, request_id=request_id, definitive=True)
        note = note_list[0] if isinstance(note_list[0], dict) else {}
        return Ok({
            "like_count": note.get("liked_count"),
            "collect_count": note.get("collected_count"),
            "share_count": note.get("shared_count"),
            "comment_count": note.get("comments_count"),
            # 上游自己的审核标记。语义还没实地验过（只见过 false），
            # 所以只写进诊断信息给人看，不参与打标签。见 docs/待验证清单.md。
            "_censored": note.get("in_censor"),
        }, request_id=request_id)

    if platform == "douyin" and purpose == "comments":
        comments = inner.get("comments") if isinstance(inner.get("comments"), list) else []
        items = []
        for c in comments:
            if not isinstance(c, dict):
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            items.append({
                "content": c.get("text") or "",
                "like_count": c.get("digg_count") or 0,
                # 抖音一律不判置顶——接口里那个 label_type 的语义没验过，
                # 而错判置顶比不判置顶伤害大得多。见 config.CommentStatus。
                "is_pinned": False,
                "is_author_comment": False,
                "ip_location": _cn_location(c.get("ip_label")),
                "author": {"nickname": user.get("nickname") or ""},
            })
        # 实测 total 是实打实的整数，不像 SocialDataX 那边是 integer|null。
        return Ok({"items": items, "comment_count": inner.get("total")}, request_id=request_id)

    if platform == "douyin" and purpose == "detail":
        aweme = inner.get("aweme_detail")
        if not isinstance(aweme, dict):
            # 干净的死亡信号：视频没了的时候 aweme_detail 是 null，
            # 同时 filter_list 里会带着这条 aweme_id 和一个 reason（实测）。
            filtered = inner.get("filter_list")
            if isinstance(filtered, list) and filtered:
                reason = filtered[0].get("reason") if isinstance(filtered[0], dict) else ""
                return Err(Failure.GONE, "filtered",
                           f"视频已被下架或不可见（上游 filter reason={reason}）",
                           http_status=http_status, request_id=request_id, definitive=True)
            return Err(Failure.GONE, "no_aweme", "详情接口没有返回视频数据",
                       http_status=http_status, request_id=request_id, definitive=False)
        stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
        status = aweme.get("status") if isinstance(aweme.get("status"), dict) else {}
        censored = None
        if status:
            censored = bool(status.get("is_prohibited") or status.get("in_reviewing"))
        return Ok({
            "like_count": stats.get("digg_count"),
            "collect_count": stats.get("collect_count"),
            "share_count": stats.get("share_count"),
            "comment_count": stats.get("comment_count"),
            "_censored": censored,
        }, request_id=request_id)

    return Err(Failure.UNKNOWN, "unsupported", f"TikHub 不支持 {platform}/{purpose}",
               http_status=http_status, request_id=request_id)


def _tikhub_parse(platform: str, purpose: str, http_status: int, content_type: str,
                  body: str, request_id: str = "", api_key: str = "") -> Result:
    try:
        payload = json.loads(body or "")
    except json.JSONDecodeError:
        # 非 JSON 的 403 基本只有一种来源：Cloudflare 的 HTML 拦截页
        # （TikHub 的业务错误全是 JSON）。那是这条出口被拦，不是 Key 的问题，
        # 归 TRANSPORT 才能降级到备胎；归 UNKNOWN 会把行判死还不换通道。
        kind = (Failure.TRANSPORT
                if http_status >= 500 or http_status == 0 or http_status == 403
                else Failure.UNKNOWN)
        return Err(
            kind,
            f"http_{http_status}",
            _redact(body or f"HTTP {http_status}", api_key)[:500],
            http_status=http_status, request_id=request_id,
        )

    if not isinstance(payload, dict):
        return Err(Failure.UNKNOWN, "unexpected_body", (body or "")[:500],
                   http_status=http_status, request_id=request_id)

    # 失败信封：整个错误对象塞在 detail 里。也兜住 Cloudflare 拦截时那种
    # 完全不同形状的响应（有 error_code / title，没有 detail）。
    if isinstance(payload.get("detail"), dict) or http_status >= 400:
        return _tikhub_error(payload, http_status, api_key)

    rid = str(payload.get("request_id") or request_id or "")
    return _tikhub_normalize(platform, purpose, payload, http_status, rid, api_key)


def _tikhub_yuan(platform: str, purpose: str) -> float:
    return TIKHUB_USD.get(platform, 0.010) * _USD_TO_CNY


def _sdx_yuan(platform: str, purpose: str) -> float:
    return SOCIALDATAX_YUAN


REGISTRY: dict[str, Provider] = {
    SOCIALDATAX: Provider(SOCIALDATAX, "SocialDataX", _sdx_build, _sdx_parse, _sdx_yuan,
                          bills_failed_lookups=False),
    TIKHUB: Provider(TIKHUB, "TikHub", _tikhub_build, _tikhub_parse, _tikhub_yuan,
                     bills_failed_lookups=True, can_handle=_tikhub_can_handle),
}


def get_provider(name: str) -> Provider:
    """按名字取供应商。

    名字刻意叫 get_provider 而不是 get——这份代码会被打成一个扁平的单文件粘进
    扣子，一个叫 get 的模块级函数在那种命名空间里迟早撞车。
    """
    provider = REGISTRY.get((name or "").strip().lower())
    if provider is None:
        raise ValueError(f"没有叫 {name!r} 的供应商，可选：{'、'.join(sorted(REGISTRY))}")
    return provider


# 值得换一家再试的失败。**GONE 不在里面**：那是行级结论，不是通道故障，
# 换一家只会再花一次钱得到同一个答案。UNKNOWN 也不在——没看懂的错误
# 换个供应商多半还是看不懂，白烧钱。
FAILOVER_KINDS = frozenset({Failure.TRANSPORT, Failure.AUTH, Failure.QUOTA})


def credentials(api_key: Any) -> dict[str, str]:
    """把调用方传进来的 key 归一成 {供应商名: key}。

    裸字符串**只登记给 SocialDataX**——那是它在只有一家供应商时的历史含义，
    保持原样，老调用方的行为一个字都不会变。想开双通道就传字典：

        {"tikhub": "...", "socialdatax": "..."}

    只配一家 key 时，`usable_order()` 会自动把另一家从顺序里滤掉，
    所以 channels 里默认写着两家也不会去打一个没配 key 的通道。
    """
    if isinstance(api_key, dict):
        return {str(k).strip().lower(): str(v) for k, v in api_key.items() if v}
    key = str(api_key or "")
    return {SOCIALDATAX: key} if key else {}


def usable_order(channels: Any, platform: str, keys: dict[str, str],
                 disabled: Optional[set[str]] = None) -> list[str]:
    """这个平台此刻真正可用的供应商顺序：有 key、且本轮没被判死。

    channels 是 config.Channels；这里刻意用 Any 接住，好让 providers 不反过来
    依赖 config——打包进扣子时模块是按依赖顺序拼接的，一旦成环就拼不出来。
    """
    dead = disabled or set()
    return [n for n in channels.for_platform(platform) if keys.get(n) and n not in dead]


# =============================================================================
#  来自 xhsearch/tags.py
# =============================================================================

@dataclass(frozen=True)
class TagMerge:
    """一次合并的结果，保留过程信息以便写回「失败原因」列时能解释清楚。"""

    final: list[str]
    added: list[str]
    removed: list[str]
    dropped_unknown: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def merge(
    current: Sequence[str] | None,
    computed: Iterable[str],
    machine_namespace: Iterable[str],
    known_options: Iterable[str] | None = None,
    exclusive: Iterable[Sequence[str]] = (),
) -> TagMerge:
    """把机器算出的标签并进现有标签，不碰人工标签。

    参数
    ----
    current:
        表里该行 流量状态 的现值。多选为空时飞书可能整个不返回这个字段，
        所以 None 和 [] 要一视同仁。
    computed:
        本次判定得出的机器标签，必须是 machine_namespace 的子集。
    machine_namespace:
        机器管辖的全部标签。不在这个集合里的一律视为人工标签，原样保留。
    known_options:
        该多选字段实际配置了哪些选项。给了就做过滤——飞书 batch_update 是
        全成功或全失败，一个字段里没有的选项名可能让整批几百行一起回滚，
        与其赌服务端会自动建选项，不如在这里挡掉并把它记进 dropped_unknown。
    exclusive:
        互斥组（如热度三档），组内同时只能留一个。只在「选项没建、保留旧
        机器标签」的路径上用，用来圈定**每个被拦标签的同类范围**：
        「大爆」写不进去 → 只保留行上同组的旧档位（爆贴），既不让两个
        档位并存，也绝不顺手把不相干的旧状态标签（比如上一轮的「已失效」）
        复活——那会把一条刚恢复正常的行继续标成死的。
    """
    current_set = [t.strip() for t in (current or []) if t and t.strip()]
    machine = set(machine_namespace)
    computed_set = {t for t in computed if t}

    unexpected = computed_set - machine
    if unexpected:
        raise ValueError(
            f"算出的标签不在机器命名空间内：{sorted(unexpected)}。"
            "要么把它加进 machine_namespace，要么它就是个笔误——"
            "放行会导致这个标签之后永远无法撤回。"
        )

    previous_machine = {t for t in current_set if t in machine}

    dropped: list[str] = []
    if known_options is not None:
        options = set(known_options)
        allowed = {t for t in computed_set if t in options}
        dropped = sorted(computed_set - allowed)
        computed_set = allowed
        if dropped:
            # 想写的标签写不进去（选项没建）时，只保留**同类**的旧标签：
            # 「大爆」被拦 → 留住行上的旧档位「爆贴」（热度信息不清零）；
            # 但绝不把不相干的旧标签一并复活——被拦的是「风控中」时，
            # 行上残留的「已失效」不在它的同类范围里，照常摘掉，
            # 否则一条刚恢复正常的行会继续顶着死亡标签。
            groups = [set(g) for g in exclusive]
            preserved: set[str] = set()
            for tag in dropped:
                category = next((g for g in groups if tag in g), {tag})
                preserved |= previous_machine & category
            computed_set |= preserved

    # 保序：先按原顺序留下人工标签，再追加机器标签，表里看起来才稳定。
    human = [t for t in current_set if t not in machine]
    seen: set[str] = set()
    final: list[str] = []
    for tag in human + sorted(computed_set):
        if tag not in seen:
            seen.add(tag)
            final.append(tag)
    return TagMerge(
        final=final,
        added=sorted(computed_set - previous_machine),
        removed=sorted(previous_machine - computed_set),
        dropped_unknown=dropped,
    )


# =============================================================================
#  来自 xhsearch/analyze.py
# =============================================================================

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
    # 和关键词命中同一道证据门槛：本轮连评论页都没看到（评论和评论数
    # 都空）就没资格说「掉了」——现有通道上小红书的空壳在上游层就被
    # 译成 GONE 到不了这里，这道闸防的是上游契约漂移。
    if (
        verdict.pin is Pin.NONE_PINNED
        and (snapshot.comments or snapshot.comment_count is not None)
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


# =============================================================================
#  来自 xhsearch/rows.py
# =============================================================================

# 直接导名字并写成一行（理由同 providers.py 顶部）：打包进扣子时模块被拼成
# 扁平文件，`providers.xxx` 这种模块前缀在那边会 NameError。


@dataclass
class ToolCall:
    platform: str            # "xhs" | "douyin"
    purpose: str             # "comments" | "detail"
    arguments: dict[str, Any]


@dataclass
class Row:
    record_id: str
    link_cell: str
    publish_time_ms: Optional[int] = None
    # 评论关键词组：任一出现在第一页评论里即算命中（不区分大小写）。
    seed_keywords: list[str] = field(default_factory=list)
    current_tags: list[str] = field(default_factory=list)
    previous_comment_count: Optional[int] = None
    # 当前点赞/收藏的现值：写新值前要搬进「上次点赞数/上次收藏数」，
    # 和评论数的搬移完全对称。
    previous_like_count: Optional[int] = None
    previous_collect_count: Optional[int] = None
    last_updated_ms: Optional[int] = None
    consecutive_failures: int = 0
    comment_status: list[str] = field(default_factory=list)
    # 上一轮写进「置顶评论」列的内容：非空（且不是抖音的「不支持」占位）
    # 而本轮没有置顶，就是「置顶刚掉」——最该被立刻发现的告警。
    pinned_comment: str = ""
    queued: bool = False

    _parsed: Optional[ParsedLink] = field(default=None, repr=False, compare=False)

    @property
    def parsed(self) -> ParsedLink:
        if self._parsed is None:
            self._parsed = parse(self.link_cell)
        return self._parsed

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.publish_time_ms is None:
            return None
        now = now or datetime.now(timezone.utc)
        published = datetime.fromtimestamp(self.publish_time_ms / 1000, tz=timezone.utc)
        return (now - published).total_seconds() / 3600

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        hours = self.age_hours(now)
        return None if hours is None else hours / 24

    def in_cooldown(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """刚刷过就别再刷。

        这是对「有人连点 200 次按钮」的完整回答：连点 200 次 = 1 次真实调用。
        """
        window = settings.safety.cooldown_seconds
        if not window or self.last_updated_ms is None:
            return False
        now = now or datetime.now(timezone.utc)
        updated = datetime.fromtimestamp(self.last_updated_ms / 1000, tz=timezone.utc)
        return (now - updated).total_seconds() < window

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
        if self.last_updated_ms is None:
            return True
        now = now or datetime.now(timezone.utc)
        updated = datetime.fromtimestamp(self.last_updated_ms / 1000, tz=timezone.utc)
        return (now - updated).total_seconds() / 3600 >= interval


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
) -> float:
    """预估这一批要花多少钱，按每个平台**实际会走的那家**的单价算。

    「实际会走的那家」= 通道顺序里第一家**配了 key 且吃这种参数形态**的。
    按配置主通道计价的话，只配了备胎 key 的部署（完全合法）会把
    SocialDataX 的账按 TikHub 的单价报——抖音差 14 倍，一个天天报错账的
    估算没人会信。keys 不传时退回按「第一家能接的」算。
    """
    total = 0.0
    for row in rows:
        for call in plan_calls(row, settings, now):
            for name in settings.channels.for_platform(call.platform):
                if keys is not None and not keys.get(name):
                    continue
                try:
                    provider = get_provider(name)
                except ValueError:
                    continue
                if provider.can_handle(call.platform, call.purpose, call.arguments):
                    total += provider.yuan_per_call(call.platform, call.purpose)
                    break
            # 一家都接不了：这一行实际会以「不支持的链接形态/没通道」失败，不产生费用
    return total


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
                        page_size=200, deadline=None, sort_field=None):
    """按条件拉记录，**自动翻页**——语义与服务端 feishu.Bitable.search 一致。
    返回 (records, 是否扫完)。

    ⚠️ 这里绝不能只取一页：过滤条件只有「监控中」，到期判断在取回后才做。
    只取一页的话每一轮看到的都是同样的前 N 条，第 N+1 条以后
    **永远轮不到刷新**——「600 行按每轮 40 行排干」全靠这里拿到全量。

    deadline：到点就带着已取到的页返回（第二个返回值 False）。扣子只有
    60 秒，飞书响应慢时不能让翻页吃光处理和写回的预算。

    sort_field：按这一列**升序**扫（传「最后更新时间」= 最久没刷的排最前）。
    这是「部分扫描也不会饿死任何行」的关键：没有可持久化的翻页游标，
    但把最饥饿的行排在最前面，游标就藏在数据自己身上——被刷过的行
    时间戳更新后自动沉到队尾，下一轮的前缀永远是最该刷的那批。
    不排序的话，慢响应下每轮都只扫到同一批前缀行，前缀刷完后
    整个节点会一直报「没有到期的行」，而后面的行永远没人看。
    """
    items, page_token = [], ""
    base = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"
    body = {"field_names": list(field_names), "automatic_fields": False}
    if filter_spec:
        body["filter"] = filter_spec
    if sort_field:
        body["sort"] = [{"field_name": sort_field, "desc": False}]
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return items, False
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
            return items, True
        page_token = payload.get("page_token") or ""
        if not page_token:
            return items, True


async def feishu_fields(token, app_token, table_id):
    """一次取回表的全部字段元数据：({列名集合}, {列名: 选项列表})。
    读不到返回 (None, {})——None = 不过滤，宁可试着写。

    两个用途，都是防「表级错误让整批回滚」：
    * 列名集合：按名字写一个表里不存在的列（1254045）会让整批写回失败，
      还没建的机器列要在写回前挡下来（挡下的列在结果消息里提示）；
    * 选项列表：写一个没建过的多选选项（1254291）同样整批回滚。
    服务端跑得了 doctor 能提前发现这两类问题，扣子跑不了，只能在这里挡。
    """
    page_token = ""
    names, options_map = set(), {}
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
                return None, {}
            payload = data.get("data") or {}
            for item in payload.get("items") or []:
                name = item.get("field_name")
                if not name:
                    continue
                names.add(str(name))
                prop = item.get("property") or {}
                if "options" in prop:
                    # 选择类字段一定带 options 键；空列表也要记下来——
                    # 空表示「一个选项都没建，全部拦下」，丢成 None 会被
                    # 解读成「读不到元数据、别过滤」，机器选项直接写进去
                    # 就是整批回滚。
                    options_map[str(name)] = [
                        o["name"] for o in (prop.get("options") or [])
                        if isinstance(o, dict) and o.get("name")]
                else:
                    # 列存在但不是选择类字段（比如被误建成文本列）：记空表全拦。
                    # 往文本列写多选列表本来就写不进去，拦下比整批回滚强——
                    # 与服务端 cli 的口径一致。.get 返回 None 只留给「列不存在」。
                    options_map[str(name)] = []
            if not payload.get("has_more"):
                return names, options_map
            page_token = payload.get("page_token") or ""
            if not page_token:
                return names, options_map
    except Exception:                                  # noqa: BLE001
        return None, {}


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


    filter_spec = None
    if mode != "row":
        filter_spec = {"conjunction": "and", "conditions": [
            {"field_name": f.monitoring, "operator": "is", "value": ["true"]}]}

    # 字段元数据必须先取：search 按名字请求列，请求一个还没建的列
    # （比如「点赞数」）会让整个 search 报 1254045、一行都读不到。
    # 元数据失败 = 不过滤（宁可试着写），与服务端语义一致。
    try:
        table_fields, options_map = await feishu_fields(
            token, p["app_token"], p["table_id"])
    except Exception:                                  # noqa: BLE001
        table_fields, options_map = None, {}
    traffic_options = options_map.get(f.traffic_status)
    status_options = options_map.get(f.comment_status)
    wanted_fields = f.must_read()
    if table_fields is not None:
        wanted_fields = [c for c in wanted_fields if c in table_fields]

    # 读表按「最后更新时间」升序扫（最久没刷的最前），预算给到软截止的一半。
    scan_deadline = deadline - SOFT_DEADLINE / 2
    try:
        records, scan_complete = await feishu_search(
            token, p["app_token"], p["table_id"], wanted_fields,
            filter_spec, deadline=scan_deadline, sort_field=f.last_updated)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0,
                "message": str(exc)}
    scan_note = ("" if scan_complete
                 else "；⚠ 读表未扫完（飞书响应慢），本轮按最久未刷的行优先，其余下一轮继续")

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
            seed_keywords=_cell_keywords(cells.get(f.seed_keywords)),
            current_tags=_cell_tags(cells.get(f.traffic_status)),
            previous_comment_count=_cell_int(cells.get(f.comment_count)),
            previous_like_count=_cell_int(cells.get(f.like_count)),
            previous_collect_count=_cell_int(cells.get(f.collect_count)),
            last_updated_ms=_cell_ms(cells.get(f.last_updated)),
            consecutive_failures=_cell_int(cells.get(f.consecutive_failures)) or 0,
            comment_status=_cell_tags(cells.get(f.comment_status)),
            pinned_comment=_cell_text(cells.get(f.pinned_comment)),
            queued=bool(cells.get(f.queued)),
        )
        if wanted or row.queued or row.is_due(settings, now):
            rows.append(row)
    rows = rows[:BATCH_LIMIT]

    if not rows:
        return {"ok": True, "processed": 0, "credits": 0, "balance": 0,
                "message": "没有到期的行" + scan_note}

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
    dropped_columns = set()
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
        if fields and table_fields is not None:
            # 表里还没建的机器列挡下来：按名字写不存在的列是表级错误，
            # 会让整批写回失败。挡下的列名在结果消息里提示。
            missing = set(fields) - table_fields
            if missing:
                dropped_columns |= missing
                fields = {k: v for k, v in fields.items() if k in table_fields}
        if fields:
            update = {"record_id": row.record_id, "fields": fields}
            updates.append(update)
            entries.append((update, status))

    # 全局熔断：一批里失效比例异常偏高 → 上游故障，撤销所有标签写入。
    # 判据与服务端 runner._apply_circuit_breaker 一致（含疑似受限、分母为
    # 实际尝试数），熔断后同样改写刷新状态并撤销计数增量——两条运行时对
    # 同一批数据必须给出同一个结论。
    tripped = attempted >= settings.safety.breaker_min_sample and \
        gone / max(attempted, 1) > settings.safety.breaker_gone_ratio
    if tripped:
        note = (f"本批 {gone}/{attempted} 行都取不到内容，疑似上游故障而非内容失效，"
                "本轮不改流量状态，请稍后复查")
        for update, status in entries:
            update["fields"].pop(f.traffic_status, None)
            if status in ("已失效", "疑似受限"):
                update["fields"][f.refresh_status] = "刷新失败"
                update["fields"].pop(f.consecutive_failures, None)
                update["fields"].pop(f.alive_confirmed, None)
                original = str(update["fields"].get(f.failure_reason) or "")
                update["fields"][f.failure_reason] = \
                    (f"{original}；{note}" if original else note)[:500]

    cents = round(fen)
    try:
        written = await feishu_batch_update(token, p["app_token"], p["table_id"], updates)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": cents, "balance": balance,
                "message": f"算完了但写回失败：{exc}"}

    via = "、".join(f"{k} {v} 次" for k, v in sorted(used.items())) or "无调用"
    message = f"刷新 {written} 行，花费 ≈ ¥{fen / 100:.2f}（{via}）" + scan_note
    if disabled:
        message += f"；⚠ 本轮 {'、'.join(sorted(disabled))} 通道不可用，已降级"
    if tripped:
        message += "；⚠ 本批失效比例异常，已熔断，未改流量状态"
    if dropped_columns:
        message += ("；⚠ 这些列在表里还没建，本轮已跳过："
                    + "、".join(sorted(dropped_columns)))
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
                               current_tags=row.current_tags) if convicted \
            else suspect_verdict(settings, strikes, error.operator_text())
        # 第一击不碰流量状态：这一轮没获得关于这篇笔记的任何新信息，
        # 摘掉上一轮的标签等于用一次失败抹掉真实结论。
        fields = _render(row, verdict, None, settings, now,
                         "已失效" if convicted else "疑似受限",
                         traffic_options, status_options, touch_tags=convicted)
        fields[f.consecutive_failures] = strikes
        if convicted:
            fields[f.alive_confirmed] = False
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
                     seed_keywords=row.seed_keywords,
                     current_tags=row.current_tags,
                     previous_pinned=row.pinned_comment)
    if snapshot.censored:
        verdict.notes.append("⚠ 上游把这条标成了审核中/受限，请人工确认")
    fields = _render(row, verdict, snapshot, settings, now, "正常",
                     traffic_options, status_options)
    fields[f.consecutive_failures] = 0
    # 有正面证据（评论或评论数）才勾「已确认存活」；空壳轮次复选框不动。
    if snapshot.comments or snapshot.comment_count:
        fields[f.alive_confirmed] = True
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
        # 「评论状态」由关键词命中结果驱动（命中=显示评论，未命中=没有显示）。
        wanted = comment_status_values(verdict, settings)
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
        # 点赞/收藏与评论数完全对称：写新值前先把现值搬进「上次」列。
        if snapshot.like_count is not None:
            if row.previous_like_count is not None:
                fields[f.previous_like_count] = row.previous_like_count
            fields[f.like_count] = snapshot.like_count
        if snapshot.collect_count is not None:
            if row.previous_collect_count is not None:
                fields[f.previous_collect_count] = row.previous_collect_count
            fields[f.collect_count] = snapshot.collect_count
        # 置顶评论和快照只在看到了评论页时更新：空壳轮写空串/「暂无评论」
        # 会抹掉上一轮的真实内容，「置顶评论」还是掉落告警的对比基线。
        if snapshot.comments or snapshot.comment_count is not None:
            fields[f.pinned_comment] = format_pinned(snapshot)
            fields[f.comment_digest] = format_digest(snapshot, settings.digest)
        seed_text = format_seed_match(verdict, snapshot)
        if seed_text is not None:
            fields[f.seed_match] = seed_text
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


def _cell_keywords(value):
    """评论关键词组：多选列取选项名；文本列按 顿号/逗号/分号/换行 拆分
    （不按空格，「cGMP 因子」这类带空格的词组不能拆碎）。"""
    raw = [w for w in _cell_tags(value) if w] if isinstance(value, list) else []
    if not raw:
        # 富文本分段（单行文本列的返回形态）走文本拼接再拆，
        # 否则文本列里的关键词会静默消失。
        raw = re.split(r"[，,、;；\n]+", _cell_text(value))
    seen, out = set(), []
    for word in raw:
        word = (word or "").strip()
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _cell_int(value):
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _cell_ms(value):
    number = _cell_int(value)
    if number is None:
        return None
    return number * 1000 if number < 10_000_000_000 else number

