"""巡检留痕与 16 点工作日报。

留痕单独存在注册表 base 的「巡检留痕」表，不使用容器磁盘，也不把历史塞进
面板设置的一格。扫描只记真实拿到评论页的成功结果；评论条数是本期检出并
去重的预警评论，既不是全站普查，也不是按评论发布时间计算的新增量。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time as dt_time, timedelta, timezone
import hashlib
import json
import threading
import time
from typing import Any, Callable, Optional

from . import feishu

TABLE_NAME = "巡检留痕"
COL_KEY = "留痕键"
COL_PROJECT = "项目表"
COL_KIND = "事件类型"
COL_START = "开始毫秒"
COL_END = "结束毫秒"
COL_PART = "分片序号"
COL_PARTS = "分片总数"
COL_DATA = "留痕内容"
FIELDS = [{"field_name": n, "type": 1} for n in
          (COL_KEY, COL_PROJECT, COL_KIND, COL_DATA)] + [
    {"field_name": n, "type": 2, "property": {"formatter": "0"}}
    for n in (COL_START, COL_END, COL_PART, COL_PARTS)]
FIELD_NAMES = [f["field_name"] for f in FIELDS]
CHUNK_CHARS = 12_000
MAX_BATCH_EVENTS = 100
KINDS = frozenset({"scan", "status", "coverage", "run_start", "run_end", "gap"})
STATES = ("pending", "processing", "resolved")
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ms(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # 飞书数字列可能返回 1.0；NaN、无穷及小数均不是有效的毫秒/分片序号。
    if value < 0 or value != value or value == float("inf") or value != int(value):
        return None
    return int(value)


def event_key(event: dict) -> str:
    """相同事实的稳定身份；分片重试、重复返回都不应重复计数。"""
    return hashlib.sha256(_json(event).encode("utf-8")).hexdigest()


def window_for_date(day: date | str) -> tuple[int, int]:
    """day 是日报结束日；北京时间前一天 16:00 至当天 16:00。"""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    end = datetime.combine(day, dt_time(16), SHANGHAI)
    return int((end - timedelta(days=1)).timestamp() * 1000), int(end.timestamp() * 1000)


class Journal:
    """专用飞书追加表。读操作从不建表；缺片/坏 JSON 显式返回 gap 元数据。

    一批同项目同类型事件用 JSON 装箱，再分成有序文本片。内容长也不截断；
    只有所有分片齐全才采纳该批次。每批 client_token 与事件内容绑定。
    """

    def __init__(self, workspace, bitable_factory: Callable, app_token: str, *,
                 log: Callable[[str], None] = print,
                 wall: Callable[[], float] = time.time, allow_create: bool = True):
        self.workspace = workspace
        self._bitable = bitable_factory
        self.app_token = app_token
        self.log, self._wall = log, wall
        self.allow_create = allow_create
        self._table_id = ""
        self._lock = threading.RLock()

    def _find(self) -> str:
        if not self._table_id:
            found = [t["table_id"] for t in self.workspace.list_tables(self.app_token)
                     if t.get("name") == TABLE_NAME and t.get("table_id")]
            if len(found) > 1:
                raise ValueError("发现多张「巡检留痕」表，请合并后保留唯一一张，日报暂不统计")
            if found:
                self._table_id = found[0]
        return self._table_id

    def _ensure(self) -> str:
        found = self._find()
        if found:
            return found
        if not self.allow_create:
            raise ValueError("巡检留痕尚未初始化，请等待首次巡检建立留痕表")
        found = self.workspace.create_table(self.app_token, TABLE_NAME, FIELDS)
        if not found:
            raise ValueError("创建「巡检留痕」失败：飞书未返回 table_id")
        self._table_id = found
        self.log("🧾 已建立「巡检留痕」表，历史数据从启用时开始积累")
        return found

    def append(self, events: list[dict]) -> None:
        if not events:
            return
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for event in events:
            if (not isinstance(event, dict) or event.get("type") not in KINDS
                    or _ms(event.get("at")) is None or not event.get("table_id")):
                raise ValueError("留痕事件缺少 type、at 或 table_id")
            if event["type"] == "status" and event.get("state") not in STATES:
                raise ValueError("状态留痕必须是 pending / processing / resolved")
            groups[(str(event["table_id"]), event["type"])].append(dict(event))
        with self._lock:
            table = self._bitable(self.app_token, self._ensure())
            for (project, kind), grouped in sorted(groups.items()):
                # 有界装箱让重试和单格长度都稳定；同一批事件去重后按事实键排序。
                unique = {event_key(e): e for e in grouped}
                ordered = [unique[k] for k in sorted(unique)]
                for start in range(0, len(ordered), MAX_BATCH_EVENTS):
                    batch = ordered[start:start + MAX_BATCH_EVENTS]
                    payload = _json(batch)
                    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    parts = [payload[i:i + CHUNK_CHARS]
                             for i in range(0, len(payload), CHUNK_CHARS)]
                    first, last = min(e["at"] for e in batch), max(e["at"] for e in batch)
                    records = [{"fields": {
                        COL_KEY: key, COL_PROJECT: project, COL_KIND: kind,
                        COL_START: first, COL_END: last, COL_PART: i,
                        COL_PARTS: len(parts), COL_DATA: part,
                    }} for i, part in enumerate(parts)]
                    created = table.batch_create(records, client_token=feishu.idempotency_key(
                        f"journal:{self.app_token}:{key}"))
                    if len(created) != len(records) or not all(created):
                        raise ValueError("巡检留痕未完整写入，日报不能视为完整")

    def _read_kind(self, kind: str, start_ms: Optional[int], end_ms: int,
                   table_id: str = "") -> list[dict]:
        found = self._find()
        if not found:
            return []
        conditions = [
            {"field_name": COL_KIND, "operator": "is", "value": [kind]},
            {"field_name": COL_START, "operator": "isLess", "value": [str(end_ms)]},
        ]
        if start_ms is not None:
            conditions.append({"field_name": COL_END, "operator": "isGreaterEqual",
                               "value": [str(start_ms)]})
        if table_id:
            conditions.append({"field_name": COL_PROJECT, "operator": "is", "value": [table_id]})
        rows = self._bitable(self.app_token, found).search(
            FIELD_NAMES, filter_spec={"conjunction": "and", "conditions": conditions})
        chunks: dict[str, dict] = {}
        for record in rows:
            cells = record.get("fields") or {}
            key = feishu.read_text(cells.get(COL_KEY))
            project = feishu.read_text(cells.get(COL_PROJECT))
            first = _ms(cells.get(COL_START))
            last = _ms(cells.get(COL_END))
            # 测试替身/手改表也再核一次过滤条件，不能把邻窗算进来。
            if (feishu.read_text(cells.get(COL_KIND)) != kind
                    or (table_id and project != table_id)
                    or first is None or last is None or first >= end_ms
                    or (start_ms is not None and last < start_ms)):
                continue
            group = chunks.setdefault(key, {"project": project, "at": first, "parts": {},
                                            "count": cells.get(COL_PARTS), "bad": False})
            index = _ms(cells.get(COL_PART))
            text = feishu.read_text(cells.get(COL_DATA))
            if (not key or index is None or group["count"] != cells.get(COL_PARTS)
                    or (index in group["parts"] and group["parts"][index] != text)):
                group["bad"] = True
            else:
                group["parts"][index] = text
        events: list[dict] = []
        for key, group in chunks.items():
            count = _ms(group["count"])
            try:
                if (group["bad"] or not count or set(group["parts"]) != set(range(count))):
                    raise ValueError("缺少分片")
                payload = "".join(group["parts"][i] for i in range(count))
                if hashlib.sha256(payload.encode("utf-8")).hexdigest() != key:
                    raise ValueError("内容校验失败")
                parsed = json.loads(payload)
                if not isinstance(parsed, list) or not all(isinstance(e, dict) for e in parsed):
                    raise ValueError("JSON 不是事件列表")
                if not all(e.get("type") == kind and e.get("table_id") == group["project"]
                           and _ms(e.get("at")) is not None for e in parsed):
                    raise ValueError("事件与分片索引不一致")
                events.extend(e for e in parsed if e["at"] < end_ms
                              and (start_ms is None or e["at"] >= start_ms))
            except (ValueError, TypeError):
                events.append({"type": "gap", "at": max(start_ms or 0, group["at"]),
                               "table_id": group["project"],
                               "reason": "留痕分片缺失或内容损坏，统计可能不全"})
        return events

    def start(self, table_id: str, at_ms: int) -> None:
        """项目首次启用留痕的真实时刻，不能补写成过去的日报起点。"""
        with self._lock:
            existing = self._read_kind("coverage", None, at_ms + 1, table_id)
            if not any(e.get("type") == "coverage" for e in existing):
                self.append([{"type": "coverage", "at": at_ms, "table_id": table_id}])

    def read_window(self, start_ms: int, end_ms: int, table_id: str = "") -> list[dict]:
        """扫描按窗口读，状态读到截止时的全部历史；调用方只应公开 aggregate 结果。

        run 留痕额外向前读一天，以识别跨窗口未完成批次。批次不是扫描成功数。
        """
        if end_ms <= start_ms:
            raise ValueError("日报结束时间必须晚于开始时间")
        with self._lock:
            events = self._read_kind("scan", start_ms, end_ms, table_id)
            for kind in ("status", "coverage"):
                events.extend(self._read_kind(kind, None, end_ms, table_id))
            for kind in ("run_start", "run_end", "gap"):
                events.extend(self._read_kind(kind, start_ms - 86_400_000, end_ms, table_id))
            return events


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%m月%d日 %H:%M")


def aggregate(events: list[dict], start: int, end: int, table_id: str, label: str,
              as_of: Optional[int] = None, *, as_of_ms: Optional[int] = None) -> dict:
    """由结构化事实生成可公开的汇总，不返回评论正文或原始事件。"""
    if end <= start:
        raise ValueError("日报结束时间必须晚于开始时间")
    if as_of_ms is not None:
        as_of = as_of_ms
    current = int(time.time() * 1000) if as_of is None else as_of
    cutoff = min(end, current)
    selected = [e for e in events if isinstance(e, dict)
                and e.get("table_id") == table_id and _ms(e.get("at")) is not None
                and e["at"] < cutoff]
    selected = list({event_key(e): e for e in selected}.values())
    scans = [e for e in selected if e.get("type") == "scan" and start <= e["at"]]
    scans.sort(key=lambda e: (e["at"], event_key(e)))
    started = min((e["at"] for e in selected if e.get("type") == "coverage"), default=None)
    limitations = ["仅统计成功读取的第一页评论；负面与竞品关键词命中属于预警，需人工复核。",
                   "评论为本期检出并去重的数量，不代表本期发布的新增评论；集中时段按发现时间计算。",
                   "处理状态按整帖标记，评论处理数随所属帖子状态统计；再次检出负面会重新打开已处理帖子。",
                   "覆盖依据已保存留痕与可见批次闭合情况，异常退出及留痕故障仍需结合运行日志核对。"]
    gaps = [e for e in selected if e.get("type") == "gap"
            and e.get("end", e["at"]) >= start]
    begun = {str(e.get("run_id")): e for e in selected
             if e.get("type") == "run_start" and e.get("run_id")}
    closed = {str(e.get("run_id")) for e in selected if e.get("type") == "run_end"}
    unclosed = [e for key, e in begun.items() if key not in closed]
    if started is None:
        limitations.append("尚无项目启用留痕记录，历史不可追溯；空白统计不能视为实际为零。")
    elif started > start:
        limitations.append(f"本期仅从{_stamp(started)}开始留痕，启用前的工作不可追溯。")
    if gaps or unclosed:
        limitations.append(f"发现 {len(gaps)} 处留痕缺口、{len(unclosed)} 个未闭合批次，本期统计可能不全。")

    post_records: dict[str, set[str]] = defaultdict(set)
    latest_negative: dict[str, int] = {}
    comments: dict[tuple[str, str], dict] = {}
    post_estimated = False
    unchecked = set()
    for scan in scans:
        post = str(scan.get("post_key") or f"row:{scan.get('record_id', '')}")
        post_estimated |= bool(scan.get("post_key_estimated")) or not bool(scan.get("post_key"))
        post_records[post].add(str(scan.get("record_id") or ""))
        if not scan.get("negative_checked"):
            unchecked.add(post)
        for hit in scan.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            cid = str(hit.get("id") or "")
            estimated = not bool(cid)
            if estimated:
                body = " ".join(str(hit.get("content") or "").split())
                cid = "text:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
            key = (post, cid)
            words = {str(w) for w in (hit.get("keywords") or []) if isinstance(w, str) and w}
            if key not in comments:
                comments[key] = {"post": post, "at": scan["at"], "estimated": estimated,
                                 "words": words}
            else:
                comments[key]["words"].update(words)
            latest_negative[post] = max(latest_negative.get(post, 0), scan["at"])

    legacy_statuses: dict[str, dict] = {}
    post_statuses: dict[str, dict] = {}
    operation_events = [e for e in selected if e.get("type") == "status"
                        and e.get("state") in STATES and start <= e["at"]]
    operations_unit = "篇" if operation_events and all(e.get("post_key") for e in operation_events) else "行"
    operations = {state: len({str(e.get("post_key") if operations_unit == "篇"
                                 else e.get("record_id")) for e in operation_events
                             if e["state"] == state}) for state in STATES}
    for event in sorted(selected, key=lambda e: (e["at"], event_key(e))):
        if event.get("type") == "status" and event.get("state") in STATES:
            if event.get("post_key"):
                post_statuses[str(event["post_key"])] = event
            else:
                legacy_statuses[str(event.get("record_id") or "")] = event
    post_states = dict.fromkeys(STATES, 0)
    comment_states = dict.fromkeys(STATES, 0)
    state_for = {}
    for post in latest_negative:
        candidates = [legacy_statuses[r] for r in post_records[post] if r in legacy_statuses]
        if post in post_statuses:
            candidates.append(post_statuses[post])
        marked = max(candidates, key=lambda e: e["at"], default=None)
        state = marked["state"] if marked else "pending"
        if (state == "resolved" and marked["at"] < latest_negative[post]):
            state = "pending"
        state_for[post] = state
        post_states[state] += 1
    hours: Counter[int] = Counter()
    keywords: Counter[str] = Counter()
    for comment in comments.values():
        comment_states[state_for[comment["post"]]] += 1
        hours[(comment["at"] // 3_600_000) * 3_600_000] += 1
        keywords.update(comment["words"])
    peak = None
    if hours:
        hour, count = min(hours.items(), key=lambda pair: (-pair[1], pair[0]))
        peak = {"start_ms": hour, "end_ms": hour + 3_600_000, "count": count}
    estimated = any(c["estimated"] for c in comments.values())
    if estimated:
        limitations.append("部分评论缺少上游评论 ID，按同帖正文归一化去重；相同文案可能合并，评论数为估计。")
    if post_estimated:
        limitations.append("部分帖子缺少作品 ID，使用链接或业务行识别，跨链接去重可能不完整。")
    if unchecked:
        limitations.append(f"本期 {len(unchecked)} 篇帖子至少一次扫描未进行负面词判定。")
    coverage_complete = started is not None and started <= start and not gaps and not unclosed
    report = {
        "table_id": table_id, "label": label,
        "window": {"start_ms": start, "end_ms": end, "as_of_ms": cutoff,
                   "timezone": "Asia/Shanghai", "complete": current >= end,
                   "coverage_complete": coverage_complete, "coverage_started_at_ms": started},
        "scanned_posts": len(post_records), "scan_count": len(scans),
        "negative_posts": len(latest_negative), "negative_comments": len(comments),
        "comment_count_estimated": estimated, "post_count_estimated": post_estimated,
        "post_states": post_states, "comment_states": comment_states,
        "operations": operations, "operations_unit": operations_unit,
        "peak": peak, "keywords": [{"word": w, "count": n}
                                 for w, n in sorted(keywords.items(), key=lambda p: (-p[1], p[0]))[:12]],
        "competitors": "待人工复核填写", "limitations": limitations,
    }
    prefix = "实时预览；" if current < end else ""
    if not coverage_complete:
        prefix += "留痕覆盖不完整，以下仅为已留痕数据；"
    estimate = "约 " if estimated else ""
    body = (f"{_stamp(start)}—{_stamp(end)}（北京时间），{prefix}"
            f"机器成功扫描{label} {len(post_records)} 篇帖子，"
            f"{len(latest_negative)} 篇帖子检出负面/竞品词预警，"
            f"本期检出去重预警评论共 {estimate}{len(comments)} 条。"
            f"本期检出预警的截至截止时状态（按整帖标记）：已处理 {post_states['resolved']} 篇（涉及 {comment_states['resolved']} 条），"
            f"正在处理 {post_states['processing']} 篇（涉及 {comment_states['processing']} 条），"
            f"待实施处理 {post_states['pending']} 篇（涉及 {comment_states['pending']} 条）。")
    body += (f"本期运营操作（含存量问题）：已处理 {operations['resolved']} {operations_unit}、"
             f"标为正在处理 {operations['processing']} {operations_unit}、"
             f"退回待处理 {operations['pending']} {operations_unit}"
             f"（同一{operations_unit}可有状态流转，不重复推算存量评论条数）。")
    if peak:
        body += f"检出较集中的时段：{_stamp(peak['start_ms'])}—{_stamp(peak['end_ms'])}（{peak['count']} 条）。"
    else:
        body += "暂无可统计的负面检出集中时段。"
    features = "、".join(f"{k['word']}（{k['count']}条）" for k in report["keywords"])
    body += f"主要命中词：{features or '暂无'}；主要内容特征及疑似竞品：待人工复核填写。"
    body += "统计仅覆盖成功取得的第一页评论；评论按检出时间归属，不代表本期新增。"
    if estimated:
        body += "部分评论缺少 ID，按正文去重，评论数为估计。"
    report["text"] = body
    return report
