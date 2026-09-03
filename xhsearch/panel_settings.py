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

两列：`键` / `值`。值一律是 JSON 文本。现在只有两个键：

* `share.managers`      → `[{"id": "ou_…", "label": "138****8888"}, …]`
* `share.editor_chats`  → `[{"chat_id": "oc_…", "name": "梨响运营群"}, …]`

按**名字**认表（「面板设置」）：没建过就建，建过就复用——不用再多一个环境变量。
读结果在进程里缓存几分钟；写完立刻失效。面板只有一个进程，够了。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from . import feishu

TABLE_NAME = "面板设置"
COL_KEY = "键"
COL_VALUE = "值"
FIELDS = [{"field_name": COL_KEY, "type": 1}, {"field_name": COL_VALUE, "type": 1}]

KEY_MANAGERS = "share.managers"
KEY_EDITOR_CHATS = "share.editor_chats"

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
        self._read_at: float = 0.0

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
        if table_id:
            table = self._bitable(self.app_token, table_id)
            for record in table.search([COL_KEY, COL_VALUE]):
                cells = record.get("fields") or {}
                key = feishu.read_text(cells.get(COL_KEY)).strip()
                if key:
                    rows[key] = (record.get("record_id") or "",
                                 feishu.read_text(cells.get(COL_VALUE)))
        self._rows, self._read_at = rows, self._clock()
        return rows

    def get_json(self, key: str, default: Any) -> Any:
        """读一个键，JSON 解开。没有、或存的不是合法 JSON → default。"""
        row = self._load().get(key)
        if row is None or not row[1].strip():
            return default
        try:
            return json.loads(row[1])
        except ValueError:
            return default

    # ---------- 写 ----------

    def set_json(self, key: str, value: Any) -> None:
        """写一个键（整体覆盖）。没有那张表就先建。写完缓存失效。"""
        table_id = self._ensure_table()
        table = self._bitable(self.app_token, table_id)
        text = json.dumps(value, ensure_ascii=False)
        existing = self._load().get(key)
        if existing and existing[0]:
            table.batch_update([{"record_id": existing[0],
                                 "fields": {COL_KEY: key, COL_VALUE: text}}])
        else:
            # 幂等键按 键+值 算：同一个值重发（超时重试）不会多出一行。
            table.batch_create(
                [{"fields": {COL_KEY: key, COL_VALUE: text}}],
                client_token=feishu.idempotency_key(f"panel-setting:{self.app_token}:{key}:{text}"))
        self._rows = None
        self.log(f"⚙ 面板设置 {key} = {text}")
