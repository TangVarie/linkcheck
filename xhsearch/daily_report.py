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
# 开跑没多久、还没收尾的批次算「进行中」，不算「未闭合」：日报窗口正好落在
# 一轮巡检中间时（当期实时预览、或者一轮跨过 16:00），不能报覆盖不完整。
RUN_GRACE_MS = 2 * 3_600_000
# 状态 / 启用记录要读「截止时的全部历史」。每次都整段重读，几个月后每次刷新
# 日报都要翻完几千条点击记录（还拿着读锁，其他人一起等）。所以记在内存里，
# 之后只往前多读这么一段补新的；隔一阵整段重读一次，纠正任何漏读。
HISTORY_KINDS = ("status", "coverage")
HISTORY_OVERLAP_MS = 2 * 86_400_000
HISTORY_FULL_SECONDS = 6 * 3600
# 留痕启用之后，一期里过了这么久还一个巡检批次都没留下，就不能说「覆盖完整」：
# 留痕整段写不进去时什么痕迹都不会留（缺口和批次本身也写在同一张表里），
# 报出来的就是一张「0 篇、0 条」的完整日报。
QUIET_RUNS_MS = 12 * 3_600_000
# 定期清理（Journal.prune）：扫描、批次、缺口留这么多天；面板只看得到最近 90 天的
# 日报，读批次时还要往前多读一天。超期的处理状态折叠成每篇最后一条，不删。
RETENTION_DAYS = 100
# 超期的状态批次攒到这么多个才折叠一次（折叠后通常只剩几个）。
COMPACT_MIN_BATCHES = 20
# 飞书多维表格单表行数上限按版本不同（帮助中心「多维表格行数扩容」）：基础版 / 商业
# 标准版默认 2,000 行，管理员可免费领到 2 万行；商业旗舰版和企业版 5 万行；
# 买扩容包最多 200 万行。满了就新增不了记录。
ROW_LIMIT_TIERS = (2_000, 20_000, 50_000)
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


def gap_events(table_id: str, first_ms: int, last_ms: int, reason: str) -> list[dict]:
    """一段留痕缺口按日报窗口切开写：每一期只往前后各多读一天的 gap，
    跨了好几天的缺口只写一条，中间那几期就读不到它。"""
    events, at = [], first_ms
    while at <= last_ms:
        moment = datetime.fromtimestamp(at / 1000, SHANGHAI)
        _, end = window_for_date(moment.date() + timedelta(days=int(moment.hour >= 16)))
        events.append({"type": "gap", "at": at, "end": min(last_ms, end - 1),
                       "table_id": table_id, "reason": reason})
        at = end
    return events


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
        # (kind, table_id) -> {"events": {身份: 事件}, "through": 毫秒, "full_at": 秒}
        self._history: dict[tuple[str, str], dict] = {}
        # 自己写进去的历史事件：(kind, table_id) -> {身份: (写入时刻秒, 事件)}。
        # **不管缓存建没建都记**：整段读（包括面板起来后的第一次）正赶上补写时，
        # 飞书那边读回来的可能还没有它，靠这里补回去。留 HISTORY_FULL_SECONDS，
        # 之后的整段重读一定已经读得到。
        self._written: dict[tuple[str, str], dict[str, tuple[float, dict]]] = {}

    def _find(self) -> str:
        with self._lock:
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
        planned = _plan(events)
        # 锁只护「找/建表」和内存缓存，**不护飞书请求**：面板点「已处理」要写留痕，
        # 日报那边正在翻好几页留痕时，这一下不能排在它后面干等。
        with self._lock:
            table_id = self._ensure()
        self._write(self._bitable(self.app_token, table_id), planned)

    def _write(self, table, planned: list[dict]) -> None:
        """把 _plan 排好的一批批写进去（不持锁）。"""
        for item in planned:
            project, kind, batch, key = item["project"], item["kind"], item["batch"], item["key"]
            records = item["records"]
            created = table.batch_create(records, client_token=feishu.idempotency_key(
                f"journal:{self.app_token}:{key}"))
            if len(created) != len(records) or not all(created):
                raise ValueError("巡检留痕未完整写入，日报不能视为完整")
            # 自己写进去的历史事件直接记进内存：面板补写的状态可能带着
            # 好几天前的时刻，增量读往前那一段不一定够得着。
            if kind in HISTORY_KINDS:
                written_at = self._wall()
                with self._lock:
                    for scope in (project, ""):
                        mine = self._written.setdefault((kind, scope), {})
                        cached = self._history.get((kind, scope))
                        for e in batch:
                            mine[_history_id(e)] = (written_at, e)
                            if cached is not None:
                                cached["events"][_history_id(e)] = e

    def usage(self) -> Optional[int]:
        """「巡检留痕」表现在有几行（飞书单表有行数上限，满了就写不进去）。
        还没建表是 0；读不出总数是 None。"""
        found = self._find()
        if not found:
            return 0
        return self._bitable(self.app_token, found).count()

    def prune(self, now_ms: int, *, retention_days: int = RETENTION_DAYS) -> dict:
        """定期清理，不让这张表涨到飞书的行数上限（满了之后什么都写不进去）。

        - 扫描、批次、缺口：超过 retention_days 天的整批删掉。面板只看得到最近
          90 天的日报，读批次时往前多读一天，留 100 天够用。
        - 处理状态：是累积的，不能直接删——把超期的那些折叠成「每篇帖子（没有帖子键
          的按行）最后一条」，原时刻原样保留，先写折叠后的、再删旧的（中途断了
          只会多几条重复的，读的时候按事实去重）。旧状态批次不到 COMPACT_MIN_BATCHES
          个就不折叠，省得每次都重写一遍。缺片、内容对不上的批次不动。
        - 启用记录（coverage）一条都不删：日报靠它知道从什么时候开始有记录。
        """
        found = self._find()
        if not found:
            return {"deleted": 0, "folded": 0}
        cutoff = now_ms - retention_days * 86_400_000
        table = self._bitable(self.app_token, found)
        rows = table.search(FIELD_NAMES, filter_spec={"conjunction": "and", "conditions": [
            {"field_name": COL_END, "operator": "isLess", "value": [str(cutoff)]}]})
        groups: dict[str, dict] = {}
        for record in rows:
            cells = record.get("fields") or {}
            last = _ms(cells.get(COL_END))
            record_id = str(record.get("record_id") or "")
            if last is None or last >= cutoff or not record_id:
                continue
            key = feishu.read_text(cells.get(COL_KEY)) or record_id
            group = groups.setdefault(key, {"kind": feishu.read_text(cells.get(COL_KIND)),
                                            "project": feishu.read_text(cells.get(COL_PROJECT)),
                                            "ids": [], "parts": {}, "count": cells.get(COL_PARTS)})
            group["ids"].append(record_id)
            index = _ms(cells.get(COL_PART))
            if index is not None:
                group["parts"][index] = feishu.read_text(cells.get(COL_DATA))
        doomed = [rid for g in groups.values()
                  if g["kind"] in ("scan", "run_start", "run_end", "gap") for rid in g["ids"]]
        folded = 0
        old_status = {key: g for key, g in groups.items() if g["kind"] == "status"}
        parsed: dict[str, list[dict]] = {}
        for key, group in old_status.items():
            count = _ms(group["count"])
            if not count or set(group["parts"]) != set(range(count)):
                continue
            payload = "".join(group["parts"][i] for i in range(count))
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != key:
                continue
            try:
                events = json.loads(payload)
            except ValueError:
                continue
            if isinstance(events, list) and all(isinstance(e, dict) for e in events):
                parsed[key] = events
        if len(parsed) >= COMPACT_MIN_BATCHES:
            latest: dict[tuple, dict] = {}
            for events in parsed.values():
                for e in events:
                    if e.get("type") != "status" or _ms(e.get("at")) is None:
                        continue
                    who = ((str(e.get("table_id")), "post", str(e["post_key"])) if e.get("post_key")
                           else (str(e.get("table_id")), "row", str(e.get("record_id") or "")))
                    if who not in latest or (e["at"], event_key(e)) > (
                            latest[who]["at"], event_key(latest[who])):
                        latest[who] = e
            planned = _plan(list(latest.values()))
            keep = {item["key"] for item in planned}
            fresh = [item for item in planned if item["key"] not in parsed]
            self._write(table, fresh)
            stale = [rid for key in parsed if key not in keep for rid in old_status[key]["ids"]]
            doomed.extend(stale)
            # 实际少掉的状态条数（被同一篇后来的状态盖掉的那些）
            folded = (sum(len(events) for key, events in parsed.items() if key not in keep)
                      - sum(len(item["batch"]) for item in fresh))
        if doomed:
            table.batch_delete(doomed)
        return {"deleted": len(doomed), "folded": folded}

    def _read_kind(self, kind: str, start_ms: Optional[int], end_ms: int,
                   table_id: str = "", whole: Optional[dict] = None) -> list[dict]:
        """`whole` 给了就把这次完整读回来的分片记进去：键 → 这一批的**全部**事件
        （不按时间截）。历史缓存靠它清掉旧缺口、补回整批。"""
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
                if whole is not None:
                    whole[key] = parsed
            except (ValueError, TypeError):
                gap = {"type": "gap", "at": max(start_ms or 0, group["at"]),
                       "table_id": group["project"], "chunk": key,
                       "reason": "留痕分片缺失或内容损坏，统计可能不全"}
                if kind in HISTORY_KINDS:
                    # 状态是累积的：坏掉的那批之后的每一期都受影响，不只它自己那一期。
                    gap["history"] = True
                events.append(gap)
        return events

    def start(self, table_id: str, at_ms: int) -> None:
        """项目首次启用留痕的真实时刻，不能补写成过去的日报起点。"""
        with self._lock:
            existing = self._read_kind("coverage", None, at_ms + 1, table_id)
            if not any(e.get("type") == "coverage" for e in existing):
                self.append([{"type": "coverage", "at": at_ms, "table_id": table_id}])

    def read_window(self, start_ms: int, end_ms: int, table_id: str = "") -> list[dict]:
        """扫描按窗口读，状态读到截止时的全部历史；调用方只应公开 aggregate 结果。

        run 留痕前后各多读一天：往前，识别跨窗口未完成的批次；往后，一轮在截止
        前开跑、截止后收尾的，要看得到它的 run_end（aggregate 只拿截止前的事实
        统计，收尾事件只用来判「闭合了没有」）。批次不是扫描成功数。
        """
        if end_ms <= start_ms:
            raise ValueError("日报结束时间必须晚于开始时间")
        events = self._read_kind("scan", start_ms, end_ms, table_id)
        for kind in HISTORY_KINDS:
            events.extend(self._read_history(kind, end_ms, table_id))
        for kind in ("run_start", "run_end", "gap"):
            events.extend(self._read_kind(kind, start_ms - 86_400_000,
                                          end_ms + 86_400_000, table_id))
        return events

    def _read_history(self, kind: str, end_ms: int, table_id: str) -> list[dict]:
        """截止时的全部历史，读过的记在内存里，之后只补最近那一段。"""
        now = self._wall()
        through = int(now * 1000) + 86_400_000      # 时钟偏一点的未来事件也收进来
        with self._lock:
            known_cache = self._history.get((kind, table_id))
            full = known_cache is None or now - known_cache["full_at"] > HISTORY_FULL_SECONDS
            since = None if full else known_cache["through"] - HISTORY_OVERLAP_MS
        whole: dict[str, list[dict]] = {}
        fresh = self._read_kind(kind, since, through, table_id, whole)    # 不持锁
        with self._lock:
            return self._merge_history(kind, table_id, full, now, through, fresh, whole, end_ms)

    def _merge_history(self, kind: str, table_id: str, full: bool, now: float,
                       through: int, fresh: list[dict], whole: dict, end_ms: int) -> list[dict]:
        """把一次读回来的并进缓存（调用方持锁）。"""
        cached = self._history.get((kind, table_id))
        if full or cached is None:
            cached = {"events": {}, "full_at": now, "through": through}
            # 整段读期间（或之前不久）自己写进去的：飞书那边读回来的可能还没有，补上。
            mine = self._written.get((kind, table_id), {})
            for identity, (written_at, event) in list(mine.items()):
                cached["events"][identity] = event
                if written_at < now - HISTORY_FULL_SECONDS:
                    del mine[identity]
            self._history[(kind, table_id)] = cached
        for event in fresh:
            identity = _history_id(event)
            known = cached["events"].get(identity)
            # 还没读全的那批：增量读报的缺口时刻被截到了截止线，留最早那个，
            # 不然更早那几期会悄悄变成「覆盖完整」。
            if (known is not None and event.get("type") == "gap"
                    and known["at"] <= event["at"]):
                continue
            cached["events"][identity] = event
        # 完整读回来的分片整批进缓存：增量读按时间截掉的那部分旧事件也要（一批
        # 可能跨在截止线上，比如上次没读全、记成缺口的那批，旧事件从没进过缓存）。
        # 它之前的缺口作废，不然日报要一直标「不完整」到下一次整段重读。
        for key, batch in whole.items():
            cached["events"].pop("gap:" + key, None)
            cached["events"].update((_history_id(e), e) for e in batch)
        cached["through"] = max(cached["through"], through)
        return [e for e in cached["events"].values() if e["at"] < end_ms]


def usage_note(rows: Optional[int]) -> str:
    """「巡检留痕」行数接近哪一档飞书上限时给运营 / 管理员看的一句话；不接近就空。
    不知道你们是哪个版本，所以按档猜：过了某一档还在写，说明上限至少是下一档。"""
    if rows is None:
        return ""
    limit = next((tier for tier in ROW_LIMIT_TIERS if rows < tier), None)
    if limit is None or rows < limit * 0.8:
        return ""
    advice = {
        2_000: "如果是飞书基础版 / 商业标准版且没领过扩容，单表上限就是 2,000 行，满了日报留痕就写不进去："
               "请飞书管理员在这张表的「行数扩容」里免费领到 2 万行（旗舰版 / 企业版可直接解锁 5 万行）",
        20_000: "如果上限是 2 万行（基础版 / 商业标准版领过扩容），快满了：请飞书管理员购买行数扩容包"
                "或升级版本",
        50_000: "如果上限是 5 万行（商业旗舰版 / 企业版），快满了：请飞书管理员购买行数扩容包",
    }[limit]
    return (f"「巡检留痕」表已有 {rows:,} 行。{advice}。系统会自动清掉 {RETENTION_DAYS} 天前的"
            "扫描记录，但表本身的上限要在飞书里调。")


def _plan(events: list[dict]) -> list[dict]:
    """把事件按（项目, 类型）装箱、分片，排成要写的一批批。内容定了键就定了：
    同一批内容重发是同一个幂等键，清理时也靠这个键认出「已经写过了」。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in events:
        if (not isinstance(event, dict) or event.get("type") not in KINDS
                or _ms(event.get("at")) is None or not event.get("table_id")):
            raise ValueError("留痕事件缺少 type、at 或 table_id")
        if event["type"] == "status" and event.get("state") not in STATES:
            raise ValueError("状态留痕必须是 pending / processing / resolved")
        groups[(str(event["table_id"]), event["type"])].append(dict(event))
    planned = []
    for (project, kind), grouped in sorted(groups.items()):
        # 有界装箱让重试和单格长度都稳定；同一批事件去重后按事实键排序。
        unique = {event_key(e): e for e in grouped}
        ordered = [unique[k] for k in sorted(unique)]
        for start in range(0, len(ordered), MAX_BATCH_EVENTS):
            batch = ordered[start:start + MAX_BATCH_EVENTS]
            payload = _json(batch)
            key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            parts = [payload[i:i + CHUNK_CHARS] for i in range(0, len(payload), CHUNK_CHARS)]
            first, last = min(e["at"] for e in batch), max(e["at"] for e in batch)
            planned.append({"project": project, "kind": kind, "batch": batch, "key": key,
                            "records": [{"fields": {
                                COL_KEY: key, COL_PROJECT: project, COL_KIND: kind,
                                COL_START: first, COL_END: last, COL_PART: i,
                                COL_PARTS: len(parts), COL_DATA: part,
                            }} for i, part in enumerate(parts)]})
    return planned


def _history_id(event: dict) -> str:
    """内存里历史事件的身份。坏分片报的 gap 每次读的时刻可能不同，按分片认。"""
    if event.get("type") == "gap" and event.get("chunk"):
        return "gap:" + str(event["chunk"])
    return event_key(event)


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime("%m月%d日 %H:%M")


def present_report(report: dict, current_snapshot: Optional[dict], now_ms: int,
                   *, journal_error: str = "") -> dict:
    """展示与复制层：优先交代可确认的数字，不把缺失留痕写成零工作量。"""
    shown = dict(report)
    window = report["window"]
    live = window["start_ms"] <= now_ms < window["end_ms"]
    snapshot = dict(current_snapshot or {"available": False})
    snapshot["window_is_current"] = live
    snapshot["reason"] = snapshot.get("error") or (
        "现有业务表尚未读取完成" if not snapshot.get("available") else "")
    shown["current_snapshot"] = snapshot
    shown["journal_error"] = journal_error
    if live and snapshot.get("available"):
        mode = "snapshot"
    elif not journal_error and (window.get("coverage_complete") or report.get("scan_count")):
        mode = "journal"
    else:
        mode = "unavailable"
    shown["display_mode"] = mode
    shown["source_label"] = {"snapshot": "现有表快照", "journal": "巡检记录",
                              "unavailable": "尚无完整记录"}[mode]
    shown["limitations"] = list(report.get("limitations") or [])
    if mode == "snapshot":
        shown["limitations"].insert(0, "本期已检查按现有表最近一次成功检查统计；负面快照及处理状态为当前值，不是逐次扫描历史。")
    if journal_error:
        shown["limitations"].append("巡检明细暂未同步，现有表的当前统计仍可查看。")
    actual_end = min(window["end_ms"], now_ms if mode == "snapshot" else
                     window.get("as_of_ms", now_ms), now_ms)
    period = f"{_stamp(window['start_ms'])}—{_stamp(actual_end)}"
    if mode == "unavailable":
        shown["text"] = ""
        shown["unavailable_reason"] = ("所选周期没有可追溯的巡检记录，不能据此认定没有扫描或负面。"
                                         if not live else "巡检记录和现有表数据暂未就绪，请稍后再试。")
        return shown
    lines = [f"{shown['label']}｜{period}（北京时间）"]
    if mode == "snapshot":
        checked = snapshot.get("window_checked_posts")
        monitoring = snapshot.get("monitoring_posts")
        negative = snapshot.get("window_negative_posts")
        comments = snapshot.get("window_negative_comments")
        lower = snapshot.get("window_negative_comments_lower_bound")
        if checked is not None:
            scope = (f"项目已纳入监测 {monitoring} 篇帖子；"
                     if monitoring is not None and monitoring >= checked else "")
            lines.append(scope + f"上述时段已检查 {checked} 篇帖子。")
        else:
            lines.append("所选时段的检查数量待核实。")
        if negative is not None:
            lines[-1] += f"其中 {negative} 篇有负面预警。"
        states = snapshot.get("window_post_states") or {}
        if comments is not None:
            lines.append(f"快照可见{'至少 ' if lower else ' '}{comments} 条预警评论。")
        elif negative:
            lines.append("负面评论条数待核实。")
        if negative is not None:
            lines.append("上述帖子：" + "，".join(
                (f"{label} {states[state]} 篇" if states.get(state) is not None else f"{label}待核实")
                for state, label in (("resolved", "已处理"), ("processing", "正在处理"),
                                     ("pending", "待处理"))) + "。")
        else:
            lines.append("所选时段的负面数量及处理进度待核实。")
        lines.append("按现有表最近检查及评论快照统计。")
    else:
        prefix = "已扫描" if window.get("coverage_complete") else "已记录扫描"
        lines.append(f"{prefix} {report['scanned_posts']} 篇帖子，{report['negative_posts']} 篇检出预警，"
                     f"共{'约 ' if report.get('comment_count_estimated') else ' '}{report['negative_comments']} 条评论。")
        states = report.get("post_states") or {}
        lines.append("负面帖子：" + "，".join(
            f"{label} {states.get(state, 0)} 篇" for state, label in
            (("resolved", "已处理"), ("processing", "正在处理"), ("pending", "待处理"))) + "。")
        peak = report.get("peak")
        if peak:
            lines.append(f"检出集中时段：{_stamp(peak['start_ms'])}—{_stamp(peak['end_ms'])}。")
        words = [item["word"] for item in report.get("keywords", [])[:5]]
        if words:
            lines.append("主要命中词：" + "、".join(words) + "。")
        if not window.get("coverage_complete"):
            lines.append("该时段记录覆盖不完整。")
    lines.insert(2, "巡检按发布时间分批进行：新帖高频检查，历史帖分批复查。")
    operations = report.get("operations") or {}
    if operations.get("resolved"):
        lines.append(f"时段内完成处理操作 {operations['resolved']} {report.get('operations_unit', '行')}（含存量问题）。")
    shown["text"] = "\n".join(lines)
    return shown


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
            and (e.get("history") or e.get("end", e["at"]) >= start)]
    begun = {str(e.get("run_id")): e for e in selected
             if e.get("type") == "run_start" and e.get("run_id")}
    # 收尾看**所有**读到的 run_end（含截止之后的）：一轮 15:58 开跑、16:03 收尾，
    # 截止 16:00 的那一期它是闭合的。开跑不到 RUN_GRACE_MS 还没收尾的是进行中。
    runs = sorted((e for e in events if isinstance(e, dict) and e.get("table_id") == table_id
                   and e.get("type") in ("run_start", "run_end") and _ms(e.get("at")) is not None),
                  key=lambda e: e["at"])
    closed = {str(e.get("run_id")) for e in runs if e["type"] == "run_end"}

    def ended_before_window(run: dict) -> bool:
        # 被杀掉的那一轮没有 run_end。同一种巡检（queue / sweep 各一个定时服务，
        # 自己不会重叠）后来又开过一轮，它就在那之前结束了；那一刻还在本期开始
        # 之前，就和本期无关。没记 mode 的老记录证明不了，照旧算。
        if not run.get("mode"):
            return False
        later = next((e["at"] for e in runs if e["at"] > run["at"]
                      and e.get("mode") == run["mode"]
                      and str(e.get("run_id")) != str(run.get("run_id"))), None)
        return later is not None and later <= start

    unclosed = [e for key, e in begun.items()
                if key not in closed and current - e["at"] >= RUN_GRACE_MS
                and not ended_before_window(e)]
    quiet = (started is not None and started <= start and cutoff - start >= QUIET_RUNS_MS
             and not any(e.get("type") == "run_start" and e["at"] >= start for e in selected))
    if started is None:
        limitations.append("尚无项目启用留痕记录，历史不可追溯；空白统计不能视为实际为零。")
    elif started > start:
        limitations.append(f"本期仅从{_stamp(started)}开始留痕，启用前的工作不可追溯。")
    if gaps or unclosed:
        limitations.append(f"发现 {len(gaps)} 处留痕缺口、{len(unclosed)} 个未闭合批次，本期统计可能不全。")
    if quiet:
        limitations.append("本期没有留痕到任何巡检批次：可能确实没有到期的帖子，也可能是留痕一直"
                           "写不进去；空白统计不能视为实际为零，请对照运行日志。")

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
    # auto：面板自己补的状态（问题消失、「正在处理」被自动收掉），不是运营点的。
    operation_events = [e for e in selected if e.get("type") == "status"
                        and e.get("state") in STATES and start <= e["at"]
                        and not e.get("auto")]
    operations_unit = "篇" if operation_events and all(e.get("post_key") for e in operation_events) else "行"
    operations = {state: len({str(e.get("post_key") if operations_unit == "篇"
                                 else e.get("record_id")) for e in operation_events
                             if e["state"] == state}) for state in STATES}
    # 短链这一轮现场展开了：扫描事件记的是展开后的作品 ID，面板那一刻还只认得
    # 链接（「作品ID」没写进表之前），点出来的状态带的是链接那个键。扫描事件
    # 把那个键记在 post_alias 里，这里认回同一篇。换了链接的行没有这层别名，
    # 旧帖的状态照旧不会套到新帖上。
    aliases = {str(s["post_alias"]): str(s.get("post_key"))
               for s in scans if s.get("post_alias") and s.get("post_key")}
    for event in sorted(selected, key=lambda e: (e["at"], event_key(e))):
        if event.get("type") == "status" and event.get("state") in STATES:
            if event.get("post_key"):
                key = str(event["post_key"])
                post_statuses[aliases.get(key, key)] = event
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
    coverage_complete = (started is not None and started <= start and not gaps
                         and not unclosed and not quiet)
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
    # 内容特征与疑似竞品由面板 AI 草稿 + 人工修订补在正文之后（见 panel_view
    # composeReport）。这里再写一句「待人工填写」会和草稿同时出现，自相矛盾。
    body += f"主要命中词：{features or '暂无'}。"
    body += "统计仅覆盖成功取得的第一页评论；评论按检出时间归属，不代表本期新增。"
    if estimated:
        body += "部分评论缺少 ID，按正文去重，评论数为估计。"
    report["text"] = body
    return report
