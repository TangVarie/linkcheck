"""短链展开：v.douyin.com/xxx → aweme_id，xhslink.cn/o/xxx → 笔记 ID。
**不花钱**，只跟一次 302。

小红书为什么也要（2026-10）：TikHub 的小红书端点吃分享链接（share_text），
但那样是**它自己**去跟短链——这一步时好时坏：同一条 App 里能正常打开、
之前也刷成功过的短链，有几轮被它回 HTTP 400「请求失败，请重试……本次请求
不会被扣费」，行就成了「刷新失败」。我们自己先跟这一跳（实测一跳就落到
`www.xiaohongshu.com/discovery/item/<笔记ID>?…`），把笔记 ID 交给 TikHub，
就和贴了完整链接的那些行走同一条路。下面说的是抖音，道理和护栏两边一样。

为什么需要它：TikHub 的抖音端点只收数字 aweme_id，不收链接；运营从 App 里
复制出来的分享文案却几乎全是 v.douyin.com 短链。没有这一步，表里的抖音行
实际上是 SocialDataX **单通道**——TikHub 配了也用不上，SocialDataX 积分一空
整个抖音平台就停摆（这正是 2026-09 那次「douyin 平台本轮无可用通道」的成因），
「抖音省 93%」那笔账也一分都没兑现。

怎么做：短链对任何 GET 都回 302，Location 就是落地页
`https://www.iesdouyin.com/share/video/<aweme_id>/?...`（图文是 /share/note/）。
我们只要那个响应头，**不跟过去、不下载落地页**（transport.get_no_redirect）。

三条护栏：

1. **只对抖音自家域名发请求**。每一跳都验 hostname（links.is_douyin_url），
   永远不拿运营贴的链接去敲陌生主机——链接是运营手工录入的，谁都能往表里贴。
2. **失败就退回原路**。展不开（网络抖动、落地页形态变了、短链过期）不报错，
   调用方照旧把 URL 交给吃链接的通道；只配 TikHub 时按「跳过」处理，
   诊断信息里写清是展开失败。一个免费的辅助步骤绝不能让一行判失效。
3. **一轮之内同一条短链只展一次**（Resolver 缓存）。同一篇内容在表里出现
   两行的情况不少见，而展开虽然免费也要几百毫秒。
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import links, transport

# 最多跟几跳。真实链路是 1 跳（v.douyin.com → iesdouyin.com/share/...），
# 偶尔 2 跳；给到 5 只是余量，主要是防止两个短链互相指着转圈。
MAX_HOPS = 5
# 单跳超时。这一步在付费请求之前、且每一行都可能做，不能让一条挂死的
# 短链拖住整轮——展不开就走备胎，代价只是这一行贵一点。
TIMEOUT_SECONDS = 10.0

_HEADERS = {
    # 抖音对无 UA / 明显是脚本的 UA 会回一个「验证」页而不是 302。
    "User-Agent": transport.BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


@dataclass(frozen=True)
class Expansion:
    """一次展开的结果。content_id 为 None 时 reason 说清为什么。"""

    # 抖音是 aweme_id，小红书是笔记 ID。
    content_id: Optional[str]
    reason: str = ""
    # 实际发出的 HTTP 请求数。免费，但要如实计入 MAX_CALLS_PER_RUN——
    # 那道闸记的是「发出去的请求」，不是「花了钱的请求」。
    requests: int = 0
    # 最后一跳的地址，给诊断信息看「到底跳到了哪」。
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.content_id)

    @property
    def aweme_id(self) -> Optional[str]:
        """旧名字（只有抖音那会儿叫这个），tools/probe_channel.py 还在用。"""
        return self.content_id


Fetch = Callable[[str, dict[str, str], float], transport.Response]


@dataclass(frozen=True)
class _Platform:
    name: str
    ours: Callable[[str], bool]
    id_in_url: Callable[[str], Optional[str]]


_DOUYIN = _Platform("抖音", links.is_douyin_url, links.douyin_id_in_url)
_XHS = _Platform("小红书", links.is_xhs_url, links.xhs_id_in_url)


def _with_scheme(url: str) -> str:
    """表里常有不带 https:// 的链接（`xhslink.cn/o/abc`）。不补的话 urlsplit
    取不到 hostname，域名校验一律判「不是我们的」，一跳都不会跟。"""
    if url and "://" not in url:
        return "https://" + url.lstrip("/")
    return url


def expand(url: str, *, fetch: Optional[Fetch] = None,
           timeout: float = TIMEOUT_SECONDS, max_hops: int = MAX_HOPS) -> Expansion:
    """按域名认平台再展开。两边都不是：不发请求，返回失败。"""
    current = _with_scheme((url or "").strip())
    platform = _XHS if links.is_xhs_url(current) else _DOUYIN
    return _expand(url, platform, fetch=fetch, timeout=timeout, max_hops=max_hops)


def expand_douyin(url: str, *, fetch: Optional[Fetch] = None,
                  timeout: float = TIMEOUT_SECONDS, max_hops: int = MAX_HOPS) -> Expansion:
    """把一条抖音链接展开到能抠出 aweme_id 为止。纯函数式：不缓存、不抛异常。

    fetch 默认是 transport.get_no_redirect；测试注入替身。
    """
    return _expand(url, _DOUYIN, fetch=fetch, timeout=timeout, max_hops=max_hops)


def expand_xhs(url: str, *, fetch: Optional[Fetch] = None,
               timeout: float = TIMEOUT_SECONDS, max_hops: int = MAX_HOPS) -> Expansion:
    """把一条小红书链接展开到能抠出笔记 ID 为止。和 expand_douyin 同一套规矩。"""
    return _expand(url, _XHS, fetch=fetch, timeout=timeout, max_hops=max_hops)


def _expand(url: str, platform: _Platform, *, fetch: Optional[Fetch],
            timeout: float, max_hops: int) -> Expansion:
    get = fetch or transport.get_no_redirect
    current = (url or "").strip()
    if not current:
        return Expansion(None, "空链接")
    # 链接本身已经带 ID：不发请求。调用方一般不会走到这里，但别为此发一次网络。
    found = platform.id_in_url(current)
    if found:
        return Expansion(found, final_url=current)
    current = _with_scheme(current)
    if not platform.ours(current):
        return Expansion(None, f"不是{platform.name}域名，不展开：{current[:120]}")

    made = 0
    for _ in range(max(1, max_hops)):
        response = get(current, _HEADERS, timeout)
        made += 1
        if not response.redirected:
            if response.status == 0:
                return Expansion(None, f"短链展开失败（网络）：{response.body[:120]}",
                                 requests=made, final_url=current)
            return Expansion(
                None,
                f"短链没有跳转（HTTP {response.status}），落地页形态可能变了或短链已过期",
                requests=made, final_url=current,
            )
        target = urllib.parse.urljoin(current, response.location)
        if not platform.ours(target):
            # 跳出了自家域名：不跟。既是安全边界（不敲陌生主机），也是
            # 业务信号（正常的分享链接不会跳去别处）。
            return Expansion(None, f"短链跳到了非{platform.name}域名，不跟：{target[:120]}",
                             requests=made, final_url=target)
        found = platform.id_in_url(target)
        if found:
            return Expansion(found, requests=made, final_url=target)
        current = target
    return Expansion(None, f"跟了 {made} 跳仍没抠出作品 ID：{current[:120]}",
                     requests=made, final_url=current)


class Resolver:
    """一轮之内的短链缓存。线程安全——refresh() 的线程池会并发调用。

    deadline 是 time.monotonic() 的绝对时刻：到点就不再发展开请求
    （返回一个「已到软截止」的失败），让调用方按原路径处理。展开本身
    不花钱，但花时间；软截止的意义就是不让任何一步拖过它。
    """

    def __init__(self, *, fetch: Optional[Fetch] = None, timeout: float = TIMEOUT_SECONDS,
                 deadline: Optional[float] = None):
        self._fetch = fetch
        self._timeout = timeout
        self._deadline = deadline
        self._cache: dict[str, Expansion] = {}
        self._lock = threading.Lock()
        # 统计给报表：这一轮展开了几条、失败了几条、发了几个请求。
        self.expanded = 0
        self.failed = 0
        self.requests = 0

    def resolve(self, url: str) -> Expansion:
        key = (url or "").strip()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            # 命中缓存没有发请求：requests 归零，否则第二行会把第一行那一跳
            # 再记一次账，MAX_CALLS_PER_RUN 就虚高了。
            return replace(hit, requests=0)
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return Expansion(None, "已到软截止，未展开短链")
        try:
            result = expand(key, fetch=self._fetch, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 —— 免费的辅助步骤绝不能把一行变成「内部错误」
            result = Expansion(None, f"短链展开异常：{type(exc).__name__}: {exc}"[:200])
        with self._lock:
            # 失败也缓存：同一轮里再遇到同一条短链，再展一次多半还是同一个结果，
            # 而每次都是一个真实请求。下一轮 Resolver 是新的，自然会重试。
            self._cache[key] = result
            self.requests += result.requests
            if result.ok:
                self.expanded += 1
            else:
                self.failed += 1
        return result
