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
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from . import balance as balance_mod
from . import feishu, provision, railway, registry, schema, summary, tablespec

tablespec_BadTarget = tablespec.BadTarget
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
# 显式配的签名密钥至少这么长。猜中它就能自己签一个未来过期的会话 Cookie，
# **完全绕过 PANEL_PASSWORD**——而且伪造 Cookie 这条路根本不经过
# LoginThrottle，连节流都碰不到，可以离线穷举。不填时自动生成的就是
# 32 字节，显式填的没理由比它弱。
MIN_SECRET_LENGTH = 32
# 一次最多勾多少行「排队刷新」。「全选」一次勾几千行 = 下一轮直接顶穿预算。
# 这个上限和 MAX_RECORDS_PER_RUN 是两回事：那个是 cron 那边的，
# 这个是防手滑的——面板上一个「全选」比误改一列容易得多。
MAX_QUEUE_ROWS = 200
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
    # 补选项默认不做：PUT fields 对 property 是整体覆盖、飞书按 id 认选项，
    # 写错一次就是清空全表那一列的值且不可逆。在一张废表上验过再开
    # （步骤见 docs/待验证清单.md）。
    allow_option_patch: bool = False
    registry_target: str = ""
    app_id: str = ""
    app_secret: str = ""
    # 待办行上「哪一条」读哪一列。**空 = 面板自己在表里找**
    # （`summary.LABEL_CANDIDATES`，「笔记内容」打头）。写了就只认它——
    # 表里没有那一列会在项目卡上点名，而不是悄悄换一列。
    label_column: str = ""
    # 「直接新建一张」建完给谁开权限。不配的话建出来的表只有应用能管：
    # 人打开只有「可阅读」，连链接分享范围都动不了。运营背后的飞书账号各不
    # 相同，所以编辑权限给**群**；管理权限给具体的人，不再全绑在机器人上。
    table_managers: tuple = ()        # FEISHU_TABLE_MANAGERS：邮箱 / ou_ open_id → 可管理
    table_editor_chats: tuple = ()    # FEISHU_TABLE_EDITOR_CHATS：oc_ chat_id → 可编辑
    table_owner: str = ""             # FEISHU_TABLE_OWNER：把所有权转给这个人（可选）

    # 余额那一块。两家的余额端点都是官方标明零费用的（见 xhsearch/balance.py
    # 开头那张表），所以「面板不发付费请求」这条不变量没被动过——
    # 而且面板本来就一直在发免费请求（飞书、Railway）。
    show_balance: bool = True
    tikhub_base: str = ""
    socialdatax_base: str = ""
    usd_to_cny: float = 7.2
    # 余额还够跑几天，低于这个就报红/报黄。做成两级是因为它们要人做的事
    # 不一样：黄 = 该安排充值了，红 = 再不管就要断供。
    runway_warn_days: float = 14.0
    runway_alert_days: float = 5.0
    # 面板在跑哪个 commit。Railway 自己会注入 RAILWAY_GIT_COMMIT_SHA。
    # 有它才能一眼回答「这个修复上线了吗」——这一轮排查里，这个问题
    # 出现了两次，每次都只能靠问，而问和答之间就是一次白跑的部署。
    # 读不到就是空串，页脚整块不显示（本地跑、别的平台跑都不该硬编一个假值）。
    commit: str = ""
    api_keys: dict = field(default_factory=dict)
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
        if raw_secret and len(raw_secret) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"PANEL_SECRET 只有 {len(raw_secret)} 个字符，至少要 "
                f"{MIN_SECRET_LENGTH} 个。它签的是会话 Cookie——猜中就能"
                "绕过 PANEL_PASSWORD 直接进来，而且伪造 Cookie 不走登录节流，"
                "可以离线慢慢试。生成一个："
                'python3 -c "import secrets; print(secrets.token_hex(32))"')
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
            allow_option_patch=_bool_env(env, "PANEL_ALLOW_OPTION_PATCH", False),
            registry_target=(env.get("FEISHU_REGISTRY") or "").strip(),
            app_id=(env.get("FEISHU_APP_ID") or "").strip(),
            app_secret=(env.get("FEISHU_APP_SECRET") or "").strip(),
            label_column=(env.get("PANEL_LABEL_COLUMN") or "").strip(),
            table_managers=_list_env(env, "FEISHU_TABLE_MANAGERS"),
            table_editor_chats=_list_env(env, "FEISHU_TABLE_EDITOR_CHATS"),
            table_owner=(env.get("FEISHU_TABLE_OWNER") or "").strip(),
            commit=(env.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()[:7],
            show_balance=_bool_env(env, "PANEL_SHOW_BALANCE", True),
            runway_warn_days=_float_env(env, "PANEL_RUNWAY_WARN_DAYS",
                                        14.0, 0.5, 3650.0),
            runway_alert_days=_float_env(env, "PANEL_RUNWAY_ALERT_DAYS",
                                         5.0, 0.5, 3650.0),
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


def _list_env(env, name) -> tuple:
    """逗号 / 分号 / 空白分隔的一组值。空 = 空元组。"""
    raw = env.get(name) or ""
    return tuple(v for v in re.split(r"[,;，；\s]+", raw) if v)


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
    # ⚠️ `RailwayConfig.from_env()` **优先**用 RAILWAY_PROJECT_TOKEN，
    # 漏掉它就等于把正在用的那把钥匙原样贴在公网页面上。
    # `tests/test_panel.py` 里有一条 AST 不变量钉住这件事：railway.py 读的
    # 每一个凭据环境变量都必须出现在这个名单里。
    names = ("TIKHUB_API_KEY", "SOCIALDATAX_API_KEY", "FEISHU_APP_SECRET",
             "FEISHU_APP_ID", "RAILWAY_PROJECT_TOKEN", "RAILWAY_API_TOKEN",
             "PANEL_PASSWORD", "PANEL_SECRET")
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
    settings_for: Optional[Callable[[str], Settings]] = None,
    label_column: str = "",
    disabled_tables: Iterable[str] = (),
) -> summary.Overview:
    """把每张表读一遍、聚合成 Overview。**只读，不花钱。**

    `disabled_tables` 是注册表里**停用了**的那些表的名字。它们不读、不统计，
    但要随快照一起带到页面上：停用之后那张表是从页面上**整体消失**的，
    不说一句的话，「停用了」和「读不到了」在运营眼里长得一模一样。

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
        # 逐表 Settings：cron 那边 `_run_locked` 是逐表 `apply_overrides` 的。
        # 这里不跟着做，改过 归档天数 的项目会出现「面板说到期 12 行、
        # cron 刷了 40 行」这种谁都不信的局面。不传就是全局那一份。
        per_table = settings_for(table.table_id) if settings_for else settings
        projects.append(_collect_one(
            label, table, per_table, api_keys,
            show_digest=show_digest, feishu_base=feishu_base, now=now,
            scrub=scrub, label_column=label_column))
    return summary.Overview(projects=projects, generated_at=now,
                            disabled_tables=[t for t in disabled_tables if t])


def _collect_one(label, table, settings, api_keys, *,
                 show_digest, feishu_base, now,
                 scrub: Optional[Callable[[str], str]] = None,
                 label_column: str = "",
                 ) -> summary.ProjectSnapshot:
    route = getattr(table, "route", "base")
    blank = summary.ProjectSnapshot(
        label=label, app_token=table.app_token, table_id=table.table_id,
        route=route,
        table_url=summary.table_url(feishu_base, table.app_token,
                                    table.table_id, route=route))
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
    # 「哪一条」用哪一列，在 search 之前定下来（下面那条纪律的同一个原因）。
    picked = summary.pick_label_column(known, label_column)
    # 只请求确实存在的列：按名字请求不存在的列会让整个 search 报 1254045，
    # 一行都读不回来（和 runner.load_rows 同一条纪律）。
    wanted = [c for c in summary.panel_fields(settings, show_digest=show_digest,
                                              extra=(picked,))
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
        scrub=scrub, route=route, label_column=picked)
    if filter_spec is None:
        snap.health = list(snap.health) + [
            f"表里没有「{f.monitoring}」列，面板无法只统计在管的行，"
            f"下面的数字包含了本该被排除的行"]
    if not picked:
        # 「哪一条」空掉一整栏，运营就只能一条条点开链接看——正是这个面板
        # 要消灭的动作。所以要在项目卡上说清楚差什么、怎么补。
        tried = "」「".join(summary.LABEL_CANDIDATES)
        snap.health = list(snap.health) + ([
            f"设了 PANEL_LABEL_COLUMN=「{label_column}」，但这张表没有这一列，"
            f"「哪一条」那一栏只能退回用「{f.link}」的残余文字。列名写错了？"
        ] if label_column else [
            f"这张表里没有笔记内容列（试过「{tried}」），「哪一条」那一栏"
            f"只能退回用「{f.link}」抠掉链接之后剩的文字。"
            f"把内容列改名成其中之一，或者设 PANEL_LABEL_COLUMN 指过去。"])
    if f.last_updated not in known:
        snap.estimate_blocked = (
            f"表里没有「{f.last_updated}」列，判不了哪些行到期——"
            "「到期待刷」和「预计花费」这两个数**算不出来**（不是 0）。"
            "把这一列建出来之后全表都会判到期，先看清有多少行")
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
        self._done = threading.Condition(self._lock)
        self._value: Optional[summary.Overview] = None
        self._error: str = ""
        self._fetched_at: float = 0.0
        self._refreshing = False
        # 最近一次取数**开始**的时刻（monotonic）。判「我之后有没有人刷过」
        # 要看开始时刻而不是完成时刻：一趟在我之前开始、我之后才完成的取数，
        # 读注册表那一步发生在我之前，我刚写进去的停用它看不见。
        self._started_at: float = -1.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def snapshot(self) -> tuple[Optional[summary.Overview], str, float]:
        with self._lock:
            return self._value, self._error, self._fetched_at

    def refresh(self) -> None:
        """跑一次取数。**返回时保证有一趟在本次调用之后开始的取数已经完成。**

        同一时刻仍只允许一个在跑——并发刷新只是把飞书请求翻倍。但「有人在跑
        就直接返回」是不够的：停用一张表之后接着调 refresh()，撞上后台那趟
        正在跑的（它读注册表时那张表还是启用的）就会空手而归，页面重载后
        那张表照样在——看着就像停用没生效。所以改成：有人在跑就**等它跑完**，
        然后看在我之后有没有人已经开始过一趟；没有才自己跑。五个人同时点
        「重新取数」仍然只会跑一到两趟，不会五趟。
        """
        asked = time.monotonic()
        with self._done:
            while self._refreshing:
                self._done.wait()
            if self._started_at >= asked:
                return
            self._refreshing = True
            self._started_at = time.monotonic()
        try:
            value = self._produce()
            with self._lock:
                self._value, self._error, self._fetched_at = value, "", time.time()
        except Exception as exc:                                # noqa: BLE001
            # 取数失败保留上一份快照：一次网络抖动不该让整个面板变空白。
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            with self._done:
                self._refreshing = False
                self._done.notify_all()

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
        # 这一轮有没有结果可以端出去（有日志、零日志、或者一个错误都算）。
        # **不能用 `self._lines or self._error` 代替**：
        # 一次成功但零行的拉取会让两个都为假，于是 TTL 内每次开页面都重打
        # Railway——而新服务、或 RUN_LOG_JSON 还没产出的时候正好就是这个
        # 状态，缓存在最该生效的空态下失效。
        self._loaded = False

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
            if fresh and not force and self._loaded:
                return list(self._lines), self._error
        try:
            lines = self._fetch(self._config.railway,
                                secrets=self._config.secrets)
            with self._lock:
                self._lines, self._error = lines, ""
                self._fetched_at = time.time()
                self._loaded = True
        except railway.RailwayError as exc:
            # 保留上一批：日志少一次刷新不该让页面上那一块变空白。
            with self._lock:
                self._error = str(exc)
                self._fetched_at = time.time()
                self._loaded = True
        except Exception as exc:                                # noqa: BLE001
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._fetched_at = time.time()
                self._loaded = True
        with self._lock:
            return list(self._lines), self._error


class BalanceFeed:
    """两家通道余额的缓存。**只打官方标明零费用的端点**（见 balance.py）。

    单独一个缓存，和聚合缓存、日志缓存都分开。三者的失败模式互不相干：
    余额接口 401 不该让整个面板空白，也不该让日志那一块跟着消失。

    TTL 跟着面板的聚合缓存走。TikHub 那个端点限速 1/second，60 秒一次
    余量三个数量级；后台单线程刷，不是「谁开页面就打一次」——
    否则五个人同时刷新页面就是五倍上游请求。
    """

    def __init__(self, config: PanelConfig, ttl: float, read=None):
        self._config = config
        self._ttl = ttl
        self._read = read or balance_mod.read_all
        self._lock = threading.Lock()
        self._items: list = []
        self._error = ""
        self._fetched_at = 0.0
        self._loaded = False

    @property
    def enabled(self) -> bool:
        return bool(self._config.show_balance and self._config.api_keys)

    def why_disabled(self) -> str:
        if not self._config.show_balance:
            return "已关掉（PANEL_SHOW_BALANCE=0）"
        return ("没配 TIKHUB_API_KEY / SOCIALDATAX_API_KEY，读不到余额。"
                "这两个端点是免费的，配上不会花钱")

    def get(self, *, force: bool = False) -> tuple[list, str]:
        if not self.enabled:
            return [], self.why_disabled()
        with self._lock:
            fresh = (time.time() - self._fetched_at) < self._ttl
            if fresh and not force and self._loaded:
                return list(self._items), self._error
        try:
            items = self._read(
                self._config.api_keys,
                tikhub_base=self._config.tikhub_base,
                socialdatax_base=self._config.socialdatax_base,
                usd_to_cny=self._config.usd_to_cny)
            with self._lock:
                self._items, self._error = list(items), ""
                self._fetched_at = time.time()
                self._loaded = True
        except Exception as exc:                                # noqa: BLE001
            # 保留上一批。整块取数炸了和「某一家读不到」是两回事——
            # 后者由 Balance.error 逐家表达，不会走到这里。
            #
            # 过一遍脱敏：这句话会渲染到页面上，而异常文本的内容不由我们决定
            # （第三方库可能把请求头、URL 原样塞进去）。这里的 Key 本来就在
            # 配置里，多脱一次的代价只是几次字符串替换。
            with self._lock:
                self._error = railway.redact(f"{type(exc).__name__}: {exc}",
                                             self._config.secrets)
                self._fetched_at = time.time()
                self._loaded = True
        with self._lock:
            return list(self._items), self._error


@dataclass
class Runway:
    """按最近的实际花速，余额还够跑多久。**不发任何请求**——
    花速来自 Railway 日志里已有的 run 历史，余额来自 BalanceFeed。

    这是运营真正想知道的那个数：「还能跑多久」比「还剩多少钱」有用，
    因为后者要人自己心算，而心算的前提是知道每天花多少——那正是没人知道的。
    """

    days: Optional[float] = None
    yuan_per_day: float = 0.0
    yuan_left: float = 0.0
    # 算这个花速用了几轮、覆盖多长时间。样本太少的时候这个数会很飘，
    # 页面上要把依据一起显示出来，别让人把它当成一个准数。
    runs_used: int = 0
    hours_covered: float = 0.0
    # 有通道读不到余额时，`yuan_left` 是**下界**，days 也就是下界。
    partial: bool = False
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.days is not None


# 花速至少要这么多轮、这么长时间才算得准。低于它就只报「样本不够」——
# 一轮恰好赶上批量勾排队刷新，算出来的日花速能比真实高一个数量级，
# 据此说「只够跑 2 天」会让人白白去充一笔钱。
MIN_RUNWAY_RUNS = 3
MIN_RUNWAY_HOURS = 2.0


def runway_from_runs(balances: list, runs: list) -> Runway:
    """余额 + run 历史 → 还够跑几天。"""
    usable = [b for b in (balances or []) if getattr(b, "ok", False)]
    if not usable:
        return Runway(reason="两家余额都读不到")
    yuan_left = sum(b.yuan or 0.0 for b in usable)
    partial = len(usable) < len(balances or [])

    # `is not None`，**不是真值判断**：一个 started_at == 0 的轮子会被真值
    # 判断悄悄丢掉，于是花速按少一轮算、还够跑多久跟着偏。同一类坑在这个
    # 仓库里已经踩过一次（`now or time.time()` 把 epoch 0 当成没传）。
    finished = [r for r in (runs or [])
                if r.started_at is not None and r.ended_at is not None
                and r.cost_yuan is not None and r.cost_yuan >= 0]
    if len(finished) < MIN_RUNWAY_RUNS:
        return Runway(yuan_left=yuan_left, partial=partial,
                      runs_used=len(finished),
                      reason=f"只有 {len(finished)} 轮记录，"
                             f"至少要 {MIN_RUNWAY_RUNS} 轮才算得准")
    first = min(r.started_at for r in finished)
    last = max(r.ended_at for r in finished)
    hours = (last - first) / 3600.0
    if hours < MIN_RUNWAY_HOURS:
        return Runway(yuan_left=yuan_left, partial=partial,
                      runs_used=len(finished), hours_covered=hours,
                      reason=f"这些轮只覆盖了 {hours:.1f} 小时，"
                             f"不足 {MIN_RUNWAY_HOURS:.0f} 小时，花速还看不准")
    spent = sum(r.cost_yuan for r in finished)
    per_day = spent / hours * 24.0
    if per_day <= 0:
        return Runway(yuan_left=yuan_left, partial=partial,
                      runs_used=len(finished), hours_covered=hours,
                      reason="最近这些轮一分钱没花，算不出花速（也就不用担心）")
    return Runway(days=yuan_left / per_day, yuan_per_day=per_day,
                  yuan_left=yuan_left, runs_used=len(finished),
                  hours_covered=hours, partial=partial)


# 面板对**业务表数据**唯一允许写的列。只有一个元素，而且不会变长。
#
# 为什么是白名单而不是黑名单：黑名单要穷举「不许写什么」，漏一个就是默许。
# 白名单漏一个只是少个功能。这一列是运营本来就会手动勾的东西，
# 面板勾它和人勾它走的是同一条路（cron 五分钟内接手）。
#
# 永不写的两类，都在这个白名单之外：
# * 机器的**结论列**（流量状态/巡查状态/诊断信息/最近检查时间/评论数……）
#   —— 写它们等于伪造监控结果，面板没量过任何东西
# * 运营的**输入列**（评论关键词/负面词/发布时间/反馈链接）
#   —— 那些是人的判断，面板只显示不代填
BUSINESS_WRITE_WHITELIST = ("排队刷新",)


@dataclass
class QueueResult:
    queued: int = 0
    skipped_archived: int = 0
    failures: list = field(default_factory=list)
    # **真的勾上了**的那些行。前端只给这些行标「排队中」——逐行失败的、
    # 整表写炸的都不在里面，否则标记会说「cron 会来刷」而 cron 根本没收到。
    record_ids: list = field(default_factory=list)


class Queueing:
    """把业务表里的「排队刷新」勾上。**面板对业务表数据唯一的写路径。**

    面板自己一个付费请求都不发——勾上之后由 cron 那个服务在五分钟内接手，
    和运营手工勾完全是同一条路，同样受 MAX_RECORDS_PER_RUN 这些硬预算约束。
    """

    def __init__(self, config: PanelConfig, settings: Settings,
                 log: Callable[[str], None] = print):
        self.config = config
        self.settings = settings
        self.log = log

    def queue(self, app_token: str, table_id: str,
              record_ids: list[str]) -> QueueResult:
        column = self.settings.fields.queued
        if column not in BUSINESS_WRITE_WHITELIST:
            # 到不了这里——除非有人改了 config 里的列名却忘了改白名单。
            # 那一天这条断言会挡住一次「面板开始写一列它不该写的东西」。
            raise RuntimeError(
                f"「{column}」不在面板的业务表写白名单里，拒绝写入")
        result = QueueResult()
        if not record_ids:
            return result
        table = feishu.Bitable(app_id=self.config.app_id,
                               app_secret=self.config.app_secret,
                               app_token=app_token, table_id=table_id)
        meta = table.fields_meta()
        if not meta:
            raise feishu.FeishuError(-1, provision.NOT_A_COLLABORATOR)
        info = meta.get(column)
        if info is None:
            raise feishu.FeishuError(
                -1, f"表里没有「{column}」列，勾不了——先在面板上把它建出来")
        if not feishu.value_fits(info.get("type"), True):
            raise feishu.FeishuError(
                -1, f"「{column}」不是复选框（现在是类型 {info.get('type')}），"
                    "勾上去会写失败")

        errors: list = []
        written = table.batch_update(
            [{"record_id": rid, "fields": {column: True}} for rid in record_ids],
            errors=errors)
        result.queued = written
        result.failures = [f"{rid}：{exc.msg}" for rid, exc in errors]
        failed = {rid for rid, _ in errors}
        result.record_ids = [rid for rid in record_ids if rid not in failed]
        self.log(f"☑ 勾「{column}」{written} 行 @ {app_token[-6:]}/{table_id}"
                 f"（cron 五分钟内接手）")
        return result


class Projects:
    """注册表的读写。面板对**注册表**的全部写路径都在这里。

    和业务表的写路径（P4 的「排队刷新」）刻意分开：注册表是这套东西自己的
    配置存储，改它不会碰运营的任何数据；业务表是运营的资产，那边的白名单
    只有一列。两者混在一起会让「面板到底能写什么」说不清。
    """

    def __init__(self, config: PanelConfig, settings: Settings,
                 log: Callable[[str], None] = print):
        self.config = config
        self.settings = settings
        self.log = log

    @property
    def enabled(self) -> bool:
        return bool(self.config.registry_target and self.config.app_id)

    def why_disabled(self) -> str:
        if not self.config.app_id:
            return "面板没配 FEISHU_APP_ID"
        return ("还没配 FEISHU_REGISTRY——在面板上加表删表需要一个存清单的地方。"
                "跑一次 `python3 cli.py init-registry`，它会自动建好并打印"
                "这一行环境变量，粘进 Railway 就行（这辈子只用做一次）。")

    def _bitable(self, app_token: str, table_id: str) -> feishu.Bitable:
        return feishu.Bitable(app_id=self.config.app_id,
                              app_secret=self.config.app_secret,
                              app_token=app_token, table_id=table_id)

    def _registry(self) -> feishu.Bitable:
        from . import tablespec
        target = tablespec.parse_target(self.config.registry_target,
                                        default_label="registry")
        return self._bitable(target.app_token, target.table_id)

    def list(self) -> list:
        return registry.read(self._registry())

    def check(self, label: str, target: str):
        """体检一个候选表。**不花钱，也不写任何东西。**"""
        from . import tablespec
        parsed = tablespec.parse_target(target, default_label=label)
        # **停用的行也要参与查重。** `usable` 里含 `enabled`，拿它过滤的话，
        # 把一张表停用之后再体检它会报「配置齐了，可以直接入册」，
        # 于是同一张表在清单里出现两行——两行都启用之后，一轮里这张表
        # 会被刷两遍，等于付两次钱。这里只要求「解析出了 table_id」。
        known = [(e.label, e.app_token, e.table_id)
                 for e in self.list() if e.table_id]
        return provision.check(
            self._bitable(parsed.app_token, parsed.table_id), self.settings,
            label=label or parsed.label, target=target, known_tables=known)

    def build(self, app_token: str, table_id: str):
        """把缺的列建出来。只追加，绝不改已有列。"""
        table = self._bitable(app_token, table_id)
        meta = table.fields_meta()
        if not meta:
            raise feishu.FeishuError(-1, provision.NOT_A_COLLABORATOR)
        return provision.build_missing(
            table, schema.diff(self.settings, meta),
            allow_option_patch=self.config.allow_option_patch, log=self.log)

    def add(self, label: str, target: str, note: str, client_token: str) -> str:
        from . import tablespec
        parsed = tablespec.parse_target(target, default_label=label)
        self.log(f"📋 入册：{label or parsed.label} → "
                 f"{parsed.app_token[-6:]}/{parsed.table_id}")
        return registry.add(self._registry(), label=label or parsed.label,
                            target=target, note=note, client_token=client_token)

    def share_plan(self) -> provision.SharePlan:
        return provision.SharePlan(
            managers=tuple(self.config.table_managers),
            editor_chats=tuple(self.config.table_editor_chats),
            owner=self.config.table_owner)

    def create(self, name: str, template: str = "full") -> dict:
        """从零建一张监控表，建完按配置加协作者。一次飞书都不用点。"""
        workspace = feishu.Workspace(app_id=self.config.app_id,
                                     app_secret=self.config.app_secret)
        made = provision.create_monitored_table(
            workspace, self.settings, name, template=template,
            share=self.share_plan(), log=self.log)
        self.log(f"🧱 新建监控表 {name}（{template}，{made['columns']} 列）→ "
                 f"{made['app_token'][-6:]}/{made['table_id']}")
        return made

    def preview_thresholds(self, record_id: str, values: dict) -> dict:
        """改阈值之前先算给人看。**不花钱**——用表里已有的评论数就能算。

        这是改阈值唯一的真实副作用：热度档是**棘轮（只升不降）**的，
        改完不回溯。调低门槛的行下一轮会升上去；调高门槛的行**不会降回来**，
        于是表里会短暂并存两套口径打出来的标签。
        """
        entries = {e.record_id: e for e in self.list()}
        entry = entries.get(record_id)
        if entry is None:
            raise registry.RegistryError("注册表里没有这一行")
        old = registry.apply_overrides(self.settings, entry)
        after = registry.Entry(label=entry.label,
                               thresholds={**entry.thresholds, **values})
        override = registry.read_overrides(after, self.settings)
        new = registry.apply_overrides(self.settings, after)

        table = self._bitable(entry.app_token, entry.table_id)
        meta = table.fields_meta()
        if not meta:
            raise feishu.FeishuError(-1, provision.NOT_A_COLLABORATOR)
        f = self.settings.fields
        wanted = [c for c in (f.link, f.publish_time, f.comment_count,
                              f.traffic_status, f.monitoring) if c in meta]
        filter_spec = None
        if f.monitoring in meta:
            filter_spec = {"conjunction": "and", "conditions": [
                {"field_name": f.monitoring, "operator": "is", "value": ["true"]}]}
        records = table.search(wanted, filter_spec=filter_spec)
        shift = summary.preview_tier_shift(records, old, new)
        return {"describe": shift.describe(), "changed": shift.changed,
                "up": shift.up, "down_blocked": shift.down_blocked,
                "examples": shift.examples, "problems": override.problems,
                "effective": override.values}

    def set_thresholds(self, record_id: str, values: dict) -> None:
        shown = "、".join(f"{k}={v}" for k, v in sorted(values.items()))
        self.log(f"⚙ 改阈值 {record_id}：{shown or '（清空，回到全局默认）'}")
        registry.set_thresholds(self._registry(), record_id, values)

    def set_enabled(self, record_id: str, enabled: bool) -> None:
        self.log(f"📋 {'启用' if enabled else '停用'} 注册表行 {record_id}")
        registry.set_enabled(self._registry(), record_id, enabled)

    def remove(self, record_id: str) -> None:
        """从注册表删一行 = 不再监控。**业务表和它的数据一个字都不动。**"""
        self.log(f"📋 移除注册表行 {record_id}（业务表数据不动）")
        registry.remove(self._registry(), record_id)


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
    balances: Optional["BalanceFeed"] = None
    projects: Optional["Projects"] = None
    queueing: Optional["Queueing"] = None
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
        if path == "/api/projects":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._send_json(200, self._projects_payload())
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
        if path.startswith("/api/projects/"):
            return self._project_action(path.rsplit("/", 1)[-1])
        if path == "/api/queue":
            return self._queue_action()
        if path == "/api/refresh":
            # 只是让缓存立刻重取一遍飞书数据和 Railway 日志。
            # **不发任何付费请求。**
            self.deps.cache.refresh()
            if self.deps.logs is not None:
                self.deps.logs.get(force=True)
            if self.deps.balances is not None:
                self.deps.balances.get(force=True)
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
            # connect-src 少了这一条，fetch() 会退到 default-src 'none'，
            # 于是页面上每一个 fetch 都被**浏览器**拦下，显示
            # `TypeError: Failed to fetch`——项目列表、加表、体检、新建、
            # 重新取数、勾排队刷新，全都点不动，而服务端一切正常、
            # 请求根本没发出去。CSP 只有真浏览器认，urllib 测试看不见它。
            "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
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
        balances, balance_error = self._balances()
        return self.deps.render_page(
            overview=overview, error=error, fetched_at=fetched_at,
            config=self.deps.config, runs=runs, log_error=log_error,
            balances=balances, balance_error=balance_error,
            runway=runway_from_runs(balances, runs),
            csrf=csrf_token(self.deps.config, self._session_token()))

    def _balances(self):
        if self.deps.balances is None:
            return [], ""
        return self.deps.balances.get()

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

    def _queue_action(self):
        """把选中的行勾上「排队刷新」。**面板对业务表数据唯一的写路径。**

        面板自己一个付费请求都不发——勾上之后 cron 五分钟内接手，
        和运营手工勾同一条路，同样受 MAX_RECORDS_PER_RUN 这些硬预算约束。
        """
        if not self._authed():
            return self._send_json(401, {"error": "未登录"})
        if not self._csrf_ok():
            return self._send_json(403, {"error": "缺少 " + CSRF_HEADER})
        if self.deps.queueing is None:
            return self._send_json(400, {"error": "面板没配 FEISHU_APP_ID"})
        body = self._read_body()
        if body is None:
            return None
        try:
            payload = json.loads(body or b"{}")
            rows = payload["rows"]
            if not isinstance(rows, list):
                raise ValueError("rows 要是数组")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return self._send_json(400, {"error": f"请求体不对：{exc}"})
        if len(rows) > MAX_QUEUE_ROWS:
            # 「全选」一次勾几千行 = 下一轮直接顶穿预算。硬上限。
            return self._send_json(400, {
                "error": f"一次最多勾 {MAX_QUEUE_ROWS} 行（这次 {len(rows)} 行）。"
                         "分几次来，或者先想想是不是该调分层刷新的节奏"})

        # 按表分组：一张表一次 batch_update，飞书官方建议同一张表
        # 同一时刻只做一个写操作。
        grouped: dict[tuple, list] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("app_token") or ""), str(item.get("table_id") or ""))
            record_id = str(item.get("record_id") or "")
            if all(key) and record_id:
                grouped.setdefault(key, []).append(record_id)

        # 只给**还在监控中**的表勾。勾上去的行由 cron 接手，而 cron 每轮只跑
        # 注册表里启用的表——给停用/已移除/配置有误的表勾，勾会写进飞书、
        # 面板回一句「等 cron 接手」，然后永远没人来接：那句话是假的，而且
        # 这些勾会一直悬着，哪天一启用就集中花一笔钱。
        # 这种情况的典型来源正是「停用之后页面还没重载，对着旧待办勾」。
        skipped: list[str] = []
        projects = self.deps.projects
        if projects is not None and projects.enabled and grouped:
            try:
                # 同一张表在注册表里可能不止一行：一行在用、一行停用的历史
                # （registry.read 的查重只看在用的行，刻意放行这种形态）。
                # 按 table_id 归并时**在用的那行优先**，不能让后面那行停用的
                # 历史把正在巡查的表盖成「已停用」。
                entries: dict = {}
                for e in projects.list():
                    if not e.table_id:
                        continue
                    seen = entries.get(e.table_id)
                    if seen is None or (e.usable and not seen.usable):
                        entries[e.table_id] = e
            except Exception as exc:                            # noqa: BLE001
                # 读不到注册表就确认不了这些表还在不在监控中。宁可这次不勾：
                # 勾错了要悬到下次启用才发现，重试只是再点一下。
                return self._send_json(400, {
                    "error": f"读不到注册表，没法确认这些表还在监控中，"
                             f"这次一行都没勾。稍后再试：{exc}"})
            for key in list(grouped):
                entry = entries.get(key[1])
                name = entry.label if entry and entry.label else key[1]
                if entry is None:
                    why = "不在监控清单里（已移除？）"
                elif not entry.enabled:
                    why = "已停用——先在下面「项目」里启用，再来勾"
                elif entry.problem:
                    why = f"配置有误（{entry.problem}）"
                else:
                    continue
                skipped.append(f"「{name}」{why}，cron 不会刷它，"
                               f"这 {len(grouped.pop(key))} 行没勾")

        queued, failures = 0, []
        # 真勾上了的行。前端只给它们标「排队中」：拒掉的表、整表写炸的、
        # 逐行失败的，一个都不在里面——标了就是在替 cron 做一个它不会兑现的承诺。
        queued_records: list[str] = []
        for (app_token, table_id), record_ids in grouped.items():
            try:
                result = self.deps.queueing.queue(app_token, table_id, record_ids)
            except feishu.FeishuError as exc:
                failures.append(f"{table_id}：{exc.msg}")
                continue
            except Exception as exc:                            # noqa: BLE001
                failures.append(f"{table_id}：{type(exc).__name__}: {exc}")
                continue
            queued += result.queued
            failures.extend(result.failures)
            queued_records.extend(result.record_ids)
        if grouped:
            self.deps.cache.refresh()
        return self._send_json(200, {"queued": queued, "failures": failures,
                                     "skipped": skipped,
                                     "queued_records": queued_records})

    def _projects_payload(self) -> dict:
        projects = self.deps.projects
        if projects is None or not projects.enabled:
            return {"enabled": False,
                    "hint": projects.why_disabled() if projects else "",
                    "entries": []}
        try:
            entries = projects.list()
        except Exception as exc:                                # noqa: BLE001
            return {"enabled": True, "error": str(exc), "entries": []}
        return {"enabled": True, "error": "",
                "allow_option_patch": self.deps.config.allow_option_patch,
                "entries": [_entry_json(e) for e in entries]}

    def _project_action(self, action: str):
        """项目页的写路径。全部要 CSRF 头，且都不碰业务表的**数据**。"""
        projects = self.deps.projects
        if not self._authed():
            return self._send_json(401, {"error": "未登录"})
        if not self._csrf_ok():
            return self._send_json(403, {"error": "缺少 " + CSRF_HEADER})
        if projects is None or not projects.enabled:
            return self._send_json(
                400, {"error": projects.why_disabled() if projects else "没配"})
        body = self._read_body()
        if body is None:
            return None
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("要一个 JSON 对象")
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send_json(400, {"error": f"请求体不是 JSON：{exc}"})

        try:
            return self._send_json(200, self._run_action(action, payload))
        except tablespec_BadTarget as exc:
            return self._send_json(400, {"error": str(exc)})
        except feishu.FeishuError as exc:
            return self._send_json(
                400, {"error": exc.msg, "hint": exc.hint, "code": exc.code})
        except registry.RegistryError as exc:
            return self._send_json(400, {"error": str(exc)})
        except ValueError as exc:
            # _run_action 里对请求内容的校验（空项目名、不认识的模板、阈值不是
            # 整数）都抛 ValueError——那是请求的错，不是服务器的错，给 400。
            return self._send_json(400, {"error": str(exc)})
        except NotImplementedError:
            return self._send_json(404, {"error": f"没有这个动作：{action}"})
        except Exception as exc:                                # noqa: BLE001
            return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _run_action(self, action: str, payload: dict) -> dict:
        projects = self.deps.projects
        text = lambda key: str(payload.get(key) or "").strip()   # noqa: E731

        if action == "check":
            result = projects.check(text("label"), text("target"))
            return {"checkup": _checkup_json(result)}
        if action == "add":
            record_id = projects.add(
                text("label"), text("target"), text("note"),
                # 幂等键由前端生成：连点两次「加」不该多出两行。
                # 前端拼的是 `add-<表链接>-<项目名>`——带 `://`、`?`、空格、
                # 中文，**不是** UUID，而飞书的 client_token 只吃标准 UUID。
                # 在这里（不可信输入进飞书之前的最后一道）转成 UUIDv5：
                # 同样的种子永远算出同一个键，幂等性原样保留。
                client_token=feishu.idempotency_key(text("client_token")))
            self.deps.cache.refresh()
            return {"record_id": record_id}
        if action == "create":
            name = text("name")
            if not name:
                raise ValueError("要填个项目名")
            template = text("template") or "full"
            if template not in provision.TEMPLATES:
                raise ValueError(f"template 只认 {'/'.join(provision.TEMPLATES)}")
            return {"created": projects.create(name, template=template)}
        if action == "build":
            result = projects.build(text("app_token"), text("table_id"))
            return {"summary": result.summary(), "created": result.created,
                    "options_added": result.options_added,
                    "skipped_options": result.skipped_options,
                    "failures": result.failures, "ok": result.ok}
        if action == "thresholds":
            values = payload.get("values")
            if not isinstance(values, dict):
                raise ValueError("values 要是对象")
            clean = {k: (None if v in ("", None) else int(v))
                     for k, v in values.items()
                     if k in registry.THRESHOLD_COLUMNS}
            if payload.get("preview"):
                return projects.preview_thresholds(text("record_id"), clean)
            projects.set_thresholds(text("record_id"), clean)
            return {"ok": True}
        if action == "enable":
            projects.set_enabled(text("record_id"), bool(payload.get("enabled")))
            self.deps.cache.refresh()
            return {"ok": True}
        if action == "remove":
            projects.remove(text("record_id"))
            self.deps.cache.refresh()
            return {"ok": True}
        raise NotImplementedError(action)

    def _overview_payload(self) -> dict:
        overview, error, fetched_at = self.deps.cache.snapshot()
        return {
            "error": error,
            "fetched_at": fetched_at,
            "projects": [_project_json(p) for p in (overview.projects if overview else [])],
            "todos": [_todo_json(t) for t in (overview.todos() if overview else [])],
        }


def _entry_json(entry) -> dict:
    return {"record_id": entry.record_id, "label": entry.label,
            "target": entry.target, "enabled": entry.enabled,
            "note": entry.note, "status": entry.status,
            "problem": entry.problem, "usable": entry.usable,
            "app_token": entry.app_token, "table_id": entry.table_id,
            "thresholds": dict(entry.thresholds or {}),
            "threshold_columns": list(registry.THRESHOLD_COLUMNS)}


def _checkup_json(c) -> dict:
    return {
        "label": c.label, "target": c.target,
        "app_token": c.app_token, "table_id": c.table_id,
        "reachable": c.reachable, "error": c.error,
        "duplicate": c.duplicate, "ready": c.ready,
        "sample_rows": c.sample_rows,
        "buildable": [col.describe() for col in c.buildable],
        "manual": c.manual,
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
        "estimate_blocked": p.estimate_blocked,
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
        "app_token": t.app_token, "table_id": t.table_id, "key": t.key,
        "link_cell": t.link_cell, "reasons": t.reasons,
        "refresh_status": t.refresh_status, "diagnosis": t.diagnosis,
        "comment_count": t.comment_count, "traffic_tags": t.traffic_tags,
        "seed_keywords": t.seed_keywords, "negative_keywords": t.negative_keywords,
        "checked_at_ms": t.checked_at_ms, "queued": t.queued,
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
                 balances: Optional["BalanceFeed"] = None,
                 projects: Optional[Projects] = None,
                 queueing: Optional[Queueing] = None,
                 render_login=None, render_page=None,
                 host: str = "0.0.0.0") -> PanelServer:
    from . import panel_view
    deps = _Deps(
        config=config, cache=cache, logs=logs, balances=balances,
        projects=projects, queueing=queueing,
        render_login=render_login or panel_view.login_page,
        render_page=render_page or panel_view.overview_page,
    )
    return PanelServer((host, config.port), deps)


def serve(config: PanelConfig, produce: Callable[[], summary.Overview],
          settings: Optional[Settings] = None) -> int:
    """起面板。阻塞到进程被杀。"""
    cache = Cache(produce, config.cache_seconds)
    logs = LogFeed(config, config.cache_seconds)
    balances = BalanceFeed(config, config.cache_seconds)
    resolved = settings or Settings()
    projects = Projects(config, resolved)
    queueing = Queueing(config, resolved) if config.app_id else None
    server = build_server(config, cache, logs=logs, balances=balances,
                          projects=projects, queueing=queueing)
    cache.start()
    print(f"面板已启动：0.0.0.0:{config.port}"
          f"（缓存 {config.cache_seconds:.0f} 秒刷一次，"
          f"评论正文{'展示' if config.show_digest else '不展示'}）", flush=True)
    if logs.enabled:
        print("Railway 日志：已接入", flush=True)
    else:
        print(f"Railway 日志：未接入（差 "
              f"{'、'.join(config.railway.missing())}）", flush=True)
    if projects.enabled:
        print("在面板上加表删表：已就绪", flush=True)
    else:
        print(f"在面板上加表删表：未就绪（{projects.why_disabled()}）", flush=True)
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
