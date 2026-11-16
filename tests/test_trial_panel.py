"""搜索可见试跑 · 面板上点着跑的那一半：账本存在登记表 base 里、租约、面板那一栏的按钮。

全部离线。飞书是内存里的替身（一张「面板设置」表 + 几张业务表），发请求的那一下是
`test_search_trial.Sender`。词、项目名都是占位的（「占位词甲」「甲表」）。
"""

import io
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import cli
from tests.test_panel import GOOD_PASSWORD
from tests.test_search_trial import (KEY, Sender, TrialTable, by_page, nid, ok, page_of,
                                     trial_record)
from xhsearch import (feishu, panel, panel_settings, panel_view, registry, summary, transport,
                      trial, visibility)
from xhsearch.config import Settings

WORD_A, WORD_B = "占位词甲", "占位词乙"


class SettingsTable:
    """「面板设置」那张表的替身：记下每一次写了什么，可以让某一次写失败。"""

    def __init__(self):
        self.rows: list[dict] = []
        self.writes: list[tuple[str, str]] = []      # (键, 值)，按写的先后
        self.fail_on = None                          # (键, 值) -> bool：这一次写要不要炸
        self.searches = 0

    def search(self, field_names, **_kwargs):
        self.searches += 1
        return [{"record_id": r["record_id"], "fields": dict(r["fields"])} for r in self.rows]

    def _check(self, key, value):
        if self.fail_on is not None and self.fail_on(key, value):
            raise feishu.FeishuError(-1, "写不进去（测试造的）")
        self.writes.append((key, value))

    def batch_create(self, records, *, client_token):
        ids = []
        for record in records:
            fields = dict(record["fields"])
            self._check(fields[panel_settings.COL_KEY], fields[panel_settings.COL_VALUE])
            rid = f"rec{len(self.rows) + 1}"
            self.rows.append({"record_id": rid, "fields": fields})
            ids.append(rid)
        return ids

    def batch_update(self, updates, **_kwargs):
        for update in updates:
            fields = update["fields"]
            self._check(fields[panel_settings.COL_KEY], fields[panel_settings.COL_VALUE])
            for row in self.rows:
                if row["record_id"] == update["record_id"]:
                    row["fields"].update(fields)
        return len(updates)

    def value(self, key):
        found = [r["fields"][panel_settings.COL_VALUE] for r in self.rows
                 if r["fields"][panel_settings.COL_KEY] == key]
        return found[-1] if found else None

    def keys(self):
        return sorted(r["fields"][panel_settings.COL_KEY] for r in self.rows)


def settings_store(table: SettingsTable) -> panel_settings.SettingsStore:
    """一个「进程」眼里的面板设置表。同一张 `table` 上建两个 = 两个容器看同一张飞书表。"""
    workspace = mock.Mock()
    workspace.list_tables.return_value = [{"table_id": "tblSET",
                                           "name": panel_settings.TABLE_NAME}]
    return panel_settings.SettingsStore(workspace, lambda _app, _table: table, "bascnREG",
                                        log=lambda *_a: None)


class Clock:
    """停住的钟（停在造它的那一刻——表里帖子的发布时间是按真的现在算的）。
    `clock.at += 秒` 往后拨。"""

    def __init__(self, at=None):
        self.at = time.time() if at is None else at

    def __call__(self):
        return self.at


def trial_store(table: SettingsTable, clock=None) -> trial.TrialStore:
    return trial.TrialStore(settings_store(table), clock=clock or Clock(),
                            sleep=lambda _s: None, log=lambda *_a: None)


def lease_rows(table: SettingsTable) -> list[dict]:
    """租约那几行（一个进程一行）现在的内容。"""
    return [json.loads(r["fields"][panel_settings.COL_VALUE]) for r in table.rows
            if r["fields"][panel_settings.COL_KEY].startswith(trial.KEY_LEASE)]


def big_state(state: visibility.State, pages: int = 60) -> None:
    """往账本里塞够多的页，让它比一个单元格大。只有 ID 和计数。"""
    digest = visibility.keyword_hash(WORD_A)
    state.data["keywords"][digest] = 1
    for page in range(1, pages + 1):
        state.data["pages"][f"first|{digest}|{page}"] = {
            "at": 1, "ids": [nid(page * 1000 + i) for i in range(80)], "ads": [],
            "items": 80, "other": 0, "types": {"note": 80}, "keys": ["note"], "sid": {"s": "1"}}


# ---------- 账本切段存、清单最后写 ----------


class TestStoredState(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()
        self.store = trial_store(self.table)

    def test_nothing_stored_is_an_empty_ledger(self):
        state = self.store.load_state()
        self.assertEqual((state.calls, state.yuan, state.data["pages"]), (0, 0.0, {}))
        self.assertEqual(self.table.writes, [])            # 读不会顺手建出什么来

    def test_a_ledger_round_trips_through_the_table(self):
        state = self.store.load_state()
        state.charge(0.072)
        state.register([WORD_A])
        state.data["pages"]["first|x|1"] = {"at": 1, "ids": ["a b"], "types": {"类型 甲": 1}}
        state.save()
        again = trial_store(self.table).load_state()
        self.assertEqual(again.data, state.data)
        self.assertEqual(again.calls, 1)

    def test_what_is_stored_is_ascii_without_spaces(self):
        """飞书怎么数字数、掐不掐一段两头的空白都不影响它。"""
        state = self.store.load_state()
        state.data["pages"]["first|x|1"] = {"at": 1, "ids": ["a b"], "types": {"类型 甲": 1}}
        state.save()
        for key, value in self.table.writes:
            if key.startswith(trial.KEY_STATE_PREFIX):
                self.assertTrue(value.isascii())
                self.assertNotIn(" ", value)

    def test_a_big_ledger_is_split_under_the_cell_limit(self):
        state = self.store.load_state()
        big_state(state)
        state.save()
        chunks = [(k, v) for k, v in self.table.writes if k.startswith(trial.KEY_STATE_PREFIX)]
        self.assertGreater(len(chunks), 1)
        for _key, value in chunks:
            self.assertLessEqual(len(value), trial.CHUNK_CHARS)
            self.assertLess(len(value), panel_settings.CELL_MAX_CHARS)
        self.assertEqual(trial_store(self.table).load_state().data, state.data)

    def test_the_manifest_is_written_last(self):
        state = self.store.load_state()
        big_state(state)
        state.save()
        self.assertEqual(self.table.writes[-1][0], trial.KEY_MANIFEST)
        self.assertEqual([k for k, _v in self.table.writes].count(trial.KEY_MANIFEST), 1)

    def test_a_save_cut_short_leaves_the_previous_ledger_readable(self):
        """写到一半断了：清单还指着上一份完整的。上一份是对的——请求是存好之后才发的。"""
        state = self.store.load_state()
        big_state(state)
        state.charge(0.072)
        state.save()
        before = json.loads(json.dumps(state.data))
        state.charge(0.072)
        self.table.fail_on = lambda key, _value: key == trial.KEY_MANIFEST
        with self.assertRaises(visibility.StateError):
            state.save()
        self.table.fail_on = None
        again = trial_store(self.table).load_state()
        self.assertEqual(again.data, before)
        self.assertEqual(again.calls, 1)

    def test_chunks_without_a_manifest_are_refused_not_treated_as_empty(self):
        """第一次存就死在清单之前（进程没了），或者清单那一行被人删了：都不能当成没花过钱。"""
        state = self.store.load_state()
        state.charge(0.072)
        wrote = []

        def dies_after_the_first_chunk(key, _value):
            wrote.append(key)
            return len(wrote) > 1                     # 第一段写进去了，之后什么都写不了
        self.table.fail_on = dies_after_the_first_chunk
        with self.assertRaises(visibility.StateError):
            state.save()
        self.table.fail_on = None
        with self.assertRaisesRegex(visibility.StateError, "只存了一半"):
            trial_store(self.table).load_state()

    def test_a_deleted_manifest_is_refused(self):
        state = self.store.load_state()
        state.charge(0.072)
        state.save()
        self.table.rows = [r for r in self.table.rows
                           if r["fields"][panel_settings.COL_KEY] != trial.KEY_MANIFEST]
        with self.assertRaisesRegex(visibility.StateError, "只存了一半"):
            trial_store(self.table).load_state()

    def test_a_first_save_that_fails_at_the_manifest_is_rolled_back(self):
        """头一次存、只有清单没写成：没有任何请求是凭这份账本发的，把刚写的段擦掉，
        退回「没存过」——不然负责人会卡在「存了一半」上，要人去表里删。"""
        state = self.store.load_state()
        state.register([WORD_A])
        self.table.fail_on = lambda key, _value: key == trial.KEY_MANIFEST
        with self.assertRaises(visibility.StateError):
            state.save()
        self.table.fail_on = None
        again = trial_store(self.table).load_state()
        self.assertEqual((again.calls, again.data["keywords"]), (0, {}))
        state.save()                                   # 飞书好了之后同一份还能存
        self.assertEqual(len(trial_store(self.table).load_state().data["keywords"]), 1)

    def test_a_write_that_landed_but_reported_an_error_counts_as_saved(self):
        """清单其实写进去了，只是回答丢在路上：再读一遍看到它，就当存好了，不去擦段。"""
        state = self.store.load_state()
        state.charge(0.072)
        plain = self.table.batch_create

        def lands_then_times_out(records, *, client_token):
            ids = plain(records, client_token=client_token)
            if records[0]["fields"][panel_settings.COL_KEY] == trial.KEY_MANIFEST:
                raise feishu.FeishuError(-1, "超时（测试造的）")
            return ids
        self.table.batch_create = lands_then_times_out
        state.save()
        self.table.batch_create = plain
        self.assertEqual(trial_store(self.table).load_state().calls, 1)
        state.charge(0.072)
        state.save()                                   # 接着存也对得上版本号
        self.assertEqual(trial_store(self.table).load_state().calls, 2)

    def test_a_tampered_chunk_is_refused(self):
        state = self.store.load_state()
        state.charge(0.072)
        state.save()
        for row in self.table.rows:
            if row["fields"][panel_settings.COL_KEY].startswith(trial.KEY_STATE_PREFIX):
                row["fields"][panel_settings.COL_VALUE] = \
                    row["fields"][panel_settings.COL_VALUE].replace('"calls":1', '"calls":0')
        with self.assertRaisesRegex(visibility.StateError, "读不懂"):
            trial_store(self.table).load_state()

    def test_a_missing_chunk_is_refused(self):
        state = self.store.load_state()
        big_state(state)
        state.save()
        self.table.rows = [r for r in self.table.rows
                           if not r["fields"][panel_settings.COL_KEY].endswith(".01")]
        with self.assertRaisesRegex(visibility.StateError, "读不懂"):
            trial_store(self.table).load_state()

    def test_duplicate_manifest_rows_are_refused(self):
        state = self.store.load_state()
        state.save()
        manifest = next(r for r in self.table.rows
                        if r["fields"][panel_settings.COL_KEY] == trial.KEY_MANIFEST)
        self.table.rows.append({"record_id": "recDUP", "fields": dict(manifest["fields"])})
        with self.assertRaisesRegex(visibility.StateError, "重复的行"):
            trial_store(self.table).load_state()

    def test_an_unreadable_table_is_an_error_not_an_empty_ledger(self):
        def explode(*_a, **_k):
            raise feishu.FeishuError(-1, "读不到")
        self.table.search = explode
        with self.assertRaisesRegex(visibility.StateError, "读不到试跑的账本"):
            self.store.load_state()

    def test_unchanged_chunks_are_not_rewritten(self):
        """页只往后加，前面的段和上一次一样：平常存一次是写最后一段 + 清单。"""
        state = self.store.load_state()
        big_state(state)
        state.save()
        state.save()                                  # 两个槽都写满一遍
        self.table.writes.clear()
        state.charge(0.072)
        state.save()
        keys = [k for k, _v in self.table.writes]
        self.assertEqual(keys[-1], trial.KEY_MANIFEST)
        self.assertEqual(len(keys), 2)                # 最后一段 + 清单
        self.assertEqual(trial_store(self.table).load_state().calls, 1)

    def test_a_stale_copy_cannot_overwrite_a_newer_ledger(self):
        """两边都读了同一份（租约被接手之后的老那一方）：后存的那个发现清单的版本变了，不写。"""
        mine = self.store.load_state()
        theirs = trial_store(self.table).load_state()
        theirs.charge(0.072)
        theirs.charge(0.072)
        theirs.save()
        mine.charge(0.072)
        with self.assertRaisesRegex(visibility.StateError, "被别处改过"):
            mine.save()
        self.assertEqual(trial_store(self.table).load_state().calls, 2)

    def test_two_live_copies_cannot_silently_overwrite_each_other(self):
        """两边从同一份读起。乙的一次存正好落在甲「对过清单」和「开始写」之间：甲把乙盖掉了。
        乙下一次存必须发现（两边的版本号是同一个数，只比版本号发现不了），而且表里
        不能给同一个键留下两行——有重复的行，整本账就再也读不了了。"""
        first = self.store.load_state()
        first.charge(0.072)
        first.save()                                   # 表里先有一份
        mine = trial_store(self.table).load_state()
        theirs = trial_store(self.table).load_state()
        theirs.charge(0.072)
        plain = mine._store.settings_store.read_one
        slipped: list = []

        def they_save_right_after_my_check(key):
            found = plain(key)
            if key == trial.KEY_MANIFEST and not slipped:
                slipped.append(True)
                theirs.save()
            return found
        mine._store.settings_store.read_one = they_save_right_after_my_check
        mine.charge(0.072)
        mine.save()
        theirs.charge(0.072)
        with self.assertRaisesRegex(visibility.StateError, "被别处改过"):
            theirs.save()
        self.assertTrue(theirs.superseded)
        keys = self.table.keys()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(trial_store(self.table).load_state().calls, 2)

    def test_a_save_overwritten_at_once_is_not_treated_as_saved(self):
        """甲刚写完清单，乙的清单就盖了上来：甲存完再读一遍，不是自己那一份，这一次不发。"""
        state = self.store.load_state()
        state.charge(0.072)
        state.save()
        plain = self.table.batch_update

        def overwritten_at_once(updates, **kwargs):
            done = plain(updates, **kwargs)
            for row in self.table.rows:
                fields = row["fields"]
                if fields[panel_settings.COL_KEY] == trial.KEY_MANIFEST \
                        and updates[0]["fields"][panel_settings.COL_KEY] == trial.KEY_MANIFEST:
                    other = json.loads(fields[panel_settings.COL_VALUE])
                    other["id"] = "someone-else"
                    fields[panel_settings.COL_VALUE] = json.dumps(other)
            return done
        self.table.batch_update = overwritten_at_once
        state.charge(0.072)
        with self.assertRaisesRegex(visibility.StateError, "被别处盖掉"):
            state.save()
        self.assertTrue(state.superseded)

    def test_a_copy_whose_lease_is_lost_does_not_write(self):
        clock = Clock()
        store = trial_store(self.table, clock)
        lease = store.acquire("命令行", "first")
        state = store.load_state(lease)
        state.charge(0.072)
        state.save()
        clock.at += trial.LEASE_SECONDS + 1
        self.assertNotEqual(lease.check(), "")
        self.table.writes.clear()
        state.charge(0.072)
        with self.assertRaises(visibility.StateError):
            state.save()
        self.assertEqual(self.table.writes, [])
        self.assertEqual(trial_store(self.table).load_state().calls, 1)

    def test_another_trials_ledger_is_refused(self):
        state = self.store.load_state()
        state.data["trial"] = "别的试跑"
        state.save()
        with self.assertRaisesRegex(visibility.StateError, "不是这次试跑的"):
            trial_store(self.table).load_state()


# ---------- 租约 ----------


class TestLease(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()
        self.clock = Clock()
        self.panel_side = trial_store(self.table, self.clock)
        self.cli_side = trial_store(self.table, self.clock)     # 另一个容器

    def test_a_second_holder_is_refused_across_processes(self):
        self.panel_side.acquire("面板", "first")
        with self.assertRaisesRegex(trial.Busy, "面板正在跑「第一轮」"):
            self.cli_side.acquire("命令行", "first")

    def test_a_released_lease_can_be_taken_at_once(self):
        self.panel_side.acquire("面板", "first").release()
        self.cli_side.acquire("命令行", "first")

    def test_an_expired_lease_is_taken_over_and_the_old_holder_stops(self):
        old = self.panel_side.acquire("面板", "first")
        self.assertEqual(old.check(), "")
        self.clock.at += trial.LEASE_SECONDS - 1
        with self.assertRaises(trial.Busy):
            self.cli_side.acquire("命令行", "first")           # 还没过期
        self.clock.at += 2
        new = self.cli_side.acquire("命令行", "first")
        self.assertEqual(new.check(), "")
        self.assertIn("另一个试跑接手了", old.check())

    def test_checking_renews_a_lease_that_is_half_used(self):
        lease = self.panel_side.acquire("面板", "first")
        self.clock.at += trial.LEASE_SECONDS * 0.6
        self.assertEqual(lease.check(), "")
        self.clock.at += trial.LEASE_SECONDS * 0.6           # 没续的话这时已经过期了
        with self.assertRaises(trial.Busy):
            self.cli_side.acquire("命令行", "first")

    def test_losing_the_race_at_the_settle_check_is_busy(self):
        """两边同时看到没人拿、同时写上自己：再读一遍时看到对方那一行，让开。"""
        def someone_else_wrote(_seconds):
            self.table.rows.append({"record_id": "recOTHER", "fields": {
                panel_settings.COL_KEY: trial.KEY_LEASE + "other",
                panel_settings.COL_VALUE: json.dumps(
                    {"owner": "别人", "who": "命令行", "round": "first",
                     "until": self.clock.at + 600})}})
        racing = trial.TrialStore(settings_store(self.table), clock=self.clock,
                                  sleep=someone_else_wrote, log=lambda *_a: None)
        with self.assertRaisesRegex(trial.Busy, "同时起跑"):
            racing.acquire("面板", "first")
        mine = [row for row in lease_rows(self.table) if row["owner"] != "别人"]
        self.assertTrue(mine[0]["released"])                 # 让开的那一边把自己那一行放掉

    def test_a_contender_whose_write_lands_after_the_winner_settled_is_refused(self):
        """乙读的时候还没人，甲随后完整地拿到了租约（等过、再读过），乙的那一下写才落地：
        乙再读时看得到甲那一行，让开。一行租约、后写的盖掉先写的话，这里两边都会拿到。"""
        plain = self.cli_side.settings_store.read_prefix
        held: list = []

        def slow_first_read(prefix):
            found = plain(prefix)
            if not held:
                held.append(self.panel_side.acquire("面板", "first"))
            return found
        self.cli_side.settings_store.read_prefix = slow_first_read
        with self.assertRaises(trial.Busy):
            self.cli_side.acquire("命令行", "first")
        self.assertEqual(held[0].check(), "")                # 甲没受影响
        with self.assertRaises(trial.Busy):                  # 第三个也进不来
            trial_store(self.table, self.clock).acquire("面板", "first")

    def test_a_holder_that_lost_its_lease_releases_only_its_own_row(self):
        """被接手的那一方收尾时放掉的是自己那一行：接手的那一方照常跑，别人也进不来。"""
        old = self.cli_side.acquire("命令行", "first")
        self.clock.at += trial.LEASE_SECONDS + 1
        new = self.panel_side.acquire("面板", "first")
        self.assertIn("接手了", old.check())
        old.release()
        self.assertEqual(new.check(), "")
        with self.assertRaisesRegex(trial.Busy, "面板正在跑"):
            trial_store(self.table, self.clock).acquire("命令行", "first")

    def test_an_expired_lease_cannot_renew_itself(self):
        """卡了 10 分钟以上的那一方醒来：就算还没人接手也停——别人可能正在接手。"""
        lease = self.cli_side.acquire("命令行", "first")
        self.clock.at += trial.LEASE_SECONDS + 1
        self.assertIn("租约过期了", lease.check())
        self.clock.at -= trial.LEASE_SECONDS                 # 钟拨回去也不会又好了
        self.assertIn("租约过期了", lease.check())
        lease.keepalive()                                    # 不会把它续回来
        self.clock.at += trial.LEASE_SECONDS
        self.panel_side.acquire("面板", "first")              # 别人可以接手

    def test_a_contender_still_settling_does_not_stop_the_running_holder(self):
        lease = self.panel_side.acquire("面板", "first")
        self.table.rows.append({"record_id": "recOTHER", "fields": {
            panel_settings.COL_KEY: trial.KEY_LEASE + "other",
            panel_settings.COL_VALUE: json.dumps(
                {"owner": "别人", "who": "命令行", "round": "first",
                 "until": self.clock.at + 600, "confirmed": False})}})
        self.assertEqual(lease.check(), "")                  # 它再读一遍会自己让开
        self.table.rows[-1]["fields"][panel_settings.COL_VALUE] = json.dumps(
            {"owner": "别人", "who": "命令行", "round": "first",
             "until": self.clock.at + 600, "confirmed": True})
        self.assertIn("接手了", lease.check())

    def test_taking_over_an_unreleased_lease_is_remembered(self):
        self.cli_side.acquire("命令行", "first").release()
        clean = self.panel_side.acquire("面板", "first")
        self.assertFalse(clean.took_over)
        self.clock.at += trial.LEASE_SECONDS + 1             # 这一份没放掉就没动静了
        waits: list = []
        third = trial.TrialStore(settings_store(self.table), clock=self.clock,
                                 sleep=waits.append, log=lambda *_a: None)
        lease = third.acquire("命令行", "first")
        self.assertTrue(lease.took_over)
        self.assertTrue(third.wait_out_previous(lease))
        self.assertEqual(waits[-1], trial.TAKEOVER_GRACE_SECONDS)
        self.assertGreater(trial.TAKEOVER_GRACE_SECONDS, 30)  # 比一个请求最长等的时间长

    def test_an_unreadable_lease_means_no_paid_call(self):
        lease = self.panel_side.acquire("面板", "first")
        def explode(*_a, **_k):
            raise feishu.FeishuError(-1, "读不到")
        self.table.search = explode
        self.assertIn("不发请求", lease.check())
        with self.assertRaises(visibility.StateError):
            self.cli_side.acquire("命令行", "first")

    def test_the_lease_row_holds_no_keyword(self):
        self.panel_side.acquire("面板", "first")
        rows = lease_rows(self.table)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]),
                         {"owner", "who", "round", "at", "until", "released", "confirmed"})


# ---------- 发之前先存进表里 ----------


class TestChargeBeforeSend(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()
        self.store = trial_store(self.table)
        self.words = [(WORD_A, visibility.keyword_hash(WORD_A))]

    def run_round(self, state, sender, **kwargs):
        return visibility.run_round(visibility.ROUNDS["first"], self.words, state, KEY,
                                    send=sender, sleep=lambda _s: None,
                                    clock=lambda: 1_800_000_000.0, log=lambda _l: None, **kwargs)

    def test_the_charge_is_in_the_table_before_the_request_leaves(self):
        state = self.store.load_state()
        state.register([WORD_A])
        seen = []

        def sender(request, _timeout):
            # 这一刻换一个进程去读表：这一笔已经在账上了。
            seen.append(trial_store(self.table).load_state().calls)
            return by_page(request)
        self.run_round(state, sender)
        self.assertEqual(seen, [1, 2, 3])

    def test_a_crash_after_sending_keeps_the_charge(self):
        """发出去之后进程被杀（重新部署）：重启读到的账本里这一笔还在。"""
        state = self.store.load_state()
        state.register([WORD_A])

        class Killed(BaseException):
            pass

        def sender(_request, _timeout):
            raise Killed()
        with self.assertRaises(Killed):
            self.run_round(state, sender)
        again = trial_store(self.table).load_state()
        self.assertEqual(again.calls, 1)
        self.assertAlmostEqual(again.yuan, 0.072)

    def test_no_request_is_sent_when_the_charge_cannot_be_stored(self):
        state = self.store.load_state()
        state.register([WORD_A])
        state.save()
        self.table.fail_on = lambda _key, _value: True
        sender = Sender()                               # 发了就炸
        result = self.run_round(state, sender)
        self.assertEqual(sender.requests, [])
        self.assertEqual(result.requests, 0)
        self.assertIn("账本存不上", result.stopped)
        self.assertEqual(state.calls, 0)                # 没发的这一次不记在手里这份上
        self.table.fail_on = None
        self.assertEqual(trial_store(self.table).load_state().calls, 0)

    def test_a_page_that_cannot_be_stored_stops_the_round(self):
        """请求发了、页存不上：这一笔在发之前已经存好，后面的不再发。"""
        state = self.store.load_state()
        state.register([WORD_A])
        state.save()
        sent = []

        def sender(request, _timeout):
            sent.append(request)
            self.table.fail_on = lambda _key, _value: True     # 回来之后表写不进去了
            return by_page(request)
        result = self.run_round(state, sender)
        self.assertEqual(len(sent), 1)
        self.assertIn("账本存不上", result.stopped)
        self.table.fail_on = None
        self.assertEqual(trial_store(self.table).load_state().calls, 1)

    def test_retries_count_against_the_number_the_round_was_started_with(self):
        """每一页先失败一次再成功：说好最多 3 次，就只发 3 次，差的页下次补。"""
        state = self.store.load_state()
        state.register([WORD_A])
        count = [0]

        def flaky(request):
            count[0] += 1
            return (transport.Response(500, "text/plain", "oops") if count[0] % 2
                    else by_page(request))
        sender = Sender(default=flaky)
        result = self.run_round(state, sender, budget=3)
        self.assertEqual((len(sender.requests), result.requests), (3, 3))
        self.assertIn("已经发满了", result.stopped)
        self.assertEqual((result.fetched, result.todo), (1, 2))
        self.assertEqual(trial_store(self.table).load_state().calls, 3)

    def test_a_lost_lease_stops_before_the_next_request(self):
        state = self.store.load_state()
        state.register([WORD_A])
        answers = iter(["", "租约不在这一边手里了"])
        sender = Sender(default=by_page)
        result = self.run_round(state, sender, guard=lambda: next(answers))
        self.assertEqual(len(sender.requests), 1)
        self.assertIn("租约不在这一边手里了", result.stopped)


# ---------- 面板那一栏 ----------


def make_trial(table: SettingsTable, tables, sender=None, *, key=KEY, clock=None, spawn=None):
    """一个「面板进程」。`tables` 是 [(项目名, 飞书表)]。任务默认当场跑完（不起线程）。"""
    return trial.Trial(lambda: list(tables), trial_store(table, clock), Settings(), key,
                       send=sender, sleep=lambda _s: None, clock=clock or Clock(),
                       resolver=lambda: (lambda _url: None), balance=None,
                       spawn=spawn or (lambda work: work()), log=lambda *_a: None)


def two_word_table(**kwargs):
    return TrialTable([trial_record(100, WORD_A), trial_record(999, WORD_A),
                       trial_record(300, WORD_B, age_days=5),
                       {"record_id": "blank", "fields": {}}], **kwargs)


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()

    def test_it_says_which_tables_are_on_and_counts_words_and_posts(self):
        status = make_trial(self.table, [("甲表", two_word_table())]).status()
        self.assertEqual(status["problem"], "")
        row = status["tables"][0]
        self.assertEqual((row["label"], row["column"], row["words"], row["posts"]),
                         ("甲表", "ok", 2, 3))
        self.assertEqual(status["keywords"]["total"], 2)
        self.assertFalse(status["keywords"]["over"])
        self.assertEqual(status["limits"]["keywords"], 15)
        self.assertEqual(status["ledger"]["calls"], 0)

    def test_the_word_list_is_only_in_the_status_payload(self):
        status = make_trial(self.table, [("甲表", two_word_table())]).status()
        self.assertEqual(status["keywords"]["list"],
                         [{"k": "K01", "word": WORD_A}, {"k": "K02", "word": WORD_B}])
        self.assertEqual(self.table.writes, [])         # 看一眼什么都不存

    def test_a_missing_column_is_named_with_the_table_to_build(self):
        missing = two_word_table(with_column=False, table_id="tblMISS")
        status = make_trial(self.table, [("乙表", missing)]).status()
        row = status["tables"][0]
        self.assertEqual((row["column"], row["table_id"]), ("missing", "tblMISS"))
        self.assertIn("补齐缺的列", status["problem"])

    def test_no_table_switched_on_says_where_to_click(self):
        status = make_trial(self.table, []).status()
        self.assertIn("加表 / 改配置", status["problem"])
        self.assertIn("搜索检查", status["problem"])

    def test_sentence_cells_and_long_options_are_counted(self):
        table = TrialTable([trial_record(1, "这一格是一句备注，先不查。"), trial_record(2, WORD_A)])
        status = make_trial(self.table, [("甲表", table)]).status()
        self.assertEqual(status["keywords"]["sentence_cells"], 1)
        self.assertEqual(status["keywords"]["total"], 1)

    def test_too_many_keywords_is_a_refusal_before_spending(self):
        records = [trial_record(i, f"占位词{i}") for i in range(1, 17)]
        made = make_trial(self.table, [("甲表", TrialTable(records))], Sender())
        status = made.status()
        self.assertTrue(status["keywords"]["over"])
        self.assertIn("词太多了", status["problem"])
        self.assertFalse(any(r["runnable"] for r in status["rounds"]))
        job = made.start("first", 45)
        self.assertIn("词太多了", job["refused"])
        self.assertIn("一分钱没花", job["refused"])
        self.assertEqual(trial_store(self.table).load_state().data["keywords"], {})

    def test_later_rounds_appear_only_after_the_first(self):
        made = make_trial(self.table, [("甲表", two_word_table())], Sender(default=by_page))
        visible = {r["name"]: r["visible"] for r in made.status()["rounds"]}
        self.assertEqual(visible, {"first": True, "latest": False, "repeat1": False,
                                   "repeat2": False, "depth": False})
        made.start("first", 6)
        rounds = {r["name"]: r for r in made.status()["rounds"]}
        self.assertTrue(all(r["visible"] for r in rounds.values()))
        self.assertEqual((rounds["first"]["max_calls"], rounds["first"]["have"]), (0, 6))
        self.assertEqual((rounds["latest"]["max_calls"], rounds["latest"]["max_yuan"]), (4, 0.29))
        self.assertEqual(rounds["depth"]["max_calls"], 20)

    def test_a_first_round_that_fetched_nothing_does_not_unlock_later_rounds(self):
        """第一轮点了，第一个请求就被拒（Key 不对）：一页都没拿到，后面几轮没有东西可比。"""
        refused = Sender(default=lambda _r: transport.Response(
            401, "application/json", '{"detail":"x"}'))
        made = make_trial(self.table, [("甲表", two_word_table())], refused)
        job = made.start("first", 6)
        self.assertEqual((job["run"]["fetched"], job["run"]["requests"]), (0, 1))
        rounds = {r["name"]: r for r in made.status()["rounds"]}
        self.assertTrue(rounds["first"]["ran"])
        for name in ("latest", "repeat1", "repeat2", "depth"):
            self.assertFalse(rounds[name]["visible"], name)
            self.assertFalse(rounds[name]["runnable"], name)
            with self.assertRaisesRegex(trial.Refused, "至少拿到过一页"):
                made.start(name, 20)
        self.assertEqual(len(refused.requests), 1)

    def test_a_later_round_is_refused_server_side_before_the_first(self):
        made = make_trial(self.table, [("甲表", two_word_table())], Sender())
        with self.assertRaisesRegex(trial.Refused, "要先跑过第一轮"):
            made.start("latest", 4)
        self.assertEqual(lease_rows(self.table), [])

    def test_a_broken_ledger_blocks_everything_and_says_so(self):
        self.table.rows.append({"record_id": "rec1", "fields": {
            panel_settings.COL_KEY: trial.chunk_key("a", 0), panel_settings.COL_VALUE: "{}"}})
        made = make_trial(self.table, [("甲表", two_word_table())], Sender())
        self.assertIn("只存了一半", made.status()["problem"])
        with self.assertRaisesRegex(trial.Refused, "只存了一半"):
            made.start("first", 6)


class TestDryRunAndReportOnly(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()
        self.tables = [("甲表", two_word_table())]

    def test_dry_run_prices_the_round_and_touches_nothing(self):
        made = make_trial(self.table, self.tables, Sender())       # 发了就炸
        result = made.dry_run("first")
        text = "\n".join(result["lines"])
        self.assertTrue(result["ok"])
        self.assertIn("只演练，不发付费请求", text)
        self.assertIn("还要发最多 6 次，约 ¥0.43", text)
        self.assertEqual((result["need"], result["yuan"]), (6, 0.43))
        self.assertNotIn(WORD_A, text)
        self.assertEqual(self.table.writes, [])

    def test_dry_run_after_a_run_leaves_the_stored_ledger_alone(self):
        make_trial(self.table, self.tables, Sender(default=by_page)).start("first", 6)
        before = [dict(r["fields"]) for r in self.table.rows]
        make_trial(self.table, self.tables, Sender()).dry_run("latest")
        self.assertEqual([dict(r["fields"]) for r in self.table.rows], before)

    def test_report_only_pays_nothing_and_leaves_the_ledger_alone(self):
        make_trial(self.table, self.tables, Sender(default=by_page)).start("first", 6)
        ledger = {k: self.table.value(k) for k in self.table.keys()
                  if k == trial.KEY_MANIFEST or k.startswith(trial.KEY_STATE_PREFIX)}
        self.table.writes.clear()
        again = make_trial(self.table, self.tables, Sender())       # 另一个进程，发了就炸
        job = again.report_only()
        self.assertFalse(job["running"])
        self.assertEqual((job["error"], job["refused"]), ("", ""))
        self.assertEqual({k for k, _v in self.table.writes}, {trial.KEY_RESULT})
        self.assertEqual({k: self.table.value(k) for k in ledger}, ledger)
        self.assertIsNone(json.loads(self.table.value(trial.KEY_RESULT))["run"])
        self.assertIn("至少在一个词下面搜到 1 篇", again.status()["result"]["text"])

    def test_report_only_with_nothing_stored_does_not_create_a_ledger(self):
        make_trial(self.table, self.tables, Sender()).report_only()
        self.assertEqual(self.table.keys(), [trial.KEY_RESULT])


class TestPaidRun(unittest.TestCase):
    def setUp(self):
        self.table = SettingsTable()
        self.tables = [("甲表", two_word_table())]

    def test_a_run_fetches_stores_the_ledger_and_publishes_counts_only(self):
        sender = Sender(default=by_page)
        made = make_trial(self.table, self.tables, sender)
        job = made.start("first", 6)
        self.assertEqual(len(sender.requests), 6)
        self.assertEqual((job["running"], job["refused"], job["error"]), (False, "", ""))
        self.assertEqual(job["run"]["fetched"], 6)
        self.assertAlmostEqual(job["run"]["yuan"], 0.43)
        stored = trial_store(self.table).load_state()
        self.assertEqual((stored.calls, len(stored.data["pages"])), (6, 6))
        text = made.status()["result"]["text"]
        self.assertIn("发布 3 天内的帖子 2 篇，综合排序前 3 页里至少在一个词下面搜到 1 篇", text)
        self.assertIn("花费：发了 6 次请求", text)
        self.assertIn("- K01：拿到 3 页", text)

    def test_nothing_stored_or_logged_holds_client_material(self):
        logs = []
        made = make_trial(self.table, self.tables, Sender(default=by_page))
        made.log = logs.append
        job = made.start("first", 6)
        everything = json.dumps([r["fields"] for r in self.table.rows], ensure_ascii=False) \
            + "\n".join(logs) + "\n".join(job["lines"])
        for forbidden in (WORD_A, WORD_B, "某某品牌的标题", "某某昵称", "xiaohongshu.com", KEY):
            self.assertNotIn(forbidden, everything)
        self.assertNotIn(nid(100), self.table.value(trial.KEY_RESULT))      # 报告里没有笔记 ID

    def test_the_confirmed_maximum_is_rechecked_on_the_server(self):
        """页面上确认的是「最多 3 次」，真要发 6 次：不跑，一分钱不花。"""
        sender = Sender()
        job = make_trial(self.table, self.tables, sender).start("first", 3)
        self.assertIn("比你刚才确认的 3 次多", job["refused"])
        self.assertEqual(sender.requests, [])
        self.assertEqual(trial_store(self.table).load_state().calls, 0)

    def test_starting_without_a_confirmed_number_is_refused(self):
        made = make_trial(self.table, self.tables, Sender())
        for bad in (None, True, "6", -1):
            with self.assertRaises(trial.Refused):
                made.start("first", bad)

    def test_no_key_means_no_run(self):
        made = make_trial(self.table, self.tables, Sender(), key="")
        with self.assertRaisesRegex(trial.Refused, "TIKHUB_API_KEY"):
            made.start("first", 6)

    def test_an_unknown_round_is_refused(self):
        with self.assertRaisesRegex(trial.Refused, "没有这一轮"):
            make_trial(self.table, self.tables, Sender()).start("everything", 6)

    def test_pages_are_reused_after_a_redeploy(self):
        """重新部署 = 新进程、空盘。拿到过的页在表里，不付第二次钱。"""
        make_trial(self.table, self.tables, Sender(default=by_page)).start("first", 6)
        fresh = make_trial(self.table, self.tables, Sender())       # 新进程，发了就炸
        status = fresh.status()
        self.assertEqual(status["ledger"]["calls"], 6)
        first = next(r for r in status["rounds"] if r["name"] == "first")
        self.assertEqual((first["max_calls"], first["have"]), (0, 6))
        job = fresh.start("first", 0)
        self.assertEqual((job["run"]["fetched"], job["run"]["reused"], job["run"]["requests"]),
                         (0, 6, 0))
        self.assertEqual(trial_store(self.table).load_state().calls, 6)

    def test_a_redeploy_mid_round_only_pays_for_the_missing_pages(self):
        class Killed(BaseException):
            pass
        count = [0]

        def dies_on_the_fourth(request, _timeout):
            count[0] += 1
            if count[0] == 4:
                # 进程没了：这之后它什么都写不出去，租约也来不及放。
                self.table.fail_on = lambda _key, _value: True
                raise Killed()
            return by_page(request)
        clock = Clock()
        with self.assertRaises(Killed):
            make_trial(self.table, self.tables, dies_on_the_fourth, clock=clock).start("first", 6)
        self.table.fail_on = None
        stored = trial_store(self.table).load_state()
        self.assertEqual((stored.calls, len(stored.data["pages"])), (4, 3))    # 第 4 次记了账
        # 被杀的那个进程没放租约：新进程要等它过期。
        fresh = make_trial(self.table, self.tables, Sender(default=by_page), clock=clock)
        with self.assertRaisesRegex(trial.Refused, "另一个试跑正在跑"):
            fresh.start("first", 3)
        clock.at += trial.LEASE_SECONDS + 1
        job = fresh.start("first", 3)
        self.assertEqual((job["run"]["fetched"], job["run"]["reused"]), (3, 3))
        self.assertEqual(trial_store(self.table).load_state().calls, 7)

    def test_the_call_cap_holds_across_processes(self):
        state = trial_store(self.table).load_state()
        state.data["calls"] = visibility.MAX_CALLS - 2
        state.save()
        sender = Sender(default=by_page)
        job = make_trial(self.table, self.tables, sender).start("first", 6)
        self.assertEqual(len(sender.requests), 2)
        self.assertIn("次数上限到了", job["run"]["stopped"])
        self.assertEqual(trial_store(self.table).load_state().calls, visibility.MAX_CALLS)

    def test_the_money_cap_holds_too(self):
        state = trial_store(self.table).load_state()
        state.data["yuan"] = visibility.MAX_YUAN - 0.1
        state.save()
        sender = Sender(default=by_page)
        job = make_trial(self.table, self.tables, sender).start("first", 6)
        self.assertEqual(len(sender.requests), 1)
        self.assertIn("金额上限到了", job["run"]["stopped"])

    def test_a_first_page_without_a_session_is_not_kept(self):
        sender = Sender([ok(page_of(100), session=False)], default=by_page)
        job = make_trial(self.table, self.tables, sender).start("first", 6)
        self.assertEqual((job["run"]["failed"], job["run"]["todo"]), (1, 3))
        self.assertIn("没查全（说不准）1 篇", json.loads(self.table.value(trial.KEY_RESULT))["text"])

    def test_an_unreadable_first_page_stops_the_round_after_one_call(self):
        weird = transport.Response(200, "application/json", json.dumps(
            {"code": 200, "data": {"code": 0, "success": True, "data": {
                "items": [{"model_type": "note", "strange": {"x": 1}}],
                "search_id": "s", "search_session_id": "t"}}}))
        sender = Sender([weird])
        job = make_trial(self.table, self.tables, sender).start("first", 6)
        self.assertEqual(len(sender.requests), 1)
        self.assertIn("认不出来", job["run"]["stopped"])
        self.assertEqual(trial_store(self.table).load_state().calls, 1)

    def test_an_unreadable_table_refuses_before_spending(self):
        broken = two_word_table()
        broken.fields_meta = lambda: None
        sender = Sender()
        job = make_trial(self.table, [("甲表", two_word_table()), ("丙表", broken)],
                         sender).start("first", 6)
        self.assertIn("读不到", job["refused"])
        self.assertIn("一分钱没花", job["refused"])
        self.assertEqual(sender.requests, [])

    def test_a_round_never_sends_more_than_the_confirmed_number(self):
        """每一页先回 500 再成功。确认的是「最多 6 次」，就最多发 6 次；再点一次补剩下的。"""
        count = [0]

        def flaky(request):
            count[0] += 1
            return (transport.Response(500, "text/plain", "oops") if count[0] % 2
                    else by_page(request))
        sender = Sender(default=flaky)
        made = make_trial(self.table, self.tables, sender)
        self.assertEqual(made.status()["rounds"][0]["max_calls"], 6)
        job = made.start("first", 6)
        self.assertEqual((len(sender.requests), job["run"]["requests"]), (6, 6))
        self.assertAlmostEqual(job["run"]["yuan"], 0.43)
        self.assertIn("已经发满了", job["run"]["stopped"])
        self.assertEqual(trial_store(self.table).load_state().calls, 6)
        left = made.status()["rounds"][0]["max_calls"]
        self.assertEqual(left, 3)
        again = made.start("first", left)
        self.assertLessEqual(again["run"]["requests"], left)

    def test_the_lease_is_released_when_the_run_ends(self):
        made = make_trial(self.table, self.tables, Sender(default=by_page))
        made.start("first", 6)
        self.assertEqual([row["released"] for row in lease_rows(self.table)], [True])
        self.assertIsNone(made.status()["lease"])


class TestOnlyOneRunAtATime(unittest.TestCase):
    """连点两下、两个标签页、面板和命令行：同一时刻只有一个在花钱。"""

    def setUp(self):
        self.table = SettingsTable()
        self.tables = [("甲表", two_word_table())]

    def test_a_double_click_in_the_same_panel_is_refused(self):
        held: list = []
        made = make_trial(self.table, self.tables, Sender(default=by_page),
                          spawn=held.append)                 # 任务先不跑：停在「正在跑」
        job = made.start("first", 6)
        self.assertTrue(job["running"])
        with self.assertRaisesRegex(trial.Refused, "已经有一个任务在跑"):
            made.start("first", 6)
        with self.assertRaisesRegex(trial.Refused, "已经有一个任务在跑"):
            made.report_only()
        self.assertEqual(len(held), 1)
        held[0]()
        self.assertFalse(made.job()["running"])

    def test_two_tabs_racing_start_only_one_run(self):
        sender = Sender(default=by_page)
        gate = threading.Event()

        def slow(request, timeout):
            gate.wait(5)
            return sender(request, timeout)
        made = make_trial(self.table, self.tables, slow,
                          spawn=lambda work: threading.Thread(target=work, daemon=True).start())
        outcomes: list = []

        def click():
            try:
                made.start("first", 6)
                outcomes.append("started")
            except trial.Refused:
                outcomes.append("refused")
        threads = [threading.Thread(target=click) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        gate.set()
        for _ in range(500):
            if not made.job()["running"]:
                break
            threading.Event().wait(0.01)
        self.assertEqual(sorted(outcomes), ["refused", "refused", "refused", "started"])
        self.assertEqual(len(sender.requests), 6)
        self.assertEqual(trial_store(self.table).load_state().calls, 6)

    def test_a_second_panel_process_is_refused_by_the_lease(self):
        held: list = []
        clock = Clock()
        first = make_trial(self.table, self.tables, Sender(default=by_page), clock=clock,
                           spawn=held.append)
        first.start("first", 6)
        other_sender = Sender()
        other = make_trial(self.table, self.tables, other_sender, clock=clock)
        with self.assertRaisesRegex(trial.Refused, "另一个试跑正在跑"):
            other.start("first", 6)
        self.assertEqual(other_sender.requests, [])
        lease = other.status()["lease"]
        self.assertEqual((lease["who"], lease["round"], lease["mine"]), ("面板", "first", False))
        held[0]()
        other.start("first", 0)                              # 放掉之后别处能跑，页不重付

    def stalled_run(self, clock):
        """一个「命令行那一边」的面板替身：第一页的请求发出去之后卡住，等 `resume`。"""
        in_send, resume = threading.Event(), threading.Event()
        sender = Sender(default=by_page)

        def stuck(request, timeout):
            first = not sender.requests
            answer = sender(request, timeout)
            if first:
                in_send.set()
                resume.wait(10)
            return answer
        made = make_trial(self.table, self.tables, stuck, clock=clock,
                          spawn=lambda work: threading.Thread(target=work, daemon=True).start())
        made.start("first", 6)
        self.assertTrue(in_send.wait(10))
        return made, sender, resume

    def wait_done(self, made):
        for _ in range(1000):
            if not made.job()["running"]:
                return
            threading.Event().wait(0.01)
        self.fail("任务没有跑完")

    def test_a_stale_run_does_not_overwrite_the_report_or_the_lease_of_its_successor(self):
        """甲卡在第一个请求上超过 10 分钟，乙接手、跑完、出了报告。甲醒来：存不进去、
        不出报告（它手里的账本没有乙拿到的页）、也不碰乙的租约。"""
        clock = Clock()
        stale, stale_sender, resume = self.stalled_run(clock)
        clock.at += trial.LEASE_SECONDS + 1
        fresh_sender = Sender(default=by_page)
        fresh = make_trial(self.table, self.tables, fresh_sender, clock=clock)
        job = fresh.start("first", 6)
        self.assertEqual(job["run"]["todo"], 0)
        published = json.loads(self.table.value(trial.KEY_RESULT))
        held: list = []
        third = make_trial(self.table, self.tables, Sender(default=by_page), clock=clock,
                           spawn=held.append)
        third.start("latest", 4)                              # 这时又有一个在跑
        resume.set()
        self.wait_done(stale)
        self.assertEqual(len(stale_sender.requests), 1)       # 甲醒来之后没有再发
        self.assertEqual(json.loads(self.table.value(trial.KEY_RESULT)), published)
        self.assertIn("不出报告", "\n".join(stale.job()["lines"]))
        with self.assertRaisesRegex(trial.Refused, "另一个试跑正在跑"):
            make_trial(self.table, self.tables, Sender(), clock=clock).start("first", 6)
        held[0]()
        self.assertEqual(third.job()["run"]["requests"], 4)   # 接手之后的那一个没被甲弄停

    def test_a_takeover_waits_for_the_page_in_flight_instead_of_paying_it_again(self):
        """甲的租约没放掉就过期了，其实甲还活着、第一页的请求还在路上。乙接手之后先等 40 秒
        再读账本：甲那一页这时存进去了，乙只付剩下的 5 页。"""
        clock = Clock()
        stale, stale_sender, resume = self.stalled_run(clock)
        clock.at += trial.LEASE_SECONDS + 1
        waits: list = []

        def grace(seconds):
            waits.append(seconds)
            if seconds == trial.TAKEOVER_GRACE_SECONDS:
                resume.set()                                  # 这 40 秒里甲的回答到了
                self.wait_done(stale)
        fresh_sender = Sender(default=by_page)
        store = trial.TrialStore(settings_store(self.table), clock=clock, sleep=grace,
                                 log=lambda *_a: None)
        fresh = trial.Trial(lambda: list(self.tables), store, Settings(), KEY,
                            send=fresh_sender, sleep=lambda _s: None, clock=clock,
                            resolver=lambda: (lambda _url: None), balance=None,
                            spawn=lambda work: work(), log=lambda *_a: None)
        job = fresh.start("first", 6)
        self.assertIn(trial.TAKEOVER_GRACE_SECONDS, waits)
        self.assertEqual((len(stale_sender.requests), len(fresh_sender.requests)), (1, 5))
        self.assertEqual((job["run"]["reused"], job["run"]["fetched"], job["run"]["todo"]),
                         (1, 5, 0))
        self.assertEqual(trial_store(self.table).load_state().calls, 6)
        self.assertEqual(json.loads(self.table.value(trial.KEY_RESULT))["run"]["requests"], 5)

    def test_the_cli_and_the_panel_share_the_lease(self):
        held: list = []
        clock = Clock()
        made = make_trial(self.table, self.tables, Sender(default=by_page), clock=clock,
                          spawn=held.append)
        made.start("first", 6)
        with self.assertRaises(trial.Busy):
            trial_store(self.table, clock).acquire("命令行", "first")


# ---------- 开关：面板上勾，登记表里没有那一列就建 ----------


class RegistryTable:
    def __init__(self, columns, records):
        self._meta = {name: {"type": kind, "options": None} for name, kind in columns.items()}
        self._records = records
        self.created_fields: list = []
        self.updated: list = []

    def fields_meta(self):
        return self._meta

    def search(self, field_names, **_kwargs):
        return [{"record_id": r["record_id"],
                 "fields": {k: v for k, v in r["fields"].items() if k in field_names}}
                for r in self._records]

    def create_field(self, body):
        self.created_fields.append(body)
        self._meta[body["field_name"]] = {"type": body["type"], "options": None}
        return "fld1"

    def batch_update(self, updates, **_kwargs):
        self.updated.extend(updates)
        for update in updates:
            for record in self._records:
                if record["record_id"] == update["record_id"]:
                    record["fields"].update(update["fields"])
        return len(updates)


OLD_COLUMNS = {registry.COL_LABEL: 1, registry.COL_TARGET: 1, registry.COL_ENABLED: 7}


def registry_row(record_id="recA", table="tblAAA", **cells):
    return {"record_id": record_id, "fields": {
        registry.COL_LABEL: "甲表" if record_id == "recA" else "乙表", registry.COL_TARGET: f"bascnAAA:{table}",
        registry.COL_ENABLED: True, **cells}}


class TestSwitch(unittest.TestCase):
    def projects(self, table):
        cfg = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32)
        cfg.registry_target, cfg.app_id, cfg.app_secret = "bascnREG:tblREG", "cli_x", "s"
        projects = panel.Projects(cfg, Settings(), log=lambda *_a: None)
        projects._registry = lambda: table
        return projects

    def test_saving_the_switch_creates_the_checkbox_on_an_old_registry(self):
        table = RegistryTable(dict(OLD_COLUMNS), [registry_row()])
        result = self.projects(table).set_search_check("recA", True)
        self.assertEqual(table.created_fields,
                         [{"field_name": registry.COL_SEARCH_CHECK, "type": 7}])
        self.assertEqual(table.updated, [{"record_id": "recA",
                                          "fields": {registry.COL_SEARCH_CHECK: True}}])
        self.assertEqual(result, {"on": True, "created_columns": [registry.COL_SEARCH_CHECK]})
        self.assertTrue(registry.read(table)[0].search_check)

    def test_an_existing_column_is_not_created_again(self):
        table = RegistryTable({**OLD_COLUMNS, registry.COL_SEARCH_CHECK: 7}, [registry_row()])
        result = self.projects(table).set_search_check("recA", True)
        self.assertEqual((table.created_fields, result["created_columns"]), ([], []))

    def test_switching_off_on_an_old_registry_writes_nothing(self):
        table = RegistryTable(dict(OLD_COLUMNS), [registry_row()])
        self.projects(table).set_search_check("recA", False)
        self.assertEqual((table.created_fields, table.updated), ([], []))

    def test_a_column_of_the_wrong_type_is_not_written(self):
        table = RegistryTable({**OLD_COLUMNS, registry.COL_SEARCH_CHECK: 1}, [registry_row()])
        with self.assertRaisesRegex(registry.RegistryError, "不是复选框"):
            self.projects(table).set_search_check("recA", True)
        self.assertEqual(table.updated, [])

    def test_ticking_by_hand_in_feishu_still_counts(self):
        table = RegistryTable({**OLD_COLUMNS, registry.COL_SEARCH_CHECK: 7},
                              [registry_row(**{registry.COL_SEARCH_CHECK: True}),
                               registry_row("recB", "tblBBB")])
        projects = self.projects(table)
        self.assertEqual([label for label, _t in projects.trial_tables()], ["甲表"])
        self.assertEqual(projects.trial_tables()[0][1].table_id, "tblAAA")

    def test_a_disabled_table_is_not_read_by_the_trial(self):
        table = RegistryTable({**OLD_COLUMNS, registry.COL_SEARCH_CHECK: 7}, [
            registry_row(**{registry.COL_SEARCH_CHECK: True, registry.COL_ENABLED: False})])
        self.assertEqual(self.projects(table).trial_tables(), [])

    def test_the_parameters_form_carries_the_checkbox(self):
        entry = registry.Entry(record_id="recA", label="甲表", search_check=True)
        self.assertTrue(panel._entry_json(entry)["search_check"])
        self.assertIn("搜索检查（搜索可见试跑）", panel_view._SCRIPT)
        self.assertIn('post("search_check", {record_id: schkBox.dataset.rec, on: tick.checked})',
                      panel_view._SCRIPT)


# ---------- 路由 ----------


class TestTrialRoutes(unittest.TestCase):
    """鉴权、CSRF、确认那一步、错误怎么回。背后是真的 `Trial`，飞书和发请求是替身。"""

    @classmethod
    def setUpClass(cls):
        quiet = mock.patch.object(panel.PanelHandler, "log_message",
                                  lambda self, fmt, *args: None)
        quiet.start()
        cls.addClassCleanup(quiet.stop)
        cls.config = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32, port=0,
                                       cache_seconds=999)
        cls.cache = panel.Cache(lambda: summary.Overview(projects=[]), ttl=999)
        cls.cache.refresh()
        cls.holder = {}
        cls.server = panel.build_server(cls.config, cls.cache, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.cache.stop()

    def setUp(self):
        self.table = SettingsTable()
        self.sender = Sender(default=by_page)
        self.trial = make_trial(self.table, [("甲表", two_word_table())], self.sender)
        self.server.deps.trial = self.trial

    def call(self, path, payload=None, headers=None):
        import urllib.error
        import urllib.request
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"http://localhost:{self.port}{path}", data=data,
                                     headers=headers or {},
                                     method="POST" if payload is not None else "GET")
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, {"raw": body}

    def login(self):
        token = panel.issue_session(self.config)
        return {"Cookie": f"{panel.COOKIE_NAME}={token}",
                panel.CSRF_HEADER: panel.csrf_token(self.config, token),
                "Content-Type": "application/json"}

    def test_everything_needs_a_session(self):
        self.assertEqual(self.call("/api/trial")[0], 401)
        for action in ("dry-run", "start", "report"):
            self.assertEqual(self.call(f"/api/trial/{action}", {})[0], 401)
        self.assertEqual(self.sender.requests, [])

    def test_buttons_need_the_csrf_header(self):
        headers = self.login()
        del headers[panel.CSRF_HEADER]
        status, _ = self.call("/api/trial/start",
                              {"round": "first", "confirm": True, "confirm_calls": 6}, headers)
        self.assertEqual(status, 403)
        self.assertEqual(self.sender.requests, [])

    def test_status_carries_the_words_only_for_a_signed_in_user(self):
        status, body = self.call("/api/trial", headers=self.login())
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertEqual(body["keywords"]["list"][0], {"k": "K01", "word": WORD_A})
        status, body = self.call("/api/trial")
        self.assertEqual(status, 401)
        self.assertNotIn(WORD_A, json.dumps(body, ensure_ascii=False))

    def test_dry_run_over_http_sends_nothing(self):
        status, body = self.call("/api/trial/dry-run", {"round": "first"}, self.login())
        self.assertEqual((status, body["ok"], body["need"]), (200, True, 6))
        self.assertEqual(self.sender.requests, [])
        self.assertEqual(self.table.writes, [])

    def test_start_without_the_confirmation_step_is_refused(self):
        headers = self.login()
        for payload in ({"round": "first"}, {"round": "first", "confirm_calls": 6},
                        {"round": "first", "confirm": True},
                        {"round": "first", "confirm": "yes", "confirm_calls": 6}):
            status, body = self.call("/api/trial/start", payload, headers)
            self.assertEqual(status, 409, payload)
            self.assertIn("确认", body["error"])
        self.assertEqual(self.sender.requests, [])
        self.assertEqual(self.table.writes, [])

    def test_a_confirmed_start_runs_and_the_job_is_visible_on_refresh(self):
        status, body = self.call("/api/trial/start",
                                 {"round": "first", "confirm": True, "confirm_calls": 6},
                                 self.login())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.sender.requests), 6)
        status, body = self.call("/api/trial?job=1", headers=self.login())
        self.assertEqual((status, body["job"]["running"], body["job"]["run"]["fetched"]),
                         (200, False, 6))
        status, body = self.call("/api/trial", headers=self.login())
        self.assertEqual(body["ledger"]["calls"], 6)
        self.assertIn("搜索可见试跑报告", body["result"]["text"])

    def test_the_job_poll_does_not_read_feishu(self):
        searches = self.table.searches
        self.call("/api/trial?job=1", headers=self.login())
        self.assertEqual(self.table.searches, searches)

    def test_a_busy_lease_is_a_409_with_a_plain_sentence(self):
        trial_store(self.table).acquire("命令行", "first")
        status, body = self.call("/api/trial/start",
                                 {"round": "first", "confirm": True, "confirm_calls": 6},
                                 self.login())
        self.assertEqual(status, 409)
        self.assertIn("另一个试跑正在跑", body["error"])
        self.assertEqual(self.sender.requests, [])

    def test_report_over_http_pays_nothing(self):
        status, body = self.call("/api/trial/report", {}, self.login())
        self.assertEqual((status, body["job"]["kind"]), (200, "report"))
        self.assertEqual(self.sender.requests, [])

    def test_an_unknown_action_is_a_404(self):
        self.assertEqual(self.call("/api/trial/everything", {}, self.login())[0], 404)

    def test_without_a_registry_the_section_says_how_to_connect_it(self):
        self.server.deps.trial = None
        status, body = self.call("/api/trial", headers=self.login())
        self.assertEqual((status, body["enabled"]), (200, False))
        status, body = self.call("/api/trial/start",
                                 {"round": "first", "confirm": True, "confirm_calls": 6},
                                 self.login())
        self.assertEqual(status, 400)


class TestWiring(unittest.TestCase):
    def config(self, **kwargs):
        cfg = panel.PanelConfig(password=GOOD_PASSWORD, secret=b"s" * 32, **kwargs)
        return cfg

    def test_no_registry_means_no_trial(self):
        cfg = self.config(app_id="cli_x")
        self.assertIsNone(panel.build_trial(cfg, panel.Projects(cfg, Settings()), Settings()))

    def test_the_panel_trial_uses_the_registry_base_and_the_tikhub_key(self):
        cfg = self.config(app_id="cli_x", app_secret="s", registry_target="bascnREG:tblREG",
                          api_keys={"tikhub": KEY, "socialdatax": "other"})
        projects = panel.Projects(cfg, Settings(), log=lambda *_a: None)
        made = panel.build_trial(cfg, projects, Settings())
        self.assertEqual(made.api_key, KEY)
        self.assertIsInstance(made.store, trial.TrialStore)
        self.assertEqual(made.store.settings_store.app_token, "bascnREG")

    def test_the_page_has_the_section_in_the_asked_order(self):
        html = panel_view._trial_section()
        self.assertIn("<h2 id=s-trial>搜索可见试跑</h2>", html)
        order = [html.index(marker) for marker in
                 ("id=trialStatus", "id=trialActs", "id=trialJob", "id=trialReport")]
        self.assertEqual(order, sorted(order))
        js = panel_view._SCRIPT
        steps = [js.index(marker) for marker in
                 ("空跑（不花钱）", "跑第一轮", "只重出报告（不花钱）")]
        self.assertEqual(steps, sorted(steps))
        self.assertIn("确认，开始跑（最多 ", js)
        self.assertIn('confirm: true, confirm_calls: Number(b.dataset.calls)', js)
        self.assertIn("补齐缺的列", js)

    def test_a_missing_key_is_explained_at_the_grey_buttons(self):
        """没配 Key 时「跑」的按钮是灰的：按钮那儿要有一句为什么、找谁。"""
        import pathlib
        source = pathlib.Path(panel_view.__file__).read_text(encoding="utf-8")
        acts = source[source.index("function renderTrialActs"):]
        acts = acts[:acts.index("id=trialConfirm")]
        self.assertIn("!j.key_ok) html.push(", acts)
        self.assertIn("按钮现在是灰的", acts)
        self.assertIn("第 0 步", acts)

    def test_the_handbook_tells_the_owner_to_get_the_key_configured_first(self):
        import pathlib
        text = (pathlib.Path(cli.__file__).resolve().parent / "docs" / "搜索可见试跑.md"
                ).read_text(encoding="utf-8")
        walk = text[text.index("## 手把手"):text.index("# 给技术的")]
        self.assertIn("### 第 0 步", walk)
        self.assertLess(walk.index("TIKHUB_API_KEY"), walk.index("### 第 1 步"))
        who = walk[walk.index("### 谁做哪几步"):walk.index("### 第 0 步")]
        self.assertIn("技术", who)
        step7 = walk[walk.index("### 第 7 步"):walk.index("### 第 8 步")]
        self.assertIn("灰的", step7)

    def test_the_sidebar_links_to_the_section(self):
        self.assertIn("href='#s-trial'", panel_view._sidebar_nav(
            todos="0", projects=0, runs=0, balances=False))


class TestCliSharesTheStore(unittest.TestCase):
    """`search-trial --state registry` 和面板是同一本账、同一把锁；默认还是本机目录。"""

    def setUp(self):
        self.table = SettingsTable()
        self.business = two_word_table()
        patches = [
            mock.patch.object(cli, "_search_trial_tables", lambda selected: [("甲表", self.business)]),
            mock.patch.object(cli, "_search_trial_store", lambda: trial_store(self.table)),
            mock.patch.object(cli, "_tikhub_balance_usd", lambda *_a: None),
            mock.patch.dict(cli.os.environ, {"TIKHUB_API_KEY": KEY}),
            mock.patch("time.sleep", lambda _s: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def invoke(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                code = cli.cmd_search_trial(list(args))
            except SystemExit as exc:
                code = exc.code
                if isinstance(code, str):
                    out.write(code)
                    code = 1
        return code, out.getvalue()

    def test_the_cli_writes_the_same_ledger_the_panel_reads(self):
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender):
            code, out = self.invoke("--state", "registry")
        self.assertEqual(code, 0)
        self.assertIn("新拿到 6 页", out)
        made = make_trial(self.table, [("甲表", self.business)], Sender())
        status = made.status()
        self.assertEqual(status["ledger"]["calls"], 6)
        self.assertIn("搜索可见试跑报告", status["result"]["text"])
        job = made.start("first", 0)                       # 面板接着点：页不重付
        self.assertEqual((job["run"]["reused"], job["run"]["requests"]), (6, 0))

    def test_the_cli_keeps_its_lease_alive_while_it_reads_tables(self):
        """读表、展开短链不发付费请求，可能拖很久：命令行也要顺手续租约，不然面板会以为它没了。"""
        sender = Sender(default=by_page)
        with mock.patch.object(visibility, "_send_once", sender), \
                mock.patch.object(trial.Lease, "keepalive", autospec=True) as keepalive:
            code, _out = self.invoke("--state", "registry")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(keepalive.call_count, 1)

    def test_a_cli_run_that_lost_its_lease_stores_no_report(self):
        """命令行那一边卡过了头：停下、不往登记表 base 里存报告，退出码不是 0。"""
        clock = Clock()
        store = trial_store(self.table, clock)
        sender = Sender(default=by_page)

        def stalls_after_the_first(request, timeout):
            answer = sender(request, timeout)
            if len(sender.requests) == 1:
                clock.at += trial.LEASE_SECONDS + 1
            return answer
        with mock.patch.object(cli, "_search_trial_store", lambda: store), \
                mock.patch.object(visibility, "_send_once", stalls_after_the_first):
            code, out = self.invoke("--state", "registry")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(sender.requests), 1)
        self.assertIn("租约过期了", out)
        self.assertIsNone(self.table.value(trial.KEY_RESULT))

    def test_the_cli_is_refused_while_the_panel_runs(self):
        held: list = []
        made = make_trial(self.table, [("甲表", self.business)], Sender(default=by_page),
                          spawn=held.append)
        made.start("first", 6)
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, out = self.invoke("--state", "registry")
        self.assertNotEqual(code, 0)
        self.assertIn("另一个试跑正在跑", out)

    def test_dry_run_and_report_only_take_no_lease_and_write_no_ledger(self):
        with mock.patch.object(visibility, "_send_once", Sender()):
            code, _out = self.invoke("--state", "registry", "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(self.table.writes, [])
            code, _out = self.invoke("--state", "registry", "--report-only")
        self.assertEqual(code, 0)
        self.assertEqual(self.table.keys(), [trial.KEY_RESULT])

    def test_state_dir_and_registry_do_not_mix(self):
        code, out = self.invoke("--state", "registry", "--state-dir", "/tmp/x")
        self.assertNotEqual(code, 0)
        self.assertIn("两个只能给一个", out)

    def test_an_unknown_store_is_refused(self):
        code, out = self.invoke("--state", "cloud")
        self.assertNotEqual(code, 0)
        self.assertIn("只认 local 或 registry", out)


if __name__ == "__main__":
    unittest.main()
