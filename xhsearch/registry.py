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

    @property
    def usable(self) -> bool:
        return self.enabled and not self.problem and bool(self.table_id)

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.label, self.app_token, self.table_id)


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
                          COL_STATUS, COL_FIRST_RUN) if c in meta]
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
        )
        if not entry.target:
            entry.problem = f"「{COL_TARGET}」是空的"
        else:
            try:
                target = tablespec.parse_target(
                    entry.target, default_label=entry.label)
                entry.app_token = target.app_token
                entry.table_id = target.table_id
                entry.label = entry.label or target.label
            except tablespec.BadTarget as exc:
                entry.problem = str(exc)
        entries.append(entry)

    # 查重只看**在用的**行：停用的那些留着当历史，不该因为和在用的撞了就报错。
    live = [tablespec.TableTarget(e.label, e.app_token, e.table_id)
            for e in entries if e.usable]
    duplicate = tablespec.find_duplicate(live)
    if duplicate:
        for entry in entries:
            if entry.usable and (entry.table_id in duplicate
                                 or entry.label in duplicate):
                entry.problem = duplicate
    return entries


def to_tuples(entries: list[Entry]) -> list[tuple[str, str, str]]:
    """能巡查的那些 → `cli._tables_from_env` 的形状。"""
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
