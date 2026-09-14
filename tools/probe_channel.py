#!/usr/bin/env python3
"""拿一条真实的笔记/视频，把某条数据通道从头到尾走一遍。

    # 两条通道都验（各花一次调用）。Key 从环境变量或仓库根的 .env 读
    #（照 .env.example 填好即可，脚本会自动加载）：
    python3 tools/probe_channel.py https://www.xiaohongshu.com/explore/xxxxxxxx

    # 只验一条
    python3 tools/probe_channel.py <链接> --only tikhub

    # 明知这家不吃这种参数形态，仍然强行发一次请求（会计费）
    python3 tools/probe_channel.py <链接> --only tikhub --force

    # 顺带打印整页评论的**原始**字段（不经过归一化，去掉头像杂项和空值）。
    # 验「这家通道的评论条目里有没有蓝词/高亮一类的字段」用：小红书的蓝词是
    # 正文里的 `#词[搜索高亮]#` 标记，抖音还没验过
    python3 tools/probe_channel.py <抖音链接> --only tikhub --raw

    # 小红书额外调一次详情接口（线上默认不调，DETAIL_WITHIN_DAYS=0），并连
    # 原始的笔记级字段一起打出来。验「分享链接打开是『笔记不存在』、评论接口
    # 却照常返回」这种**仅作者可见**的笔记，详情接口到底怎么说
    python3 tools/probe_channel.py <小红书链接> --only tikhub --detail --raw

和 `cli.py doctor` 的分工：doctor 不花钱，只查飞书那边的权限和列名；
这个脚本**会真的发请求、真的扣费**，验的是「数据通道能不能用、字段全不全」。
一次小红书调用 ≈ ¥0.07（TikHub）或 ¥0.10（SocialDataX），抖音便宜十倍。

走的是 `xhsearch/providers.py` 里那一份真代码，不是另写一遍——
所以它验的就是线上会跑的东西：**同一套通道路由（can_handle）**、
同一套归一化、置顶识别、脱敏、错误分类，最后再跑一遍 analyze.decide
把真正会写进表的判定打出来。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载（看不懂的行当场拒跑）

cli.load_env_or_exit()   # 本地跑时补上 .env 里的 Key；已存在的环境变量优先

from xhsearch import analyze, providers, shortlink, transport  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.links import parse  # noqa: E402
from xhsearch.protocol import Err  # noqa: E402
from xhsearch.rows import Row, ToolCall, id_form, plan_calls  # noqa: E402

ENV = {
    providers.TIKHUB: "TIKHUB_API_KEY",
    providers.SOCIALDATAX: "SOCIALDATAX_API_KEY",
}


def _raw_comments(body: str) -> list:
    """从评论接口的**原始**响应里挖出整页评论条目，不经过归一化。

    两家的信封不一样：SocialDataX 是顶层 items[]；TikHub 是 data.data.comments[]
    （小红书）或 data.comments[]（抖音）。都找不到就返回空列表。
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return []
    node = payload
    for _ in range(3):
        if not isinstance(node, dict):
            break
        for key in ("items", "comments"):
            items = node.get(key)
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items
        node = node.get("data")
    return []


def _raw_envelope(body: str) -> dict:
    """原始响应去掉评论条目之后**剩下的**部分：页级字段。

    抖音的蓝词若不挂在每条评论上（--raw 已验：带蓝词的评论 text_extra 为空），
    就只可能挂在整页响应的顶层或 data 层。这里把评论列表本身换成一句占位，
    其余层层去掉空值后原样返回，人工看一眼哪个键像搜索词列表。
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}

    def strip(node):
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in ("items", "comments") and isinstance(value, list) \
                    and value and isinstance(value[0], dict):
                out[key] = f"（{len(value)} 条评论，见上）"
                continue
            value = strip(value)
            if value in (None, "", [], {}, 0, False, -1):
                continue
            out[key] = value
        return out

    return strip(payload)


def _raw_detail(body: str) -> dict:
    """详情接口原始响应里的**笔记 / 视频本体**，不经过归一化。

    小红书是 data.data[0].note_list[0]，抖音是 data.aweme_detail。找不到本体
    （比如笔记没了时 data 直接是 []）就返回整个信封去空值——那时候信封里的
    code / msg 才是要看的东西。
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("data")
    if isinstance(inner, dict):
        aweme = inner.get("aweme_detail")
        if isinstance(aweme, dict):
            return _trim_raw(aweme, noisy=_DETAIL_NOISY_KEYS)
        core = inner.get("data")
        if isinstance(core, list) and core and isinstance(core[0], dict):
            notes = core[0].get("note_list")
            if isinstance(notes, list) and notes and isinstance(notes[0], dict):
                return _trim_raw(notes[0], noisy=_DETAIL_NOISY_KEYS)
    return _raw_envelope(body)


# 原始条目里这些键是头像/主页/账号杂项，一条评论里能占两千多字符，把真正要看的
# 字段（text_extra / label_* / is_hot …）全挤出屏幕。打印时只留昵称。
_NOISY_KEYS = {"user", "author", "avatar_thumb", "avatar", "image_list", "sticker"}
# 条目里装着**另一层评论**的键：抖音把作者回复塞在 reply_comment 里。
_NESTED_COMMENT_KEYS = {"reply_comment", "sub_comments", "replies"}
# 详情本体里的媒体/作者杂项：一条视频的 video / music 对象几千字符，
# 而要看的是 in_censor / status / risk 这类几十字节的状态字段。
_DETAIL_NOISY_KEYS = _NOISY_KEYS | {
    "images_list", "image_info_list", "video", "video_info", "music", "cover",
    "dynamic_cover", "origin_cover", "share_info", "video_tag", "text_extra",
}


def _trim_raw(item: dict, *, noisy: set | None = None) -> dict:
    """把一条原始评论压成能读的样子：去掉杂项对象，去掉空值，其余原样。

    「空值」= None / "" / [] / {} / 0 / False / -1。抖音的原始条目一半以上是这种
    占位，留着只会淹没那几个真正有信息的字段。要看全貌就去掉这个过滤。
    """
    noisy = _NOISY_KEYS if noisy is None else noisy
    out = {}
    for key, value in item.items():
        if key in noisy:
            if isinstance(value, dict):
                name = value.get("nickname") or value.get("name")
                if name:
                    out[key] = {"nickname": name}
            continue
        if key in _NESTED_COMMENT_KEYS and isinstance(value, list):
            # 二级回复也是评论条目，同样压一遍——作者回复里的 user 一样带两千字
            # 头像杂项。**只对已知的嵌套评论键递归**：text_extra 这类元数据里
            # `type: 0` / `start: 0` 的零是有信息的（0 = @ 提及、从第 0 个字开始），
            # 而 --raw 正是用来看这些未归一化结构的，不能拿评论级的去空值规则去压它。
            value = [_trim_raw(v) if isinstance(v, dict) else v for v in value]
        if value in (None, "", [], {}, 0, False, -1):
            continue
        out[key] = value
    return out


def probe(name: str, key: str, link: str, settings: Settings, *, force: bool = False,
          raw: bool = False) -> bool:
    provider = providers.get_provider(name)
    row = Row(record_id="probe", link_cell=link)
    calls = plan_calls(row, settings)
    raw_comments: list = []
    raw_envelope: dict = {}
    raw_detail: dict | None = None

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
    if unsupported and calls[0].platform == "douyin" and calls[0].arguments.get("url"):
        # 线上开跑时会先把抖音短链免费展开成 aweme_id 再挑通道（shortlink.py），
        # 探针照做——否则「TikHub 不吃这种链接」这个结论只对展不开的短链成立。
        expansion = shortlink.expand_douyin(calls[0].arguments["url"])
        if expansion.ok:
            print(f"  🔗 短链已展开（免费，{expansion.requests} 跳）：aweme_id={expansion.aweme_id}")
            calls = [ToolCall(c.platform, c.purpose, id_form(c.arguments, expansion.aweme_id))
                     for c in calls]
            unsupported = [c for c in calls
                           if not provider.can_handle(c.platform, c.purpose, c.arguments)]
        else:
            print(f"  ⚠ 短链展不开：{expansion.reason}")
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
        if raw and call.purpose == "detail":
            # 成功失败都留着：笔记「仅作者可见」时详情接口可能直接回空（GONE），
            # 那时候信封里的 code / msg 正是要看的。
            raw_detail = _raw_detail(response.body)
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
            if raw:
                raw_comments = _raw_comments(response.body)
                raw_envelope = _raw_envelope(response.body)
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
    blue = analyze.highlighted_words(snapshot)
    print(f"  蓝词（#词[搜索高亮]# 标记）  {'、'.join(blue) if blue else '（这一页没有）'}")
    if raw:
        # 验「这家通道的评论条目里到底有哪些字段」用的：蓝词在小红书是正文里的
        # 标记，在抖音还没验过（原始条目里可能是 text_extra 之类的富文本结构）。
        # 整页每一条都打、去掉头像等杂项和空值，人工看一眼哪个字段像。
        print(f"  整页评论的原始字段（--raw，共 {len(raw_comments)} 条，已去掉空值和头像杂项）：")
        for index, item in enumerate(raw_comments, start=1):
            print(f"    #{index} " + json.dumps(_trim_raw(item), ensure_ascii=False))
        if not raw_comments:
            print("    （原始响应里没找到评论列表）")
        # 抖音已验：带蓝词的评论条目里没有任何字段标出蓝词（text_extra 只装 @ 和话题）。
        # 那蓝词只可能在页级字段里——把评论列表之外的整个信封也打出来。
        print("  评论条目之外的页级字段（--raw，已去掉空值）：")
        print("    " + json.dumps(raw_envelope, ensure_ascii=False))
    if raw_detail is not None:
        # 「分享链接打开是『笔记不存在』、评论接口却照常返回」的笔记，评论接口
        # 看不出任何异常（它量的是笔记在不在后台，不是公众看不看得见）。
        # 能看出来的话只可能在详情本体的状态字段里——整个打出来人工看。
        print("  详情接口的原始笔记/视频字段（--raw，已去掉媒体和作者杂项）：")
        print("    " + json.dumps(raw_detail, ensure_ascii=False))

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
    raw = "--raw" in sys.argv
    detail = "--detail" in sys.argv

    unsafe = os.environ.get("ALLOW_UNSAFE_ENDPOINT_OVERRIDE", "").strip() in ("1", "true", "yes")
    try:
        providers.set_tikhub_base(os.environ.get("TIKHUB_BASE", ""), allow_unsafe=unsafe)
    except providers.EndpointRejected as exc:
        print(f"TIKHUB_BASE 被拒绝：{exc}", file=sys.stderr)
        return 2
    settings = Settings()
    if detail:
        # 线上默认不调小红书详情（占月成本 39%，见 config.Settings.detail_within_days）。
        # 探针上显式要了才调，而且不看帖龄——探针的行没有发布时间。
        settings.detail_within_days = 3650
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
        all_ok = probe(name, key, link, settings, force=force, raw=raw) and all_ok

    if not ran:
        print("一个 Key 都没配。至少设一个：", " / ".join(ENV.values()), file=sys.stderr)
        return 2

    print(f"\n{'=' * 68}")
    print("✅ 通道可用" if all_ok else "❌ 有通道不可用，看上面的报错")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
