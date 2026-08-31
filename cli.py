#!/usr/bin/env python3
"""命令行入口。

    python3 cli.py doctor              # 体检：不花积分，检查配置/权限/字段是否齐全
    python3 cli.py sweep               # 分层巡检：只刷到期的行
    python3 cli.py queue               # 只刷勾了「排队刷新」的行
    python3 cli.py row <record_id>...  # 刷指定行（无视冷却和分层节流）
    python3 cli.py estimate            # 只估算这一轮要花多少钱，不发请求
    python3 cli.py serve               # 起监控面板（常驻、只读，一分钱不花）
    python3 cli.py init-registry       # 建那张存表清单的飞书表（这辈子跑一次）

多表：设 FEISHU_TABLES 一次巡查多张表（见 .env.example），上面每个命令都会
逐表执行；`--table 标签` 可以只跑其中某几张（逗号分隔）。
单表继续用 FEISHU_APP_TOKEN + FEISHU_TABLE_ID，行为不变。

配置走环境变量，见 .env.example。
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from xhsearch import (feishu, providers, rows as rows_mod, runlock, runner,
                      schema, tablespec)
from xhsearch.config import Budget, Channels, Settings
from xhsearch.envfile import load_dotenv


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        sys.exit(f"缺少环境变量 {name}（参考 .env.example）")
    return value


def _numeric_env(name: str, cast, default, *, minimum=None, maximum=None):
    """数值型环境变量，带合法区间。

    越界**拒绝**而不是静默 clamp：clamp 会让人以为自己设的值生效了，
    然后按一个从没生效过的配置去解释运行结果。

    没有边界检查时，这里接受过 `MAX_CONCURRENCY=1000`（线程/FD 耗尽 +
    把供应商限流打出来）和 `SOFT_DEADLINE_SECONDS=-1`（每一行都立刻
    「留待下一轮」，任务表面正常、实际一行都不刷）。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        sys.exit(f"环境变量 {name} 的值 {raw!r} 不是数字（参考 .env.example）")
    if value != value or value in (float("inf"), float("-inf")):
        sys.exit(f"环境变量 {name} 的值 {raw!r} 不是有限数字")
    if minimum is not None and value < minimum:
        sys.exit(f"环境变量 {name} 的值 {raw} 太小，合法区间是 "
                 f"[{minimum}, {maximum if maximum is not None else '∞'}]")
    if maximum is not None and value > maximum:
        sys.exit(f"环境变量 {name} 的值 {raw} 太大，合法区间是 "
                 f"[{minimum if minimum is not None else '-∞'}, {maximum}]")
    return value


def _apply_endpoint_overrides() -> None:
    """按部署位置切 TikHub 的接入域名。

    对方文档要求「请勿跨区使用，会影响访问速度」：
        境内（国内 VPS）        → api.tikhub.dev（默认，不用设）
        境外（Railway、Actions）→ TIKHUB_BASE=https://api.tikhub.io

    只放行 HTTPS + 官方域名：改 base 等于改「API Key 发到哪台机器」，
    一个拼错的域名就是把生产 Key 明文送给别人。真要指到自建代理，
    显式设 ALLOW_UNSAFE_ENDPOINT_OVERRIDE=1。
    """
    unsafe = os.environ.get("ALLOW_UNSAFE_ENDPOINT_OVERRIDE", "").strip() in ("1", "true", "yes")
    raw = os.environ.get("TIKHUB_BASE", "")
    try:
        providers.set_tikhub_base(raw, allow_unsafe=unsafe)
    except providers.EndpointRejected as exc:
        sys.exit(f"TIKHUB_BASE 被拒绝：{exc}")
    if unsafe and raw.strip():
        print(f"⚠️⚠️ 已允许非官方 TikHub 端点：{providers.TIKHUB_BASE}"
              "——API Key 会发到这个地址，确认这是你自己的机器")


def load_env_or_exit() -> None:
    """读 .env，**看不懂的行当场拒跑**。

    本地跑时把仓库根的 .env 补进环境变量（已存在的环境变量优先）。
    Railway / Actions 由平台注入，这一步是空操作。

    为什么是拒跑而不是提示：严格解析器只有在调用方真的去看 issues 时才有意义。
    悄悄跳过一行畸形配置，产生的是一个「看起来配好了、实际值不对」的部署——
    比如 `MAX_YUAN_PER_RUN="10`（引号没配对）会被跳过，于是这一轮按
    「不限金额」跑，而运维以为自己设了上限。这正是这套严格解析要消灭的东西。
    """
    issues: list[str] = []
    load_dotenv(issues=issues)
    if issues:
        print("❌ .env 里有看不懂的行，已拒绝启动（怕你以为配好了、其实没生效）：")
        for problem in issues:
            print(f"  · {problem}")
        sys.exit("修好这些行再跑；语法说明见 .env.example 和 xhsearch/envfile.py")


def apply_pricing_overrides() -> None:
    """计价参数的环境变量覆盖。供应商改价时不用改代码重新发版。

    公开（不带下划线）是给 tools/estimate_cost.py 用的：两个入口必须共用
    同一份覆盖逻辑，否则运维改完价格之后，生产预算用新价、
    月度成本规划脚本还在报编译进代码的旧价。
    """
    overrides = {}
    if os.environ.get("TIKHUB_USD_XHS", "").strip():
        overrides["xhs"] = _numeric_env("TIKHUB_USD_XHS", float, None, minimum=0.0000001)
    if os.environ.get("TIKHUB_USD_DOUYIN", "").strip():
        overrides["douyin"] = _numeric_env("TIKHUB_USD_DOUYIN", float, None, minimum=0.0000001)
    try:
        providers.set_pricing(
            usd_to_cny=(_numeric_env("USD_TO_CNY", float, None, minimum=0.01, maximum=100.0)
                        if os.environ.get("USD_TO_CNY", "").strip() else None),
            tikhub_usd=overrides or None,
            socialdatax_yuan=(_numeric_env("SOCIALDATAX_YUAN", float, None, minimum=0.0000001)
                              if os.environ.get("SOCIALDATAX_YUAN", "").strip() else None),
        )
    except ValueError as exc:
        sys.exit(f"计价配置不合法：{exc}")


def _api_keys() -> dict[str, str]:
    """双通道的凭据。两个都配就自动开降级；只配一个也能跑。

    环境变量名和供应商名一一对应，加第三家时不用改这里的结构。
    """
    keys = {
        providers.TIKHUB: os.environ.get("TIKHUB_API_KEY", "").strip(),
        providers.SOCIALDATAX: os.environ.get("SOCIALDATAX_API_KEY", "").strip(),
    }
    return {k: v for k, v in keys.items() if v}


# 解析和校验都在 xhsearch/tablespec.py（注册表和面板要用同一套：报错文案不能漂、
# 安全校验不能漏）。这两个名字保留成别名，旧调用点和旧测试不用改。
valid_token = tablespec.valid_token
_TOKEN_RE = tablespec._TOKEN_RE


def _tables_from_env(environ) -> list:
    """解析要巡查的表清单，返回 `[tablespec.TableTarget, ...]`。

    返回 TableTarget 而不是三元组，是为了**保住 `route`**（原链接走的是
    /base/ 还是 /wiki/）。接口两种 token 通用，浏览器地址不通用——丢了它，
    用 wiki 链接登记的项目在面板上「去这一行」会指向打不开的地址，
    而行级直达是这个面板一半的价值。

    多表用 FEISHU_TABLES，**分号或换行**分隔，每一项的写法见
    `tablespec.parse_target`。标签用在日志分节和 `--table` 筛选上。

    单表继续用 FEISHU_APP_TOKEN + FEISHU_TABLE_ID；两种都配了以
    FEISHU_TABLES 为准。所有表共用同一个飞书应用（App ID/Secret），
    应用要逐张表「添加文档应用」授权。

    **这里的失败一律 `sys.exit`**：环境变量是部署方填的，填错了整个进程就该
    起不来。注册表那条路不一样——那是运营填的，一行错不该让其余表停摆，
    所以它自己 catch `BadTarget` 把那一行标成配置有误（见 xhsearch/registry.py）。
    """
    spec = environ.get("FEISHU_TABLES", "").strip()
    if spec:
        try:
            targets = tablespec.parse_many(spec)
        except tablespec.BadTarget as exc:
            sys.exit(f"FEISHU_TABLES 里有一项解析不了：{exc}（参考 .env.example）")
        if not targets:
            sys.exit("FEISHU_TABLES 设了但一张表都没解析出来，检查格式（参考 .env.example）")
        problem = tablespec.find_duplicate(targets)
        if problem:
            sys.exit(f"FEISHU_TABLES 里 {problem}")
        return targets

    app_token = environ.get("FEISHU_APP_TOKEN", "").strip()
    table_id = environ.get("FEISHU_TABLE_ID", "").strip()
    if not app_token or not table_id:
        sys.exit("没配任何表：多表设 FEISHU_TABLES，单表设 "
                 "FEISHU_APP_TOKEN + FEISHU_TABLE_ID（参考 .env.example）")
    for what, token in (("FEISHU_APP_TOKEN", app_token), ("FEISHU_TABLE_ID", table_id)):
        if not tablespec.valid_token(token):
            sys.exit(f"{what} 不合法：{token!r}。飞书的 token 只会是字母和数字")
    return [tablespec.TableTarget(table_id, app_token, table_id)]


def _registry_table(app_id: str, app_secret: str) -> feishu.Bitable | None:
    """FEISHU_REGISTRY 指向的那张表，没配返回 None。"""
    spec = os.environ.get("FEISHU_REGISTRY", "").strip()
    if not spec:
        return None
    try:
        target = tablespec.parse_target(spec, default_label="registry")
    except tablespec.BadTarget as exc:
        sys.exit(f"FEISHU_REGISTRY 看不懂：{exc}")
    return feishu.Bitable(app_id=app_id, app_secret=app_secret,
                          app_token=target.app_token, table_id=target.table_id)


class NoTables(RuntimeError):
    """一张能巡查的表都没有。`str(exc)` 是可以直接给人看的中文。"""


def _entries_or_raise(app_id: str, app_secret: str, *,
                      allow_empty: bool = False) -> list:
    """表清单（`TableTarget` 列表）：优先注册表，读不到就退回 FEISHU_TABLES
    并**大声警告**。走不通时 `raise NoTables`，不 `sys.exit`。

    ⚠️ **绝不静默降级成零张表。** 那种失败长这样：进程正常退出、日志一切
    正常、退出码 0，而实际一行都没刷。等有人发现的时候已经过去几天了。

    `allow_empty` 只给 `serve` 用：面板存在的意义之一就是**在上面加第一张
    表**，而刚 `init-registry` 出来的注册表是空的。在这条路上拒绝启动，
    等于「要用面板加表，得先有表」——那一整块功能根本走不到。
    停用最后一个项目之后同样再也起不来。付费的那几条命令（sweep / queue /
    estimate）不给这个开关，零表照旧拒跑。

    抛异常而不是 `sys.exit`：面板的后台刷新线程会调它，而 `sys.exit` 在
    子线程里只是悄悄杀掉那个线程——页面会一直显示上一份快照，看着一切正常。
    """
    registry = _registry_table(app_id, app_secret)
    if registry is None:
        _REGISTRY_ROWS.clear()
        return _tables_from_env(os.environ)

    from xhsearch import registry as registry_mod
    try:
        entries = registry_mod.read(registry)
    except Exception as exc:                                    # noqa: BLE001
        print(f"⚠ 读不到注册表（{exc}）")
        if os.environ.get("FEISHU_TABLES", "").strip() or \
                os.environ.get("FEISHU_APP_TOKEN", "").strip():
            print("  退回环境变量里的表清单。**这份清单可能是旧的**——"
                  "在注册表里停用过的表会在这一轮复活并花钱，注意看下面刷了哪些表")
            # 上一次读到的逐表阈值一并丢掉。cron 每轮是全新进程，读不到注册表
            # 就是「没有逐表覆盖」；面板是常驻的，留着上一次的会让它和 cron
            # 用不同的口径算同一张表——而面板正是用来看这件事的。
            _REGISTRY_ROWS.clear()
            return _tables_from_env(os.environ)
        raise NoTables("  而且没有 FEISHU_TABLES 可以兜底。本轮拒跑——"
                       "静默跑成「零张表」比报错难发现得多") from exc

    for entry in entries:
        if entry.problem:
            print(f"⚠ 注册表里「{entry.label or entry.record_id}」这一行有问题，"
                  f"本轮跳过：{entry.problem}")
    _REGISTRY_ROWS.clear()
    _REGISTRY_ROWS.update({e.table_id: e for e in entries if e.usable})
    usable = registry_mod.to_targets(entries)
    if not usable and not allow_empty:
        raise NoTables(f"注册表里没有一张可用的表（共 {len(entries)} 行，"
                       "要么没勾「启用」，要么配置有误）。本轮拒跑")
    return usable


def _entries(app_id: str, app_secret: str) -> list:
    """`_entries_or_raise` 的 `sys.exit` 版。付费命令走这条。"""
    try:
        return _entries_or_raise(app_id, app_secret)
    except NoTables as exc:
        sys.exit(str(exc))


# 注册表里那一行，按 table_id 索引。逐表阈值从这儿来；没用注册表时是空的。
_REGISTRY_ROWS: dict[str, Any] = {}


def _tables(selected: list[str] | None = None) -> list[tuple[str, feishu.Bitable]]:
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    return _bitables(_env_filtered(_entries(app_id, app_secret), selected),
                     app_id, app_secret)


def _env_filtered(targets: list, selected: list[str] | None) -> list:
    """按 `--table` 筛。指定了不存在的标签就退出——静默跑成别的表更糟。"""
    if not selected:
        return targets
    by_label = {t.label: t for t in targets}
    missing = [s for s in selected if s not in by_label]
    if missing:
        sys.exit(f"--table 指定的表不存在：{'、'.join(missing)}。"
                 f"可选：{'、'.join(t.label for t in targets)}")
    return [by_label[s] for s in selected]


def _bitables(targets: list, app_id: str, app_secret: str
              ) -> list[tuple[str, feishu.Bitable]]:
    return [(t.label, feishu.Bitable(app_id=app_id, app_secret=app_secret,
                                     app_token=t.app_token,
                                     table_id=t.table_id, route=t.route))
            for t in targets]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    sys.exit(f"环境变量 {name} 的值 {raw!r} 看不懂，填 1/0（或 true/false）")


def build_settings() -> Settings:
    _apply_endpoint_overrides()
    apply_pricing_overrides()
    settings = Settings()
    # 独立服务跑批量不需要软截止（那是给有执行时限的运行时准备的）。
    # 负数会让每一行立刻「留待下一轮」——一个看起来在跑、实际一行都不刷的任务。
    settings.soft_deadline_seconds = _numeric_env(
        "SOFT_DEADLINE_SECONDS", float, 0.0, minimum=0.0, maximum=86400.0)
    # 上限 3 是 SocialDataX 官方 skill 的明文要求（最多 3 并发，不要突发请求）。
    settings.max_concurrency = _numeric_env(
        "MAX_CONCURRENCY", int, settings.max_concurrency, minimum=1, maximum=3)
    settings.detail_within_days = _numeric_env(
        "DETAIL_WITHIN_DAYS", int, settings.detail_within_days, minimum=0, maximum=3650)

    # —— 单轮硬预算（0 = 不限）——
    settings.budget = Budget(
        max_records_per_run=_numeric_env("MAX_RECORDS_PER_RUN", int, 0, minimum=0),
        max_calls_per_run=_numeric_env("MAX_CALLS_PER_RUN", int, 0, minimum=0),
        max_yuan_per_run=_numeric_env("MAX_YUAN_PER_RUN", float, 0.0, minimum=0.0),
    )

    # —— 评论区快照的数据最小化开关 ——
    settings.digest.show_author_name = _bool_env(
        "DIGEST_SHOW_AUTHOR_NAME", settings.digest.show_author_name)
    settings.digest.show_ip_location = _bool_env(
        "DIGEST_SHOW_IP_LOCATION", settings.digest.show_ip_location)

    # —— 日志里时间按哪个时区打印（只影响打印，见 config.Display）——
    # 默认 +8：飞书国内租户就是按北京时间渲染「最近检查时间」的，
    # 而容器日志的时间戳是 UTC——不对齐的话两边差 8 小时，谁看都对不上。
    settings.display.utc_offset_hours = _numeric_env(
        "DISPLAY_UTC_OFFSET", float, settings.display.utc_offset_hours,
        minimum=-12.0, maximum=14.0)

    # —— 最低热度档「观察中」的名字；留空 = 关掉这一档 ——
    # 关掉之后冷启动窗口内的新帖又会一个标签都不打（`流量状态` 留空），
    # 所以默认开着。改名要**同时**去飞书把多选选项建好，否则机器写不进去
    # （会被安全跳过并在「诊断信息」里说明，不会写坏表）。
    raw_observing = os.environ.get("TAG_OBSERVING")
    if raw_observing is not None:
        default_observing = settings.tags.observing
        settings.tags.observing = raw_observing.strip()
        if settings.tags.observing != default_observing:
            # 改名或关掉，都要把**旧名字**留在机器命名空间里，下一轮才摘得掉。
            # 漏了这一条，已经写出去的「观察中」就掉到命名空间外面，
            # 被 merge 当成人工标签保护起来——于是一行会同时挂着「观察中」和
            # 改名后的（或升上去的）档位，两个热度档并排，棘轮形同虚设。
            settings.tags.retired = tuple(
                dict.fromkeys((*settings.tags.retired, default_observing)))

    # CHANNEL_ORDER="xhs=tikhub,socialdatax; douyin=tikhub"
    # 想把某个平台钉死在一家时用，不改代码。
    # 在**默认配置的基础上合并**：只写 douyin 就只改 douyin，
    # 没提到的平台保持默认双通道——整体替换会让缺席的平台静默退回
    # socialdatax 单通道，既贵又丢掉降级能力，且没有任何提示。
    spec = os.environ.get("CHANNEL_ORDER", "").strip()
    if spec:
        order = dict(Channels().order)
        for chunk in spec.split(";"):
            if not chunk.strip():
                continue
            if "=" not in chunk:
                sys.exit(f"CHANNEL_ORDER 里这一段看不懂：{chunk.strip()!r}。"
                         "格式：平台=第一家,第二家；平台=……（参考 .env.example）")
            platform, names = chunk.split("=", 1)
            platform = platform.strip().lower()
            if platform not in ("xhs", "douyin"):
                sys.exit(f"CHANNEL_ORDER 里的平台 {platform!r} 不认识，可选：xhs、douyin")
            picked = [n.strip().lower() for n in names.split(",") if n.strip()]
            for name in picked:
                if name not in providers.REGISTRY:
                    sys.exit(f"CHANNEL_ORDER 里的供应商 {name!r} 不存在，"
                             f"可选：{'、'.join(sorted(providers.REGISTRY))}")
            if len(set(picked)) != len(picked):
                # 同一家写两遍 = 可降级错误发生后再打它一次，白花一次钱
                # 拿到同一个答案。这是配置笔误，报出来而不是默默去重。
                sys.exit(f"CHANNEL_ORDER 里 {platform} 的通道有重复："
                         f"{'、'.join(picked)}——同一家排两遍会重复花钱")
            if picked:
                order[platform] = picked
        settings.channels = Channels(order=order)
    return settings


# 表结构的期望值和体检判据都在 xhsearch/schema.py 里（面板也要用，
# 留在这里就是循环依赖）。这几个名字保留成别名：调用点和测试都不用改，
# 而「唯一一份定义」的性质没变。
_FIELD_TYPE_NAMES = schema.FIELD_TYPE_NAMES
_EXOTIC_UI_TYPES = schema.EXOTIC_UI_TYPES
_type_name = schema.type_name
_expected_schema = schema.expected_schema
_schema_problems = schema.schema_problems
_options_from_meta = schema.options_from_meta


def _doctor_table(settings: Settings, table: feishu.Bitable) -> int:
    """体检一张表（①token ②列名/类型/选项 ③试读），就地打印问题，返回问题数。"""
    f = settings.fields
    problems: list[str] = []

    print("① 取 tenant_access_token …", end=" ", flush=True)
    try:
        table.token()
        print("OK")
    except Exception as exc:
        print("失败")
        print(f"   {exc}")
        return 1

    print("② 全量字段体检（列名、类型、选项）…", end=" ", flush=True)
    meta = table.fields_meta()
    if not meta:
        # None（读失败）和空 dict（0 列）同罪：多维表格的主字段不可删，
        # 健康的表不可能一列都没有——空结果正是权限没配全的典型症状。
        print("读不到字段列表" if meta is None else "字段列表为空")
        problems.append(
            f"读不到字段列表。多半是应用没被加进这张多维表格："
            f"表格右上角「…」→「添加文档应用」把应用加成协作者。"
            f"若这张表开了「高级权限」，还要在高级权限里给应用「可管理」——"
            f"漏这一步的表现是读到空结果而不是报错。"
        )
    else:
        print(f"OK（表里共 {len(meta)} 列）")
        problems.extend(_schema_problems(settings, meta))

    print("③ 试读一行 …", end=" ", flush=True)
    try:
        # 只问表里真有的列。按名字请求一个不存在的列，飞书会整个 search 报
        # 1254045、一行都读不到——那时这一步会用一句原始 API 错误盖住②已经
        # 说清楚的「缺哪几列」，把「少建了一列」显示成「读不了表」。
        # 真正在跑的读侧（load_rows）本来就按 known_fields 过滤，这里对齐它。
        wanted = [c for c in f.must_read() if not meta or c in meta]
        sample = table.search(wanted, max_records=1)
        print(f"OK（读到 {len(sample)} 行）")
        if sample:
            present = set((sample[0].get("fields") or {}).keys())
            # 飞书不会为空单元格返回键，所以只能提示而不能断言。
            print(f"   本行有值的列：{'、'.join(sorted(present)) or '（全空）'}")
    except Exception as exc:
        print("失败")
        problems.append(str(exc))

    if problems:
        print(f"⚠ 这张表发现 {len(problems)} 个问题：")
        for index, problem in enumerate(problems, 1):
            print(f"  {index}. {problem}")
    else:
        print("✅ 这张表检查通过")
    return len(problems)


def cmd_doctor(selected: list[str] | None = None) -> int:
    """上线前体检。不花一分钱，但能挡掉九成的「配好了跑不通」。"""
    settings = build_settings()
    tables = _tables(selected)
    total = 0
    for index, (label, table) in enumerate(tables):
        if len(tables) > 1:
            print(("\n" if index else "") + f"━━━━ 表：{label} ━━━━")
        total += _doctor_table(settings, table)

    print("\n④ 数据通道 …")
    keys = _api_keys()
    for platform in ("xhs", "douyin"):
        order = settings.channels.for_platform(platform)
        live = providers.usable_order(settings.channels, platform, keys)
        label = "小红书" if platform == "xhs" else "抖音"
        if not live:
            print(f"   {label}：❌ 配置的是 {'、'.join(order)}，但一个 Key 都没有")
            total += 1
        elif len(live) == 1:
            print(f"   {label}：⚠ 只有 {live[0]} 一条通道，它挂了这一轮就全丢")
        else:
            print(f"   {label}：✅ {live[0]} 为主，{'、'.join(live[1:])} 兜底")
    if keys:
        print(f"   已配置的 Key：{'、'.join(sorted(keys))}"
              "（是否有效需要真实调用一次才知道）")

    print("\n⑤ 单轮花费的上界 …")
    budget = settings.budget
    if budget.max_yuan_per_run:
        print(f"   ✅ 金额上限 ¥{budget.max_yuan_per_run:.2f}/轮")
    else:
        # 这一段存在的全部理由：没设上限时**以前一个字都不打**。
        # Budget.describe() 返回「无上限」，而运行时只在 != 无上限 时才打印
        # ——最危险的那种配置反而是最安静的。
        print("   ⚠️  没设 MAX_YUAN_PER_RUN：**单轮花费没有上界**")
        print("      「全表被误勾排队刷新」「最近检查时间列被清空」"
              "「上游故障导致每轮重刷」这三类事故，")
        print("      单轮成本都是没有上界的，而且都不会报错。"
              "建议按你们一轮的正常花费给 3–5 倍。")
        total += 0        # 不算问题，但要吵闹：拒跑是部署方的决定，不是这里的
    print(f"   行数上限：{budget.max_records_per_run or '无'}"
          f"   调用数上限：{budget.max_calls_per_run or '无'}")

    print()
    if total:
        print(f"共发现 {total} 个问题（明细见上）")
        return 1
    print("✅ 全部通过，可以开跑")
    return 0


def _refresh_table(mode: str, record_ids: list[str] | None, settings: Settings,
                   api_keys: dict[str, str], table: feishu.Bitable, now: datetime,
                   *, quiet_missing: bool = False,
                   deadline: float | None = None,
                   disabled: set[str] | None = None,
                   budget: runner.RunBudget | None = None,
                   stop: threading.Event | None = None):
    """在一张表上执行 mode 的**刷新阶段**（读表 + 打上游），不写回。

    返回（退出码, 找到的 record_id, 待刷行数, 预估元, 待写回材料）。
    待写回材料是 (report, fields_meta)，None 表示这张表本轮没有要写的
    （estimate、没有待刷行、缺列护栏拦下）。写回推迟到所有表都刷完之后
    （见 _run）：跨表熔断要先看全局样本再定罪，作废的判定不能已经落了表。

    quiet_missing：多表 row 模式下，某张表没有目标行属于正常（行在别的
    表里），静默跳过；缺不缺由调用方汇总所有表之后统一报。
    """
    print(f"读表（模式：{mode}）…")
    # 字段元数据读一次、处处共用：读侧过滤 search 请求的列（请求不存在的
    # 列会让整个 search 失败），写侧过滤还没建的机器列，两个选择列的
    # 选项清单也从同一份里取——省两次分页请求。
    fields_meta = table.fields_meta()
    if fields_meta is None:
        # 失败关闭（ROB-007）：元数据读不到时，读侧不知道该请求哪些列、
        # 写侧不知道该挡掉哪些列、两个选择列的选项清单也拿不到。
        # 原来的行为是「不过滤，宁可试着写」——那等于先花钱把整表刷一遍，
        # 再赌写回不会撞上不存在的列/选项。宁可这一轮不跑。
        print("❌ 读不到这张表的字段元数据（权限或网络问题），本轮不跑："
              "没有列清单就无法安全地读写，继续下去会先花钱、再赌写回。"
              "先跑 `python3 cli.py doctor` 看具体原因")
        return 1, set(), 0, 0.0, None
    known_fields = set(fields_meta)
    blockers = _scheduling_blockers(settings, fields_meta)
    if blockers and mode in ("sweep", "queue"):
        # 在**花钱之前**拦下来。这两列写不进去会让每一轮重新付费刷同一批行，
        # 而进程一路返回 0——跳过一轮是有界的损失，循环付费不是。
        print("❌ 调度地基列的类型不对，本轮不跑（一行都没花钱）：")
        for problem in blockers:
            print(f"  {problem}")
        print("  这两列写不进去，刷过的行下一轮还会被判成到期，"
              "于是每一轮都重新付费刷同一批行，而且不会报错。"
              "去飞书把类型改对，或跑 `python3 cli.py doctor` 看完整体检。")
        return 1, set(), 0, 0.0, None
    if blockers:
        # row 模式是人工点名要这几行的数据，拦住不如给：照跑，但把后果说清楚。
        print("⚠ 调度地基列的类型不对，这几行刷完不会被记成「已检查」：")
        for problem in blockers:
            print(f"  {problem}")
    row_list = runner.load_rows(
        table,
        settings,
        only_record_ids=record_ids,
        # estimate 的语义是「这一轮要花多少钱」，所以必须和 sweep 用同一套
        # 到期筛选——把未到期、已归档的行也算进去，报出来的数字会虚高一个量级。
        only_due=(mode in ("sweep", "estimate")),
        only_queued=(mode == "queue"),
        now=now,
        known_fields=known_fields,
    )
    found = {r.record_id for r in row_list}
    if mode == "queue" and not row_list and known_fields is not None \
            and settings.fields.queued not in known_fields:
        print(f"⚠ 表里还没建「{settings.fields.queued}」列，queue 模式无法工作，先去建列")
        return 1, found, 0, 0.0, None
    if mode in ("sweep", "estimate") and not row_list and known_fields is not None \
            and settings.fields.last_updated not in known_fields:
        # 花费返回 **None（未知）而不是 0.0**。这条护栏让 load_rows 直接
        # return []，于是 estimate 会报「待刷 0 行 ≈ ¥0.00」——而真相是
        # 「这张表还没法估算」。把列建出来之后全表 last_updated 全空、
        # 一轮 sweep 全判到期，而人刚刚才看着那个 0 放下心来。
        print(f"⚠ 表里还没建「{settings.fields.last_updated}」列：分层刷新没有依据，"
              "每一轮 sweep 都会全表重刷烧钱，先去建列")
        print("  这张表的「预计花费」**无法估算**（不是 ¥0.00）——"
              "建完这一列之后全表都会判到期，先看清有多少行再开跑")
        return 1, found, 0, None, None
    if record_ids and not quiet_missing:
        missing = [rid for rid in record_ids if rid not in found]
        if missing:
            print(f"⚠ 这些 record_id 在表里没找到（可能拼错或已删除）：{'、'.join(missing)}")
    if not row_list:
        if not (record_ids and quiet_missing):
            print("没有需要刷新的行。")
        return (1 if record_ids and not quiet_missing else 0), found, 0, 0.0, None

    # 首轮小闸：整张表**一个**「最近检查时间」都没有 = 要么是全新表，要么是
    # 刚把这一列建出来。两种情况下每一行都判到期，一轮就是全表付费。
    # 这个闸和 MAX_RECORDS_PER_RUN 不是一回事：那个是整次运行共享的，
    # 这个是**单张新表**的，防的是「一张 800 行的表刚入册就吃掉整轮预算」。
    first_run_cap = _numeric_env("FIRST_RUN_MAX_RECORDS", int, 20, minimum=0)
    if (first_run_cap and len(row_list) > first_run_cap
            and all(r.last_updated_ms is None for r in row_list)):
        print(f"🐣 这张表一个「{settings.fields.last_updated}」都没有"
              f"（{len(row_list)} 行全是新的或刚建完列），本轮只刷前 "
              f"{first_run_cap} 行。剩下的每轮再来一批，几轮之后就铺满了。"
              "（想一次刷完：FIRST_RUN_MAX_RECORDS=0）")
        row_list = row_list[:first_run_cap]
        found = {r.record_id for r in row_list}

    yuan = rows_mod.estimate_yuan(row_list, settings, now, keys=api_keys)

    # 显示的通道要和计价用的是同一套选择逻辑（第一家配了 key 的），
    # 否则只配备胎 key 时会按 SDX 计价、嘴上却说走 TikHub，自相矛盾。
    def _display_channel(platform: str) -> str:
        for name in settings.channels.for_platform(platform):
            if api_keys.get(name):
                return name
        return settings.channels.primary(platform)

    print(f"待刷 {len(row_list)} 行，预计花费 ≈ ¥{yuan:.2f}"
          f"（按各行实际会走的通道计价："
          f"小红书主走 {_display_channel('xhs')}，"
          f"抖音主走 {_display_channel('douyin')}；"
          "提不出数字 ID 的抖音链接按 socialdatax 计）")
    if settings.budget.describe() != "无上限":
        print(f"  本轮硬预算：{settings.budget.describe()}（超出的行保持原样，留给下一轮）")

    if mode == "estimate":
        return 0, found, len(row_list), yuan, None

    report = runner.refresh(
        row_list, api_keys, settings,
        now=now,
        known_options=_options_from_meta(fields_meta, settings.fields.traffic_status),
        comment_status_options=_options_from_meta(fields_meta, settings.fields.comment_status),
        negative_status_options=_options_from_meta(fields_meta, settings.fields.negative_status),
        pin_status_options=_options_from_meta(fields_meta, settings.fields.pinned_status),
        forced=(record_ids is not None),
        progress=print,
        deadline=deadline,
        disabled=disabled,
        budget=budget,
        stop=stop,
    )
    return 0, found, len(row_list), yuan, (report, fields_meta)


def _scheduling_blockers(settings: Settings, fields_meta: dict) -> list[str]:
    """「调度地基」两列里类型建错的那些，翻译成人话。

    `最近检查时间` 和 `排队刷新` 和别的机器列不是一个量级：别的列写不进去
    只是这一列没数据，这两列写不进去会**循环烧钱**——刷过的行 last_updated
    没推进，下一轮 sweep 照样判它到期，于是每一轮都重新付费刷同一批行；
    勾选清不掉的话 queue 模式同理。而进程一路返回 0，cron 和云平台看到的
    是一片绿色的成功。

    所以这两列的类型不对时，自动模式在**花钱之前**就拒跑：跳过一轮的代价
    是有界的，循环付费的代价不是。缺列不归这里管（另有护栏）。
    """
    expected = {name: (allowed, label)
                for name, allowed, label, _o, _n in _expected_schema(settings)}
    problems = []
    for name in (settings.fields.last_updated, settings.fields.queued):
        info = fields_meta.get(name)
        if info is None:
            continue
        allowed, label = expected[name]
        if info["type"] not in allowed:
            problems.append(f"「{name}」现在是「{_type_name(info['type'])}」，"
                            f"需要「{label}」")
    return problems


def _mistyped_warning(mistyped: set[str], fields_meta: dict | None,
                      settings: Settings, label: str = "") -> str:
    """写回时按类型摘掉的列，翻译成运营看得懂的一段话。

    这段话必须说清三件事：哪一列、现在建成了什么、应该是什么。只报
    「跳过了 N 列」等于让人回头再体检一遍——而这时候钱已经花了。
    """
    expected = {name: (allowed, type_label)
                for name, allowed, type_label, _opts, _note in _expected_schema(settings)}
    lines = [
        "❌ 这些列的**字段类型**和机器要写的值对不上，本轮已跳过这几列"
        "（其余列照常写回，不会写坏表）："
    ]
    for name in sorted(mistyped):
        actual = _type_name((fields_meta or {}).get(name, {}).get("type"))
        want = expected.get(name, (None, "见 docs/表结构.md"))[1]
        lines.append(f"  「{name}」现在是「{actual}」，需要「{want}」")
    scope = f" --table {label}" if label else ""
    lines.append(f"  去飞书把类型改过来，或跑 `python3 cli.py doctor{scope}` 看完整体检。"
                 "不改的话这几列的数据每一轮都落不下来。")
    return "\n".join(lines)


def _write_back_table(table: feishu.Bitable, report,
                      fields_meta: dict | None,
                      settings: Settings, label: str = "") -> int:
    """写回阶段。summary 也在这里打印——跨表熔断可能刚作废过判定，
    这时各行显示的才是真正落表的最终状态。"""
    print(report.summary())

    write_errors: list = []
    dropped_fields: set = set()
    mistyped_fields: set = set()
    try:
        written = runner.write_back(
            table, report, errors=write_errors,
            known_fields=None if fields_meta is None else set(fields_meta),
            # 列名之外还要带上类型：一列被建错类型（比如「流量状态」建成单选）
            # 会让飞书回 1254063，整表已付费的结果全部落空。
            field_types=None if fields_meta is None
            else {name: info.get("type") for name, info in fields_meta.items()},
            dropped_fields=dropped_fields,
            mistyped_fields=mistyped_fields,
            say=print)
    except feishu.FeishuError as exc:
        # 表级错误（权限、列名、token）：逐行重试无意义，说清楚原因退出。
        # 未写回的行 last_updated 没动，下一轮会自然重捞。
        print(f"❌ 写回失败（表级错误）：{exc}")
        return 1
    # 报出去的时间跨度只能覆盖**真的写进去了**的行：逐行失败的那些
    # （读表之后记录被删等）时间戳压根没落表，「最近检查时间」整列被挡下来时
    # （列没建、类型建错）更是一行都没盖上。报一个表里不存在的时刻，
    # 恰恰就是这次要修的「日志和表对不上」。
    stamp_column = settings.fields.last_updated
    span = "" if stamp_column in dropped_fields or stamp_column in mistyped_fields \
        else report.checked_span(settings.display,
                                 skip=[record_id for record_id, _ in write_errors])
    print(f"已写回 {written} 行" + (f"，本轮「最近检查时间」= {span}" if span else ""))
    if dropped_fields:
        print(f"⚠ 这些列在表里还没建，本轮已跳过（建好后下一轮自动补上）："
              f"{'、'.join(sorted(dropped_fields))}")
    if mistyped_fields:
        print(_mistyped_warning(mistyped_fields, fields_meta, settings, label))
    if write_errors:
        print(f"⚠ {len(write_errors)} 行写回失败（其余行不受影响）：")
        for record_id, exc in write_errors:
            print(f"  {record_id}: {exc}")
    # 退出码语义：到软截止「留给下一轮」是正常运行返回 0（返回非零会让
    # cron / 云平台的重启策略把它当失败反复重启）；真故障（Key/余额）和
    # 「花了钱但有行没写回」都返回非零——花出去的钱没落进表里，
    # 不能让 cron 和 Actions 显示一个绿色的成功。
    #
    # 类型建错的列同样非零：钱花了，那一列的数据没落表，而且不改配置的话
    # 每一轮都会这样。缺列（dropped_fields）不算——那是「还没建，建好就补上」
    # 的正常过渡态，不该每轮都把 cron 染红。
    return 1 if (report.fatal or write_errors or mistyped_fields) else 0


def _install_stop_handlers(stop: threading.Event) -> None:
    """SIGTERM / SIGINT → 只置一个标志位，不在信号处理器里干活（ROB-009）。

    收到信号后：停止派发新行、让在跑的行跑完、把**已经付过钱**的结果照常
    写回，然后正常退出。原来没有这一步，Railway redeploy / 容器回收 /
    人工 Ctrl-C 都会让本轮所有未写回的付费结果直接蒸发，下一轮再付一次。

    信号处理器里只做 set()：它跑在主线程的任意指令边界上，
    在里面写飞书或打大段日志是重入地雷。
    """
    def handler(signum, _frame):
        if not stop.is_set():
            stop.set()
            name = signal.Signals(signum).name
            print(f"\n⚠ 收到 {name}：停止派发新行，把已完成的结果写回后退出", flush=True)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持：优雅停机降级为不可用，不影响主流程。
            pass


def _event_sink():
    """结构化运行日志（SUP-004）。RUN_LOG_JSON=1 时每行一条 JSON。

    控制台文本是给人看的，按 run/table/provider 聚合、定义 SLO、报警
    都需要机器可读的一行一条。写到 stderr，不和业务输出混在一起。
    """
    if not _bool_env("RUN_LOG_JSON", False):
        return None, ""
    run_id = f"{int(time.time())}-{os.getpid()}"

    def sink(event: dict) -> None:
        payload = {"run_id": run_id, "ts": round(time.time(), 3), **event}
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)

    return sink, run_id


def _run(mode: str, record_ids: list[str] | None,
         selected: list[str] | None = None) -> int:
    settings = build_settings()
    api_keys = _api_keys()
    if not api_keys:
        sys.exit("一个数据通道的 Key 都没配：需要 TIKHUB_API_KEY 或 SOCIALDATAX_API_KEY"
                 "（参考 .env.example）")

    # —— 运行租约（ROB-001）：同一时刻只允许一个付费执行者 ——
    # 拿不到就干净退出，一个付费请求都不发。RUN_LOCK_DISABLED=1 可以关掉
    # （只在明确知道自己在做什么时用，比如在两台机器上分别跑不同的表）。
    lease = None
    if not _bool_env("RUN_LOCK_DISABLED", False):
        try:
            lease = runlock.acquire(f"cli.py {mode}")
        except runlock.Busy as exc:
            print(f"⏭ {exc}\n"
                  "  本轮跳过：两个进程同时刷同一张表会重复花钱、互相覆盖写入。"
                  "  如果这是误报（比如上一轮真的挂死了），删掉锁文件即可；"
                  "  锁文件路径可用 RUN_LOCK_PATH 指定。")
            return 0     # 正常跳过，不是失败——返回非零会让云平台反复重启
        except OSError as exc:
            # 锁文件所在目录不可写（只读根文件系统、权限不对）：这不该
            # 让整轮跑不起来，但必须吵闹——此刻是没有互斥保护在跑的。
            print(f"⚠ 拿不到运行租约的锁文件（{exc}）：本轮**没有**跨进程互斥保护。"
                  "  设一个可写的 RUN_LOCK_PATH，或确认只有一个调度器在跑。")
    try:
        return _run_locked(mode, record_ids, selected, settings, api_keys)
    finally:
        if lease is not None:
            lease.release()


def _run_locked(mode: str, record_ids: list[str] | None,
                selected: list[str] | None,
                settings: Settings, api_keys: dict[str, str]) -> int:
    tables = _tables(selected)
    now = datetime.now(timezone.utc)
    multi = len(tables) > 1

    # 开跑先把时间打出来，两个时区都打。容器日志的行首时间戳是 UTC，
    # 表里的「最近检查时间」是飞书按租户时区渲染的——不把换算摆在眼前，
    # 每一次「日志和表对不上」都要有人重新算一遍 8 小时。
    print(f"⏱ {mode} 开跑：{settings.display.stamp(now)}"
          f"（UTC {now:%H:%M:%S}，容器日志用的就是这个）")
    if mode in ("sweep", "queue", "row") and not settings.budget.max_yuan_per_run:
        # 以前这里是沉默的：Budget.describe() 说「无上限」，而收尾只在
        # 有上限时才打印。于是唯一真正能兜住烧钱事故的那道闸没设时，
        # 日志里一个字都没有。
        print("⚠ 本轮**没有金额上界**（MAX_YUAN_PER_RUN 未设）"
              "——建议设一个，见 .env.example")

    # 软截止是整次运行的预算，不是每张表各领一份——在这里算一次绝对
    # 截止点传给每张表共享，五张表就不会把时限放大成五倍。
    deadline = (time.monotonic() + settings.soft_deadline_seconds
                if settings.soft_deadline_seconds else None)
    # 「某家 Key 失效/余额耗尽」的死讯同理跨表共享：第一张表用真实
    # 请求换来的结论，后面的表直接沿用，不再逐表花钱重新发现。
    disabled: set[str] = set()
    # 硬预算同理是整次运行的，不是每张表各领一份。
    budget = runner.RunBudget(settings.budget)
    stop = threading.Event()
    _install_stop_handlers(stop)
    on_event, run_id = _event_sink()
    # 一轮的开头也发一条：面板靠「有 run_start 没有 run_end」认出
    # 跑到一半被杀掉的那些轮（Railway redeploy、容器回收、OOM）。
    # 只看结束事件的话，那种轮在看板上根本不存在。
    runner.emit(on_event, runner.EVENT_RUN_START, mode=mode,
                tables=[label for label, _ in tables],
                started_at=now.isoformat(),
                budget=settings.budget.describe())

    def _finish(code: int, error: str = "") -> int:
        """收尾事件。**每一条退出路径都要经过它。**

        面板靠「有 run_start 没有 run_end」认出被杀掉的轮子。要是某条正常
        退出路径漏发了，那一轮在看板上就长得和「跑到一半被容器回收」一模一样
        ——一个假的故障信号比没有信号更糟。
        """
        runner.emit(on_event, runner.EVENT_RUN_END, mode=mode,
                    tables=len(tables), exit_code=code, error=error,
                    rows=sum(len(r.outcomes) for _l, _t, r, _m, _c in pending),
                    cost_yuan=round(
                        sum(r.cost_yuan for _l, _t, r, _m, _c in pending), 6),
                    channels_dead=channels_dead, stopped=stop.is_set(),
                    budget_stopped=budget.stopped_reason,
                    # 「跑完剩」：这一轮最后看到的 SocialDataX 积分余额。
                    # 取最小的那个（宁可低报）。全走 TikHub 的轮子是 None，
                    # 面板显示「—」而不是 ¥0——两者要人做的事不一样。
                    points_balance=min(
                        (r.points_balance for _l, _t, r, _m, _c in pending
                         if r.points_balance is not None), default=None))
        return code

    worst = 0
    found_all: set[str] = set()
    total_rows, total_yuan = 0, 0.0
    # 有几张表压根估不出来（缺「最近检查时间」列）。合计里必须说出来——
    # 把它们当成 ¥0 加进去，报出来的数字就是个假的下界。
    unknown_cost = 0
    # 先把所有表都刷完、攒起来，写回放到跨表熔断之后（见下）。
    pending: list[tuple[str, feishu.Bitable, runner.RunReport,
                       set[str] | None, Settings]] = []
    channels_dead = False
    try:
        for index, (label, table) in enumerate(tables):
            if multi:
                print(("\n" if index else "") + f"━━━━ 表：{label} ━━━━")
            if channels_dead:
                print("⚠ 数据通道已全部不可用（Key 失效或余额耗尽），这张表本轮"
                      "不再尝试；行都没动过，修好通道后下一轮自然补上")
                worst = 1
                continue
            if stop.is_set():
                print("⚠ 运行已被终止，这张表本轮不再尝试；行都没动过，下一轮自然补上")
                worst = 1
                continue
            # 逐表阈值：深拷贝 + 浅覆盖，绝不就地改基准 Settings——
            # 就地改会让第一张表的阈值串味到后面所有表，而那种 bug 只在
            # 多表部署上出现，看起来像「判定口径莫名其妙」。
            per_table = settings
            row = _REGISTRY_ROWS.get(table.table_id)
            if row is not None:
                from xhsearch import registry as registry_mod
                per_table = registry_mod.apply_overrides(settings, row, log=print)
            try:
                code, found, row_count, yuan, prep = _refresh_table(
                    mode, record_ids, per_table, api_keys, table, now,
                    quiet_missing=multi, deadline=deadline, disabled=disabled,
                    budget=budget, stop=stop)
            except feishu.FeishuError as exc:
                # 一张表的表级故障（权限被收回、表被删）不该拖垮其余表的巡查。
                print(f"❌ 这张表读写失败（表级错误）：{exc}；继续处理其余表")
                worst = 1
                continue
            worst = max(worst, code)
            found_all |= found
            total_rows += row_count
            if yuan is None:
                unknown_cost += 1
            else:
                total_yuan += yuan
            if prep is not None:
                report, fields_meta = prep
                pending.append((label, table, report, fields_meta, per_table))
                if report.fatal and not [k for k in api_keys if k not in disabled]:
                    channels_dead = True
    except BaseException:
        # 刷新阶段炸了（含 KeyboardInterrupt）：**已经付过钱**的表照样写回，
        # 再把异常抛出去。不这么做的话，一次意外就让这一轮花的钱全部蒸发，
        # 下一轮按原样的 last_updated 再付一次。
        if pending:
            print("\n⚠ 运行中断，先把已经完成的表写回（避免已付费的结果丢失）…")
            for label, table, report, fields_meta, per_table in pending:
                try:
                    _write_back_table(table, report, fields_meta, per_table, label)
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ 表 {label} 写回失败：{exc}")
        _finish(1, error=type(sys.exc_info()[1]).__name__)
        raise

    # 跨表熔断：单表可能只有三五行，永远凑不满熔断的最小样本，但上游
    # 故障是通道级的——把这一轮所有表的观测合起来再判一次，该作废的
    # 失效判定在写回**之前**作废掉。
    # 跨表熔断用**全局** settings：熔断参数刻意不允许逐表（见 registry.py），
    # 逐表不同的熔断口径会让「这一轮到底该不该熔」说不清。
    if len(pending) > 1 and runner.apply_cross_run_breaker(
            [report for _, _, report, _, _ in pending], settings):
        print("\n🛑 跨表熔断：本轮各表合计的失效比例异常偏高，疑似上游故障，"
              "已作废所有表的失效判定（明细见各表结果）")

    # 结构化事件在**所有**熔断（含上面这次跨表熔断）定案之后才发：
    # 提前发的话，被熔断改写过的行会让看板和告警看到一个比实际落表更吓人的
    # 结论，而那正好发生在上游故障、最不该误报的时候。
    for label, _table, report, _meta, _cfg in pending:
        runner.emit_run_events(report, on_event, table=label, mode=mode)

    for label, table, report, fields_meta, per_table in pending:
        if multi:
            print(f"\n━━━━ 表：{label}（结果）━━━━")
        else:
            print()
        try:
            code = _write_back_table(table, report, fields_meta, per_table, label)
        except feishu.FeishuError as exc:
            print(f"❌ 这张表写回失败（表级错误）：{exc}；继续处理其余表")
            worst = 1
            continue
        worst = max(worst, code)

    if run_id:
        print(f"\nrun_id={run_id}（结构化日志已写到 stderr）")
    if budget.stopped_reason:
        print(f"💰 {budget.stopped_reason}"
              f"（本轮实际：{budget.records} 行 / {budget.calls} 次调用 / ≈¥{budget.yuan:.2f}）")

    if record_ids and multi:
        missing = [rid for rid in record_ids if rid not in found_all]
        if missing:
            print(f"\n⚠ 这些 record_id 在所有已配置的表里都没找到："
                  f"{'、'.join(missing)}")
            if not found_all:
                return _finish(1)
    if mode == "estimate" and multi:
        tail = (f"，另有 {unknown_cost} 张表**无法估算**（缺「最近检查时间」列，"
                "建完之后会全表判到期）" if unknown_cost else "")
        print(f"\n合计：待刷 {total_rows} 行，预计花费 ≈ ¥{total_yuan:.2f}{tail}")
    return _finish(worst)


def _line_buffer_stdout() -> None:
    """逐行 flush。**这是「日志乱序」的根治办法，不是性能微调。**

    stdout 不接终端时 Python 默认是块缓冲：几十行攒够 8KB 才一起吐出去，
    云平台（Railway / Actions）按**收到的时刻**给整块打同一个时间戳，
    块内的顺序还可能被打乱。线上真实出现过的样子是：

        08:45:15  没有需要刷新的行。
        08:45:15  ━━━━ 表：西屋第一期 ━━━━      ← 表头跑到结论后面
        15:05:35  已写回 8 行
        15:05:35    recvs9cC7i7jDl → 正常 …     ← 写回打完了还在出逐行结果

    于是「哪一行属于哪张表」「先后发生了什么」全都读不出来，
    拿日志去对表自然对不上。逐行 flush 之后每一行都带自己真实的时刻。

    代价是每行一次 write 系统调用——一轮几百行，完全不值得权衡。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError, OSError):
            # 被重定向成非 TextIOWrapper（测试里的 StringIO、某些托管运行时）：
            # 日志格式的优化不该让整个进程起不来。
            pass


def cmd_init_registry() -> int:
    """建那张存表清单的飞书表，打印一行环境变量。**这辈子跑一次。**

    为什么要有它、为什么不是「每次启动自己去云盘里找」：见
    `xhsearch/registry.py` 的模块说明。一句话——面板和 cron 是两个不共享
    任何东西的容器，「你点了加表」必须落到一个 cron 五分钟后读得到的地方；
    而隐式发现（找不到 / 找错 / 找到两张）的失败模式比一次复制粘贴贵得多。
    """
    from xhsearch import registry as registry_mod

    existing = os.environ.get("FEISHU_REGISTRY", "").strip()
    if existing:
        print(f"FEISHU_REGISTRY 已经配了：{existing}")
        print("要重建就先把这个变量清掉。（重建会得到一张空表，"
              "原来那张里的项目不会自动搬过去。）")
        return 1

    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    workspace = feishu.Workspace(app_id=app_id, app_secret=app_secret)

    print("① 建多维表格 …", end=" ", flush=True)
    base = workspace.create_base("linkcheck 监控台")
    if not base["app_token"]:
        sys.exit("失败：飞书没返回 app_token")
    print(f"OK（{base['app_token']}）")

    print("② 建注册表数据表 …", end=" ", flush=True)
    table_id = workspace.create_table(
        base["app_token"], "被监控的表", registry_mod.REGISTRY_FIELDS)
    if not table_id:
        sys.exit("失败：飞书没返回 table_id")
    print(f"OK（{table_id}）")

    print("\n✅ 建好了。把这一行加进 Railway 的变量，**这辈子只加这一次**：\n")
    print(f"    FEISHU_REGISTRY={base['app_token']}:{table_id}\n")
    if base["url"]:
        print(f"表在这儿（平时不用打开）：{base['url']}")
    print("之后加表删表都在面板上做。留着这张表能进去改，是为了面板挂了的时候"
          "还有个地方能止损（去掉某张表的「启用」）。")
    return 0


def cmd_serve(selected: list[str] | None = None) -> int:
    """起监控面板。**常驻进程，但一个付费请求都不发。**

    它和 cron 那个服务是两个独立的 Railway service，彼此不通信——
    只通过飞书表间接耦合。面板挂了巡检照跑；面板重启，状态从表里重新读一遍就有。

    ⚠️ 这个命令**绝不能**配 cron，也绝不能和 `queue`/`sweep` 配在同一个
    Start Command 里。两个容器同时刷同一张表 = 钱花两份 + 人工标签被旧快照
    覆盖 + 飞书写冲突，而 runlock 是文件锁，跨容器拦不住。
    """
    from xhsearch import panel

    try:
        config = panel.PanelConfig.from_env()
    except panel.ConfigError as exc:
        sys.exit(f"❌ {exc}")

    settings = build_settings()
    api_keys = _api_keys()
    # Key 有两个用途，**都不花钱**：
    # ① 给「预计花费」选对单价（不同通道差十几倍）——纯本地计算；
    # ② 查两家的余额——两个端点都是官方标明零费用的（见 xhsearch/balance.py
    #    开头那张表，以及那里为什么是单独一个模块）。
    # 没配也能起：那时按默认通道计价、余额那一块显示「没配 Key」。
    from xhsearch import protocol
    config.api_keys = dict(api_keys)
    config.tikhub_base = providers.TIKHUB_BASE
    config.socialdatax_base = protocol.BASE
    config.usd_to_cny = providers.usd_to_cny()
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")

    def resolve_tables():
        """**每一轮刷新都重新读一次表清单。**

        只在启动时读一次的话：在面板上加表/启用，概览里永远不出现；
        停用/移除，面板还在读它。cron 下一轮就看见了，面板要重启才看见——
        两边显示的「在管哪些表」长期分叉，而面板正是用来看这件事的。

        `allow_empty=True`：刚 `init-registry` 出来的注册表是空的，
        而在面板上加第一张表正是它存在的理由。付费那几条命令不给这个开关。
        """
        targets = _entries_or_raise(app_id, app_secret, allow_empty=True)
        return _bitables(_env_filtered(targets, selected), app_id, app_secret)

    def settings_for(table_id: str):
        """这张表自己的 Settings。**和 cron 用同一个函数算**，口径不会打架。

        `_run_locked` 的表循环里是 `registry.apply_overrides`；面板这边不做
        同一件事的话，逐表 `归档天数` 改过之后，面板算出的到期/超期/归档/
        预估花费和 cron 不是一回事，待办行还会被放进错误的「在管/已归档」分区。
        """
        row = _REGISTRY_ROWS.get(table_id)
        if row is None:
            return settings
        from xhsearch import registry as registry_mod
        # log=None：面板一分钟刷一次，逐表阈值那句话打一遍就够了，
        # 每分钟重复一遍只会把日志淹掉。cron 那边照常打。
        return registry_mod.apply_overrides(settings, row)

    try:
        tables = resolve_tables()
    except NoTables as exc:
        sys.exit(str(exc))
    if tables:
        print(f"面板要看 {len(tables)} 张表："
              f"{'、'.join(label for label, _ in tables)}")
    else:
        print("⚠ 注册表里还没有可用的表。面板照常启动——"
              "到「项目」页上加第一张")

    def produce():
        return panel.collect(
            resolve_tables(), settings, api_keys,
            show_digest=config.show_digest,
            feishu_base=config.feishu_base,
            secrets=config.secrets,
            settings_for=settings_for,
            label_column=config.label_column)

    return panel.serve(config, produce, settings)


def main(argv: list[str]) -> int:
    _line_buffer_stdout()
    load_env_or_exit()
    args = list(argv[1:])
    selected: list[str] | None = None
    if "--table" in args:
        index = args.index("--table")
        if index + 1 >= len(args):
            sys.exit("--table 后面要跟表标签（FEISHU_TABLES 里起的名字），多个用逗号分隔")
        selected = [s.strip() for s in args[index + 1].split(",") if s.strip()]
        if not selected:
            # 「--table ,」这类写法解析出空清单。空清单和「没传 --table」
            # 在下游没法区分，会静默变成全表都跑——必须在这里吵闹。
            sys.exit("--table 后面要跟表标签（FEISHU_TABLES 里起的名字），多个用逗号分隔")
        if len(set(selected)) != len(selected):
            # `--table A,A` 会让同一张表被读两遍、付费刷两遍，两次结果还可能
            # 互相覆盖。仓库对 FEISHU_TABLES 里的重复物理表已经是报错处理，
            # 这里保持同一口径。
            sys.exit(f"--table 里有重复的表标签：{'、'.join(selected)}——"
                     "同一张表刷两次是白花钱")
        del args[index:index + 2]
    if not args:
        print(__doc__)
        return 2
    command = args[0]
    if command == "doctor":
        return cmd_doctor(selected)
    if command == "serve":
        return cmd_serve(selected)
    if command == "init-registry":
        return cmd_init_registry()
    if command in ("sweep", "queue", "estimate"):
        return _run(command, None, selected)
    if command == "row":
        if len(args) < 2:
            sys.exit("用法：python3 cli.py row <record_id> [<record_id> ...] [--table 标签]")
        return _run("row", args[1:], selected)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
