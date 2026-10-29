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

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings
from .links import ParsedLink, id_timestamp, parse
# 直接导名字并写成一行（理由同 providers.py 顶部的历史说明）。
from .providers import get_provider, is_disabled  # noqa: F401

# 「巡查状态」那一列的取值。放在这里而不是 runner 里，是因为**选行**也要认它
# （熔断记过失败的行要提前复查），而 rows 不能反过来依赖 runner。
# runner 原样再导出一遍，调用方照旧写 runner.STATUS_OK。
STATUS_OK = "正常"
STATUS_SUSPECT = "疑似受限"
STATUS_GONE = "已失效"
STATUS_FAILED = "刷新失败"
STATUS_SKIPPED = "跳过"
STATUS_COOLDOWN = "冷却跳过"
# 到软截止（或整批中止）没轮到的行：**完全不写回**，最后更新时间保持原样，
# 下一轮自然重新捞起——这才是 refresh() docstring 承诺的断点续跑。
STATUS_DEFERRED = "留待下一轮"
# 被「同篇搭车」拉进来的行（见 runner.load_rows）：它自己还没到期，只是同一篇
# 内容的另一行这一轮要刷、顺手白拿一份结果。那一行没刷成（预算、截止、
# 通道倒了），这一行就**什么都不写**、一分钱不花——它本来就不该这一轮刷。
# 内部状态，从不落表。
STATUS_RIDE_SKIPPED = "同篇未刷到"


@dataclass
class ToolCall:
    platform: str            # "xhs" | "douyin"
    purpose: str             # "comments" | "detail"
    arguments: dict[str, Any]


# 「作品ID」那一格的写法：`7584415807459888384 ← https://v.douyin.com/abc/`
# （小红书：`6ad8209b000000003300aac2 ← xhslink.cn/o/8BQXAJqCggS`）。
# 箭头右边是**当时展开的那条短链**：运营哪天把「反馈链接」换成另一条，
# 两边对不上，这一格就自动作废、下一轮重新展开——存下来的 ID 永远只替
# 它自己那条链接说话。
_RESOLVED_RE = re.compile(r"^\s*([0-9a-f]{24}|\d{6,})\s*←\s*(\S+)\s*$", re.I)
# 存下来的 ID 也要像这个平台的 ID：抖音是一串数字，小红书是 24 位十六进制。
_RESOLVED_SHAPE = {"douyin": re.compile(r"^\d{6,}$"),
                   "xhs": re.compile(r"^[0-9a-f]{24}$", re.I)}


def format_resolved_id(aweme_id: str, url: str) -> str:
    """短链展开结果 → 「作品ID」那一格要写的字。"""
    return f"{aweme_id} ← {url}"


def read_resolved_id(cell: str, url: Optional[str],
                     platform: str = "douyin") -> Optional[str]:
    """「作品ID」那一格存的 ID，**仅当它是从这条链接展开出来的**、而且长得像
    这个平台的 ID。"""
    if not cell or not url:
        return None
    found = _RESOLVED_RE.match(cell)
    if not found:
        return None
    if found.group(2).rstrip("/") != url.strip().rstrip("/"):
        return None
    shape = _RESOLVED_SHAPE.get(platform)
    if shape is None or not shape.match(found.group(1)):
        return None
    return found.group(1)


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
    # 「评论状态」现值：手填的（不是机器那两个值）不覆盖。
    comment_status: str = ""
    # 「起量时间」那一格的现值。None = 还没起过量，这一轮判起量就写；
    # 有值 = 已经记过第一次，之后再猛的一轮也不覆盖它。
    surge_time_ms: Optional[int] = None
    queued: bool = False
    # 「是否巡查」那一格的现值。日常巡查（sweep）只看勾着的行；**queue 不看它**
    # ——勾「排队刷新」是人明确说「我现在就要这一行的数据」，和「这一行日常
    # 要不要维护」是两件事（见 load_rows）。读回来只为了在日志里说清
    # 「这一批里有 N 行没开巡查」，让这种主动取数在运行历史里看得见。
    monitoring: bool = True
    # 「蓝词字段」的现值（人工填的 + 机器此前回填的）。回填只追加表里还没有
    # 的词，所以得先知道有哪些；空着 = 还没抓到过，每轮都要扫。
    blue_words: list[str] = field(default_factory=list)
    # 「蓝词来源」的现值。行首记着每次扫描时的档位，是「爆贴/大爆那一次
    # 复扫做过没有」的账（见 analyze.plan_blue_word_scan）。
    blue_word_sources: str = ""
    # 「评估基线评论数 / 评估基线时间」的现值：评估中的增长判据拿这一轮和它比
    # （见 analyze._eval_growth）。两格都有才算有基线。
    eval_baseline_count: Optional[int] = None
    eval_baseline_ms: Optional[int] = None
    # 「限流前评论数」那一格（0 / 空 = 没有在跟的掉量），见 Fields.throttle_baseline。
    throttle_baseline: Optional[int] = None
    # 「巡查状态」的现值。选行只用它认一种情况：熔断记过一次失败的行
    # （刷新失败 + 连续失败次数 ≥ 1），要在 breaker_recheck_hours 之后提前复查，
    # 而不是等分层间隔、也不是排队勾一在就每 5 分钟重刷。
    refresh_status: str = ""
    # 「作品ID」那一格的现值：抖音短链上一次展开出来的 aweme_id（带着它
    # 对应的那条短链，见 read_resolved_id）。对得上就直接用 ID，不再展开。
    resolved_id_cell: str = ""
    # —— 数据整理基线（见 analyze.recollect_check）——
    # 「数据整理」原文（AI 读截图吐的那行字），从里面拆出这次整理的互动量。
    collation_text: str = ""
    # 「整理时评论数」「整理时互动数」「整理状态」三格的现值。
    collation_comments: Optional[int] = None
    collation_interactions: Optional[int] = None
    collation_status: str = ""
    # 由 load_rows 置位：这一行自己没到期，是因为同一篇内容的另一行到期了才被
    # 带进来的。它只吃这一轮已经取到的结果，绝不单独花钱（见 STATUS_RIDE_SKIPPED）。
    ride_along: bool = False

    _parsed: Optional[ParsedLink] = field(default=None, repr=False, compare=False)

    @property
    def parsed(self) -> ParsedLink:
        """这一格链接的解析结果，**加上机器已经知道的 ID**。

        抖音短链本身不带 aweme_id。以前每一轮都要现场跟一次 302 才知道是哪个
        作品，展不开（短链过期、落地页改版、网络抖动）的那一轮就只能走贵 14 倍
        的 SocialDataX，只配 TikHub 时干脆跳过。第一次展开成功后机器把 ID 记进
        「作品ID」，之后这一行就是 ID 形态，和贴了完整链接的行一样走。
        """
        if self._parsed is None:
            link = parse(self.link_cell)
            if link.platform in ("douyin", "xhs") and not link.content_id:
                known = read_resolved_id(self.resolved_id_cell, link.url, link.platform)
                if known:
                    link = ParsedLink(link.platform, known, None, link.raw)
            self._parsed = link
        return self._parsed

    def content_key(self) -> Optional[tuple[str, str]]:
        """这一行是哪一篇内容：（平台, ID）。拿不到 ID 的（小红书短链、没展开过的
        抖音短链）返回 None——光看链接文字认不出两条短链是不是同一篇。"""
        link = self.parsed
        if link.platform and link.content_id:
            return (link.platform, link.content_id)
        return None

    def id_publish_ms(self, now: Optional[datetime] = None) -> Optional[int]:
        """链接 ID 里自带的发布时刻（毫秒）。解不出、或解出来在未来，返回 None。"""
        link = self.parsed
        seconds = id_timestamp(link.platform, link.content_id)
        if seconds is None:
            return None
        now = now or datetime.now(timezone.utc)
        # 给一天余量：ID 是平台服务器的时钟，跟我们差几分钟很正常；
        # 差出一天以上就不是这种编码的 ID 了。
        if seconds > now.timestamp() + 86400:
            return None
        return seconds * 1000

    def schedule_publish_ms(self, settings: Settings,
                            now: Optional[datetime] = None) -> Optional[int]:
        """**分层和归档**用的发布时刻：表里填了就用表里的；空着就用链接 ID
        推一个**最晚可能**的发布时刻。

        「发布时间」空着的行以前按最快一档（8 小时）一直刷、**永远不归档**
        （现在连 ID 也解不出的干脆不巡查，见 refresh_interval_hours），
        一条小红书一个月就是 ¥6.5，而且没有尽头。两个平台的 ID 里编着创建
        时刻（见 links.id_timestamp），但 ID 创建不等于发布：定时发布的笔记
        ID 在排期那一刻就有了，真正发出去可能晚好几天。直接拿 ID 时间当发布
        时间，一条定时发布的帖子会被当成「已经很老了」——分层刷得更慢，甚至
        还活着就被归档、从此没人看。

        所以这里取**最晚可能的发布时刻** = ID 时间 + 定时发布最长的提前量
        （RefreshTiers.inferred_publish_margin_days）。拿它算出来的帖龄只会
        比真实的小、不会比真实的大：任何一条帖子都不会因为这一刀刷得比
        「填了真实发布时间」更慢，也不会更早归档。代价是没定时的帖子在头
        两周多按快档刷几次——比起以前的「永远 8 小时」，还是有尽头的。

        **热度档位不用它**（见 age_hours）：那是给人看的结论，宁可照旧报
        「发布时间是空的，判不了」，也不拿一个推出来的时刻去打「无水花」。
        填了但填成未来的时间同样不换成 ID 时间：那是填错了，诊断信息要
        照实报出来让人去改（见 analyze.decide）。
        """
        if self.publish_time_ms is not None:
            return self.publish_time_ms
        created = self.id_publish_ms(now)
        if created is None:
            return None
        now = now or datetime.now(timezone.utc)
        latest = created + int(settings.refresh.inferred_publish_margin_days * 86_400_000)
        # 链接都在表里了，帖子不可能还没发：最晚也就是此刻。
        return min(latest, int(now.timestamp() * 1000))

    def schedule_age_days(self, settings: Settings,
                          now: Optional[datetime] = None) -> Optional[float]:
        """分层和归档用的帖龄（天）。见 schedule_publish_ms。"""
        published = _utc(self.schedule_publish_ms(settings, now))
        if published is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - published).total_seconds() / 86400

    def publish_time_inferred(self, now: Optional[datetime] = None) -> bool:
        """这一行的刷新节奏是不是从链接 ID 推出来的（表里「发布时间」空着）。"""
        return self.publish_time_ms is None and self.id_publish_ms(now) is not None

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        """**表里填的**发布时间到现在多少小时。热度档位（观察中 / 无水花）只认它。"""
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

    def hours_since_check(self, now: Optional[datetime] = None) -> float:
        """距上次检查过了多少小时。从没刷过 = +∞（等最久的那种）。"""
        updated = _utc(self.last_updated_ms)
        if updated is None:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return (now - updated).total_seconds() / 3600

    @property
    def breaker_strike_pending(self) -> bool:
        """上一轮的失效判定被熔断挡下、只记了一次失败：等着第二次看。

        判据是「巡查状态=刷新失败 且 连续失败次数 ≥ 1」。刷新失败本身不涨计数
        （网络抖动对「内容还在不在」没有证据力），所以这个组合几乎只有熔断轮
        会写出来。唯一的旁路是「上一轮疑似受限、这一轮网络失败」的行——它也会
        被提前复查一次，一行的钱，可以接受。
        """
        return self.refresh_status == STATUS_FAILED and (self.consecutive_failures or 0) >= 1

    def in_breaker_hold(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """熔断记过失败之后的等待期内：**排队勾也不接**。

        这是死循环的那一刀。熔断轮以前不推进「最近检查时间」也不清排队勾，
        本意是「这一行没有结论，下一轮接着来」；可 cron 每 5 分钟一轮，
        同一批行每轮重刷、每轮再熔断，一天烧两百多块，没有任何东西让它停。
        现在熔断轮照常盖时间戳、记一次失败，这里按时间戳把它挡 breaker_recheck_hours，
        到点复查一次，还取不到就按两击定罪判失效往下走（Ziao 2026-09 拍板：
        「最多两次，两次还不行就算失效」）。
        """
        if not self.breaker_strike_pending:
            return False
        since = self.hours_since_check(now)
        return 0 <= since < settings.safety.breaker_recheck_hours

    def refresh_interval_hours(self, settings: Settings,
                               now: Optional[datetime] = None) -> Optional[float]:
        """这一行现在按几小时的间隔刷；None = 不自动刷：已归档，或者帖龄算不出来。

        **档位按「上一次检查那一刻」的帖龄算，不是按此刻的帖龄算。**
        这是为了让「下次检查时间」那一列说的是真话：档位按此刻算的话，
        中午显示「下次 14:00」的行，下午一跨过 2 天线就自己变成
        「次日 06:00」——14:00 什么也没发生，而运营完全看不出为什么。
        改成按上次检查时刻定档之后，`最近检查时间 + 间隔` 是一个**定值**，
        表里那个公式算出来的就是机器真正会用的时间。
        代价是跨档前后多刷一轮（按旧档多刷一次就转档了），可以忽略。

        归档仍然看**当下**的帖龄：归档就是归档，不该因为上次检查发生在
        归档线之前就再刷一轮。

        **帖龄算不出来的行不自动巡查**（「发布时间」空着、链接里也解不出 ID
        时间，比如小红书短链；Ziao 2026-10-16 拍板）。以前按最快一档兜底，
        而这种行永远跨不过归档线——一直按 8 小时刷、一直花钱，没有尽头。
        补上发布时间，下一轮就开始查；「排队刷新」和手动刷一行不看到期，照常管用。
        面板项目卡「没填发布时间」数的就是这些行。
        """
        published = _utc(self.schedule_publish_ms(settings, now))
        if published is None:
            return None  # 帖龄算不出来：不自动巡查
        age_now = self.schedule_age_days(settings, now)
        if age_now is not None and settings.refresh.interval_hours_for_age(age_now) is None:
            return None  # 已归档
        updated = _utc(self.last_updated_ms)
        if updated is None:
            # 从没刷过：按最快的那一档（is_due 对从没刷过的行本来就判到期）。
            interval = settings.refresh.fastest_interval_hours()
        else:
            age_at_check = (updated - published).total_seconds() / 86400
            interval = settings.refresh.interval_hours_for_age(age_at_check)
            # None 只可能出现在「最近检查时间」是未来时间的脏数据上（此刻还没
            # 归档、上次检查却已经过了归档线）。兜底成最快档，让它能被刷一次
            # 把那个时间戳纠正回来。
            if interval is None:
                interval = settings.refresh.fastest_interval_hours()
        if interval is not None and self.in_eval_window(settings):
            # 评估中的增长判据要每个窗口看一次：发布 8 天以后的档位是 72 小时，
            # 凑不出 24 小时的窗口。判出无水花（或升了爆贴）就回到分层节奏。
            interval = min(interval, settings.thresholds.eval_window_hours)
        backoff = self.gone_backoff_hours(settings)
        if interval is not None and backoff is not None:
            interval = max(interval, backoff)
        return interval

    def in_eval_window(self, settings: Settings) -> bool:
        """这一行是不是正在走「评估中」的增长判据：记过基线、现在还是评估中。

        认基线而不是认评论数：基线是 analyze 真进了这条判断才写的（表里
        填了发布时间、满了起算小时、两列都建了），这里不用再把那几个条件
        抄一遍。还没记基线的老帖下一次照常按分层节奏查，那一次记上基线，
        之后就按窗口查——和「从下一次巡查开始建基线，24 小时后判」一致。
        """
        if self.eval_baseline_count is None or self.eval_baseline_ms is None:
            return False
        if self.publish_time_ms is None:
            return False      # 热度档位只认表里的发布时间，空了就判不了
        t = settings.tags
        return t.top_tier(self.current_tags) == t.evaluating and t.flop not in self.current_tags

    def gone_backoff_hours(self, settings: Settings) -> Optional[float]:
        """已失效的行多久再看一次；None = 不是已失效的行，按分层走。

        已失效以前和活帖一个节奏：发布第 1 天死掉的笔记，归档前还要按
        8 / 24 / 72 小时再查十几次，而 TikHub 查一条不存在的笔记**照样扣钱**
        （返回一个空壳，见 docs/供应商对比.md 3.1）。这十几次几乎只买得到
        同一个答案。

        但也不能一判死就再也不看：两击定罪认的是启发式的空壳，审核中的
        笔记偶尔会这样「死」一阵再回来。所以退避而不是停刷——从最慢那一档
        起步，之后每再确认一次死讯就翻一倍（「连续失败次数」每次失效都 +1）：
        默认 72 → 144 → 288 小时……发布第 1 天死掉的笔记归档前只再看三次左右。
        真活过来了，那一轮成功会把计数清零、巡查状态写回「正常」，节奏自动复原。
        人工勾「排队刷新」不受影响，随时可以手动复查。
        """
        if self.refresh_status != STATUS_GONE:
            return None
        slowest = settings.refresh.slowest_interval_hours()
        if slowest is None:
            return None
        extra = max(0, (self.consecutive_failures or 0) - settings.safety.strikes_before_gone)
        # 封顶 2^10：计数被人手填成一个离谱的大数时，别算出一个溢出的间隔。
        return slowest * 2 ** min(extra, 10)

    def is_due(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """按分层策略判断这一行现在该不该刷。"""
        interval = self.refresh_interval_hours(settings, now)
        if interval is None:
            return False  # 已归档
        updated = _utc(self.last_updated_ms)
        if updated is None:
            return True   # 在管、没归档、从来没刷过
        if self.breaker_strike_pending:
            # 熔断只记了一次失败的行：不等分层间隔（老帖是 72 小时），
            # 等待期一过就复查，给「两次」一个明确的第二次。归档线照旧。
            interval = min(interval, settings.safety.breaker_recheck_hours)
        now = now or datetime.now(timezone.utc)
        elapsed_hours = (now - updated).total_seconds() / 3600
        # 未来的更新时间同样按「该刷」处理：否则一个填错的日期能让这一行
        # 长期不到期，而且没有任何人会发现。
        # 余量（DUE_SLACK_MINUTES）只在 sweep 一天跑固定几次的部署上有用，
        # 见 RefreshTiers.due_slack_minutes。
        return elapsed_hours < 0 or settings.refresh.window_reached(elapsed_hours, interval)

    def overdue_hours(self, settings: Settings,
                      now: Optional[datetime] = None) -> float:
        """逾期了多久（小时）。**派发排序用的就是它，越大越先刷。**

        以前派发顺序就是飞书返回的表顺序，中间没有任何排序：到期行数超过
        单轮预算时，排在表上面的那批每一轮都先花掉预算，表尾那批系统性地、
        反复地输——不是随机漂移，是固定歧视。

        从没刷过 = +∞（等最久，最该先刷）；已归档 = -∞（本来就不该刷）。
        「最近检查时间」是未来时间的脏行会算出一个很负的值、排到最后：
        它确实需要被刷一次纠正时间戳，但让整列填错的脏数据插到真正饿着的
        行前面更糟——有余量的轮次里它照样会被刷到。
        """
        interval = self.refresh_interval_hours(settings, now)
        if interval is None:
            return float("-inf")
        return self.hours_since_check(now) - interval


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


def share_key(calls: list[ToolCall]) -> tuple:
    """这一组调用的「指纹」：指纹相同 = 上游会回同一份东西 = 一轮之内只付一次钱。

    用的是**真正要发出去的参数**（平台、用途、ID/链接、排序），不是表里那一格
    原文：同一篇笔记带不同 xsec_token 的两条链接抽出来是同一个 note_id，
    指纹相同；同一个 ID 一行要 detail、一行不要（发布时间不同），指纹不同，
    各走各的——那本来就是两组不同的请求。
    """
    return tuple((c.platform, c.purpose, tuple(sorted(c.arguments.items())))
                 for c in calls)


def post_key(row: "Row") -> str:
    """这一行是哪一篇帖子，给日报留痕和面板处理状态用。**两边必须同一个算法**
    （以前各写一份，已经对不上过）。有作品 ID 用 ID；没有就用链接的摘要。"""
    parsed = row.parsed
    if parsed.content_id:
        return f"{parsed.platform}:{parsed.content_id}"
    if not (parsed.url or row.link_cell):
        return f"row:{row.record_id}"
    return f"{parsed.platform}:url:" + hashlib.sha256(
        (parsed.url or row.link_cell).encode("utf-8")).hexdigest()


def id_form(arguments: dict[str, Any], content_id: str,
            platform: str = "douyin") -> dict[str, Any]:
    """把一条调用的参数从「链接形态」改写成「ID 形态」。

    短链展开成功后用它改写整行的调用计划：去掉 url、放进 aweme_id（抖音）
    或 note_id（小红书），其余参数（sort 等）原样保留。改写完和贴了完整链接
    的行一模一样（plan_calls 给那种行的也只有 ID）。两家 provider 的 build
    都先看 ID。
    """
    args = {k: v for k, v in arguments.items() if k != "url"}
    args["note_id" if platform == "xhs" else "aweme_id"] = content_id
    return args


def _argument_forms(call: ToolCall) -> list[dict[str, Any]]:
    """这条调用在开跑时可能呈现的参数形态。

    抖音短链在开跑时会先被免费展开成 aweme_id（shortlink.py），展开成功
    就走吃 ID 的通道。所以估算要把两种形态都考虑进去：链接形态是它现在
    的样子，ID 形态是展开之后的样子。小红书和已带 ID 的抖音只有一种形态。
    """
    args = call.arguments
    if call.platform == "douyin" and args.get("url") and not args.get("aweme_id"):
        return [args, id_form(args, "0")]
    return [args]


def paying_rows(rows: list[Row], settings: Settings,
                now: Optional[datetime] = None,
                seen: Optional[set] = None) -> list[Row]:
    """这一批里**真正要付钱**的那些行：搭车的不算，同指纹的只算第一行。

    估算和面板「预计花费」都该按它算——一轮里同一篇只付一次钱
    （runner.FetchShare），按行数算就会把重复的那几行报成真花销。
    抖音短链按链接文字算指纹（展开前认不出是不是同一篇），宁可多报一点。

    seen 传同一个集合给每张表，跨表的重复也只算一次（和 cron 跨表共享
    FetchShare 一个口径：按表的先后顺序，第一张表付钱）。
    """
    seen = set() if seen is None else seen
    out: list[Row] = []
    for row in rows:
        if row.ride_along:
            continue
        calls = plan_calls(row, settings, now)
        key = share_key(calls) if calls else ("row", row.record_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


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

    抖音短链算两种形态（见 _argument_forms）：乐观口径假定展开成功、走吃 ID 的
    TikHub；悲观口径把展不开、退回 SocialDataX 的那条路也算上——预算闸门
    预留时展开还没发生，不能假定它一定成功。

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
                if is_disabled(name, call.platform, dead):
                    continue
                try:
                    provider = get_provider(name)
                except ValueError:
                    continue
                if any(provider.can_handle(call.platform, call.purpose, form)
                       for form in _argument_forms(call)):
                    prices.append(provider.yuan_per_call(call.platform, call.purpose))
                    if not worst_case:
                        break
            # 一家都接不了：这一行实际会以「不支持的链接形态/没通道」失败，不产生费用
            if prices:
                total += max(prices) if worst_case else prices[0]
    return total
