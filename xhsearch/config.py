"""列名映射与判定口径。

这个文件是整套东西唯一需要按你们实际表结构改的地方。列名必须与多维表格
表头**逐字相同**（飞书按名字寻址，差一个空格就报 1254045 FieldNameNotFound）。

判定阈值（爆文/风控）没有通用答案，默认值是占位的，上线前必须按你们自己的
历史数据校准一次。口径没定死就上生产，是这类监控表最大的返工来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FieldNames:
    """多维表格列名。左边是代码里的角色，右边是表头文字。

    默认值按 OKMAN 系列表的实际结构对齐（2026-08 以「OKMAN第一期」为准）：
    巡查三件套（是否巡查/巡查状态/最近检查时间）由本系统接管，
    「实时数据.评论数」是评论数落点。
    表里还没有的列（评论关键词、置顶状态、上次点赞数等）要先在飞书里建好——
    doctor 会列出缺哪些；没建的列会被自动跳过并在日志里提示，不会写坏表。

    ⚠️ 「蓝词字段」机器**完全不读不写**：蓝词（评论里变成超链接的词）
    是人工在手机端自查的，和「评论关键词」（查我们自己的种子评论有没有
    显示出来）是两回事。接口数据里目前拿不到「这个词有没有变成超链接」，
    所以两者没有任何关联（见 docs/待验证清单.md）。
    """

    # —— 人工维护 ——
    link: str = "反馈链接"
    publish_time: str = "发布时间"
    seed_keywords: str = "评论关键词"         # 多选或文本（顿号/逗号分隔）：一组词，
                                              # 任一出现在第一页任一条评论里 = 我们的
                                              # 评论显示出来了。注意不是「蓝词字段」！
    monitoring: str = "是否巡查"              # 复选框；取消勾选即停止刷新
    queued: str = "排队刷新"                  # 复选框；勾上=手动请求刷新，机器处理完自动清掉

    # —— 机器写入 · 运营主视图 ——
    platform: str = "平台"
    comment_count: str = "实时数据.评论数"
    previous_comment_count: str = "上次评论数"
    like_count: str = "点赞数"
    previous_like_count: str = "上次点赞数"
    collect_count: str = "收藏数"
    previous_collect_count: str = "上次收藏数"
    pinned_status: str = "置顶状态"           # 单选：置顶成功/置顶掉了/无置顶。
                                              # 自家帖子置顶必为我方，只记状态不记内容；
                                              # 抖音接口没有置顶字段，抖音行不写
    comment_status: str = "评论状态"          # 单选，由关键词命中驱动直接覆盖：
                                              # 命中=显示评论，未命中=没有显示；
                                              # 没填关键词的行不碰
    comment_digest: str = "评论区快照"        # 命中的那条评论排最前并带「命中」标记，
                                              # 其余按 置顶优先 接在后面
    traffic_status: str = "流量状态"          # 多选，人机共用（爆帖预备等人工值不碰）
    refresh_status: str = "巡查状态"
    failure_reason: str = "诊断信息"
    last_updated: str = "最近检查时间"
    alive_confirmed: str = "已确认存活"       # 复选框：本轮取到数=勾上，确认失效=取消

    # —— 机器写入 · 系统列（建议在运营视图里隐藏）——
    consecutive_failures: str = "连续失败次数"   # 两击定罪的计数器

    def must_read(self) -> list[str]:
        """search 时必须拉回来的列。少读一个就会写错。"""
        return [
            self.link,
            self.publish_time,
            self.seed_keywords,
            self.monitoring,
            self.queued,
            self.traffic_status,          # 合并多选必须先读现值
            self.comment_count,           # 判定掉量必须知道上一次的数
            self.like_count,              # 搬「上次点赞数」要先读现值
            self.collect_count,           # 搬「上次收藏数」同理
            self.last_updated,            # 分层刷新靠它判断到期
            self.consecutive_failures,    # 两击定罪
            self.pinned_status,           # 「掉了」和「从来没有」的区分全看这列的历史
        ]


@dataclass
class Tags:
    """机器管辖的标签。必须穷举——漏一个，那个标签就再也撤不回来。

    热度档位（无水花 / 评估中 / 爆贴 / 大爆）是**互斥**的：一条帖子同时
    挂着几个没有意义，所以每轮只留最高的那一个。而且只升不降（棘轮）——
    爆过就是爆过，评论被删导致数字掉下去不该让它从「大爆」退回「爆贴」，
    那种异常该由 疑似限流 表达。「无水花」是发出去够久还起不来的最低档。

    风控中只认两种**硬证据**：上游返回了审查/受限标记，或链接已失效
    （两击定罪之后）。评论数的异常都不进风控——腰斩是 疑似限流，
    起不来是 无水花，口径分开，运营才知道每个标签背后是什么证据。

    风控中 / 疑似限流反映**当前状态**，每轮重算，恢复正常要能自动摘掉，
    否则表会越来越红，最后没人看。
    """

    flop: str = "无水花"
    evaluating: str = "评估中"
    hot: str = "爆贴"
    super_hot: str = "大爆"
    risk: str = "风控中"
    throttled: str = "疑似限流"

    # 已退役的机器标签：不再产出，但仍算机器管辖——留在 namespace 里，
    # 旧行上残留的「已失效」才会在下一轮被自动摘掉（失效现在并入「风控中」）。
    retired: tuple[str, ...] = ("已失效",)

    def heat_tiers(self) -> list[str]:
        """热度档位，由低到高。互斥，同时只留一个。"""
        return [self.flop, self.evaluating, self.hot, self.super_hot]

    def namespace(self) -> list[str]:
        """机器管辖的全部标签（含退役标签）。merge 的可撤回范围。"""
        return [*self.heat_tiers(), self.risk, self.throttled, *self.retired]

    def machine_written(self) -> list[str]:
        """机器仍会写的标签。doctor 只要求表里建这些选项，退役的不用建。"""
        return [*self.heat_tiers(), self.risk, self.throttled]

    def rank(self, tag: str) -> int:
        """热度档位的高低。不是热度标签返回 -1。"""
        tiers = self.heat_tiers()
        return tiers.index(tag) if tag in tiers else -1


@dataclass
class CommentStatus:
    """「评论状态」单选列的取值：我们的种子评论有没有显示出来。

    判定完全由「评论关键词」的命中结果驱动，机器**直接覆盖**当前值
    （单选只显示当前状态，「待评论」这类人工排期旧值一律被结论覆盖）：

        显示评论  —— 任一关键词出现在第一页任一条评论里
        没有显示  —— 配了关键词，但第一页一条都没命中

    没填关键词的行、以及本轮没看到评论页内容的行机器完全不碰这一列。
    两个平台都能判——匹配的是第一页评论内容，不依赖置顶字段，
    所以抖音行同样有效。
    """

    displayed: str = "显示评论"
    not_displayed: str = "没有显示"

    def machine_written(self) -> list[str]:
        """机器会写入的值——doctor 检查选项是否建好用这个清单。"""
        return [self.displayed, self.not_displayed]


@dataclass
class PinStatus:
    """「置顶状态」单选列的取值：我们的置顶评论还在不在。

    自家帖子置顶必为我方，所以只记状态、不记内容，机器直接覆盖：

        置顶成功  —— 本轮第一页有置顶评论
        置顶掉了  —— 此前置顶成功过（本列历史是 成功/掉了），现在没了
        无置顶    —— 从来没成功过，现在也没有

    「掉了」和「从来没有」的区分全看这一列自己的历史；掉了之后一直
    没恢复保持「置顶掉了」，不退回「无置顶」——曾经置顶过这件事不抹掉。
    只有小红书能判（抖音评论接口没有置顶字段），抖音行完全不碰这一列；
    本轮没看到评论页内容的空壳轮也不碰、不误报。
    """

    pinned_ok: str = "置顶成功"
    pinned_lost: str = "置顶掉了"
    never_pinned: str = "无置顶"

    def machine_written(self) -> list[str]:
        return [self.pinned_ok, self.pinned_lost, self.never_pinned]

    def ever_pinned(self, current: str) -> bool:
        return current in (self.pinned_ok, self.pinned_lost)


@dataclass
class Thresholds:
    """判定口径。

    热度三档互斥，取最高：
        ≥ 20 → 评估中
        ≥ 50 → 爆贴
        ≥ 100 → 大爆
    """

    tier_evaluating: int = 20
    tier_hot: int = 50
    tier_super_hot: int = 100

    # 评论数相对上次下跌超过这个比例，判定疑似风控（限流/删评/折叠）。
    risk_drop_ratio: float = 0.5
    # 上次评论数低于这个值时不做掉量判定——从 3 掉到 1 没有意义。
    risk_drop_min_baseline: int = 20

    # 发布这么多小时后评论数仍够不上「评估中」门槛，判「无水花」。
    # 太短会把正常冷启动误标——刚发两小时只有几条评论再正常不过。
    flop_hours: int = 48

    def heat_tier(self, count: int, tags: "Tags") -> str | None:
        """按评论数算热度档位。返回 None 表示还够不上最低档。"""
        if count >= self.tier_super_hot:
            return tags.super_hot
        if count >= self.tier_hot:
            return tags.hot
        if count >= self.tier_evaluating:
            return tags.evaluating
        return None


@dataclass
class DigestFormat:
    """评论区快照的排版。计费按页不按条，所以能存多少存多少。"""

    max_comments: int = 8
    per_comment_chars: int = 40
    total_chars: int = 700
    show_like_count: bool = True
    show_ip_location: bool = True


@dataclass
class RefreshTiers:
    """分层刷新：越新的帖子刷得越勤。

    这不是一套调度代码，就是筛选时的一个条件：
    「发布时间落在这一层 且 最后更新时间早于 now - interval_hours」= 该刷了。
    """

    tiers: list[tuple[int, int]] = field(
        default_factory=lambda: [
            (2, 8),      # 发布 0-2 天：每 8 小时一次（风控和置顶的关键窗口）
            (7, 24),     # 3-7 天：每天一次
            (30, 72),    # 8-30 天：每 3 天一次
        ]
    )
    archive_after_days: int = 30     # 超过就不再自动刷，只保留手动触发

    def interval_hours_for_age(self, age_days: float) -> int | None:
        """返回该年龄的帖子应有的刷新间隔；None 表示已归档。

        归档界线由 archive_after_days 决定；tiers 只决定归档前的刷新节奏，
        超出最后一档年龄但还没到归档线的，沿用最后一档的间隔。
        （默认两者都是 30 天，行为不变；把 archive_after_days 调大才有差别。）
        """
        if age_days > self.archive_after_days:
            return None
        for max_age_days, interval_hours in self.tiers:
            if age_days <= max_age_days:
                return interval_hours
        return self.tiers[-1][1] if self.tiers else None


@dataclass
class Safety:
    """防误伤参数。这一组的每个默认值都是为了「宁可少报，不可错报」。"""

    # 连续多少次取不到才敢判「已失效」。一次网络抖动就把好帖子标成风控，
    # 运营会全线停投，信任一旦丢了就补不回来。
    strikes_before_gone: int = 2

    # 一批里判定为「失效」的比例超过这个数（且样本够大）就整批作废。
    # 几百条笔记不可能在同一小时里被集体删除 —— 那是上游故障或话术改版。
    breaker_gone_ratio: float = 0.2
    breaker_min_sample: int = 10

    # 同一行在这个时间窗内刚成功刷过就跳过，不花积分。
    # 有人连点 200 次按钮 = 1 次真实调用。
    cooldown_seconds: int = 90


@dataclass
class Channels:
    """双通道：每个平台走哪几家数据供应商，按顺序降级。

    默认两个平台都优先 TikHub，SocialDataX 作备胎。理由（都是实测的，
    完整对比见 docs/供应商对比.md）：

    * 抖音便宜 93%（¥0.10 → ¥0.0072 一次），小红书便宜 28%
    * 两家需要的字段都拿得到——TikHub 的置顶标记藏在 `show_tags_v2` 里，
      名字不叫 is_pinned，但确实有
    * SocialDataX 的错误码是有契约的（1006 封控 / 1008 已删除，
      厂商自己标了「不要重试」），TikHub 没有这张表。所以它更适合当备胎：
      平时不花钱，主通道挂了或余额空了立刻顶上

    降级只在**主通道自己有问题**时发生（网络故障、Key 失效、余额耗尽），
    不会因为「这条笔记没了」而去第二家再花一次钱——那是行级结论，不是故障。

    只想用一家：把列表写成单元素即可，行为和改造前完全一致。
        settings.channels.order = {"xhs": ["socialdatax"], "douyin": ["socialdatax"]}
    """

    order: dict[str, list[str]] = field(default_factory=lambda: {
        "xhs": ["tikhub", "socialdatax"],
        "douyin": ["tikhub", "socialdatax"],
    })

    def for_platform(self, platform: str) -> list[str]:
        return list(self.order.get(platform) or ["socialdatax"])

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for names in self.order.values():
            for name in names:
                if name not in seen:
                    seen.append(name)
        return seen

    def primary(self, platform: str) -> str:
        names = self.for_platform(platform)
        return names[0] if names else "socialdatax"


@dataclass
class Settings:
    fields: FieldNames = field(default_factory=FieldNames)
    tags: Tags = field(default_factory=Tags)
    comment_status: CommentStatus = field(default_factory=CommentStatus)
    pin_status: PinStatus = field(default_factory=PinStatus)
    thresholds: Thresholds = field(default_factory=Thresholds)
    digest: DigestFormat = field(default_factory=DigestFormat)
    refresh: RefreshTiers = field(default_factory=RefreshTiers)
    safety: Safety = field(default_factory=Safety)
    channels: Channels = field(default_factory=Channels)

    # 小红书笔记发布多少天内额外调一次 detail 拿点赞/收藏。
    # 设为 0 表示完全不调 detail（省一半钱，代价是没有爆文的点赞维度）。
    detail_within_days: int = 7

    # SocialDataX 官方 skill 明文要求最多 3 并发，不要突发请求。留一档余量。
    max_concurrency: int = 2

    # 单次运行的软截止。给受执行时限约束的运行时准备（曾经的扣子代码
    # 节点硬上限 60 秒，默认 45 由此而来）；Railway 独立服务跑批量时
    # cli 会用环境变量 SOFT_DEADLINE_SECONDS（默认 0 = 不限）覆盖。
    soft_deadline_seconds: float = 45.0
