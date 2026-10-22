#!/usr/bin/env python3
"""把「蓝词字段」里已经混进去的竞品词清掉（一次性）。

    python3 tools/purge_blue_word_excludes.py                       # 只看，不改
    python3 tools/purge_blue_word_excludes.py --table SPX           # 只看这几张
    python3 tools/purge_blue_word_excludes.py --table SPX --apply   # 真删
    python3 tools/purge_blue_word_excludes.py --table SPX --exclude 康比特,迈胜,maurten,sis

**默认只读**：逐表逐行列出「要删哪些词、因为哪个排除词、删完剩什么」，一个字
都不写。把这份清单给运营确认过，再加 `--apply` 跑一次才真删。不发任何付费请求。

排除词默认取登记表里这张表的「蓝词排除词」那一格；`--exclude` 临时指定一份
（逗号分隔），只能和 `--table` 一起用——一份竞品名单套到所有表上多半是填错了。
匹配规则和机器回填时一样（analyze.excluded_by）：带中文的按子串，纯英文按整词，
都不分大小写，不到 2 个字的词不生效。

为什么机器自己不删、要单独跑这个：机器回填蓝词只加不减，格子里的词分不清是
人手填的还是机器扫到的。这个脚本也只动「蓝词字段」这一列，「蓝词来源」「是否
截图」「蓝词图片」一概不碰。删完一行如果空了，机器会当它「还没抓到过蓝词」，
下一轮重新开始抓（排除词照样不记）。

写之前会**重读一次**这几行的现值再算：清单列出来到真删之间，运营可能又手工改过。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  —— 复用同一份 .env 加载、表清单、逐表设置
from xhsearch import analyze, feishu, registry  # noqa: E402
from xhsearch.config import Settings  # noqa: E402


def plan_rows(records: list, column: str, excludes) -> list[tuple[str, list, list]]:
    """读回来的行 → [(record_id, [(要删的词, 排除词)], 删完剩下的词)]。**纯函数。**

    只列真有词要删的行。
    """
    out = []
    for record in records:
        words = feishu.read_multi_select((record.get("fields") or {}).get(column))
        hits = [(w, analyze.excluded_by(w, excludes)) for w in words]
        drop = [(w, term) for w, term in hits if term]
        if drop:
            out.append((record.get("record_id") or "",
                        drop, [w for w, term in hits if not term]))
    return out


def run(tables, settings: Settings, apply: bool, *, excludes_override=None,
        out=print) -> int:
    """逐表处理。一张表失败不挡别的表；有失败返回 1。"""
    column = settings.fields.blue_words
    failed = 0
    total_rows = total_words = changed = 0
    for label, table in tables:
        per_table = cli.settings_for_table(settings, table)
        excludes = (list(excludes_override) if excludes_override is not None
                    else list(per_table.blue_word_excludes))
        if not excludes:
            out(f"表「{label}」：登记表里没填「{registry.COL_BLUE_WORD_EXCLUDES}」，跳过")
            continue
        try:
            meta = table.fields_meta()
            if meta is None:
                raise RuntimeError("读不到字段列表（应用没被加进这张表？先跑 cli.py doctor）")
            if column not in meta:
                out(f"表「{label}」：没有「{column}」这一列，跳过")
                continue
            link = settings.fields.link
            records = table.search([c for c in (column, link) if c in meta])
        except Exception as exc:  # noqa: BLE001 —— 一张表读不到不该挡住别的表
            out(f"⚠ 表「{label}」读不到：{exc}")
            failed += 1
            continue
        planned = plan_rows(records, column, excludes)
        links = {r.get("record_id"): feishu.read_text((r.get("fields") or {}).get(link))
                 for r in records}
        out(f"表「{label}」：排除词 {'、'.join(excludes)}；"
            f"{len(planned)} 行要删" if planned else
            f"表「{label}」：排除词 {'、'.join(excludes)}；没有要删的词")
        for record_id, drop, keep in planned:
            shown = "、".join(f"{w}（{term}）" if w != term else w for w, term in drop)
            out(f"  {record_id}  删：{shown}  →  剩：{'、'.join(keep) or '（空）'}"
                + (f"  {links.get(record_id)}" if links.get(record_id) else ""))
            total_rows += 1
            total_words += len(drop)
        if not apply or not planned:
            continue
        try:
            fresh = table.batch_get([record_id for record_id, _, _ in planned])
            updates = [{"record_id": record_id, "fields": {column: keep}}
                       for record_id, _, keep in plan_rows(fresh, column, excludes)]
            if updates:
                table.batch_update(updates)
        except Exception as exc:  # noqa: BLE001
            out(f"  ✗ 没删成：{exc}")
            out("    飞书报错 = 表没动；网络超时的话说不准，不带 --apply 再跑一次看现状")
            failed += 1
            continue
        changed += len(updates)
        out(f"  ✓ 删好了 {len(updates)} 行")

    out("")
    out(f"合计：{total_rows} 行、{total_words} 个词要删" if total_rows
        else "合计：没有要删的词")
    if apply:
        out(f"这次改了 {changed} 行")
    elif total_rows:
        out("把上面的清单给运营确认过，加 --apply 再跑一次才真删")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    cli.load_env_or_exit()
    args = list(argv[1:])
    selected = cli.pop_table_flag(args)
    apply = "--apply" in args
    if apply:
        args.remove("--apply")
    excludes_override = None
    if "--exclude" in args:
        index = args.index("--exclude")
        if index + 1 >= len(args):
            print(__doc__)
            return 2
        excludes_override, _ = registry.read_blue_word_excludes(registry.Entry(
            blue_word_excludes=feishu.read_keywords(args[index + 1])))
        del args[index:index + 2]
        if not selected:
            print("--exclude 要和 --table 一起用：一份竞品名单套到所有表上多半是填错了")
            return 2
        if not excludes_override:
            print("--exclude 里没有能用的词（每个至少 2 个字）")
            return 2
    if args:
        print(__doc__)
        return 2
    settings = cli.build_settings()
    print("会真改表（只动「蓝词字段」这一列，删掉排除词）\n" if apply
          else "只看不改。加 --apply 才真删\n")
    return run(cli._tables(selected), settings, apply, excludes_override=excludes_override)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
