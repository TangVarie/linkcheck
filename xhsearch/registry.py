"""表清单存在哪儿：一张飞书多维表格。

## 为什么需要它

Railway 上是两个容器，**它们之间什么都不共享**：面板是常驻的，cron 每 5 分钟
起一次、跑完就退出。你在面板上点「加表」，这件事必须写到一个 **cron 五分钟后
读得到的地方**。没有共享磁盘（Railway 的 Volume 只能挂一个服务），没有共享内存。

四个候选里只有「一张飞书表」是通的：应用已经有飞书凭据、持久、不要钱，
而且人也看得懂——**面板挂了的时候，它是唯一还能止损的地方**（去掉某张表的
`启用`）。改环境变量要一个可写的 Railway token 且每次触发重新部署；
Volume 挂不到两个服务上；让 cron 去面板拉清单，则面板一崩巡检静默全停。

## 它是数据库，不是界面

`python3 cli.py init-registry` 自动建好，打印一行 `FEISHU_REGISTRY=...`
粘进 Railway，**这辈子就这一次**。之后加表删表全在面板上。

## 失败时绝不静默降级成零张表

读不到注册表（网络、权限、表被删）时用 `FEISHU_TABLES` 兜底并**大声警告**；
两个都没有就让调用方非零退出。静默跑成「零张表」= 进程正常退出、日志一切正常、
实际一行都不刷——这是这套东西最难发现的一种故障。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import feishu, tablespec

# 注册表自己的列名。刻意和业务表的列名（config.FieldNames）完全不重叠：
# 万一 FEISHU_REGISTRY 被误填成一张业务表，`looks_like_registry()` 才认得出来。
COL_LABEL = "项目名"
COL_TARGET = "表格链接"
COL_ENABLED = "启用"
COL_NOTE = "备注"
COL_STATUS = "入册状态"        # 机器写：体检结论
COL_CHECKED = "上次体检"       # 机器写：时间戳
COL_FIRST_RUN = "首轮已完成"   # 机器写：新表的小闸用它判断「还是不是首轮」

# —— 逐表阈值。空着 = 用全局默认（config.py 里那一套）——
#
# 只放**纯行级分类**的参数：它们没有跨表语义，一张表改了不影响另一张表的
# 任何判定。而共用一套本来就是错的——美妆客户和医药客户的评论量级差一个
# 数量级，config.py 自己就写着那几个默认值「是占位的，上线前必须按你们
# 自己的历史数据校准一次」。
#
# **熔断比例、熔断最小样本、两击定罪次数、冷却秒数、单轮硬预算不在这里，
# 而且不会加。** 它们有跨表语义——`runner.apply_cross_run_breaker` 要把各表
# 样本加总重算比例，逐表不同的熔断口径会让「这一轮到底该不该熔」说不清。
COL_TIER_EVALUATING = "评估中门槛"
COL_TIER_HOT = "爆贴门槛"
COL_TIER_SUPER_HOT = "大爆门槛"
COL_FLOP_HOURS = "无水花小时"
COL_ARCHIVE_DAYS = "归档天数"

THRESHOLD_COLUMNS = (COL_TIER_EVALUATING, COL_TIER_HOT, COL_TIER_SUPER_HOT,
                     COL_FLOP_HOURS, COL_ARCHIVE_DAYS)

# 建注册表时用的列定义（类型码见 schema.FIELD_TYPE_NAMES）。
REGISTRY_FIELDS = [
    {"field_name": COL_LABEL, "type": 1},
    {"field_name": COL_TARGET, "type": 1},
    {"field_name": COL_ENABLED, "type": 7},
    {"field_name": COL_NOTE, "type": 1},
    {"field_name": COL_STATUS, "type": 1},
    {"field_name": COL_CHECKED, "type": 5,
     "property": {"date_formatter": "yyyy/MM/dd HH:mm"}},
    {"field_name": COL_FIRST_RUN, "type": 7},
    *[{"field_name": name, "type": 2, "property": {"formatter": "0"}}
      for name in THRESHOLD_COLUMNS],
]

# 认出「这确实是一张注册表」要的最小列集。用来挡住 FEISHU_REGISTRY 被误填成
# 业务表——那种情况下机器会往运营的生产表里凭空 batch_create 出一批行。
_SIGNATURE = (COL_LABEL, COL_TARGET, COL_ENABLED)


class RegistryError(RuntimeError):
    pass


@dataclass
class Entry:
    """注册表里的一行。"""

    record_id: str = ""
    label: str = ""
    target: str = ""
    enabled: bool = True
    note: str = ""
    status: str = ""
    first_run_done: bool = False
    # 解析出来的结果；`problem` 非空表示这一行有毛病，不参与巡查。
    app_token: str = ""
    table_id: str = ""
    problem: str = ""
    # /base/ 还是 /wiki/。只用来拼给人点的链接，见 tablespec.TableTarget.route。
    route: str = "base"
    # 逐表阈值。None = 这一格空着，用全局默认。
    thresholds: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problem and bool(self.table_id)

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.label, self.app_token, self.table_id)

    def as_target(self) -> tablespec.TableTarget:
        return tablespec.TableTarget(self.label, self.app_token,
                                     self.table_id, route=self.route)


def looks_like_registry(meta: Optional[dict]) -> bool:
    """这张表是不是一张注册表。

    **写任何一行之前必须过这一关。** `FEISHU_REGISTRY` 填成一张业务表的话，
    机器会往运营的生产表里凭空造出一批行，而这个项目刻意不实现批量删行——
    只能一行行手工删。
    """
    if not meta:
        return False
    return all(name in meta for name in _SIGNATURE)


def read(table: feishu.Bitable) -> list[Entry]:
    """读注册表。**一行解析不了只影响它自己**，其余照常返回。

    这一点和 `cli._tables_from_env` 相反：环境变量是部署方填的，填错就该让
    整个进程起不来；注册表是运营填的，一行填错不该让其余项目停摆——
    那一行标成「配置有误」，面板上红着，剩下的照跑。
    """
    meta = table.fields_meta()
    if meta is None:
        raise RegistryError(
            "读不到注册表的字段列表。多半是应用没被加进这张表："
            "表格右上角「…」→「添加文档应用」，权限给「可编辑」")
    if not looks_like_registry(meta):
        raise RegistryError(
            f"FEISHU_REGISTRY 指向的表不像注册表——缺 "
            f"{'、'.join(n for n in _SIGNATURE if n not in meta)} 这几列。"
            "填成业务表了？机器绝不会往一张认不出来的表里写东西")

    wanted = [c for c in (COL_LABEL, COL_TARGET, COL_ENABLED, COL_NOTE,
                          COL_STATUS, COL_FIRST_RUN, *THRESHOLD_COLUMNS)
              if c in meta]
    entries: list[Entry] = []
    for record in table.search(wanted):
        cells = record.get("fields") or {}
        entry = Entry(
            record_id=record.get("record_id") or "",
            label=feishu.read_text(cells.get(COL_LABEL)).strip(),
            target=feishu.read_text(cells.get(COL_TARGET)).strip(),
            # 空着算启用：新加的一行忘了勾就静默不刷，是个很难发现的坑。
            enabled=feishu.read_bool(cells.get(COL_ENABLED))
            if COL_ENABLED in cells else True,
            note=feishu.read_text(cells.get(COL_NOTE)),
            status=feishu.read_text(cells.get(COL_STATUS)),
            first_run_done=feishu.read_bool(cells.get(COL_FIRST_RUN)),
            thresholds={name: feishu.read_int(cells.get(name))
                        for name in THRESHOLD_COLUMNS
                        if feishu.read_int(cells.get(name)) is not None},
        )
        if not entry.target:
            entry.problem = f"「{COL_TARGET}」是空的"
        else:
            try:
                target = tablespec.parse_target(
                    entry.target, default_label=entry.label)
                entry.app_token = target.app_token
                entry.table_id = target.table_id
                entry.route = target.route
                entry.label = entry.label or target.label
            except tablespec.BadTarget as exc:
                entry.problem = str(exc)
        entries.append(entry)

    # 查重只看**在用的**行：停用的那些留着当历史，不该因为和在用的撞了就报错。
    #
    # 用 `find_duplicates`（复数）而不是 `find_duplicate`：后者只报碰到的
    # 第一组，第二组重复会原样放行——那张表一轮里被排两遍，钱付两份、
    # 两份旧快照互相覆盖人工标签。
    # 而且是**精确查表**，不是拿 table_id 去那句话里做子串匹配：
    # 一个恰好是别处子串的 table_id 会被误伤，那种故障查起来毫无线索。
    live = [e.as_target() for e in entries if e.usable]
    duplicates = tablespec.find_duplicates(live)
    if duplicates:
        for entry in entries:
            if not entry.usable:
                continue
            message = duplicates.message_for(table_id=entry.table_id,
                                             label=entry.label)
            if message:
                entry.problem = message
    return entries


def to_targets(entries: list[Entry]) -> list:
    """能巡查的那些 → `TableTarget` 列表（带 `route`，拼链接要用）。"""
    return [e.as_target() for e in entries if e.usable]


def to_tuples(entries: list[Entry]) -> list[tuple[str, str, str]]:
    """能巡查的那些 → `(label, app_token, table_id)` 三元组。

    路由信息在这里会丢掉，只给不拼链接的调用方用；要拼链接走 `to_targets`。
    """
    return [e.as_tuple() for e in entries if e.usable]


def add(table: feishu.Bitable, *, label: str, target: str, note: str = "",
        enabled: bool = False, client_token: str) -> str:
    """往注册表加一行，返回 record_id。

    `enabled` 默认 **False**：新加的表体检多半还没过（应用没加协作者、
    列没建全），先让它躺在注册表里不参与巡查，体检绿了再由人点「启用」。
    默认开启的话，一张缺「最近检查时间」列的表会在下一轮 sweep 全表判到期。
    """
    meta = table.fields_meta()
    if not looks_like_registry(meta):
        raise RegistryError("目标表不像注册表，拒绝写入")
    fields: dict[str, Any] = {COL_LABEL: label, COL_TARGET: target,
                              COL_ENABLED: enabled}
    if note:
        fields[COL_NOTE] = note
    # 只写表里真有的列：注册表是机器建的，但人可能删过列。
    fields = {k: v for k, v in fields.items() if k in (meta or {})}
    created = table.batch_create([{"fields": fields}], client_token=client_token)
    return created[0] if created else ""


def set_enabled(table: feishu.Bitable, record_id: str, enabled: bool) -> None:
    """停用 / 启用一张表。**可逆，不动业务表里的任何数据。**"""
    table.batch_update([{"record_id": record_id,
                         "fields": {COL_ENABLED: enabled}}])


def remove(table: feishu.Bitable, record_id: str) -> None:
    """从注册表删掉一行 = 不再监控这张表。

    **只删注册表那一行，业务表和它的数据一个字都不动。**
    停止监控和删除数据是两回事。
    """
    table.delete_record(record_id)


@dataclass
class Health:
    """一行的体检结果，写回注册表的机器列，也给面板展示。"""

    ok: bool = False
    summary: str = ""
    problems: list = field(default_factory=list)


# ---------- 逐表阈值 ----------

@dataclass
class Override:
    """一张表的阈值覆盖，以及它有没有毛病。"""

    values: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.values)


def read_overrides(entry: Entry, base) -> Override:
    """把注册表那几格数字读成覆盖值。**纯函数。**

    每一格单独校验，一格填错只丢那一格、不影响别的——和「一行填错只影响
    那一行」是同一条纪律。校验不过的**不静默忽略**：写进 problems 让面板
    和日志都能看见。静默忽略一个配置，比不支持它更糟。

    三档门槛是唯一的例外：它们是**一组**，得按**生效后的值**一起看。
    `base` 就是为这个要的——只校验「填了的那几格」会漏掉两种形态：

    * 只填「爆贴门槛=10」，全局「评估中门槛」是 20 → 生效后是 (20, 10, 100)
    * 两档填成相等 → `heat_tier()` 从高往低判，下面那一档**永远够不着**

    两种都会让 cron 从下一轮起给这张表写错热度档，而热度是**棘轮
    （只升不降）**的——写错了不会自己回来。所以要求**严格递增**。
    """
    out = Override()
    raw = dict(entry.thresholds or {})

    tier_columns = (COL_TIER_EVALUATING, COL_TIER_HOT, COL_TIER_SUPER_HOT)
    tiers = {}
    for column in tier_columns:
        value = raw.get(column)
        if value is None:
            continue
        if value < 1:
            out.problems.append(f"「{column}」= {value}，要是正整数，已忽略")
            continue
        tiers[column] = value
    if tiers:
        # 没填的格子用全局值补齐，校验的是**这张表实际会用的那三个数**。
        fallback = {
            COL_TIER_EVALUATING: base.thresholds.tier_evaluating,
            COL_TIER_HOT: base.thresholds.tier_hot,
            COL_TIER_SUPER_HOT: base.thresholds.tier_super_hot,
        }
        effective = [tiers.get(c, fallback[c]) for c in tier_columns]
        a, b, c = effective
        if not (a < b < c):
            shown = "、".join(f"{col}={val}" for col, val
                             in zip(tier_columns, effective))
            out.problems.append(
                f"三档门槛生效后不是严格递增的（{shown}；没填的那几格用的是"
                "全局默认），整组已忽略——必须 评估中 < 爆贴 < 大爆，"
                "相等也不行：热度从高往低判，等值会让下面那一档永远够不着")
            tiers = {}
    out.values.update(tiers)

    for column, floor, ceiling in ((COL_FLOP_HOURS, 1, 24 * 365),
                                   (COL_ARCHIVE_DAYS, 1, 3650)):
        value = raw.get(column)
        if value is None:
            continue
        if not floor <= value <= ceiling:
            out.problems.append(
                f"「{column}」= {value} 超出 [{floor}, {ceiling}]，已忽略")
            continue
        out.values[column] = value
    return out


def apply_overrides(base, entry: Entry, *, log=None):
    """全局 Settings + 这一行的覆盖 → 这张表专用的 Settings。**纯函数。**

    深拷贝而不是就地改：`_run_locked` 的表循环共用一个基准 Settings，
    就地改会让第一张表的阈值串味到后面所有表——那种 bug 只在多表部署上
    出现，而且看起来像「判定口径莫名其妙」。
    """
    import copy

    override = read_overrides(entry, base)
    if log:
        for problem in override.problems:
            log(f"⚠ 注册表里「{entry.label}」的阈值有问题：{problem}")
    if not override.any:
        return base

    settings = copy.deepcopy(base)
    values = override.values
    if COL_TIER_EVALUATING in values:
        settings.thresholds.tier_evaluating = values[COL_TIER_EVALUATING]
    if COL_TIER_HOT in values:
        settings.thresholds.tier_hot = values[COL_TIER_HOT]
    if COL_TIER_SUPER_HOT in values:
        settings.thresholds.tier_super_hot = values[COL_TIER_SUPER_HOT]
    if COL_FLOP_HOURS in values:
        settings.thresholds.flop_hours = values[COL_FLOP_HOURS]
    if COL_ARCHIVE_DAYS in values:
        settings.refresh.archive_after_days = values[COL_ARCHIVE_DAYS]
    if log:
        shown = "、".join(f"{k}={v}" for k, v in sorted(values.items()))
        log(f"⚙ 「{entry.label}」用的是这张表自己的阈值：{shown}")
    return settings


def set_thresholds(table: feishu.Bitable, record_id: str,
                   values: dict) -> None:
    """写回逐表阈值。`None` / 空 = 清掉那一格，回到全局默认。"""
    fields = {}
    for column in THRESHOLD_COLUMNS:
        if column in values:
            fields[column] = values[column]
    if fields:
        table.batch_update([{"record_id": record_id, "fields": fields}])
