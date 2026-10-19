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

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import feishu, links, rows as rows_mod, runner
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
    # 人一眼认得出「这是哪一条」的短标识。运营拿它去飞书表里 Ctrl+F
    # 直接定位，不用每条都点开一个新网页——「去这一行」是兜底不是主路径。
    # 来源见 `row_label()`。
    label: str = ""
    # 勾「排队刷新」要拿这两个去定位那张表。
    app_token: str = ""
    table_id: str = ""
    # 已归档 = 超过 archive_after_days，本来就不再自动刷。
    # 面板默认**不把它们混进主待办列表**（除非刚查过，见 recent）：批量勾选会
    # 绕过归档线，而面板恰好把跨表的老旧失效行摆在一屏、天然鼓励全选。
    archived: bool = False
    # 没勾「是否巡查」、但最近 ADHOC_WINDOW_HOURS 小时里被查过（多半是有人勾了
    # 「排队刷新」临时查一下）的行。进主列表，但「全选」不带它：
    # 没勾巡查是运营说了「这行别管了」，全选重刷等于替人改了主意。
    adhoc: bool = False
    # 最近 ADHOC_WINDOW_HOURS 小时里查过。已归档的行查过不久（多半是有人勾了排队
    # 刷新单独看一眼）照样进主列表；过了这个窗口才收进那个项目底下的归档折叠区。
    recent: bool = False
    reasons: list[str] = field(default_factory=list)
    refresh_status: str = ""
    diagnosis: str = ""
    comment_count: Optional[int] = None
    traffic_tags: list[str] = field(default_factory=list)
    seed_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    checked_at_ms: Optional[int] = None
    # 「排队刷新」此刻勾着 = 有人要求刷、cron 还没来接。这是面板上唯一能
    # 分开「同样的错」和「还没刷回来」的信号：勾着，看到的就还是旧结果；
    # 勾没了、「最近检查」变新了，看到的才是这一次的结论。
    queued: bool = False
    # 只有 PANEL_SHOW_DIGEST=1 时才填。默认空——评论正文、昵称、IP 属地
    # 是别人的个人信息，不该因为「顺手」就上一个公网页面。
    digest: str = ""
    negative_digest: str = ""
    # 只在 Overview.resolved 里的行有：运营什么时候点的「已解决」（毫秒）。
    resolved_at_ms: Optional[int] = None

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

    # 「发布时间」那一格空着（或读不出来）、链接里也解不出发布时刻的在管行。
    # 帖龄算不出来的行**不自动巡查**（见 rows.Row.refresh_interval_hours；以前
    # 按最快一档一直刷、永不归档，是个会安静烧钱的洞）——于是它们安静地停着，
    # 补上发布时间才会开始查。这件事原来在面板上一点声音都没有：`_is_stale` 故意不把它算成
    # 「卡住了」，`_todo_reasons` 只收机器下过结论的事，`analyze` 那句
    # ⚠ 也只在**评论数够不上任何热度档**时才写进「诊断信息」——
    # 一条有热度、发布时间空着的行，三道口全部躲过去。所以单独数一个。
    # 只数勾着「是否巡查」的行：没勾的多半是还没发的草稿，
    # 那种行本来就不刷，报出来只是噪声（Ziao 2026-09 拍板）。
    missing_publish_time_rows: int = 0
    rows_without_publish_time: list[str] = field(default_factory=list)

    refresh_status_counts: dict[str, int] = field(default_factory=dict)
    traffic_tag_counts: dict[str, int] = field(default_factory=dict)
    negative_rows: int = 0
    pin_lost_rows: int = 0
    # 「整理状态 = 需重新整理」的行：评论数比数据整理那一刻涨了一大截。
    recollect_rows: int = 0

    # 关键词覆盖度。没填词的行机器**完全不碰**对应的状态列，所以那两格是空的——
    # 把这件事摆出来，运营才分得清「机器没判」和「这行压根没填词」。
    seed_keyword_rows: int = 0
    negative_keyword_rows: int = 0
    rows_without_seed_keyword: list[str] = field(default_factory=list)
    rows_without_negative_keyword: list[str] = field(default_factory=list)

    todos: list[TodoRow] = field(default_factory=list)
    # 没勾「是否巡查」、最近被临时查过、查出了问题的行（见 TodoRow.adhoc）。
    # 不进 todos、不进这张卡上的任何计数——卡上的数字只说在管的行；
    # 主列表那边由 Overview.todos() 合进去。
    adhoc_todos: list[TodoRow] = field(default_factory=list)
    # 按 max_todos 截掉了多少条。**必须显示出来**：把截断后的长度当成精确的
    # 「要人管」行数报出去，等于在大面积事故的时候少报，而那正是最不该少报
    # 的时候。0 = 一条没丢，那个计数就是精确的。
    todos_dropped: int = 0
    # adhoc_todos 按条数上限截掉了多少条（for_display 填）。和 todos_dropped
    # 分开记：那个说的是项目卡上「要人管」准不准，这个不进项目卡。
    adhoc_dropped: int = 0

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


# 「哪一条」优先去这些列里找。第一个命中的就用，所以顺序 = 优先级。
# 「笔记内容」排第一是因为运营表里那一列就叫这个名字（2026-08 确认）；
# 后面几个是兜底，别的表换了叫法不至于整栏空掉。
LABEL_CANDIDATES = ("笔记内容", "文案", "笔记标题", "标题", "内容")


def pick_label_column(known: set[str], configured: str = "") -> str:
    """这张表拿哪一列当「哪一条」。**纯函数**，在 search 之前调。

    要在 search 之前定下来，因为按名字请求一个不存在的列会让整个 search
    报 1254045、一行都读不回来（和 `runner.load_rows` 同一条纪律）。
    字段元数据 `_collect_one` 本来就读了，列名是白拿的。

    `configured`（`PANEL_LABEL_COLUMN`）写了就**只认它**：显式配置压过自动识别，
    表里没有那一列就返回空串，让调用方去报「列名写错了」，
    而不是悄悄换一列——悄悄换等于配置没生效却没人知道。
    """
    if configured:
        return configured if configured in known else ""
    for name in LABEL_CANDIDATES:
        if name in known:
            return name
    return ""


def row_label(row: rows_mod.Row, cells: dict, settings: Settings,
              column: str = "") -> str:
    """这一行给人看的短标识。**纯函数。**

    运营拿它去飞书表里 Ctrl+F 直接定位——「去这一行」那个链接是兜底，
    不该是主路径（每条都要开一个新网页，很麻烦）。

    取值顺序：

    1. `column`（`pick_label_column` 挑出来的内容列）的文本——**主路径**；
    2. 「反馈链接」那格**抠掉链接之后**剩的文字：分享文案开头往往就是笔记标题。
       注意不能直接用整格原文，那一格多数是 `https://www.xiaohongsh…`，
       每一行前十几个字**一模一样**，等于没显示；
    3. 笔记 ID——认不出内容，但在表里搜得到；
    4. 都没有 → 空串，视图渲染成「—」。
    """
    if column:
        text = feishu.read_text(cells.get(column)).strip()
        if text:
            return text
    text = links.text_without_urls(row.link_cell or "")
    if text:
        return text
    return row.parsed.content_id or ""


def panel_fields(settings: Settings, *, show_digest: bool = False,
                 extra: tuple = ()) -> list[str]:
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
        f.collation_status,
        # 抖音短链记过的「作品ID」：没有它，面板认不出这一行是哪个作品，
        # 「发布时间」空着时就算不出帖龄、把 cron 早已归档的行报成空转。
        f.resolved_id,
    ]
    if show_digest:
        wanted += [f.comment_digest, f.negative_digest]
    # 额外要读的列（「哪一条」那一列，见 `pick_label_column`；截图读数的
    # 「相关截图」「数据整理」，见 panel._collect_one）。
    # 读表不计费，多读一列只是多几个字节。
    wanted += [c for c in extra if c]
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
    if row.schedule_age_days(settings, now) is None:
        return False
    # 和 Row.is_due 用同一个间隔（按上次检查时刻定档），否则面板说的
    # 「卡住了」和机器真正的到期判断不是一个口径。
    interval = row.refresh_interval_hours(settings, now)
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
    label_column: str = "",
    seen_calls: Optional[set] = None,
    truncate: bool = True,
) -> ProjectSnapshot:
    """把一张表读回来的 records 聚合成一个项目快照。

    seen_calls：跨表共用的调用指纹集合（rows.paying_rows）。同一篇内容
    登记在两张表里时，cron 一轮只付一次钱，「预计花费」也只算在先读到的
    那张表头上——各项目加起来才等于真花销。

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

        # 和 cron 同一个口径：「发布时间」空着时按链接 ID 推的保守帖龄（见
        # Row.schedule_publish_ms），算不出来的才是真空转。
        age = row.schedule_age_days(settings, now)
        archived = age is not None and settings.refresh.interval_hours_for_age(age) is None
        if archived:
            snap.archived_rows += 1
        # 帖龄算不出来 = 那一格是空的或者是脏的，两种都不巡查（见 Row.refresh_interval_hours）。
        # `row.monitoring` 在表里没有「是否巡查」列时默认 True，于是这个数
        # 和这张卡上别的数一个口径（那种表另有一条体检提示在说这件事）。
        if age is None and row.monitoring:
            snap.missing_publish_time_rows += 1
            if len(snap.rows_without_publish_time) < max_todos:
                snap.rows_without_publish_time.append(row.record_id)
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
        if row.collation_status == settings.collation_status.stale:
            snap.recollect_rows += 1

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
            todos.append(_make_todo(
                row, cells, settings, reasons=reasons, refresh_status=refresh_status,
                project=label, app_token=app_token, table_id=table_id,
                feishu_base=feishu_base, route=route, label_column=label_column,
                show_digest=show_digest, clean=clean, archived=archived,
                recent=_checked_recently(row, now)))

    snap.due_rows = len(due)
    if due:
        # 同一篇内容一轮只付一次钱（runner.FetchShare），按「真正付钱的行」估。
        snap.due_yuan = rows_mod.estimate_yuan(
            rows_mod.paying_rows(due, settings, now, seen=seen_calls),
            settings, now, keys=api_keys)
    todos.sort(key=_todo_sort_key)
    # truncate=False：全留着，截断交给出页面那一步（面板要先按「已解决」拿掉
    # 一些行再截，否则被收起来的行占着前 max_todos 格，后面的永远轮不上来，
    # 见 for_display）。
    snap.todos = todos[:max_todos] if truncate else todos
    snap.todos_dropped = max(0, len(todos) - max_todos) if truncate else 0
    return snap


# 待办的严重度排序。数字小的排前面。
# 风控和失效是「可能已经出事了」，负面是「有人在说话」，卡住是「机器没在干活」，
# 置顶掉了是「要补一个动作」——这个顺序就是运营该处理的顺序。
_REASON_RANK = {
    "风控中": 0, "已失效": 1, "刷新失败": 2, "有负面": 3,
    "疑似限流": 4, "卡住了": 5, "置顶掉了": 6, "需重新整理": 7, "疑似受限": 8,
}


def _make_todo(row: rows_mod.Row, cells: dict, settings: Settings, *,
               reasons: list[str], refresh_status: str, project: str,
               app_token: str, table_id: str, feishu_base: str, route: str,
               label_column: str, show_digest: bool, clean,
               archived: bool = False, adhoc: bool = False,
               recent: bool = False) -> TodoRow:
    f = settings.fields
    return TodoRow(
        archived=archived,
        adhoc=adhoc,
        recent=recent,
        record_id=row.record_id,
        project=project,
        record_url=record_url(feishu_base, app_token, table_id,
                              row.record_id, route=route),
        app_token=app_token,
        table_id=table_id,
        link_cell=row.link_cell,
        label=clean(row_label(row, cells, settings, label_column)),
        reasons=reasons,
        refresh_status=refresh_status,
        diagnosis=clean(feishu.read_text(cells.get(f.failure_reason))),
        comment_count=row.previous_comment_count,
        traffic_tags=list(row.current_tags),
        seed_keywords=list(row.seed_keywords),
        negative_keywords=list(row.negative_keywords),
        checked_at_ms=row.last_updated_ms,
        queued=row.queued,
        digest=(clean(feishu.read_text(cells.get(f.comment_digest)))
                if show_digest else ""),
        negative_digest=(clean(feishu.read_text(cells.get(f.negative_digest)))
                         if show_digest else ""),
    )


# 「最近查过」的窗口（小时）。两处用：没勾巡查的行，这么久以内查过才上面板；
# 已归档的行，这么久以内查过还在主列表，过了才收进归档折叠区。
ADHOC_WINDOW_HOURS = 24


def _checked_recently(row: rows_mod.Row, now: datetime) -> bool:
    if row.last_updated_ms is None:
        return False
    since_ms = int((now - timedelta(hours=ADHOC_WINDOW_HOURS)).timestamp() * 1000)
    return row.last_updated_ms >= since_ms


def adhoc_todos(
    *,
    label: str,
    app_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    settings: Settings,
    now: Optional[datetime] = None,
    feishu_base: str = "https://feishu.cn",
    show_digest: bool = False,
    max_todos: Optional[int] = 200,
    scrub: Optional[Any] = None,
    route: str = "base",
    label_column: str = "",
) -> list[TodoRow]:
    """**没勾「是否巡查」**的行里，最近 ADHOC_WINDOW_HOURS 小时被查过、而且
    查出了问题的那些。

    「排队刷新」不看「是否巡查」（见 runner.load_rows）：结案的表里想临时查
    一行，勾上排队就会刷。以前刷出来的问题只落在飞书那一行里，面板只读在管的
    行，一个字都看不到。窗口之外的不算——那是很久以前查的，早就不是「刚查出来」。
    「卡住了」不判：没勾巡查的行本来就不自动刷，谈不上卡住。
    """
    now = now or datetime.now(timezone.utc)
    clean = scrub or (lambda text: text)
    f = settings.fields
    out: list[TodoRow] = []
    for record in records:
        cells = record.get("fields") or {}
        row = runner.row_from_record(record, settings)
        if not _checked_recently(row, now):
            continue
        refresh_status = feishu.read_text(cells.get(f.refresh_status))
        reasons = _todo_reasons(
            row, settings, refresh_status=refresh_status,
            negative_status=feishu.read_text(cells.get(f.negative_status)), stale=False)
        if reasons:
            out.append(_make_todo(
                row, cells, settings, reasons=reasons, refresh_status=refresh_status,
                project=label, app_token=app_token, table_id=table_id,
                feishu_base=feishu_base, route=route, label_column=label_column,
                show_digest=show_digest, clean=clean, adhoc=True, recent=True))
    out.sort(key=_todo_sort_key)
    return out if max_todos is None else out[:max_todos]


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
    # 评论数比数据整理那一刻涨了一大截：要人重新截图回收后台数据。
    # 和「置顶掉了」一样是「要补一个动作」，不是出事了。
    if row.collation_status == settings.collation_status.stale:
        reasons.append(settings.collation_status.stale)
    if stale:
        reasons.append("卡住了")
    # 去重但保持顺序：`风控中` 可能同时来自标签和「已失效」，报两遍没意义。
    return list(dict.fromkeys(reasons))


@dataclass
class Overview:
    """所有项目合起来的样子。顶栏就是它。"""

    projects: list[ProjectSnapshot] = field(default_factory=list)
    generated_at: Optional[datetime] = None
    # 注册表里停用了的表的名字。它们不在 projects 里——停用的表不读、不统计、
    # 行也不进待办——但页面上得说一句，否则「停用了」和「读不到了」分不开。
    disabled_tables: list[str] = field(default_factory=list)
    # 点了「已解决」、此刻被收起来的行（apply_resolutions 填）。不在 projects 里。
    resolved: list[TodoRow] = field(default_factory=list)

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
        return sum(p.todos_dropped + p.adhoc_dropped for p in self.projects)

    def todos_dropped_by(self, limit: int = 500, *,
                         include_archived: bool = False) -> int:
        """`todos(limit)` 这一次调用又额外截掉了多少条。

        和 `todos_dropped` 分开：那个是每张表内部截的，这个是跨表拉平之后
        再截的。两个加起来才是「一共有多少条没显示」。
        """
        total = len(self._main(include_archived))
        return max(0, total - limit)

    def todos(self, limit: int = 500, *, include_archived: bool = False
              ) -> list[TodoRow]:
        """跨表拉平的待办。**这是整个面板的价值所在**——
        运营看到的是「这 7 行要处理」，而不是「去 5 张表里找」。

        默认**不含已归档的行**。它们超过了 archive_after_days、本来就不再
        自动刷，而「排队刷新」会绕过归档线——把一堆老帖混进这一屏，
        再配上一个「全选」，就是一次把钱花在几个月前的内容上。
        要看它们走 `archived_todos()`，那边是折叠的、要单独确认。

        例外是**最近 24 小时查过的**：已归档的行有人勾排队刷新单独看了一眼、
        没勾巡查的行被临时查了一下——刚查出来的问题照样进主列表（TodoRow.recent /
        adhoc）。过了这个窗口，已归档的收进折叠区，没勾巡查的就不再显示。
        """
        merged = self._main(include_archived)
        merged.sort(key=_todo_sort_key)
        return merged[:limit]

    def _main(self, include_archived: bool = False) -> list[TodoRow]:
        out: list[TodoRow] = []
        for project in self.projects:
            out.extend(t for t in project.todos
                       if include_archived or not t.archived or t.recent)
            out.extend(project.adhoc_todos)
        return out

    def archived_todos(self, limit: int = 200) -> list[TodoRow]:
        """已归档、而且 24 小时内没人查过、仍挂着问题的行——每个项目底下的折叠区。"""
        merged = [t for p in self.projects for t in p.todos
                  if t.archived and not t.recent]
        merged.sort(key=_todo_sort_key)
        return merged[:limit]

    def adhoc_todos(self, limit: int = 200) -> list[TodoRow]:
        """没勾巡查、最近临时查过、查出了问题的行。单独一区，同上。"""
        merged = [t for p in self.projects for t in p.adhoc_todos]
        merged.sort(key=_todo_sort_key)
        return merged[:limit]


# ---------- 「已解决」 ----------
#
# 运营在面板上点「已解决」= 「这一行的这几个问题我处理完了，先别显示」。
# 不写业务表（面板对业务表的写白名单只有「排队刷新」一列），存在注册表 base
# 的「面板设置」里（panel.Resolutions）；这里只管**什么时候再冒出来**。
#
# 难点在「下次刷新又发现问题再显示」：有几种问题刷新也不会自己消——
# 风控中 / 疑似限流是只增不减的标签（Tags.sticky），置顶掉了要等有人重新
# 置顶，需重新整理要等下一次数据整理：人不去动，刷多少次都还在。按「刷过
# 一次就重新显示」处理，这几种点了「已解决」下一轮又回来了，按钮等于没用。所以：
#
# * 冒出了**没标过的问题** → 重新显示（新问题一定要看见）；
# * 只剩标过的问题，而且**还没再刷过**（最近检查 ≤ 点的时候看到的那一次）→ 收着；
# * 又刷过了、问题还在：每一轮都会重新判的（有负面 / 已失效 / 刷新失败 /
#   疑似受限 / 卡住了）→ 重新显示——这就是「下次刷新又发现问题」；
#   只剩上面那几种刷新消不掉的 → 继续收着，它们的「还在」不是新发现。
#
# 「继续收着」只管同一次没消掉的问题。问题**消掉过**（重新置顶了、标签被人删了、
# 整理过了）之后再冒出来，是「又发生了一次」，要重新显示——这一步靠面板每次读完
# 表之后把已经消掉的问题从记录里划掉（trim_resolutions → panel.Resolutions.reconcile）：
# 记录里没有它了，它再出现就是「没标过的问题」。


def resolution_key(table_id: str, record_id: str) -> str:
    return f"{table_id}/{record_id}"


def sticky_reasons(settings: Settings) -> set[str]:
    """刷新不会自己消掉的问题。见上面那段。"""
    return {r for r in (*settings.tags.sticky(), settings.pin_status.pinned_lost,
                        settings.collation_status.stale) if r}


def _int_ms(value: Any) -> Optional[int]:
    """存起来的毫秒数。不是整数（坏了 / 手改过）就当没有。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def still_resolved(todo: TodoRow, entry: Any, sticky: set[str]) -> bool:
    """这一行按那条「已解决」记录该不该继续收着。**纯函数。**"""
    if not isinstance(entry, dict):
        return False
    marked = entry.get("reasons")
    if not isinstance(marked, list) or not set(todo.reasons) <= set(marked):
        return False                       # 有没标过的新问题
    seen = _int_ms(entry.get("seen"))
    if todo.checked_at_ms is None:
        return True                        # 从来没查过，自然也没再刷过
    if seen is not None and todo.checked_at_ms <= seen:
        return True                        # 点的时候看到的就是这一次的结果
    return set(todo.reasons) <= sticky     # 又刷过了：只剩刷新消不掉的才继续收着


def apply_resolutions(overview: Overview, resolved: dict,
                      settings: Settings) -> Overview:
    """按「已解决」记录把行收起来。返回一份新的 Overview，**不改传进来的那份**
    （它是面板缓存里的，别的请求还在读）。收起来的行放进 `resolved`。"""
    if not resolved:
        return overview
    sticky = sticky_reasons(settings)
    hidden: list[TodoRow] = []

    def keep(todo: TodoRow) -> bool:
        entry = resolved.get(resolution_key(todo.table_id, todo.record_id))
        if entry is None or not still_resolved(todo, entry, sticky):
            return True
        hidden.append(replace(todo, resolved_at_ms=_int_ms(entry.get("at"))))
        return False

    projects = [replace(p, todos=[t for t in p.todos if keep(t)],
                        adhoc_todos=[t for t in p.adhoc_todos if keep(t)])
                for p in overview.projects]
    hidden.sort(key=lambda t: -(t.resolved_at_ms or 0))
    return replace(overview, projects=projects, resolved=hidden)


def trim_resolutions(overview: Overview, resolved: dict) -> dict[str, Optional[list]]:
    """哪些「已解决」记录里的问题已经消掉了。**纯函数。**

    返回 {记录键: 还剩下的问题}，None = 整条可以删（这一行已经不在待办里，
    或者标过的问题一个都不剩）。没变的记录不在返回值里。

    只看读成功的表：读失败的表（ProjectSnapshot.error）分不出「问题消了」和
    「没读到」，一律不动。overview 必须是**没截过**的那份（panel.collect 读回来的），
    截过的话「不在列表里」可能只是被截掉了。
    """
    by_table = {p.table_id: p for p in overview.projects if p.table_id and not p.error}
    rows_of: dict[str, dict[str, TodoRow]] = {}
    edits: dict[str, Optional[list]] = {}
    for key, entry in resolved.items():
        table_id, _, record_id = key.partition("/")
        project = by_table.get(table_id)
        if project is None or not isinstance(entry, dict):
            continue
        if table_id not in rows_of:
            rows_of[table_id] = {t.record_id: t for t in (*project.todos, *project.adhoc_todos)}
        todo = rows_of[table_id].get(record_id)
        marked = entry.get("reasons") if isinstance(entry.get("reasons"), list) else []
        kept = [r for r in marked if todo is not None and r in todo.reasons]
        if kept != marked:
            edits[key] = kept or None
    return edits


# 出页面时每个项目最多列多少条待办（没勾巡查的那几条另算一份）。
MAX_TODOS = 200


def for_display(overview: Overview, resolved: dict, settings: Settings, *,
                max_todos: int = MAX_TODOS) -> Overview:
    """出页面前的最后一步：先按「已解决」收起来，**再**按条数上限截。

    顺序不能反：先截再收，被收起来的行照样占着前 max_todos 格——一次大面积
    事故里运营把前 200 行都点了「已解决」，页面上就一行不剩，却还写着「另有
    N 行没显示」，而那 N 行永远轮不上来。所以面板读表时全留着（build_snapshot
    truncate=False），截断放在这里。截掉的条数照样记下来，页面上照实说。
    """
    shown = apply_resolutions(overview, resolved, settings)
    projects = [replace(p, todos=p.todos[:max_todos],
                        todos_dropped=p.todos_dropped + max(0, len(p.todos) - max_todos),
                        adhoc_todos=p.adhoc_todos[:max_todos],
                        adhoc_dropped=p.adhoc_dropped + max(0, len(p.adhoc_todos) - max_todos))
                for p in shown.projects]
    return replace(shown, projects=projects)


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
