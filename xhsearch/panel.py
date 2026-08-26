"""监控面板：一个只读的常驻 HTTP 服务。

**这个进程一个付费请求都不发。** 它只做三件事：读飞书表、聚合、渲染。
巡检仍然只有那一个 Railway cron service 在跑——`xhsearch/runlock.py` 是
文件锁，跨容器挡不住第二个付费执行者，所以面板从设计上就不许成为执行者。
代码层面的保证：这个模块不 import 供应商那一层，也不调用任何会发付费请求的
函数——有一条测试按 AST 逐个节点检查，不是靠字符串搜。

## 为什么敢用标准库的 http.server

Python 文档自己写着「not recommended for production, it only implements
basic security checks」。用它是为了守住这个项目的零依赖，代价逐条堵掉：

* **不用 `SimpleHTTPRequestHandler`**——它会跟符号链接、能读到目录外的文件。
  面板不从磁盘 serve 任何东西，HTML 在内存里拼好写出去。
* **响应头全是写死的常量**——`send_header()` 不校验 CRLF，任何把用户输入
  放进响应头的路径都是头注入，所以干脆没有这种路径。
* **请求体卡上限**（`MAX_BODY_BYTES`）——它没有内置上限，不卡就是一个
  「发一个超大 body 把内存打爆」的洞。
* **不开 HTTP/1.1 长连接**——保持默认的 HTTP/1.0，每个响应后关连接。
  少一点吞吐，换掉一整类 keep-alive 上的请求走私/长度失配问题。
* **socket 有超时**，线程是 daemon，进程不会被半开连接拖住。

再加两层是环境给的：Railway 的边缘做 TLS 终止和一遍 HTTP 解析，面板不直接
面对裸 TCP；以及除 `/healthz` 外所有路由都要口令。

要给更大范围的人用，正确做法是前面放 nginx/Caddy——那会打破零依赖，
所以现在不做，但这个取舍写在这里，不是等出事了才发现。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from . import feishu, railway, schema, summary
from .config import Settings

# 请求体上限。面板的 POST 只有登录表单，几十字节；64 KiB 是三个数量级的余量。
MAX_BODY_BYTES = 64 * 1024
# 读一个请求最多等这么久。半开连接不该占着一个线程不放。
SOCKET_TIMEOUT_SECONDS = 30.0
# 会话有效期。面板是内部工具，一天一次登录不算负担。
SESSION_TTL_SECONDS = 12 * 3600
# 口令最短长度。**拒绝启动**而不是警告——一个 4 位口令的公网面板，
# 和没有口令的区别只是攻击者要多试几千次。
MIN_PASSWORD_LENGTH = 12
# 登录失败的限速：同一来源在窗口内失败这么多次就先歇着。
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 300


class ConfigError(RuntimeError):
    """配置不对，拒绝启动。"""


@dataclass
class PanelConfig:
    password: str
    secret: bytes
    port: int = 8080
    cache_seconds: float = 60.0
    show_digest: bool = False
    # 飞书租户域名。直达链接靠它拼出来，而面板的价值有一半在那个链接上。
    feishu_base: str = "https://feishu.cn"
    read_only: bool = True
    railway: railway.RailwayConfig = field(
        default_factory=lambda: railway.RailwayConfig(token="", environment_id=""))
    # 送到浏览器的日志文本要按这一组逐个脱敏。**别只放上游 Key**：
    # 飞书 Secret、Railway token、面板口令同样可能出现在某条报错话术里。
    secrets: tuple = ()

    @staticmethod
    def from_env(environ: Optional[dict] = None) -> "PanelConfig":
        env = environ if environ is not None else os.environ
        password = (env.get("PANEL_PASSWORD") or "").strip()
        if not password:
            raise ConfigError(
                "没设 PANEL_PASSWORD，拒绝启动。这个服务会把所有项目的监控数据"
                "放到公网上，不能默认裸奔。设一个长口令再来（参考 .env.example）")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ConfigError(
                f"PANEL_PASSWORD 只有 {len(password)} 个字符，至少要 "
                f"{MIN_PASSWORD_LENGTH} 个。公网上的短口令等于没有口令")
        raw_secret = (env.get("PANEL_SECRET") or "").strip()
        # 不填就每次启动随机生成：重启即掉线，比一个默认密钥好得多——
        # 默认密钥会被写进文档、被复制到每一个部署里，等于人人都能伪造会话。
        secret = raw_secret.encode("utf-8") if raw_secret else secrets.token_bytes(32)
        return PanelConfig(
            password=password,
            secret=secret,
            port=_int_env(env, "PORT", 8080, 1, 65535),
            cache_seconds=_float_env(env, "PANEL_CACHE_SECONDS", 60.0, 5.0, 3600.0),
            show_digest=_bool_env(env, "PANEL_SHOW_DIGEST", False),
            feishu_base=_feishu_base(env),
            railway=railway.RailwayConfig.from_env(env),
            secrets=_secrets(env),
        )


def _int_env(env, name, default, minimum, maximum) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 的值 {raw!r} 不是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 的值 {value} 超出 [{minimum}, {maximum}]")
    return value


def _float_env(env, name, default, minimum, maximum) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 的值 {raw!r} 不是数字") from exc
    if value != value or not minimum <= value <= maximum:
        raise ConfigError(f"环境变量 {name} 的值 {raw} 超出 [{minimum}, {maximum}]")
    return value


def _bool_env(env, name, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"环境变量 {name} 的值 {raw!r} 看不懂，填 1/0")


def _secrets(env) -> tuple:
    """所有可能出现在日志里、绝不能送到浏览器的值。

    宁可多列几个：漏一个的代价是把生产密钥贴在一个公网页面上，
    多列一个的代价只是某条日志里多几个星号。
    """
    names = ("TIKHUB_API_KEY", "SOCIALDATAX_API_KEY", "FEISHU_APP_SECRET",
             "FEISHU_APP_ID", "RAILWAY_API_TOKEN", "PANEL_PASSWORD",
             "PANEL_SECRET")
    return tuple(v for v in ((env.get(n) or "").strip() for n in names) if v)


# 直达链接只允许指向飞书自己。这个域名会被渲染进页面上每一个「去这一行」，
# 而它的来源是配置（将来还会是注册表——运营能改的地方）。不过白名单的话，
# 一个改过的域名就是一整屏钓鱼链接，指向的还是「点进去填飞书账号」的场景。
FEISHU_HOSTS = ("feishu.cn", "larksuite.com", "feishu-pre.net")
DEFAULT_FEISHU_BASE = "https://feishu.cn"


def allowed_feishu_base(raw: str) -> str:
    """把一个候选域名收敛成安全的 base，不合格返回空串。

    只放行 https + 飞书自己的域名（含子域）。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower()
    if not any(host == d or host.endswith("." + d) for d in FEISHU_HOSTS):
        return ""
    return f"https://{parsed.netloc}"


def _feishu_base(env) -> str:
    """直达链接的域名。

    优先 `FEISHU_DOMAIN`；没设就从 `FEISHU_TABLES` 里第一条完整网址上扒一个下来
    （多表配置里本来就常常直接粘网址）；都没有就退回 `https://feishu.cn`。
    退回值多半也能跳转，但不保证——所以面板上会提示去设 `FEISHU_DOMAIN`。

    每一个候选都要过 `allowed_feishu_base`：不认识的域名一律丢掉退回默认值，
    而不是「用户填什么就渲染什么」。
    """
    explicit = allowed_feishu_base(env.get("FEISHU_DOMAIN") or "")
    if explicit:
        return explicit
    spec = env.get("FEISHU_TABLES") or ""
    for chunk in spec.replace("；", ";").split(";"):
        if "://" not in chunk:
            continue
        candidate = allowed_feishu_base(chunk[chunk.index("http"):].strip().split("/base")[0].split("/wiki")[0])
        if candidate:
            return candidate
    return DEFAULT_FEISHU_BASE


# ---------- 取数 ----------

def collect(
    tables: list[tuple[str, feishu.Bitable]],
    settings: Settings,
    api_keys: dict[str, str],
    *,
    show_digest: bool = False,
    feishu_base: str = DEFAULT_FEISHU_BASE,
    now: Optional[datetime] = None,
    secrets: Iterable[str] = (),
) -> summary.Overview:
    """把每张表读一遍、聚合成 Overview。**只读，不花钱。**

    一张表出问题只影响它自己那张卡：错误写进 `ProjectSnapshot.error`，
    其余表照常展示。整个面板因为一张表挂掉而空白，是最没用的失败方式。

    `secrets` 是最后一道脱敏：`诊断信息` 那一列存的是上游的错误话术，
    `providers._redact` 在写进表之前已经脱过一次，但那只脱了当时那家的 Key，
    而且是**写入时**的行为——表里可能还留着更早版本写下的值。
    这里是送上公网页面前的最后一次机会，成本只是几次字符串替换。
    """
    now = now or datetime.now(timezone.utc)
    projects: list[summary.ProjectSnapshot] = []
    scrub = (lambda text: railway.redact(text, secrets)) if secrets else None
    for label, table in tables:
        projects.append(_collect_one(
            label, table, settings, api_keys,
            show_digest=show_digest, feishu_base=feishu_base, now=now,
            scrub=scrub))
    return summary.Overview(projects=projects, generated_at=now)


def _collect_one(label, table, settings, api_keys, *,
                 show_digest, feishu_base, now,
                 scrub: Optional[Callable[[str], str]] = None
                 ) -> summary.ProjectSnapshot:
    blank = summary.ProjectSnapshot(
        label=label, app_token=table.app_token, table_id=table.table_id,
        table_url=summary.table_url(feishu_base, table.app_token, table.table_id))
    f = settings.fields
    try:
        meta = table.fields_meta()
    except Exception as exc:                                    # noqa: BLE001
        blank.error = f"读字段元数据失败：{exc}"
        return blank
    if not meta:
        # None（读失败）和空 dict（0 列）同罪，和 doctor 一个口径：
        # 多维表格的主字段不可删，健康的表不可能一列都没有。
        blank.error = (
            "读不到字段列表。多半是应用没被加进这张多维表格："
            "表格右上角「…」→「添加文档应用」，权限给「可编辑」。"
            "若这张表开了「高级权限」，还要在高级权限里给应用「可管理」——"
            "漏这一步的表现是读到空结果而不是报错。")
        return blank

    known = set(meta)
    health = schema.schema_problems(settings, meta)
    # 只请求确实存在的列：按名字请求不存在的列会让整个 search 报 1254045，
    # 一行都读不回来（和 runner.load_rows 同一条纪律）。
    wanted = [c for c in summary.panel_fields(settings, show_digest=show_digest)
              if c in known]
    filter_spec = None
    if f.monitoring in known:
        filter_spec = {"conjunction": "and", "conditions": [
            {"field_name": f.monitoring, "operator": "is", "value": ["true"]}]}
    try:
        records = table.search(wanted, filter_spec=filter_spec)
    except Exception as exc:                                    # noqa: BLE001
        blank.error = f"读记录失败：{exc}"
        blank.health = health
        return blank

    snap = summary.build_snapshot(
        label=label, app_token=table.app_token, table_id=table.table_id,
        records=records, settings=settings, now=now, api_keys=api_keys,
        health=health, feishu_base=feishu_base, show_digest=show_digest,
        scrub=scrub)
    if filter_spec is None:
        snap.health = list(snap.health) + [
            f"表里没有「{f.monitoring}」列，面板无法只统计在管的行，"
            f"下面的数字包含了本该被排除的行"]
    if not records:
        # 飞书在「应用被移出协作者」时是**静默返回 0 行**，不报错。
        # 不单独点名的话，这张表会渲染成一张所有数字都是 0 的健康卡片——
        # 和「这个项目结案了、行都取消巡查了」长得一模一样，
        # 而前者意味着这张表已经完全没在被巡查，没人会发现。
        snap.health = list(snap.health) + [
            f"读到 0 行在管的记录。要么这个项目确实全部取消了「{f.monitoring}」，"
            "要么应用被移出了这张表的协作者——飞书对后者是静默返回空结果、"
            "不报错的。去表格右上角「…」→「添加文档应用」确认一下。"]
    return snap


# ---------- 缓存 ----------

class Cache:
    """TTL 缓存 + 后台单线程刷新。

    **不能改成「每次有人开页面就去拉一遍」**：五个人同时刷新页面就是五倍
    飞书请求，而这一趟要把每张表整表读回来。后台一个线程按 TTL 刷，
    所有人看同一份快照，页面秒开。
    """

    def __init__(self, produce: Callable[[], summary.Overview], ttl: float):
        self._produce = produce
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: Optional[summary.Overview] = None
        self._error: str = ""
        self._fetched_at: float = 0.0
        self._refreshing = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> tuple[Optional[summary.Overview], str, float]:
        with self._lock:
            return self._value, self._error, self._fetched_at

    def refresh(self) -> None:
        """跑一次取数。同一时刻只允许一个在跑——并发刷新只是把飞书请求翻倍。"""
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            value = self._produce()
            with self._lock:
                self._value, self._error, self._fetched_at = value, "", time.time()
        except Exception as exc:                                # noqa: BLE001
            # 取数失败保留上一份快照：一次网络抖动不该让整个面板变空白。
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._refreshing = False

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.refresh()
                self._stop.wait(self._ttl)
        self._thread = threading.Thread(target=loop, name="panel-cache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class LogFeed:
    """Railway 日志的缓存。

    和聚合缓存分开是有意的：飞书那趟慢、Railway 这趟快，失败模式也不一样
    （飞书挂了整个面板没数据，Railway 挂了只是日志那一块看不了）。
    合成一个的话，Railway 一个 401 就能让整个面板变空白。
    """

    def __init__(self, config: PanelConfig, ttl: float,
                 fetch: Optional[Callable[..., list]] = None):
        self._config = config
        self._ttl = ttl
        self._fetch = fetch or railway.fetch_logs
        self._lock = threading.Lock()
        self._lines: list = []
        self._error = ""
        self._fetched_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._config.railway.enabled

    def _missing_hint(self) -> str:
        gaps = self._config.railway.missing()
        return ("没接 Railway 日志：还差 " + "、".join(gaps) +
                "。设上就能在这里看到每一轮的真实输出（见 docs/面板.md）")

    def get(self, *, force: bool = False) -> tuple[list, str]:
        if not self.enabled:
            return [], self._missing_hint()
        with self._lock:
            fresh = (time.time() - self._fetched_at) < self._ttl
            if fresh and not force and (self._lines or self._error):
                return list(self._lines), self._error
        try:
            lines = self._fetch(self._config.railway,
                                secrets=self._config.secrets)
            with self._lock:
                self._lines, self._error = lines, ""
                self._fetched_at = time.time()
        except railway.RailwayError as exc:
            # 保留上一批：日志少一次刷新不该让页面上那一块变空白。
            with self._lock:
                self._error = str(exc)
                self._fetched_at = time.time()
        except Exception as exc:                                # noqa: BLE001
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._fetched_at = time.time()
        with self._lock:
            return list(self._lines), self._error


# ---------- 鉴权 ----------

def issue_session(config: PanelConfig, *, now: Optional[float] = None) -> str:
    # `now if now is not None else ...`，不是 `now or ...`：
    # now=0（epoch）是合法值，用 or 会把它当成「没传」而悄悄取当前时间。
    expiry = int(_clock(now) + SESSION_TTL_SECONDS)
    return f"{expiry}.{_sign(config.secret, expiry)}"


def valid_session(config: PanelConfig, token: str, *,
                  now: Optional[float] = None) -> bool:
    if not token or "." not in token:
        return False
    raw_expiry, _, signature = token.partition(".")
    try:
        expiry = int(raw_expiry)
    except ValueError:
        return False
    # 先验签名再看过期：反过来的话，一个过期时间字段就成了不需要签名也能
    # 走到的分支，而签名比较是这里唯一真正拦人的东西。
    if not hmac.compare_digest(signature, _sign(config.secret, expiry)):
        return False
    return expiry > _clock(now)


def _clock(now: Optional[float]) -> float:
    return time.time() if now is None else now


def csrf_token(config: PanelConfig, session: str) -> str:
    """由会话派生出来的 CSRF token，**故意不等于会话本身**。

    它要放进页面的 DOM 里供前端 JS 读，而会话 Cookie 是 HttpOnly 的——
    把会话原值塞进 DOM 等于白设 HttpOnly：一次 XSS 就能把它抄走、
    带到任何一台机器上重放。派生值被抄走只能在这个源里发请求，
    而那本来就是 XSS 已经能做的事。
    """
    if not session:
        return ""
    return hmac.new(config.secret, b"csrf:" + session.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _sign(secret: bytes, expiry: int) -> str:
    return hmac.new(secret, str(expiry).encode("ascii"), hashlib.sha256).hexdigest()


def check_password(config: PanelConfig, attempt: str) -> bool:
    """常量时间比较。用 `==` 的话，响应时间会逐字符泄露口令。"""
    return hmac.compare_digest(attempt.encode("utf-8"),
                               config.password.encode("utf-8"))


class LoginThrottle:
    """登录失败限速。按来源分桶，不做全局锁定——全局锁定意味着任何人都能
    让整个团队登不进来。"""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES,
                 window: float = LOGIN_WINDOW_SECONDS):
        self._max = max_failures
        self._window = window
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, *, now: Optional[float] = None) -> bool:
        now = _clock(now)
        with self._lock:
            hits = [t for t in self._buckets.get(key, []) if now - t < self._window]
            self._buckets[key] = hits
            return len(hits) >= self._max

    def record_failure(self, key: str, *, now: Optional[float] = None) -> None:
        now = _clock(now)
        with self._lock:
            hits = [t for t in self._buckets.get(key, []) if now - t < self._window]
            hits.append(now)
            self._buckets[key] = hits
            # 桶数有上界：伪造 X-Forwarded-For 就能无限撑大这个 dict，
            # 那是把限速本身变成内存耗尽的洞。
            if len(self._buckets) > 4096:
                self._buckets = {key: hits}

    def clear(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)


# ---------- HTTP ----------

COOKIE_NAME = "panel_session"
# 非 GET 请求要带这个头，值是**由会话派生**的 token（不是会话本身，见 csrf_token）。
# SameSite=Strict 已经挡掉了绝大多数 CSRF，但跨站请求带不上自定义头，
# 这一条是不依赖浏览器 SameSite 实现的第二道。
CSRF_HEADER = "X-Panel-Token"

_SAFE_LOG = {chr(c) for c in range(32)} | {"\x7f"}


def _sanitize_for_log(text: str) -> str:
    """日志行里的控制字符全部换掉。

    Python 3.12 才在 BaseHTTPRequestHandler 里做这件事，而这个项目钉在 3.11——
    不自己做的话，远端可以往你的终端里塞 ANSI 转义序列。
    """
    return "".join("?" if ch in _SAFE_LOG else ch for ch in text)[:200]


@dataclass
class _Deps:
    """handler 要用的东西。挂在 server 上，不用全局变量——
    测试要能起两个配置不同的实例。"""

    config: PanelConfig
    cache: Cache
    logs: Optional["LogFeed"] = None
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    render_login: Callable[[str], str] = lambda message: ""
    render_page: Callable[..., str] = lambda **kw: ""


class PanelHandler(BaseHTTPRequestHandler):
    # 保持 HTTP/1.0：每个响应后关连接，换掉一整类 keep-alive 上的
    # 请求走私/长度失配问题。代价只是多几个 TCP 连接。
    protocol_version = "HTTP/1.0"
    server_version = "linkcheck-panel"
    sys_version = ""
    timeout = SOCKET_TIMEOUT_SECONDS

    @property
    def deps(self) -> _Deps:
        return self.server.deps                                 # type: ignore[attr-defined]

    # —— 日志 ——
    def log_message(self, fmt, *args):                          # noqa: A003
        line = _sanitize_for_log(fmt % args)
        print(f"[panel] {self.address_string()} {line}", flush=True)

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)

    # —— 路由 ——
    def do_GET(self):                                           # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            # 免鉴权：Railway 要拿它判活，而健康检查不该需要口令。
            # 它只回一个字面量，不泄露任何东西。
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        if path == "/":
            if not self._authed():
                return self._send_html(200, self.deps.render_login(""))
            return self._send_html(200, self._page())
        if path == "/api/overview":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._send_json(200, self._overview_payload())
        if path == "/api/logs":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._send_json(200, self._logs_payload())
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):                                          # noqa: N802
        path = urlparse(self.path).path
        if path == "/login":
            return self._do_login()
        if not self._authed():
            return self._send_json(401, {"error": "未登录"})
        # 写路径（现在只有 logout 和强制刷新）必须带自定义头，
        # 跨站请求带不上它。GET 全是只读的，所以不管。
        if path != "/logout" and not self._csrf_ok():
            return self._send_json(403, {"error": "缺少 " + CSRF_HEADER})
        if path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}=; Max-Age=0; {self._cookie_flags()}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/api/refresh":
            # 只是让缓存立刻重取一遍飞书数据和 Railway 日志。
            # **不发任何付费请求。**
            self.deps.cache.refresh()
            if self.deps.logs is not None:
                self.deps.logs.get(force=True)
            return self._send_json(200, self._overview_payload())
        return self._send_json(404, {"error": "not found"})

    # —— 登录 ——
    def _do_login(self):
        source = self._client_key()
        if self.deps.throttle.blocked(source):
            return self._send_html(
                429, self.deps.render_login("试得太频繁了，先歇五分钟再来"))
        body = self._read_body()
        if body is None:
            return None
        password = (parse_qs(body.decode("utf-8", "replace")).get("password") or [""])[0]
        if not check_password(self.deps.config, password):
            self.deps.throttle.record_failure(source)
            self.log_message("登录失败")
            return self._send_html(401, self.deps.render_login("口令不对"))
        self.deps.throttle.clear(source)
        token = issue_session(self.deps.config)
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NAME}={token}; Max-Age={SESSION_TTL_SECONDS}; "
            f"{self._cookie_flags()}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    def _cookie_flags(self) -> str:
        """会话 Cookie 的属性。

        `Secure` 在生产上必须有：Railway 的边缘是 HTTPS，没有它 Cookie 会
        在任何一次降级到明文的请求里裸奔。但本地开发是 `http://localhost:8080`，
        带了 `Secure` 就永远登不进去。

        所以只在**能确定是明文的 localhost** 时才去掉它——判断不出来就照加，
        失败方向是「更严」而不是「更松」。
        """
        proto = (self.headers.get("X-Forwarded-Proto") or "").strip().lower()
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        local = host in ("localhost", "127.0.0.1", "::1", "[::1]")
        plain = proto in ("", "http")
        secure = "" if (local and plain) else " Secure;"
        return f"Path=/;{secure} HttpOnly; SameSite=Strict"

    def _client_key(self) -> str:
        """限速的分桶键。Railway 的边缘会带 X-Forwarded-For，取第一跳。

        它是可以伪造的，所以桶数有上界（见 LoginThrottle）——
        伪造只能绕过限速，不能把限速变成内存耗尽。
        """
        forwarded = self.headers.get("X-Forwarded-For") or ""
        first = forwarded.split(",")[0].strip()
        return (first or self.client_address[0] or "?")[:64]

    # —— 会话 ——
    def _authed(self) -> bool:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return valid_session(self.deps.config, value.strip())
        return False

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value.strip()
        return ""

    def _csrf_ok(self) -> bool:
        session = self._session_token()
        if not session:
            return False
        sent = (self.headers.get(CSRF_HEADER) or "").strip()
        return bool(sent) and hmac.compare_digest(
            sent, csrf_token(self.deps.config, session))

    # —— 请求体 ——
    def _read_body(self) -> Optional[bytes]:
        raw = self.headers.get("Content-Length")
        if raw is None:
            self._send(411, b"need Content-Length", "text/plain; charset=utf-8")
            return None
        try:
            length = int(raw)
        except ValueError:
            self._send(400, b"bad Content-Length", "text/plain; charset=utf-8")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            # 先拒再读。读了再判等于已经把内存吃进去了。
            self._send(413, b"body too large", "text/plain; charset=utf-8")
            return None
        return self.rfile.read(length)

    # —— 响应 ——
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        # 响应头全部是写死的常量：send_header 不校验 CRLF，
        # 任何把用户输入放进头里的路径都是头注入。
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # —— 页面数据 ——
    def _page(self) -> str:
        overview, error, fetched_at = self.deps.cache.snapshot()
        runs, log_error = self._runs()
        return self.deps.render_page(
            overview=overview, error=error, fetched_at=fetched_at,
            config=self.deps.config, runs=runs, log_error=log_error,
            csrf=csrf_token(self.deps.config, self._session_token()))

    def _runs(self):
        if self.deps.logs is None:
            return [], ""
        lines, error = self.deps.logs.get()
        return railway.build_runs(lines), error

    def _logs_payload(self) -> dict:
        if self.deps.logs is None:
            return {"enabled": False, "error": "", "lines": [], "runs": []}
        lines, error = self.deps.logs.get()
        return {
            "enabled": self.deps.logs.enabled,
            "error": error,
            # 原始日志只回最近这些行：整批几千行塞进一个 JSON 响应，
            # 浏览器渲染比取数还慢。
            "lines": [{"timestamp": l.timestamp, "severity": l.severity,
                       "message": l.message} for l in lines[:300]],
            "runs": [_run_json(r) for r in railway.build_runs(lines)],
        }

    def _overview_payload(self) -> dict:
        overview, error, fetched_at = self.deps.cache.snapshot()
        return {
            "error": error,
            "fetched_at": fetched_at,
            "projects": [_project_json(p) for p in (overview.projects if overview else [])],
            "todos": [_todo_json(t) for t in (overview.todos() if overview else [])],
        }


def _run_json(run) -> dict:
    return {
        "run_id": run.run_id, "mode": run.mode,
        "started_at": run.started_at, "ended_at": run.ended_at,
        "exit_code": run.exit_code, "error": run.error,
        "rows": run.rows, "cost_yuan": round(run.cost_yuan, 4),
        "finished": run.finished, "ok": run.ok,
        "breaker_tripped": run.breaker_tripped, "failovers": run.failovers,
        "channels_dead": run.channels_dead, "stopped": run.stopped,
        "budget_stopped": run.budget_stopped,
        "tables": [{"label": t.label, "rows": t.rows,
                    "cost_yuan": round(t.cost_yuan, 4), "counts": t.counts,
                    "breaker_tripped": t.breaker_tripped,
                    "failovers": t.failovers} for t in run.tables],
    }


def _project_json(p: summary.ProjectSnapshot) -> dict:
    return {
        "label": p.label, "table_url": p.table_url, "error": p.error,
        "health": p.health, "total_rows": p.total_rows,
        "archived_rows": p.archived_rows, "queued_rows": p.queued_rows,
        "due_rows": p.due_rows, "due_yuan": round(p.due_yuan, 2),
        "stale_rows": p.stale_rows, "never_checked_rows": p.never_checked_rows,
        "oldest_checked_ms": p.oldest_checked_ms,
        "refresh_status_counts": p.refresh_status_counts,
        "traffic_tag_counts": p.traffic_tag_counts,
        "negative_rows": p.negative_rows, "pin_lost_rows": p.pin_lost_rows,
        "seed_keyword_rows": p.seed_keyword_rows,
        "negative_keyword_rows": p.negative_keyword_rows,
        "needs_attention": p.needs_attention,
    }


def _todo_json(t: summary.TodoRow) -> dict:
    return {
        "record_id": t.record_id, "project": t.project, "record_url": t.record_url,
        "link_cell": t.link_cell, "reasons": t.reasons,
        "refresh_status": t.refresh_status, "diagnosis": t.diagnosis,
        "comment_count": t.comment_count, "traffic_tags": t.traffic_tags,
        "seed_keywords": t.seed_keywords, "negative_keywords": t.negative_keywords,
        "checked_at_ms": t.checked_at_ms,
        "digest": t.digest, "negative_digest": t.negative_digest,
    }


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    # 重启时端口还在 TIME_WAIT 就起不来，容器里每次部署都会撞上。
    allow_reuse_address = True

    def __init__(self, address, deps: _Deps):
        self.deps = deps
        super().__init__(address, PanelHandler)


def build_server(config: PanelConfig, cache: Cache, *, logs: Optional[LogFeed] = None,
                 render_login=None, render_page=None,
                 host: str = "0.0.0.0") -> PanelServer:
    from . import panel_view
    deps = _Deps(
        config=config, cache=cache, logs=logs,
        render_login=render_login or panel_view.login_page,
        render_page=render_page or panel_view.overview_page,
    )
    return PanelServer((host, config.port), deps)


def serve(config: PanelConfig, produce: Callable[[], summary.Overview]) -> int:
    """起面板。阻塞到进程被杀。"""
    cache = Cache(produce, config.cache_seconds)
    logs = LogFeed(config, config.cache_seconds)
    server = build_server(config, cache, logs=logs)
    cache.start()
    print(f"面板已启动：0.0.0.0:{config.port}"
          f"（缓存 {config.cache_seconds:.0f} 秒刷一次，"
          f"评论正文{'展示' if config.show_digest else '不展示'}）", flush=True)
    if logs.enabled:
        print("Railway 日志：已接入", flush=True)
    else:
        print(f"Railway 日志：未接入（差 "
              f"{'、'.join(config.railway.missing())}）", flush=True)
    print("⚠ 这个进程不发任何付费请求：要刷新就在飞书表里勾「排队刷新」，"
          "由 cron 那个服务处理", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n收到中断，关闭面板", flush=True)
    finally:
        cache.stop()
        server.server_close()
    return 0
