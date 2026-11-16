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

    def test_a_value_over_the_feishu_cell_limit_is_refused_locally(self):
        """飞书单个文本单元格最多 10 万字；超了在本地就拦下，说清楚，不写。"""
        bitable = FakeBitable([row("rec1", panel_settings.KEY_RESOLVED, "{}")])
        store, *_ = self._store([{"table_id": "tblSET", "name": panel_settings.TABLE_NAME}],
                                bitable)
        with self.assertRaisesRegex(ValueError, "超过飞书单个单元格"):
            store.update_json(panel_settings.KEY_RESOLVED,
                              lambda current: {"x": "长" * panel_settings.CELL_MAX_CHARS}, {})
        self.assertEqual(bitable.updated, [])

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

    def test_two_concurrent_first_saves_leave_exactly_one_row(self):
        """两个运营同时第一次保存：找行 → 建行不是原子的，没有锁就会同一个键
        两行，之后读到哪一行看飞书心情。"""
        import threading
        import time as time_mod

        bitable = FakeBitable()
        real_search = bitable.search
        def slow_search(*a, **k):
            time_mod.sleep(0.02)          # 放大「看到没有这一行」到「建行」之间的窗口
            return real_search(*a, **k)
        bitable.search = slow_search
        store, *_ = self._store([{"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}],
                                bitable)
        threads = [threading.Thread(target=store.set_json, args=("some.key", [i]))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(bitable.created), 1, "同一个键建了两行")
        self.assertEqual(len(bitable.updated), 1, "第二次保存应该是改那一行")
        keys = [r["fields"][panel_settings.COL_KEY] for r in bitable.rows]
        self.assertEqual(keys, ["some.key"])

    def test_a_failed_table_creation_raises(self):
        store, workspace, *_ = self._store([], FakeBitable())
        workspace.create_table.return_value = ""
        with self.assertRaises(feishu.FeishuError):
            store.set_json("some.key", [])


class TestUpdateJson(unittest.TestCase):
    """读-改-写：「已解决」两个人同时点，不能互相覆盖。"""

    TABLES = [{"table_id": "tblOLD", "name": panel_settings.TABLE_NAME}]

    def _store(self, bitable, log=None):
        workspace = mock.Mock()
        workspace.list_tables.return_value = self.TABLES
        return panel_settings.SettingsStore(
            workspace, lambda a, t: bitable, "bascnREG",
            log=log or (lambda *a: None), clock=lambda: 0.0)

    def test_missing_key_starts_from_the_default_and_creates_the_row(self):
        bitable = FakeBitable()
        store = self._store(bitable)
        out = store.update_json("todo.resolved", lambda cur: {**cur, "a": 1}, {})
        self.assertEqual(out, {"a": 1})
        self.assertEqual(len(bitable.created), 1)
        self.assertEqual(store.get_json("todo.resolved", {}), {"a": 1})

    def test_it_changes_what_is_stored_now_not_a_stale_cache(self):
        bitable = FakeBitable([row("rec1", "todo.resolved", '{"a": 1}')])
        store = self._store(bitable)
        self.assertEqual(store.get_json("todo.resolved", {}), {"a": 1})   # 进了缓存
        # 另一处（人在飞书里手改）把值改了；缓存还没过期
        bitable.rows[0]["fields"][panel_settings.COL_VALUE] = '{"a": 1, "b": 2}'
        out = store.update_json("todo.resolved", lambda cur: {**cur, "c": 3}, {})
        self.assertEqual(out, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(bitable.updated[0]["record_id"], "rec1")
        self.assertEqual(bitable.created, [])

    def test_a_broken_value_is_replaced_from_the_default(self):
        bitable = FakeBitable([row("rec1", "todo.resolved", "{not json")])
        out = self._store(bitable).update_json("todo.resolved", lambda cur: {**cur, "x": 1}, {})
        self.assertEqual(out, {"x": 1})

    def test_two_concurrent_updates_both_land(self):
        import threading
        import time as time_mod

        bitable = FakeBitable([row("rec1", "todo.resolved", "{}")])
        real_search = bitable.search
        def slow_search(*a, **k):
            time_mod.sleep(0.02)          # 放大「读到」和「写回」之间的窗口
            return real_search(*a, **k)
        bitable.search = slow_search
        store = self._store(bitable)
        threads = [threading.Thread(target=store.update_json,
                                    args=("todo.resolved", (lambda k: lambda cur: {**cur, k: 1})(f"k{i}"), {}))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(json.loads(bitable.rows[0]["fields"][panel_settings.COL_VALUE]),
                         {"k0": 1, "k1": 1}, "后一个人的改动把前一个盖掉了")

    def test_the_log_does_not_dump_the_whole_value(self):
        lines = []
        store = self._store(FakeBitable(), log=lines.append)
        store.update_json("todo.resolved", lambda cur: {"tblA/rec1": {"reasons": ["有负面"]}}, {})
        self.assertEqual(len(lines), 1)
        self.assertIn("todo.resolved", lines[0])
        self.assertNotIn("rec1", lines[0])


class TestRawText(unittest.TestCase):
    """试跑的账本用的那三个口子：原样读一批、只读一行、原样写一行（见 trial.py）。"""

    def _store(self, bitable):
        workspace = mock.Mock()
        workspace.list_tables.return_value = [{"table_id": "tblSET",
                                               "name": panel_settings.TABLE_NAME}]
        return panel_settings.SettingsStore(workspace, lambda _a, _t: bitable, "bascnREG",
                                            log=lambda *a: None, clock=lambda: 0.0)

    def test_read_texts_returns_raw_values_by_prefix_and_names_duplicates(self):
        bitable = FakeBitable([row("rec1", "a.one", "不是 JSON"), row("rec2", "a.two", "x"),
                               row("rec3", "b.one", "y"), row("rec4", "a.two", "z")])
        texts, dupes = self._store(bitable).read_texts("a.")
        self.assertEqual(texts, {"a.one": "不是 JSON", "a.two": "z"})
        self.assertEqual(dupes, {"a.two"})

    def test_read_texts_always_reads_the_table_again(self):
        bitable = FakeBitable([row("rec1", "a.one", "1")])
        store = self._store(bitable)
        store.read_texts("a.")
        bitable.rows[0]["fields"][panel_settings.COL_VALUE] = "2"     # 另一个进程改了
        self.assertEqual(store.read_texts("a.")[0], {"a.one": "2"})

    def test_read_one_picks_the_exact_key_even_if_the_filter_is_ignored(self):
        bitable = FakeBitable([row("rec1", "a.one", "1"), row("rec2", "a.one.more", "2")])
        store = self._store(bitable)
        self.assertEqual(store.read_one("a.one"), "1")
        self.assertIsNone(store.read_one("a.missing"))

    def test_read_one_falls_back_to_a_full_read_when_the_filter_is_refused(self):
        bitable = FakeBitable([row("rec1", "a.one", "1")])
        plain = bitable.search

        def picky(field_names, **kwargs):
            if kwargs.get("filter_spec"):
                raise feishu.FeishuError(-1, "筛选条件不认")
            return plain(field_names)
        bitable.search = picky
        self.assertEqual(self._store(bitable).read_one("a.one"), "1")

    def test_read_prefix_keeps_only_keys_that_start_with_it(self):
        """筛选是「包含」，飞书不认时退回整表读：两条路都只留真以它开头的键，读的是此刻的值。"""
        bitable = FakeBitable([row("rec1", "a.x.one", "1"), row("rec2", "b.a.x.two", "2"),
                               row("rec3", "a.x.three", "3")])
        store = self._store(bitable)
        self.assertEqual(store.read_prefix("a.x."), {"a.x.one": "1", "a.x.three": "3"})
        bitable.rows[0]["fields"][panel_settings.COL_VALUE] = "9"     # 另一个进程改了
        plain = bitable.search

        def picky(field_names, **kwargs):
            if kwargs.get("filter_spec"):
                raise feishu.FeishuError(-1, "筛选条件不认")
            return plain(field_names)
        bitable.search = picky
        self.assertEqual(store.read_prefix("a.x."), {"a.x.one": "9", "a.x.three": "3"})

    def test_write_text_refuses_a_value_over_the_cell_limit(self):
        bitable = FakeBitable()
        with self.assertRaisesRegex(ValueError, "超过飞书单个单元格"):
            self._store(bitable).write_text("a.one", "x" * (panel_settings.CELL_MAX_CHARS + 1))
        self.assertEqual((bitable.created, bitable.updated), ([], []))

    def test_write_text_without_a_fresh_read_reuses_the_row_it_made(self):
        bitable = FakeBitable()
        reads = []
        plain = bitable.search
        bitable.search = lambda field_names, **kw: (reads.append(1), plain(field_names))[1]
        store = self._store(bitable)
        store.write_text("a.one", "1", fresh=False)
        store.write_text("a.one", "2", fresh=False)
        store.write_text("a.two", "3", fresh=False)
        self.assertEqual(len(reads), 1)                       # 只在一开始读了一遍
        self.assertEqual(len(bitable.created), 2)             # 两个键两行，没有重复的行
        self.assertEqual(bitable.updated[0]["record_id"], "rec1")
        self.assertEqual(store.read_texts("a.")[0], {"a.one": "2", "a.two": "3"})


if __name__ == "__main__":
    unittest.main()
