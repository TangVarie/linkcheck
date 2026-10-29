"""监控面板：一个只读的常驻 HTTP 服务。

**这个进程不发巡检付费请求。** 它只做三件事：读飞书表、聚合、渲染。
巡检仍然只有那一个 Railway cron service 在跑——`xhsearch/runlock.py` 是
文件锁，跨容器挡不住第二个付费执行者，所以面板从设计上就不许成为执行者。
代码层面的保证：这个模块不 import 供应商那一层，也不调用任何会发付费请求的
函数——有一条测试按 AST 逐个节点检查，不是靠字符串搜。

唯一的计费外呼是**运营打开单个项目日报时的 DeepSeek 草稿**（POST
`/api/report-ai`，见 `report_ai.py`）：按需、有短缓存、有超时，不参与任何轮询，
也不碰业务表。不配 `DEEPSEEK_API_KEY` 就整块降级成人工填写。

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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from . import balance as balance_mod
from . import (channels as channels_mod, feishu, panel_settings, panel_view,
               provision, railway, readout, registry, report_ai as report_ai_mod,
               schema, summary, tablespec)

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
    # 最后一个请求之后多久没人来，后台就停止整表拉取（见 Cache）。0 = 永不停。
    # 默认 0（Ziao 2026-10-16 拍板）：停过之后每次打开都要先等一趟整表取数，
    # 进来看一眼就得干等；后台一直刷只花飞书的免费读额度，不花钱。
    idle_minutes: float = 0.0
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
    # DeepSeek 只用于用户打开单项目日报后的按需草稿，不在轮询或巡检中调用。
    deepseek_api_key: str = ""
    deepseek_timeout: float = 20.0
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
            idle_minutes=_float_env(env, "PANEL_IDLE_MINUTES", 0.0, 0.0, 1440.0),
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
            deepseek_api_key=(env.get("DEEPSEEK_API_KEY") or "").strip(),
            deepseek_timeout=_float_env(env, "DEEPSEEK_TIMEOUT_SECONDS", 20.0, 5.0, 60.0),
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
    """逗号 / 分号分隔的一组值，每一项两头的空白去掉。空 = 空元组。

    **不按空白切**：手机号常写成「+86 138-0000-0000」，按空白切会变成两个人
    （`+86` 和 `138-0000-0000`），谁都对不上。号码内部的空格和连字符交给
    provision.normalize_mobile 去掉。
    """
    raw = env.get(name) or ""
    return tuple(v.strip() for v in re.split(r"[,;，；]+", raw) if v.strip())


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
             "PANEL_PASSWORD", "PANEL_SECRET", "DEEPSEEK_API_KEY")
    out = [v for v in ((env.get(n) or "").strip() for n in names) if v]
    # 管理员的手机号 / 邮箱不是密钥，但同样不该出现在这个共用口令的页面上。
    # `provision.redact` 已经在源头抹过一遍；这里是第二道——运行日志是 cron
    # 那个进程打的，将来某条话术回声了身份，面板这边也不会原样贴出来。
    # 是**逐项**加进来的：整串「a,b」在日志里永远不会原样出现，抹不到任何东西。
    out += [v for v in _list_env(env, "FEISHU_TABLE_MANAGERS")]
    owner = (env.get("FEISHU_TABLE_OWNER") or "").strip()
    if owner:
        out.append(owner)
    return tuple(out)


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
    # 跨表共用：同一篇登记在两张表里，「预计花费」只算一次（和 cron 一个口径）。
    seen_calls: set = set()
    for label, table in tables:
        # 逐表 Settings：cron 那边 `_run_locked` 是逐表 `apply_overrides` 的。
        # 这里不跟着做，改过 归档天数 的项目会出现「面板说到期 12 行、
        # cron 刷了 40 行」这种谁都不信的局面。不传就是全局那一份。
        per_table = settings_for(table.table_id) if settings_for else settings
        projects.append(_collect_one(
            label, table, per_table, api_keys,
            show_digest=show_digest, feishu_base=feishu_base, now=now,
            scrub=scrub, label_column=label_column, seen_calls=seen_calls))
    return summary.Overview(projects=projects, generated_at=now,
                            disabled_tables=[t for t in disabled_tables if t])


def _collect_one(label, table, settings, api_keys, *,
                 show_digest, feishu_base, now,
                 scrub: Optional[Callable[[str], str]] = None,
                 seen_calls: Optional[set] = None,
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
    # 「相关截图」「数据整理」跟着同一次 search 读回来，只为数「截图传了、
    # 数没出来」的行（见下面）——不多发一个请求。
    wanted = [c for c in summary.panel_fields(
                  settings, show_digest=show_digest,
                  extra=(picked, summary.PERSONA_COLUMN,
                         readout.SCREENSHOT_COLUMN, readout.DATA_COLUMN))
              if c in known]
    # 不按「是否巡查」筛，一次读回整张表，在这里分成两份：在管的行照旧统计；
    # 没勾的行只挑「最近临时查过、查出了问题」的（summary.adhoc_todos）——
    # 排队刷新不看「是否巡查」，以前那些结果在面板上一个字都看不到。
    # 读表不计费，多读几行只是多几个字节，也省得再发第二次 search。
    #
    # ⚠️ 分法不能用 Row.monitoring：飞书对**没勾**的复选框直接不返回这一格，
    # row_from_record 把「这一格不在」当成「表里没这一列」→ 默认在管。
    # 这里已经确认表里有这一列，所以「这一格不在」就是没勾。
    has_monitoring = f.monitoring in known
    try:
        everything = table.search(wanted, filter_spec=None)
    except Exception as exc:                                    # noqa: BLE001
        blank.error = f"读记录失败：{exc}"
        blank.health = health
        return blank
    if has_monitoring:
        records = [r for r in everything
                   if feishu.read_bool((r.get("fields") or {}).get(f.monitoring))]
        unmonitored = [r for r in everything
                       if not feishu.read_bool((r.get("fields") or {}).get(f.monitoring))]
    else:
        records, unmonitored = everything, []

    snap = summary.build_snapshot(
        label=label, app_token=table.app_token, table_id=table.table_id,
        records=records, settings=settings, now=now, api_keys=api_keys,
        health=health, feishu_base=feishu_base, show_digest=show_digest,
        scrub=scrub, route=route, label_column=picked, seen_calls=seen_calls,
        # 待办全留着，截断放到出页面那一步——先按「已解决」拿掉再截（summary.for_display）。
        # 「已解决」记录的收拾（Resolutions.reconcile）也要靠这份没截过的全集。
        truncate=False)
    snap.report_checked_available = f.last_updated in known and f.refresh_status in known
    snap.report_negative_available = f.negative_status in known
    snap.adhoc_todos = summary.adhoc_todos(
        label=label, app_token=table.app_token, table_id=table.table_id,
        records=unmonitored, settings=settings, now=now, feishu_base=feishu_base,
        show_digest=show_digest, scrub=scrub, route=route, label_column=picked,
        max_todos=None)
    snap.report_rows.extend(summary.build_report_rows(
        unmonitored, settings, now=now, adhoc=True))
    # 截图读数：字段接口看不出「数据整理」上的 AI 捷径挂没挂，只能看数据——
    # 截图传了、这一格却一直空着，就是捷径没在干活。两列都在才判得了；
    # 缺列的情况体检那边已经在报，这里不重复。
    if readout.SCREENSHOT_COLUMN in known and readout.DATA_COLUMN in known:
        unread = readout.unread_rows(records)
        if unread:
            snap.health = list(snap.health) + [readout.unread_message(unread)]
    if not has_monitoring:
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
    """TTL 缓存 + 后台单线程刷新，**没人看的时候停下来**。

    **不能改成「每次有人开页面就去拉一遍」**：五个人同时刷新页面就是五倍
    飞书请求，而这一趟要把每张表整表读回来。后台一个线程按 TTL 刷，
    所有人看同一份快照，页面秒开。

    但以前这个线程是 7×24 小时不停的：没人打开面板的深夜，也每 60 秒把所有表
    整表读一遍（一张表一天 1440 次），和 cron 抢同一个飞书应用的限频额度，
    读回来的东西谁都没看。

    现在：**有人看**的判据是「来了一个请求」——这个页面自己不会轮询，数据
    只有在有人打开、刷新页面或点按钮时才会送到人眼前，所以请求就是唯一的、
    确切的信号，不用猜。最后一个请求之后 idle_seconds 内照旧每 TTL 刷一次，
    和以前完全一样；过了这段时间就停下。停过之后的第一个请求会**先等一趟
    新的取数再出页面**（和点「重新取数」一样的等待），所以回来的人看到的
    永远不会比 TTL 更旧——取数失败时照旧显示上一份快照和它的时间、外加
    错误原因。面板是只读的、不发告警，cron 也不读这份缓存，停下来不会让
    任何判断出错。idle_seconds=None = 永不停（老行为）。
    """

    def __init__(self, produce: Callable[[], summary.Overview], ttl: float,
                 idle_seconds: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._produce = produce
        self._ttl = ttl
        self._idle = idle_seconds if idle_seconds and idle_seconds > 0 else None
        self._clock = clock
        self._last_seen = clock()
        # 后台线程因为没人看跳过过至少一轮：下一个请求得先等一趟新的。
        self._paused = False
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

    def touch(self) -> None:
        """有人来了（任何一个登录后的请求）。"""
        with self._lock:
            self._last_seen = self._clock()

    def view(self) -> tuple[Optional[summary.Overview], str, float]:
        """给页面用的快照：记一次「有人在看」；停过的话先等一趟新的再给。

        停过之后同时进来好几个请求（两个人同时打开）：有人正在取就**等它**——
        停着的时候后台不取，正在跑的那一趟一定是停过之后才开始的，就是新的；
        没人在取才自己去取。「停过」的标记要等那一趟**取完**才清（见 refresh），
        否则第二个请求会在第一个还没取完时拿走停之前的旧快照。
        """
        with self._done:
            self._last_seen = self._clock()
            while self._paused and self._refreshing:
                self._done.wait()
            resume = self._paused
        if resume:
            self.refresh()
        return self.snapshot()

    def _someone_watching(self) -> bool:
        if self._idle is None:
            return True
        with self._lock:
            return self._clock() - self._last_seen < self._idle

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
                # 这一趟取完了，就是「停过之后的新一趟」：清掉停过的标记，紧接着
                # 的 view() 不再取第二遍（点「重新取数」= 先 refresh 再出页面）。
                self._paused = False
                self._done.notify_all()

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                if self._someone_watching():
                    self.refresh()
                else:
                    with self._lock:
                        self._paused = True
                self._stop.wait(self._ttl)
        self._thread = threading.Thread(target=loop, name="panel-cache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class _SlowFeed:
    """取一次要好几秒的那几块（Railway 日志、两家余额）共用的缓存。

    以前这几块是「过期了就在请求里当场去取」：TTL 60 秒，而面板常常隔几分钟
    才有人开一次，于是几乎每次打开页面都要先等 Railway（每个服务各查一次，
    20 秒超时、最多试三次）和两家余额接口（各 15 秒超时、试两次）全跑完，
    上游一慢整页就超时。

    现在：过期了在**后台**重取，页面最多等 STALE_WAIT 秒——取完了给新的，
    没取完先给上一份（和它当时的错误），下一次打开就是新的了。一份都还
    没有的时候最多等 FIRST_WAIT 秒，再没有就说「正在读取」。同一时刻只有
    一趟在取：几个人同时开页面也只打一次上游。force=True（点「重新取数」）
    照旧等到取完。页面先对每一块调 prime()，几块一起取，等的时间不叠加。
    """

    STALE_WAIT = 0.3
    FIRST_WAIT = 1.5

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        self._items: list = []
        self._error = ""
        self._fetched_at = 0.0
        # 这一轮有没有结果可以端出去（有数据、零条、或者一个错误都算）。
        # **不能用 `self._items or self._error` 代替**：
        # 一次成功但零条的拉取会让两个都为假，于是 TTL 内每次开页面都重打
        # 上游——而新服务、或 RUN_LOG_JSON 还没产出的时候正好就是这个
        # 状态，缓存在最该生效的空态下失效。
        self._loaded = False
        self._running = False
        # 已经取完了几趟。等的人认「我来之后又取完了一趟」，不认时间。
        self._finished = 0

    def _fetch_now(self) -> list:
        raise NotImplementedError

    def _describe(self, exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"

    def _loading_hint(self) -> str:
        return panel_view.Loading("正在读取，几秒后刷新页面再看")

    def _run(self) -> None:
        try:
            items, error = list(self._fetch_now()), ""
        except Exception as exc:                                # noqa: BLE001
            # 保留上一批：少一次刷新不该让页面上那一块变空白。
            items, error = None, self._describe(exc)
        with self._done:
            if items is not None:
                self._items = items
            self._error = error
            self._fetched_at = time.time()
            self._loaded = True
            self._running = False
            self._finished += 1
            self._done.notify_all()

    def _start_locked(self) -> None:
        if not self._running:
            self._running = True
            try:
                threading.Thread(target=self._run, daemon=True,
                                 name=f"panel-{type(self).__name__}").start()
            except RuntimeError:
                # 起不了线程（极少见）：别让 _running 卡在 True，下次再试。
                self._running = False
                raise

    def prime(self) -> None:
        """没有、或者已经过期，就在后台开始取，不等。

        启动时调一次，第一个打开面板的人就不用等；出页面前对每一块各调一次，
        几块一起取，而不是取完日志再开始取余额。
        """
        with self._done:
            if not self._loaded or (time.time() - self._fetched_at) >= self._ttl:
                self._start_locked()

    def _get(self, force: bool) -> tuple[list, str]:
        with self._done:
            fresh = self._loaded and (time.time() - self._fetched_at) < self._ttl
            if fresh and not force:
                return list(self._items), self._error
            target = self._finished + 1
            self._start_locked()
            budget = (None if force else
                      self.STALE_WAIT if self._loaded else self.FIRST_WAIT)
            deadline = None if budget is None else time.monotonic() + budget
            while self._finished < target:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._done.wait(remaining)
            if not self._loaded:
                return [], self._loading_hint()
            return list(self._items), self._error


class LogFeed(_SlowFeed):
    """Railway 日志的缓存。

    和聚合缓存分开是有意的：飞书那趟慢、Railway 这趟快，失败模式也不一样
    （飞书挂了整个面板没数据，Railway 挂了只是日志那一块看不了）。
    合成一个的话，Railway 一个 401 就能让整个面板变空白。
    """

    def __init__(self, config: PanelConfig, ttl: float,
                 fetch: Optional[Callable[..., list]] = None):
        super().__init__(ttl)
        self._config = config
        self._fetch = fetch or railway.fetch_logs

    @property
    def enabled(self) -> bool:
        return self._config.railway.enabled

    def _missing_hint(self) -> str:
        gaps = self._config.railway.missing()
        return ("没接 Railway 日志：还差 " + "、".join(gaps) +
                "。设上就能在这里看到每一轮的真实输出（见 docs/面板.md）")

    def _fetch_now(self) -> list:
        return self._fetch(self._config.railway, secrets=self._config.secrets)

    def _describe(self, exc: Exception) -> str:
        if isinstance(exc, railway.RailwayError):
            return str(exc)
        return super()._describe(exc)

    def _loading_hint(self) -> str:
        return panel_view.Loading("正在从 Railway 读取运行记录，几秒后刷新页面再看")

    def get(self, *, force: bool = False) -> tuple[list, str]:
        if not self.enabled:
            return [], self._missing_hint()
        return self._get(force)


class BalanceFeed(_SlowFeed):
    """两家通道余额的缓存。**只打官方标明零费用的端点**（见 balance.py）。

    单独一个缓存，和聚合缓存、日志缓存都分开。三者的失败模式互不相干：
    余额接口 401 不该让整个面板空白，也不该让日志那一块跟着消失。

    TTL 跟着面板的聚合缓存走。TikHub 那个端点限速 1/second，60 秒一次
    余量三个数量级；后台一趟一趟地取（见 _SlowFeed），不是「谁开页面就打
    一次」——否则五个人同时刷新页面就是五倍上游请求。
    """

    def __init__(self, config: PanelConfig, ttl: float, read=None):
        super().__init__(ttl)
        self._config = config
        self._read = read or balance_mod.read_all

    @property
    def enabled(self) -> bool:
        return bool(self._config.show_balance and self._config.api_keys)

    def why_disabled(self) -> str:
        if not self._config.show_balance:
            return "已关掉（PANEL_SHOW_BALANCE=0）"
        return ("没配 TIKHUB_API_KEY / SOCIALDATAX_API_KEY，读不到余额。"
                "这两个端点是免费的，配上不会花钱")

    def _fetch_now(self) -> list:
        return self._read(
            self._config.api_keys,
            tikhub_base=self._config.tikhub_base,
            socialdatax_base=self._config.socialdatax_base,
            usd_to_cny=self._config.usd_to_cny)

    def _describe(self, exc: Exception) -> str:
        # 整块取数炸了和「某一家读不到」是两回事——后者由 Balance.error
        # 逐家表达，不会走到这里。
        #
        # 过一遍脱敏：这句话会渲染到页面上，而异常文本的内容不由我们决定
        # （第三方库可能把请求头、URL 原样塞进去）。这里的 Key 本来就在
        # 配置里，多脱一次的代价只是几次字符串替换。
        return railway.redact(super()._describe(exc), self._config.secrets)

    def _loading_hint(self) -> str:
        return panel_view.Loading("正在读取两家余额，几秒后刷新页面再看")

    def get(self, *, force: bool = False) -> tuple[list, str]:
        if not self.enabled:
            return [], self.why_disabled()
        return self._get(force)


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

    # ---------- 新建表给谁开权限 ----------
    #
    # 两种权限两个来源，刻意不一样：
    # * **可管理（人）只来自环境变量**（FEISHU_TABLE_MANAGERS，部署方在后台定）。
    #   面板口令是运营共用的，要是面板上能随手把一个人加成所有新表的管理员，
    #   那就等于谁拿到面板口令谁就能给自己开全部表的管理权。面板上只显示。
    # * **可编辑（群）在面板上选**，存在注册表 base 里的「面板设置」表
    #   （见 panel_settings）。给哪个运营群开编辑权限本来就是运营自己的事，
    #   而且编辑权限的上限是「改数据」，不是「改权限」。环境变量里配的群也认，合并。

    def _workspace(self) -> feishu.Workspace:
        return feishu.Workspace(app_id=self.config.app_id,
                                app_secret=self.config.app_secret)

    def _store(self) -> panel_settings.SettingsStore:
        store = getattr(self, "_settings_store", None)
        if store is None:
            target = tablespec.parse_target(self.config.registry_target,
                                            default_label="registry")
            store = panel_settings.SettingsStore(
                self._workspace(), self._bitable, target.app_token, log=self.log)
            self._settings_store = store
        return store

    def _stored_chats(self) -> list:
        raw = self._store().get_json(panel_settings.KEY_EDITOR_CHATS, [])
        return [c for c in raw if isinstance(c, dict) and c.get("chat_id")]

    def _chat_entries(self) -> list:
        return ([{"chat_id": c, "name": "", "source": "env"}
                 for c in self.config.table_editor_chats]
                + [{"chat_id": c["chat_id"], "name": c.get("name") or "",
                    "source": "panel"} for c in self._stored_chats()])

    def _managers(self) -> list:
        """去重之后的可管理清单。**数数和真正开权限必须用同一份**——
        同一个人在环境变量里写了两遍时，面板说「已配 2 人」而实际只开一个，
        那个数字就是错的（去重在下面 share_plan 里做过一次）。"""
        out: list = []
        for m in self.config.table_managers:
            if m not in out:
                out.append(m)
        return out

    def share_state(self) -> dict:
        """面板「协作者」那一栏要显示的东西。

        **可管理的人只给数量，不给是谁。** 手机号 / 邮箱是个人信息，面板口令又是
        运营共用的——管理员是谁不该从面板上看出来。这个字典是原样送进浏览器的，
        所以这里就不放。群名可以放：群本来就是给运营看的。
        """
        return {"managers_count": len(self._managers()),
                "editor_chats": self._chat_entries(),
                "owner_configured": bool(self.config.table_owner)}

    def share_plan(self) -> provision.SharePlan:
        managers = self._managers()
        chats, labels = [], {}
        for c in self._chat_entries():
            if c["chat_id"] not in chats:
                chats.append(c["chat_id"])
                if c["name"]:
                    labels[c["chat_id"]] = c["name"]
        return provision.SharePlan(managers=tuple(managers), editor_chats=tuple(chats),
                                   owner=self.config.table_owner, labels=labels)

    def list_chats(self) -> list:
        """应用（作为机器人）所在的群。给「选群」用——飞书界面上普通成员
        看不到群的 ID。只读，不花钱。"""
        return self._workspace().list_chats()

    def set_editor_chats(self, chats: list) -> dict:
        """面板上选好的群，整体覆盖存下来。只认 oc_ 开头的 chat_id。"""
        clean = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            chat_id = str(item.get("chat_id") or "").strip()
            if provision.member_type(chat_id) != "openchat":
                raise ValueError(f"「{chat_id}」不是群 ID（要 oc_ 开头）")
            if chat_id in [c["chat_id"] for c in clean]:
                continue
            clean.append({"chat_id": chat_id, "name": str(item.get("name") or "").strip()})
        self._store().set_json(panel_settings.KEY_EDITOR_CHATS, clean)
        self.log(f"🔑 新建表的可编辑群 → {'、'.join(c['name'] or c['chat_id'] for c in clean) or '（清空）'}")
        return self.share_state()

    def apply_share(self, app_token: str) -> provision.ShareResult:
        """按当前设置给一张**已经建好的**表补协作者。应用得是这张表的所有者或
        可管理——自己建的表天然是；接管的业务表要看当初给应用的是什么权限。

        **只认注册表里的表。** 面板口令是运营共用的；不查的话，知道任何一个
        应用能管的 base 的 app_token，就能把配好的群（和人）加到那个 base 上去
        ——下拉框只列清单里的表，但服务端不能指望前端。
        """
        if not app_token:
            raise ValueError("要选一张表")
        known = {e.app_token for e in self.list() if e.app_token}
        if app_token not in known:
            raise ValueError("这张表不在监控清单里——只给清单里的表补协作者")
        self.log(f"🔑 给 {app_token[-6:]} 补协作者")
        return provision.share_table(self._workspace(), app_token, self.share_plan(),
                                     log=self.log)

    def create(self, name: str, template: str = "full") -> dict:
        """从零建一张监控表，建完按配置加协作者。一次飞书都不用点。"""
        made = provision.create_monitored_table(
            self._workspace(), self.settings, name, template=template,
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


# 「已解决」的 table_id / record_id：飞书的 id 只有字母数字和 _-。
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class DailyReports:
    """日报免费读取与短缓存；与主页面读表解耦，不在请求中触发巡检。"""

    def __init__(self, projects: Projects, *, journal=None,
                 wall: Callable[[], float] = time.time):
        self.projects = projects
        self._journal_value = journal
        self._wall = wall
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._version = 0
        self.warning = ""
        self.resolutions = None

    def _journal(self):
        if self._journal_value is None:
            from .daily_report import Journal
            target = tablespec.parse_target(self.projects.config.registry_target,
                                            default_label="registry")
            self._journal_value = Journal(self.projects._workspace(),
                                          self.projects._bitable, target.app_token,
                                          allow_create=False)
        return self._journal_value

    def get(self, overview: Optional[summary.Overview], *, date: str = "",
            table_id: str = "", with_events: bool = False):
        from . import daily_report
        now = self._wall()
        today = datetime.fromtimestamp(now, daily_report.SHANGHAI).date()
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date() if date else today
        except ValueError as exc:
            raise ValueError("date 请使用 YYYY-MM-DD，表示日报截止日") from exc
        if not today - timedelta(days=90) <= day <= today + timedelta(days=1):
            raise ValueError("可查看最近 90 天及当前周期的日报")
        start_ms, end_ms = daily_report.window_for_date(day)
        if start_ms >= int(now * 1000):
            raise ValueError("这个日报周期还未开始；请在北京时间 16 点后查看下一期")
        if overview is None:
            payload = {"reports": [], "error": "监控数据还未就绪，请稍后再试", "as_of": now}
            # with_events 的调用方（AI 草稿）按元组解包，早退也必须同一个形状，
            # 否则冷启动时打开日报会变成一个「AI 生成失败」的假错误。
            return (payload, []) if with_events else payload
        projects = [p for p in overview.projects if not table_id or p.table_id == table_id]
        if table_id and not projects:
            raise ValueError("项目不在当前监控清单里")
        if self.resolutions is not None:
            self.resolutions.flush_journal(self)
        resolved = self.resolutions.snapshot() if self.resolutions is not None else {}
        snapshots = {item["table_id"]: item for item in summary.snapshot_report(
            overview, resolved, self.projects.settings, start_ms, end_ms, int(now * 1000))}
        journal_error = ""
        # 同一时间窗只读一次留痕；并发浏览器不各扫一遍飞书。
        with self._read_lock:
            with self._lock:
                version = self._version
                cached = self._cache.get(start_ms)
            if cached and cached[0] == version and now - cached[1] < 60:
                events = cached[2]
                events_wall = cached[1]
            else:
                try:
                    events = self._journal().read_window(start_ms, min(end_ms, int(now * 1000)))
                except Exception as exc:                        # noqa: BLE001
                    events = []
                    journal_error = str(exc)
                events_wall = now
                with self._lock:
                    if version == self._version and not journal_error:
                        self._cache[start_ms] = (version, now, events)
                        while len(self._cache) > 8:
                            self._cache.pop(next(iter(self._cache)))
        reports = [daily_report.aggregate(events, start_ms, end_ms, p.table_id, p.label,
                                          as_of_ms=int(events_wall * 1000)) for p in projects]
        for report, project in zip(reports, projects):
            if project.error:
                report["limitations"].append("当前业务表读取失败：" + project.error)
            if self.warning:
                report["limitations"].append(self.warning)
                report["window"]["coverage_complete"] = False
        reports = [daily_report.present_report(report, snapshots.get(report["table_id"]),
                                               int(now * 1000), journal_error=journal_error)
                   for report in reports]
        if self.warning:
            # 加在 present_report **之后**：它会整段重写 text，加在前面就只剩页面上的
            # 限制说明里有，运营复制出去的那段汇报里看不到「可能不完整」。
            for shown in reports:
                if shown.get("text"):
                    shown["text"] = "部分处理状态留痕待同步，本报可能不完整。" + shown["text"]
        payload = {"reports": reports, "error": self.warning, "as_of": events_wall}
        # 原始留痕只供服务端 AI 证据抽取；普通日报 API 永远不返回它。
        return (payload, events) if with_events else payload

    def record_status(self, table_id: str, record_id: str, state: str, *,
                      at_ms: Optional[int] = None) -> None:
        self.record_events([{"type": "status", "at": (int(self._wall() * 1000)
                                                       if at_ms is None else at_ms),
                             "table_id": table_id, "record_id": record_id, "state": state}])

    def record_events(self, events: list[dict]) -> None:
        self._journal().append(events)
        with self._lock:
            self._version += 1
            self._cache.clear()
        self.warning = ""


class Resolutions:
    """运营在面板上点的「已解决」。**不写业务表**——存在注册表 base 的「面板设置」
    表（键 todo.resolved，见 panel_settings），出页面之前按它把行收起来；
    什么时候再冒出来由 summary.apply_resolutions 定。每次读完表之后
    reconcile 把已经消掉的问题从记录里划掉，同一个问题再发生一次才会重新显示。

    读是热路径（每次出页面都要），所以内存里留一份：面板是单进程，点「已解决」
    写完就地换掉这一份；每 CACHE_SECONDS 才重读一次（有人在飞书里手改那张表也认），
    而且是**先给旧的、后台再读**——出页面不等飞书。只有进程起来后的第一次要等
    （serve 里起进程时就先读一遍，一般轮不到页面等）。
    **读失败不挡页面**：用上一份（第一次就失败 = 什么都不收），一分钟后再试——
    失败方向是「多显示几行」，不是「藏掉该看的」。
    """

    # 记录留多久。过期 = 那一行如果还挂着问题，会重新出现（多看一眼，不是少看）。
    # 读的时候就按它筛（_live），不用等下一次有人点按钮才生效。
    KEEP_DAYS = 90
    # 整份存在一格文本里，不能无限长。超了先丢最早点的。
    MAX_ENTRIES = 500
    # 按字数再收一道：一整份（记录 + 待补写 + 丢弃记账）都在「面板设置」的一格里，
    # 飞书单格上限 10 万字（panel_settings.CELL_MAX_CHARS）。记录本身控制在这个数以内，
    # 加上待补写的 OUTBOX_MAX_CHARS 和这次要加的那条也到不了上限——
    # 不然记录攒满 500 条（带帖子键的一条两百来字）以后，每一次点击都写不进去。
    ENTRIES_MAX_CHARS = 70_000
    RETRY_SECONDS = 60.0
    OUTBOX = "__journal_outbox__"
    # 留痕一直写不进去时，待补写的事件不能无限攒（整份在一格文本里）。超了先丢
    # 被同一篇帖子（同一行、同一个帖子键）后面的操作盖掉的，没有才丢最早的。
    # 每张表记 [最早丢的时刻, 丢了几条, 其中几条是帖子的最新状态]（DROPPED）。
    # 补写时：从最早丢的那一刻到补写这一刻，各期日报一律标「覆盖不完整」——
    # 不逐条推算哪几期受影响，这段时间留痕本来就是坏的。丢过最新状态的，那篇帖子
    # 之后的状态也不可信，缺口再往后标 LOST_STATE_DAYS 天。
    # 不能反过来拦住运营点「已处理」：显示状态是运营的工作，日报留痕只是记录，
    # 记录坏了不该让工作停下来。
    DROPPED = "__journal_outbox_dropped__"
    OUTBOX_MAX_EVENTS = 1000
    OUTBOX_MAX_CHARS = 20_000
    LOST_STATE_DAYS = KEEP_DAYS

    def __init__(self, projects: Projects, *, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 spawn: Optional[Callable[[Callable[[], None]], None]] = None):
        self.projects = projects
        self.log = log
        self._clock = clock
        self._wall = wall
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        # 写飞书 + 换内存那一份，整段串起来：两个请求同时点，飞书那边有锁按顺序
        # 写进去了，但先写完的那个要是晚一步换内存，就会把后一个的结果盖回旧的。
        self._commit_lock = threading.Lock()
        self._transition_lock = threading.Lock()
        self._last_transition_ms = 0
        self._data: dict = {}
        self._loaded_at: Optional[float] = None
        self._failed_at: Optional[float] = None
        self._reading = False
        # 读的时候有人点了「已解决」：读回来的那份比内存里的旧，不能拿它盖掉。
        self._version = 0
        # 接了日报留痕（serve 里有 DailyReports 时打开）。没接就不往待补写里放东西——
        # 没人来补写，只会越攒越多。
        self.journaling = False

    @property
    def enabled(self) -> bool:
        return self.projects.enabled

    def snapshot(self) -> dict:
        """现在有哪些「已解决」。**从不抛**——见类注释。"""
        if not self.enabled:
            return {}
        now = self._clock()
        with self._lock:
            fresh = (self._loaded_at is not None
                     and now - self._loaded_at < panel_settings.CACHE_SECONDS)
            waiting = (self._failed_at is not None
                       and now - self._failed_at < self.RETRY_SECONDS)
            if fresh or waiting or self._reading:
                return self._live(self._data)
            self._reading = True
            first = self._loaded_at is None
            version = self._version
        if first:
            self._read(now, version)            # 手里什么都没有，只能等这一次
        else:
            self._spawn(lambda: self._read(now, version))
        with self._lock:
            return self._live(self._data)

    def _live(self, data: dict) -> dict:
        """没过期的那些。存着的过期记录不在这里删（删要写飞书），下次有人点按钮时顺手删。"""
        since = int(self._wall() * 1000) - self.KEEP_DAYS * 86_400_000
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and isinstance(v.get("at"), int) and v["at"] >= since}

    def reconcile(self, overview: summary.Overview) -> int:
        """读完表之后：问题已经消掉了的，从「已解决」记录里划掉。返回改了几条。

        overview 要是 panel.collect 读回来、**没截过**的那份（见 summary.trim_resolutions）。
        只在有东西要改时才写一次飞书。改的时候核对每条记录的 at：这期间有人
        重新点过「已解决」的，以那一次为准，不动。
        """
        if not self.enabled:
            return 0
        current = self.snapshot()
        edits = summary.trim_resolutions(overview, current)
        if not edits:
            return 0
        stamps = {key: current[key].get("at") for key in edits}
        now = int(self._wall() * 1000)

        def change(stored):
            data = dict(stored) if isinstance(stored, dict) else {}
            cleared = []
            for key, kept in edits.items():
                entry = data.get(key)
                if not isinstance(entry, dict) or entry.get("at") != stamps[key]:
                    continue
                if kept:
                    data[key] = {**entry, "reasons": kept}
                else:
                    data.pop(key)
                    if entry.get("state") == "processing" and self.journaling:
                        cleared.append((key, entry))
            # 「正在处理」被这里收掉了（问题消失），日报留痕里也得记一笔回到待处理：
            # 不然留痕里它一直是「正在处理」，过了 16 点按留痕出的日报还把它算成有人在跟，
            # 面板上却早已是待处理。「已处理」不用记：再出负面时日报自己会重新打开它。
            events = []
            for key, entry in cleared:
                table_id, _, record_id = key.partition("/")
                event = {"type": "status", "at": max(now, entry["at"] + 1),
                         "table_id": table_id, "record_id": record_id,
                         "state": "pending", "auto": True}
                if entry.get("post_key"):
                    event["post_key"] = entry["post_key"]
                events.append(event)
            if events:
                self._enqueue(data, events)
            return data

        self._commit(change)
        self.log(f"↻ 「已解决」记录里 {len(edits)} 条的问题已经消掉了，划掉"
                 "（之后再出现就重新显示）")
        return len(edits)

    def _read(self, now: float, version: int) -> None:
        try:
            raw = self.projects._store().get_json(panel_settings.KEY_RESOLVED, {})
        except Exception as exc:                                # noqa: BLE001
            with self._lock:
                self._failed_at, self._reading = now, False
            self.log(f"⚠ 读「已解决」记录失败，先按上一份来、一分钟后再试：{exc}")
            return
        with self._lock:
            if version == self._version:
                self._data = raw if isinstance(raw, dict) else {}
                self._loaded_at, self._failed_at = now, None
            self._reading = False

    def mark(self, table_id: str, record_id: str, reasons: list,
             seen: Optional[int], *, state: str = "resolved",
             at_ms: Optional[int] = None, post_key: str = "") -> None:
        """记下「这一行的这几个问题处理完了」。seen = 点的时候看到的那一次检查。"""
        key = summary.resolution_key(table_id, record_id)
        if state not in ("processing", "resolved"):
            raise ValueError("state 只认 processing / resolved")
        at = int(self._wall() * 1000) if at_ms is None else at_ms

        def change(current):
            data = self._pruned(current, at, room=1)
            data.pop(key, None)
            data[key] = {"reasons": list(reasons), "seen": seen, "at": at}
            if state == "processing":
                data[key]["state"] = state
            if post_key:
                data[key]["post_key"] = post_key
            return data

        self._commit(change)
        self.log(f"✓ {'正在处理' if state == 'processing' else '已解决'} {key}：{'、'.join(reasons)}")

    def transition(self, table_id: str, record_id: str, reasons: list,
                   seen: Optional[int], state: str, reports=None, *, post_key: str = "") -> str:
        """同一个转换串行保存显示状态和日报留痕，共用时间戳，保持并发顺序。"""
        with self._transition_lock:
            self.snapshot()
            with self._lock:
                stamped = [v.get("at", 0) for v in self._data.values() if isinstance(v, dict)]
                stamped.extend(e.get("at", 0) for e in (self._data.get(self.OUTBOX) or [])
                               if isinstance(e, dict))
            previous = max((v for v in stamped if isinstance(v, int)), default=0)
            at = max(int(self._wall() * 1000), self._last_transition_ms + 1, previous + 1)
            self._last_transition_ms = at
            if reports is None:
                if state == "pending":
                    self.unmark(table_id, record_id)
                else:
                    self.mark(table_id, record_id, reasons, seen, state=state, at_ms=at,
                              post_key=post_key)
                return ""
            key = summary.resolution_key(table_id, record_id)
            event = {"type": "status", "at": at, "table_id": table_id,
                     "record_id": record_id, "state": state}
            if post_key:
                event["post_key"] = post_key

            def change(current):
                data = self._pruned(current, at, room=int(state != "pending"))
                self._enqueue(data, [event])
                data.pop(key, None)
                if state != "pending":
                    data[key] = {"reasons": list(reasons), "seen": seen, "at": at}
                    if state == "processing":
                        data[key]["state"] = state
                    if post_key:
                        data[key]["post_key"] = post_key
                return data

            # 显示状态与待补写事件同一格原子保存。远程留痕失败后重启也能重试。
            self._commit(change)
            return self._flush_journal(reports)

    def _enqueue(self, data: dict, events: list[dict]) -> None:
        """把待补写的留痕事件放进 data 的 OUTBOX（就地改）。满了按 DROPPED 那段的规矩丢。"""
        outbox = list(data.get(self.OUTBOX) or [])
        outbox.extend(events)
        dropped = dict(data.get(self.DROPPED) or {})
        while len(outbox) > 1 and (
                len(outbox) > self.OUTBOX_MAX_EVENTS
                or len(json.dumps(outbox, ensure_ascii=False)) > self.OUTBOX_MAX_CHARS):
            index, superseded = _evictable(outbox)
            lost = outbox.pop(index)
            if (not isinstance(lost, dict) or not isinstance(lost.get("at"), int)
                    or not lost.get("table_id")):
                continue
            first, count, latest = _dropped_span(
                dropped.get(str(lost["table_id"]))) or (lost["at"], 0, 0)
            dropped[str(lost["table_id"])] = [
                min(first, lost["at"]), count + 1, latest + int(not superseded)]
        data[self.OUTBOX] = outbox
        if dropped:
            data[self.DROPPED] = dropped

    def flush_journal(self, reports) -> str:
        self.snapshot()
        with self._lock:
            unreadable = self._failed_at is not None
        if unreadable:
            reports.warning = "处理状态暂时读取失败，待补写记录可能尚未同步；日报可能不完整。"
            return reports.warning
        with self._transition_lock:
            return self._flush_journal(reports)

    def _flush_journal(self, reports) -> str:
        from . import daily_report
        with self._lock:
            events = list(self._data.get(self.OUTBOX) or [])
            dropped = dict(self._data.get(self.DROPPED) or {})
        gaps = []
        now = int(self._wall() * 1000)
        for table_id, span in sorted(dropped.items()):
            parsed = _dropped_span(span)
            if not table_id or parsed is None:
                continue
            first, count, latest = parsed
            reason = f"日报留痕积压过久，丢了 {count} 条待补写的处理状态记录"
            end = now
            if latest:
                reason += (f"，其中 {latest} 条是帖子当时的最新状态，"
                           f"之后 {self.LOST_STATE_DAYS} 天内这些帖子的状态可能不准")
                end = now + self.LOST_STATE_DAYS * 86_400_000
            gaps.extend(daily_report.gap_events(table_id, first, end, reason))
        if not events and not gaps:
            # 上一次留下的「待同步」提示到这里已经不成立了（比如之前读不到处理状态）。
            reports.warning = ""
            return ""
        try:
            reports.record_events(events + gaps)
            def acknowledge(current):
                data = dict(current) if isinstance(current, dict) else {}
                pending = [e for e in (data.get(self.OUTBOX) or []) if e not in events]
                if pending:
                    data[self.OUTBOX] = pending
                else:
                    data.pop(self.OUTBOX, None)
                if data.get(self.DROPPED) == dropped:
                    data.pop(self.DROPPED, None)
                return data
            self._commit(acknowledge)
        except Exception as exc:                                # noqa: BLE001
            warning = "状态已保存，日报留痕待同步；系统会自动重试。"
            lost = sum(parsed[1] for parsed in map(_dropped_span, dropped.values())
                       if parsed is not None)
            if lost:
                warning += f"积压过久，最早的 {lost} 条处理状态记录已丢弃，相关日报会标覆盖不完整。"
            reports.warning = warning
            self.log(f"⚠ 日报状态留痕待同步：{exc}")
            return warning
        reports.warning = ""
        return ""

    def unmark(self, table_id: str, record_id: str) -> None:
        key = summary.resolution_key(table_id, record_id)

        def change(current):
            data = self._pruned(current, int(self._wall() * 1000))
            data.pop(key, None)
            return data

        self._commit(change)
        self.log(f"↩ 撤销已解决 {key}")

    def _pruned(self, current: Any, now_ms: int, *, room: int = 0) -> dict:
        """去掉过期的、坏掉的；超过上限丢最早点的。room = 给马上要加的那条留几个位置
        （只有 mark 要留：撤销本来就是删，留了位置会白白多挤掉一条别人的）。"""
        if not isinstance(current, dict):
            return {}
        since = now_ms - self.KEEP_DAYS * 86_400_000
        kept = [(k, v) for k, v in current.items()
                if isinstance(v, dict) and isinstance(v.get("at"), int)
                and v["at"] >= since]
        kept.sort(key=lambda kv: kv[1]["at"])
        limit = self.MAX_ENTRIES - room
        kept = kept[-limit:] if limit > 0 else []
        # 超字数也丢最早点的：那一行如果还挂着问题会重新出现（多看一眼，不是少看）。
        size = len(json.dumps(dict(kept), ensure_ascii=False))
        budget = self.ENTRIES_MAX_CHARS - room * 1000
        trimmed = 0
        while kept and size > budget:
            key, value = kept.pop(0)
            size -= len(json.dumps({key: value}, ensure_ascii=False))
            trimmed += 1
        if trimmed:
            self.log(f"⚠ 「已解决」记录快到飞书单格字数上限，先收掉最早的 {trimmed} 条"
                     "（那几行要是还有问题会重新出现）")
        data = dict(kept)
        if isinstance(current.get(self.OUTBOX), list) and current[self.OUTBOX]:
            data[self.OUTBOX] = list(current[self.OUTBOX])
        if isinstance(current.get(self.DROPPED), dict) and current[self.DROPPED]:
            data[self.DROPPED] = dict(current[self.DROPPED])
        return data

    def _commit(self, change) -> None:
        with self._commit_lock:
            value = self.projects._store().update_json(
                panel_settings.KEY_RESOLVED, change, {})
            with self._lock:
                self._data = value
                self._version += 1
                self._loaded_at, self._failed_at = self._clock(), None


def _evictable(outbox: list) -> tuple[int, bool]:
    """待补写满了先丢哪一条：(位置, 是否被同一篇帖子后面的操作盖掉)。
    「同一篇」= 同一行、同一个帖子键：一行换了链接，新帖子的操作盖不掉旧帖子的
    状态。盖掉的那篇最终状态还在；都没有才丢最早的。最新那条从不丢。"""
    later: set = set()
    superseded = [False] * len(outbox)
    for index in range(len(outbox) - 1, -1, -1):
        event = outbox[index]
        identity = ((event.get("table_id"), event.get("record_id"), event.get("post_key"))
                    if isinstance(event, dict) else ("", index, None))
        superseded[index] = identity in later
        later.add(identity)
    return next(((i, True) for i, flag in enumerate(superseded) if flag), (0, False))


def _dropped_span(value: Any) -> Optional[tuple[int, int, int]]:
    """DROPPED 里一张表的 [最早丢的时刻, 丢了几条, 其中几条是最新状态]。"""
    if (isinstance(value, list) and len(value) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        return value[0], value[1], value[2]
    return None


def _clean_reasons(value: Any) -> list[str]:
    """前端传来的「当时看到的问题」。只认短字符串，去重保序。"""
    if not isinstance(value, list):
        raise ValueError("reasons 要是数组")
    out: list[str] = []
    for item in value[:12]:
        # isprintable：这几个字会进日志，不让人往终端里塞控制字符
        if isinstance(item, str) and 0 < len(item.strip()) <= 16 and item.isprintable():
            out.append(item.strip())
    return list(dict.fromkeys(out))


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
    # 算「哪条通道下线了」要用 channels.order（CHANNEL_ORDER 可以改它），
    # 所以这一层得拿得到 settings。留 None 是给只测渲染的用例兜底。
    settings: Optional[Settings] = None
    logs: Optional["LogFeed"] = None
    balances: Optional["BalanceFeed"] = None
    projects: Optional["Projects"] = None
    queueing: Optional["Queueing"] = None
    resolutions: Optional["Resolutions"] = None
    reports: Optional["DailyReports"] = None
    report_drafts: Optional["report_ai_mod.ReportDrafts"] = None
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
        if path == "/api/daily-report":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            reports = self.deps.reports
            if reports is None:
                return self._send_json(200, {"reports": [], "error": "日报留痕尚未接入；需配置 FEISHU_REGISTRY"})
            query = parse_qs(urlparse(self.path).query)
            try:
                overview, _, _ = self._cached_view()
                return self._send_json(200, reports.get(
                    overview, date=(query.get("date") or [""])[0],
                    table_id=(query.get("table_id") or [""])[0]))
            except ValueError as exc:
                return self._send_json(400, {"reports": [], "error": str(exc)})
            except Exception as exc:                            # noqa: BLE001
                return self._send_json(503, {"reports": [], "error": f"读取日报失败：{exc}"})
        if path == "/api/logs":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._send_json(200, self._logs_payload())
        if path == "/api/projects":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._send_json(200, self._projects_payload())
        if path == "/api/chats":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._chats_payload()
        if path == "/api/share":
            if not self._authed():
                return self._send_json(401, {"error": "未登录"})
            return self._share_payload()
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _share_payload(self):
        """新建表给谁开权限：环境变量里的 + 面板上点出来的。只读。"""
        projects = self.deps.projects
        if projects is None or not projects.enabled:
            return self._send_json(400, {
                "error": projects.why_disabled() if projects else "面板没配 FEISHU_APP_ID"})
        try:
            return self._send_json(200, {"share": projects.share_state()})
        except feishu.FeishuError as exc:
            return self._send_json(400, {"error": exc.msg, "hint": exc.hint})
        except Exception as exc:                                # noqa: BLE001
            return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _chats_payload(self):
        """应用所在的群和 chat_id。只读；是给填 FEISHU_TABLE_EDITOR_CHATS 找值用的。"""
        projects = self.deps.projects
        if projects is None or not self.deps.config.app_id:
            return self._send_json(400, {"error": "面板没配 FEISHU_APP_ID"})
        try:
            chats = projects.list_chats()
        except feishu.FeishuError as exc:
            return self._send_json(400, {
                "error": f"列群失败：{exc.msg}",
                "hint": "多半是应用没开 im:chat:readonly（读群信息）权限：开放平台 → "
                        "权限管理 → 搜 im:chat → 开通，然后发布一个新版本。"})
        except Exception as exc:                                # noqa: BLE001
            return self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send_json(200, {"chats": chats})

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
        if path == "/api/report-ai":
            return self._report_ai_action()
        if path == "/api/queue":
            return self._queue_action()
        if path == "/api/resolve":
            return self._resolve_action()
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

    def _report_ai_action(self):
        """按需生成单项目日报草稿。AI 调用是 POST + CSRF，不进入任何轮询。"""
        if self.deps.reports is None:
            return self._send_json(400, {"error": "日报留痕尚未接入；需配置 FEISHU_REGISTRY"})
        if self.deps.report_drafts is None:
            return self._send_json(200, {
                "status": "unavailable", "error": "未配置 DEEPSEEK_API_KEY",
                "features": "", "competitors": []})
        body = self._read_body()
        if body is None:
            return None
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            date = payload.get("date") or ""
            table_id = payload.get("table_id") or ""
            force = payload.get("force", False)
            if not isinstance(date, str) or len(date) > 10:
                raise ValueError("date 格式不正确")
            if not isinstance(table_id, str) or not _ID_RE.fullmatch(table_id):
                raise ValueError("请选择一个具体项目")
            if not isinstance(force, bool):
                raise ValueError("force 必须是布尔值")
            overview, _, _ = self.deps.cache.view()
            # 日报快照自己套「已解决」，要原样那份；挑预警标题要套过的那份，
            # 处理完收起来的帖子不再当本期问题送给模型。
            shown = overview
            if overview is not None and self.deps.resolutions is not None:
                shown = summary.apply_resolutions(
                    overview, self.deps.resolutions.snapshot(),
                    self.deps.settings or Settings())
            result = self.deps.report_drafts.generate(
                overview, date=date, table_id=table_id, force=force, shown=shown)
            return self._send_json(200, result)
        except ValueError as exc:
            return self._send_json(400, {"error": str(exc)})
        except report_ai_mod.ReportAIError as exc:
            return self._send_json(502, {"error": str(exc), "status": "error"})
        except Exception:                                       # noqa: BLE001
            return self._send_json(500, {"error": "AI 草稿生成失败，请稍后重试", "status": "error"})

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
                ok = valid_session(self.deps.config, value.strip())
                if ok and not self._polling():
                    # 登录后的请求都算「有人在看」，后台取数照常（见 Cache）。页面
                    # 自己定时拉的不算：开着不动的标签页不能让后台整夜整表读。
                    self.deps.cache.touch()
                return ok
        return False

    def _polling(self) -> bool:
        """页面定时器发的请求（X-Panel-Poll: 1），不是人点的。"""
        headers = getattr(self, "headers", None)
        return headers is not None and headers.get("X-Panel-Poll") == "1"

    def _cached_view(self):
        """定时轮询只拿现成的快照，不记「有人在看」、不叫醒停下的后台取数。"""
        return self.deps.cache.snapshot() if self._polling() else self.deps.cache.view()

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
    def _view(self):
        """缓存里那份快照，按「已解决」收掉该收的行、再按条数上限截（顺序见
        summary.for_display）。缓存里的那份不动。"""
        overview, error, fetched_at = self._cached_view()
        if overview is not None:
            resolutions = self.deps.resolutions
            resolved = resolutions.snapshot() if resolutions is not None else {}
            overview = summary.for_display(overview, resolved,
                                           self.deps.settings or Settings())
        return overview, error, fetched_at

    def _page(self) -> str:
        for feed in (self.deps.logs, self.deps.balances):
            if feed is not None and feed.enabled:
                feed.prime()
        overview, error, fetched_at = self._view()
        runs, log_error = self._runs()
        balances, balance_error = self._balances()
        resolutions = self.deps.resolutions
        return self.deps.render_page(
            overview=overview, error=error, fetched_at=fetched_at,
            config=self.deps.config, runs=runs, log_error=log_error,
            balances=balances, balance_error=balance_error,
            runway=runway_from_runs(balances, runs),
            outages=self._outages(balances),
            resolvable=bool(resolutions and resolutions.enabled),
            csrf=csrf_token(self.deps.config, self._session_token()))

    def _outages(self, balances) -> list:
        """哪条通道此刻发不出请求了。算不出就当没有——**绝不猜**。

        这一步不花钱：余额是 BalanceFeed 早取好的，其余全是本地常量。
        """
        settings = self.deps.settings
        if settings is None:
            return []
        return channels_mod.outages(balances, settings, self.deps.config.api_keys)

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

    def _resolve_action(self):
        """「已解决」/ 撤销。**不写业务表**——只写注册表 base 里的「面板设置」
        （见 Resolutions）。不花钱，也不碰飞书表里那一行。"""
        resolutions = self.deps.resolutions
        if resolutions is None or not resolutions.enabled:
            projects = self.deps.projects
            why = projects.why_disabled() if projects else "面板没配 FEISHU_APP_ID"
            return self._send_json(400, {"error": f"「已解决」要存在注册表那个 base 里，现在存不了：{why}"})
        body = self._read_body()
        if body is None:
            return None
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("要是一个对象")
            table_id = str(payload.get("table_id") or "")
            record_id = str(payload.get("record_id") or "")
            if not (_ID_RE.match(table_id) and _ID_RE.match(record_id)):
                raise ValueError("table_id / record_id 不对")
            state = "pending" if payload.get("undo") is True else payload.get("state", "resolved")
            if state not in ("pending", "processing", "resolved"):
                raise ValueError("state 只认 pending / processing / resolved")
            undo = state == "pending"
            reasons = [] if undo else _clean_reasons(payload.get("reasons"))
            if not undo and not reasons:
                raise ValueError("reasons 不能是空的")
            seen = payload.get("checked_at_ms")
            if seen is not None and (isinstance(seen, bool) or not isinstance(seen, int)):
                raise ValueError("checked_at_ms 要是整数")
        except (ValueError, TypeError) as exc:
            return self._send_json(400, {"error": f"请求体不对：{exc}"})
        # 只操作服务端完整快照中确有的记录；不信任浏览器伪造的理由或检查时刻。
        overview, error, _ = self.deps.cache.view()
        if overview is None:
            return self._send_json(409, {"error": error or "监控数据还未就绪，请稍后再试"})
        project = next((p for p in overview.projects if p.table_id == table_id), None)
        if project is None or project.error:
            return self._send_json(409, {"error": "项目已移除或暂时读取失败，请刷新后再试"})
        todo = next((t for t in (*project.todos, *project.adhoc_todos)
                     if t.record_id == record_id), None)
        stored = resolutions.snapshot().get(summary.resolution_key(table_id, record_id))
        if todo is None and not (undo and isinstance(stored, dict)):
            return self._send_json(409, {"error": "这条记录已不在待办中，请刷新后再试"})
        if not undo:
            if (set(reasons) != set(todo.reasons)
                    or (seen is not None and seen != todo.checked_at_ms)):
                return self._send_json(409, {"error": "扫描结果已更新，请查看最新问题后再标记"})
            reasons, seen = todo.reasons, todo.checked_at_ms
        try:
            warning = resolutions.transition(table_id, record_id, reasons, seen, state,
                                              self.deps.reports,
                                              post_key=todo.post_key if todo else "")
        except feishu.FeishuError as exc:
            return self._send_json(400, {"error": f"没存上：{exc.msg}", "hint": exc.hint})
        except Exception as exc:                                # noqa: BLE001
            return self._send_json(500, {"error": f"没存上：{type(exc).__name__}: {exc}"})
        return self._send_json(200, {"ok": True, "undo": undo, "state": state,
                                     "work_stats": self._work_stats(), "warning": warning})

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
        if action == "share_chats":
            chats = payload.get("chats")
            if not isinstance(chats, list):
                raise ValueError("chats 要是数组")
            return {"ok": True, "share": projects.set_editor_chats(chats)}
        # 刻意**没有** share_manager 这个动作：可管理的人只在后台环境变量里定
        # （见 Projects 里那段说明），面板上不能加。
        if action == "share_apply":
            result = projects.apply_share(text("app_token"))
            return {"granted": result.granted, "failures": result.failures,
                    "ok": not result.failures}
        if action == "build":
            result = projects.build(text("app_token"), text("table_id"))
            return {"summary": result.summary(), "created": result.created,
                    "options_added": result.options_added,
                    "skipped_options": result.skipped_options,
                    "failures": result.failures, "ok": result.ok,
                    "manual_steps": result.manual_steps}
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
        overview, error, fetched_at = self._view()
        return {
            "error": error,
            "fetched_at": fetched_at,
            "projects": [_project_json(p) for p in (overview.projects if overview else [])],
            "todos": [_todo_json(t) for t in (overview.todos() if overview else [])],
            "resolved": [_todo_json(t) for t in (overview.resolved if overview else [])],
            "archived_todos": [_todo_json(t) for t in (overview.archived_todos() if overview else [])],
            "work_stats": self._work_stats(),
            "revision": self.deps.resolutions._version if self.deps.resolutions else 0,
            "todos_hidden": (overview.todos_dropped + overview.todos_dropped_by()
                              if overview else 0),
        }

    def _work_stats(self) -> list[dict]:
        overview, _, _ = self._cached_view()
        if overview is None:
            return []
        resolutions = self.deps.resolutions
        return summary.work_stats(overview, resolutions.snapshot() if resolutions else {},
                                  self.deps.settings or Settings())


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
        "label": p.label, "table_id": p.table_id, "app_token": p.app_token,
        "table_url": p.table_url, "error": p.error,
        "health": p.health, "total_rows": p.total_rows,
        "archived_rows": p.archived_rows, "queued_rows": p.queued_rows,
        "due_rows": p.due_rows, "due_yuan": round(p.due_yuan, 2),
        "estimate_blocked": p.estimate_blocked,
        "stale_rows": p.stale_rows, "never_checked_rows": p.never_checked_rows,
        "missing_publish_time_rows": p.missing_publish_time_rows,
        "oldest_checked_ms": p.oldest_checked_ms,
        "refresh_status_counts": p.refresh_status_counts,
        "traffic_tag_counts": p.traffic_tag_counts,
        "negative_rows": p.negative_rows, "pin_lost_rows": p.pin_lost_rows,
        "recollect_rows": p.recollect_rows,
        "seed_keyword_rows": p.seed_keyword_rows,
        "negative_keyword_rows": p.negative_keyword_rows,
        "needs_attention": p.needs_attention,
    }


def _todo_json(t: summary.TodoRow) -> dict:
    return {
        "record_id": t.record_id, "project": t.project, "record_url": t.record_url,
        "app_token": t.app_token, "table_id": t.table_id, "key": t.key,
        "link_cell": t.link_cell, "reasons": t.reasons,
        "label": t.label, "persona": t.persona, "post_url": t.post_url,
        "post_key": t.post_key,
        "archived": t.archived, "adhoc": t.adhoc, "recent": t.recent,
        "refresh_status": t.refresh_status, "diagnosis": t.diagnosis,
        "comment_count": t.comment_count, "traffic_tags": t.traffic_tags,
        "seed_keywords": t.seed_keywords, "negative_keywords": t.negative_keywords,
        "checked_at_ms": t.checked_at_ms, "queued": t.queued,
        "digest": t.digest, "negative_digest": t.negative_digest,
        "work_state": t.work_state, "working_at_ms": t.working_at_ms,
        "resolved_at_ms": t.resolved_at_ms,
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
                 settings: Optional[Settings] = None,
                 resolutions: Optional[Resolutions] = None,
                 reports: Optional["DailyReports"] = None,
                 report_drafts: Optional["report_ai_mod.ReportDrafts"] = None,
                 render_login=None, render_page=None,
                 host: str = "0.0.0.0") -> PanelServer:
    from . import panel_view
    deps = _Deps(
        config=config, cache=cache, settings=settings, logs=logs,
        balances=balances, projects=projects, queueing=queueing,
        resolutions=resolutions, reports=reports, report_drafts=report_drafts,
        render_login=render_login or panel_view.login_page,
        render_page=render_page or panel_view.overview_page,
    )
    return PanelServer((host, config.port), deps)


def reconciling(produce: Callable[[], summary.Overview], resolutions: Resolutions,
                log: Callable[[str], None] = print) -> Callable[[], summary.Overview]:
    """每次读完表，顺手把「已解决」里已经消掉的问题划掉（Resolutions.reconcile）。
    这一步失败只记一句日志，不能让面板读不出数。"""
    def run() -> summary.Overview:
        overview = produce()
        if resolutions.enabled:
            try:
                resolutions.reconcile(overview)
            except Exception as exc:                            # noqa: BLE001
                log(f"⚠ 收拾「已解决」记录失败（不影响面板，下一轮再试）：{exc}")
        return overview
    return run


def serve(config: PanelConfig, produce: Callable[[], summary.Overview],
          settings: Optional[Settings] = None) -> int:
    """起面板。阻塞到进程被杀。"""
    resolved = settings or Settings()
    projects = Projects(config, resolved)
    resolutions = Resolutions(projects)
    reports = DailyReports(projects) if projects.enabled else None
    if reports is not None:
        reports.resolutions = resolutions
        resolutions.journaling = True
    report_drafts = (report_ai_mod.ReportDrafts(
        config.deepseek_api_key, reports, timeout=config.deepseek_timeout,
        alert_reason=resolved.negative_status.found)
        if reports is not None and config.deepseek_api_key else None)
    cache = Cache(reconciling(produce, resolutions), config.cache_seconds,
                  idle_seconds=config.idle_minutes * 60 or None)
    logs = LogFeed(config, config.cache_seconds)
    balances = BalanceFeed(config, config.cache_seconds)
    queueing = Queueing(config, resolved) if config.app_id else None
    server = build_server(config, cache, logs=logs, balances=balances,
                          projects=projects, queueing=queueing,
                          settings=resolved, resolutions=resolutions, reports=reports,
                          report_drafts=report_drafts)
    cache.start()
    # 日志和余额也先在后台取一趟：第一个打开面板的人不用等 Railway 和两家余额接口。
    if logs.enabled:
        logs.prime()
    if balances.enabled:
        balances.prime()
    if resolutions.enabled:
        # 先把「已解决」读进来，第一个打开面板的人就不用等这一趟飞书。
        threading.Thread(target=resolutions.snapshot, daemon=True).start()
    print(f"面板已启动：0.0.0.0:{config.port}"
          f"（缓存 {config.cache_seconds:.0f} 秒刷一次，"
          + (f"{config.idle_minutes:g} 分钟没人看就停，有人来先取一趟新的，"
             if config.idle_minutes else "一直刷，")
          + f"评论正文{'展示' if config.show_digest else '不展示'}）", flush=True)
    if logs.enabled:
        print("Railway 日志：已接入", flush=True)
    else:
        print(f"Railway 日志：未接入（差 "
              f"{'、'.join(config.railway.missing())}）", flush=True)
    if projects.enabled:
        print("在面板上加表删表：已就绪", flush=True)
    else:
        print(f"在面板上加表删表：未就绪（{projects.why_disabled()}）", flush=True)
    print("⚠ 这个进程不发巡检付费请求：要刷新就在飞书表里勾「排队刷新」，"
          "由 cron 那个服务处理", flush=True)
    print("DeepSeek 日报草稿：" + ("已接入 deepseek-flash（仅打开单项目日报时调用）"
          if report_drafts is not None else "未配置 DEEPSEEK_API_KEY"), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n收到中断，关闭面板", flush=True)
    finally:
        cache.stop()
        server.server_close()
    return 0
