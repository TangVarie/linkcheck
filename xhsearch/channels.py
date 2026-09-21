"""通道此刻还能不能用，以及用不了的时候我们丢了什么能力。

## 为什么这件事值得单独一个模块

面板上那个「余额还够跑 N 天」是**两家加起来**算的。主通道余额见底、备胎还
很充裕时，它照样是绿的——而这恰恰是 2026-09 那次事故的形态：TikHub 归零
→ `runner._call` 把它判死 → 整轮小红书降到备胎 → 一整表的「置顶成功」被刷成
「置顶掉了」，**面板上一个字都没报**。

所以要的不是「还剩多少钱」，是几个更具体的问题：

    这家在**这个平台上**还发得出一次请求吗？  ← 不是「余额是不是 0」，
                                              也不是所有平台一个口径
    顶上来的是谁？它比这家少了哪几项能力？     ← 照实测登记表逐项比
    说得准吗？                                ← 读不到余额时一个字都不许编

## 三条贯穿始终的纪律

**一、按平台分别算，每一条结论都绑着平台。** 同一家对小红书下线、对抖音
还能用是常态（抖音一次 ¥0.0072，小红书 ¥0.072，差十倍）；两个平台的备胎
也可以配成不同的（`CHANNEL_ORDER`）。所以「降到了谁」「有没有备胎」都是
**每个平台各自一份**，只有处境完全相同的平台才合并成一条告警。

**二、读不到就说说不准，绝不替它编一个状态。** 这和 `balance.py` 开头那条
「读不到就报读不到，不报 ¥0」是同一条纪律，而且这里有**两个**方向要挡：
把读不到算成「下线了」会在 Key 过期时报假警；把读不到算成「备胎顶上了」
同样是编——它的 Key 可能就是坏的，付费端点照样会拒。

**三、只说自己算得出的那部分。** 实际走哪家还取决于**链接形态**
（`runner._call` 会拿 `provider.can_handle` 过滤顺序），而面板手上没有行、
看不到链接。所以这里不假装能预测每一行，只报一件算得出的事：某一家空了、
而首选恰好接不了某种形态、只有它能接——那些行会刷不到。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import providers

PLATFORM_LABELS = {"xhs": "小红书", "douyin": "抖音"}

# 能力维度 → 运营看得懂的说法。`providers.XHS_COMMENT_CAPABILITIES` 里那几个
# 名字是判定层的词汇，这里是给人看的话，两套刻意不混：改判定不该顺手改话术，
# 改话术更不该碰到判定。
CAPABILITY_LABELS = {
    "pinned": "置顶监控",
    "author": "负面判定里「自家回复不算」",
    "blue_words": "蓝词回填",
}

# TikHub 的赠送额度（每日签到送的 free_credit）**不是所有端点都认**：
# 小红书那两个端点 `allow_free_credit = 0`，抖音的可以用（实测，见
# docs/供应商对比.md 二）。所以同一笔赠送额度，对抖音算钱、对小红书不算。
#
# ⚠️ 注意这和 balance.py 里「free_credit 不算进余额、也不算进还够跑多久」
# 不矛盾，方向恰恰相反：那边算的是「还能跑多久」，少算一点是**保守**；
# 这边判的是「是不是已经发不出请求了」，少算一点会**造一条假告警**。
# 同一个数字，在两个问题上的安全方向不同。
FREE_CREDIT_PLATFORMS = frozenset({"douyin"})

# 拿来问 `can_handle` 的「链接形态」样本。值本身无所谓——那几个判据只看
# 有没有 aweme_id / note_id 这类 ID 字段，不看链接内容。
_LINK_SHAPED = {"url": "https://example.invalid/probe"}


@dataclass
class Outage:
    """一条数据通道在某几个平台上发不出请求了，以及由此丢了什么。

    只有**处境完全相同**的平台才会合在一条里（同一家、同样的角色、同样的
    备胎结论）。备胎不同就是两条——把它们并成一条，必然有一个平台的说法
    是假的。
    """

    channel: str
    label: str
    # 还能花多少、发一次要多少。两个都显示，因为「还剩 ¥0.04」单看像是还有钱，
    # 配上「一次要 ¥0.07」才看得懂为什么说它下线了。一条里含多个平台时取
    # **最便宜的那次调用**——「连最便宜的一次都发不起」是最强的真话。
    yuan_left: float
    one_call_yuan: float
    platforms: list = field(default_factory=list)
    # 顶上来的那家。空串 + fallback_unknown=False = 确认没有备胎了。
    fallback_label: str = ""
    # 后面只剩「余额读不到」的通道：**说不准**。既不能说降级成功
    # （它的 Key 可能就是坏的），也不能说彻底停摆（它可能好好的）。
    fallback_unknown: bool = False
    # 降到备胎之后丢掉的能力（中文）。只有小红书有登记表，所以只在小红书
    # 那一份上算得出来。
    lost: list = field(default_factory=list)
    # primary    = 这个平台的首选，整轮都走它
    # shape_only = 首选还健康，但首选接不了某种链接形态、只有这家能接
    #              （展不开的抖音短链就是这种），那些行会刷不到
    role: str = "primary"

    @property
    def stopped(self) -> bool:
        """确认没有任何通道顶得上：这些平台彻底不转了。

        `fallback_unknown` 时**不算** stopped——那时候我们不知道，
        而「巡查已经停了」是一句不能靠猜说出口的话。
        """
        return (self.role == "primary"
                and not self.fallback_label and not self.fallback_unknown)


def _spendable_yuan(balance: Any, platform: str) -> Optional[float]:
    """这家在这个平台上还能花多少钱（人民币）。**None = 读不到，不下结论。**"""
    if balance is None or not getattr(balance, "ok", False):
        return None
    yuan = getattr(balance, "yuan", None)
    if yuan is None:
        return None
    credit = getattr(balance, "free_credit", None)
    rate = getattr(balance, "rate", None)
    # 赠送额度和 amount 同单位（TikHub 是美元），要拿汇率折成人民币才能加。
    # 折不了（没有 rate）就不加——宁可低报，也不拿一个单位不明的数去
    # 抵消一条可能是真的告警。
    if platform in FREE_CREDIT_PLATFORMS and credit and isinstance(rate, (int, float)):
        yuan += credit * rate
    return yuan


def _affordable(balance: Any, platform: str, one_call_yuan: float) -> Optional[bool]:
    """这家在这个平台上还发得出一次请求吗。None = 读不到。"""
    spendable = _spendable_yuan(balance, platform)
    if spendable is None:
        return None
    return spendable >= one_call_yuan


def _pick_standby(rest: list, by_name: dict, platform: str) -> tuple:
    """顺着配置往后找顶上来的那一家。返回 `(名字, 说不准)`。

    三种结局，缺一不可：

        ("socialdatax", False)  确认它还付得起 → 可以说「降到了它」
        ("",            True)   后面只剩读不到余额的 → **说不准**
        ("",            False)  后面全都确认付不起 → 真的没有备胎了

    读不到的那家不会让搜索停下来：后面若还有一家确认付得起，那家才是答案。
    """
    unknown = False
    for name, cost in rest:
        payable = _affordable(by_name.get(name), platform, cost)
        if payable is None:
            unknown = True
            continue
        if payable:
            return name, False
    return "", unknown


def _lost_capabilities(primary: str, standby: str) -> list:
    """主通道有、备胎没有的那几项能力，照登记表逐项比。

    照表生成而不是写死一句「置顶和蓝词没了」：哪天备胎验收通过、表里那一格
    翻成 True，这句话要跟着自己变短，而不是还得有人记得回来改文案。
    """
    caps = providers.XHS_COMMENT_CAPABILITIES
    before = caps.get(primary) or {}
    after = caps.get(standby) or {}
    return [CAPABILITY_LABELS.get(dim, dim)
            for dim, ok in before.items()
            if ok and not after.get(dim)]


def _takes_links(name: str, platform: str) -> bool:
    """这家收不收「只有链接、没有 ID」的那种调用。

    `runner._call` 会拿 `can_handle` 过滤通道顺序，所以首选接不了、而它接得了
    的时候，**它才是那些行的实际首选**——抖音短链展不开时就是这样。
    """
    return providers.get_provider(name).can_handle(platform, "comments", _LINK_SHAPED)


def outages(balances: Any, settings: Any, api_keys: Any) -> list:
    """哪几条通道此刻发不出请求了，以及各自的处境。

    纯函数，不发任何请求——余额是 `BalanceFeed` 早就取好的（那两个端点官方
    标明零费用），通道顺序来自 settings，能力来自 providers 那张实测登记表。
    面板「永远不发付费请求」这条不变量一点没动。
    """
    by_name = {getattr(b, "channel", ""): b for b in (balances or [])}
    keys = providers.credentials(api_keys)
    records: list[tuple] = []     # (分组键, 平台, 单价, 能花多少, 丢了什么)

    for platform in PLATFORM_LABELS:
        order = providers.usable_order(settings.channels, platform, keys)
        if not order:
            continue
        # 用**评论接口**的价：每一行必发的就是它，detail 是可选的第二发，
        # 拿它当门槛会高估。
        states = [
            (name, providers.get_provider(name).yuan_per_call(platform, "comments"))
            for name in order
        ]
        primary, primary_cost = states[0]

        if _affordable(by_name.get(primary), platform, primary_cost) is False:
            standby, unknown = _pick_standby(states[1:], by_name, platform)
            lost = (_lost_capabilities(primary, standby)
                    if platform == "xhs" and standby else [])
            label = providers.get_provider(standby).label if standby else ""
            records.append((
                (primary, "primary", label, unknown), platform, primary_cost,
                _spendable_yuan(by_name.get(primary), platform) or 0.0, lost))
            continue

        # 首选还付得起。但**实际走哪家还看链接形态**：首选接不了、只有后面
        # 某一家能接的那种形态，那家才是那些行的首选。它空了，那些行就刷不到，
        # 而上面那道闸一个字都不会报。面板看不到具体的行，所以只报这一件
        # 算得出的事，不假装能预测每一行。
        if _takes_links(primary, platform):
            continue
        for name, cost in states[1:]:
            if (_affordable(by_name.get(name), platform, cost) is False
                    and _takes_links(name, platform)):
                records.append((
                    (name, "shape_only", "", False), platform, cost,
                    _spendable_yuan(by_name.get(name), platform) or 0.0, []))

    return _group(records)


def _group(records: list) -> list:
    """把逐平台的记录合并成告警：**处境相同的平台才合并**。

    备胎不同、角色不同的平台绝不并进同一条——并了就必然有一个平台的说法
    是假的（比如 `douyin=[tikhub]` 没有备胎，却跟着小红书一起被说成
    「降到了 SocialDataX」）。
    """
    found: dict[tuple, Outage] = {}
    for key, platform, cost, spendable, lost in records:
        channel, role, fallback_label, unknown = key
        outage = found.get(key)
        if outage is None:
            outage = Outage(
                channel=channel,
                label=providers.get_provider(channel).label,
                yuan_left=spendable, one_call_yuan=cost,
                fallback_label=fallback_label, fallback_unknown=unknown,
                role=role)
            found[key] = outage
        else:
            # 一条里含多个平台时取**最便宜的那次调用**和对应的最小余额：
            # 「连最便宜的一次都发不起」是这组平台上最强的那句真话。
            if cost < outage.one_call_yuan:
                outage.one_call_yuan = cost
                outage.yuan_left = spendable
        outage.platforms.append(PLATFORM_LABELS[platform])
        for item in lost:
            if item not in outage.lost:
                outage.lost.append(item)
    return list(found.values())
