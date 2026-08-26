"""cli 层的离线测试：doctor 的期望 schema 必须和代码实际读写的列对得上。

「表面对了，内在配置没对」的体检清单如果本身漏了列，就等于没体检——
这里钉住：代码要读的每一列、要写的每个选择值，都在 doctor 的清单里。
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import cli
from xhsearch import feishu, runner
from xhsearch.config import Settings


class TestDoctorSchema(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.schema = {name: (allowed, label, options, note)
                       for name, allowed, label, options, note
                       in cli._expected_schema(self.settings)}

    def test_covers_every_column_the_code_reads(self):
        for column in self.settings.fields.must_read():
            self.assertIn(column, self.schema, f"doctor 的体检清单漏了要读的列「{column}」")

    def test_covers_every_machine_written_column(self):
        f = self.settings.fields
        for column in (f.platform, f.comment_count, f.previous_comment_count,
                       f.pinned_status, f.comment_status, f.comment_digest,
                       f.negative_status, f.negative_digest,
                       f.traffic_status, f.refresh_status,
                       f.failure_reason, f.last_updated, f.alive_confirmed,
                       f.consecutive_failures):
            self.assertIn(column, self.schema, f"doctor 的体检清单漏了要写的列「{column}」")

    def test_select_columns_require_every_machine_value(self):
        """机器往选择列里写的每一个值都必须在必备选项清单里——
        漏一个，那个值就会被 known_options 静默拦下，判定等于没生效。"""
        f = self.settings.fields
        _, _, traffic_required, _ = self.schema[f.traffic_status]
        for tag in self.settings.tags.machine_written():
            self.assertIn(tag, traffic_required)
        # 退役标签（已失效）只留在 merge 的管辖范围里用于摘掉旧值，
        # 不该被 doctor 要求建选项——新表根本不需要它。
        for tag in self.settings.tags.retired:
            self.assertNotIn(tag, traffic_required)
            self.assertIn(tag, self.settings.tags.namespace())
        _, _, status_required, _ = self.schema[f.comment_status]
        for value in self.settings.comment_status.machine_written():
            self.assertIn(value, status_required)
        _, _, pin_required, _ = self.schema[f.pinned_status]
        for value in self.settings.pin_status.machine_written():
            self.assertIn(value, pin_required)
        _, _, negative_required, _ = self.schema[f.negative_status]
        for value in self.settings.negative_status.machine_written():
            self.assertIn(value, negative_required)

    def test_column_types_match_write_semantics(self):
        """流量状态按多选列表合并写入（必须类型码 4）；评论状态/置顶状态
        按单选字符串覆盖写入（必须类型码 3）。类型和写法错配 = 整行写回失败。"""
        f = self.settings.fields
        self.assertEqual(self.schema[f.traffic_status][0], (4,))
        self.assertEqual(self.schema[f.comment_status][0], (3,))
        self.assertEqual(self.schema[f.negative_status][0], (3,))
        self.assertEqual(self.schema[f.pinned_status][0], (3,))


class TestTablesFromEnv(unittest.TestCase):
    """FEISHU_TABLES 解析：这是多表部署的唯一配置入口，
    解析错一项就是少刷一整张表（静默）或整个进程起不来（吵闹）——
    必须吵闹，且报错要说得清哪一项、该怎么写。"""

    def test_single_table_fallback(self):
        entries = cli._tables_from_env(
            {"FEISHU_APP_TOKEN": "bascnA", "FEISHU_TABLE_ID": "tblX"})
        self.assertEqual(entries, [("tblX", "bascnA", "tblX")])

    def test_multi_with_labels_and_both_forms(self):
        spec = ("OKMAN一期=bascnA:tbl1; "
                "OKMAN二期=https://xx.feishu.cn/base/bascnA?table=tbl2&view=vewZ;"
                "bascnB:tbl3")
        entries = cli._tables_from_env({"FEISHU_TABLES": spec})
        self.assertEqual(entries, [
            ("OKMAN一期", "bascnA", "tbl1"),
            ("OKMAN二期", "bascnA", "tbl2"),
            ("tbl3", "bascnB", "tbl3"),      # 不带标签时标签取 table_id
        ])

    def test_tables_wins_over_single_vars(self):
        entries = cli._tables_from_env({
            "FEISHU_TABLES": "甲=bascnA:tbl1",
            "FEISHU_APP_TOKEN": "bascnZ", "FEISHU_TABLE_ID": "tblZ"})
        self.assertEqual(entries, [("甲", "bascnA", "tbl1")])

    def test_newline_and_chinese_semicolon_separators(self):
        entries = cli._tables_from_env(
            {"FEISHU_TABLES": "甲=bascnA:tbl1\n乙=bascnA:tbl2；丙=bascnB:tbl3"})
        self.assertEqual([e[0] for e in entries], ["甲", "乙", "丙"])

    def test_nothing_configured_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env({})

    def test_garbage_entry_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env({"FEISHU_TABLES": "甲=看不懂的东西"})

    def test_url_without_table_param_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=https://xx.feishu.cn/base/bascnA"})

    def test_duplicate_table_exits(self):
        """同一张表配两遍会被两轮 cron 各刷一次——纯白花钱，必须拦。"""
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=bascnA:tbl1; 乙=bascnA:tbl1"})

    def test_duplicate_label_exits(self):
        with self.assertRaises(SystemExit):
            cli._tables_from_env(
                {"FEISHU_TABLES": "甲=bascnA:tbl1; 甲=bascnB:tbl2"})

    def test_wiki_link_is_parsed_like_a_base_link(self):
        """实测过：/wiki/ 地址栏里的 token 直接当 app_token 用，接口就认，
        不需要额外换算——跟 /base/ 一视同仁，只是前缀不同。"""
        entries = cli._tables_from_env(
            {"FEISHU_TABLES": "企业C=https://xx.feishu.cn/wiki/wikcnA?table=tbl9&view=vewZ"})
        self.assertEqual(entries, [("企业C", "wikcnA", "tbl9")])


class TestMainArgs(unittest.TestCase):
    def test_empty_table_filter_exits(self):
        """「--table ,」解析出空清单，和「没传 --table」在下游没法区分，
        会静默变成全表都跑——必须当场拒绝。"""
        with self.assertRaises(SystemExit):
            cli.main(["cli.py", "sweep", "--table", ","])

    def test_duplicate_table_label_exits(self):
        """COR-010：`--table A,A` 会让同一张表被读两遍、付费刷两遍，
        两次结果还可能互相覆盖。仓库对 FEISHU_TABLES 里的重复物理表
        已经是报错处理，这里保持同一口径。"""
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["cli.py", "sweep", "--table", "A,A"])
        self.assertIn("重复", str(ctx.exception))


class TestNumericEnvBounds(unittest.TestCase):
    """ROB-005：越界要**拒绝**，不能静默 clamp。

    静默 clamp 会让人以为自己设的值生效了，然后按一个从没生效过的配置
    去解释运行结果。没有这道闸时，这里接受过 MAX_CONCURRENCY=1000
    （线程/FD 耗尽 + 把供应商限流打出来）和 SOFT_DEADLINE_SECONDS=-1
    （每一行都立刻「留待下一轮」，任务表面正常、实际一行都不刷）。
    """

    def setUp(self):
        import os
        self._saved = {}
        for name in ("MAX_CONCURRENCY", "SOFT_DEADLINE_SECONDS", "DETAIL_WITHIN_DAYS",
                     "MAX_RECORDS_PER_RUN", "TIKHUB_BASE", "CHANNEL_ORDER",
                     "DISPLAY_UTC_OFFSET", "TAG_OBSERVING"):
            self._saved[name] = os.environ.pop(name, None)

    def tearDown(self):
        import os
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _settings_with(self, **env):
        import os
        for key, value in env.items():
            os.environ[key] = value
        return cli.build_settings()

    def test_concurrency_out_of_range_is_rejected(self):
        for value in ("0", "4", "1000", "-1"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self._settings_with(MAX_CONCURRENCY=value)

    def test_negative_deadline_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._settings_with(SOFT_DEADLINE_SECONDS="-1")

    def test_negative_detail_days_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._settings_with(DETAIL_WITHIN_DAYS="-3")

    def test_legal_values_are_accepted(self):
        settings = self._settings_with(MAX_CONCURRENCY="3", SOFT_DEADLINE_SECONDS="1500",
                                       DETAIL_WITHIN_DAYS="0", MAX_RECORDS_PER_RUN="40")
        self.assertEqual(settings.max_concurrency, 3)
        self.assertEqual(settings.soft_deadline_seconds, 1500.0)
        self.assertEqual(settings.detail_within_days, 0)
        self.assertEqual(settings.budget.max_records_per_run, 40)

    def test_duplicate_provider_in_channel_order_is_rejected(self):
        """COR-010：同一家排两遍 = 可降级错误发生后再打它一次，
        白花一次钱拿到同一个答案。"""
        with self.assertRaises(SystemExit) as ctx:
            self._settings_with(CHANNEL_ORDER="xhs=tikhub,tikhub,socialdatax")
        self.assertIn("重复", str(ctx.exception))

    def test_non_https_tikhub_base_is_rejected(self):
        """SUP-007：改 base 等于改「API Key 发到哪台机器」。"""
        with self.assertRaises(SystemExit):
            self._settings_with(TIKHUB_BASE="http://api.tikhub.io")

    def test_unlisted_tikhub_host_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._settings_with(TIKHUB_BASE="https://evil.example")

    def test_line_buffering_never_breaks_a_redirected_stdout(self):
        """日志格式的优化不该让进程起不来：stdout 被换成 StringIO
        （测试、某些托管运行时）时静默跳过即可。"""
        import io
        import sys
        saved = sys.stdout
        sys.stdout = io.StringIO()
        try:
            cli._line_buffer_stdout()      # 不抛异常就是通过
        finally:
            sys.stdout = saved

    def test_display_timezone_defaults_to_beijing(self):
        """默认必须是 +8：飞书国内租户就是按北京时间渲染「最近检查时间」的，
        默认不对齐的话每个人第一次看日志都会以为对不上。"""
        self.assertEqual(cli.build_settings().display.utc_offset_hours, 8.0)

    def test_display_offset_out_of_range_is_rejected(self):
        for value in ("-13", "15", "99"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self._settings_with(DISPLAY_UTC_OFFSET=value)

    def test_half_hour_offsets_work(self):
        settings = self._settings_with(DISPLAY_UTC_OFFSET="5.5")
        self.assertEqual(settings.display.label(), "+05:30")

    def test_observing_tag_can_be_renamed(self):
        settings = self._settings_with(TAG_OBSERVING="冷启动")
        self.assertEqual(settings.tags.heat_tiers()[0], "冷启动")
        self.assertIn("冷启动", settings.tags.machine_written())

    def test_renaming_keeps_the_old_name_revocable(self):
        """改名和关掉一样要退役**旧**名字。漏了的话，已经写出去的「观察中」
        掉到机器命名空间外面、被当成人工标签保护起来，于是一行会同时挂着
        「观察中」和改名后的档位——两个热度档并排，棘轮形同虚设。"""
        settings = self._settings_with(TAG_OBSERVING="冷启动")
        self.assertIn("观察中", settings.tags.namespace())
        self.assertNotIn("观察中", settings.tags.machine_written())

    def test_setting_the_same_name_changes_nothing(self):
        """显式填成默认值不该把默认名退役掉——那会让它立刻被自己摘掉。"""
        settings = self._settings_with(TAG_OBSERVING="观察中")
        self.assertEqual(settings.tags.observing, "观察中")
        self.assertIn("观察中", settings.tags.machine_written())
        self.assertEqual(settings.tags.retired, Settings().tags.retired)

    def test_switching_observing_off_keeps_it_revocable(self):
        """关掉这一档 ≠ 放着不管：已经写出去的「观察中」要还在机器命名空间里，
        下一轮才摘得掉。漏了这一条，那些格子会永远卡在一个没人再更新的标签上。"""
        settings = self._settings_with(TAG_OBSERVING="")
        self.assertNotIn("观察中", settings.tags.heat_tiers())
        self.assertNotIn("观察中", settings.tags.machine_written())
        self.assertIn("观察中", settings.tags.namespace())


class TestLoadDotenv(unittest.TestCase):
    """本地跑 doctor/row 的前提：.env 真的会被读进环境变量。
    没有这个加载器，文档里「填好 .env 就能跑」在本地是空头支票。"""

    def test_reads_env_file_without_overriding_real_env(self):
        import os
        import tempfile
        from pathlib import Path

        from xhsearch.envfile import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "# 注释行\n"
                "TEST_ENVFILE_A=hello\n"
                'TEST_ENVFILE_B="quoted"\n'
                "TEST_ENVFILE_EXISTING=from-file\n"
                "TEST_ENVFILE_EMPTY=\n"
                "没有等号的行\n",
                encoding="utf-8")
            os.environ["TEST_ENVFILE_EXISTING"] = "from-real-env"
            try:
                load_dotenv(Path(tmp))
                self.assertEqual(os.environ["TEST_ENVFILE_A"], "hello")
                self.assertEqual(os.environ["TEST_ENVFILE_B"], "quoted")
                # 已存在的环境变量优先——.env 只补缺不覆盖
                self.assertEqual(os.environ["TEST_ENVFILE_EXISTING"], "from-real-env")
                # 空值不写入，免得把「没填」变成「填了空串」
                self.assertNotIn("TEST_ENVFILE_EMPTY", os.environ)
            finally:
                for name in ("TEST_ENVFILE_A", "TEST_ENVFILE_B",
                             "TEST_ENVFILE_EXISTING", "TEST_ENVFILE_EMPTY"):
                    os.environ.pop(name, None)

    def test_missing_file_is_silently_skipped(self):
        import tempfile
        from pathlib import Path

        from xhsearch.envfile import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            load_dotenv(Path(tmp))   # 没有 .env：不抛异常即通过


class TestDotenvParsing(unittest.TestCase):
    """SUP-009：旧解析器只做 strip('"').strip("'")，一个引号或一个井号的
    差别就会产生「看起来配好了、实际值不对」的部署，而且完全不报错。"""

    def parse(self, line):
        from xhsearch.envfile import parse_line
        return parse_line(line)

    def test_inline_comment_needs_whitespace(self):
        self.assertEqual(self.parse("KEY=value  # 说明")[:2], ("KEY", "value"))
        # 值里本来就有 # 时不能被当注释切掉——Key 里带 # 很常见
        self.assertEqual(self.parse("KEY=abc#def")[:2], ("KEY", "abc#def"))

    def test_quotes_preserve_spaces_and_hashes(self):
        self.assertEqual(self.parse('KEY="a # b"')[:2], ("KEY", "a # b"))
        self.assertEqual(self.parse("KEY='a # b'")[:2], ("KEY", "a # b"))

    def test_double_quotes_handle_escapes_single_quotes_do_not(self):
        self.assertEqual(self.parse(r'KEY="a\nb"')[1], "a\nb")
        self.assertEqual(self.parse(r"KEY='a\nb'")[1], r"a\nb")

    def test_export_prefix_is_accepted(self):
        self.assertEqual(self.parse("export KEY=value")[:2], ("KEY", "value"))

    def test_unterminated_quote_is_reported_not_guessed(self):
        name, value, problem = self.parse('KEY="未闭合')
        self.assertIsNone(name)
        self.assertIn("未配对的引号", problem)

    def test_illegal_name_is_reported(self):
        name, _, problem = self.parse("2BAD=value")
        self.assertIsNone(name)
        self.assertIn("不合法", problem)

    def test_comment_and_blank_lines_are_not_problems(self):
        for line in ("", "   ", "# 注释"):
            name, value, problem = self.parse(line)
            self.assertIsNone(name)
            self.assertEqual(problem, "")

    def test_load_collects_issues(self):
        import tempfile
        from pathlib import Path

        from xhsearch.envfile import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("没有等号的行\nOK_KEY=1\n", encoding="utf-8")
            issues: list = []
            load_dotenv(Path(tmp), issues=issues)
            self.assertEqual(len(issues), 1)
            self.assertIn("第 1 行", issues[0])

    def test_real_callers_refuse_to_start_on_a_malformed_line(self):
        """严格解析器只有在调用方真的去看 issues 时才有意义。

        悄悄跳过一行畸形配置，产生的是一个「看起来配好了、实际值不对」的
        部署：`MAX_YUAN_PER_RUN="10`（引号没配对）被跳过，这一轮就按
        「不限金额」跑，而运维以为自己设了上限。
        """
        def fake_load(directory=None, *, issues=None):
            if issues is not None:
                issues.append('.env 第 3 行：MAX_YUAN_PER_RUN 的值有未配对的引号')

        with mock.patch.object(cli, "load_dotenv", side_effect=fake_load):
            with self.assertRaises(SystemExit):
                cli.load_env_or_exit()

    def test_clean_env_file_starts_normally(self):
        with mock.patch.object(cli, "load_dotenv", side_effect=lambda *a, **k: None):
            cli.load_env_or_exit()   # 不抛即通过


class TestRunLock(unittest.TestCase):
    """ROB-001：两个进程同时刷同一张表 = 重复花钱 + 互相覆盖写入 + 写冲突。"""

    def test_second_acquirer_is_refused_and_told_who_holds_it(self):
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.lock")
            first = runlock.acquire("cli.py sweep", path=path)
            try:
                with self.assertRaises(runlock.Busy) as ctx:
                    runlock.acquire("cli.py queue", path=path)
                self.assertIn("cli.py sweep", str(ctx.exception))
                self.assertIsNotNone(ctx.exception.holder)
            finally:
                first.release()

    def test_normal_release_is_recorded_so_the_ttl_path_can_tell(self):
        """没有 fcntl 的平台（Windows）完全靠 released 标记判断上一任是不是
        正常收工的。不写这个标记、只看 TTL 的话，一次正常跑完的运行会把
        后面 30 分钟内的每一次调用都误挡成 Busy。"""
        import json
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            runlock.acquire("first", path=str(path)).release()
            self.assertTrue(json.loads(path.read_text())["released"])

            # 模拟没有 fcntl 的平台：正常释放过的租约必须能被立刻接管
            with mock.patch.object(runlock, "fcntl", None):
                with runlock.acquire("second", path=str(path)) as second:
                    self.assertEqual(second.info.owner, "second")

    def test_ttl_path_still_blocks_a_crashed_holder(self):
        """反面：上一任**没有**正常释放（崩了），TTL 之内仍然要挡住。"""
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.lock"
            crashed = runlock.acquire("crashed", path=str(path))
            crashed._fd = -1          # 假装进程没走 release 就没了
            with mock.patch.object(runlock, "fcntl", None):
                with self.assertRaises(runlock.Busy):
                    runlock.acquire("next", path=str(path))

    def test_lock_file_open_refuses_to_follow_symlinks(self):
        """锁文件路径是可预测的，而我们随后会 ftruncate + 写它。
        跟着符号链接走 = 把「清空并覆盖任意文件」的能力交给任何能在
        这个目录里建文件的人。"""
        import os
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("这个平台没有 O_NOFOLLOW")
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim.txt"
            victim.write_text("不该被清空的内容", encoding="utf-8")
            path = Path(tmp) / "run.lock"
            os.symlink(victim, path)
            with self.assertRaises(OSError):
                runlock.acquire("attacker", path=str(path))
            self.assertEqual(victim.read_text(encoding="utf-8"), "不该被清空的内容")

    def test_unsupported_flock_is_not_reported_as_contention(self):
        """有些文件系统（部分 NFS 配置、容器挂载）根本不支持 flock，会给
        ENOTSUP/ENOLCK。把它们当成 Busy 的话，CLI 会安静地跳过**每一次**
        定时运行并返回 0——一个部署可以就这样永远不刷表而没人发现。"""
        import errno
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        if runlock.fcntl is None:
            self.skipTest("这个平台没有 fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.lock")
            with mock.patch.object(runlock.fcntl, "flock",
                                   side_effect=OSError(errno.ENOTSUP, "not supported")):
                with self.assertRaises(OSError) as ctx:
                    runlock.acquire("me", path=path)
            self.assertNotIsInstance(ctx.exception, runlock.Busy)
            self.assertEqual(ctx.exception.errno, errno.ENOTSUP)

    def test_real_contention_is_still_reported_as_busy(self):
        """反面：EAGAIN/EACCES 是真的「别人占着」，必须照旧报 Busy。"""
        import errno
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        if runlock.fcntl is None:
            self.skipTest("这个平台没有 fcntl")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.lock")
            for code in (errno.EAGAIN, errno.EACCES):
                with self.subTest(code=code):
                    with mock.patch.object(runlock.fcntl, "flock",
                                           side_effect=OSError(code, "locked")):
                        with self.assertRaises(runlock.Busy):
                            runlock.acquire("me", path=path)

    def test_default_path_is_a_private_per_user_directory(self):
        """默认不能是 /tmp 里一个所有人都能预测、都能抢先建立的固定文件名。"""
        import os

        from xhsearch import runlock

        default = runlock.default_path()
        self.assertTrue(default.endswith("run.lock"))
        if hasattr(os, "getuid"):
            self.assertIn(str(os.getuid()), default)

    def test_released_lease_can_be_taken_again_with_a_higher_token(self):
        import tempfile
        from pathlib import Path

        from xhsearch import runlock

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "run.lock")
            with runlock.acquire("first", path=path) as first:
                first_token = first.token
            with runlock.acquire("second", path=path) as second:
                # fencing token 单调递增：一个「以为自己还持有租约」的僵尸
                # 进程拿着旧 token，凭它可以在日志里被认出来
                self.assertGreater(second.token, first_token)


class TestOptionsFromMeta(unittest.TestCase):
    """None = 查不到别过滤；[] = 不是选择列全拦。写反任何一个分支，
    要么未建选项写进表（整批回滚），要么机器值全部静默丢失。"""

    def test_unreadable_meta_means_no_filter(self):
        self.assertIsNone(cli._options_from_meta(None, "流量状态"))

    def test_missing_column_means_no_filter(self):
        self.assertIsNone(cli._options_from_meta({}, "流量状态"))

    def test_non_select_column_blocks_everything(self):
        meta = {"流量状态": {"type": 1, "ui_type": "Text", "options": None}}
        self.assertEqual(cli._options_from_meta(meta, "流量状态"), [])

    def test_select_column_returns_its_options(self):
        meta = {"流量状态": {"type": 4, "ui_type": "MultiSelect", "options": ["爆贴"]}}
        self.assertEqual(cli._options_from_meta(meta, "流量状态"), ["爆贴"])


class TestSchemaProblems(unittest.TestCase):
    """doctor 的判定逻辑本体：喂典型翻车样本，核对报没报、报得对不对。"""

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields

    def _healthy_meta(self) -> dict:
        meta = {}
        for name, allowed, _label, options, _note in cli._expected_schema(self.settings):
            field_type = allowed[0]
            meta[name] = {
                "type": field_type,
                "ui_type": "",
                "options": list(options or []) if field_type in (3, 4) else None,
            }
        return meta

    def test_healthy_table_has_no_problems(self):
        self.assertEqual(cli._schema_problems(self.settings, self._healthy_meta()), [])

    def test_multiselect_comment_status_is_flagged(self):
        """评论状态现在是单选覆盖写入：建成多选（旧口径的类型）要被点名。"""
        meta = self._healthy_meta()
        meta[self.f.comment_status]["type"] = 4
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.comment_status in p and "类型" in p for p in problems))

    def test_system_modified_time_is_flagged(self):
        """「最近检查时间」建成系统的「最后更新时间」类型：机器写不进去，
        且任何编辑都会刷新它——doctor 必须点名。"""
        meta = self._healthy_meta()
        meta[self.f.last_updated] = {"type": 1002, "ui_type": "ModifiedTime",
                                     "options": None}
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.last_updated in p for p in problems))

    def test_missing_machine_option_wording_depends_on_filtering(self):
        """流量状态走 merge 过滤，缺选项=安全跳过；巡查状态是直写，
        缺选项=可能整行写回失败。两种后果的文案必须如实区分。"""
        meta = self._healthy_meta()
        meta[self.f.traffic_status]["options"].remove("大爆")
        meta[self.f.refresh_status]["options"].remove("跳过")
        problems = cli._schema_problems(self.settings, meta)
        traffic = next(p for p in problems if self.f.traffic_status in p)
        status = next(p for p in problems if self.f.refresh_status in p)
        self.assertIn("跳过（不会误写", traffic)
        self.assertIn("写回失败", status)

    def test_select_without_options_key_still_reports_missing(self):
        """类型已确认是多选、但 API 没带 options 键（零选项的另一种形态）：
        必须按空清单报缺选项，不能当成非选择列放行——运行时会全拦。"""
        meta = self._healthy_meta()
        meta[self.f.comment_status]["options"] = None
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.comment_status in p and "缺这些选项" in p
                            for p in problems))

    def test_exotic_number_subtype_is_flagged(self):
        """评分字段和普通数字共用类型码 2，但封顶 5 星——光看类型码抓不到。"""
        meta = self._healthy_meta()
        meta[self.f.comment_count]["ui_type"] = "Rating"
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any(self.f.comment_count in p and "评分" in p for p in problems))

    def test_missing_required_column_is_called_out_separately(self):
        meta = self._healthy_meta()
        del meta[self.f.link]
        del meta[self.f.comment_digest]
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any("必备列" in p and self.f.link in p for p in problems))
        self.assertTrue(any("自动跳过" in p and self.f.comment_digest in p for p in problems))

    def test_missing_timestamp_column_is_required(self):
        """「最近检查时间」缺失时 sweep 会失控全表重刷——必须按必备列报，
        不能混进「会被自动跳过」的机器列清单里轻描淡写。"""
        meta = self._healthy_meta()
        del meta[self.f.last_updated]
        problems = cli._schema_problems(self.settings, meta)
        self.assertTrue(any("必备列" in p and self.f.last_updated in p for p in problems))


class TestSchedulingBlockers(unittest.TestCase):
    """`最近检查时间` / `排队刷新` 写不进去会**循环烧钱**：刷过的行下一轮
    还是判到期，每一轮重新付费刷同一批，而进程一路返回 0。别的机器列
    建错只是少一列数据，这两列建错是无底洞——必须在花钱之前拦。"""

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields

    def _meta(self, **overrides) -> dict:
        meta = {}
        for name, allowed, _l, options, _n in cli._expected_schema(self.settings):
            t = overrides.get(name, allowed[0])
            meta[name] = {"type": t, "ui_type": "",
                          "options": list(options or []) if t in (3, 4) else None}
        return meta

    def test_a_healthy_table_blocks_nothing(self):
        self.assertEqual(cli._scheduling_blockers(self.settings, self._meta()), [])

    def test_last_updated_built_as_text_is_a_blocker(self):
        blockers = cli._scheduling_blockers(
            self.settings, self._meta(**{self.f.last_updated: 1}))
        self.assertEqual(len(blockers), 1)
        self.assertIn(self.f.last_updated, blockers[0])
        self.assertIn("文本", blockers[0])       # 现在是什么
        self.assertIn("日期", blockers[0])       # 应该是什么

    def test_queued_built_as_text_is_a_blocker(self):
        blockers = cli._scheduling_blockers(
            self.settings, self._meta(**{self.f.queued: 1}))
        self.assertEqual(len(blockers), 1)
        self.assertIn(self.f.queued, blockers[0])

    def test_a_missing_column_is_not_this_guards_business(self):
        """缺列另有护栏（而且缺 `最近检查时间` 时 sweep 本来就拒跑）。
        两道闸各管各的，别互相顶替。"""
        meta = self._meta()
        del meta[self.f.last_updated]
        self.assertEqual(cli._scheduling_blockers(self.settings, meta), [])

    def test_other_mistyped_columns_do_not_block_the_run(self):
        """「流量状态」建错只是这一列落不下来，不该拦下整轮巡查——
        拦得过宽和拦不住一样糟。"""
        self.assertEqual(
            cli._scheduling_blockers(self.settings,
                                     self._meta(**{self.f.traffic_status: 3})),
            [])


class TestMistypedWarning(unittest.TestCase):
    """写回时按类型摘掉的列，得让运营不看代码就知道该去改什么。
    只说「跳过了 1 列」等于让人回头再体检一遍——而钱已经花了。"""

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields

    def test_it_names_the_column_the_actual_type_and_the_wanted_type(self):
        meta = {self.f.traffic_status: {"type": 3, "ui_type": "", "options": []}}
        text = cli._mistyped_warning({self.f.traffic_status}, meta,
                                     self.settings, "西屋第一期")
        self.assertIn(self.f.traffic_status, text)
        self.assertIn("单选", text)      # 现在建成了什么
        self.assertIn("多选", text)      # 应该是什么
        self.assertIn("doctor --table 西屋第一期", text)

    def test_it_survives_a_column_missing_from_the_meta(self):
        text = cli._mistyped_warning({self.f.traffic_status}, None, self.settings)
        self.assertIn(self.f.traffic_status, text)
        self.assertIn("doctor", text)
        self.assertNotIn("--table", text)   # 没表名就别编一个

    def _run_write_back(self, meta) -> int:
        """把一份 meta 喂给写回，拿到退出码。"""
        f = self.f

        class _Outcome:
            tag_plan = None
            record_id = "rec1"
            checked_at = None
            fields = {f.refresh_status: "正常", f.last_updated: 1787313600000,
                      f.queued: False, f.traffic_status: ["大爆"]}

        class _Report:
            fatal = False
            outcomes = [_Outcome()]

            def summary(self):
                return ""

            def checked_span(self, display, *, skip=()):
                return ""

        class _Table:
            def batch_get(self, ids):
                return [{"record_id": i, "fields": {}} for i in ids]

            def batch_update(self, updates, errors=None):
                return len(updates)

        with mock.patch("builtins.print"):
            return cli._write_back_table(_Table(), _Report(), meta, self.settings, "表A")

    def _meta(self, **overrides) -> dict:
        meta = {}
        for name, allowed, _l, options, _n in cli._expected_schema(self.settings):
            t = overrides.get(name, allowed[0])
            meta[name] = {"type": t, "ui_type": "",
                          "options": list(options or []) if t in (3, 4) else None}
        return meta

    def test_a_dropped_column_does_not_exit_green(self):
        """钱花了、那一列没落表，而且不改配置每一轮都会这样。
        让 cron 和 Actions 显示绿色的成功等于把它藏起来。"""
        self.assertEqual(self._run_write_back(self._meta()), 0)
        self.assertEqual(
            self._run_write_back(self._meta(**{self.f.traffic_status: 3})), 1)


class TestSpendCapIsLoudWhenAbsent(unittest.TestCase):
    """没设金额上限时，以前**一个字都不打**。

    `Budget.describe()` 在什么都没设时返回「无上限」，而收尾那行只在
    `!= "无上限"` 时才打印——于是唯一真正能兜住「全表被误勾」「最近检查
    时间列被清空」「上游故障每轮重刷」这三类事故的闸门，没设时是最安静的。
    """

    def test_describe_still_says_unbounded(self):
        from xhsearch.config import Budget
        self.assertEqual(Budget().describe(), "无上限")

    def test_doctor_has_a_budget_section(self):
        import inspect
        source = inspect.getsource(cli.cmd_doctor)
        self.assertIn("MAX_YUAN_PER_RUN", source)
        self.assertIn("没有上界", source)

    def test_a_paid_run_warns_before_spending(self):
        import inspect
        source = inspect.getsource(cli._run_locked)
        head = source[:source.index("for index, (label, table)")]
        self.assertIn("没有金额上界", head,
                      "警告必须在开跑之前打，事后说没有意义")


class TestEveryExitPathReportsTheRunEnd(unittest.TestCase):
    """面板靠「有 run_start 没有 run_end」认出被容器杀掉的那些轮。

    所以每一条**正常**退出路径都必须发 run_end。漏一条，那一轮在看板上
    就长得和「跑到一半被回收」一模一样——一个假的故障信号比没有信号更糟。
    这里按 AST 检查，因为这类遗漏都是「新加了一个 return 忘了改」，
    靠人看 diff 挡不住。
    """

    def _run_locked_ast(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(cli))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_run_locked":
                return node
        self.fail("找不到 cli._run_locked")

    def _returns_in(self, node):
        """_run_locked 自己的 return，不含嵌套函数（_finish 自己那条不算）。"""
        import ast
        found = []
        stack = list(node.body)
        while stack:
            item = stack.pop()
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(item, ast.Return):
                found.append(item)
            for child in ast.iter_child_nodes(item):
                stack.append(child)
        return found

    def test_every_return_goes_through_finish(self):
        import ast
        for node in self._returns_in(self._run_locked_ast()):
            self.assertIsInstance(
                node.value, ast.Call,
                f"cli.py 第 {node.lineno} 行的 return 不是 _finish(...)")
            self.assertEqual(
                getattr(node.value.func, "id", None), "_finish",
                f"cli.py 第 {node.lineno} 行的 return 绕开了 _finish，"
                "这一轮不会发 run_end，看板会把它当成被杀掉的轮子")

    def test_the_raise_path_also_reports(self):
        """刷新阶段炸了也要发一条（带错误类型），否则一次 Python 异常
        和一次 SIGKILL 在看板上分不出来。"""
        import inspect
        source = inspect.getsource(cli._run_locked)
        head, _, tail = source.partition("except BaseException:")
        self.assertTrue(tail, "_run_locked 里的 BaseException 兜底不见了")
        self.assertIn("_finish(1, error=", tail)

    def test_run_start_is_emitted_before_any_table_runs(self):
        import inspect
        source = inspect.getsource(cli._run_locked)
        self.assertIn("EVENT_RUN_START", source)
        self.assertLess(source.index("EVENT_RUN_START"),
                        source.index("for index, (label, table) in enumerate(tables)"),
                        "run_start 要在开跑之前发")


class TestReportedSpanOnlyCoversRowsThatLanded(unittest.TestCase):
    """「已写回 N 行，本轮『最近检查时间』= …」那一行只能报**真的落表**的时刻。

    报一个表里不存在的时刻，就是这次要修的「日志和表对不上」本身。
    """

    def setUp(self):
        self.settings = Settings()
        self.f = self.settings.fields
        self.stamp = datetime(2026, 8, 26, 0, 7, 14, tzinfo=timezone.utc)

    def _report(self):
        report = runner.RunReport()
        for index in range(2):
            report.outcomes.append(runner.Outcome(
                f"rec{index}", runner.STATUS_OK,
                {self.f.refresh_status: "正常",
                 self.f.last_updated: int(self.stamp.timestamp() * 1000)},
                checked_at=self.stamp + timedelta(minutes=index),
            ))
        return report

    def _write_back(self, meta, *, row_error=None):
        settings = self.settings

        class _Table:
            def batch_get(self, ids):
                return [{"record_id": i, "fields": {}} for i in ids]

            def batch_update(self, updates, errors=None):
                if row_error is not None and errors is not None:
                    errors.append(
                        (row_error, feishu.FeishuError(1254005, "record_id 不存在")))
                    return len(updates) - 1
                return len(updates)

        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(
                " ".join(str(x) for x in a))):
            cli._write_back_table(_Table(), self._report(), meta, settings, "表A")
        return "\n".join(printed)

    def _meta(self, **overrides):
        meta = {}
        for name, allowed, _l, options, _n in cli._expected_schema(self.settings):
            t = overrides.get(name, allowed[0])
            meta[name] = {"type": t, "ui_type": "",
                          "options": list(options or []) if t in (3, 4) else None}
        return meta

    def test_all_rows_landed_reports_the_full_span(self):
        out = self._write_back(self._meta())
        self.assertIn("2026-08-26 08:07:14 +08", out)
        self.assertIn("08:08:14", out)

    def test_a_row_that_failed_to_write_is_excluded(self):
        """rec1 没写进去 → 跨度不能把它的时刻当成终点。"""
        out = self._write_back(self._meta(), row_error="rec1")
        self.assertIn("2026-08-26 08:07:14 +08", out)
        self.assertNotIn("08:08:14", out)

    def test_the_span_is_omitted_when_the_timestamp_column_itself_was_blocked(self):
        """「最近检查时间」整列被挡下来（列没建 / 类型建错）= 一行都没盖上。"""
        without_column = self._meta()
        del without_column[self.f.last_updated]
        self.assertNotIn("最近检查时间」=", self._write_back(without_column))
        mistyped = self._meta(**{self.f.last_updated: 1})   # 建成了文本
        self.assertNotIn("最近检查时间」=", self._write_back(mistyped))


if __name__ == "__main__":
    unittest.main()


class TestFirstRunCap(unittest.TestCase):
    """整张表一个「最近检查时间」都没有 = 全新表，或者刚把那一列建出来。
    两种情况下每一行都判到期，一轮就是全表付费。

    这个闸和 MAX_RECORDS_PER_RUN 不是一回事：那个是整次运行共享的，
    这个是**单张新表**的，防的是「一张 800 行的表刚入册就吃掉整轮预算」。
    """

    def test_the_cap_is_read_from_the_environment(self):
        import inspect
        source = inspect.getsource(cli._refresh_table)
        self.assertIn("FIRST_RUN_MAX_RECORDS", source)

    def test_it_only_fires_when_every_row_is_unchecked(self):
        """有一行刷过就说明这张表已经在跑了，不该再当新表限流。"""
        import inspect
        source = inspect.getsource(cli._refresh_table)
        self.assertIn("all(r.last_updated_ms is None for r in row_list)", source)

    def test_zero_disables_it(self):
        import inspect
        source = inspect.getsource(cli._refresh_table)
        self.assertIn("if (first_run_cap and", source,
                      "0 要能关掉这个闸，否则没法一次刷完")


class TestEstimateSaysUnknownNotZero(unittest.TestCase):
    """缺「最近检查时间」列时 load_rows 直接 return []，
    于是 estimate 报「¥0.00」——而真相是算不出来。"""

    def test_the_guard_returns_none_not_zero(self):
        import inspect
        source = inspect.getsource(cli._refresh_table)
        guard = source[source.index("分层刷新没有依据"):]
        self.assertIn("return 1, found, 0, None, None", guard,
                      "花费要返回 None（未知），不是 0.0")

    def test_the_total_line_counts_the_unestimatable_tables(self):
        import inspect
        source = inspect.getsource(cli._run_locked)
        self.assertIn("unknown_cost", source)
        self.assertIn("无法估算", source)
