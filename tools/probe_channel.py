#!/usr/bin/env python3
"""拿一条真实的笔记/视频，把某条数据通道从头到尾走一遍。

    # 两条通道都验（各花一次调用）。Key 从环境变量或仓库根的 .env 读
    #（照 .env.example 填好即可，脚本会自动加载）：
    python3 tools/probe_channel.py https://www.xiaohongshu.com/explore/xxxxxxxx

    # 只验一条
    python3 tools/probe_channel.py <链接> --only tikhub

    # 明知这家不吃这种参数形态，仍然强行发一次请求（会计费）
    python3 tools/probe_channel.py <链接> --only tikhub --force

和 `cli.py doctor` 的分工：doctor 不花钱，只查飞书那边的权限和列名；
这个脚本**会真的发请求、真的扣费**，验的是「数据通道能不能用、字段全不全」。
一次小红书调用 ≈ ¥0.07（TikHub）或 ¥0.10（SocialDataX），抖音便宜十倍。

走的是 `xhsearch/providers.py` 里那一份真代码，不是另写一遍——
所以它验的就是线上会跑的东西：**同一套通道路由（can_handle）**、
同一套归一化、置顶识别、脱敏、错误分类，最后再跑一遍 analyze.decide
把真正会写进表的判定打出来。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载（看不懂的行当场拒跑）

cli.load_env_or_exit()   # 本地跑时补上 .env 里的 Key；已存在的环境变量优先

from xhsearch import analyze, providers, transport  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.links import parse  # noqa: E402
from xhsearch.protocol import Err  # noqa: E402
from xhsearch.rows import Row, plan_calls  # noqa: E402

ENV = {
    providers.TIKHUB: "TIKHUB_API_KEY",
    providers.SOCIALDATAX: "SOCIALDATAX_API_KEY",
}


def probe(name: str, key: str, link: str, settings: Settings, *, force: bool = False) -> bool:
    provider = providers.get_provider(name)
    row = Row(record_id="probe", link_cell=link)
    calls = plan_calls(row, settings)

    print(f"\n{'=' * 68}\n{provider.label}\n{'=' * 68}")
    if not calls:
        print(f"  ❌ 这个链接解析不出来：{row.parsed.describe_failure()}")
        return False

    # 和线上走**同一套**路由判据（COR-009）：runner 挑通道时先过
    # provider.can_handle，探针以前不过。差别的后果是真金白银的：
    # 把 v.douyin.com 短链塞进 TikHub 的 aweme_id 参数必然失败，
    # 而 TikHub 对失败的业务查询照样计费——探针会白扣一次费，
    # 然后报「这条通道不可用」，而线上其实会自动让给吃短链的 SocialDataX。
    unsupported = [c for c in calls if not provider.can_handle(c.platform, c.purpose, c.arguments)]
    if unsupported and not force:
        print(f"  ⏭ 跳过（不发请求、不扣费）：{provider.label} 不吃这种参数形态——"
              f"{row.parsed.platform} 的 {'、'.join(sorted({c.purpose for c in unsupported}))} "
              "端点只收数字 ID，不收链接。")
        print("     线上遇到这种行会自动让给吃链接的通道；"
              "真要强行发一次请求验证，加 --force。")
        return True
    if unsupported and force:
        print("  ⚠ --force：明知这家不吃这种参数形态仍然发请求，"
              "失败很可能照样计费（TikHub 明确会计费）")

    print(f"  链接识别为：{row.parsed.platform} / {row.parsed.content_id or row.parsed.url}")
    print(f"  本次会发 {len(calls)} 个请求，预计花费 ¥"
          f"{sum(provider.yuan_per_call(c.platform, c.purpose) for c in calls):.3f}")

    snapshot = None
    ok = True
    for call in calls:
        request = provider.build(key, call.platform, call.purpose, call.arguments)
        response = transport.request(request.method, request.url, request.headers,
                                     request.body, timeout=45)
        result = provider.parse(call.platform, call.purpose, response.status,
                                response.content_type, response.body,
                                response.request_id, key)

        label = "评论" if call.purpose == "comments" else "详情"
        if isinstance(result, Err):
            print(f"  {label}：❌ [{result.kind.value}] {result.operator_text()[:150]}")
            if result.kind.value == "auth":
                print(f"     → 检查 {ENV[name]} 是否填对")
            if name == providers.TIKHUB and result.kind.value == "transport":
                print(f"     → 确认这台机器能出网到 {providers.TIKHUB_BASE}")
                print("       境内用 api.tikhub.dev，境外设 TIKHUB_BASE=https://api.tikhub.io")
            ok = False
            if call.purpose == "comments":
                break
            continue

        print(f"  {label}：✅ {request.method} {response.status}")
        if call.purpose == "comments":
            snapshot = analyze.read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            analyze.merge_detail(snapshot, result.data)

    if snapshot is None:
        return False

    print()
    print(f"  评论总数   {snapshot.comment_count}")
    print(f"  一级评论   {snapshot.top_level_comment_count}")
    print(f"  点赞/收藏  {snapshot.like_count} / {snapshot.collect_count}")
    # 文案要和 analyze.decide 的实际行为一致：审核标记为 True 会打「风控中」。
    # 原来这里写「不参与打标签」，照它验收会得出错误的上线结论。
    print(f"  上游审核标记 {snapshot.censored}"
          "（None = 这家不提供；True 会被 analyze.decide 打成「风控中」）")
    if snapshot.supports_pinned:
        pinned = snapshot.pinned
        print(f"  置顶评论   {pinned.content if pinned else '（无）'}")
    else:
        print("  置顶评论   —（抖音接口没有置顶字段，「置顶状态」列不写）")
    print("  评论区快照：")
    for line in analyze.format_digest(snapshot, settings.digest).splitlines():
        print(f"    {line}")

    if snapshot.supports_pinned and snapshot.pinned is None:
        print("\n  ⚠ 没识别到置顶评论。如果这条笔记**确实有**置顶，说明上游改字段了，"
              "\n    去 providers.py 的 _tag_types() 看一眼。"
              "\n    如果这条本来就没置顶，那是正常的——换一条有置顶的再验一次。")

    # 把线上真正会写进表的判定也打出来：探针的价值是「验线上那条路」，
    # 只打原始字段的话，判定口径出问题它一个字都不会说。
    verdict = analyze.decide(
        snapshot, settings,
        previous_comment_count=None,
        age_hours=row.age_hours(),
        seed_keywords=row.seed_keywords,
        current_tags=[],
        current_pin_status="",
    )
    print("\n  线上判定（analyze.decide 的真实结果）：")
    print(f"    流量状态标签 {'、'.join(sorted(verdict.tags)) or '（无）'}")
    print(f"    置顶状态     {analyze.pin_status_value(verdict, '', settings) or '（本轮不写）'}")
    print(f"    评论状态     {analyze.comment_status_value(verdict, settings) or '（本轮不写）'}")
    for note in verdict.notes:
        print(f"    · {note}")
    return ok


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    link = sys.argv[1]

    only = ""
    if "--only" in sys.argv:
        index = sys.argv.index("--only") + 1
        if index >= len(sys.argv):
            print("--only 后面要跟通道名：tikhub 或 socialdatax", file=sys.stderr)
            return 2
        only = sys.argv[index].strip().lower()
        if only not in ENV:
            print(f"--only 的值 {only!r} 不认识，可选：{' / '.join(ENV)}", file=sys.stderr)
            return 2
    force = "--force" in sys.argv

    unsafe = os.environ.get("ALLOW_UNSAFE_ENDPOINT_OVERRIDE", "").strip() in ("1", "true", "yes")
    try:
        providers.set_tikhub_base(os.environ.get("TIKHUB_BASE", ""), allow_unsafe=unsafe)
    except providers.EndpointRejected as exc:
        print(f"TIKHUB_BASE 被拒绝：{exc}", file=sys.stderr)
        return 2
    settings = Settings()
    if not parse(link).usable:
        print(f"链接识别不了：{parse(link).describe_failure()}", file=sys.stderr)
        return 2

    ran = False
    all_ok = True
    for name, env_name in ENV.items():
        if only and name != only:
            continue
        key = os.environ.get(env_name, "").strip()
        if not key:
            print(f"\n（跳过 {name}：没有 {env_name}）")
            continue
        ran = True
        all_ok = probe(name, key, link, settings, force=force) and all_ok

    if not ran:
        print("一个 Key 都没配。至少设一个：", " / ".join(ENV.values()), file=sys.stderr)
        return 2

    print(f"\n{'=' * 68}")
    print("✅ 通道可用" if all_ok else "❌ 有通道不可用，看上面的报错")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
