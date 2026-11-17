#!/usr/bin/env python3
"""导出评论区处置的后台底稿：每条负面/竞品评论什么时候被发现、什么时候标了已处理、
按竞品各有几条。

    python3 tools/export_comment_handling.py --table 项目A
    python3 tools/export_comment_handling.py --table 项目A --since 2026-10-28 --until 2026-11-17
    python3 tools/export_comment_handling.py --table 项目A --competitors 邦乐健,荣康,东方神
    python3 tools/export_comment_handling.py --table 项目A --out ~/Desktop/处置底稿

**只读**：读注册表 base 里的「巡检留痕」「面板设置」和业务表，一个字都不写，
不发付费请求。不带 --table 就导出全部在用的表。--since / --until 是北京时间的
日期（两头都含），按「首次扫到」那一刻筛；不给就是留痕里有的全部。

## 数从哪来

* **发现时间**：「巡检留痕」里这条评论（按平台评论 ID 认）第一次被扫到的那一轮。
  留痕 2026-10-28 才上线，之前的没有；扫描记录只留 100 天。
* **处置时间**：发现之后，运营第一次在面板上把这篇帖子标成「已处理」的时刻
  （点了又撤销、或者又改回「正在处理」的不算，往后找下一次）。这是**帖子级**的、
  **点按钮**的时刻，不是素人删评那一刻；面板自己补的状态不算。开了「最新评论」或
  「展开楼中楼」的表另有逐条记录（「面板设置」里的 negatives.<表>，留 45 天），
  留痕里找不到状态时拿它补，「处置时间来源」一列写明是哪一种。
* **竞品**：拿评论正文去比 --competitors 给的名单；没给就用登记表里这张表的
  「蓝词排除词」（竞品名）。只有命中过「负面词」的评论才进留痕：竞品名没填进
  「负面词」的，机器没在找，这里也数不到。
* **此后首次没扫到**：最后一次扫到它之后，同一篇帖子下一次查负面时它不在了。
  可能是删了，也可能只是被挤出了第一页，只能当旁证，不能当删除时间。
  攒负面的表在标了已处理之后不再记这条评论，那之后的「没扫到」看不出来，留空。
* **已处理后仍扫到**：不攒负面的表每一轮都记，标了已处理之后还扫到 = 那一刻
  评论多半还在。攒负面的表看不出来，写「—」。

机器只看第一页（项目开了的话再加最新 10 条和楼中楼），条数都是**下限**。

## 输出（UTF-8 带 BOM 的 CSV，Excel 直接打开）

* 评论明细.csv     一条评论一行
* 帖子汇总.csv     一篇帖子一行
* 按竞品汇总.csv   有竞品名单的表才有
* 按命中词汇总.csv 按「负面词」里命中的那个词

默认写到当前目录下的 exports/评论处置-<时刻>/（exports/ 已在 .gitignore 里，
评论正文是别人写的东西，别提交进仓库）。
"""

from __future__ import annotations

import csv
import hashlib
import os
import pathlib
import sys
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载、表清单、逐表设置
from xhsearch import (analyze, daily_report, feishu, negatives,  # noqa: E402
                      panel_settings, summary)
from xhsearch.config import Settings  # noqa: E402
from xhsearch.rows import Row  # noqa: E402

SHANGHAI = daily_report.SHANGHAI
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
SLA_HOURS = 24
TITLE_CHARS = 40
NO_COMPETITOR = "（没提到名单里的竞品）"
SOURCE_PANEL = "面板「已处理」（帖子级）"
SOURCE_LEDGER = "攒负面记录（逐条）"


# ---------- 纯函数 ----------

def _ms(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value or value == float("inf") or value != int(value):
        return None
    return int(value)


def fmt_time(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M")


def fmt_hours(start: Optional[int], end: Optional[int]) -> str:
    if start is None or end is None:
        return ""
    return f"{(end - start) / HOUR_MS:.1f}"


def day_start_ms(day: date) -> int:
    return int(datetime.combine(day, dt_time(0), SHANGHAI).timestamp() * 1000)


def comment_identity(hit: dict) -> str:
    """留痕里一条命中是哪条评论：平台评论 ID；没有 ID 的按正文摘要。
    和 negatives.comment_key 的 ID 那一支同一个写法，好和攒负面记录对上。"""
    cid = str(hit.get("id") or "")
    if cid:
        return "id:" + cid
    text = analyze._normalize(str(hit.get("content") or ""))
    return "txt:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _alias_map(events: list[dict]) -> dict[tuple[str, str], str]:
    """短链展开前按链接文字算的帖子键 → 展开后的键（和 daily_report.trend 同一套）。"""
    out = {}
    for e in events:
        if e.get("type") == "scan" and e.get("post_alias") and e.get("post_key"):
            out[(str(e.get("table_id") or ""), str(e["post_alias"]))] = str(e["post_key"])
    return out


def _post_of(event: dict, aliases: dict) -> str:
    post = str(event.get("post_key") or f"row:{event.get('record_id', '')}")
    return aliases.get((str(event.get("table_id") or ""), post), post)


def handling_times(statuses: list[tuple[int, str]], since_ms: int
                   ) -> tuple[Optional[int], Optional[int]]:
    """一篇帖子的运营状态（按时刻排好的 [(毫秒, 状态)]，面板自己补的已剔掉）→
    （发现之后第一次标「正在处理」, 发现之后算数的那一次「已处理」）。**纯函数。**

    「已处理」之后又改回待处理 / 正在处理，等于撤销：那一下不算，往后找下一次。"""
    processing = handled = None
    for at, state in statuses:
        if at < since_ms:
            continue
        if state == "processing" and processing is None:
            processing = at
        if state == "resolved":
            if handled is None:
                handled = at
        elif handled is not None:
            handled = None
    return processing, handled


def sla_label(first_seen: int, handled: Optional[int], now_ms: int,
              hours: int = SLA_HOURS) -> str:
    if handled is not None:
        return "是" if handled - first_seen <= hours * HOUR_MS else "否"
    waited = (now_ms - first_seen) / HOUR_MS
    if waited < hours:
        return f"未标已处理（未满 {hours} 小时）"
    return f"未标已处理（已过 {waited:.0f} 小时）"


def competitors_in(text: str, keywords: list[str], names) -> list[str]:
    """评论正文（或命中的那个词）里提到了名单里的哪几家。**纯函数。**

    中文按去掉空白标点后的子串（和「负面词」同一种宽松比对：「邦 乐健」也算），
    英文按整词（analyze.excluded_by，和「蓝词排除词」同一套：sis 不算 analysis）。"""
    found: list[str] = []
    haystacks = [text or "", *keywords]
    normalized = [analyze._normalize(h) for h in haystacks]
    for raw in names or ():
        name = raw.strip()
        if len(name) < 2 or name in found:
            continue
        if name.isascii():
            hit = any(analyze.excluded_by(h, [name]) for h in haystacks)
        else:
            needle = analyze._normalize(name)
            hit = bool(needle) and any(needle in h for h in normalized)
        if hit:
            found.append(name)
    return found


def collect_comments(events: list[dict], *, ledger_rows: Optional[dict] = None,
                     accumulating: bool = False) -> list[dict]:
    """一张表的留痕事件（扫描 + 处理状态）和攒负面记录 → 一条评论一份记录。**纯函数。**

    同一行换过链接（帖子键变了）的，按帖子分开算，状态也只认同一篇的。"""
    unique = {daily_report.event_key(e): e for e in events
              if isinstance(e, dict) and _ms(e.get("at")) is not None}
    events = list(unique.values())
    aliases = _alias_map(events)
    statuses: dict[str, list[tuple[int, Optional[str], str]]] = defaultdict(list)
    for e in events:
        if e.get("type") == "status" and not e.get("auto"):
            post = _post_of(e, aliases) if e.get("post_key") else None
            statuses[str(e.get("record_id") or "")].append(
                (e["at"], post, str(e.get("state") or "")))
    comments: dict[tuple[str, str, str], dict] = {}
    checks: dict[tuple[str, str], list[int]] = defaultdict(list)
    for scan in sorted((e for e in events if e.get("type") == "scan"), key=lambda e: e["at"]):
        record_id = str(scan.get("record_id") or "")
        post = _post_of(scan, aliases)
        if scan.get("negative_checked", True):
            checks[(record_id, post)].append(scan["at"])
        for hit in scan.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            key = comment_identity(hit)
            item = comments.get((record_id, post, key))
            if item is None:
                item = comments[(record_id, post, key)] = _blank(
                    record_id, post, key, str(hit.get("id") or ""), scan["at"])
            if scan["at"] not in item["_seen"]:
                item["_seen"].add(scan["at"])
            item["first_seen_ms"] = min(item["first_seen_ms"], scan["at"])
            item["last_seen_ms"] = max(item["last_seen_ms"] or 0, scan["at"])
            content = str(hit.get("content") or "")
            if len(content) > len(item["content"]):
                item["content"] = content
            for word in hit.get("keywords") or []:
                if isinstance(word, str) and word and word not in item["keywords"]:
                    item["keywords"].append(word)
            created = _ms(hit.get("created_at_ms"))
            if created is not None:
                item["created_at_ms"] = created
    _merge_ledger(comments, ledger_rows, aliases)

    out = []
    for (record_id, post, _key), item in comments.items():
        mine = sorted((at, state) for at, p, state in statuses.get(record_id, [])
                      if p is None or p == post)
        processing, handled = handling_times(mine, item["first_seen_ms"])
        source = SOURCE_PANEL if handled is not None else ""
        if handled is None and item["ledger_handled_ms"] is not None:
            handled, source = item["ledger_handled_ms"], SOURCE_LEDGER
        gone = gone_note = None
        last = item["last_seen_ms"]
        if last is not None:
            later = [at for at in checks.get((record_id, post), []) if at > last]
            if later:
                gone = later[0]
                if accumulating and handled is not None and gone > handled:
                    gone, gone_note = None, "已处理后不再留痕，看不出"
        if accumulating or handled is None or last is None:
            after = "—" if accumulating and handled is not None else ""
        else:
            after = "是" if last > handled else "否"
        item.update(processing_ms=processing, handled_ms=handled, handled_source=source,
                    gone_ms=gone, gone_note=gone_note or "", seen_after_handled=after,
                    sightings=len(item.pop("_seen")))
        out.append(item)
    out.sort(key=lambda c: (c["first_seen_ms"], c["record_id"], c["key"]))
    return out


def _blank(record_id: str, post: str, key: str, comment_id: str, at: int) -> dict:
    return {"record_id": record_id, "post": post, "key": key, "comment_id": comment_id,
            "content": "", "keywords": [], "created_at_ms": None,
            "first_seen_ms": at, "last_seen_ms": None, "_seen": set(),
            "ledger_handled_ms": None, "from_ledger_only": False}


def _merge_ledger(comments: dict, ledger_rows: Optional[dict], aliases: dict) -> None:
    """攒负面记录（negatives.<表>）里的逐条记录并进来：首次扫到取更早的那个，
    逐条的「已处理」时刻记下备用；留痕里没有的（留痕写失败那几轮）补进来。"""
    if not isinstance(ledger_rows, dict):
        return
    for record_id, row in ledger_rows.items():
        if not isinstance(row, dict):
            continue
        post_key = str(row.get("p") or f"row:{record_id}")
        for table_alias, target in aliases.items():
            if table_alias[1] == post_key:
                post_key = target
        for entry in row.get("i") or []:
            if not (isinstance(entry, dict) and isinstance(entry.get("k"), str)
                    and _ms(entry.get("t")) is not None):
                continue
            key = entry["k"]
            item = comments.get((str(record_id), post_key, key))
            if item is None and not key.startswith("id:"):
                # 没有平台 ID 的那几条：攒负面记录按「正文 + 昵称」认，留痕里没昵称，
                # 只能按正文开头对。
                head = str(entry.get("c") or "")
                item = next((c for (rid, p, _k), c in comments.items()
                             if rid == str(record_id) and p == post_key and head
                             and c["content"].startswith(head)), None)
            if item is None:
                item = comments[(str(record_id), post_key, key)] = _blank(
                    str(record_id), post_key, key,
                    key[3:] if key.startswith("id:") else "", entry["t"])
                item["content"] = str(entry.get("c") or "")
                item["from_ledger_only"] = True
            item["first_seen_ms"] = min(item["first_seen_ms"], entry["t"])
            word = entry.get("w")
            if isinstance(word, str) and word and word not in item["keywords"]:
                item["keywords"].append(word)
            if _ms(entry.get("d")) is not None:
                item["ledger_handled_ms"] = entry["d"]


def within(comments: list[dict], since_ms: Optional[int], until_ms: Optional[int]) -> list[dict]:
    return [c for c in comments
            if (since_ms is None or c["first_seen_ms"] >= since_ms)
            and (until_ms is None or c["first_seen_ms"] < until_ms)]


# ---------- 汇总 ----------

def _tally(rows: list[dict]) -> dict:
    posts = {(c["project"], c["record_id"], c["post"]) for c in rows}
    on_time = sum(1 for c in rows if c["sla"] == "是")
    late = sum(1 for c in rows if c["sla"] == "否")
    return {"评论条数": len(rows), "涉及帖子数": len(posts),
            "24小时内标已处理": on_time, "超过24小时才标已处理": late,
            "还没标已处理": len(rows) - on_time - late}


def by_competitor(comments: list[dict]) -> list[dict]:
    """按（项目, 竞品）数。一条评论提到两家的两边各算一次；没提到名单里的单列一行。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    order: dict[str, list[str]] = {}
    for c in comments:
        names = c["competitors"] or [NO_COMPETITOR]
        order.setdefault(c["project"], list(c["competitor_list"]))
        for name in names:
            groups[(c["project"], name)].append(c)
    out = []
    for project, names in order.items():
        for name in [*names, NO_COMPETITOR]:
            rows = groups.get((project, name))
            if rows:
                out.append({"项目": project, "竞品": name, **_tally(rows)})
    return out


def by_keyword(comments: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in comments:
        for word in c["keywords"] or ["（没记命中词）"]:
            groups[(c["project"], word)].append(c)
    rows = [{"项目": project, "命中词": word, **_tally(items)}
            for (project, word), items in groups.items()]
    rows.sort(key=lambda r: (r["项目"], -r["评论条数"], r["命中词"]))
    return rows


def by_post(comments: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for c in comments:
        groups[(c["project"], c["record_id"], c["post"])].append(c)
    out = []
    for (project, record_id, _post), items in groups.items():
        first = items[0]
        handled = [c["handled_ms"] for c in items if c["handled_ms"] is not None]
        waits = [c["handled_ms"] - c["first_seen_ms"] for c in items
                 if c["handled_ms"] is not None]
        if any(c["handled_ms"] is None for c in items):
            verdict = "有还没标已处理的"
        elif all(c["sla"] == "是" for c in items):
            verdict = "是"
        else:
            verdict = "否"
        names = sorted({n for c in items for n in c["competitors"]})
        out.append({
            "项目": project, "素人编号": first["persona"], "帖子标题": first["title"],
            "帖子链接": first["link"], "飞书行ID": record_id,
            "负面/竞品评论条数": len(items),
            "其中提到竞品的条数": sum(1 for c in items if c["competitors"]),
            "提到的竞品": "、".join(names),
            "最早发现": fmt_time(min(c["first_seen_ms"] for c in items)),
            "最晚发现": fmt_time(max(c["first_seen_ms"] for c in items)),
            "首次标已处理": fmt_time(min(handled) if handled else None),
            "最长 发现→标已处理（小时）": f"{max(waits) / HOUR_MS:.1f}" if waits else "",
            "全部24小时内标已处理": verdict,
        })
    out.sort(key=lambda r: (r["项目"], r["最早发现"]))
    return out


def detail_rows(comments: list[dict]) -> list[dict]:
    return [{
        "项目": c["project"], "素人编号": c["persona"], "帖子标题": c["title"],
        "帖子链接": c["link"], "飞书行ID": c["record_id"], "评论ID": c["comment_id"],
        "评论正文": c["content"], "命中词": "、".join(c["keywords"]),
        "提到的竞品": "、".join(c["competitors"]),
        "评论发布时间": fmt_time(c["created_at_ms"]),
        "首次扫到（发现时间）": fmt_time(c["first_seen_ms"]),
        "发布→发现（小时）": fmt_hours(c["created_at_ms"], c["first_seen_ms"]),
        "最后一次扫到": fmt_time(c["last_seen_ms"]),
        "扫到次数": c["sightings"],
        "首次标正在处理": fmt_time(c["processing_ms"]),
        "标已处理（处置时间）": fmt_time(c["handled_ms"]),
        "处置时间来源": c["handled_source"],
        "发现→标已处理（小时）": fmt_hours(c["first_seen_ms"], c["handled_ms"]),
        "24小时内标已处理": c["sla"],
        "此后首次没扫到": fmt_time(c["gone_ms"]) or c["gone_note"],
        "已处理后仍扫到": c["seen_after_handled"],
        "备注": "只在攒负面记录里（留痕那几轮没写进去），正文截到 60 字"
                if c["from_ledger_only"] else "",
    } for c in comments]


def write_csv(path: pathlib.Path, rows: list[dict], header: Optional[list[str]] = None) -> None:
    header = header or (list(rows[0]) if rows else [])
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


# ---------- 读数 ----------

PERSONA = summary.PERSONA_COLUMN


def read_posts(table, settings: Settings) -> dict[str, dict]:
    """业务表 → {record_id: {persona, title, link}}。只请求表里真有的列
    （按名字请求不存在的列，飞书整个 search 报错、一行都读不回来）。"""
    meta = table.fields_meta()
    if meta is None:
        raise RuntimeError("读不到字段列表（应用没被加进这张表？先跑 cli.py doctor）")
    link = settings.fields.link
    label = summary.pick_label_column(set(meta))
    wanted = [c for c in dict.fromkeys((link, PERSONA, label)) if c and c in meta]
    out = {}
    for record in table.search(wanted):
        cells = record.get("fields") or {}
        record_id = str(record.get("record_id") or "")
        row = Row(record_id=record_id, link_cell=feishu.read_text(cells.get(link)))
        title = " ".join(summary.row_label(row, cells, settings, label).split())
        if len(title) > TITLE_CHARS:
            title = title[: TITLE_CHARS - 1] + "…"
        out[record_id] = {"persona": feishu.read_text(cells.get(PERSONA)).strip(),
                          "title": title,
                          "link": row.parsed.url or row.link_cell.strip()}
    return out


def export_table(label: str, table, settings: Settings, journal, store, *,
                 now_ms: int, since_ms: Optional[int], until_ms: Optional[int],
                 competitors: Optional[list[str]], out: Callable[[str], None] = print
                 ) -> list[dict]:
    """一张表 → 这张表的评论记录（已按时间段筛过、补好帖子信息）。"""
    table_id = table.table_id
    start = now_ms - (daily_report.RETENTION_DAYS + 1) * DAY_MS
    events = journal.read_window(start, now_ms + 1, table_id)
    try:
        ledger = store.get_json(negatives.store_key(table_id), {})
    except Exception as exc:  # noqa: BLE001 —— 逐条记录只是补充，读不到照样导
        out(f"  ⚠ 读不到攒负面记录（{type(exc).__name__}: {exc}），只用留痕")
        ledger = {}
    ledger_rows = ledger.get("rows") if isinstance(ledger, dict) else None
    accumulating = negatives.accumulates(settings) or bool(ledger_rows)
    comments = within(collect_comments(events, ledger_rows=ledger_rows,
                                       accumulating=accumulating), since_ms, until_ms)

    started = [e["at"] for e in events
               if e.get("type") == "coverage" and _ms(e.get("at")) is not None]
    gaps = [e for e in events if e.get("type") == "gap"]
    if started:
        out(f"  留痕从 {fmt_time(min(started))} 开始")
        if since_ms is not None and since_ms < min(started):
            out("  ⚠ 选的起点早于留痕启用，那之前的发现时间没有记录")
    else:
        out("  ⚠ 这张表在「巡检留痕」里没有启用记录（还没巡检过，或留痕没接上）")
    if gaps:
        out(f"  ⚠ 留痕有 {len(gaps)} 处缺口（分片缺失或面板没写进去的状态），数可能偏少")

    try:
        posts = read_posts(table, settings)
    except Exception as exc:  # noqa: BLE001 —— 业务表读不到，留痕照样能导
        out(f"  ⚠ 业务表读不到（{exc}）：素人编号、标题、链接留空")
        posts = {}
    names = list(competitors) if competitors is not None else list(settings.blue_word_excludes)
    for c in comments:
        info = posts.get(c["record_id"], {})
        c.update(project=label, persona=info.get("persona", ""),
                 title=info.get("title", ""), link=info.get("link", ""),
                 competitor_list=names,
                 competitors=competitors_in(c["content"], c["keywords"], names),
                 sla=sla_label(c["first_seen_ms"], c["handled_ms"], now_ms))
    return comments


def run(tables, settings: Settings, journal, store, out_dir: pathlib.Path, *,
        now_ms: int, since_ms: Optional[int] = None, until_ms: Optional[int] = None,
        competitors: Optional[list[str]] = None, out: Callable[[str], None] = print) -> int:
    failed = 0
    everything: list[dict] = []
    for label, table in tables:
        out(f"表「{label}」")
        per_table = cli.settings_for_table(settings, table)
        try:
            comments = export_table(label, table, per_table, journal, store, now_ms=now_ms,
                                    since_ms=since_ms, until_ms=until_ms,
                                    competitors=competitors, out=out)
        except Exception as exc:  # noqa: BLE001 —— 一张表读不到不该挡住别的表
            out(f"  ✗ 留痕读不到：{type(exc).__name__}: {exc}")
            failed += 1
            continue
        everything.extend(comments)
        t = _tally(comments) if comments else None
        if t is None:
            out("  这段时间没有扫到负面/竞品评论")
            continue
        out(f"  {t['评论条数']} 条评论、{t['涉及帖子数']} 篇帖子；24 小时内标已处理 "
            f"{t['24小时内标已处理']} 条，超时 {t['超过24小时才标已处理']} 条，"
            f"还没标 {t['还没标已处理']} 条")
        names = comments[0]["competitor_list"]
        if not names:
            out("  没给 --competitors，登记表里这张表也没填「蓝词排除词」：不分竞品，"
                "只出按命中词汇总")
        else:
            counts = {r["竞品"]: r["评论条数"] for r in by_competitor(comments)}
            out("  按竞品：" + "，".join(f"{n} {counts.get(n, 0)} 条" for n in names)
                + f"，{NO_COMPETITOR} {counts.get(NO_COMPETITOR, 0)} 条")

    out_dir.mkdir(parents=True, exist_ok=True)
    details = detail_rows(everything)
    header = list(detail_rows([_header_probe()])[0])
    write_csv(out_dir / "评论明细.csv", details, header)
    written = ["评论明细.csv"]
    for name, rows in (("帖子汇总.csv", by_post(everything)),
                       ("按竞品汇总.csv",
                        by_competitor([c for c in everything if c["competitor_list"]])),
                       ("按命中词汇总.csv", by_keyword(everything))):
        if rows:
            write_csv(out_dir / name, rows)
            written.append(name)
    out("")
    out(f"导出到 {out_dir}：{len(details)} 条评论（{'、'.join(written)}）")
    out("口径：发现 = 机器首次扫到；处置 = 运营在面板标「已处理」（帖子级，不是删评那一刻）；"
        "只看第一页，条数是下限")
    return 1 if failed else 0


def _header_probe() -> dict:
    """空表也要写出列名：拿一条假记录过一遍 detail_rows。"""
    return {**_blank("", "", "", "", 0), "sightings": 0, "processing_ms": None,
            "handled_ms": None, "handled_source": "", "gone_ms": None, "gone_note": "",
            "seen_after_handled": "", "project": "", "persona": "", "title": "",
            "link": "", "competitors": [], "competitor_list": [], "sla": ""}


# ---------- 命令行 ----------

def _pop_value(args: list[str], flag: str) -> Optional[str]:
    if flag not in args:
        return None
    index = args.index(flag)
    if index + 1 >= len(args):
        raise ValueError(f"{flag} 后面要跟值")
    value = args[index + 1]
    del args[index:index + 2]
    return value


def parse_window(since: Optional[str], until: Optional[str]
                 ) -> tuple[Optional[int], Optional[int]]:
    """北京时间日期 → [起点当天 0 点, 终点次日 0 点)，两头都含。"""
    def day(flag: str, text: str) -> date:
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{flag} 要写成 2026-11-01 这样的日期，收到的是 {text!r}") from None

    since_ms = day_start_ms(day("--since", since)) if since else None
    until_ms = day_start_ms(day("--until", until) + timedelta(days=1)) if until else None
    if since_ms is not None and until_ms is not None and until_ms <= since_ms:
        raise ValueError("--until 不能早于 --since")
    return since_ms, until_ms


def main(argv: list[str]) -> int:
    cli.load_env_or_exit()
    args = list(argv[1:])
    selected = cli.pop_table_flag(args)
    try:
        since = _pop_value(args, "--since")
        until = _pop_value(args, "--until")
        names = _pop_value(args, "--competitors")
        out_dir = _pop_value(args, "--out")
        since_ms, until_ms = parse_window(since, until)
    except ValueError as exc:
        print(f"{exc}\n\n{__doc__}")
        return 2
    if args:
        print(__doc__)
        return 2
    competitors = None
    if names is not None:
        competitors = [n.strip() for n in names.replace("，", ",").split(",")
                       if len(n.strip()) >= 2]
        if not competitors:
            print("--competitors 里没有能用的名字（逗号分隔，每个至少 2 个字）")
            return 2
    if not os.environ.get("FEISHU_REGISTRY", "").strip():
        print("没配 FEISHU_REGISTRY：留痕和攒负面记录都在注册表 base 里，读不到")
        return 2
    settings = cli.build_settings()
    tables = cli._tables(selected)
    app_id, app_secret = cli._env("FEISHU_APP_ID"), cli._env("FEISHU_APP_SECRET")
    registry_table = cli._registry_table(app_id, app_secret)
    workspace = feishu.Workspace(app_id=app_id, app_secret=app_secret)
    journal = daily_report.Journal(workspace, workspace.table, registry_table.app_token,
                                   allow_create=False)
    store = panel_settings.SettingsStore(workspace, workspace.table, registry_table.app_token)
    now = datetime.now(SHANGHAI)
    target = (pathlib.Path(out_dir).expanduser() if out_dir else
              pathlib.Path("exports") / f"评论处置-{now.strftime('%Y%m%d-%H%M')}")
    print("只读，不改表、不花钱\n")
    return run(tables, settings, journal, store, target, now_ms=int(now.timestamp() * 1000),
               since_ms=since_ms, until_ms=until_ms, competitors=competitors)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
