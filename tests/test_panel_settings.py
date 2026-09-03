"""面板设置：存在注册表 base 里的一张键值表。全部离线。"""

import json
import unittest
from unittest import mock

from xhsearch import feishu, panel_settings


class FakeBitable:
    """只实现 SettingsStore 会调的三个方法。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])       # [{"record_id", "fields": {键, 值}}]
        self.created = []
        self.updated = []

    def search(self, field_names, **kwargs):
        return list(self.rows)

    def batch_create(self, records, *, client_token):
        self.created.append((records, client_token))
        ids = []
        for i, record in enumerate(records):
            rid = f"rec{len(self.rows) + 1}"
            self.rows.append({"record_id": rid, "fields": dict(record["fields"])})
            ids.append(rid)
        return ids

    def batch_update(self, updates, **kwargs):
        self.updated.extend(updates)
        for update in updates:
            for row in self.rows:
                if row["record_id"] == update["record_id"]:
                    row["fields"].update(update["fields"])
        return len(updates)


def row(record_id, key, value):
    return {"record_id": record_id,
            "fields": {panel_settings.COL_KEY: key, panel_settings.COL_VALUE: value}}


class TestSettingsStore(unittest.TestCase):
    def _store(self, tables, bitable, clock=None):
        workspace = mock.Mock()
        workspace.list_tables.return_value = tables
        workspace.create_table.return_value = "tblSET"
        made = []
        def factory(app_token, table_id):
            made.append((app_token, table_id))
            return bitable
        ticks = [0.0]
        store = panel_settings.SettingsStore(
            workspace, factory, "bascnREG", log=lambda *a: None,
            clock=clock or (lambda: ticks[0]))
        return store, workspace, made, ticks

    def test_missing_table_reads_as_empty_without_creating_it(self):
        """只读的时候不建表——面板只是打开看看，不该在别人的 base 里留东西。"""
        store, workspace, made, _ = self._store([{"table_id": "tblREG", "name": "被监控的表"}],
                                                FakeBitable())
        self.assertEqual(store.get_json("some.key", []), [])
        workspace.create_table.assert_not_called()
        self.assertEqual(made, [])

    def test_first_write_creates_the_table_next_to_the_registry(self):
        bitable = FakeBitable()
        store, workspace, made, _ = self._store([{"table_id": "tblREG", "name": "被监控的表"}],
                                                bitable)
        store.set_json(panel_settings.KEY_EDITOR_CHATS, [{"chat_id": "oc_1", "name": "运营群"}])
        workspace.create_table.assert_called_once_with(
            "bascnREG", panel_settings.TABLE_NAME, panel_settings.FIELDS)
        self.assertEqual(made[0], ("bascnREG", "tblSET"))
        records, client_token = bitable.created[0]
        self.assertEqual(records[0]["fields"][panel_settings.COL_KEY], panel_settings.KEY_EDITOR_CHATS)
        self.assertEqual(json.loads(records[0]["fields"][panel_settings.COL_VALUE]),
                         [{"chat_id": "oc_1", "name": "运营群"}])
        # 幂等键是规范 UUID（飞书只吃这个）
        self.assertEqual(len(client_token), 36)

    def test_an_existing_table_is_reused_by_name(self):
        bitable = FakeBitable([row("rec1", "some.key",
                                   json.dumps([{"id": "ou_a", "label": "138"}]))])
        store, workspace, made, _ = self._store(
            [{"table_id": "tblREG", "name": "被监控的表"},
             {"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}], bitable)
        self.assertEqual(store.get_json("some.key", []),
                         [{"id": "ou_a", "label": "138"}])
        workspace.create_table.assert_not_called()
        self.assertEqual(made[0], ("bascnREG", "tblOLD"))

    def test_rewriting_a_key_updates_the_same_row(self):
        bitable = FakeBitable([row("rec1", "some.key", "[]")])
        store, _w, _m, _ = self._store([{"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}],
                                       bitable)
        store.set_json("some.key", [{"id": "ou_b", "label": "b@x"}])
        self.assertEqual(bitable.created, [])
        self.assertEqual(bitable.updated[0]["record_id"], "rec1")
        self.assertEqual(json.loads(bitable.updated[0]["fields"][panel_settings.COL_VALUE]),
                         [{"id": "ou_b", "label": "b@x"}])
        # 写完立刻读到新值（缓存失效）
        self.assertEqual(store.get_json("some.key", []),
                         [{"id": "ou_b", "label": "b@x"}])

    def test_reads_are_cached_and_expire(self):
        bitable = FakeBitable([row("rec1", "some.key", "[]")])
        calls = []
        real_search = bitable.search
        bitable.search = lambda *a, **k: (calls.append(1), real_search(*a, **k))[1]
        store, _w, _m, ticks = self._store([{"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}],
                                           bitable)
        store.get_json("some.key", [])
        store.get_json("some.key", [])
        self.assertEqual(len(calls), 1)
        ticks[0] = panel_settings.CACHE_SECONDS + 1
        store.get_json("some.key", [])
        self.assertEqual(len(calls), 2)

    def test_garbage_in_the_cell_falls_back_to_the_default(self):
        """人手改坏了那一格，面板不该整栏炸掉。"""
        bitable = FakeBitable([row("rec1", "some.key", "not json")])
        store, *_ = self._store([{"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}], bitable)
        self.assertEqual(store.get_json("some.key", []), [])

    def test_a_failed_table_creation_raises(self):
        store, workspace, *_ = self._store([], FakeBitable())
        workspace.create_table.return_value = ""
        with self.assertRaises(feishu.FeishuError):
            store.set_json("some.key", [])


if __name__ == "__main__":
    unittest.main()
