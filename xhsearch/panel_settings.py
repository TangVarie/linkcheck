"""面板自己的几项设置，存在注册表那个 base 里的一张小表。

## 为什么要有它

「新建的表给谁开权限」是运营在面板上点出来的（选群、填手机号），得落到一个
**重新部署之后还在**的地方。面板容器没有持久盘（Railway 的 Volume 只能挂一个
服务，挂给了谁都不对）；环境变量倒是持久，但改一次要一个可写的 Railway token
且触发重新部署——而这件事的本意就是「点一下，不用碰部署」。

注册表那张表所在的 base 是这套东西自己的（`init-registry` 建的，应用是所有者），
往里再建一张「面板设置」的键值表，代价是零：同一份凭据、同一个 base、人看得懂、
面板挂了也能进去改。

## 它是键值表，不是配置文件

两列：`键` / `值`。值一律是 JSON 文本。现在有这些键：

* `share.editor_chats`  → `[{"chat_id": "oc_…", "name": "梨响运营群"}, …]`
* `todo.resolved`       → `{"tbl…/rec…": {"reasons": ["有负面"], "seen": 毫秒, "at": 毫秒}, …}`
  面板上点的「已解决」（见 panel.Resolutions、summary.apply_resolutions）。
* `channels.standby`    → `{"enabled": true, "at": 毫秒}`：SocialDataX 备用通道开关
  （见 standby.py）。面板写，cron 每一轮开跑时只读这一个键。没有这一行 = 关。
* `trial.search.*`      → 搜索可见试跑的账本、拿到过的页、租约和最近一份报告
  （见 trial.py）。账本比一个单元格装得下的大，所以是切成几段存的——那几段不是
  JSON，走下面的 `read_texts` / `write_text`（原样的文本）。

「谁可管理」刻意**不**存在这里——它来自后台环境变量 FEISHU_TABLE_MANAGERS。
面板口令是运营共用的，管理权限不能是面板上点一下就给的东西（见 panel.Projects）。

按**名字**认表（「面板设置」）：没建过就建，建过就复用——不用再多一个环境变量。
读结果在进程里缓存几分钟；写完立刻失效。面板只有一个进程，够了。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from . import feishu

TABLE_NAME = "面板设置"
COL_KEY = "键"
COL_VALUE = "值"
FIELDS = [{"field_name": COL_KEY, "type": 1}, {"field_name": COL_VALUE, "type": 1}]

KEY_EDITOR_CHATS = "share.editor_chats"
KEY_RESOLVED = "todo.resolved"
# 每期日报结算时各项目的负面处置数（panel.PeriodCloses 写，工作台「比上一期」读）。
KEY_CLOSES = "metrics.closes"
# 飞书多维表格一个文本单元格最多 100,000 字（帮助中心「多维表格常见上限」）。
# 超了飞书直接拒写；在本地先拦下，报一句看得懂的话。
CELL_MAX_CHARS = 100_000

CACHE_SECONDS = 300.0


class SettingsStore:
    """注册表 base 里那张「面板设置」表的读写。"""

    def __init__(self, workspace: feishu.Workspace, bitable_factory: Callable,
                 app_token: str, *, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic):
        self.workspace = workspace
        self._bitable = bitable_factory     # (app_token, table_id) -> feishu.Bitable
        self.app_token = app_token
        self.log = log
        self._clock = clock
        self._table_id: str = ""
        self._rows: Optional[dict[str, tuple[str, str]]] = None   # key -> (record_id, value)
        self._dupes: set[str] = set()      # 最近一次读到的、出现了不止一行的键
        self._read_at: float = 0.0
        # 「找行 → 没有就 batch_create、有就 batch_update」不是原子的：两个运营
        # 同时第一次保存，两个线程都看到没有那一行，就各建一行——同一个键两行，
        # 之后读到哪一行看飞书心情。面板是单进程，一把锁把整段串起来就够。
        self._lock = threading.RLock()

    # ---------- 找表 / 建表 ----------

    def _find_table(self) -> str:
        if self._table_id:
            return self._table_id
        for table in self.workspace.list_tables(self.app_token):
            if table.get("name") == TABLE_NAME:
                self._table_id = table["table_id"]
                return self._table_id
        return ""

    def _ensure_table(self) -> str:
        table_id = self._find_table()
        if table_id:
            return table_id
        table_id = self.workspace.create_table(self.app_token, TABLE_NAME, FIELDS)
        if not table_id:
            raise feishu.FeishuError(-1, f"建「{TABLE_NAME}」表失败：飞书没返回 table_id")
        self.log(f"🧱 在注册表 base 里建了「{TABLE_NAME}」表（{table_id}）")
        self._table_id = table_id
        self._rows = {}
        return table_id

    # ---------- 读 ----------

    def _load(self, force: bool = False) -> dict[str, tuple[str, str]]:
        fresh = self._rows is not None and (self._clock() - self._read_at) < CACHE_SECONDS
        if fresh and not force:
            return self._rows or {}
        table_id = self._find_table()
        rows: dict[str, tuple[str, str]] = {}
        dupes: set[str] = set()
        if table_id:
            table = self._bitable(self.app_token, table_id)
            for record in table.search([COL_KEY, COL_VALUE]):
                cells = record.get("fields") or {}
                key = feishu.read_text(cells.get(COL_KEY)).strip()
                if key:
                    if key in rows:
                        dupes.add(key)
                    rows[key] = (record.get("record_id") or "",
                                 feishu.read_text(cells.get(COL_VALUE)))
        self._dupes = dupes
        self._rows, self._read_at = rows, self._clock()
        return rows

    def get_json(self, key: str, default: Any) -> Any:
        """读一个键，JSON 解开。没有、或存的不是合法 JSON → default。"""
        with self._lock:
            row = self._load().get(key)
        if row is None or not row[1].strip():
            return default
        try:
            return json.loads(row[1])
        except ValueError:
            return default

    def read_texts(self, prefix: str) -> tuple[dict[str, str], set[str]]:
        """**现读一遍**，把键以 `prefix` 开头的行原样交回 →（键 → 值的原文, 出现了不止一行的键）。
        不解 JSON、不走缓存：试跑的账本要的是此刻表里的那一份。"""
        with self._lock:
            rows = self._load(force=True)
            dupes = {key for key in self._dupes if key.startswith(prefix)}
        return {key: text for key, (_rid, text) in rows.items()
                if key.startswith(prefix)}, dupes

    def read_one(self, key: str) -> Optional[str]:
        """只读一个键此刻的值（原文）；没有这一行就是 None。带着筛选条件去读，
        不把整张表拉回来——试跑每发一个付费请求之前都要看一眼租约。
        同一个键有几行时取最后一行（和 `_load` 一样）。不动缓存。"""
        with self._lock:
            table_id = self._find_table()
            if not table_id:
                return None
            table = self._bitable(self.app_token, table_id)
            found: Optional[str] = None
            try:
                records = table.search(
                    [COL_KEY, COL_VALUE],
                    filter_spec={"conjunction": "and", "conditions": [
                        {"field_name": COL_KEY, "operator": "is", "value": [key]}]})
            except feishu.FeishuError:
                # 筛选条件飞书不认（这个写法没在真表上验过）：退回整张表读一遍，慢一点，结果一样。
                records = table.search([COL_KEY, COL_VALUE])
            for record in records:
                cells = record.get("fields") or {}
                # 筛选是飞书那边做的；这里再对一遍，筛选没生效也不会拿错行。
                if feishu.read_text(cells.get(COL_KEY)).strip() == key:
                    found = feishu.read_text(cells.get(COL_VALUE))
        return found

    def read_prefix(self, prefix: str) -> dict[str, str]:
        """键以 `prefix` 开头的那几行此刻的值（键 → 原文）。和 `read_one` 一样带着筛选条件
        去读、不动缓存——试跑的租约是一边一行，每发一个付费请求之前都要把这几行看一遍，
        不能每次把账本的分段也拉回来。同一个键有几行时取最后一行。"""
        with self._lock:
            table_id = self._find_table()
            if not table_id:
                return {}
            table = self._bitable(self.app_token, table_id)
            try:
                records = table.search(
                    [COL_KEY, COL_VALUE],
                    filter_spec={"conjunction": "and", "conditions": [
                        {"field_name": COL_KEY, "operator": "contains", "value": [prefix]}]})
            except feishu.FeishuError:
                # 筛选条件飞书不认（这个写法没在真表上验过）：退回整张表读一遍，慢一点，结果一样。
                records = table.search([COL_KEY, COL_VALUE])
            found: dict[str, str] = {}
            for record in records:
                cells = record.get("fields") or {}
                key = feishu.read_text(cells.get(COL_KEY)).strip()
                if key.startswith(prefix):        # 筛选没生效也不会拿错行
                    found[key] = feishu.read_text(cells.get(COL_VALUE))
        return found

    # ---------- 写 ----------

    def write_text(self, key: str, text: str, *, fresh: bool = True) -> None:
        """写一个键的原文（不是 JSON 也行）。超过单元格上限的在本地拦下。

        `fresh=False`：不为了找那一行再把整张表读一遍，用手里那份清单（没有才读）。
        只给**拿着租约、独占这几个键**的调用方用（试跑存账本：一轮要存上百次）。
        写完不扔缓存，只把这一行补进去。日志里不打值。
        """
        if len(text) > CELL_MAX_CHARS:
            raise ValueError(f"「面板设置」{key} 有 {len(text)} 字，超过飞书单个单元格 "
                             f"{CELL_MAX_CHARS} 字的上限，没有写入")
        with self._lock:
            table_id = self._ensure_table()
            rows = self._rows if (not fresh and self._rows is not None) \
                else self._load(force=True)
            existing = rows.get(key)
            table = self._bitable(self.app_token, table_id)
            try:
                if existing and existing[0]:
                    table.batch_update([{"record_id": existing[0],
                                         "fields": {COL_KEY: key, COL_VALUE: text}}])
                    record_id = existing[0]
                else:
                    created = table.batch_create(
                        [{"fields": {COL_KEY: key, COL_VALUE: text}}],
                        client_token=feishu.idempotency_key(
                            f"panel-setting:{self.app_token}:{key}:{text}"))
                    record_id = created[0] if created else ""
            except Exception:
                # 报了错不等于没写进去。手里的清单不可信了：下次重读，免得给同一个键再建一行。
                self._rows = None
                raise
            if self._rows is not None and record_id:
                self._rows[key] = (record_id, text)
            else:
                self._rows = None       # 没拿到行号：下次老老实实重读

    def set_json(self, key: str, value: Any) -> None:
        """写一个键（整体覆盖）。没有那张表就先建。写完缓存失效。
        找行和写行在同一把锁里，两个人同时保存也只会有一行。"""
        text = json.dumps(value, ensure_ascii=False)
        with self._lock:
            table_id = self._ensure_table()
            # 强制重读：别拿缓存里的「没有这一行」去决定 create——另一个线程
            # 刚建完的那一行缓存里还没有。
            self._put(table_id, key, text, self._load(force=True).get(key))
        self.log(f"⚙ 面板设置 {key} = {text}")

    def update_json(self, key: str, change: Callable[[Any], Any], default: Any) -> Any:
        """读-改-写一个键，**整段在锁里**：两个人同时点，后一个改的是前一个
        写完的那份，不会互相覆盖。`change` 拿到当前值（没有 / 坏了 → default），
        返回要写的新值。返回写进去的值。

        和 set_json 不同，日志里不打整段值——这种键（「已解决」）会越攒越长。
        """
        with self._lock:
            table_id = self._ensure_table()
            existing = self._load(force=True).get(key)
            current = default
            if existing and existing[1].strip():
                try:
                    current = json.loads(existing[1])
                except ValueError:
                    current = default
            value = change(current)
            text = json.dumps(value, ensure_ascii=False)
            if len(text) > CELL_MAX_CHARS:
                raise ValueError(f"「面板设置」{key} 有 {len(text)} 字，超过飞书单个单元格 "
                                 f"{CELL_MAX_CHARS} 字的上限，没有写入")
            self._put(table_id, key, text, existing)
        self.log(f"⚙ 面板设置 {key} 已更新（{len(text)} 字）")
        return value

    def _put(self, table_id: str, key: str, text: str,
             existing: Optional[tuple[str, str]]) -> None:
        """有那一行就改，没有就建。调用方持锁。"""
        table = self._bitable(self.app_token, table_id)
        if existing and existing[0]:
            table.batch_update([{"record_id": existing[0],
                                 "fields": {COL_KEY: key, COL_VALUE: text}}])
        else:
            # 幂等键按 键+值 算：同一个值重发（超时重试）不会多出一行。
            table.batch_create(
                [{"fields": {COL_KEY: key, COL_VALUE: text}}],
                client_token=feishu.idempotency_key(
                    f"panel-setting:{self.app_token}:{key}:{text}"))
        self._rows = None
