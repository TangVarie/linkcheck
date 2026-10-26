"""把接口返回的评论页/详情，变成可以直接写进多维表格的值。

全部纯函数，不碰网络也不碰飞书——方便单测，离线毫秒级跑完。

两个平台的能力差异必须在这一层显式处理，不能糊过去：

* 小红书评论条目有 is_pinned / is_author_comment，且支持 sort_type=default
* 抖音评论条目**没有 is_pinned**（只有 is_hot / is_folded），也**没有 sort_type**

所以「置顶状态」在抖音侧判不了。这里不静默写「无置顶」——
那会被运营读成「没置顶」，而真相是「这个接口根本看不到置顶」，
抖音行的置顶状态列完全不碰。

**同一件事在通道之间也成立，而且更阴险。** 平台能看见置顶，不等于这一轮
用的那家供应商会报置顶：主通道余额见底后整轮都会静默降到备胎（runner._call
把 QUOTA 通道判死，之后每一行直接走下一家），如果备胎的响应里压根没有
置顶这个维度，「没有任何一条带置顶标记」就会被当成「置顶掉了」写进表——
评论照常有、诊断信息一个字不报，运营只看到一屋子假告警。
所以下面的资格判断是**平台 × 通道**两个条件（见 Snapshot.pin_reported），
缺哪一个都按抖音那个待遇办：这一列完全不碰。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
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
    # 运营定的口径：它返回 True 就打「风控中」——风控中只认这一种硬证据
    # （链接失效另打「链接失效」），评论数异常一律走 疑似限流 / 无水花，不进风控。
    # 字段语义还没在真实被封的帖子上验过（只见过 false），所以打标签的同时
    # 仍在诊断信息里请人工确认，见 docs/待验证清单.md。
    censored: Optional[bool] = None
    # 上游给审核/受限标记时附带的一句人话（比如「详情接口不给笔记本体，只回
    # Note is not available」）。有它诊断信息就说具体的，没有就说通用的那句。
    censor_reason: str = ""

    # 这一轮的响应里**到底有没有置顶这个维度**——注意这问的不是「有没有置顶」，
    # 是「这家通道会不会报置顶」。两件事必须分开：平台能力（抖音没有置顶字段）
    # 和通道能力（同一个平台，换一家供应商就可能不报）都会让置顶判不了，
    # 而后者是运行期才会发生的——主通道余额一见底，整轮都降到备胎上去了。
    # 判据见 read_comment_page，两层：通道**声明**的能力优先（providers 注入的
    # `_capabilities`），没有声明才退回看 is_pinned 这个键在不在。
    # 声明必须排在前面，因为看键只认得出「这家不说话」，认不出「这家说了假话」
    # ——而后者是实测到的真实形态：SocialDataX 的 is_pinned 一直在，只是对真正
    # 置顶的那条也给 false（2026-09-21 同一条 comment_id 两家对打，见
    # providers.XHS_COMMENT_CAPABILITIES）。
    pin_reported: bool = True
    # 有没有**作者标记**这个维度，语义和 pin_reported 完全对称：问的是
    # 「这家通道会不会报作者」，不是「有没有作者评论」。它撑着的是负面判定
    # 里的 skip_author——靠它把品牌号自己的回复排除在负面命中之外。
    # 静默失效的后果和置顶那个方向相反：置顶是无中生有报一个「掉了」，
    # 这里是把自家回的「温和配方，不会过敏」算成负面，一条干净的帖子
    # 被写成「有负面」。
    # ⚠️ 所以处置也**不能**照抄置顶：置顶判不了就整列不碰，负面判不了却不能
    # 不判——漏掉真负面比误报一条糟得多。照常出结论，只在诊断信息里说清
    # 「这一轮自家回复没被排除」，让人工过一眼快照。见 decide()。
    # 抖音两家通道都恒定写这个键（值恒为 False），所以抖音行这个标志为真：
    # 抖音看不见作者是**平台**边界，docs/表结构.md 已经写明「这个排除等于
    # 没开」，每一行再报一次只是固定噪音。
    author_reported: bool = True
    # 这一轮的通道会不会原样带回蓝词标记 `#词[搜索高亮]#`。SocialDataX 会把它
    # 抹平成光秃秃的词（providers.XHS_COMMENT_CAPABILITIES 里的实测记录）——
    # 那一轮抠不到蓝词不等于评论里没有蓝词。首抓没关系（抠不到就下一轮再扫），
    # 爆贴/大爆那一次复扫却会被记成「扫过了、没有新词」，从此不再扫。
    # 所以通道不报时整个蓝词判定都不做，留给通道恢复后的下一轮。
    blue_words_reported: bool = True
    # 评论接口**自己**说有几条、却一条都不给的那几条（见 comments_hidden）。
    # 这种时候 comment_count 记的是**别人看得到的** 0——小红书界面上也不显示
    # 评论数——接口报的原数留在这里，只给诊断信息和存活判断用。
    hidden_comment_count: int = 0

    @property
    def pinned(self) -> Optional[CommentView]:
        return next((c for c in self.comments if c.is_pinned), None)

    @property
    def supports_pinned(self) -> bool:
        """这一轮有没有**资格**对置顶下结论。平台和通道缺一不可。"""
        return self.platform == "xhs" and self.pin_reported


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


# 一页评论最多几条（两家通道实测整页 10 条，见 providers 里置顶那段）。
FIRST_PAGE_SIZE = 10


def comments_hidden(snapshot: Snapshot) -> bool:
    """评论接口自己说「有 N 条」，N 一页就装得下，却一条都没给。

    这是平台的真话，不是没拿到：那 N 条评论**别人看不到**——被删了（计数
    还没跟上）、被折叠、在审核中，或者只有发评论的人自己看得到（小红书对
    营销评论常这么处理）。小红书界面上连评论数都不显示，点开也是空的
    （2026-10-16 实例：表里实时评论数 1、还挂着上一轮的「有负面」）。

    判据在 read_comment_page 里定，因为只有那里知道两件事：
    * 数是**评论接口这一页自己报的**——detail 回填的数和评论页是两次请求，
      评论页空着只说明这次没拿到（saw_comment_page 里那次 150 条的事故）；
    * 数**一页装得下**（一级评论 ≤ FIRST_PAGE_SIZE）——评论多到要翻页时，
      空页更可能是上游这一页没给，不下结论。
    """
    return snapshot.hidden_comment_count > 0


def _hidden_count(platform: str, data: dict[str, Any], comments: list,
                  count: Optional[int], top_level: Optional[int]) -> int:
    """read_comment_page 用：这一页说有、却一条都不给的评论数（不是这种情况返回 0）。

    * 只认通道明确作证的空页（`_empty_page`）：缺字段、类型不对、条目全解析
      不了，归一化之后也是空列表，但那是**没拿到**，不是「别人看不到」；
    * 只认小红书：实测过的是小红书（界面上不显示评论数、一页 10 条），抖音一页
      20 条、计数口径也没对过，不拿这条推断去改抖音的表；
    * 一级评论 0 条也算一页装得下——总数里剩的只是看不到的楼中楼，或者一级
      计数已经先归零了，正是这条要处理的「计数对不上」。
    """
    if platform != "xhs" or data.get("_empty_page") is not True:
        return 0
    fits = top_level if top_level is not None else count
    if comments or count is None or count <= 0 or fits is None:
        return 0
    return count if fits <= FIRST_PAGE_SIZE else 0


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

    第三种也算看到了：评论接口自己说有几条、一页装得下、却一条都没给
    （comments_hidden）——看到的就是「别人一条都看不到」。
    """
    return (bool(snapshot.comments) or snapshot.comment_count == 0
            or comments_hidden(snapshot))


def read_comment_page(platform: str, data: dict[str, Any]) -> Snapshot:
    """解析评论接口的一页返回。

    小红书这一次调用就同时给到评论总数、置顶评论和综合排序的前一页评论——
    R1/R2/R3 三个需求一次拿全，不需要再调 detail。
    抖音的 comment_count 类型是 integer|null，拿不到时留 None，由上层决定兜底。
    """
    items = data.get("items")
    comments: list[CommentView] = []
    # 兜底判据（`_capabilities` 缺席时才用）：这一页里只要有**一条**带着
    # is_pinned 这个键，就说明这条通道会报置顶；is_author_comment 同理。
    # 靠的是归一化层的契约（见 providers 顶部）：能报的通道对每一条评论恒定写
    # 这个键，True 和 False 都写。所以整页一个都没有 = 这家根本不报，而不是
    # 「这一页恰好没有置顶 / 没有作者评论」——两者写进表里的后果天差地别。
    pin_reported = False
    author_reported = False
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            pin_reported = pin_reported or "is_pinned" in item
            author_reported = author_reported or "is_author_comment" in item
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
    count = _int_or_none(data.get("comment_count"))
    top_level = _int_or_none(data.get("top_level_comment_count"))
    hidden = _hidden_count(platform, data, comments, count, top_level)
    return Snapshot(
        platform=platform,
        # 一条都看不到的那几条不算：表里的实时评论数跟小红书界面一致（0），
        # 热度、掉量、起量也只按别人看得到的算。
        comment_count=0 if hidden else count,
        hidden_comment_count=hidden,
        top_level_comment_count=0 if hidden else top_level,
        comments=comments,
        points_balance=points.get("balance"),
        pin_reported=_trusted(data, "pinned", pin_reported, comments),
        author_reported=_trusted(data, "author", author_reported, comments),
        # 蓝词标记写在正文里，没有「键在不在」可看：没有声明就当它报。
        blue_words_reported=_trusted(data, "blue_words", True, comments),
    )


def _trusted(data: dict[str, Any], dimension: str,
             saw_the_key: bool, comments: list[CommentView]) -> bool:
    """这一轮的响应在某个维度上可不可信。三层，由强到弱：

    1. **一条评论都没有** → 可信。不是因为看得出这家报不报，而是这个问题
       此刻不存在：没有评论就不可能有置顶，也不存在「自家回复被算成负面」。
       （空壳轮另有 saw_comment_page 那道闸挡着，走不到下结论这一步。）
    2. **通道声明了**（providers 注入的 `_capabilities`）→ 以声明为准。
       声明是唯一认得出「这家说了假话」的办法：SocialDataX 的 is_pinned
       一直在、值却是错的，从响应里怎么看都看不出来。
    3. 都没有 → 退回看键在不在（老行为，给直接构造字典的调用方和测试兜底）。
    """
    if not comments:
        return True
    caps = data.get("_capabilities")
    if isinstance(caps, dict) and dimension in caps:
        return bool(caps[dimension])
    return saw_the_key


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
        reason = data.get("_censor_reason")
        if isinstance(reason, str) and reason:
            snapshot.censor_reason = reason
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
        if snapshot.hidden_comment_count:
            return (f"（接口报评论数 {snapshot.hidden_comment_count}，但评论区一条都看不到："
                    "被删除、被折叠、审核中，或只有发评论的人自己看得到）")
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


# 小红书评论正文里的「蓝词」标记：被平台变成搜索超链接的词，原文长这样
#     是我理解的这个 #快充小片[搜索高亮]# 吗
# 这是小红书自己的富文本格式（话题是 `#xx[话题]#`），TikHub 原样透传（2026-09
# 实测能稳定看到）。只认「搜索高亮」：话题标签也是蓝色链接，但那是发评论的人
# 自己打的，不是平台判定出来的搜索词，运营要的是后者。
HIGHLIGHT_RE = re.compile(r"#([^#\[\]]+?)\[搜索高亮\]#")


@dataclass
class BlueWordSighting:
    """一个蓝词第一次出现在这一页的哪条评论里——「蓝词来源」记的就是它。"""

    word: str
    comment: CommentView
    text: str        # 那条评论的正文（空白已收成一个空格）
    at: int          # 标记在 text 里的位置：评论太长时围着它截一段


def highlighted(snapshot: Snapshot) -> list[BlueWordSighting]:
    """这一页评论里所有被标成搜索高亮的词，带出处。按出现顺序、去重
    （大小写和标点不计），同一个词只记它第一次出现的那条评论。

    只看第一页——和关键词、负面词一样，不为它多花一次请求。
    """
    seen: set[str] = set()
    found: list[BlueWordSighting] = []
    for comment in snapshot.comments:
        # 先收空白再抠：词里夹着换行的话，写进多选选项的就是一个带换行的词。
        text = re.sub(r"\s+", " ", comment.content or "").strip()
        for match in HIGHLIGHT_RE.finditer(text):
            word = match.group(1).strip()
            key = _normalize(word)
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(BlueWordSighting(word, comment, text, match.start()))
    return found


def highlighted_words(snapshot: Snapshot) -> list[str]:
    """highlighted 的词本身。"""
    return [s.word for s in highlighted(snapshot)]


def excluded_by(word: str, excludes) -> Optional[str]:
    """这个蓝词被哪个排除词排掉了（登记表里那一格，见 registry）；没排掉返回 None。

    两种匹配，按排除词本身是什么字分：

    * 带中文（或任何非 ASCII 字符）的按**子串**、不分大小写：「奥佳华」排得掉
      「奥佳华7598vpro」；
    * 纯英文 / 数字的按**整词**、不分大小写，前面挨着的不能是字母或数字：
      「sis」排得掉「sis黑胶」「SiS 能量胶」，排不掉「analysis」。英文按子串
      会误伤——这正是不拿每行的「负面词」来排的原因（careplus 里有 are）。
      以字母结尾的词后面**紧跟型号数字也算**（2026-10-26 运营定）：「Maurten」
      排得掉「Maurten320」，「sis」排得掉「sis2」；以数字结尾的（「C300」）
      后面再接数字就是另一个型号了（「C3000」），不排。

    不到 2 个字的排除词不生效（registry.read_blue_word_excludes 已经报过）。
    """
    for term in excludes or ():
        term = term.strip()
        if len(term) < 2:
            continue
        if term.isascii():
            after = "[A-Za-z]" if term[-1].isalpha() else "[A-Za-z0-9]"
            pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?!{after})"
            if re.search(pattern, word, re.IGNORECASE):
                return term
        elif term.casefold() in word.casefold():
            return term
    return None


def new_blue_words(found: list[str], current: list[str]) -> list[str]:
    """found 里哪些是「蓝词字段」还没有的。比对不计大小写、空白和标点：
    表里人工填的「gt33」和评论里高亮的「GT33」是同一个词，不该再提醒一次。"""
    known = {_normalize(w) for w in current}
    return [w for w in found if _normalize(w) not in known]


# ---------- 蓝词什么时候扫（2026-10-22 运营定的口径）----------
#
# 以前每轮都扫、每出一个新词就翻一次「是否截图」：三条评论的帖子也叫人去截图，
# 同一条帖子隔三差五冒一个新词就再叫一次。现在只在三个时刻扫：
#
#   1. 「蓝词字段」还空着 → 每轮都扫，抓到一次就停；
#   2. 帖子到「爆贴」（默认 50 条评论，项目里改过「爆贴门槛」就按项目的）→ 再扫一次；
#   3. 帖子到「大爆」（默认 100 条）→ 再扫一次。
#
# 扫到的新词只加不减（竞品的蓝词也留着）。截图提醒只在爆贴（含大爆）以上发：
# 爆贴以下抓到的词只记录、不叫人截图。
#
# 「到爆贴的那一次扫过没有」记在「蓝词来源」里：每条记录行首写着当时的档位，
# 记过的最高档位就是已经扫到哪一档了。只看「流量状态」这一轮有没有跨过门槛也
# 能定，但跨门槛那一轮要是正好降到抹掉蓝词标记的备胎、或者写回前重读失败，
# 这一次复扫就永远错过了；记在账上，没扫成的下一轮接着扫。

# 「蓝词来源」每条记录的行首：「2026-10-22 14:05 【评估中】」。只认行首——评论
# 正文里有人打了「【大爆】」不算（正文收过空白，不会自己起一行）。
_SOURCE_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} 【([^】\n]*)】", re.M)

# 「蓝词来源」里每条评论截多长。比快照那一列宽：这里要看得见蓝词前后在说什么。
SOURCE_EXCERPT_CHARS = 80


def scanned_tier_rank(log_text: str, settings: Settings) -> Optional[int]:
    """「蓝词来源」里记过的最高档位（rank）。一条记录都没有返回 None。

    行首写的档位不认识（未定档、改过名的旧档位）按 -1 算——比任何门槛都低，
    最坏是多扫一次，不会少扫。
    """
    heads = _SOURCE_HEAD_RE.findall(log_text or "")
    if not heads:
        return None
    return max(settings.tags.rank(h) for h in heads)


@dataclass(frozen=True)
class BlueWordScan:
    """这一轮扫蓝词的口径。"""

    rescan: bool    # False = 首抓（表里还一个蓝词都没有）；True = 到爆贴/大爆的那一次复扫
    remind: bool    # 帖子在爆贴（含）以上：评论里看得到蓝词就提醒截图
    stage: str      # 这一轮落表的热度档位，写进「蓝词来源」行首；没有档位写「未定档」


UNTIERED = "未定档"


def plan_blue_word_scan(settings: Settings, *, known_words: list[str], log_text: str,
                        read_tier: Optional[str], landed_tier: Optional[str]
                        ) -> Optional[BlueWordScan]:
    """这一轮扫不扫蓝词。None = 不扫，蓝词那几列一个都不碰。

    read_tier 是开跑时「流量状态」里的档位，landed_tier 是这一轮会写进去的档位
    （选项没建、写不进去的档位不算——不然那一档每轮都像是刚到）。

    复扫看的是「蓝词来源」这本账：记过的最高档位低于爆贴、现在到了爆贴 → 扫；
    大爆同理。账上一条都没有（这一列刚建、或者还没建）时，拿开跑时的档位当
    已经扫过的位置——上线那天已经爆贴的老帖不会集体再被叫一遍截图。
    """
    t = settings.tags
    now = t.rank(landed_tier or "")
    gates = [t.rank(g) for g in (t.hot, t.super_hot) if g and t.rank(g) >= 0]
    remind = bool(gates) and now >= min(gates)
    stage = landed_tier or UNTIERED
    if not known_words:
        return BlueWordScan(rescan=False, remind=remind, stage=stage)
    logged = scanned_tier_rank(log_text, settings)
    done = logged if logged is not None else t.rank(read_tier or "")
    if any(done < gate <= now for gate in gates):
        return BlueWordScan(rescan=True, remind=remind, stage=stage)
    return None


def _excerpt(text: str, at: int, width: int) -> str:
    """围着位置 at 截一段 width 长的正文，蓝词标记不会被截到外面去。"""
    if len(text) <= width:
        return text
    start = max(0, min(at - width // 4, len(text) - width))
    piece = text[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def blue_word_source_line(sighting: BlueWordSighting, *, when: str, stage: str,
                          fmt: DigestFormat) -> str:
    """「蓝词来源」里一个新词的那一行：时刻 【当时的档位】词 ← 出自哪条评论。

    昵称跟着快照那一列的开关走（DigestFormat.show_author_name）。
    """
    who = (f"{sighting.comment.author_name}: "
           if fmt.show_author_name and sighting.comment.author_name else "")
    excerpt = _excerpt(sighting.text, sighting.at, SOURCE_EXCERPT_CHARS)
    return f"{when} 【{stage}】{sighting.word} ← {who}{excerpt}"


def blue_word_rescan_line(*, when: str, stage: str, visible: list[str],
                          excluded: Optional[list[str]] = None) -> str:
    """复扫没有新词时也记一行——这一行就是「这一档扫过了」的账。"""
    tail = (f"，评论里还看得到：{'、'.join(visible)}" if visible
            else "，评论里也没看到蓝词")
    if excluded:
        tail += f"（排除词不记：{'、'.join(excluded)}）"
    return f"{when} 【{stage}】复扫：没有新蓝词{tail}"


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
    蓝词指评论里被平台变成搜索超链接的词，由 highlighted_words 另行抠出）。
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
    这里可以补的。**通道也是一道边界**：降级到作者标记不可信的备胎时，
    调用方（decide）会把 `skip_author` 直接关掉——不可信的标记不该拿来
    排除任何东西，排除就是信了它，而错标成作者的那条真负面会被静默丢掉。
    关掉之后自家回复也会算进命中，由诊断信息里那句提醒交给人工核一眼。

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

    UNSUPPORTED = "unsupported"   # 判不了：抖音没有这个字段，或这一轮的通道不报置顶
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

    返回 None 表示不碰这一列，三种情况：抖音（接口没有置顶字段）、
    这一轮的通道不报置顶（降级到备胎时会发生）、以及没看到评论页内容的
    空壳轮——全都保持原样。拿上游缺数当「掉了」的证据会让运营白跑一趟。
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
    # 这一轮的评论数相对上一次是不是「起量」（判据见 Thresholds.surged）。
    # 它不是标签：热度档位说的是「现在有多热」，这个说的是「这一轮涨得猛」，
    # 两件事。写不写进表由调用方决定——「起量时间」只记第一次。
    surged: bool = False
    # 第一页评论里被平台标成搜索高亮的词（蓝词），按出现顺序去重；sightings 带出处。
    # blue_words_checked=False（空壳轮、或这一轮的通道抹掉了蓝词标记）时两个都是
    # 空列表，调用方据此不碰蓝词那几列——那不是「没有蓝词」，是「没看到」。
    blue_words: list[str] = field(default_factory=list)
    blue_word_sightings: list[BlueWordSighting] = field(default_factory=list)
    blue_words_checked: bool = False
    # 评论里看得到、但被这张表的「蓝词排除词」排掉的词（不记、不叫截图）。
    blue_words_excluded: list[str] = field(default_factory=list)
    # 「评估中」增长判据：这一轮要不要把（本轮评论数, 本轮时刻）记成新基线——
    # 第一次进入判断、或者这个窗口涨够了。写不写由 runner 定（两列在不在）。
    eval_baseline_reset: bool = False
    # 这一轮要从行上**摘掉**的 sticky 标签。sticky 的意思是「机器不摘」，唯一的
    # 例外是机器自己打的「疑似限流」评论数恢复了（2026-10-26 运营定）。
    retract: set[str] = field(default_factory=set)
    # 「限流前评论数」要写成什么：None = 不动；正数 = 这次掉量之前的评论数；
    # 0 = 不再跟了（恢复了，或者人已经把标签摘了）。
    throttle_baseline: Optional[int] = None


def held_tier(current_tags: Optional[list[str]], settings: Settings) -> Optional[str]:
    """表里现有的热度档位——棘轮从这里起算。

    一般就是最高的那一档。例外是「无水花」：判过（或人手改成）无水花的帖，
    格子里哪怕还挂着「评估中」「观察中」，也按无水花算——否则排序上评估中
    高一档，下一轮就被改回去了（2026-10-22 运营定的口径）。爆贴、大爆照旧
    压过它：那两档只升不降，大爆不会因为删评退回爆贴，更不会退回无水花。
    """
    t = settings.tags
    best = t.top_tier(current_tags)
    if (t.flop and t.flop in (current_tags or [])
            and t.rank(best or "") <= t.rank(t.evaluating)):
        return t.flop
    return best


def _eval_growth(verdict: Verdict, settings: Settings, *, count: int,
                 count_tier: Optional[str], held: Optional[str],
                 age_hours: Optional[float], baseline_count: Optional[int],
                 baseline_at: Optional[datetime], now: Optional[datetime],
                 unready: str) -> tuple[bool, str]:
    """「评估中」的增长判据（2026-10-22 运营定的口径）。返回（判无水花了吗, 诊断那句话）。

    发布满 eval_start_hours、评论数在爆贴门槛以下、现在是评估中的帖：

    1. 没有基线 → 记下（本轮评论数, 本轮时刻），档位照旧；
    2. 距基线不到 eval_window_hours → 什么都不改；
    3. 满了 → 涨了不到 eval_min_gain 条判无水花；涨够了继续评估中、基线重记。

    「现在是评估中」包括评论数掉回评估中门槛以下、靠棘轮挂着评估中的帖——
    不然它们永远判不了：掉下去本来就是没涨。基线只在量到评论数的轮次写
    （巡查失败那一轮走不到这里），所以中间一轮失败，窗口就顺延到下一次量到为止。
    门槛读的是这张表生效的那一套（登记表里的「评估中门槛」「爆贴门槛」）。
    """
    t = settings.tags
    th = settings.thresholds
    # 不满冷启动窗口（flop_hours，默认 48 小时）绝不往下判无水花（2026-10-26
    # 运营再次确认）。EVAL_START_HOURS 最低能配到 1，单靠它挡不住。
    in_eval = (age_hours is not None
               and age_hours >= max(th.eval_start_hours, th.flop_hours)
               and count < th.tier_hot
               and (count_tier == t.evaluating or held == t.evaluating)
               and held != t.flop
               and t.rank(held or "") <= t.rank(t.evaluating))
    if not in_eval:
        return False, ""
    window = th.eval_window_hours
    if unready:
        return False, (f"⚠ {unready}，评估中满 {th.eval_start_hours} 小时后的增长判断"
                       "这一轮跳过了（面板上点这张表的「补齐缺的列」）")
    if baseline_count is None or baseline_at is None or (now is not None and baseline_at > now):
        verdict.eval_baseline_reset = True
        return False, (f"记下评估基线：评论数 {count}，满 {window} 小时看涨没涨够 "
                       f"{th.eval_min_gain} 条")
    if now is None:
        return False, ""
    elapsed = (now - baseline_at).total_seconds() / 3600
    gain = count - baseline_count
    # 「满没满 24 小时」和到期判断同一个口径（含 DUE_SLACK_MINUTES 的余量）：
    # 不然 sweep 一天只跑几次的部署上，一行到期被刷了，却因为还差两分钟不满
    # 24 小时而不判，要再等一整轮。
    if not settings.refresh.window_reached(elapsed, window):
        return False, (f"评估中：距基线（{baseline_count} 条）{elapsed:.0f} 小时，"
                       f"满 {window} 小时再判")
    if gain < th.eval_min_gain:
        return True, (f"评估中 {elapsed:.0f} 小时只涨了 {gain} 条（{baseline_count} → {count}，"
                      f"不足 {th.eval_min_gain} 条）→ {t.flop}")
    verdict.eval_baseline_reset = True
    return False, (f"评估中 {elapsed:.0f} 小时涨了 {gain} 条（{baseline_count} → {count}），"
                   "继续评估中，基线重记")


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
    eval_baseline_count: Optional[int] = None,
    eval_baseline_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
    eval_unready: str = "",
    throttle_baseline: Optional[int] = None,
    throttle_tracked: bool = False,
) -> Verdict:
    """算出这一行本次应有的机器标签和置顶判定。

    只产出 settings.tags.namespace() 里的标签。人工标签由 tags.merge 保护，
    这里完全不需要知道它们的存在。

    eval_baseline_count / eval_baseline_at 是「评估基线评论数 / 时间」那两格，
    now 是这一轮的时刻（窗口满没满拿它比）；eval_unready 非空 = 那两列不能用，
    增长判据整个跳过，这句话就是原因（写进诊断信息）。

    throttle_baseline 是「限流前评论数」那一格；throttle_tracked = 表里有这一列
    （没有就不跟，机器打的疑似限流照旧只能人摘）。
    """
    t = settings.tags
    th: Thresholds = settings.thresholds
    verdict = Verdict()

    count = snapshot.comment_count

    # —— 热度档位：互斥，取最高，且只升不降（无水花例外，见 held_tier）——
    previous_best = held_tier(current_tags, settings)
    if count is not None:
        # 评论数够得上档位时，发布时长根本不参与判定——先把这一档单独算出来，
        # 后面「发布时间有没有毛病」才问得准（它只在评论数定不了档时才是关键）。
        count_tier = th.heat_tier(count, t)
        # 发布时长能不能用，有两种坏法，处置相同、话术必须分开：
        # None = 那一格是空的；负数 = 填成了未来（手滑填错年份、时区搞反）。
        # 未来时间必须挡在这里：它满足「不足 48 小时」，会让一条日期填错的行
        # 拿到一个看着很合理的「观察中」，然后一直挂到那个错误时间点之后
        # 48 小时——错误被标签盖住，没人会发现。表里「最近检查时间」被填成
        # 未来时同样是显式挡掉的（见 rows.is_due / in_cooldown），口径一致。
        age_usable = age_hours is not None and age_hours >= 0
        # 发出去够久了还够不上最低热度档 → 无水花。这不是异常，就是没起来——
        # 和「疑似限流」（有量之后异常下跌）必须分开，证据完全不同。
        flopped = (count_tier is None and age_usable
                   and age_hours >= th.flop_hours)
        # 还在冷启动窗口里 → 观察中。刚发两小时只有几条评论再正常不过，
        # 这时候判「无水花」是误标；但**什么都不写**同样是错的：
        # 空格分不出「还没巡查」「巡查了没结论」「机器坏了」，
        # 于是这一批新帖只能靠人手工去填。这一档就是为了不留空格。
        observing = (count_tier is None and not flopped and bool(t.observing)
                     and age_usable and age_hours < th.flop_hours)
        tier = count_tier
        if flopped:
            tier = t.flop
        elif observing:
            tier = t.observing
        stalled, eval_note = _eval_growth(
            verdict, settings, count=count, count_tier=count_tier, held=previous_best,
            age_hours=age_hours if age_usable else None,
            baseline_count=eval_baseline_count, baseline_at=eval_baseline_at,
            now=now, unready=eval_unready)
        # 棘轮：算上表里已有的档位取最高。评论被删导致数字掉下去，不该让
        # 一条帖子从「大爆」退回「爆贴」——那种异常由「疑似限流」表达。
        # 两个例外都只在爆贴以下：增长判据判了无水花（评估中 → 无水花）；
        # 格子里已经是无水花（见 held_tier），没到爆贴门槛之前不回评估中。
        flop_held = previous_best == t.flop and count < th.tier_hot
        if stalled or flop_held:
            best = t.flop
        else:
            best = max(
                (x for x in (tier, previous_best) if x),
                key=t.rank,
                default=None,
            )
        if best:
            verdict.tags.add(best)
            if stalled:
                pass   # 增长判据那句话（eval_note）说清楚了
            elif flop_held and tier != t.flop:
                verdict.notes.append(
                    f"评论数 {count}，此前已判「{t.flop}」，没到「{t.hot}」"
                    f"（{th.tier_hot} 条）之前不回「{tier or t.evaluating}」")
            elif best != tier:
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
        if eval_note:
            verdict.notes.append(eval_note)
        # 发布时间有毛病、而且这一轮**正好要靠它**定档（评论数够不上任何一档）
        # → 如实报出来。这一条**不能**挂在 `if best` 的 else 上：表里已经有档位时
        # `best` 是真值，那条路径只会说「保留高档位」，而真正的问题是那一格坏了。
        # 最难发现的正是这种：一行卡在「观察中」，发布时间却是空的或未来的，
        # 于是它永远熬不到 48 小时、永远升不成「无水花」，表面上一切正常。
        if count_tier is None and not age_usable:
            column = settings.fields.publish_time
            if age_hours is None:
                verdict.notes.append(
                    f"⚠「{column}」是空的，算不出发布多久，本轮判不了热度档位"
                    "（把发布时间填上，下一轮自动补判）")
            else:
                verdict.notes.append(
                    f"⚠「{column}」填的是未来时间（还差 {-age_hours:.0f} 小时才到），"
                    "本轮判不了热度档位，请核对这一格")
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
    throttled_now = t.throttled in (current_tags or [])
    following = throttle_baseline if (throttle_baseline or 0) > 0 else None
    if (
        count is not None
        and previous_comment_count is not None
        and previous_comment_count >= th.risk_drop_min_baseline
        and count <= previous_comment_count * (1 - th.risk_drop_ratio)
    ):
        verdict.tags.add(t.throttled)
        note = (f"⚠ 评论数从 {previous_comment_count} 掉到 {count}，"
                f"跌幅超过 {int(th.risk_drop_ratio * 100)}% → {t.throttled}（也可能是删评/折叠）")
        if throttle_tracked:
            if not throttled_now:
                # 这一次是机器新打的：记下掉量前的数，恢复了机器自己摘。
                following = previous_comment_count
                verdict.throttle_baseline = following
            # 已经挂着：在跟的（又掉了一截）沿用最早那个数；没在跟的是人手打的、
            # 或这一列建之前打的——不接手，照旧人摘。
            if following is not None:
                note += (f"；评论数回到 {math.ceil(following * th.throttle_recovery_ratio)} 条"
                         f"以上（掉量前 {following} 条的 {th.throttle_recovery_ratio:.0%}）"
                         "机器自己摘")
        verdict.notes.append(note)
    elif throttle_tracked and following is not None:
        if not throttled_now:
            # 人已经把「疑似限流」摘了：这次掉量不用再跟。
            verdict.throttle_baseline = 0
        elif count is not None and count >= following * th.throttle_recovery_ratio:
            # —— 疑似限流恢复：机器自己打的，评论数回来了，机器自己摘 ——
            # 只摘有「限流前评论数」的：人手打的、或这一列建之前打的，机器不知道
            # 回到多少才算恢复，照旧人摘。
            verdict.retract.add(t.throttled)
            verdict.throttle_baseline = 0
            verdict.notes.append(f"评论数回到 {count}（掉量前 {following}）→ 摘掉「{t.throttled}」")

    # —— 起量：掉量的镜像。这一轮涨得够猛，记下**发现**它的时刻 ——
    # 判据在 Thresholds.surged（两个闸门 + 第一次量到不算）。这里只下结论、
    # 不打标签：热度档位回答「现在有多热」，起量回答「这一轮涨得猛」。
    # 真正写不写「起量时间」由 runner 决定——那一列只记第一次。
    if th.surged(count, previous_comment_count):
        verdict.surged = True
        gain = count - previous_comment_count
        # 上一轮是 0 条时算不出百分比（除零），如实只报增量。
        pace = (f"（+{gain}，涨了 {gain / previous_comment_count:.0%}）"
                if previous_comment_count else f"（+{gain}）")
        # 措辞只陈述**这一轮的事实**，不用「→ 起量」那种箭头结论句式——
        # 箭头在这份诊断里一直表示「所以写了什么」（→ 疑似限流 是真打了标签）。
        # 而第二波、第三波同样会走到这里，「起量时间」却只记第一次；
        # 每波都写一句带箭头的话，运营会去表里找一个没有变过的格子。
        # 真正落了戳的那一轮由 runner 在后面补一句（它才知道格子空不空）。
        verdict.notes.append(
            f"评论数从 {previous_comment_count} 涨到 {count}{pace}，这一轮起量")

    # —— 审查标记：上游明确说这条在审核/受限，才打「风控中」——
    if snapshot.censored:
        verdict.tags.add(t.risk)
        if snapshot.censor_reason:
            verdict.notes.append(f"⚠ {snapshot.censor_reason} → 风控中，请人工确认")
        else:
            verdict.notes.append("⚠ 上游把这条标成了审核中/受限 → 风控中，请人工确认")

    # —— 置顶 ——
    verdict.pin = decide_pin(snapshot)
    # 只有小红书、这一轮的通道会报置顶、且本轮真的看到了评论页（有评论、
    # 或至少知道评论数）才有资格对置顶下结论——空壳轮拿上游缺数当「掉了」
    # 的证据会误报；现有通道上小红书的空壳在上游层就被译成 GONE 到不了这里，
    # 这道闸防的是上游契约漂移。
    verdict.pin_checked = snapshot.supports_pinned and saw_comment_page(snapshot)
    # 平台有置顶、评论也拿到了，却整页没有置顶这个维度 = 这一轮走的通道不报
    # 置顶。**必须说出来**：不碰那一列的结果和「什么都没发生」长得一模一样，
    # 运营看到的是一个再也不更新的旧状态，而真相是这一轮压根没查过置顶。
    if (snapshot.platform == "xhs" and not snapshot.pin_reported
            and saw_comment_page(snapshot)):
        verdict.notes.append(
            "本轮这条数据通道不提供可信的置顶标记（多半是主通道余额/故障降到了"
            "备胎），「置顶状态」保持原样——是没查过，不是没置顶")
    # 掉落的那一轮在诊断信息里额外报一声——自家帖子的置顶掉了，
    # 最该被立刻发现；之后每轮的状态由「置顶状态」列自己持续表达。
    if (
        verdict.pin_checked
        and verdict.pin is Pin.NONE_PINNED
        and current_pin_status == settings.pin_status.pinned_ok
    ):
        verdict.notes.append("⚠ 此前已确认有置顶，本轮置顶已不在")

    if comments_hidden(snapshot):
        verdict.notes.append(
            f"接口报评论数 {snapshot.hidden_comment_count}，但评论区一条都看不到"
            "（被删除、被折叠、审核中，或只有发评论的人自己看得到；小红书界面上也不显示）"
            "→ 实时评论数按 0 记，评论状态、负面状态按「没有可见评论」判")

    # —— 关键词命中：任一关键词出现在第一页任一条评论里即算命中 ——
    # 只在真的看到了评论页（有评论、或至少知道评论数）时才下结论：
    # 空壳轮（items 空 + 评论数也没拿到）写「未命中/没有显示」是拿
    # 上游缺数当证据，会诱导运营去无谓补评论。
    if seed_keywords and saw_comment_page(snapshot):
        verdict.seed_checked = True
        verdict.seed_hit = match_seed_keywords(snapshot, seed_keywords)
        if verdict.seed_hit is None and not snapshot.comments:
            verdict.notes.append("⚠ 评论区一条可见的评论都没有 → 评论没有显示")
        elif verdict.seed_hit is None:
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
        # 作者标记不可信时**不拿它排除任何东西**——排除就是信了它。
        # 一边声明「这家的作者标记不可信」、一边还照着它把评论丢掉，是自相
        # 矛盾的，而且坏在最要命的方向上：一条真负面被错标成作者就会被静默
        # 丢掉，命中数归零，于是负面告警和下面那句提醒**一个都不发**。
        # 宁可把自家回复也算进来（下面那句提醒会让人去核一眼），
        # 也不能漏掉真负面。
        verdict.negative_hits = match_all_keywords(
            snapshot, negative_keywords, skip_author=snapshot.author_reported)
        if verdict.negative_hits:
            words = "、".join(dict.fromkeys(h.keyword for h in verdict.negative_hits))
            verdict.notes.append(
                f"⚠ 第一页评论命中 {len(verdict.negative_hits)} 条负面/竞品词（{words}）"
                f"→ {settings.negative_status.found}，详见「{settings.fields.negative_digest}」"
            )
            # 这一轮的通道不报作者标记 → skip_author 静默失效，自家账号回的
            # 「温和配方，不会过敏」会原样算成负面命中。**结论照出**（漏掉真
            # 负面比误报一条糟得多），但必须说出来是谁在报警：不说的话运营
            # 看到的是一条一模一样的「有负面」，照着它去走公关流程，
            # 而命中的可能是自己昨天回的那一句。
            # 只在真有命中时才报——没命中就没有「排除没生效」这回事，
            # 每轮都挂一句只会把真正要看的那几条淹掉。
            if not snapshot.author_reported:
                # 措辞是给运营看的，别在这里用 markdown 记号——「诊断信息」是
                # 飞书的纯文本格子，星号会原样显示出来（其余诊断语句也都不带）。
                verdict.notes.append(
                    "⚠ 本轮这条数据通道不报作者标记（多半是主通道余额/故障降到了"
                    "备胎），自家账号的回复没有被排除在上面的命中之外"
                    "——先过一眼快照，确认命中的不是自己回的")
    elif negative_keywords:
        verdict.notes.append("本轮未取到评论页内容，负面词判定保持原样")

    # —— 蓝词：评论正文里的 `#词[搜索高亮]#` 标记，同一份第一页评论 ——
    # 这一轮扫不扫、哪些是新词、要不要翻「是否截图」由调用方对着表里的现值
    # 决定（plan_blue_word_scan）；这里只负责把词和出处抠出来。
    # 登记表里配的排除词（竞品名）在这里就筛掉：后面的「是新词吗」「要不要
    # 截图」「还没抓到过蓝词吗」都只看剩下的——一轮里只出现了排除词，就和
    # 没出现蓝词一样，「是否截图」不翻。
    if saw_comment_page(snapshot) and snapshot.blue_words_reported:
        verdict.blue_words_checked = True
        for sighting in highlighted(snapshot):
            if excluded_by(sighting.word, settings.blue_word_excludes):
                verdict.blue_words_excluded.append(sighting.word)
            else:
                verdict.blue_word_sightings.append(sighting)
        verdict.blue_words = [s.word for s in verdict.blue_word_sightings]

    return verdict


def gone_verdict(settings: Settings, reason: str = "",
                 current_tags: Optional[list[str]] = None) -> Verdict:
    """帖子确认取不到时的结论（已经过两击确认，不是第一次失败就走这里）。

    打「链接失效」（2026-10-26 运营定，以前打「风控中」——下游参考库把带
    「风控中」的帖子一律当风控处理，爆过又被删的帖会因此被撤出参考库）。
    事实细节（几次没取到、上游原话）在「巡查状态=已失效」和诊断信息里，
    流量状态这列只留运营真正会去筛的那一个词。行上已有的「风控中」不动
    （sticky，人摘）。链接又取得到时，下一轮机器自己摘掉「链接失效」。
    已有的热度档位原样保留：「爆过就是爆过」的棘轮不因帖子死了而清史——
    一条大爆过的帖子被删，恰恰是最需要留着「大爆」标签供复盘的那种。
    """
    verdict = Verdict()
    if settings.tags.link_dead:
        verdict.tags.add(settings.tags.link_dead)
    best = held_tier(current_tags, settings)
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


# ---------- 数据整理过时提醒 ----------
#
# 老帖都做过一次「数据整理」：运营把后台截图丢进「相关截图」，AI 捷径读出
# `曝光量：600；阅读量：43；互动量：2`。那一份数字对应的是**截图那一刻**——
# 之后评论区又涨了一大截的话，后台的曝光 / 阅读 / 互动多半也早变了，
# 表里那份整理就过时了，该重新回收后台数据。
#
# 机器做的事只有两件：第一次看到一份整理时记下「那一刻的评论数」当基线；
# 之后每轮拿实时评论数和基线比，涨够了就提醒。全程只读「数据整理」，
# 不碰截图、不碰三个拆数公式，也不多发一个付费请求——评论数本来就在量。

# 拆「互动量」那一段，和「互动数」那个拆数公式同一个口径：冒号后面的
# 东西整段当一个数（VALUE 转得动才算），认不出（AI 写了「/」、带了「万」）
# 就当这一份整理里没有互动量，和那一列空着是一回事。
_INTERACTION_RE = re.compile(r"互动量\s*[：:]\s*([^；;\s]*)")


def collation_interactions(text: Any) -> Optional[int]:
    """从「数据整理」那行字里拆出互动量。认不出返回 None。"""
    if not isinstance(text, str):
        return None
    found = _INTERACTION_RE.search(text)
    if not found:
        return None
    raw = found.group(1).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return None
    value = int(float(raw))
    return value if 0 <= value <= MAX_COUNT else None


@dataclass
class Recollect:
    """这一轮对「数据整理」的结论。三个值都是要写进表的；None = 那一格不碰。"""

    status: str
    baseline_comments: Optional[int] = None
    baseline_interactions: Optional[int] = None
    stale: bool = False
    note: str = ""


def recollect_check(
    *,
    collation_text: Any,
    stored_comments: Optional[int],
    stored_interactions: Optional[int],
    comment_count: Optional[int],
    settings: Settings,
) -> Optional[Recollect]:
    """评论数相对数据整理那一刻涨了多少，要不要提醒重新回收后台数据。

    返回 None = 这一轮不碰这三列（没有整理、认不出互动量、这一轮没量到评论数）。

    **基线怎么来。** 整理是人在某个时刻做的，机器要到下一次巡查才看得见
    （老帖 3 天一轮），手里最贴近的只有这一轮量到的评论数——它只会比整理
    那一刻多、不会少（删评另说），拿它当基线是**保守**的：宁可晚提醒，
    不会刚整理完就报「过时了」。

    但整理那一刻的评论数还有一个硬上界：**不可能超过当时的互动量**
    （互动量 = 赞 + 藏 + 评……，评论是其中一项）。所以基线取两者较小的那个。
    这一刀专门照顾上线那一刻已经整理过的老帖：它们的整理可能是几周前做的，
    这一轮评论数要是已经比当时整个互动量还多出一截，那份整理早就过时了，
    不用再等它涨一轮才说。

    **怎么认出「换了一份整理」。** 基线连同那份整理的互动量一起记下
    （「整理时互动数」）；之后读到的互动量和它不一样，就是运营重新截图、
    AI 重新读了一份——基线重记、状态回到「已整理」，不用人去改那一格。
    手工清空「整理时评论数」也会让机器在下一轮重新记基线。
    """
    interactions = collation_interactions(collation_text)
    if interactions is None or comment_count is None:
        return None
    labels = settings.collation_status
    th = settings.thresholds

    fresh_collation = stored_comments is None or stored_interactions != interactions
    baseline = min(comment_count, interactions) if fresh_collation else stored_comments
    stale = th.recollect(comment_count, baseline)

    note = ""
    if stale:
        gain = comment_count - baseline
        pace = f"，涨了 {gain / baseline:.0%}" if baseline else ""
        if fresh_collation and baseline == interactions < comment_count:
            note = (f"📊 评论数 {comment_count}，已经比「{settings.fields.collation_source}」"
                    f"里整个互动量 {interactions} 还多 {gain} 条——那份后台数据早就过时了"
                    f" → {labels.stale}，请重新回收后台数据截图")
        else:
            note = (f"📊 数据整理时评论数 {baseline}（互动量 {interactions}），"
                    f"现在 {comment_count}（+{gain}{pace}）→ {labels.stale}，"
                    "请重新回收后台数据截图")
    elif fresh_collation:
        note = f"📊 记下这份数据整理的基线：互动量 {interactions}、评论数 {baseline}"
    return Recollect(
        status=labels.stale if stale else labels.fresh,
        baseline_comments=baseline if fresh_collation else None,
        baseline_interactions=interactions if fresh_collation else None,
        stale=stale,
        note=note,
    )
