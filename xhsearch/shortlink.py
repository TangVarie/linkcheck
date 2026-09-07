"""抖音短链展开：v.douyin.com/xxx → aweme_id。**不花钱**，只跟一次 302。

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
    """一次展开的结果。aweme_id 为 None 时 reason 说清为什么。"""

    aweme_id: Optional[str]
    reason: str = ""
    # 实际发出的 HTTP 请求数。免费，但要如实计入 MAX_CALLS_PER_RUN——
    # 那道闸记的是「发出去的请求」，不是「花了钱的请求」。
    requests: int = 0
    # 最后一跳的地址，给诊断信息看「到底跳到了哪」。
    final_url: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.aweme_id)


Fetch = Callable[[str, dict[str, str], float], transport.Response]


def expand_douyin(url: str, *, fetch: Optional[Fetch] = None,
                  timeout: float = TIMEOUT_SECONDS, max_hops: int = MAX_HOPS) -> Expansion:
    """把一条抖音链接展开到能抠出 aweme_id 为止。纯函数式：不缓存、不抛异常。

    fetch 默认是 transport.get_no_redirect；测试注入替身。
    """
    get = fetch or transport.get_no_redirect
    current = (url or "").strip()
    if not current:
        return Expansion(None, "空链接")
    # 链接本身已经带 ID：不发请求。调用方一般不会走到这里，但别为此发一次网络。
    found = links.douyin_id_in_url(current)
    if found:
        return Expansion(found, final_url=current)
    if not links.is_douyin_url(current):
        return Expansion(None, f"不是抖音域名，不展开：{current[:120]}")

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
        if not links.is_douyin_url(target):
            # 跳出了抖音域名：不跟。既是安全边界（不敲陌生主机），也是
            # 业务信号（正常的分享链接不会跳去别处）。
            return Expansion(None, f"短链跳到了非抖音域名，不跟：{target[:120]}",
                             requests=made, final_url=target)
        found = links.douyin_id_in_url(target)
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
            result = expand_douyin(key, fetch=self._fetch, timeout=self._timeout)
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
