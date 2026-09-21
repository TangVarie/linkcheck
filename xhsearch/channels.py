"""通道此刻还能不能用，以及用不了的时候我们丢了什么能力。

## 为什么这件事值得单独一个模块

面板上那个「余额还够跑 N 天」是**两家加起来**算的。主通道余额见底、备胎还
很充裕时，它照样是绿的——而这恰恰是 2026-09 那次事故的形态：TikHub 归零
→ `runner._call` 把它判死 → 整轮小红书降到备胎 → 一整表的「置顶成功」被刷成
「置顶掉了」，**面板上一个字都没报**。

所以要的不是「还剩多少钱」，是两个更具体的问题：

    这家还发得出一次请求吗？           ← 不是「余额是不是 0」
    顶上来的那家，比它少了哪几项能力？  ← 照 providers 的实测登记表逐项比

第一个问题的口径很关键：TikHub 剩 $0.005 而一次小红书调用要 $0.01，它对
我们来说**已经下线了**，余额却还是正数。按「余额 > 0」判会漏掉这一整段。

## 为什么不写进 panel.py

`tests/test_panel.py::TestNeverSpends` 钉着一条硬不变量：**panel.py 不许
import providers**，也不许调 `get_provider`。那条不变量的价值在于「审计
panel.py 这一个文件就能确认面板不会花钱」，而这里要读供应商的单价和能力
登记表，必然要碰 providers。

所以放在这里：本模块**只读 providers 里的常量和纯函数**（单价、能力表、
通道顺序过滤），不发任何请求、不写任何东西。面板通过 `channels.` 这个
命名空间调它，panel.py 自己的 AST 里一个 `providers.` 都不会出现。
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


@dataclass
class Outage:
    """一条数据通道此刻发不出请求了，以及这件事让我们丢了什么。"""

    channel: str
    label: str
    # 还剩多少钱、发一次要多少钱。两个都显示出来，因为「还剩 ¥0.04」单看
    # 像是还有钱，配上「一次要 ¥0.07」才看得懂为什么说它下线了。
    yuan_left: float
    one_call_yuan: float
    # 受影响的平台（中文名）。同一家可能同时是两个平台的主通道。
    platforms: list = field(default_factory=list)
    # 顶上来的那家。**空串 = 没有备胎了**，那是更严重的一种：整个平台停摆，
    # 不是「少几项能力」。
    fallback_label: str = ""
    # 降到备胎之后丢掉的能力（中文）。没有备胎时为空——那时候丢的不是
    # 某几项能力，是整条链路。
    lost: list = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        """没有备胎顶上：这个平台这一轮彻底不转了。"""
        return not self.fallback_label


def _can_pay_for_one_call(balance: Any, one_call_yuan: float) -> Optional[bool]:
    """这家还发得出一次请求吗。**None = 读不到余额，不下结论。**

    和 `balance.py` 开头那条「读不到就报读不到，不报 ¥0」同一个道理：
    读不到余额和余额真的见底，要人做的事完全相反（一个去查 Key、查网络，
    一个去充值）。这里若把「读不到」算成「下线了」，面板就会在 Key 过期的
    时候报一条「置顶监控停摆」，把人引向错误的那一边。
    """
    if balance is None or not getattr(balance, "ok", False):
        return None
    yuan = getattr(balance, "yuan", None)
    if yuan is None:
        return None
    return yuan >= one_call_yuan


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


def outages(balances: Any, settings: Any, api_keys: Any) -> list:
    """哪几条通道此刻发不出请求了，以及各自丢了什么能力。

    纯函数，不发任何请求——余额是 `BalanceFeed` 早就取好的（那两个端点官方
    标明零费用），通道顺序来自 settings，能力来自 providers 那张实测登记表。
    面板「永远不发付费请求」这条不变量一点没动。

    返回按通道去重：同一家可能同时是两个平台的主通道，那是一条告警、
    两个受影响平台，不是两条告警。
    """
    by_name = {getattr(b, "channel", ""): b for b in (balances or [])}
    keys = providers.credentials(api_keys)
    found: dict[str, Outage] = {}

    for platform in PLATFORM_LABELS:
        order = providers.usable_order(settings.channels, platform, keys)
        if not order:
            continue
        # 逐家算「还发得出一次请求吗」。用**评论接口**的价：每一行必发的
        # 就是它，detail 是可选的第二发，拿它当门槛会高估。
        states = [
            (name, providers.get_provider(name).yuan_per_call(platform, "comments"))
            for name in order
        ]
        primary, primary_cost = states[0]
        if _can_pay_for_one_call(by_name.get(primary), primary_cost) is not False:
            continue     # 还付得起，或者读不到余额（读不到就不下结论）

        # 顶上来的是后面第一家**没被判定为付不起**的。读不到余额的也算候选：
        # 它可能好好的，不该因为读不到就报「没有备胎了」——那是更吓人的
        # 一句话，不能靠猜说出口。
        standby = next(
            (n for n, cost in states[1:]
             if _can_pay_for_one_call(by_name.get(n), cost) is not False),
            "",
        )
        outage = found.get(primary)
        if outage is None:
            balance = by_name.get(primary)
            outage = Outage(
                channel=primary,
                label=(getattr(balance, "label", "")
                       or providers.get_provider(primary).label),
                yuan_left=float(getattr(balance, "yuan", 0.0) or 0.0),
                one_call_yuan=primary_cost,
            )
            found[primary] = outage
        outage.platforms.append(PLATFORM_LABELS[platform])
        # 备胎和能力按第一次算出来的留着。同一家在两个平台上的备胎理论上
        # 可以配成不同的，真出现那种配置时，先把人叫过来比把每种组合都
        # 罗列清楚更要紧。
        if not outage.fallback_label and standby:
            outage.fallback_label = providers.get_provider(standby).label
        # 能力登记表是小红书评论页的，所以只在小红书这一轮比。
        if platform == "xhs" and standby and not outage.lost:
            outage.lost = _lost_capabilities(primary, standby)

    return list(found.values())
