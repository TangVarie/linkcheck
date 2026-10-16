#!/usr/bin/env python3
"""C1 核对：链接 ID 里的时间，和表里填的真实「发布时间」到底差多少。

    python3 tools/check_id_time.py              # 所有登记的表
    python3 tools/check_id_time.py --table A,B  # 只看这几张

**只读飞书，一个付费请求都不发，不写表。**

为什么要核对：「发布时间」空着的行，机器按链接 ID 推一个**最晚可能**的发布
时刻（ID 时间 + INFERRED_PUBLISH_MARGIN_DAYS，默认 14 天）来分层、归档。
ID 是**创建**时刻——定时发布的笔记在排期那一刻就有 ID，真正发出去要晚几天。
所以那 14 天就是「定时发布最多提前几天」的假设，这个脚本拿你们自己的表验它：

    发布时间 − ID 时间  ≤ 14 天   → 假设成立，空着发布时间的行不会被提前归档
    发布时间 − ID 时间  > 14 天   → 这种帖子要是发布时间没填，会被提前归档
    发布时间 − ID 时间  < −1 天   → 不该出现：多半是发布时间填错了或链接贴错了

（−1 到 0 天是正常的：「发布时间」常常只填到天，记成当天 0 点，ID 时间是
当天的某个钟点。）

只有表里两样都有的行才算样本：发布时间填了、链接里解得出 ID（完整链接，
或者抖音短链已经记进「作品ID」的）。小红书短链（xhslink）解不出，不算。
"""

from __future__ import annotations

import math
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载、表清单、逐表阈值
from xhsearch import runner  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.rows import Row  # noqa: E402

# 「发布时间」只填到天时，ID 时间最多比它晚这么多（当天 23:59 发的帖子）。
DATE_ONLY_SLACK_DAYS = 1.0
# 晚于 ID 时间超过这么多天，多半是定时发布。
SCHEDULED_DAYS = 1.0
# 样本少于这么多行，不给建议值——几行数据定不了一个全局参数。
MIN_SAMPLE = 30
# 每一类异常最多列几条，免得几千行的表把屏幕刷满。
SHOW = 8


@dataclass
class Sample:
    table: str
    record_id: str
    gap_days: float     # 发布时间 − ID 时间


@dataclass
class TableCount:
    table: str
    rows: int = 0
    with_publish: int = 0
    with_both: int = 0
    no_id: int = 0      # 发布时间填了，链接里解不出 ID（短链）


@dataclass
class Findings:
    counts: list[TableCount] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    # 读不到的表。余量对**所有**表生效，有一张没看过，建议值就不作数。
    unreadable: list[str] = field(default_factory=list)


def collect(findings: Findings, label: str, rows: list[Row],
            now: Optional[datetime] = None) -> None:
    """把一张表的行加进样本。**纯函数，不发请求。**"""
    now = now or datetime.now(timezone.utc)
    count = TableCount(label)
    for row in rows:
        count.rows += 1
        if row.publish_time_ms is None:
            continue
        count.with_publish += 1
        created_ms = row.id_publish_ms(now)
        if created_ms is None:
            count.no_id += 1
            continue
        count.with_both += 1
        findings.samples.append(Sample(
            label, row.record_id, (row.publish_time_ms - created_ms) / 86_400_000))
    findings.counts.append(count)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """最近秩法：样本里排在这个比例上的那一个值。"""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, math.ceil(fraction * len(sorted_values)) - 1))
    return sorted_values[index]


def recommend(samples: list[Sample], margin_days: int) -> tuple[Optional[int], str]:
    """给一个建议的 INFERRED_PUBLISH_MARGIN_DAYS，和一句为什么。

    样本不够返回 (None, 原因)。建议值 = 最大的「晚于 ID」再多留一天，
    至少 2 天（发布时间只填到天的那一天误差 + 一天余量）。
    """
    if len(samples) < MIN_SAMPLE:
        return None, (f"样本只有 {len(samples)} 行（至少要 {MIN_SAMPLE} 行），"
                      "先别改，多攒些填了发布时间的行再跑一次")
    worst = max(s.gap_days for s in samples)
    suggested = max(2, math.ceil(max(worst, 0.0)) + 1)
    if worst > margin_days:
        over = sum(1 for s in samples if s.gap_days > margin_days)
        if suggested > cli.INFERRED_MARGIN_MAX_DAYS:
            # 照着改服务会起不来；而晚两个月发的帖子，多半是发布时间填错了。
            return None, (f"有 {over} 行的发布时间比 ID 时间晚了 {margin_days} 天以上（最多 "
                          f"{worst:.1f} 天），要罩住得设 {suggested}，超过了上限 "
                          f"{cli.INFERRED_MARGIN_MAX_DAYS} 天。晚这么多多半是发布时间填错了"
                          "（年份、月份），先去表里看上面列出的那几行")
        return suggested, (f"有 {over} 行的发布时间比 ID 时间晚了 {margin_days} 天以上（最多 "
                           f"{worst:.1f} 天）：现在的 {margin_days} 天不够，这种帖子要是没填"
                           f"发布时间会被提前归档。建议调大到 {suggested}")
    if suggested < margin_days:
        return suggested, (f"最多只晚 {worst:.1f} 天，现在的 {margin_days} 天绰绰有余。"
                           f"调小到 {suggested} 能让空着发布时间的行早点降频、早点归档，"
                           "省得更多；不调也没有任何风险")
    return margin_days, f"最多晚 {worst:.1f} 天，现在的 {margin_days} 天正合适，不用改"


def report(findings: Findings, margin_days: int) -> list[str]:
    """给人看的结论。**纯函数。**"""
    lines: list[str] = []
    for c in findings.counts:
        lines.append(f"表「{c.table}」：读了 {c.rows} 行，发布时间填了 {c.with_publish} 行，"
                     f"其中 {c.with_both} 行能从链接解出 ID 时间"
                     + (f"（{c.no_id} 行是短链，解不出）" if c.no_id else ""))
    samples = findings.samples
    lines.append("")
    if not samples:
        lines.append("一行能比的都没有：要么发布时间都没填，要么链接全是短链。")
        if findings.unreadable:
            lines.append(f"另有 {len(findings.unreadable)} 张表读不到：{'、'.join(findings.unreadable)}")
        return lines

    gaps = sorted(s.gap_days for s in samples)
    lines.append(f"合计 {len(samples)} 行样本，发布时间 − ID 时间（天）：")
    lines.append(f"  中位 {_percentile(gaps, 0.5):+.1f}，P90 {_percentile(gaps, 0.9):+.1f}，"
                 f"P99 {_percentile(gaps, 0.99):+.1f}，最大 {gaps[-1]:+.1f}，最小 {gaps[0]:+.1f}")

    def listing(title: str, picked: list[Sample]) -> None:
        lines.append(f"  {title}：{len(picked)} 行")
        for s in sorted(picked, key=lambda s: -abs(s.gap_days))[:SHOW]:
            lines.append(f"    · 表「{s.table}」 {s.record_id}：{s.gap_days:+.1f} 天")
        if len(picked) > SHOW:
            lines.append(f"    · ……还有 {len(picked) - SHOW} 行")

    listing(f"晚于 ID 时间超过 {margin_days} 天（没填发布时间的话会被提前归档）",
            [s for s in samples if s.gap_days > margin_days])
    lines.append(f"  晚于 ID 时间 {SCHEDULED_DAYS:g}–{margin_days} 天（多半是定时发布，余量罩得住）："
                 f"{sum(1 for s in samples if SCHEDULED_DAYS < s.gap_days <= margin_days)} 行")
    listing(f"早于 ID 时间超过 {DATE_ONLY_SLACK_DAYS:g} 天（发布时间多半填错了，或链接贴错了，去表里看看）",
            [s for s in samples if s.gap_days < -DATE_ONLY_SLACK_DAYS])

    suggested, why = recommend(samples, margin_days)
    if findings.unreadable:
        suggested = None
        why = (f"有 {len(findings.unreadable)} 张表读不到（{'、'.join(findings.unreadable)}），"
               "这次的数不作数：余量对所有表生效，没看过的表里可能正好有定时发布的帖子。"
               "先让这几张表能读（应用加进表里），再跑一次")
    lines.append("")
    lines.append(f"结论：{why}")
    if suggested is not None and suggested != margin_days:
        lines.append(f"  改法：在 cron 和面板两个服务的环境变量里设 "
                     f"INFERRED_PUBLISH_MARGIN_DAYS={suggested}（不用改代码）")
    return lines


def _read_rows(table, settings: Settings) -> list[Row]:
    f = settings.fields
    meta = table.fields_meta()
    if not meta:
        raise RuntimeError("读不到字段列表（应用没被加进这张表？先跑 cli.py doctor）")
    wanted = [c for c in (f.link, f.publish_time, f.resolved_id) if c in meta]
    if f.publish_time not in wanted:
        return []
    # 不加「是否巡查」的筛选：结案的、归档的行也是有效样本，越多越准。
    records = table.search(wanted)
    return [runner.row_from_record(r, settings) for r in records]


def main(argv: list[str]) -> int:
    cli.load_env_or_exit()
    args = list(argv[1:])
    selected = cli.pop_table_flag(args)
    if args:
        print(__doc__)
        return 2
    settings = cli.build_settings()
    margin = settings.refresh.inferred_publish_margin_days
    findings = Findings()
    print(f"只读飞书，不发付费请求、不写表。当前余量 INFERRED_PUBLISH_MARGIN_DAYS={margin}\n")
    for label, table in cli._tables(selected):
        try:
            collect(findings, label, _read_rows(table, settings))
        except Exception as exc:  # noqa: BLE001 —— 一张表读不到不该挡住别的表
            print(f"⚠ 表「{label}」读不到：{exc}")
            findings.unreadable.append(label)
    for line in report(findings, margin):
        print(line)
    return 1 if findings.unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
