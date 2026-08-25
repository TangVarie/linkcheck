#!/usr/bin/env python3
"""试一个 TikHub 端点能不能用、返回些什么。**先免费验路由，再决定要不要花钱。**

    # 默认测那个「便宜十倍还能打折」的候选（结论见下，已经是死路）
    python3 tools/probe_endpoint.py "<小红书链接>"

    # 测任意一个端点
    python3 tools/probe_endpoint.py "<小红书链接>" --path /api/v1/xiaohongshu/xxx/yyy

## 这个脚本存在的原因，以及它已经回答过的那个问题

小红书抓评论的 10 个端点全是 $0.010 且 `allow_discount=0`——阶梯折扣对它们
不生效。但 TikHub 计价表里挂着 3 个 $0.001、**标着能吃折扣**的小红书端点：

    /api/v1/xiaohongshu/web/get_note_info_v5
    /api/v1/xiaohongshu/web/sign
    /api/v1/xiaohongshu/web/get_note_id_and_xsec_token

看起来像是能把 detail 调用的成本砍到十分之一。**实测：这三个全都 404。**
（2026-08-25，`api.tikhub.io` 和 `api.tikhub.dev` 两台都试过。）它们在计价表
里、却不在公开 OpenAPI 里、服务器上也没有——挂着而已。

判据是 **401 和 404 的区别**：拿一个**故意写错的 key** 去打，
路由存在会先过鉴权返回 401，路由不存在直接 404。同一台主机上，
我们在用的 `app_v2/get_image_note_detail` 返回 401，那三个返回 404，
对照清清楚楚。这一步不花钱，所以它排在最前面。

下次再看到计价表里冒出便宜端点，先跑这个脚本，别先改代码。

## 流程

1. 用**假 key** 打一次，401 = 路由在，404 = 没这个接口（不花钱，到此为止）；
2. 路由在的话，拿真 key 按几种常见参数名各试一次（没文档只能猜）；
3. 在返回里递归找点赞/收藏/评论数，报出它们在哪个字段上；
4. 再打一次我们**现在用的**那个端点，同一条笔记两边数字并排比——
   便宜的那个可能返回的是旧数据或空数据，不比对就换会出事。

第 2–4 步大约 ¥0.10。Key 从环境变量或仓库根的 .env 读，和其他脚本一样。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载（看不懂的行当场拒跑）

cli.load_env_or_exit()

from xhsearch import providers, transport  # noqa: E402
from xhsearch.links import parse  # noqa: E402

DEFAULT_PATH = "/api/v1/xiaohongshu/web/get_note_info_v5"
CURRENT = providers.TIKHUB_PATHS[("xhs", "detail")]

# 故意写错的 key：只用来分辨 401（路由在）和 404（路由不在）。
BAD_KEY = "probe-only-not-a-real-key"

# 没有文档，只能把常见的参数名都试一遍。顺序 = 最可能的在前。
PARAM_SHAPES = ["note_id", "note_url", "url", "share_text"]

# 要在返回里找的字段。同一个数字各家叫法不同，都列上。
WANTED = {
    "点赞数": ("liked_count", "like_count", "likes", "digg_count"),
    "收藏数": ("collected_count", "collect_count", "favorite_count", "fav_count"),
    "评论数": ("comment_count", "comments_count", "comment_num"),
}


def _headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": providers._BROWSER_UA,
    }


def _walk(node, path=""):
    """把嵌套 JSON 摊平成 (路径, 值)，用来在没有文档的返回里找字段。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node[:3]):     # 列表只看前几个，够定位了
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path, node


def _find(payload) -> dict[str, list[tuple[str, object]]]:
    found: dict[str, list[tuple[str, object]]] = {}
    for label, names in WANTED.items():
        hits = [(path, value) for path, value in _walk(payload)
                if path.split(".")[-1].split("[")[0] in names
                and isinstance(value, (int, float)) and not isinstance(value, bool)]
        if hits:
            found[label] = hits
    return found


def route_exists(path: str) -> tuple[bool, int]:
    """路由在不在。拿假 key 问，**不花钱**：401=在，404=不在。"""
    url = f"{providers.TIKHUB_BASE}{path}?note_id=probe"
    status = transport.get(url, _headers(BAD_KEY), timeout=30.0).status
    return status != 404, status


def _call(url: str, key: str, label: str):
    """打一次，返回 (成功?, 解析出来的 JSON)。失败时把原因打成人话。"""
    resp = transport.get(url, _headers(key), timeout=30.0)
    if resp.status != 200:
        body = providers._redact(resp.body or "", key)[:200]
        print(f"    ✗ HTTP {resp.status}  {body}")
        return False, None
    payload = resp.json()
    if not isinstance(payload, dict):
        print(f"    ✗ 返回不是 JSON：{providers._redact(resp.body, key)[:150]}")
        return False, None
    code = payload.get("code")
    if code not in (200, 0, None):
        message = payload.get("message") or payload.get("detail") or ""
        print(f"    ✗ 接口报错 code={code} {providers._redact(str(message), key)[:180]}")
        return False, None
    print(f"    ✓ {label} 通了")
    return True, payload


def _report(label: str, found: dict) -> None:
    print(f"  {label}：")
    if not found:
        print("    （一个互动数字都没找到）")
    for name, hits in found.items():
        for path, value in hits[:2]:
            print(f"    {name} = {value}   ← 字段路径 {path}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    link = argv[1]
    path = DEFAULT_PATH
    if "--path" in argv:
        path = argv[argv.index("--path") + 1]

    # ---------- 0. 先免费验路由 ----------
    print(f"① 验路由是否存在（用假 key，不花钱）：{path}")
    exists, status = route_exists(path)
    print(f"   HTTP {status} → " + ("路由存在（卡在鉴权，正常）" if exists
                                    else "**没有这个接口**"))
    if not exists:
        print("\n结论：这个端点在计价表里挂着，服务器上没有。**保持现状，别改代码。**\n"
              "     （对照：我们在用的端点同样用假 key 打会返回 401，不是 404。）")
        return 0

    key = os.environ.get("TIKHUB_API_KEY", "").strip()
    if not key:
        print("\n路由是在的，但要看返回内容得有真 key。"
              "在仓库根的 .env 里填 TIKHUB_API_KEY=你的key（照 .env.example）再跑一次。")
        return 1

    parsed = parse(link)
    if parsed.platform != "xhs":
        print(f"❌ 这个脚本只测小红书。链接解析结果：{parsed.platform or '认不出来'}")
        return 1
    note_id = parsed.content_id
    print(f"\n链接解析：note_id = {note_id or '（抠不出来，只能拿整条链接当参数）'}")
    print("接下来会真的发请求、真的扣费，大约 ¥0.10")

    # ---------- 1. 候选端点：参数名逐个试 ----------
    print(f"\n② 试 {path}")
    payload = None
    for name in PARAM_SHAPES:
        if name == "note_id" and not note_id:
            continue
        value = note_id if name == "note_id" else link
        url = f"{providers.TIKHUB_BASE}{path}?{providers._query({name: value})}"
        print(f"  参数名 {name}=…")
        ok, payload = _call(url, key, f"参数名 {name}")
        if ok:
            break
        payload = None

    if payload is None:
        print("\n结论：路由在，但这几种参数名都没通。没有文档就只能问 TikHub "
              "客服要参数说明（Discord: https://discord.gg/aMEAS8Xsvz）。")
        return 0

    found = _find(payload)
    _report("返回里找到的互动数字", found)

    # ---------- 2. 拿同一条笔记和现用端点比对 ----------
    print(f"\n③ 对照我们现在用的 {CURRENT}（$0.010，不打折）")
    args = {"note_id": note_id} if note_id else {"share_text": link}
    url = f"{providers.TIKHUB_BASE}{CURRENT}?{providers._query(args)}"
    ok, current = _call(url, key, "现用端点")
    current_found = _find(current) if ok else {}
    _report("返回里找到的互动数字", current_found)

    # ---------- 3. 结论 ----------
    print("\n" + "=" * 68)
    need = ["点赞数", "收藏数"]          # detail 调用存在的理由就是这两个
    missing = [n for n in need if n not in found]
    if missing:
        print(f"结论：候选端点缺 {'、'.join(missing)}，**顶替不了 detail**，保持现状。")
    else:
        same = all(found[n][0][1] == current_found[n][0][1]
                   for n in need if n in current_found)
        print("结论：候选端点把点赞和收藏都返回了。"
              + ("两边数字一致，值得换。" if same else
                 "⚠ 但和现用端点的数字**对不上**，先弄清楚谁是对的，别急着换。"))
    print("把上面这段整个发过来，我来判断怎么改。")

    out = pathlib.Path("probe_endpoint_raw.json")
    out.write_text(json.dumps({"candidate": payload, "current": current},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"（完整返回已存到 {out}，里面不含 Key）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
