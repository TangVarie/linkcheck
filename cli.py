#!/usr/bin/env python3
"""命令行入口。

    python3 cli.py doctor              # 体检：不花积分，检查配置/权限/字段是否齐全
    python3 cli.py sweep               # 分层巡检：只刷到期的行
    python3 cli.py queue               # 只刷勾了「排队刷新」的行
    python3 cli.py row <record_id>...  # 刷指定行（无视冷却和分层节流）
    python3 cli.py estimate            # 只估算这一轮要花多少钱，不发请求

配置走环境变量，见 .env.example。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from xhsearch import feishu, providers, rows as rows_mod, runner
from xhsearch.config import Channels, Settings


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        sys.exit(f"缺少环境变量 {name}（参考 .env.example）")
    return value


def _numeric_env(name: str, cast, default):
    """数值型环境变量。填错时给一句能看懂的话，而不是一屏 ValueError 回溯。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        sys.exit(f"环境变量 {name} 的值 {raw!r} 不是数字（参考 .env.example）")


def _apply_endpoint_overrides() -> None:
    """按部署位置切 TikHub 的接入域名。

    对方文档要求「请勿跨区使用，会影响访问速度」：
        境内（扣子、国内 VPS）  → api.tikhub.dev（默认，不用设）
        境外（Railway、Actions）→ TIKHUB_BASE=https://api.tikhub.io
    """
    providers.set_tikhub_base(os.environ.get("TIKHUB_BASE", ""))


def _api_keys() -> dict[str, str]:
    """双通道的凭据。两个都配就自动开降级；只配一个也能跑。

    环境变量名和供应商名一一对应，加第三家时不用改这里的结构。
    """
    keys = {
        providers.TIKHUB: os.environ.get("TIKHUB_API_KEY", "").strip(),
        providers.SOCIALDATAX: os.environ.get("SOCIALDATAX_API_KEY", "").strip(),
    }
    return {k: v for k, v in keys.items() if v}


def _table() -> feishu.Bitable:
    return feishu.Bitable(
        app_id=_env("FEISHU_APP_ID"),
        app_secret=_env("FEISHU_APP_SECRET"),
        app_token=_env("FEISHU_APP_TOKEN"),
        table_id=_env("FEISHU_TABLE_ID"),
    )


def _settings() -> Settings:
    _apply_endpoint_overrides()
    settings = Settings()
    # 独立服务跑批量不需要软截止（那是给扣子 60 秒硬上限准备的）。
    settings.soft_deadline_seconds = _numeric_env("SOFT_DEADLINE_SECONDS", float, 0.0)
    settings.max_concurrency = _numeric_env("MAX_CONCURRENCY", int, settings.max_concurrency)
    settings.detail_within_days = _numeric_env("DETAIL_WITHIN_DAYS", int, settings.detail_within_days)
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
        (f.pinned_comment, (1,), "文本", None, None),
        (f.comment_status, (4,), "多选", settings.comment_status.machine_written(),
         "机器按多选合并写入——建成单选会让写回整批失败"),
        (f.comment_digest, (1,), "文本", None, None),
        (f.seed_match, (1,), "文本", None, None),
        (f.traffic_status, (4,), "多选", settings.tags.namespace(),
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
    # 这两列走 merge(known_options=...)：缺选项会被安全跳过。
    # 其余带选项要求的列（平台/巡查状态）是直写字符串，没有写侧过滤，
    # 缺选项的后果是写回可能失败——两种情况的文案必须如实区分。
    filtered_columns = {f.traffic_status, f.comment_status}
    for name, allowed, type_label, required_options, note in _expected_schema(settings):
        info = meta.get(name)
        if info is None:
            # 没有链接和巡查开关，机器连一行都读不出来——单独点名。
            (missing_required if name in (f.link, f.monitoring)
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
            f"表里缺必备列：{'、'.join(missing_required)}——没有它们机器一行都处理不了。"
            "列名要和 config.py 逐字一致（含空格和标点）。"
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


def cmd_doctor() -> int:
    """上线前体检。不花一分钱，但能挡掉九成的「配好了跑不通」。"""
    settings = _settings()
    f = settings.fields
    table = _table()
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

    print("④ 数据通道 …")
    keys = _api_keys()
    for platform in ("xhs", "douyin"):
        order = settings.channels.for_platform(platform)
        live = providers.usable_order(settings.channels, platform, keys)
        label = "小红书" if platform == "xhs" else "抖音"
        if not live:
            print(f"   {label}：❌ 配置的是 {'、'.join(order)}，但一个 Key 都没有")
            problems.append(f"{label}没有可用的数据通道，任何刷新都会立刻失败")
        elif len(live) == 1:
            print(f"   {label}：⚠ 只有 {live[0]} 一条通道，它挂了这一轮就全丢")
        else:
            print(f"   {label}：✅ {live[0]} 为主，{'、'.join(live[1:])} 兜底")
    if keys:
        print(f"   已配置的 Key：{'、'.join(sorted(keys))}"
              "（是否有效需要真实调用一次才知道）")

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, problem in enumerate(problems, 1):
            print(f"  {i}. {problem}")
        return 1
    print("✅ 全部通过，可以开跑")
    return 0


def _run(mode: str, record_ids: list[str] | None) -> int:
    settings = _settings()
    table = _table()
    api_keys = _api_keys()
    if not api_keys:
        sys.exit("一个数据通道的 Key 都没配：需要 TIKHUB_API_KEY 或 SOCIALDATAX_API_KEY"
                 "（参考 .env.example）")
    now = datetime.now(timezone.utc)

    print(f"读表（模式：{mode}）…")
    # 字段元数据读一次、处处共用：读侧过滤 search 请求的列（请求不存在的
    # 列会让整个 search 失败），写侧过滤还没建的机器列，两个选择列的
    # 选项清单也从同一份里取——省两次分页请求。
    fields_meta = table.fields_meta()
    known_fields = set(fields_meta) if fields_meta is not None else None
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
    if mode == "queue" and not row_list and known_fields is not None \
            and settings.fields.queued not in known_fields:
        print(f"⚠ 表里还没建「{settings.fields.queued}」列，queue 模式无法工作，先去建列")
        return 1
    if record_ids:
        found = {r.record_id for r in row_list}
        missing = [rid for rid in record_ids if rid not in found]
        if missing:
            print(f"⚠ 这些 record_id 在表里没找到（可能拼错或已删除）：{'、'.join(missing)}")
            if not row_list:
                return 1
    if not row_list:
        print("没有需要刷新的行。")
        return 0

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

    if mode == "estimate":
        return 0

    report = runner.refresh(
        row_list, api_keys, settings,
        now=now,
        known_options=_options_from_meta(fields_meta, settings.fields.traffic_status),
        comment_status_options=_options_from_meta(fields_meta, settings.fields.comment_status),
        forced=(record_ids is not None),
        progress=print,
    )
    print()
    print(report.summary())

    write_errors: list = []
    dropped_fields: set = set()
    try:
        written = runner.write_back(table, report, errors=write_errors,
                                    known_fields=known_fields,
                                    dropped_fields=dropped_fields)
    except feishu.FeishuError as exc:
        # 表级错误（权限、列名、token）：逐行重试无意义，说清楚原因退出。
        # 未写回的行 last_updated 没动，下一轮会自然重捞。
        print(f"❌ 写回失败（表级错误）：{exc}")
        return 1
    print(f"已写回 {written} 行")
    if dropped_fields:
        print(f"⚠ 这些列在表里还没建，本轮已跳过（建好后下一轮自动补上）："
              f"{'、'.join(sorted(dropped_fields))}")
    if write_errors:
        print(f"⚠ {len(write_errors)} 行写回失败（其余行不受影响）：")
        for record_id, exc in write_errors:
            print(f"  {record_id}: {exc}")
    # 退出码语义：到软截止「留给下一轮」是正常运行返回 0（返回非零会让
    # cron / 云平台的重启策略把它当失败反复重启）；真故障（Key/余额）和
    # 「花了钱但有行没写回」都返回非零——花出去的钱没落进表里，
    # 不能让 cron 和 Actions 显示一个绿色的成功。
    return 1 if (report.fatal or write_errors) else 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "doctor":
        return cmd_doctor()
    if command in ("sweep", "queue", "estimate"):
        return _run(command, None)
    if command == "row":
        if len(argv) < 3:
            sys.exit("用法：python3 cli.py row <record_id> [<record_id> ...]")
        return _run("row", argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
