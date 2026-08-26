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


@dataclass
class ProjectSnapshot:
    """一个项目（= 一张表）此刻的样子。"""

    label: str
    app_token: str
    table_id: str
    table_url: str = ""
    # 读这张表失败时只填这一个，其余字段保持零值。一张表挂了不该让整个面板空掉。
    error: str = ""
    # 体检问题（_schema_problems 的原文）。空 = 这张表配置没问题。
    health: list[str] = field(default_factory=list)

    total_rows: int = 0
    archived_rows: int = 0
    queued_rows: int = 0
    due_rows: int = 0
    due_yuan: float = 0.0
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

    @property
    def needs_attention(self) -> int:
        return len(self.todos)

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


def table_url(base: str, app_token: str, table_id: str) -> str:
    return f"{base.rstrip('/')}/base/{app_token}?table={table_id}"


def record_url(base: str, app_token: str, table_id: str, record_id: str) -> str:
    """直达某一行。面板的全部价值有一半在这个链接上——
    「这 7 行要处理」如果还要人自己去表里找，等于没解决问题。
    """
    return f"{table_url(base, app_token, table_id)}&record={record_id}"


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
) -> ProjectSnapshot:
    """把一张表读回来的 records 聚合成一个项目快照。

    records 应该是**在管的行**（`是否巡查` 勾着的）。取消勾选就是运营
    明确说了「这行别管了」，把它算进任何一个数字都是在制造噪声。
    """
    now = now or datetime.now(timezone.utc)
    clean = scrub or (lambda text: text)
    f = settings.fields
    snap = ProjectSnapshot(
        label=label, app_token=app_token, table_id=table_id,
        table_url=table_url(feishu_base, app_token, table_id),
        health=list(health or []),
    )

    due: list[rows_mod.Row] = []
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
        if row.is_due(settings, now):
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
        if reasons and len(snap.todos) < max_todos:
            snap.todos.append(TodoRow(
                record_id=row.record_id,
                project=label,
                record_url=record_url(feishu_base, app_token, table_id, row.record_id),
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
    snap.todos.sort(key=_todo_sort_key)
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
    def unhealthy_projects(self) -> list[ProjectSnapshot]:
        return [p for p in self.projects if not p.healthy]

    def todos(self, limit: int = 500) -> list[TodoRow]:
        """跨表拉平的待办。**这是整个面板的价值所在**——
        运营看到的是「这 7 行要处理」，而不是「去 5 张表里找」。
        """
        merged: list[TodoRow] = []
        for project in self.projects:
            merged.extend(project.todos)
        merged.sort(key=_todo_sort_key)
        return merged[:limit]
