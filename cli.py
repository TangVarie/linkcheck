#!/usr/bin/env python3
"""命令行入口。

    python3 cli.py doctor              # 体检：不花积分，检查配置/权限/字段是否齐全
    python3 cli.py sweep               # 分层巡检：只刷到期的行
    python3 cli.py queue               # 只刷勾了「排队刷新」的行
    python3 cli.py row <record_id>...  # 刷指定行（无视冷却和分层节流）
    python3 cli.py estimate            # 只估算这一轮要花多少钱，不发请求

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

from xhsearch import feishu, providers, rows as rows_mod, runlock, runner
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


def _tables_from_env(environ) -> list[tuple[str, str, str]]:
    """解析要巡查的表清单，返回 [(标签, app_token, table_id), ...]。

    多表用 FEISHU_TABLES，**分号或换行**分隔，每一项几种写法都认：

        OKMAN一期=bascnXXX:tblAAA
        OKMAN二期=https://xx.feishu.cn/base/bascnXXX?table=tblBBB
        企业C期=https://xx.feishu.cn/wiki/wikcnXXX?table=tblDDD   （挂在知识库里的表也认）
        bascnYYY:tblCCC                     （不带标签时标签取 table_id）

    标签用在日志分节和 --table 筛选上，起个人能认的名字。
    单表继续用 FEISHU_APP_TOKEN + FEISHU_TABLE_ID；两种都配了以
    FEISHU_TABLES 为准。所有表共用同一个飞书应用（App ID/Secret），
    应用要逐张表「添加文档应用」授权。

    /wiki/ 链接实测直接把地址栏里那段 token 当 app_token 用，多维表格接口
    照样认——不需要额外调接口换算、也不需要给应用多开知识库权限，跟
    /base/ 链接一视同仁，只是换了个前缀。
    """
    spec = environ.get("FEISHU_TABLES", "").strip()
    if spec:
        entries: list[tuple[str, str, str]] = []
        for chunk in re.split(r"[;；\n]+", spec):
            chunk = chunk.strip().strip(",")
            if not chunk:
                continue
            head, sep, rest = chunk.partition("=")
            # URL 里本来就有 =（?table=tbl...），只有「短标签=」才当标签用
            if sep and "://" not in head and "/" not in head and ":" not in head:
                label, target = head.strip(), rest.strip()
            else:
                label, target = "", chunk
            match = re.search(r"/(?:base|wiki)/([A-Za-z0-9]+)\S*?[?&]table=([A-Za-z0-9]+)", target)
            if match:
                app_token, table_id = match.group(1), match.group(2)
            elif "://" in target:
                sys.exit(f"FEISHU_TABLES 里这个网址提不出表信息：{target!r}。"
                         "要用 /base/xxx?table=tblxxx 或 /wiki/xxx?table=tblxxx 形式的地址"
                         "（打开目标数据表时浏览器地址栏那串）")
            elif ":" in target:
                app_token, _, table_id = target.partition(":")
                app_token, table_id = app_token.strip(), table_id.strip()
            else:
                sys.exit(f"FEISHU_TABLES 里这一项看不懂：{chunk!r}。每一项写成 "
                         "标签=app_token:table_id 或 标签=表格完整网址，"
                         "多项之间用分号隔开（参考 .env.example）")
            if not app_token or not table_id:
                sys.exit(f"FEISHU_TABLES 里这一项缺 app_token 或 table_id：{chunk!r}")
            entries.append((label or table_id, app_token, table_id))
        if not entries:
            sys.exit("FEISHU_TABLES 设了但一张表都没解析出来，检查格式（参考 .env.example）")
        seen: set[tuple[str, str]] = set()
        for _, app_token, table_id in entries:
            if (app_token, table_id) in seen:
                sys.exit(f"FEISHU_TABLES 里 {table_id} 配了两遍——同一张表刷两次是白花钱")
            seen.add((app_token, table_id))
        labels = [label for label, _, _ in entries]
        if len(set(labels)) != len(labels):
            sys.exit("FEISHU_TABLES 里有重复的标签，--table 会分不清——给每张表起个不同的名字")
        return entries

    app_token = environ.get("FEISHU_APP_TOKEN", "").strip()
    table_id = environ.get("FEISHU_TABLE_ID", "").strip()
    if not app_token or not table_id:
        sys.exit("没配任何表：多表设 FEISHU_TABLES，单表设 "
                 "FEISHU_APP_TOKEN + FEISHU_TABLE_ID（参考 .env.example）")
    return [(table_id, app_token, table_id)]


def _tables(selected: list[str] | None = None) -> list[tuple[str, feishu.Bitable]]:
    app_id = _env("FEISHU_APP_ID")
    app_secret = _env("FEISHU_APP_SECRET")
    entries = _tables_from_env(os.environ)
    if selected:
        by_label = {label: entry for entry in entries for label in [entry[0]]}
        missing = [s for s in selected if s not in by_label]
        if missing:
            sys.exit(f"--table 指定的表不存在：{'、'.join(missing)}。"
                     f"可选：{'、'.join(label for label, _, _ in entries)}")
        entries = [by_label[s] for s in selected]
    return [(label, feishu.Bitable(app_id=app_id, app_secret=app_secret,
                                   app_token=app_token, table_id=table_id))
            for label, app_token, table_id in entries]


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


# 飞书多维表格字段类型码 → 界面上的叫法。体检报错时把数字翻译成人话。
_FIELD_TYPE_NAMES = {
    1: "文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期", 7: "复选框",
    11: "人员", 13: "电话号码", 15: "超链接", 17: "附件", 18: "单向关联",
    19: "查找引用", 20: "公式", 21: "双向关联", 22: "地理位置", 23: "群组",
    1001: "创建时间", 1002: "最后更新时间", 1003: "创建人", 1004: "修改人",
    1005: "自动编号",
}


def _type_name(code) -> str:
    return _FIELD_TYPE_NAMES.get(code, f"未知类型 {code}")


def _expected_schema(settings: Settings) -> list[tuple]:
    """每一列的期望配置：(列名, 允许的类型码, 类型的人话, 必备选项, 备注)。

    「表面对了，内在配置没对」通常就死在类型上：列名一字不差，
    但「最近检查时间」建成了系统的「最后更新时间」类型（机器写不进去），
    或「评论状态」建成了单选（机器按多选合并写入会整批失败）。
    这张清单就是 docs/表结构.md 的机器可执行版。
    """
    f = settings.fields
    statuses = [runner.STATUS_OK, runner.STATUS_SUSPECT, runner.STATUS_GONE,
                runner.STATUS_FAILED, runner.STATUS_SKIPPED]
    return [
        # —— 人工维护 ——
        (f.link, (1,), "文本", None,
         "别建成「超链接」字段——它会规范化链接，可能吞掉小红书短链里的 token"),
        (f.publish_time, (5,), "日期", None,
         "要手填的普通日期字段；建成「创建时间」类型拿到的是建行时间，不是发布时间"),
        (f.seed_keywords, (4, 1), "多选或文本", None,
         "文本列时用顿号/逗号/分号分隔多个词"),
        (f.negative_keywords, (4, 1), "多选或文本", None,
         "负面词 + 竞品词，格式同「评论关键词」；留空的行完全不做负面判定"),
        (f.monitoring, (7,), "复选框", None, None),
        (f.queued, (7,), "复选框", None, None),
        # —— 机器写入 ——
        (f.platform, (3, 1), "单选或文本", ["小红书", "抖音"], None),
        (f.comment_count, (2,), "数字", None, None),
        (f.previous_comment_count, (2,), "数字", None, None),
        (f.like_count, (2,), "数字", None, None),
        (f.previous_like_count, (2,), "数字", None, None),
        (f.collect_count, (2,), "数字", None, None),
        (f.previous_collect_count, (2,), "数字", None, None),
        (f.pinned_status, (3,), "单选", settings.pin_status.machine_written(),
         "机器直接覆盖写入当前状态；抖音行不写这一列"),
        (f.comment_status, (3,), "单选", settings.comment_status.machine_written(),
         "机器直接覆盖写入当前状态（待评论等旧值会被覆盖）"),
        (f.comment_digest, (1,), "文本", None, None),
        (f.negative_status, (3,), "单选", settings.negative_status.machine_written(),
         "由「负面词」命中驱动，机器直接覆盖；没填负面词的行不碰这一列"),
        (f.negative_digest, (1,), "文本", None, None),
        (f.traffic_status, (4,), "多选", settings.tags.machine_written(),
         "机器按多选合并写入——建成单选会让写回整批失败"),
        (f.refresh_status, (3, 1), "单选或文本", statuses, None),
        (f.failure_reason, (1,), "文本", None, None),
        (f.last_updated, (5,), "日期", None,
         "必须是普通「日期」字段——建成系统的「最后更新时间」类型机器写不进去，"
         "而且任何人工编辑都会刷新它，分层刷新的节奏会被打乱"),
        (f.alive_confirmed, (7,), "复选框", None, None),
        (f.consecutive_failures, (2,), "数字", None, None),
    ]


# 这些 ui_type 和普通数字/文本共用类型码（2/1），但写入行为完全不同：
# 评分字段封顶 5 星，写 like_count=3000 会失败或被截断。光看类型码抓不到。
_EXOTIC_UI_TYPES = {"Progress": "进度", "Currency": "货币",
                    "Rating": "评分", "Barcode": "条码"}


def _schema_problems(settings: Settings, meta: dict) -> list[str]:
    """按期望 schema 逐列核对 fields_meta 的结果，返回人话问题清单。

    独立成纯函数：doctor 调用它，测试也能直接喂假 meta 驱动。
    """
    f = settings.fields
    problems: list[str] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []
    # 这几列的写入有选项守卫（流量状态走 merge 过滤，评论状态/置顶状态
    # 写前核对选项清单）：缺选项会被安全跳过。其余带选项要求的列
    # （平台/巡查状态）是直写字符串，没有写侧守卫，缺选项的后果是
    # 写回可能失败——两种情况的文案必须如实区分。
    filtered_columns = {f.traffic_status, f.comment_status, f.negative_status,
                        f.pinned_status}
    for name, allowed, type_label, required_options, note in _expected_schema(settings):
        info = meta.get(name)
        if info is None:
            # 缺链接/巡查开关机器一行都读不出来；缺最近检查时间分层刷新
            # 失去依据，每轮 sweep 都会全表重刷烧钱——这三列单独点名。
            (missing_required if name in (f.link, f.monitoring, f.last_updated)
             else missing_optional).append(name)
            continue
        if info["type"] not in allowed:
            hint = f"。{note}" if note else ""
            problems.append(
                f"「{name}」的字段类型是「{_type_name(info['type'])}」，"
                f"需要「{type_label}」——列名对了但类型不对，"
                f"机器会读不到或写不进这一列{hint}"
            )
            continue
        ui = info.get("ui_type") or ""
        if ui in _EXOTIC_UI_TYPES:
            problems.append(
                f"「{name}」是「{_EXOTIC_UI_TYPES[ui]}」字段——它和普通"
                f"「{type_label}」共用类型码，但写入行为不同（评分封顶 5 星、"
                f"进度按百分比），请换成普通「{type_label}」"
            )
            continue
        # 类型对了再看选项。零选项的选择列 options 可能是 [] 也可能整个
        # 缺 options 键（API 行为未验证）——既然类型已确认是单选/多选，
        # 一律按「已建选项清单」对待，缺键当成空清单，别放行。
        if required_options and info["type"] in (3, 4):
            missing = [v for v in required_options if v not in (info["options"] or [])]
            if missing:
                if name in filtered_columns:
                    problems.append(
                        f"「{name}」缺这些选项，请先在飞书里手工建好：{'、'.join(missing)}。"
                        f"机器要写它们，没建就会被跳过（不会误写，但对应判定等于没生效）。"
                    )
                else:
                    problems.append(
                        f"「{name}」缺这些选项，请先在飞书里手工建好：{'、'.join(missing)}。"
                        f"机器写这一列时**不做选项过滤**，缺选项可能让该行整行写回失败。"
                    )
    if missing_required:
        problems.append(
            f"表里缺必备列：{'、'.join(missing_required)}——"
            f"缺「{f.link}」「{f.monitoring}」机器一行都读不出来；"
            f"缺「{f.last_updated}」分层刷新失去依据，每轮 sweep 都会全表重刷烧钱"
            "（sweep 会拒跑）。列名要和 config.py 逐字一致（含空格和标点）。"
        )
    if missing_optional:
        problems.append(
            f"表里缺这些列（列名要和 config.py 逐字一致）：{'、'.join(missing_optional)}。"
            "机器列没建会被自动跳过（不会写坏表），但对应的数据就落不下来。"
        )
    return problems


def _options_from_meta(meta, column: str):
    """从 fields_meta 的结果里取某列的选项清单，语义与旧 list_field_options 一致：
    None = 查不到别过滤（元数据整体读不到、或列不存在）；
    []   = 列存在但不是选择类字段，机器值全拦（写文本列本来就写不进多选列表）。
    """
    if meta is None or column not in meta:
        return None
    options = meta[column]["options"]
    return options if options is not None else []


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
        sample = table.search(f.must_read(), max_records=1)
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
        print(f"⚠ 表里还没建「{settings.fields.last_updated}」列：分层刷新没有依据，"
              "每一轮 sweep 都会全表重刷烧钱，先去建列")
        return 1, found, 0, 0.0, None
    if record_ids and not quiet_missing:
        missing = [rid for rid in record_ids if rid not in found]
        if missing:
            print(f"⚠ 这些 record_id 在表里没找到（可能拼错或已删除）：{'、'.join(missing)}")
    if not row_list:
        if not (record_ids and quiet_missing):
            print("没有需要刷新的行。")
        return (1 if record_ids and not quiet_missing else 0), found, 0, 0.0, None

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
    print(f"已写回 {written} 行")
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
    return 1 if (report.fatal or write_errors) else 0


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

    worst = 0
    found_all: set[str] = set()
    total_rows, total_yuan = 0, 0.0
    # 先把所有表都刷完、攒起来，写回放到跨表熔断之后（见下）。
    pending: list[tuple[str, feishu.Bitable, runner.RunReport, set[str] | None]] = []
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
            try:
                code, found, row_count, yuan, prep = _refresh_table(
                    mode, record_ids, settings, api_keys, table, now,
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
            total_yuan += yuan
            if prep is not None:
                report, fields_meta = prep
                pending.append((label, table, report, fields_meta))
                if report.fatal and not [k for k in api_keys if k not in disabled]:
                    channels_dead = True
    except BaseException:
        # 刷新阶段炸了（含 KeyboardInterrupt）：**已经付过钱**的表照样写回，
        # 再把异常抛出去。不这么做的话，一次意外就让这一轮花的钱全部蒸发，
        # 下一轮按原样的 last_updated 再付一次。
        if pending:
            print("\n⚠ 运行中断，先把已经完成的表写回（避免已付费的结果丢失）…")
            for label, table, report, fields_meta in pending:
                try:
                    _write_back_table(table, report, fields_meta, settings, label)
                except Exception as exc:  # noqa: BLE001
                    print(f"❌ 表 {label} 写回失败：{exc}")
        raise

    # 跨表熔断：单表可能只有三五行，永远凑不满熔断的最小样本，但上游
    # 故障是通道级的——把这一轮所有表的观测合起来再判一次，该作废的
    # 失效判定在写回**之前**作废掉。
    if len(pending) > 1 and runner.apply_cross_run_breaker(
            [report for _, _, report, _ in pending], settings):
        print("\n🛑 跨表熔断：本轮各表合计的失效比例异常偏高，疑似上游故障，"
              "已作废所有表的失效判定（明细见各表结果）")

    # 结构化事件在**所有**熔断（含上面这次跨表熔断）定案之后才发：
    # 提前发的话，被熔断改写过的行会让看板和告警看到一个比实际落表更吓人的
    # 结论，而那正好发生在上游故障、最不该误报的时候。
    for label, _table, report, _meta in pending:
        runner.emit_run_events(report, on_event, table=label)

    for label, table, report, fields_meta in pending:
        if multi:
            print(f"\n━━━━ 表：{label}（结果）━━━━")
        else:
            print()
        try:
            code = _write_back_table(table, report, fields_meta, settings, label)
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
                return 1
    if mode == "estimate" and multi:
        print(f"\n合计：待刷 {total_rows} 行，预计花费 ≈ ¥{total_yuan:.2f}")
    return worst


def main(argv: list[str]) -> int:
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
