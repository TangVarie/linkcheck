"""HTTP 传输层。整个项目只有这里和 feishu.py 会碰网络。

刻意只用标准库：不引第三方依赖，独立服务、云函数、本地定时任务都能直接跑。

扣子代码节点是例外——它禁用 http.client（urllib 底层就是它），只提供
requests_async。要在扣子里跑，把下面的 post() 换成 coze/ 目录里的异步版本即可，
其余模块一行都不用改。
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Response:
    status: int
    content_type: str
    body: str
    # 找厂商排查问题时唯一的凭据，每次都要记进日志。
    request_id: str = ""

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


def _read(resp: Any) -> Response:
    raw = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass  # 响应头声称 gzip 但 body 不是（或被截断）：按原样解码，交给上层解析层报错
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:  # 服务端乱报 charset 名
        body = raw.decode("utf-8", errors="replace")
    return Response(
        status=getattr(resp, "status", None) or getattr(resp, "code", 0),
        content_type=resp.headers.get("Content-Type", ""),
        body=body,
        request_id=resp.headers.get("x-request-id", "") or "",
    )


def request(method: str, url: str, headers: dict[str, str], body: str = "",
            timeout: float = 30.0) -> Response:
    """按方法名分发。双通道之后两家的动词不一样：
    SocialDataX 是 POST + JSON body，TikHub 是 GET + query string。"""
    if (method or "POST").upper() == "GET":
        return get(url, headers, timeout=timeout)
    return post(url, headers, body, timeout=timeout)


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
        if should_retry is not None and not should_retry(last):
            return last
        if attempt == attempts - 1:
            break
        delay = base_delay * (2 ** attempt)
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

    deadline 是 time.monotonic() 的绝对时刻。到点就不再重试——在扣子代码节点
    这种有 60 秒硬上限的地方，宁可把这一条留给下一轮，也不能让整个节点超时，
    因为超时会把已经跑完的几十条结果一起丢掉。
    """
    return request_with_retry(
        "POST", url, headers, body,
        timeout=timeout, attempts=attempts, base_delay=base_delay,
        should_retry=should_retry, deadline=deadline, sleep=sleep,
    )
