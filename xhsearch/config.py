"""列名映射与判定口径。

这个文件是整套东西唯一需要按你们实际表结构改的地方。列名必须与多维表格
表头**逐字相同**（飞书按名字寻址，差一个空格就报 1254045 FieldNameNotFound）。

判定阈值（爆文/风控）没有通用答案，默认值是占位的，上线前必须按你们自己的
历史数据校准一次。口径没定死就上生产，是这类监控表最大的返工来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


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
    negative_keywords: str = "负面词"         # 多选或文本，格式同「评论关键词」：
                                              # 负面词 + 竞品词。命中的是**别人**写的东西，
                                              # 和「评论关键词」查自己的种子评论正好相反
    monitoring: str = "是否巡查"              # 复选框；取消勾选即停止刷新
    queued: str = "排队刷新"                  # 复选框；勾上=手动请求刷新，机器处理完自动清掉

    # —— 机器写入 · 运营主视图 ——
    platform: str = "平台"
    comment_count: str = "实时数据.评论数"
    previous_comment_count: str = "上次评论数"
    # 这一行**第一次**被判定为起量的那一次巡查时刻（判据见 Thresholds.surged）。
    # 写一次就不再改：它回答的是「什么时候起来的」，那是个历史事实。
    # 第二波、第三波由「评论增量」和热度档位表达，不该把第一次的答案覆盖掉。
    # ⚠️ 分辨率受分层刷新节奏限制：写进去的是**发现**起量的那一刻，
    # 真正起飞发生在上一次巡查到这一次之间（0-2 天档 8 小时，往后更粗）。
    surge_time: str = "起量时间"
    # 「点赞数 / 上次点赞数 / 收藏数 / 上次收藏数」四列已经去掉了。
    # 小红书的赞藏只能从 detail 接口拿（评论接口不带），而 detail 占了
    # 月成本的 39%——运营确认这两个数字不看，那就没有理由继续为它付费。
    # 判定链路本来也只用评论数（见 Thresholds），赞藏一直只是参考值。
    pinned_status: str = "置顶状态"           # 单选：置顶成功/置顶掉了/无置顶。
                                              # 自家帖子置顶必为我方，只记状态不记内容；
                                              # 抖音接口没有置顶字段，抖音行不写
    comment_status: str = "评论状态"          # 单选，由关键词命中驱动直接覆盖：
                                              # 命中=显示评论，未命中=没有显示；
                                              # 没填关键词的行不碰
    comment_digest: str = "评论区快照"        # 命中的那条评论排最前并带「命中」标记，
                                              # 其余按 置顶优先 接在后面
    negative_status: str = "负面状态"         # 单选，由「负面词」命中驱动直接覆盖：
                                              # 有负面 / 无负面；没填负面词的行不碰
    negative_digest: str = "负面评论快照"     # 只放命中负面词的那几条，每条标出命中的是哪个词。
                                              # 判定过但一条没中时写「（未命中）」——
                                              # 空单元格要留给「这一轮压根没查过」
    traffic_status: str = "流量状态"          # 多选，人机共用（爆帖预备等人工值不碰）
    refresh_status: str = "巡查状态"
    failure_reason: str = "诊断信息"
    last_updated: str = "最近检查时间"
    alive_confirmed: str = "已确认存活"       # 复选框：本轮真的量到数字（哪怕是0）=勾上，确认失效=取消

    # —— 机器写入 · 系统列（建议在运营视图里隐藏）——
    consecutive_failures: str = "连续失败次数"   # 两击定罪的计数器

    def must_read(self) -> list[str]:
        """search 时必须拉回来的列。少读一个就会写错。"""
        return [
            self.link,
            self.publish_time,
            self.seed_keywords,
            self.negative_keywords,       # 负面/竞品词，和种子词共用同一份评论页
            self.monitoring,
            self.queued,
            self.traffic_status,          # 合并多选必须先读现值
            self.comment_count,           # 判定掉量必须知道上一次的数
            self.last_updated,            # 分层刷新靠它判断到期
            self.consecutive_failures,    # 两击定罪
            self.pinned_status,           # 「掉了」和「从来没有」的区分全看这列的历史
            self.surge_time,              # 已经写过就不再改，得先知道那一格空不空
        ]


@dataclass
class Tags:
    """机器管辖的标签。必须穷举——漏一个，那个标签就再也撤不回来。

    热度档位（观察中 / 无水花 / 评估中 / 爆贴 / 大爆）是**互斥**的：一条帖子
    同时挂着几个没有意义，所以每轮只留最高的那一个。而且只升不降（棘轮）——
    爆过就是爆过，评论被删导致数字掉下去不该让它从「大爆」退回「爆贴」，
    那种异常该由 疑似限流 表达。

    「观察中」是最低档，代表**还没到下结论的时候**：发布不满
    Thresholds.flop_hours（默认 48 小时）且评论数还够不上「评估中」。
    这一档存在的唯一理由是**不留空格**——它出现之前，冷启动窗口内的新帖
    一个标签都不打，`流量状态` 那一格是空的，而空格在运营眼里有三种读法
    （还没巡查 / 巡查了没结论 / 机器坏了），分不出来就只能一条条手工去填。
    到点之后它会自动升成「无水花」或更高档，不需要任何人去清。
    留空值（`TAG_OBSERVING=`）可以关掉这一档，行为退回从前。

    风控中只认两种**硬证据**：上游返回了审查/受限标记，或链接已失效
    （两击定罪之后）。评论数的异常都不进风控——腰斩是 疑似限流，
    起不来是 无水花，口径分开，运营才知道每个标签背后是什么证据。

    风控中 / 疑似限流反映**当前状态**，每轮重算，恢复正常要能自动摘掉，
    否则表会越来越红，最后没人看。
    """

    observing: str = "观察中"
    flop: str = "无水花"
    evaluating: str = "评估中"
    hot: str = "爆贴"
    super_hot: str = "大爆"
    risk: str = "风控中"
    throttled: str = "疑似限流"

    # 已退役的机器标签：不再产出，但仍算机器管辖——留在 namespace 里，
    # 旧行上残留的「已失效」才会在下一轮被自动摘掉（失效现在并入「风控中」）。
    # 关掉「观察中」时它的默认名也会被追加进来（见 cli.build_settings），
    # 否则已经写出去的那些「观察中」会永远撤不回来。
    retired: tuple[str, ...] = ("已失效",)

    def heat_tiers(self) -> list[str]:
        """热度档位，由低到高。互斥，同时只留一个。

        空名字 = 这一档被关掉了，不参与排序也不产出（见 observing）。
        """
        return [t for t in (self.observing, self.flop, self.evaluating,
                            self.hot, self.super_hot) if t]

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
class NegativeStatus:
    """「负面状态」单选列的取值：第一页评论里有没有出现负面词或竞品词。

    和「评论状态」是同一套机制、**相反的方向**：
    评论状态查的是「我们自己种的评论显示出来没有」（希望命中），
    负面状态查的是「别人有没有在底下说难听的、或者提竞品」（不希望命中）。

    两者共用**同一份**第一页评论快照，不额外发任何请求、不翻页——
    多一列判定不该多花一分钱。代价要说清楚：只看得到第一页，
    埋在第二页往后的负面评论这里看不见，这一列是**预警**不是**普查**。

        有负面  —— 任一负面词/竞品词出现在第一页任一条评论里
        无负面  —— 配了负面词，但第一页一条都没命中

    没填负面词的行、以及本轮没看到评论页内容的行完全不碰这一列。
    """

    found: str = "有负面"
    clean: str = "无负面"

    def machine_written(self) -> list[str]:
        return [self.found, self.clean]


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

    热度按评论数分三档，互斥，取最高：
        ≥ 20 → 评估中
        ≥ 50 → 爆贴
        ≥ 100 → 大爆

    够不上 20 条的行由**发布时长**决定落在哪一档：
        发布不满 flop_hours → 观察中（还在冷启动窗口，没到下结论的时候）
        发布满 flop_hours   → 无水花（给了足够时间还是没起来）

    另外两组是**和上一次比**的口径，一涨一跌，互为镜像，**两边都含边界**：
        跌幅 ≥ risk_drop_ratio（基线 ≥ 20）→ 疑似限流
        涨幅 ≥ surge_ratio 且增量 ≥ surge_min_gain → 起量，记一个时刻
    正好 50% 那一档算命中（100 → 50 判限流，40 → 60 判起量）。
    """

    tier_evaluating: int = 20
    tier_hot: int = 50
    tier_super_hot: int = 100

    # 评论数相对上次下跌超过这个比例，判定疑似风控（限流/删评/折叠）。
    risk_drop_ratio: float = 0.5
    # 上次评论数低于这个值时不做掉量判定——从 3 掉到 1 没有意义。
    risk_drop_min_baseline: int = 20

    # —— 起量：掉量那一对的镜像，两个闸门都要过 ——
    # 涨幅比例够 + 绝对增量够，才算「这一轮起来了」，时刻写进「起量时间」。
    # 两个都是**含边界**（≥），和掉量那边的 `<=` 对称：正好涨 50% 也算。
    surge_ratio: float = 0.5
    surge_min_gain: int = 20

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

    def surged(self, count: int | None, previous: int | None) -> bool:
        """这一轮算不算「起量」——涨幅和绝对增量**两个闸门都要过**。

        `previous is None` 一律判否，这一条最要紧：那是**第一次量这一行**
        （「上次评论数」还空着），手里只有一个孤零零的数字，没有任何速率信息。
        一张刚入册的老表里每条都有几百条评论，漏了这一条会给全表盖上同一个
        「起量时间」——正好是入册那天，而且是错的。

        两个闸门的分工，照掉量那一对的镜像来读：

        * 比例闸 `surge_ratio` 挡住「大体量帖子的正常增长」——
          1000 涨到 1100 是日常波动，不是起飞。
        * 绝对闸 `surge_min_gain` 挡住「小基数的假暴涨」——
          2 涨到 6 是 +200%，但那是三条评论的事。
          掉量那边用的是**基线**下限（跌之前得先有量），起量这边必须换成
          **增量**下限：起飞的基数按定义就是低的，拿基线卡会把真正的起量全滤掉。

        比例写成乘法不是除法：`previous` 为 0（上一轮真的量到 0 条）时
        除法要炸，而乘法天然退化成「只看绝对增量」，正是想要的语义。

        两个闸门都**含边界**（`>=`），和掉量那边的 `<=` 对称：100 → 50
        算腰斩，那么 40 → 60 也该算起量。散文里写「涨幅超 50%」是不准的，
        文档一律写 `≥`——边界差一档就够让一条真起量的行错过它唯一一次盖戳。
        """
        if count is None or previous is None:
            return False
        return (count >= previous * (1 + self.surge_ratio)
                and count - previous >= self.surge_min_gain)


@dataclass
class DigestFormat:
    """评论区快照的排版。计费按页不按条，所以能存多少存多少。

    ⚠️ 数据最小化：这一列会把**别人写的评论正文、昵称和 IP 属地**长期存进
    飞书。这些是个人信息，保留期、访问范围和跨境传输都该由你们的法务/信息
    安全先过一遍（见 docs/待验证清单.md）。两个开关给的是「不必改代码就能
    收窄」的能力：

        show_author_name=False   不落库昵称，只留评论正文
        show_ip_location=False   不落库 IP 属地

    环境变量 DIGEST_SHOW_AUTHOR_NAME / DIGEST_SHOW_IP_LOCATION 可以覆盖。
    默认保持原行为（都写），因为改默认值会让已经在跑的表突然少两列信息；
    要不要收窄是业务/合规决定，不是代码能替人做的决定。
    """

    max_comments: int = 8
    per_comment_chars: int = 40
    total_chars: int = 700
    show_like_count: bool = True
    show_ip_location: bool = True
    show_author_name: bool = True


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

    # 小样本的第二道熔断：比例闸门在样本 <10 时完全不生效，而 queue 模式
    # 的一批经常只有三五行。如果这一批里**每一行都失败、且失败原因是同一个
    # 错误码**，那是上游 schema 漂移的典型形态（比如所有笔记都被译成
    # empty_shell），不是这三五条内容恰好同时被删。达到这个行数就熔断。
    breaker_uniform_min_sample: int = 3

    # 同一行在这个时间窗内刚成功刷过就跳过，不花积分。
    # 有人连点 200 次按钮 = 1 次真实调用。
    cooldown_seconds: int = 90


@dataclass
class Budget:
    """单次运行的硬预算。**在发请求之前**预留，不是事后统计。

    没有它的时候，单轮费用是没有上界的：所有行同时被勾上「排队刷新」、
    首次上线全表到期、「最近检查时间」列被误清空，任意一个都会让一轮
    把整张表刷一遍。文档里写着「queue 每轮 ≤40 行」，代码里从来没有这条限制。

    三个闸门任一触顶就停止派发新行，**已到手的结果照常写回**，
    没轮到的行保持 queued / 到期状态，下一轮自然继续。

    0 = 不限（保持老行为）。默认给 records 一个上界、金额留给部署方按
    自己的规模填——一个拍脑袋的金额上限比没有上限更容易误伤。

    **三个闸门的强度不一样，别当成一回事：**

    * `max_yuan_per_run` 是**硬上限**。预留按「这一行可能走到的最贵那家」算，
      跑完再按实际开销退还差额，所以任何一行都不可能把它顶穿。
    * `max_calls_per_run` 记的是**实际发出的 HTTP 请求数**（含传输层重试和
      降级到备胎的那几次）。预留按「计划调用数 × 可用通道数」算，覆盖降级；
      但传输层的重试（最多 3 次，只在网络故障/5xx 上发生）无法提前预留，
      所以单行最多会超出它自己的重试次数，超出部分在下一行的闸门上体现。
      要卡死钱就用金额闸门。
    * `max_records_per_run` 是精确的行数闸。
    """

    max_records_per_run: int = 0
    max_calls_per_run: int = 0
    max_yuan_per_run: float = 0.0

    def describe(self) -> str:
        parts = []
        if self.max_records_per_run:
            parts.append(f"最多 {self.max_records_per_run} 行")
        if self.max_calls_per_run:
            parts.append(f"最多 {self.max_calls_per_run} 次调用")
        if self.max_yuan_per_run:
            parts.append(f"最多 ¥{self.max_yuan_per_run:.2f}")
        return "、".join(parts) or "无上限"


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
class Display:
    """日志里时间怎么显示。**只影响打印，不影响任何判定或写入的值。**

    这一段存在的理由是「日志和表对不上」这个具体的坑：

    * 表里的「最近检查时间」是飞书按**租户时区**渲染的（国内租户 = 北京时间）
    * Railway / GitHub Actions 的日志时间戳是**容器的 UTC**

    两边差 8 小时，谁去对都会觉得对不上。所以运行日志里的每一个时间
    都按这里的偏移打印一遍，让日志里看到的时刻和表里那一格**逐字相同**。

    用固定偏移而不是 IANA 时区名，是因为 `zoneinfo` 依赖系统 tzdata，
    精简容器里经常没有——一个日志格式化的小功能不该让整轮跑不起来。
    中国不实行夏令时，固定 +8 就是正确答案；别的时区按需改
    `DISPLAY_UTC_OFFSET`（支持 5.5 这样的半小时偏移）。
    """

    utc_offset_hours: float = 8.0

    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours))

    def label(self) -> str:
        """时区标签，例如 +08 / +05:30 / -03。"""
        total = round(self.utc_offset_hours * 60)
        sign = "-" if total < 0 else "+"
        hours, minutes = divmod(abs(total), 60)
        return f"{sign}{hours:02d}" + (f":{minutes:02d}" if minutes else "")

    def clock(self, moment: datetime) -> str:
        """只有时分秒。给逐行进度用——同一轮里日期不会变，占宽度没意义。"""
        return moment.astimezone(self.tz()).strftime("%H:%M:%S")

    def stamp(self, moment: datetime) -> str:
        """完整时刻 + 时区标签。给一轮的开跑/收尾这种关键节点用。"""
        return f"{moment.astimezone(self.tz()):%Y-%m-%d %H:%M:%S} {self.label()}"


@dataclass
class Settings:
    fields: FieldNames = field(default_factory=FieldNames)
    tags: Tags = field(default_factory=Tags)
    comment_status: CommentStatus = field(default_factory=CommentStatus)
    negative_status: NegativeStatus = field(default_factory=NegativeStatus)
    pin_status: PinStatus = field(default_factory=PinStatus)
    thresholds: Thresholds = field(default_factory=Thresholds)
    digest: DigestFormat = field(default_factory=DigestFormat)
    refresh: RefreshTiers = field(default_factory=RefreshTiers)
    safety: Safety = field(default_factory=Safety)
    channels: Channels = field(default_factory=Channels)
    budget: Budget = field(default_factory=Budget)
    display: Display = field(default_factory=Display)

    # 小红书笔记发布多少天内额外调一次 detail。**默认 0 = 不调。**
    #
    # 这个调用原本只为「点赞数/收藏数」两列而存在，而那四列已经去掉了
    # （运营确认不看）。它占月成本的 39%——每天 100 条笔记的量级下
    # 是 ¥2,592/月，买的东西全部落不到表里，所以默认关掉。
    #
    # 关掉之后**小红书**失去两样（抖音不受影响，它的 detail 是恒定追加的）：
    # 1. 上游的 in_censor 审核标记 → 「风控中」少一条证据来源。
    #    影响有限：这个标记的语义一直没实地验过（只见过 false），
    #    而链接失效那条硬证据仍然在。见 docs/待验证清单.md。
    # 2. 「确定性」的死讯。detail 返回空 data 是 definitive=True，一轮定罪；
    #    只有评论接口时退回 definitive=False 的空壳启发式，走两击定罪，
    #    在 0-2 天档（每 8 小时一轮）意味着晚约 16 小时确认。
    #
    # 判定链路一点没变：打标签、评论状态、负面词、置顶全都只看评论接口。
    # 想要回来就把这个值调回 7（或 2 —— 风控和删帖最可能发生在头两天）。
    detail_within_days: int = 0

    # SocialDataX 官方 skill 明文要求最多 3 并发，不要突发请求。留一档余量。
    max_concurrency: int = 2

    # 单次运行的软截止。给受执行时限约束的运行时准备（曾经的扣子代码
    # 节点硬上限 60 秒，默认 45 由此而来）；Railway 独立服务跑批量时
    # cli 会用环境变量 SOFT_DEADLINE_SECONDS（默认 0 = 不限）覆盖。
    soft_deadline_seconds: float = 45.0
