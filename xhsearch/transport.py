"""HTTP 传输层。整个项目只有这里和 feishu.py 会碰网络。

刻意只用标准库：不引第三方依赖，独立服务、云函数、本地定时任务都能直接跑。
换运行时只需要换这一层，其余模块一行都不用改。

三条硬约束写在这里，别的地方不重复实现：

1. **永远返回 Response，不抛异常**（见 _perform）。
2. **响应体有上限**：压缩前 MAX_COMPRESSED_BYTES、解压后 MAX_BODY_BYTES。
   上游或代理返回一个超大 body / gzip 炸弹时，进程会 OOM 而不是失败——
   OOM 会把本轮已经付费刷完、还没写回的结果全部丢掉，这是最贵的一种崩溃。
3. **重试用 full jitter**：固定的 2/4 秒退避在多实例同时撞上 429/5xx 时
   会形成惊群，重试本身把限流拖得更久。
"""

from __future__ import annotations

import email.utils
import json
import random
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any, Optional

# 解压后的 body 上限。真实响应里最大的是小红书评论页（实测几十 KB），
# 8 MiB 已经是三个数量级的余量；超过它一定是上游/网关出了问题。
MAX_BODY_BYTES = 8 * 1024 * 1024
# 压缩体上限。gzip 炸弹的特征就是压缩体很小、解压后极大，所以两道闸都要有。
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024

_CHUNK = 64 * 1024

# 服务端 Retry-After 的可信区间。上游给一个 3600 秒会把整轮拖死，
# 给负数/NaN 会让 time.sleep 直接抛异常。
MAX_RETRY_AFTER_SECONDS = 60.0


@dataclass
class Response:
    status: int
    content_type: str
    body: str
    # 找厂商排查问题时唯一的凭据，每次都要记进日志。
    request_id: str = ""
    # 服务端 Retry-After 响应头（秒）。429/503 时按它等待，比我们自己拍的
    # 退避准得多；已经 clamp 到 [0, MAX_RETRY_AFTER_SECONDS]。
    retry_after: Optional[float] = None
    # 响应体超过上限被截断。这种失败重试没有意义（再来一次还是那么大），
    # request_with_retry 会直接返回它。
    oversized: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return None


def post(
    url: str,
    headers: dict[str, str],
    body: str,
    timeout: float = 30.0,
) -> Response:
    """发一个 POST，永远返回 Response，不因 4xx/5xx 抛异常。

    这一点很重要：SocialDataX 的鉴权错误和飞书的业务错误都把有用信息放在
    非 2xx 的 body 里，urllib 默认会把它包成 HTTPError 抛出去，直接 raise
    就把诊断信息丢了。
    """
    data = body.encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return _perform(request, timeout)


def get(url: str, headers: dict[str, str], timeout: float = 30.0) -> Response:
    """发一个 GET，语义与 post() 一致：非 2xx 也返回 Response 而不是抛异常。"""
    request = urllib.request.Request(url, headers=headers, method="GET")
    return _perform(request, timeout)


def _perform(request: urllib.request.Request, timeout: float) -> Response:
    """「永远返回 Response」的承诺在这里兑现。

    urllib 只把**建连阶段**的 OSError 包成 URLError；状态行前被断开
    （RemoteDisconnected）、读响应体中途连接被重置（ConnectionResetError /
    IncompleteRead）、坏 gzip、服务端乱报 charset——这些都在读取阶段裸抛。
    漏掉任何一个，一次网络毛刺就会炸穿整批：write_back 执行不到，
    本轮已经花钱刷完的行一条都不写回。所以最后必须有 Exception 兜底。
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return _read(resp)
    except urllib.error.HTTPError as exc:
        try:
            return _read(exc)
        except Exception as read_exc:  # noqa: BLE001 —— 错误响应的 body 读不出来，至少保住状态码
            return Response(getattr(exc, "code", 0) or 0, "",
                            f"读取错误响应失败：{type(read_exc).__name__}: {read_exc}")
    except urllib.error.URLError as exc:
        return Response(0, "", f"网络错误：{exc.reason}")
    except TimeoutError:
        return Response(0, "", f"请求超时（{timeout}s）")
    except Exception as exc:  # noqa: BLE001
        return Response(0, "", f"网络错误：{type(exc).__name__}: {exc}")


def _read_capped(resp: Any, limit: int) -> tuple[bytes, bool]:
    """分块读，最多读 limit 字节。返回（内容, 是否超限）。

    分块而不是一次 read()：一次读会先把整个 body 分配进内存，
    「读完再检查大小」对 OOM 毫无帮助——检查那行代码根本执行不到。

    有些响应对象（测试替身、老式的 file-like）read() 不接受长度参数，
    这时退回一次性读取再事后判定：至少大小语义仍然成立。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = resp.read(_CHUNK)
        except TypeError:
            raw = resp.read() or b""
            return (raw[:limit], True) if len(raw) > limit else (raw, False)
        if not chunk:
            return b"".join(chunks), False
        total += len(chunk)
        if total > limit:
            chunks.append(chunk[: max(0, limit - (total - len(chunk)))])
            return b"".join(chunks), True
        chunks.append(chunk)


def _gunzip_capped(raw: bytes, limit: int) -> tuple[bytes, bool]:
    """流式解压并设上限。返回（解压结果, 是否超限）。

    gzip.decompress() 会把整个结果一次性分配出来——压缩比 1000:1 的
    2 MiB 响应能变成 2 GiB，进程当场被 OOM killer 干掉。
    """
    out: list[bytes] = []
    total = 0
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        for start in range(0, len(raw), _CHUNK):
            piece = decompressor.decompress(raw[start : start + _CHUNK], limit - total + 1)
            total += len(piece)
            out.append(piece)
            if total > limit:
                return b"".join(out)[:limit], True
        tail = decompressor.flush()
        total += len(tail)
        out.append(tail)
        if total > limit:
            return b"".join(out)[:limit], True
    except zlib.error:
        # 响应头声称 gzip 但 body 不是（或被截断）：按原样交给上层解析层报错。
        return raw, False
    return b"".join(out), False


def _retry_after(headers: Any) -> Optional[float]:
    """解析 Retry-After 响应头。秒数和 HTTP-date 两种写法都认。

    上游的等待建议比我们自己拍的退避准得多，但也不能全信：
    clamp 到 [0, MAX_RETRY_AFTER_SECONDS]，一个 3600 会把整轮拖死。
    """
    raw = ""
    try:
        raw = (headers.get("Retry-After") or "").strip()
    except Exception:  # noqa: BLE001 —— 响应头对象形状不对时不该炸穿传输层
        return None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        # parsedate_to_datetime 对畸形日期是**抛 ValueError**，不是返回 None。
        # 漏掉这个 except，一个坏响应头就会顺着 _read 炸到 _perform 的兜底里，
        # 把整条响应换成合成的「网络错误」——429 的真实状态码和 body 全丢，
        # 连一条成功响应带了个坏头都会被当成网络故障去重试/降级。
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        try:
            seconds = parsed.timestamp() - time.time()
        except (OverflowError, OSError, ValueError):
            return None
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def _read(resp: Any) -> Response:
    raw, truncated = _read_capped(resp, MAX_COMPRESSED_BYTES)
    oversized = truncated
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        raw, blown = _gunzip_capped(raw, MAX_BODY_BYTES)
        oversized = oversized or blown
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:  # 服务端乱报 charset 名
        body = raw.decode("utf-8", errors="replace")
    if oversized:
        # 明确报成一个可分类的失败，而不是把半截 JSON 交给上层——
        # 半截 body 解析失败的报错完全看不出真正的原因是「响应体过大」。
        return Response(
            0, resp.headers.get("Content-Type", ""),
            f"响应体过大（压缩前上限 {MAX_COMPRESSED_BYTES} 字节、"
            f"解压后上限 {MAX_BODY_BYTES} 字节），已丢弃；"
            f"前 200 字符：{body[:200]}",
            request_id=resp.headers.get("x-request-id", "") or "",
            oversized=True,
        )
    return Response(
        status=getattr(resp, "status", None) or getattr(resp, "code", 0),
        content_type=resp.headers.get("Content-Type", ""),
        body=body,
        request_id=resp.headers.get("x-request-id", "") or "",
        retry_after=_retry_after(resp.headers),
    )


def request(method: str, url: str, headers: dict[str, str], body: str = "",
            timeout: float = 30.0) -> Response:
    """按方法名分发。双通道之后两家的动词不一样：
    SocialDataX 是 POST + JSON body，TikHub 是 GET + query string。"""
    if (method or "POST").upper() == "GET":
        return get(url, headers, timeout=timeout)
    return post(url, headers, body, timeout=timeout)


def backoff_delay(attempt: int, base_delay: float, retry_after: Optional[float] = None) -> float:
    """第 attempt 次重试该等多久（full jitter）。

    full jitter = random.uniform(0, base * 2**attempt)：多个实例同时撞上
    同一次 429/5xx 时不会在同一毫秒一起回来。固定退避在这种场景下
    只是把惊群整体平移了两秒。

    服务端给了 Retry-After 就以它为下界——它比我们拍的数字准，
    但仍然在它之上加一点抖动，避免所有实例卡着同一秒同时醒。
    """
    ceiling = base_delay * (2 ** max(0, attempt))
    if retry_after is not None and retry_after > 0:
        return retry_after + random.uniform(0, min(base_delay, retry_after))
    return random.uniform(0, ceiling)


def request_with_retry(
    method: str,
    url: str,
    headers: dict[str, str],
    body: str = "",
    *,
    timeout: float = 30.0,
    attempts: int = 3,
    base_delay: float = 2.0,
    should_retry=None,
    deadline: Optional[float] = None,
    sleep=None,
) -> Response:
    """带指数退避的请求，GET/POST 通用。语义见 post_with_retry。"""
    last = Response(0, "", "未发起任何请求")
    for attempt in range(attempts):
        if deadline is not None and time.monotonic() >= deadline:
            return Response(0, "", "已到本次运行的软截止，剩余重试留给下一轮")
        last = request(method, url, headers, body, timeout=timeout)
        if last.ok:
            return last
        if last.oversized:
            # 再来一次还是那么大：重试只是白花时间和上游配额。
            return last
        if should_retry is not None and not should_retry(last):
            return last
        if attempt == attempts - 1:
            break
        delay = backoff_delay(attempt, base_delay, last.retry_after)
        if deadline is not None and time.monotonic() + delay >= deadline:
            return last
        # 在调用时才取 time.sleep，而不是绑在默认参数上——绑死了测试就 patch 不掉，
        # 一个重试用例能让整个测试套慢好几秒。
        (sleep or time.sleep)(delay)
    return last


def post_with_retry(
    url: str,
    headers: dict[str, str],
    body: str,
    *,
    timeout: float = 30.0,
    attempts: int = 3,
    base_delay: float = 2.0,
    should_retry=None,
    deadline: Optional[float] = None,
    sleep=None,
) -> Response:
    """带指数退避的 POST。

    SocialDataX 的官方 skill 明文要求：遇到限流不要放弃整批，停止发起新请求，
    按返回的等待时间等待（没给就等 2 秒），然后从未完成的位置继续。base_delay
    默认取 2 秒就是照这个来的。

    deadline 是 time.monotonic() 的绝对时刻。到点就不再重试——在有执行
    时限的运行时里，宁可把这一条留给下一轮，也不能让整轮超时，
    因为超时会把已经跑完的几十条结果一起丢掉。
    """
    return request_with_retry(
        "POST", url, headers, body,
        timeout=timeout, attempts=attempts, base_delay=base_delay,
        should_retry=should_retry, deadline=deadline, sleep=sleep,
    )
