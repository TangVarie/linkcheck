"""SocialDataX 备用通道的开关：**默认关**，运营在面板上点开。

## 为什么要有这个开关

2026-11-28 北京时间 07:36 到 13:11，TikHub 对每一个请求都回 HTTP 400「请求失败，
请重试……本次请求不会被扣费」。那时 400 归的是行级失败，不在可降级的那几类里，
配好的 SocialDataX 一次都没被尝试：五个半小时里到期的每一行都是「刷新失败」。

分类已经改了（`providers._tikhub_error`：监控端点的 400 换下一家试）。可 400 一旦能
换通道，TikHub 对**某一行**回 400（参数形态它不认、短链它跟不动）也会去 SocialDataX
花一次钱。备胎比 TikHub 贵（小红书贵约四成，抖音贵十几倍），小红书那边还不判置顶、
不扫蓝词——所以按需求方的决定：**平时关着，TikHub 出事时运营在面板上打开**。

## 「关」关的是什么

只关**当备胎**的那一份：某个平台上，排在 SocialDataX 前面、而且配了 Key 的通道还在，
就把 SocialDataX 从这个平台的顺序里拿掉。只配了 SocialDataX 一家（或者 CHANNEL_ORDER
把它排第一）的部署，它在那个平台上是主通道，开关不碰它——不然「关备胎」就是关掉整个巡查。

关着的代价，面板上照实说：

* TikHub 发不出请求的那几轮，到期的行直接「刷新失败」
* 展不开的抖音短链只有 SocialDataX 能接，关着时这些行也是「刷新失败」
  （诊断信息里写着：贴含数字 ID 的完整链接，或者打开备用通道）

## 存在哪、谁读

注册表 base 的「面板设置」表，键 `channels.standby`，值 `{"enabled": true, "at": 毫秒}`。
面板写；cron 每一轮开跑时读一次（cron 每轮都是新进程，没有缓存过期的问题）。
没有这一行 = 没人动过 = 关。读不到（没配注册表、飞书抖了）也按关，日志里说一句。

环境变量 `SOCIALDATAX_STANDBY=on|off` 可以钉死它，面板上的按钮随之变灰。
给没有注册表的部署用；不设（或设成 `panel`）= 听面板的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from . import providers
from .config import Channels

# 「面板设置」表里的键（panel_settings 那张表，见那里的键清单）。
KEY = "channels.standby"
ENV = "SOCIALDATAX_STANDBY"
# 开关管的就是这一家。写成常量而不是到处写字面量：哪天备胎换人，改这一处。
NAME = providers.SOCIALDATAX

SOURCE_DEFAULT = "default"     # 没人动过
SOURCE_PANEL = "panel"         # 面板上点的
SOURCE_ENV = "env"             # 环境变量钉死的

_ON = {"1", "on", "true", "yes", "开"}
_OFF = {"0", "off", "false", "no", "关"}


@dataclass(frozen=True)
class State:
    enabled: bool = False
    # 最近一次在面板上改它的时刻（毫秒）。没人动过、或钉在环境变量里 = None。
    changed_at_ms: Optional[int] = None
    source: str = SOURCE_DEFAULT
    # 读不到时的原因。读不到一律按关——这时 enabled 一定是 False。
    error: str = ""

    @property
    def locked(self) -> bool:
        """钉在环境变量里：面板上改不了。"""
        return self.source == SOURCE_ENV

    def describe(self) -> str:
        """日志里那一句。"""
        if self.locked:
            return f"{'开' if self.enabled else '关'}（环境变量 {ENV} 钉死）"
        if self.error:
            return f"关（读不到面板上的开关：{self.error}）"
        if self.source == SOURCE_DEFAULT:
            return "关（默认；面板「余额」那一栏可以打开）"
        return "开（面板上打开的）" if self.enabled else "关（面板上关的）"


def env_override(environ: Mapping[str, str]) -> Optional[bool]:
    """`SOCIALDATAX_STANDBY`：None = 听面板的。写了认不出的值就报错，不猜。"""
    raw = (environ.get(ENV) or "").strip().lower()
    if raw in ("", "panel", "auto"):
        return None
    if raw in _ON:
        return True
    if raw in _OFF:
        return False
    raise ValueError(f"{ENV}={raw!r} 认不出：写 on / off，或者不设（听面板的）")


def from_stored(value: Any) -> State:
    """面板设置里存的那份 → State。没有、坏了 → 默认的关。

    只认 `enabled is True`：存成字符串 "false"、数字 1 之类人手改坏的值一律按关——
    这个开关的安全方向是关（关着最多是刷新失败，开着是在花钱）。
    """
    if not isinstance(value, dict):
        return State()
    at = value.get("at")
    return State(enabled=value.get("enabled") is True,
                 changed_at_ms=at if isinstance(at, int) and not isinstance(at, bool) else None,
                 source=SOURCE_PANEL)


def from_text(text: Optional[str]) -> State:
    """`SettingsStore.read_one` 读回来的原文 → State。"""
    if text is None or not text.strip():
        return State()
    try:
        return from_stored(json.loads(text))
    except ValueError:
        return State()


def stored_value(enabled: bool, now_ms: int) -> dict:
    return {"enabled": bool(enabled), "at": int(now_ms)}


def resolve(environ: Mapping[str, str], read_text) -> State:
    """这一轮按什么来：环境变量钉死的优先，否则读面板设置。

    `read_text()` 返回那一格的原文（没有那一行 = None），出错就抛——这里接住，
    按关处理并带上原因。读不到绝不当成开：开是在花钱。
    """
    forced = env_override(environ)
    if forced is not None:
        return State(enabled=forced, source=SOURCE_ENV)
    try:
        return from_text(read_text())
    except Exception as exc:                                    # noqa: BLE001
        return State(error=(str(exc) or type(exc).__name__)[:200])


def keys_of(api_keys: Any) -> dict[str, str]:
    """{供应商: Key}，和 runner 认的是同一份（providers.credentials）。

    面板经由这里拿，而不是自己 import providers：面板不许持有会发请求那一层的引用
    （tests/test_panel.py 守着）。这一步只是整理字典，不发任何请求。
    """
    return providers.credentials(api_keys)


def standby_platforms(channels: Channels, keys: Optional[Mapping[str, str]]) -> list[str]:
    """SocialDataX 在哪几个平台上是**备胎**：前面还有一家配了 Key 的通道。

    开关只管这几个平台。一个都没有（没配它的 Key、或者它在每个平台上都是主通道），
    cron 就不读开关，面板上也不摆一个按了没用的开关。

    `keys=None` = 不知道配了哪些 Key，按通道顺序里写的都配了算。给面板用：
    文档建议面板服务上不配上游 Key，它看不到 cron 那边的 Key，但开关照样要摆出来。
    """
    def keyed(name: str) -> bool:
        return keys is None or bool(keys.get(name))

    out = []
    for platform, names in channels.order.items():
        if NAME not in names or not keyed(NAME):
            continue
        ahead = names[:names.index(NAME)]
        if any(keyed(n) for n in ahead):
            out.append(platform)
    return out


def apply(channels: Channels, keys: Mapping[str, str], enabled: bool) -> Channels:
    """按开关出这一轮真正用的通道顺序。开着原样返回；关着把**当备胎**的那一份拿掉。

    返回新的 Channels，不改传进来的那份（面板进程里它是共享的）。
    """
    if enabled:
        return channels
    drop = set(standby_platforms(channels, keys))
    if not drop:
        return channels
    return Channels(order={
        platform: [n for n in names if not (platform in drop and n == NAME)]
        for platform, names in channels.order.items()})


@dataclass(frozen=True)
class View:
    """面板上那张开关卡片要的全部东西。面板（panel.Standby）组好交给页面渲染。"""

    state: State
    # SocialDataX 在哪几个平台上是备胎（平台代号）。空 = 开关管不着，卡片不出。
    platforms: tuple = ()
    # 非空 = 面板上改不了，写着为什么（钉在环境变量里、没配注册表）。
    why_locked: str = ""
    # 面板服务自己看得到 SocialDataX 的 Key。看不到（文档建议面板上不配上游 Key）时
    # 卡片上提一句：开关存得下，但 cron 那边没配这个 Key 的话开了也没用。
    key_seen: bool = True

    @property
    def relevant(self) -> bool:
        return bool(self.platforms)

    @property
    def writable(self) -> bool:
        return self.relevant and not self.why_locked
