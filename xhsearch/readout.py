"""截图读数四列：「数据整理」+「曝光量 / 阅读量 / 互动数」。**纯定义，不发请求。**

运营把后台数据截图 / 发布页截图丢进「相关截图」，一个 AI 字段捷径（豆包
「AI 图片理解」）读图，吐出一行固定格式的字：

    曝光量：600；阅读量：43；互动量：2

三个公式列再把这行字拆成三个能排序、能求和的数字。认不出的项 AI 输出
「/」，VALUE 转不动 → IFERROR 兜成空格子，不会冒出一个假的 0。

**这四列每张表都必须有**（2026-09-23 定的）。机器巡查一个字都不读不写它们，
所以缺了**不拦巡查、只提醒**：体检（面板项目卡、「体检一下」、doctor）按这里
核对——缺列、类型不对、公式和标准对不上都报；两个建表模板都带上；「补齐缺的
列」也建它们。定义只有这一份，体检和建表都从这里取，两边不会漂。

⚠️ 「数据整理」的 AI 那一半**开放接口建不了，也看不见**。2026-09 核对过飞书
官方 SDK（lark-oapi 1.7.3，从接口定义生成）和字段编辑指南：多维表格的字段模型
只有 field_name / type / property / description / is_primary / field_id /
ui_type / is_hidden，没有任何「字段捷径 / AI / 插件 / 模型账号」的概念；那个
捷径还要绑一个**人的**豆包账号，应用身份本来就给不了。所以：

* 建：先建成一列普通文本（捷径本来就挂在文本列上——飞书里打开它，字段类型那一栏
  写的就是「文本」+「AI 图片理解（字段捷径）」），挂捷径那一步交给人，建完原样交代。
* 查：字段接口看不出捷径挂没挂，只能看**数据**——传了截图、这一格却一直空着，
  就是捷径没在干活（见 `unread_rows`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

# AI 读的是这一列。它是普通业务列、不在必备的四列里——但「截图传了、数没出来」
# 这条提醒要靠它才判得出来。
SCREENSHOT_COLUMN = "相关截图"
DATA_COLUMN = "数据整理"

# 你们在用的那份指令，原文照录。它是这一列唯一要人手动粘的东西，所以放在
# 代码里当唯一的真相：建表结果直接把它递到人手上，docs/表结构.md 里那份
# 抄本由测试盯着，两边一个字都不许漂。
DATA_EXTRACT_PROMPT = '''# 任务
从上传的截图中识别并提取内容数据，严格按指定格式输出。

# 图片分类规则
上传图片分为两类，先分类再提取：

1. 后台数据截图：含数据看板/数据统计页面，通常带有"曝光""阅读""互动"等字段标签和对应数值。
2. 内容发布页截图：内容详情页，右下角有三个图标（从左到右：点赞、收藏、评论），每个图标旁有数字。

# 数据提取优先级
1. 优先使用后台数据截图提取全部三项数据。
2. 仅当无后台数据截图时，才使用内容发布页截图。
3. 内容发布页截图仅能提取互动量（=点赞+收藏+评论三个数字之和），曝光量和阅读量输出"/"。

# 多图去重规则
若存在多张同类截图：
- 判定标准：同一内容、同一页面的截图视为相似图。
- 取值规则：以互动量最高的一张为准，其余不计。
- 若互动量相同，取曝光量最高的一张。

# 数据识别要求
- 仅当在图片中识别到明确、可辨认的数值时才输出该数值。
- 数值模糊、被遮挡、无法确认时，该项输出"/"。
- 不得猜测、推算或补全未识别到的数据。

# 输出格式（严格按此格式输出，不输出任何其他内容）
曝光量：；阅读量：；互动量：'''

# 三个拆数公式，照你们表里在用的原样。「阅读量」那条原来是多行写的，这里
# 只是折成一行，一个字符都没改（公式里引号外的空白不影响求值）。注意列名是
# 「互动数」、而 AI 吐的那行字里写的是「互动量」——两边本来就不一样，照旧。
# 公式逻辑有离线测试：拿几种真实形态的「数据整理」原文逐条求值。
FORMULAS = (
    ("曝光量",
     f'IFERROR(VALUE(MID([{DATA_COLUMN}], 5, FIND("；", [{DATA_COLUMN}]) - 5)), "")'),
    ("阅读量",
     f'IFERROR(VALUE(MID([{DATA_COLUMN}], FIND("阅读量：", [{DATA_COLUMN}]) + 4, '
     f'FIND("；", [{DATA_COLUMN}], FIND("阅读量：", [{DATA_COLUMN}]) + 4) '
     f'- FIND("阅读量：", [{DATA_COLUMN}]) - 4)), "")'),
    ("互动数",
     f'IFERROR(VALUE(MID([{DATA_COLUMN}], FIND("互动量：", [{DATA_COLUMN}]) + 4, '
     f'LEN([{DATA_COLUMN}]))), "")'),
)

MANUAL_STEP = (
    f"「{DATA_COLUMN}」建出来是一列普通文本，还要在飞书里挂一次 AI 字段捷径"
    "才会自己读图：打开这一列的设置 → 字段类型选「字段捷径 → AI 图片理解」"
    f"→「原图」选「{SCREENSHOT_COLUMN}」→ 自定义指令粘下面这段 → 关联你自己的"
    "豆包账号 → 生成范围选「整列」、打开「自动更新」。挂好之前「曝光量 / 阅读量 / "
    "互动数」三列会一直是空的——公式没坏，只是还没有东西可拆。"
)


@dataclass(frozen=True)
class Column:
    """必备的一列：叫什么、是什么类型、公式列的标准公式。"""

    name: str
    type_code: int
    type_label: str
    formula: str = ""
    # 建出来之后**还要人在飞书里补一步**才算完整（只有「数据整理」有：挂 AI
    # 捷径）。这一列看着和真的一样却不会自己填，不说出来就是一个安静的坑。
    manual_step: str = ""
    # 那一步里要人粘进去的原文（AI 指令），跟着结果一起递到人手上。
    manual_paste: str = ""
    # 建它之前得先在的列。公式引用「数据整理」：那一列这次没建成的话，
    # 硬建公式只会再报一条看不懂的飞书错误。
    needs: tuple = ()


COLUMNS: tuple = (
    Column(DATA_COLUMN, 1, "文本（上面挂 AI 图片理解字段捷径）",
           manual_step=MANUAL_STEP, manual_paste=DATA_EXTRACT_PROMPT),
    *(Column(name, 20, "公式", formula=expr, needs=(DATA_COLUMN,))
      for name, expr in FORMULAS),
)

NAMES: tuple = tuple(c.name for c in COLUMNS)


# ---------- 公式比对 ----------

# 字段接口里公式按 id 引用列（`bitable::$table[tbl…].$field[fld…]`），
# feishu.readable_formula 会把本表的引用翻回 `[列名]`。翻完还剩这些字样 =
# 有引用没翻回来（别的表、认不出的 id、接口换了写法），这条公式**没法比**。
_UNRESOLVED = ("bitable::", "$table[", "$field[")


def canonical(expression: str) -> str:
    """去掉引号外的空白。运营在飞书里多行缩进写的和模板里一行写的是同一条
    公式；引号**里**的空白要留着——`" "` 和 `""` 是两个不同的字符串。"""
    out: list[str] = []
    quoted = False
    for ch in expression or "":
        if ch == '"':
            quoted = not quoted
        if quoted or not ch.isspace():
            out.append(ch)
    return "".join(out)


def formula_differs(actual: Optional[str], expected: str) -> bool:
    """这一列的公式确定和标准的不一样吗。

    **只在比得了的时候说「不一样」。** 读不到公式（接口没给）、或者读到的里面
    还有没翻回列名的引用，都返回 False：那种情况下说「对不上」可能只是写法
    不同，而一条假提醒会教人不再信这张卡。
    """
    if not isinstance(actual, str):
        return False
    if any(mark in actual for mark in _UNRESOLVED):
        return False
    return canonical(actual) != canonical(expected)


# ---------- 数据上看：截图传了、数没出来 ----------


def blank(value: Any) -> bool:
    """这一格是不是空的。

    飞书对空单元格**不返回键**（取出来是 None）；再兜一下空串、空列表、只剩空
    文本段的富文本。**认不出的形状一律算「有东西」**：AI 捷径写进来的值长什么样
    没在真机上看过，拿不准的时候宁可少报，也别对着一格有字的说它是空的。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return all(blank(item) for item in value)
    if isinstance(value, dict):
        # 富文本段形如 {"text": "...", "type": "text"}；多出别的键就不是它了。
        return set(value) <= {"text", "type"} and blank(value.get("text"))
    return False


def unread_rows(records: Iterable[dict]) -> int:
    """传了「相关截图」、「数据整理」却是空的行数。

    字段接口看不出 AI 捷径挂没挂，这是唯一看得出「捷径没在干活」的地方：
    没挂、没开「自动更新」、关联的豆包账号失效，表现都是这一格一直空着。
    （AI 认不出数时按指令输出「曝光量：/；…」，不是空的——空着就是没跑。）
    """
    count = 0
    for record in records:
        cells = record.get("fields") or {}
        if not blank(cells.get(SCREENSHOT_COLUMN)) and blank(cells.get(DATA_COLUMN)):
            count += 1
    return count


def unread_message(count: int) -> str:
    return (f"截图读数：有 {count} 行传了「{SCREENSHOT_COLUMN}」，「{DATA_COLUMN}」"
            "却是空的——AI 字段捷径多半没挂上、没开「自动更新」，或者关联的豆包账号"
            "失效了，这几行的「曝光量 / 阅读量 / 互动数」也跟着是空的。刚传的截图"
            "等一两分钟再看。")
