"""开了「最新评论」的项目：扫到的负面评论攒着，点了「已处理」才收。

## 为什么要攒

「最新」排序那一页翻得快：这一轮扫到的负面评论，下一轮可能已经被更新的评论
挤出那一页了。只按「这一轮看到的」写「负面状态」，它就会自己从「有负面」变回
「无负面」，面板上的待办跟着消失——评论还在、没人处理，只是机器这一轮没看到。
所以开了最新评论的项目（Settings.latest_comments_over），负面评论按条攒着：

* 扫到一条就记下（按平台评论 ID 认是不是同一条，记第一次扫到的时刻）；
* 之后哪一轮没再看到它，也照样留在「负面评论快照」里、「负面状态」保持有负面；
* 运营在面板上点了「已处理」，那一刻已经摆在面前的那些就算处理过了，之后
  再看到同一条也不再提醒（不然它还在最新那一页上，每一轮都会冒回来）；
  只有**新扫到的**才让这一行重新上待办。

## 存在哪儿

注册表 base 的「面板设置」里，每张表一个键 `negatives.<table_id>`
（cron 自己读写，queue 和 sweep 共用一个运行租约，不会同时写）。业务表一列都
不加，面板对业务表的写白名单也不用放宽。「已处理」读的是面板存的 `todo.resolved`
（panel.Resolutions）：点的时候看到的是哪一次检查的结果（seen），那一次以前
扫到的就是那一下处理掉的。

存进去的评论正文截短到 CONTENT_CHARS 字，留 KEEP_DAYS 天：快照里要能把「这一轮
没再看到」的那几条写出来，所以正文得留一点；整份在一格里，所以要截、要过期。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .analyze import CommentView, KeywordHit, _normalize
from .config import DigestFormat, Settings

KEY_PREFIX = "negatives."
# 记录留多久（天）：处理过的（防止同一条再冒回来）和没处理的都是。
# 默认 30 天归档、归档后不再自动刷，留 45 天够盖住。
KEEP_DAYS = 45
DAY_MS = 86_400_000
CONTENT_CHARS = 60
# 一行最多记几条：快照一格本来就只放得下 8 条左右，再多也是「还有 N 条」。
MAX_ITEMS_PER_ROW = 40
# 整份 JSON 在「面板设置」一格里（飞书上限 10 万字，每个键一格），留余量。
MAX_CHARS = 95_000


def store_key(table_id: str) -> str:
    return KEY_PREFIX + table_id


def comment_key(comment: CommentView) -> str:
    """同一条评论的身份：平台评论 ID；没有 ID 的按正文 + 昵称的摘要。"""
    if comment.comment_id:
        return "id:" + comment.comment_id
    text = _normalize(comment.content) + "\0" + (comment.author_name or "")
    return "txt:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolution_cut(resolved: Any, table_id: str, record_id: str, post_key: str,
                   found_label: str) -> Optional[int]:
    """面板上这一行点过「已处理」（而且当时挂着「有负面」）的话，那一刻看到的是
    哪一次检查的结果（毫秒）；那一次以前扫到的负面评论都算处理过了。没点过 → None。
    **纯函数。**"""
    if not isinstance(resolved, dict):
        return None
    entry = resolved.get(f"{table_id}/{record_id}")
    if not isinstance(entry, dict) or entry.get("state", "resolved") != "resolved":
        return None
    if entry.get("post_key") and entry["post_key"] != post_key:
        return None                 # 链接换过了，点的是上一篇
    reasons = entry.get("reasons")
    if not isinstance(reasons, list) or found_label not in reasons:
        return None
    for name in ("seen", "at"):
        value = entry.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


@dataclass
class Pending:
    """一条还没处理的负面评论。`hit` 有值 = 这一轮又看到了（排版用完整的那份）。"""

    key: str
    keyword: str
    content: str
    first_seen_ms: int
    from_latest: bool = False
    hit: Optional[KeywordHit] = None


def _valid_item(item: Any) -> bool:
    return (isinstance(item, dict) and isinstance(item.get("k"), str)
            and isinstance(item.get("t"), int) and not isinstance(item.get("t"), bool))


def advance(memory: Optional[dict], *, post_key: str, hits: list[KeywordHit],
            now_ms: int, cut_ms: Optional[int]) -> tuple[list[Pending], dict]:
    """一行这一轮的负面命中 + 之前攒的 → （还没处理的，要存回去的那份）。**纯函数。**

    排序：这一轮看到的按页面顺序在前，之前扫到、这一轮没再看到的按扫到的时刻
    从新到旧排在后面。
    """
    items: dict[str, dict] = {}
    if isinstance(memory, dict) and memory.get("p") == post_key:
        for item in memory.get("i") or []:
            if _valid_item(item):
                items[item["k"]] = dict(item)
    if cut_ms is not None:
        for item in items.values():
            if "d" not in item and item["t"] <= cut_ms:
                item["d"] = cut_ms
    for item in items.values():
        if "d" in item:
            # 处理过的只用来认「是不是同一条」，正文和命中词不用再存。
            item.pop("c", None)
            item.pop("w", None)
    current: list[str] = []
    seen_now: dict[str, KeywordHit] = {}
    for hit in hits:
        key = comment_key(hit.comment)
        if key in seen_now:
            continue
        seen_now[key] = hit
        current.append(key)
        if key not in items:
            items[key] = {"k": key, "t": now_ms, "w": hit.keyword,
                          "c": (hit.comment.content or "")[:CONTENT_CHARS]}
            if hit.comment.from_latest:
                items[key]["l"] = 1
    # 过期：扫到超过 KEEP_DAYS 天的不再记（处理过的、没处理的都是）。
    floor = now_ms - KEEP_DAYS * DAY_MS
    items = {k: v for k, v in items.items() if v["t"] >= floor or k in seen_now}
    if len(items) > MAX_ITEMS_PER_ROW:
        # 先丢处理过的里最早的，再丢没处理的里最早的；这一轮看到的不丢
        # （这一轮看到的比上限还多，就先都留着，下一轮再收）。
        order = sorted((v for v in items.values() if v["k"] not in seen_now),
                       key=lambda v: ("d" not in v, v["t"]))
        for victim in order[:len(items) - MAX_ITEMS_PER_ROW]:
            items.pop(victim["k"], None)
    pending: list[Pending] = []
    for key in current:
        item = items.get(key)
        if item is not None and "d" not in item:
            pending.append(_pending(item, seen_now[key]))
    rest = [v for k, v in items.items() if k not in seen_now and "d" not in v]
    rest.sort(key=lambda v: -v["t"])
    pending.extend(_pending(item, None) for item in rest)
    stored = {"p": post_key, "i": sorted(items.values(), key=lambda v: v["t"])}
    return pending, stored


def _pending(item: dict, hit: Optional[KeywordHit]) -> Pending:
    return Pending(key=item["k"], keyword=str(item.get("w") or ""),
                   content=str(item.get("c") or ""), first_seen_ms=item["t"],
                   from_latest=bool(item.get("l")), hit=hit)


def format_digest(pending: list[Pending], fmt: DigestFormat, *, minute: Callable[[datetime], str],
                  resolved_now: int = 0) -> str:
    """攒着的那几条排成「负面评论快照」。编号格式和 analyze.format_negative_digest
    一样（面板按「N. [命中「」数条数），放不下的写明还有几条。"""
    if not pending:
        if resolved_now:
            return f"（这一轮看到的 {resolved_now} 条负面评论都已处理过）"
        return "（未命中）"
    lines: list[str] = []
    used = 0
    shown = pending[: fmt.max_comments]
    for index, item in enumerate(shown, start=1):
        if item.hit is not None:
            body = item.hit.comment.one_line(fmt, extra_mark=f"命中「{item.keyword}」")
        else:
            when = minute(datetime.fromtimestamp(item.first_seen_ms / 1000, timezone.utc))
            marks = [f"命中「{item.keyword}」", f"{when} 扫到", "这一轮没再看到"]
            if item.from_latest:
                marks.insert(1, "最新")
            text = " ".join((item.content or "").split())
            if len(text) > fmt.per_comment_chars:
                text = text[: fmt.per_comment_chars - 1] + "…"
            body = f"[{' · '.join(marks)}] {text}"
        line = f"{index}. {body}"
        if used + len(line) + 1 > fmt.total_chars:
            lines.append(f"…（还有 {len(pending) - index + 1} 条未显示）")
            return "\n".join(lines)
        lines.append(line)
        used += len(line) + 1
    if len(pending) > len(shown):
        lines.append(f"…（还有 {len(pending) - len(shown)} 条未显示）")
    return "\n".join(lines)


class TableLedger:
    """一张表这一轮的负面记忆。runner 逐行调 apply（多线程，有锁）。"""

    def __init__(self, table_id: str, rows: Any, resolved: Any, settings: Settings):
        self.table_id = table_id
        self.rows: dict[str, dict] = dict(rows) if isinstance(rows, dict) else {}
        self.resolved = resolved if isinstance(resolved, dict) else {}
        self.found_label = settings.negative_status.found
        self.changed = False
        self.dropped_rows = 0
        self._lock = threading.Lock()

    def apply(self, record_id: str, post_key: str, hits: list[KeywordHit],
              now_ms: int) -> tuple[list[Pending], int]:
        """返回（还没处理的, 这一轮看到却已经处理过的条数）。"""
        cut = resolution_cut(self.resolved, self.table_id, record_id, post_key, self.found_label)
        with self._lock:
            pending, stored = advance(self.rows.get(record_id), post_key=post_key,
                                      hits=hits, now_ms=now_ms, cut_ms=cut)
            if stored["i"]:
                self.rows[record_id] = stored
            else:
                self.rows.pop(record_id, None)
            self.changed = True
        keys = {p.key for p in pending}
        handled = len({comment_key(h.comment) for h in hits} - keys)
        return pending, handled

    def payload(self, now_ms: int) -> dict:
        """要存回去的那份：过期的整行扔掉，太长就先扔最久没动的行。"""
        import json

        floor = now_ms - KEEP_DAYS * DAY_MS
        rows = {rid: row for rid, row in self.rows.items()
                if isinstance(row, dict) and any(
                    _valid_item(i) and i["t"] >= floor for i in row.get("i") or [])}
        def newest(row: dict) -> int:
            return max((i["t"] for i in row.get("i") or [] if _valid_item(i)), default=0)
        ordered = sorted(rows, key=lambda rid: newest(rows[rid]))
        out = {"v": 1, "rows": rows}
        self.dropped_rows = 0
        while ordered and len(json.dumps(out, ensure_ascii=False)) > MAX_CHARS:
            rows.pop(ordered.pop(0), None)
            self.dropped_rows += 1
        return out


class Ledger:
    """cron 一轮里所有开了最新评论的表的负面记忆，读写「面板设置」。

    读失败返回 None（这张表这一轮按老办法只看这一轮的命中，并大声说一句）；
    写失败只警告——下一轮按存着的那份重算，这一轮新扫到的、下一轮还在页面上的
    会再记一次，被挤出去的那几条就只留在这一轮写的快照里。
    """

    def __init__(self, store, settings: Settings, *, log: Callable[[str], None] = print):
        self.store = store
        self.settings = settings
        self.log = log
        self._resolved: Any = None
        self.tables: dict[str, TableLedger] = {}

    def for_table(self, table_id: str, settings: Settings) -> Optional[TableLedger]:
        try:
            if self._resolved is None:
                from .panel_settings import KEY_RESOLVED
                self._resolved = self.store.get_json(KEY_RESOLVED, {})
            stored = self.store.get_json(store_key(table_id), {})
        except Exception as exc:  # noqa: BLE001
            self.log(f"⚠ 读不到攒着的负面评论（{type(exc).__name__}: {exc}）：这张表这一轮"
                     "「负面评论快照」只写这一轮看到的，之前扫到、这一轮没再看到的下一轮补回来")
            return None
        rows = stored.get("rows") if isinstance(stored, dict) else None
        ledger = TableLedger(table_id, rows, self._resolved, settings)
        self.tables[table_id] = ledger
        return ledger

    def save(self, now_ms: int) -> bool:
        ok = True
        for table_id, ledger in self.tables.items():
            if not ledger.changed:
                continue
            payload = ledger.payload(now_ms)
            if ledger.dropped_rows:
                self.log(f"⚠ 攒着的负面评论太多、一格放不下，最久没动的 {ledger.dropped_rows} 行"
                         "的记录没存：那几行之前处理过的负面评论要是还在页面上，会再提醒一次")
            try:
                self.store.update_json(store_key(table_id), lambda _current: payload, {})
            except Exception as exc:  # noqa: BLE001
                ok = False
                self.log(f"⚠ 攒着的负面评论没存上（{type(exc).__name__}: {exc}）：下一轮按上一次"
                         "存的那份算，这一轮新扫到、下一轮已经被挤出最新那一页的会漏记")
        return ok
