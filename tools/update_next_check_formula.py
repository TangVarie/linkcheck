#!/usr/bin/env python3
"""把已有表的「下次检查时间」换成新版公式；没有这一列的表顺手建上。

    python3 tools/update_next_check_formula.py                    # 只看，不改
    python3 tools/update_next_check_formula.py --apply            # 真换、真建
    python3 tools/update_next_check_formula.py --table A,B [--apply]

**默认只读**：逐表列出「现在的公式 → 新版公式」和「要建的列」，一个字都
不写。看过没问题，加 `--apply` 再跑一次才真动。不发任何付费请求。

新版比老版多一支：「巡查状态 = 已失效」的行按退避节奏算（72 → 144 →
288 小时……），和机器真正的复查节奏一致。老版对已失效的行报的是一个
**更早**的时间。归档天数按每张表自己的（注册表里改过的就用改过的），
不用再去列设置里手改那个 30。

为什么敢自动改：公式列的值是飞书算出来的，不存人填的数据，换公式不丢
东西。只动「下次检查时间」这一列，而且只在它**已经是公式列**时才动——
不是公式列（有人手填了日期）的一律跳过，改类型会清掉那一列。建列是纯
追加：新列排在表尾，已有的列一个都不碰。

**显示格式。** 公式算出来的是一个日期，但飞书默认可能按数字显示（一串像
46312 的数）。写公式时会连「按日期显示」一起发；这一段是照接口文档写的、
没在真表上验过，飞书不收就只写公式，再提示你在列设置里把格式选成日期。
换完读回来核对公式；格式读不出来时把这一列的原始设置打出来，发给开发看。
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载、表清单、逐表阈值
from xhsearch import provision, readout  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.feishu import DATE_RESULT  # noqa: E402
from xhsearch.provision import NEXT_CHECK_COLUMN, FormulaPlan  # noqa: E402

LABELS = {
    "update": "要换",
    "missing": "没有这一列，要建",
    "up_to_date": "已经是新版",
    "wrong_type": "不是公式列，跳过",
    "no_refs": "缺引用的列，跳过",
}

MANUAL_FORMAT = ("去飞书点这一列的列名 → 编辑字段 → 「格式」选日期（带时分的那种），"
                 "保存就好，不用动公式")


def plan_for(table, settings: Settings) -> FormulaPlan:
    """读一次字段列表，判断这张表要不要换。**只读。**"""
    meta = table.fields_meta()
    if meta is None:
        raise RuntimeError("读不到字段列表（应用没被加进这张表？先跑 cli.py doctor）")
    return provision.next_check_plan(settings, meta)


def describe(label: str, plan: FormulaPlan) -> list[str]:
    """给人看的那几行。**纯函数。**"""
    lines = [f"表「{label}」：{LABELS.get(plan.action, plan.action)}"]
    if plan.reason:
        lines.append(f"  {plan.reason}")
    if plan.action == "update":
        lines.append(f"  现在：{plan.current or '（飞书没给公式原文，按新版覆盖）'}")
        lines.append(f"  换成：{plan.wanted}")
    elif plan.action == "missing":
        lines.append("  --apply 会在表尾建一列公式列「下次检查时间」，公式：")
        lines.append(f"  {plan.wanted}")
    return lines


def _raw_property(table) -> str:
    try:
        info = (table.fields_meta_raw() or {}).get(NEXT_CHECK_COLUMN) or {}
    except Exception as exc:  # noqa: BLE001
        return f"（读不到：{exc}）"
    return json.dumps(info.get("property"), ensure_ascii=False)


def verify(table, settings: Settings, format_sent: bool) -> tuple[list[str], bool]:
    """写完读回来看一眼：公式对不对、是不是按日期显示。返回（给人看的行, 公式没问题）。

    只有「读回来的公式确定和新版不一样」算失败（进退出码）。读回来比不了、
    显示格式没设上都不算——前者说不准，后者公式是对的、点一下就能补，
    都照实说、给出手动的办法。
    """
    try:
        info = (table.fields_meta() or {}).get(NEXT_CHECK_COLUMN) or {}
    except Exception as exc:  # noqa: BLE001
        return [f"  ✓ 已提交，但读回来失败（{exc}）：去表里看一眼那一列"], True
    current = info.get("formula") or ""
    shown_as = info.get("result_type")
    wanted = provision.next_check_formula(settings)
    lines: list[str] = []
    formula_ok = True
    if current and readout.canonical(current) == readout.canonical(wanted):
        lines.append("  ✓ 公式已是新版（读回来核对过）")
    elif not current or "bitable::" in current:
        lines.append("  ✓ 已提交，飞书没给能比的公式原文：去表里看一眼那一列")
    else:
        formula_ok = False
        lines.append(f"  ✗ 写进去了，但读回来的公式和新版不一样：{current}")
        lines.append("    飞书可能改写了公式，去表里看一眼它算不算得出日期，把这一段发给开发")

    if shown_as == DATE_RESULT["data_type"]:
        lines.append("  ✓ 按日期显示")
        return lines, formula_ok
    if not format_sent:
        lines.append("  ⚠ 飞书没收「按日期显示」那一段，只写了公式。")
    elif shown_as is None:
        lines.append("  ? 显示格式读不出来，看一眼那一列：显示成一串数字（像 46312）的话，")
    else:
        lines.append(f"  ⚠ 飞书收了，但读回来还是按「{shown_as}」类显示，不是日期。")
    lines.append(f"    {MANUAL_FORMAT}")
    lines.append(f"    这一列的原始设置（显示成数字的话发给开发）：{_raw_property(table)}")
    return lines, formula_ok


def run(tables, settings: Settings, apply: bool, out=print) -> int:
    """逐表处理。一张表失败不挡别的表；有失败返回 1。"""
    counts: Counter = Counter()
    for label, table in tables:
        per_table = cli.settings_for_table(settings, table)
        try:
            plan = plan_for(table, per_table)
        except Exception as exc:  # noqa: BLE001 —— 一张表读不到不该挡住别的表
            out(f"⚠ 表「{label}」读不到：{exc}")
            counts["unreadable"] += 1
            continue
        counts[plan.action] += 1
        for line in describe(label, plan):
            out(line)
        if not apply or plan.action not in ("update", "missing"):
            continue
        creating = plan.action == "missing"
        try:
            if creating:
                format_sent = table.create_formula_field(
                    NEXT_CHECK_COLUMN, plan.wanted, result_type=DATE_RESULT)
            else:
                format_sent = table.set_formula(
                    NEXT_CHECK_COLUMN, plan.wanted, result_type=DATE_RESULT)
        except Exception as exc:  # noqa: BLE001
            out(f"  ✗ 没{'建' if creating else '换'}成：{exc}")
            out("    飞书报错 = 表没动；网络超时的话说不准，不带 --apply 再跑一次看现状")
            counts["apply_failed"] += 1
            continue
        if creating:
            out("  ✓ 已在表尾建好（视图里可以拖到「最近检查时间」旁边）")
        lines, formula_ok = verify(table, per_table, format_sent)
        for line in lines:
            out(line)
        if formula_ok:
            counts["created" if creating else "applied"] += 1
        else:
            counts["verify_failed"] += 1

    out("")
    parts = [f"{LABELS[k]} {counts[k]} 张" for k in LABELS if counts[k]]
    if counts["unreadable"]:
        parts.append(f"读不到 {counts['unreadable']} 张")
    out(f"合计：{'，'.join(parts) or '一张表都没有'}")
    if apply:
        out(f"这次换了 {counts['applied']} 张，建了 {counts['created']} 张"
            + (f"，没成 {counts['apply_failed']} 张" if counts["apply_failed"] else "")
            + (f"，读回来对不上 {counts['verify_failed']} 张" if counts["verify_failed"] else ""))
    elif counts["update"] or counts["missing"]:
        out("上面看着没问题，加 --apply 再跑一次才真动")
    return 1 if counts["unreadable"] or counts["apply_failed"] or counts["verify_failed"] else 0


def main(argv: list[str]) -> int:
    cli.load_env_or_exit()
    args = list(argv[1:])
    selected = cli.pop_table_flag(args)
    apply = "--apply" in args
    if apply:
        args.remove("--apply")
    if args:
        print(__doc__)
        return 2
    settings = cli.build_settings()
    print("会真改表（只动「下次检查时间」这一列：换公式，或者在表尾建上）\n" if apply
          else "只看不改。加 --apply 才真动\n")
    return run(cli._tables(selected), settings, apply)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
