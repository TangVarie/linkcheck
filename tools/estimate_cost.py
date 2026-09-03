#!/usr/bin/env python3
"""按当前配置算月度成本，两家供应商分开报。

    python3 tools/estimate_cost.py [每天新发条数] [小红书占比]

计价（都已实地核实）：
    SocialDataX  全平台 10 积分/页，1 积分 = ¥0.01 → 一次调用 ¥0.10
    TikHub       小红书 $0.010/次（不参与走量折扣），抖音 $0.001/次
                 数字来自它自己公开免鉴权的计价接口 get_all_endpoints_info

调用次数（已从接口实测核实）：
    小红书 = 1 次评论调用（评论数 + 置顶 + 前 N 条一次拿全）
             + 新笔记额外 1 次 detail（点赞/收藏）
    抖音   = 1 次评论调用 + 恒定 1 次 detail
             （SocialDataX 的 comment_count 是 integer|null，必须兜底；
               TikHub 的 total 实测是实数，但 detail 还要拿点赞收藏，照调）
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载和计价覆盖，见 main()
from xhsearch import providers  # noqa: E402
from xhsearch.config import Settings  # noqa: E402


def _yuan_per_call() -> float:
    """SocialDataX 单价，用作对照基准。

    每次调用时现取，不在导入时固化：计价覆盖是在 main() 里应用的，
    模块级常量会把覆盖前的旧价钉死。
    """
    return providers.SOCIALDATAX_YUAN


def tier_segments(settings: Settings) -> list[tuple[float, float, int]]:
    """把刷新分层切成 [(起始天, 结束天, 刷新间隔小时)] 的**区间**清单。

    ⚠️ 这里是这个脚本最容易算错的地方，之前就错过：

    1. 归档线不等于最后一档的端点。`archive_after_days` 比 tiers 末端**小**时
       （比如 tiers 到 30 天、archive 设 14 天），超过 14 天的帖子根本不再刷，
       但旧算法照样把整个 8–30 天档算进人口，成本凭空多算一倍多。
       反过来 archive 比末端大时，末档要按「沿用最后一档间隔」延长。
       两条都要和 RefreshTiers.interval_hours_for_age 的口径完全一致。
    2. 区间要真的按天切，不能拿档位端点代表整档。
    3. **端点要 +1。** 档位按完整天数判（`interval_hours_for_age` 里的
       `math.floor`，和飞书 DATEDIF 同口径），所以「2 天档」覆盖的是
       第 0、1、2 天，连续时间上是 [0, 3)；归档线 30 同理是「满 31 天才停」。
       少了这个 +1，projection 会比真实少算整整一天的高频刷新。
    """
    # +1 见上面第 3 条：配置里的天数是**完整天数**的上界，不是连续时间的端点。
    archive = float(settings.refresh.archive_after_days) + 1.0
    segments: list[tuple[float, float, int]] = []
    start = 0.0
    for max_age_days, interval_hours in settings.refresh.tiers:
        end = min(float(max_age_days) + 1.0, archive)
        if end > start:
            segments.append((start, end, interval_hours))
        start = float(max_age_days) + 1.0
        if start >= archive:
            return segments
    # 超出最后一档年龄但还没到归档线：沿用最后一档的间隔。
    if settings.refresh.tiers and archive > start:
        segments.append((start, archive, settings.refresh.tiers[-1][1]))
    return segments


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def tier_population(settings: Settings, per_day: float) -> list[tuple[str, float, int, float]]:
    """返回 [(档位说明, 该档笔记数, 刷新间隔小时, 每天刷新次数)]"""
    return [
        (f"{start:g}-{end:g} 天", per_day * (end - start), interval, 24 / interval)
        for start, end, interval in tier_segments(settings)
    ]


def _calls_by_platform(per_day: float, xhs_share: float, settings: Settings):
    """按区间交集算调用次数，返回（小红书调用/天, 抖音调用/天, 评论调用, detail 调用）。

    小红书的 detail 只在 `detail_within_days` 之内补，而这个窗口可以落在
    某一档的**中间**（比如 detail 7 天、档位是 3–7/8–30）。按「整档有或没有」
    近似会整档算错；这里取区间交集，误差为零。
    """
    xhs_calls = dy_calls = 0.0
    comment_calls = detail_calls = 0.0
    detail_window = float(settings.detail_within_days)
    for start, end, interval_hours in tier_segments(settings):
        refreshes = 24 / interval_hours
        population = per_day * (end - start)
        xhs_posts = population * xhs_share
        dy_posts = population * (1 - xhs_share)

        # 评论调用：这一段里每篇每天刷 refreshes 次。
        xhs_calls += xhs_posts * refreshes
        dy_calls += dy_posts * refreshes
        comment_calls += population * refreshes

        # 小红书 detail：只算落在 detail 窗口里的那部分天数。
        detail_days = _overlap(start, end, 0.0, detail_window)
        xhs_detail = per_day * detail_days * xhs_share * refreshes
        xhs_calls += xhs_detail
        # 抖音 detail：恒定追加（评论接口的 comment_count 可能为 null）。
        dy_calls += dy_posts * refreshes
        detail_calls += xhs_detail + dy_posts * refreshes
    return xhs_calls, dy_calls, comment_calls, detail_calls


def estimate(per_day: float, xhs_share: float, settings: Settings) -> dict:
    tiers = tier_population(settings, per_day)
    total_posts = sum(t[1] for t in tiers)
    xhs_calls, dy_calls, comment_calls, detail_calls = _calls_by_platform(
        per_day, xhs_share, settings)

    rows = []
    for (start, end, interval_hours), (label, population, _, refreshes) in zip(
            tier_segments(settings), tiers):
        detail_days = _overlap(start, end, 0.0, float(settings.detail_within_days))
        tier_calls = (
            population * refreshes                                        # 评论
            + per_day * detail_days * xhs_share * refreshes               # 小红书 detail
            + population * (1 - xhs_share) * refreshes                    # 抖音 detail
        )
        rows.append({"label": label, "posts": population,
                     "interval": interval_hours, "calls": tier_calls})

    total_calls = comment_calls + detail_calls
    return {
        "posts": total_posts,
        "rows": rows,
        "comment_calls": comment_calls,
        "detail_calls": detail_calls,
        "calls_per_day": total_calls,
        "xhs_calls_per_day": xhs_calls,
        "douyin_calls_per_day": dy_calls,
        "yuan_per_day": total_calls * _yuan_per_call(),
        "yuan_per_month": total_calls * _yuan_per_call() * 30,
    }


def _split_by_platform(per_day: float, xhs_share: float, settings: Settings):
    """把每天的调用次数按平台拆开。双通道之后两家单价差 10 倍，不拆就算不出钱。"""
    xhs_calls, dy_calls, _, _ = _calls_by_platform(per_day, xhs_share, settings)
    return xhs_calls, dy_calls


def flat_daily(total_posts: float, xhs_share: float, with_detail: bool) -> dict:
    """对照组：不分层，每天全表刷一遍。"""
    xhs = total_posts * xhs_share
    dy = total_posts * (1 - xhs_share)
    calls = xhs * (2 if with_detail else 1) + dy * 2
    return {"calls_per_day": calls, "yuan_per_month": calls * _yuan_per_call() * 30}


def main() -> int:
    # 这个脚本是用来**做月度成本决策**的，所以它必须和生产读同一份配置：
    # 不只是计价覆盖（USD_TO_CNY / TIKHUB_USD_*），还包括 DETAIL_WITHIN_DAYS。
    # 自己 new 一个裸 Settings() 的话，生产设了 DETAIL_WITHIN_DAYS=0
    # （关掉小红书 detail）时，这里仍按编译进代码的 7 天窗口算，
    # 把每一次合格的小红书刷新都多算一个付费调用，成本被系统性高估。
    cli.load_env_or_exit()

    per_day = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    xhs_share = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7
    if per_day <= 0:
        print("每天新发条数要大于 0", file=sys.stderr)
        return 2
    if not 0 <= xhs_share <= 1:
        print("小红书占比要在 0 和 1 之间（如 0.7）", file=sys.stderr)
        return 2

    settings = cli.build_settings()
    result = estimate(per_day, xhs_share, settings)

    print(f"假设：每天新发 {per_day:.0f} 条，小红书占 {xhs_share:.0%}，"
          f"{settings.refresh.archive_after_days} 天后归档停刷")
    print(f"稳态在管：{result['posts']:.0f} 条\n")

    print(f"{'档位':<14}{'条数':>8}{'刷新间隔':>10}{'调用/天':>10}")
    print("-" * 44)
    for row in result["rows"]:
        print(f"{row['label']:<14}{row['posts']:>8.0f}{row['interval']:>8}h{row['calls']:>10.0f}")
    print("-" * 44)
    print(f"{'合计':<14}{result['posts']:>8.0f}{'':>10}{result['calls_per_day']:>10.0f}")
    print(f"\n  其中评论调用 {result['comment_calls']:.0f}／detail 调用 {result['detail_calls']:.0f}")
    xhs_calls, dy_calls = _split_by_platform(per_day, xhs_share, settings)
    sdx = providers.get_provider("socialdatax")
    tik = providers.get_provider("tikhub")
    sdx_month = (xhs_calls * sdx.yuan_per_call("xhs", "comments")
                 + dy_calls * sdx.yuan_per_call("douyin", "comments")) * 30
    tik_month = (xhs_calls * tik.yuan_per_call("xhs", "comments")
                 + dy_calls * tik.yuan_per_call("douyin", "comments")) * 30

    print(f"\n  按平台拆：小红书 {xhs_calls:.0f} 次/天，抖音 {dy_calls:.0f} 次/天")
    print(f"\n【分层刷新 · 全走 SocialDataX】 {result['calls_per_day']:.0f} 次/天 "
          f"= ¥{sdx_month:,.0f}/月")
    print(f"【分层刷新 · 全走 TikHub】     同样次数 = ¥{tik_month:,.0f}/月"
          f"（省 {100 * (1 - tik_month / sdx_month):.0f}%）")
    print(f"  其中抖音那段：¥{dy_calls * _yuan_per_call() * 30:,.0f} → "
          f"¥{dy_calls * tik.yuan_per_call('douyin', 'comments') * 30:,.0f}/月"
          f"（这是省得最狠的一块）")
    print(f"\n  当前配置：小红书主通道 {settings.channels.primary('xhs')}，"
          f"抖音主通道 {settings.channels.primary('douyin')}")

    flat_full = flat_daily(result["posts"], xhs_share, with_detail=True)
    flat_lean = flat_daily(result["posts"], xhs_share, with_detail=False)
    print(f"【全表日刷】 {flat_full['calls_per_day']:.0f} 次/天 "
          f"= ¥{flat_full['yuan_per_month']:,.0f}/月"
          f"（省掉小红书 detail 则 ¥{flat_lean['yuan_per_month']:,.0f}/月）")

    manual = result["posts"] * 0.15 * 1.4
    print(f"【纯手动点】 {manual:.0f} 次/天（按 15% 行被点到估）"
          f" = ¥{manual * _yuan_per_call() * 30:,.0f}/月"
          f"  ⚠ 没被点到的行永远不更新，最坏陈旧度无穷大")

    no_detail = estimate(per_day, xhs_share, _without_xhs_detail(settings))
    print(f"【分层 + 关掉小红书 detail】 {no_detail['calls_per_day']:.0f} 次/天 "
          f"= ¥{no_detail['yuan_per_month']:,.0f}/月"
          f"  （代价：没有点赞/收藏，爆文只能靠评论数判）")
    return 0


def _without_xhs_detail(settings: Settings) -> Settings:
    lean = Settings()
    lean.refresh = settings.refresh
    lean.detail_within_days = 0
    return lean


if __name__ == "__main__":
    raise SystemExit(main())
