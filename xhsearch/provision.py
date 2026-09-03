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

反过来，**应用自己建的表**是应用的文档，把人和群加成协作者是它的本分
（见 share_table）——不加的话人打开只有「可阅读」，连分享范围都动不了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from . import feishu, schema, summary
from .config import Settings
from .schema import expected_schema

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


# ---------- 从零建表：标准业务表模板 ----------
#
# 「直接新建一张」原来只建巡查要用的那二十来列。但运营真正用的表远不止这些
# ——素人编号、文案、配图、截图、蓝词、笔记状态……建完还得回飞书手工补十几列，
# 等于没省事。下面这份模板照 2026-09 的「西屋」表导出逐列抄的：列名、类型、
# 选项、**顺序**都跟它一致，巡查列夹在业务列中间的位置也原样保留
# （运营看惯了那个顺序，把机器列全堆到末尾反而要重新找）。
#
# 巡查列**不在这里重复定义**：它们的名字和类型只有 schema.expected_schema
# 一份真相，这里只按角色名引用（"link"、"traffic_status"……），改列名两边不漂。

TEXT, NUMBER, SINGLE, MULTI, DATE, CHECKBOX, ATTACHMENT, LINK, FORMULA = (
    1, 2, 3, 4, 5, 7, 17, 18, 20)


@dataclass(frozen=True)
class BusinessColumn:
    """一列机器**完全不读不写**的业务列。选项为空 = 建成空的选择列，运营
    往里填的时候飞书会自动加选项（蓝词、作者这类每个项目都不一样的词）。"""

    name: str
    type_code: int
    options: tuple = ()
    # 单向关联到**本表**（「父记录」）。property 里要填 table_id，而 table_id
    # 要等表建出来才有——所以这种列不能跟着 create_table 一次带上，得建完再补。
    self_link: bool = False
    # 公式列。公式按列名引用别的列（[最近检查时间]），那些列得先存在，
    # 所以同样是建完再补。飞书的 POST fields 只会追加到末尾，这两种列
    # 在新表里会排在最后，而不是西屋表里的位置——顺序在视图里拖一下就好。
    formula: str = ""

    @property
    def deferred(self) -> bool:
        return self.self_link or bool(self.formula)


def next_check_formula(settings: Settings) -> str:
    """「下次检查时间」的公式。里层是运营给的原文，只把列名换成配置里的：

        [最近检查时间] + IF(DATEDIF([发布时间], NOW(), "D") <= 2, 8,
                         IF(DATEDIF([发布时间], NOW(), "D") <= 7, 24, 72)) / 24

    发布 2 天内每 8 小时、7 天内每 24 小时、之后每 72 小时——和分层刷新的
    节奏一致，表里看得到「下一次大概什么时候来」。

    外面再套一层：发布超过归档天数（全局默认 30 天）就显示空——归档后 sweep
    不再自动刷这一行，再显示一个「下次检查」就是在报一个不会发生的时间。
    用的是全局默认值：建表时这张表还没入册，没有逐表覆盖可看；改过「归档天数」
    的表，去列设置里把那个数改一下就行。
    """
    f = settings.fields
    age = f'DATEDIF([{f.publish_time}], NOW(), "D")'
    days = settings.refresh.archive_after_days
    inner = f"[{f.last_updated}] + IF({age} <= 2, 8, IF({age} <= 7, 24, 72)) / 24"
    return f'IF({age} > {days}, "", {inner})'


# 字符串 = 巡查列的角色名（settings.fields 上的属性），BusinessColumn = 业务列。
FULL_LAYOUT: tuple = (
    BusinessColumn("素人编号", TEXT),
    "publish_time",
    "link",
    BusinessColumn("已删除评论留底", ATTACHMENT),
    "traffic_status",
    "surge_time",
    BusinessColumn("笔记状态", MULTI, ("已发布", "重点关注⭐️", "控评&置顶✅",
                                      "已投流，等回收数据", "待回复", "待删除竞品评论")),
    BusinessColumn("方向", SINGLE, ("流量贴", "产品贴")),
    BusinessColumn("找人备注", TEXT),
    BusinessColumn("内容配图", ATTACHMENT),
    BusinessColumn("文案", TEXT),
    BusinessColumn("随贴评论", TEXT),
    BusinessColumn("评论的素人编号", TEXT),
    BusinessColumn("评论配图", ATTACHMENT),
    "comment_status",
    BusinessColumn("发布截图", ATTACHMENT),
    BusinessColumn("相关截图", ATTACHMENT),
    # 蓝词是人工在手机端自查的，机器不碰（docs/表结构.md §〇）；每个项目的词
    # 都不一样，所以建成空多选，填的时候自动加选项。
    BusinessColumn("蓝词字段", MULTI),
    BusinessColumn("蓝词图片", ATTACHMENT),
    BusinessColumn("作者", SINGLE),
    BusinessColumn("父记录", LINK, self_link=True),
    "negative_keywords",
    "seed_keywords",
    "monitoring",
    "queued",
    "failure_reason",
    "pinned_status",
    "alive_confirmed",
    "refresh_status",
    "negative_status",
    "negative_digest",
    "comment_digest",
    "comment_count",
    "last_updated",
    # 公式本体在 next_check_formula()（要按配置里的列名拼），这里只占位。
    BusinessColumn("下次检查时间", FORMULA, formula="<next_check_formula>"),
    "consecutive_failures",
    "platform",
    "previous_comment_count",
)

# 模板里有但刻意不建的列。现在是空的：西屋表的列全部建得出来。
# 留着这个口子是给将来「这一列机器不该猜」的情况用——那种列宁可不建、
# 明说没建，也别建一个看着像真的的。
NOT_BUILT: tuple = ()

# 人机共用的「流量状态」在业务表里还有两个人工选项。新表一起带上是纯追加、
# 零风险；接管已有表时**不**补它们——补选项对已有列是整体覆盖（见 build_missing）。
EXTRA_OPTIONS = {"traffic_status": ("观察中", "爆帖预备")}

TEMPLATES = ("full", "monitor")


def template_fields(settings: Settings, template: str = "full"
                    ) -> tuple[list[dict], list[BusinessColumn], list[str]]:
    """把模板翻译成建表请求：(建表时一次带上的列, 建完再补的自关联列, 没建的列)。

    `monitor` = 只建巡查列（旧行为，顺序按 expected_schema）；
    `full` = 按西屋表的结构连业务列一起建。
    """
    if template not in TEMPLATES:
        raise ValueError(f"不认识的建表模板 {template!r}，只有 {'/'.join(TEMPLATES)}")
    expected = {name: (allowed, list(options or []))
                for name, allowed, _label, options, _note in expected_schema(settings)}
    if template == "monitor":
        return ([schema.create_field_body(name, allowed[0], options or None)
                 for name, (allowed, options) in expected.items()], [], [])

    fields: list[dict] = []
    deferred: list[BusinessColumn] = []
    placed: set = set()
    for item in FULL_LAYOUT:
        if isinstance(item, str):
            name = getattr(settings.fields, item)
            allowed, options = expected[name]
            options = options + [o for o in EXTRA_OPTIONS.get(item, ()) if o not in options]
            fields.append(schema.create_field_body(name, allowed[0], options or None))
            placed.add(name)
        elif item.deferred:
            if item.formula == "<next_check_formula>":
                item = BusinessColumn(item.name, item.type_code,
                                      formula=next_check_formula(settings))
            deferred.append(item)
        else:
            fields.append(schema.create_field_body(
                item.name, item.type_code, list(item.options) or None))
    # expected_schema 以后再加的巡查列，模板还没来得及排位置也不能漏建——
    # 少一列巡查列的后果（体检红、数据落不下来）比排在末尾大得多。
    for name, (allowed, options) in expected.items():
        if name not in placed:
            fields.append(schema.create_field_body(name, allowed[0], options or None))
    return fields, deferred, list(NOT_BUILT)


# ---------- 建好之后：把人和群加成协作者 ----------


@dataclass(frozen=True)
class SharePlan:
    """建完表要给谁开什么权限。全部来自环境变量，每次建表都一样——
    运营背后的飞书账号各不相同，所以给的是**群**（可编辑）；管理权限给
    具体的人（可管理），不再全绑在应用这个机器人身上。"""

    managers: tuple = ()       # 可管理（full_access）：手机号 / 邮箱 / ou_ 开头的 open_id
    editor_chats: tuple = ()   # 可编辑（edit）：oc_ 开头的 chat_id。应用得先在群里
    owner: str = ""            # 把所有权转给这个人（应用保留可管理）。可选
    # 给人看的名字：open_id 是一串乱码，面板上和日志里要显示的是「138****」
    # 或「梨响运营群」。键是上面三项里的 ID，缺就显示 ID 本身。
    labels: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.managers or self.editor_chats or self.owner)

    def label(self, member_id: str) -> str:
        return self.labels.get(member_id) or member_id


@dataclass
class ShareResult:
    granted: list = field(default_factory=list)
    failures: list = field(default_factory=list)


_PHONE = re.compile(r"^\+?\d{6,15}$")


def normalize_mobile(value: str) -> str:
    """去掉空格和连字符。大陆号码飞书收裸的 11 位；非大陆要带 + 区号，原样保留。"""
    return re.sub(r"[\s\-]", "", (value or "").strip())


def member_type(member_id: str) -> str:
    """一个 ID 长什么样就是什么：邮箱 / 手机号 / ou_ 开头的 open_id /
    oc_ 开头的 chat_id。认不出返回空串——宁可拒掉让人改配置，也别猜一种
    类型送给飞书。手机号不是协作者接口认的类型，要先换成 open_id
    （见 share_table）。"""
    value = (member_id or "").strip()
    if "@" in value:
        return "email"
    if value.startswith("ou_"):
        return "openid"
    if value.startswith("oc_"):
        return "openchat"
    if _PHONE.match(normalize_mobile(value)):
        return "mobile"
    return ""


def resolve_person(workspace: feishu.Workspace, who: str, role: str,
                   failures: list) -> Optional[tuple[str, str]]:
    """把一个「人」的写法变成协作者接口认的 (member_type, member_id)。
    认不出 / 找不到 → 记一条失败，返回 None。"""
    kind = member_type(who)
    if kind in ("email", "openid"):
        return kind, who.strip()
    if kind == "mobile":
        mobile = normalize_mobile(who)
        try:
            open_id = workspace.resolve_open_id(mobile=mobile)
        except Exception as exc:                                # noqa: BLE001
            failures.append(
                f"按手机号 {who} 找用户失败：{exc}。要给应用开 "
                "contact:user.id:readonly（通过手机号或邮箱获取用户 ID）权限，开完发布新版本")
            return None
        if not open_id:
            failures.append(
                f"飞书里没有手机号 {who} 对应的用户——号码要和飞书账号绑定的一致，"
                "非大陆号码带 + 区号")
            return None
        return "openid", open_id
    failures.append(f"「{who}」认不出是谁：{role}只认手机号、邮箱，或 ou_ 开头的 open_id")
    return None


def share_table(workspace: feishu.Workspace, app_token: str, plan: SharePlan,
                *, log=print) -> ShareResult:
    """按 SharePlan 给一张**应用自己建的** base 加协作者。

    一条失败不拦下一条，也不拦建表——表已经建好了，权限少给一个人的代价是
    去飞书里补一下，比「建了又建」便宜。每一步单独打日志：给谁开了什么权限
    是这套东西改别人可见范围的唯一审计线索。
    """
    result = ShareResult()
    tag = app_token[-6:]
    for who in plan.managers:
        shown = plan.label(who)
        person = resolve_person(workspace, who, "可管理的人", result.failures)
        if person is None:
            continue
        kind, member_id = person
        try:
            workspace.add_member(app_token, kind, member_id, "full_access")
        except Exception as exc:                                # noqa: BLE001
            result.failures.append(f"给「{shown}」开可管理失败：{exc}")
            log(f"🔑 [{tag}] 开可管理失败 {shown}：{exc}")
            continue
        result.granted.append(f"{shown} 可管理")
        log(f"🔑 [{tag}] 协作者 {shown} 可管理")
    for chat in plan.editor_chats:
        shown = plan.label(chat)
        if member_type(chat) != "openchat":
            result.failures.append(
                f"「{chat}」不是群 ID：群要填 oc_ 开头的 chat_id"
                "（面板「选群」或 python3 cli.py chats 能列出应用所在的群）")
            continue
        try:
            workspace.add_member(app_token, "openchat", chat, "edit")
        except Exception as exc:                                # noqa: BLE001
            result.failures.append(
                f"给群「{shown}」开可编辑失败：{exc}。飞书要求应用本身在这个群里"
                "——群设置里把它作为机器人加进去，再来一次")
            log(f"🔑 [{tag}] 开群可编辑失败 {shown}：{exc}")
            continue
        result.granted.append(f"群 {shown} 可编辑")
        log(f"🔑 [{tag}] 协作者 群 {shown} 可编辑")
    if plan.owner:
        shown = plan.label(plan.owner)
        person = resolve_person(workspace, plan.owner, "所有者", result.failures)
        if person is not None:
            kind, member_id = person
            try:
                workspace.transfer_owner(app_token, kind, member_id)
            except Exception as exc:                            # noqa: BLE001
                result.failures.append(f"把所有权转给「{shown}」失败：{exc}")
                log(f"🔑 [{tag}] 转移所有权失败 {shown}：{exc}")
            else:
                result.granted.append(f"{shown} 所有者（应用保留可管理）")
                log(f"🔑 [{tag}] 所有权 → {shown}（应用保留可管理）")
    return result


def create_monitored_table(workspace: feishu.Workspace, settings: Settings,
                           name: str, *, template: str = "full",
                           share: Optional[SharePlan] = None,
                           log=print) -> dict:
    """从零建一张监控表：建 base → 建数据表（连列带选项一次建齐）→ 补自关联列
    和公式列（要等表和被引用的列先存在）→ 按 SharePlan 加协作者。

    这条路**一次飞书都不用点**：应用自己建的 base，应用天然有完全权限，
    不需要「添加文档应用」。（这一点还没在真机上验过，见待验证清单。）
    """
    fields, deferred, skipped = template_fields(settings, template)
    base = workspace.create_base(f"linkcheck · {name}")
    if not base["app_token"]:
        raise feishu.FeishuError(-1, "建 base 失败：飞书没返回 app_token")
    table_id = workspace.create_table(base["app_token"], name, fields)
    if not table_id:
        raise feishu.FeishuError(-1, "建数据表失败：飞书没返回 table_id")
    built = [f["field_name"] for f in fields]
    column_failures: list[str] = []
    for column in deferred:
        if column.formula:
            prop: dict = {"formula_expression": column.formula}
        else:
            prop = {"table_id": table_id, "multiple": False}
        body = {"field_name": column.name, "type": column.type_code, "property": prop}
        try:
            workspace.create_field(base["app_token"], table_id, body)
        except Exception as exc:                                # noqa: BLE001
            # 表已经建好了。一列关联列没补上，不该让整张表变成「建失败」。
            column_failures.append(f"建列「{column.name}」失败：{exc}")
            log(f"🧱 [{base['app_token'][-6:]}/{table_id}] 补列失败 {column.name}：{exc}")
        else:
            built.append(column.name)
    shared = share_table(workspace, base["app_token"], share, log=log) if share else ShareResult()
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
            "target": f"{base['app_token']}:{table_id}",
            "template": template,
            "columns": len(built), "built": built,
            "skipped_columns": skipped, "column_failures": column_failures,
            "shared": shared.granted, "share_failures": shared.failures}


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
