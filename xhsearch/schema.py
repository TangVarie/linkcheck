"""表结构的期望值，以及「现有表和期望差在哪」。**纯函数，不发请求。**

这是 `docs/表结构.md` 的机器可执行版，而且是**唯一的一份**。三个地方用它：

* `cli doctor` —— 逐列核对，报出人话问题
* 写回前的类型闸 —— 一列建错只摘掉它自己，不作废整表
* 监控面板 —— 同一份定义既当体检判据，将来也当建表脚本

从 `cli.py` 挪进包里，是因为面板要用它，而 `cli` 反过来 import 包——
留在 cli 里就是循环依赖。挪动只改了位置，逻辑一行没动，
`cli._expected_schema` 这些名字仍然指向这里（旧测试照常绿）。
"""

from __future__ import annotations

from . import runner
from .config import Settings


# 飞书多维表格字段类型码 → 界面上的叫法。体检报错时把数字翻译成人话。
FIELD_TYPE_NAMES = {
    1: "文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期", 7: "复选框",
    11: "人员", 13: "电话号码", 15: "超链接", 17: "附件", 18: "单向关联",
    19: "查找引用", 20: "公式", 21: "双向关联", 22: "地理位置", 23: "群组",
    1001: "创建时间", 1002: "最后更新时间", 1003: "创建人", 1004: "修改人",
    1005: "自动编号",
}


def type_name(code) -> str:
    return FIELD_TYPE_NAMES.get(code, f"未知类型 {code}")


def expected_schema(settings: Settings) -> list[tuple]:
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
EXOTIC_UI_TYPES = {"Progress": "进度", "Currency": "货币",
                    "Rating": "评分", "Barcode": "条码"}


def schema_problems(settings: Settings, meta: dict) -> list[str]:
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
    for name, allowed, type_label, required_options, note in expected_schema(settings):
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
                f"「{name}」的字段类型是「{type_name(info['type'])}」，"
                f"需要「{type_label}」——列名对了但类型不对，"
                f"机器会读不到或写不进这一列{hint}"
            )
            continue
        ui = info.get("ui_type") or ""
        if ui in EXOTIC_UI_TYPES:
            problems.append(
                f"「{name}」是「{EXOTIC_UI_TYPES[ui]}」字段——它和普通"
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


def options_from_meta(meta, column: str):
    """从 fields_meta 的结果里取某列的选项清单，语义与旧 list_field_options 一致：
    None = 查不到别过滤（元数据整体读不到、或列不存在）；
    []   = 列存在但不是选择类字段，机器值全拦（写文本列本来就写不进多选列表）。
    """
    if meta is None or column not in meta:
        return None
    options = meta[column]["options"]
    return options if options is not None else []
