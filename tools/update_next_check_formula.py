#!/usr/bin/env python3
"""把已有表的「下次检查时间」换成新版公式。

    python3 tools/update_next_check_formula.py                    # 只看，不改
    python3 tools/update_next_check_formula.py --apply            # 真换
    python3 tools/update_next_check_formula.py --table A,B [--apply]

**默认只读**：逐表列出「现在的公式 → 新版公式」，一个字都不写。看过没问题，
加 `--apply` 再跑一次才真换。不发任何付费请求。

新版比老版多一支：「巡查状态 = 已失效」的行按退避节奏算（72 → 144 →
288 小时……），和机器真正的复查节奏一致。老版对已失效的行报的是一个
**更早**的时间。归档天数按每张表自己的（注册表里改过的就用改过的），
不用再去列设置里手改那个 30。

为什么敢自动改：公式列的值是飞书算出来的，不存人填的数据，换公式不丢
东西。只动「下次检查时间」这一列，而且只在它**已经是公式列**时才动——
不是公式列（有人手填了日期）的一律跳过，改类型会清掉那一列。

发给飞书的是接口文档的写法（本表的列换成 `bitable::$table[…].$field[…]`
id 引用），读回来再翻成列名和新版逐字比。飞书报错 = 这一列没改，还是
原来的公式；换完看一眼那一列是不是日期（显示成数字就去列设置里把格式
选成日期）。
"""

from __future__ import annotations

import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载、表清单、逐表阈值
from xhsearch import provision  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.provision import NEXT_CHECK_COLUMN, FormulaPlan  # noqa: E402

LABELS = {
    "update": "要换",
    "up_to_date": "已经是新版",
    "missing": "没有这一列",
    "wrong_type": "不是公式列，跳过",
    "no_refs": "缺引用的列，跳过",
}


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
        lines.append("  要的话在飞书里加一列公式列「下次检查时间」，公式粘这条：")
        lines.append(f"  {plan.wanted}")
    return lines


def verify(table, settings: Settings) -> str:
    """换完读回来看一眼。读回来比不了不算失败——只是说不准。"""
    try:
        after = plan_for(table, settings)
    except Exception as exc:  # noqa: BLE001
        return f"  ✓ 已提交，但读回来失败（{exc}）：去表里看一眼那一列"
    if after.action == "up_to_date":
        return "  ✓ 已换成新版（读回来核对过）"
    if not after.current or "bitable::" in after.current:
        return "  ✓ 已提交，飞书没给能比的公式原文：去表里看一眼那一列"
    return (f"  ⚠ 写进去了，但读回来的和新版不一样：{after.current}\n"
            "    飞书可能改写了公式，去表里看一眼它算不算得出日期")


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
        if not apply or plan.action != "update":
            continue
        try:
            table.set_formula(NEXT_CHECK_COLUMN, plan.wanted)
        except Exception as exc:  # noqa: BLE001
            out(f"  ✗ 没换成：{exc}")
            out("    飞书报错 = 这一列没动；网络超时的话说不准，不带 --apply 再跑一次看现状")
            counts["apply_failed"] += 1
            continue
        counts["applied"] += 1
        out(verify(table, per_table))

    out("")
    parts = [f"{LABELS[k]} {counts[k]} 张" for k in LABELS if counts[k]]
    if counts["unreadable"]:
        parts.append(f"读不到 {counts['unreadable']} 张")
    out(f"合计：{'，'.join(parts) or '一张表都没有'}")
    if apply:
        out(f"这次换了 {counts['applied']} 张"
            + (f"，没换成 {counts['apply_failed']} 张" if counts["apply_failed"] else ""))
    elif counts["update"]:
        out("上面看着没问题，加 --apply 再跑一次才真换")
    return 1 if counts["unreadable"] or counts["apply_failed"] else 0


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
    print("会真改表（只动「下次检查时间」这一列的公式）\n" if apply
          else "只看不改。加 --apply 才真换\n")
    return run(cli._tables(selected), settings, apply)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
