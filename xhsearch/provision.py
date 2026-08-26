"""把一张表「接进来」：免费体检 → 建缺的列 → 入册。

这一层是面板「加表」按钮背后的东西，也是整个项目里少数几个**会改别人表
结构**的地方。三条纪律写在实现里：

1. **体检一分钱不花。** 只读字段元数据、只读一行——判定链路一个请求都不发。
2. **只追加。** 建列是 `POST fields`，这张表已有的列一个都不碰，
   建错了删掉那列就回退干净。**绝不改已有列的类型**（会转换/丢数据）。
3. **补选项默认不做。** `PUT fields` 对 property 是整体覆盖、飞书按 id 认
   选项，写错一次就是清空全表那一列的值且不可逆。默认只给清单，
   等真机验过（见 docs/待验证清单.md）再由 `PANEL_ALLOW_OPTION_PATCH` 放行。

「把应用加进这张表当协作者」这一步机器代替不了——飞书没有接口能让应用给
自己开别人文档的权限，有的话就是提权漏洞。体检读不到字段列表时，
九成是这一步没做，所以那句提示要写得能直接照着操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import feishu, schema, summary
from .config import Settings

# 「应用没被加进这张表」的标准话术。这是接管已有表时唯一必须人做的一步，
# 也是最容易卡住半天的一步——飞书对它是**静默返回空结果**，不报错。
NOT_A_COLLABORATOR = (
    "读不到这张表的字段列表。九成是应用还没被加进去：\n"
    "打开表格 → 右上角「···」→ 更多 → 添加文档应用 → 搜应用名 → 添加，"
    "权限给「可编辑」。\n"
    "如果这张表开了「高级权限」，还要进高级权限设置再给这个应用「可管理」——"
    "光「可编辑」不够。\n"
    "（飞书对这种情况是静默返回空结果、不报错，所以这里只能猜，"
    "但九成是它。）")


@dataclass
class Checkup:
    """一张候选表的体检结果。**做这件事不花钱。**"""

    label: str = ""
    target: str = ""
    app_token: str = ""
    table_id: str = ""
    reachable: bool = False
    error: str = ""
    diff: Optional[schema.SchemaDiff] = None
    sample_rows: Optional[int] = None
    duplicate: str = ""

    @property
    def ready(self) -> bool:
        """现在就能开始巡查吗。"""
        return (self.reachable and not self.duplicate
                and self.diff is not None and self.diff.clean)

    @property
    def buildable(self) -> list:
        return list(self.diff.missing_columns) if self.diff else []

    @property
    def manual(self) -> list[str]:
        """机器不做、要人去飞书处理的那几条。**这是唯一还要回飞书的清单。**"""
        if self.diff is None:
            return []
        out = [w.describe() for w in self.diff.wrong_types]
        out += [g.describe() for g in self.diff.missing_options]
        return out


def check(table: feishu.Bitable, settings: Settings, *,
          label: str = "", target: str = "",
          known_tables: Optional[list] = None) -> Checkup:
    """给一张候选表做体检。只读，不花钱。"""
    result = Checkup(label=label, target=target,
                     app_token=table.app_token, table_id=table.table_id)

    for existing_label, _app_token, table_id in (known_tables or []):
        if table_id == result.table_id:
            # 查重看 table_id 单独一维：同一张表用 /base/ 和 /wiki/ 两种链接
            # 各登记一次，app_token 不同但指的是同一张表，一轮内付两次钱。
            result.duplicate = (
                f"这张表已经在监控里了（叫「{existing_label}」）。"
                "同一张表登记两遍会一轮内付两次钱、两份旧快照互相覆盖")
            break

    try:
        meta = table.fields_meta()
    except Exception as exc:                                    # noqa: BLE001
        result.error = f"读字段元数据失败：{exc}"
        return result
    if not meta:
        result.error = NOT_A_COLLABORATOR
        return result

    result.reachable = True
    result.diff = schema.diff(settings, meta)
    try:
        # 读一行确认真读得到记录。字段列表读得到、记录读到 0 行，
        # 也是「没加协作者」的典型症状之一。
        result.sample_rows = len(table.search(
            [c for c in settings.fields.must_read() if c in meta], max_records=1))
    except Exception:                                           # noqa: BLE001
        result.sample_rows = None
    return result


@dataclass
class BuildResult:
    created: list = field(default_factory=list)
    options_added: dict = field(default_factory=dict)
    skipped_options: list = field(default_factory=list)
    failures: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"建了 {len(self.created)} 列（{'、'.join(self.created)}）")
        if self.options_added:
            done = "；".join(f"{k} +{'、'.join(v)}"
                             for k, v in self.options_added.items())
            parts.append(f"补了选项（{done}）")
        if self.skipped_options:
            parts.append(f"{len(self.skipped_options)} 处缺选项**没动**（见下）")
        if self.failures:
            parts.append(f"{len(self.failures)} 处失败")
        return "；".join(parts) or "没有要做的"


def build_missing(table: feishu.Bitable, diff: schema.SchemaDiff, *,
                  allow_option_patch: bool = False,
                  log=print) -> BuildResult:
    """把缺的列建出来。**只追加，绝不改已有列的类型。**

    每一次结构写入都单独打一条日志（表 + 动了哪一列），在 Railway 日志里
    可检索——这是「面板能改别人表结构」这件事唯一的审计线索。

    `allow_option_patch` 默认关：补选项对已有列是整体覆盖，未经真机验证之前
    只给清单。开之前请照 docs/待验证清单.md 在一张废表上验一次。
    """
    result = BuildResult()
    tag = f"{table.app_token[-6:]}/{table.table_id}"

    for column in diff.missing_columns:
        try:
            table.create_field(column.body())
        except Exception as exc:                                # noqa: BLE001
            result.failures.append(f"建列「{column.name}」失败：{exc}")
            log(f"🧱 [{tag}] 建列失败 {column.name}：{exc}")
            continue
        result.created.append(column.name)
        log(f"🧱 [{tag}] 建列 {column.describe()}")

    for gap in diff.missing_options:
        if not allow_option_patch:
            result.skipped_options.append(gap.describe())
            continue
        try:
            added = table.add_field_options(gap.column, gap.missing)
        except Exception as exc:                                # noqa: BLE001
            result.failures.append(f"给「{gap.column}」补选项失败：{exc}")
            log(f"🧱 [{tag}] 补选项失败 {gap.column}：{exc}")
            continue
        if added:
            result.options_added[gap.column] = added
            log(f"🧱 [{tag}] 补选项 {gap.column} +{'、'.join(added)}")

    # 类型建错的列一个字都不碰：改类型会转换/丢已有数据。
    return result


def create_monitored_table(workspace: feishu.Workspace, settings: Settings,
                           name: str) -> dict[str, str]:
    """从零建一张监控表：建 base → 建数据表（连列带选项一次建齐）。

    这条路**一次飞书都不用点**：应用自己建的 base，应用天然有完全权限，
    不需要「添加文档应用」。（这一点还没在真机上验过，见待验证清单。）
    """
    base = workspace.create_base(f"linkcheck · {name}")
    if not base["app_token"]:
        raise feishu.FeishuError(-1, "建 base 失败：飞书没返回 app_token")
    fields = [schema.create_field_body(col, allowed[0], options)
              for col, allowed, _label, options, _note
              in schema.expected_schema(settings)]
    table_id = workspace.create_table(base["app_token"], name, fields)
    if not table_id:
        raise feishu.FeishuError(-1, "建数据表失败：飞书没返回 table_id")
    # ⚠️ 链接必须带上**新建的这张表**的 table_id。`create_base` 会顺带建一张
    # 飞书自己的默认表，返回的 base 级 url 点进去就是那一张——运营可能直接
    # 在里面开始填数据，而注册表监控的是另一张，填的东西一行都不会被巡查。
    return {"app_token": base["app_token"], "table_id": table_id,
            "url": _table_url(base.get("url") or "", base["app_token"], table_id),
            "base_url": base.get("url") or "",
            # base 里那张默认表不删——删表不可逆，不该有一个网页按钮能干。
            # 页面上说一句就够了。
            "note": "这个 base 里还有一张飞书自动建的默认表，没在监控范围内，"
                    "别往那张里填东西",
            "target": f"{base['app_token']}:{table_id}"}


def _table_url(base_url: str, app_token: str, table_id: str) -> str:
    """把飞书返回的 base 级链接改写成指向具体某张表。

    只取 scheme+host 重拼，不在原地址后面接 `?table=`——那个地址可能已经
    带了查询串，接上去就成了两个 `?`。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        # 飞书没给可用的地址时，退回 base 级原文，别拼一个假的出来。
        return base_url
    return summary.table_url(f"{parts.scheme}://{parts.netloc}",
                             app_token, table_id)
