"""跨表聚合：一批飞书 record → 一个项目的快照。**纯函数，不发请求。**

面板存在的理由是「表多了以后一张张点开去看太麻烦」，所以这一层的产物
必须是**能直接下判断的东西**，而不是原始行的搬运：哪个项目该管了、
哪几行要人处理、为什么。

三条口径纪律，都是为了让面板上的数字和真正开跑时是同一个数：

1. **Row 的构造复用 `runner.row_from_record`**，不在这里重写读单元格的逻辑。
   两处各写一遍，面板算出来的「到期几行」迟早和 sweep 实际刷的行对不上。
2. **「到期」用 `Row.is_due`、「多少钱」用 `rows.estimate_yuan`**，
   和 `cli estimate` 走的是同一个函数，报出来的数字逐字相同。
3. **「卡住了」和「到期了」是两回事。** 到期只是说「下一轮该刷它」，
   而 cron 每 5 分钟就跑一次，任何时刻都有一批行是刚到期的——
   拿它当告警会天天红。真正要报的是**超过两倍间隔还没刷到**的行，
   那说明它一直没被轮到（预算触顶、一直报错、或者调度根本没在跑）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from . import feishu, rows as rows_mod, runner
from .config import Settings

# 「卡住了」的判据：超过它应有刷新间隔的这么多倍还没被刷到。
# 1 倍就是「刚到期」，每一轮都会有一堆，报出来没有信息量。
STALE_INTERVAL_MULTIPLE = 2.0


@dataclass
class TodoRow:
    """一行「要人处理的」。字段全部是给人看的，不参与任何判定。"""

    record_id: str
    project: str
    record_url: str
    link_cell: str
    # 勾「排队刷新」要拿这两个去定位那张表。
    app_token: str = ""
    table_id: str = ""
    # 已归档 = 超过 archive_after_days，本来就不再自动刷。
    # 面板默认**不把它们混进主待办列表**：批量勾选会绕过归档线，
    # 而面板恰好把跨表的老旧失效行摆在一屏、天然鼓励全选。
    archived: bool = False
    reasons: list[str] = field(default_factory=list)
    refresh_status: str = ""
    diagnosis: str = ""
    comment_count: Optional[int] = None
    traffic_tags: list[str] = field(default_factory=list)
    seed_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    checked_at_ms: Optional[int] = None
    # 只有 PANEL_SHOW_DIGEST=1 时才填。默认空——评论正文、昵称、IP 属地
    # 是别人的个人信息，不该因为「顺手」就上一个公网页面。
    digest: str = ""
    negative_digest: str = ""

    @property
    def key(self) -> str:
        """这一条待办的身份。**同一行因为不同原因上榜算不同的待办**——
        「这行开始有负面了」和「这行早就风控中」是两件事，
        后者已经看过不代表前者也看过。
        """
        return f"{self.table_id}:{self.record_id}:{','.join(sorted(self.reasons))}"


@dataclass
class ProjectSnapshot:
    """一个项目（= 一张表）此刻的样子。"""

    label: str
    app_token: str
    table_id: str
    table_url: str = ""
    # /base/ 还是 /wiki/，只用于拼链接。见 tablespec.TableTarget.route。
    route: str = "base"
    # 读这张表失败时只填这一个，其余字段保持零值。一张表挂了不该让整个面板空掉。
    error: str = ""
    # 体检问题（_schema_problems 的原文）。空 = 这张表配置没问题。
    health: list[str] = field(default_factory=list)

    total_rows: int = 0
    archived_rows: int = 0
    queued_rows: int = 0
    due_rows: int = 0
    due_yuan: float = 0.0
    # 非空 = 这张表的「到期待刷 / 预计花费」**算不出来**，不是 0。
    # 缺「最近检查时间」列时 load_rows 直接 return []，两个数都会是 0——
    # 而真相是「没有依据判到期」，把列建出来之后全表都会判到期。
    # 显示 0 会让人刚好在最该警惕的时候放下心来。
    estimate_blocked: str = ""
    stale_rows: int = 0
    oldest_checked_ms: Optional[int] = None
    never_checked_rows: int = 0

    refresh_status_counts: dict[str, int] = field(default_factory=dict)
    traffic_tag_counts: dict[str, int] = field(default_factory=dict)
    negative_rows: int = 0
    pin_lost_rows: int = 0

    # 关键词覆盖度。没填词的行机器**完全不碰**对应的状态列，所以那两格是空的——
    # 把这件事摆出来，运营才分得清「机器没判」和「这行压根没填词」。
    seed_keyword_rows: int = 0
    negative_keyword_rows: int = 0
    rows_without_seed_keyword: list[str] = field(default_factory=list)
    rows_without_negative_keyword: list[str] = field(default_factory=list)

    todos: list[TodoRow] = field(default_factory=list)
    # 按 max_todos 截掉了多少条。**必须显示出来**：把截断后的长度当成精确的
    # 「要人管」行数报出去，等于在大面积事故的时候少报，而那正是最不该少报
    # 的时候。0 = 一条没丢，那个计数就是精确的。
    todos_dropped: int = 0

    @property
    def needs_attention(self) -> int:
        return len(self.todos)

    @property
    def attention_exact(self) -> bool:
        """`needs_attention` 是不是精确值。False = 还有 `todos_dropped` 条没算进来。"""
        return self.todos_dropped == 0

    @property
    def healthy(self) -> bool:
        return not self.error and not self.health


def panel_fields(settings: Settings, *, show_digest: bool = False) -> list[str]:
    """面板一次 search 要拉的列。

    比 `FieldNames.must_read()` 宽：判定链路用不到 巡查状态/负面状态/诊断信息，
    但面板要靠它们回答「这行怎么了」。宽出来的这几列不花钱——
    飞书读表不计费，多读几列只是多几个字节。
    """
    f = settings.fields
    wanted = [
        f.link, f.publish_time, f.seed_keywords, f.negative_keywords,
        f.monitoring, f.queued, f.traffic_status, f.comment_count,
        f.last_updated, f.consecutive_failures, f.pinned_status,
        f.platform, f.refresh_status, f.failure_reason,
        f.comment_status, f.negative_status, f.alive_confirmed,
    ]
    if show_digest:
        wanted += [f.comment_digest, f.negative_digest]
    # 去重但保持顺序：列名允许被配置成同一个（不推荐，但不该在这里崩）。
    return list(dict.fromkeys(wanted))


def table_url(base: str, app_token: str, table_id: str,
              *, route: str = "base") -> str:
    """拼一个能点开这张表的链接。

    `route` 是原来那条链接走的 /base/ 还是 /wiki/（见
    `tablespec.TableTarget.route`）。一律拼成 /base/ 的话，用 wiki 链接
    登记的项目点开是打不开的——接口两种 token 通用，浏览器地址不通用。
    """
    prefix = "wiki" if route == "wiki" else "base"
    return f"{base.rstrip('/')}/{prefix}/{app_token}?table={table_id}"


def record_url(base: str, app_token: str, table_id: str, record_id: str,
               *, route: str = "base") -> str:
    """直达某一行。面板的全部价值有一半在这个链接上——
    「这 7 行要处理」如果还要人自己去表里找，等于没解决问题。
    """
    return (f"{table_url(base, app_token, table_id, route=route)}"
            f"&record={record_id}")


def _ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _is_stale(row: rows_mod.Row, settings: Settings, now: datetime) -> bool:
    """这一行是不是「早该刷到却一直没轮到」。

    归档的行不算（它本来就不再自动刷）。发布时间读不出来的行也不算——
    那是「发布时间那一格有问题」，`诊断信息` 里已经在报了，
    在这里再报一次只会让同一个毛病占两个位置。
    """
    age = row.age_days(now)
    if age is None:
        return False
    interval = settings.refresh.interval_hours_for_age(age)
    if interval is None:
        return False
    updated = _ms(row.last_updated_ms)
    if updated is None:
        # 在管、没归档、却从来没被刷过。这正是最该报的一种。
        return True
    elapsed = (now - updated).total_seconds() / 3600
    return elapsed >= interval * STALE_INTERVAL_MULTIPLE


def build_snapshot(
    *,
    label: str,
    app_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    settings: Settings,
    now: Optional[datetime] = None,
    api_keys: Optional[dict[str, str]] = None,
    health: Optional[list[str]] = None,
    feishu_base: str = "https://feishu.cn",
    show_digest: bool = False,
    max_todos: int = 200,
    scrub: Optional[Any] = None,
    route: str = "base",
) -> ProjectSnapshot:
    """把一张表读回来的 records 聚合成一个项目快照。

    records 应该是**在管的行**（`是否巡查` 勾着的）。取消勾选就是运营
    明确说了「这行别管了」，把它算进任何一个数字都是在制造噪声。
    """
    now = now or datetime.now(timezone.utc)
    clean = scrub or (lambda text: text)
    f = settings.fields
    snap = ProjectSnapshot(
        label=label, app_token=app_token, table_id=table_id, route=route,
        table_url=table_url(feishu_base, app_token, table_id, route=route),
        health=list(health or []),
    )

    due: list[rows_mod.Row] = []
    todos: list[TodoRow] = []
    for record in records:
        cells = record.get("fields") or {}
        row = runner.row_from_record(record, settings)
        snap.total_rows += 1

        age = row.age_days(now)
        archived = age is not None and settings.refresh.interval_hours_for_age(age) is None
        if archived:
            snap.archived_rows += 1
        if row.queued:
            snap.queued_rows += 1
        if row.last_updated_ms is None:
            snap.never_checked_rows += 1
        elif (snap.oldest_checked_ms is None
              or row.last_updated_ms < snap.oldest_checked_ms):
            snap.oldest_checked_ms = row.last_updated_ms
        # `queued` 也要算：`runner.load_rows` 决定花钱时写的是
        # `wanted or row.queued or not only_due or row.is_due(...)`。
        # 光看 is_due 会漏掉「人工勾了、但还没到自然间隔」的行——**恰恰是
        # 面板自己那个批量勾按钮制造出来的行**。漏了就意味着：用完面板的
        # 批量勾，面板显示的待刷行数和预估花费比下一轮真实要花的少。
        # （is_due 对已归档行返回 False，而 queued 本来就绕过归档线，
        # 这个写法把两边的语义也对齐了。）
        if row.queued or row.is_due(settings, now):
            due.append(row)

        refresh_status = feishu.read_text(cells.get(f.refresh_status))
        if refresh_status:
            snap.refresh_status_counts[refresh_status] = \
                snap.refresh_status_counts.get(refresh_status, 0) + 1
        for tag in row.current_tags:
            snap.traffic_tag_counts[tag] = snap.traffic_tag_counts.get(tag, 0) + 1

        negative_status = feishu.read_text(cells.get(f.negative_status))
        if negative_status == settings.negative_status.found:
            snap.negative_rows += 1
        if row.pin_status == settings.pin_status.pinned_lost:
            snap.pin_lost_rows += 1

        if row.seed_keywords:
            snap.seed_keyword_rows += 1
        elif len(snap.rows_without_seed_keyword) < max_todos:
            snap.rows_without_seed_keyword.append(row.record_id)
        if row.negative_keywords:
            snap.negative_keyword_rows += 1
        elif len(snap.rows_without_negative_keyword) < max_todos:
            snap.rows_without_negative_keyword.append(row.record_id)

        stale = _is_stale(row, settings, now)
        if stale:
            snap.stale_rows += 1

        reasons = _todo_reasons(
            row, settings, refresh_status=refresh_status,
            negative_status=negative_status, stale=stale)
        if reasons:
            # **全部收进来，排完序再截断**（见函数末尾）。在这里按飞书返回的
            # 记录顺序截，一次大面积事故就能用低优先级的「卡住了」占满前
            # max_todos 格，把后面的 风控中/已失效/刷新失败 整个藏掉。
            # 待办只是异常行，records 本来就整份在内存里，全收不多占什么。
            todos.append(TodoRow(
                archived=archived,
                record_id=row.record_id,
                project=label,
                record_url=record_url(feishu_base, app_token, table_id,
                                      row.record_id, route=route),
                app_token=app_token,
                table_id=table_id,
                link_cell=row.link_cell,
                reasons=reasons,
                refresh_status=refresh_status,
                diagnosis=clean(feishu.read_text(cells.get(f.failure_reason))),
                comment_count=row.previous_comment_count,
                traffic_tags=list(row.current_tags),
                seed_keywords=list(row.seed_keywords),
                negative_keywords=list(row.negative_keywords),
                checked_at_ms=row.last_updated_ms,
                digest=(clean(feishu.read_text(cells.get(f.comment_digest)))
                        if show_digest else ""),
                negative_digest=(clean(feishu.read_text(cells.get(f.negative_digest)))
                                 if show_digest else ""),
            ))

    snap.due_rows = len(due)
    if due:
        snap.due_yuan = rows_mod.estimate_yuan(due, settings, now, keys=api_keys)
    todos.sort(key=_todo_sort_key)
    snap.todos = todos[:max_todos]
    snap.todos_dropped = max(0, len(todos) - max_todos)
    return snap


# 待办的严重度排序。数字小的排前面。
# 风控和失效是「可能已经出事了」，负面是「有人在说话」，卡住是「机器没在干活」，
# 置顶掉了是「要补一个动作」——这个顺序就是运营该处理的顺序。
_REASON_RANK = {
    "风控中": 0, "已失效": 1, "刷新失败": 2, "有负面": 3,
    "疑似限流": 4, "卡住了": 5, "置顶掉了": 6, "疑似受限": 7,
}


def _todo_sort_key(todo: TodoRow) -> tuple:
    best = min((_REASON_RANK.get(r, 99) for r in todo.reasons), default=99)
    return (best, todo.project, todo.record_id)


def _todo_reasons(row: rows_mod.Row, settings: Settings, *,
                  refresh_status: str, negative_status: str,
                  stale: bool) -> list[str]:
    """这一行为什么需要人看一眼。空 = 不需要。

    只报**机器已经下过结论**的事和「机器没在干活」这一件事。
    不在这里自己重新判一遍热度或掉量——那是 `analyze` 的活，
    面板再判一次就会出现「面板说是爆贴、表里写着评估中」这种谁都不信的局面。
    """
    reasons: list[str] = []
    tags = set(row.current_tags)
    if settings.tags.risk and settings.tags.risk in tags:
        reasons.append(settings.tags.risk)
    if settings.tags.throttled and settings.tags.throttled in tags:
        reasons.append(settings.tags.throttled)
    if refresh_status in (runner.STATUS_GONE, runner.STATUS_FAILED,
                          runner.STATUS_SUSPECT):
        reasons.append(refresh_status)
    if negative_status == settings.negative_status.found:
        reasons.append(settings.negative_status.found)
    if row.pin_status == settings.pin_status.pinned_lost:
        reasons.append(settings.pin_status.pinned_lost)
    if stale:
        reasons.append("卡住了")
    # 去重但保持顺序：`风控中` 可能同时来自标签和「已失效」，报两遍没意义。
    return list(dict.fromkeys(reasons))


@dataclass
class Overview:
    """所有项目合起来的样子。顶栏就是它。"""

    projects: list[ProjectSnapshot] = field(default_factory=list)
    generated_at: Optional[datetime] = None

    @property
    def total_rows(self) -> int:
        return sum(p.total_rows for p in self.projects)

    @property
    def due_rows(self) -> int:
        return sum(p.due_rows for p in self.projects)

    @property
    def due_yuan(self) -> float:
        return sum(p.due_yuan for p in self.projects)

    @property
    def queued_rows(self) -> int:
        return sum(p.queued_rows for p in self.projects)

    @property
    def stale_rows(self) -> int:
        return sum(p.stale_rows for p in self.projects)

    @property
    def unestimatable(self) -> list[ProjectSnapshot]:
        """算不出「要花多少钱」的项目。顶栏那个金额是**不含它们**的下界，
        所以必须单独说一句，否则那个数字看着像全部。"""
        return [p for p in self.projects if p.estimate_blocked]

    @property
    def unhealthy_projects(self) -> list[ProjectSnapshot]:
        return [p for p in self.projects if not p.healthy]

    @property
    def todos_dropped(self) -> int:
        """各项目按 max_todos 截掉的总条数。>0 = 顶栏那个「要人管」是下界。"""
        return sum(p.todos_dropped for p in self.projects)

    def todos_dropped_by(self, limit: int = 500, *,
                         include_archived: bool = False) -> int:
        """`todos(limit)` 这一次调用又额外截掉了多少条。

        和 `todos_dropped` 分开：那个是每张表内部截的，这个是跨表拉平之后
        再截的。两个加起来才是「一共有多少条没显示」。
        """
        total = sum(len([t for t in p.todos
                         if include_archived or not t.archived])
                    for p in self.projects)
        return max(0, total - limit)

    def todos(self, limit: int = 500, *, include_archived: bool = False
              ) -> list[TodoRow]:
        """跨表拉平的待办。**这是整个面板的价值所在**——
        运营看到的是「这 7 行要处理」，而不是「去 5 张表里找」。

        默认**不含已归档的行**。它们超过了 archive_after_days、本来就不再
        自动刷，而「排队刷新」会绕过归档线——把一堆老帖混进这一屏，
        再配上一个「全选」，就是一次把钱花在几个月前的内容上。
        要看它们走 `archived_todos()`，那边是折叠的、要单独确认。
        """
        merged: list[TodoRow] = []
        for project in self.projects:
            merged.extend(t for t in project.todos
                          if include_archived or not t.archived)
        merged.sort(key=_todo_sort_key)
        return merged[:limit]

    def archived_todos(self, limit: int = 200) -> list[TodoRow]:
        """已归档但仍有异常的行。单独一区，重刷要另外确认。"""
        merged = [t for p in self.projects for t in p.todos if t.archived]
        merged.sort(key=_todo_sort_key)
        return merged[:limit]


# ---------- 改阈值之前先算给人看 ----------

@dataclass
class TierShift:
    """按新口径，这个项目有多少行的热度档会变。"""

    changed: int = 0
    up: int = 0
    down_blocked: int = 0
    examples: list = field(default_factory=list)

    def describe(self) -> str:
        if not self.changed and not self.down_blocked:
            return "按新口径，这个项目没有行的档位会变。"
        parts = []
        if self.up:
            parts.append(f"{self.up} 行会**升档**（下一轮生效）")
        if self.down_blocked:
            parts.append(
                f"{self.down_blocked} 行按新口径本该**降档，但不会降**"
                "——热度档是棘轮（只升不降），改阈值不回溯")
        return "；".join(parts) + "。"


def preview_tier_shift(records: list[dict], old: Settings, new: Settings,
                       *, now: Optional[datetime] = None,
                       max_examples: int = 5) -> TierShift:
    """改阈值会影响哪些行。**纯函数，不发请求、不花钱**——
    用表里已经有的评论数就能算。

    这是改阈值唯一的真实副作用，所以保存前必须摆出来：热度档是**棘轮
    （只升不降）**的，改完不回溯。调低门槛的行下一轮会升上去；
    调高门槛的行**不会降回来**，于是表里会短暂并存两套口径打出来的标签。
    """
    now = now or datetime.now(timezone.utc)
    shift = TierShift()
    for record in records:
        row = runner.row_from_record(record, old)
        count = row.previous_comment_count
        if count is None:
            continue
        before = _tier_for(row, count, old, now)
        after = _tier_for(row, count, new, now)
        if before == after:
            continue
        rank_before = old.tags.rank(before or "")
        rank_after = new.tags.rank(after or "")
        if rank_after > rank_before:
            shift.changed += 1
            shift.up += 1
            if len(shift.examples) < max_examples:
                shift.examples.append(
                    f"{row.record_id}：{count} 条评论 {before or '—'} → {after}")
        else:
            # 棘轮：算出来更低也不会真降。摆出来，别让人以为改完就回退了。
            shift.down_blocked += 1
    return shift


def _tier_for(row: rows_mod.Row, count: int, settings: Settings,
              now: datetime) -> Optional[str]:
    """这一行按这套口径落在哪一档。和 analyze 那边同一套判据。"""
    tier = settings.thresholds.heat_tier(count, settings.tags)
    if tier:
        return tier
    age = row.age_hours(now)
    if age is None:
        return None
    if age < settings.thresholds.flop_hours:
        return settings.tags.observing or None
    return settings.tags.flop or None
