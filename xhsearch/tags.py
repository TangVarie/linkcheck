"""流量状态多选字段的合并。

飞书多选字段无论走开放平台 API 还是自动化「更新记录」节点，写入都是**整体覆盖**，
没有原子 append。所以每次写回都必须读-改-写，且必须区分「机器管的标签」和
「人手打的标签」，否则两种结果二选一地发生：

* 覆盖式写入 —— 运营手工标的「已复盘」「客户已确认」被机器清空
* 只增不删 —— 帖子早就掉出爆文了，「爆文」标签永远摘不掉，表逐渐失真

合并公式：

    新值 = (现有标签 − 机器命名空间) ∪ (本次算出的标签)

机器命名空间必须**穷举**。少写一个，那个标签就永远撤不回来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TagMerge:
    """一次合并的结果，保留过程信息以便写回「失败原因」列时能解释清楚。"""

    final: list[str]
    added: list[str]
    removed: list[str]
    dropped_unknown: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def merge(
    current: Sequence[str] | None,
    computed: Iterable[str],
    machine_namespace: Iterable[str],
    known_options: Iterable[str] | None = None,
    exclusive: Iterable[Sequence[str]] = (),
) -> TagMerge:
    """把机器算出的标签并进现有标签，不碰人工标签。

    参数
    ----
    current:
        表里该行 流量状态 的现值。多选为空时飞书可能整个不返回这个字段，
        所以 None 和 [] 要一视同仁。
    computed:
        本次判定得出的机器标签，必须是 machine_namespace 的子集。
    machine_namespace:
        机器管辖的全部标签。不在这个集合里的一律视为人工标签，原样保留。
    known_options:
        该多选字段实际配置了哪些选项。给了就做过滤——飞书 batch_update 是
        全成功或全失败，一个字段里没有的选项名可能让整批几百行一起回滚，
        与其赌服务端会自动建选项，不如在这里挡掉并把它记进 dropped_unknown。
    exclusive:
        互斥组（如热度三档），组内同时只能留一个。只在「选项没建、保留旧
        机器标签」的路径上用，用来圈定**每个被拦标签的同类范围**：
        「大爆」写不进去 → 只保留行上同组的旧档位（爆贴），既不让两个
        档位并存，也绝不顺手把不相干的旧状态标签（比如上一轮的「已失效」）
        复活——那会把一条刚恢复正常的行继续标成死的。
    """
    current_set = [t.strip() for t in (current or []) if t and t.strip()]
    machine = set(machine_namespace)
    computed_set = {t for t in computed if t}

    unexpected = computed_set - machine
    if unexpected:
        raise ValueError(
            f"算出的标签不在机器命名空间内：{sorted(unexpected)}。"
            "要么把它加进 machine_namespace，要么它就是个笔误——"
            "放行会导致这个标签之后永远无法撤回。"
        )

    previous_machine = {t for t in current_set if t in machine}

    dropped: list[str] = []
    if known_options is not None:
        options = set(known_options)
        allowed = {t for t in computed_set if t in options}
        dropped = sorted(computed_set - allowed)
        computed_set = allowed
        if dropped:
            # 想写的标签写不进去（选项没建）时，只保留**同类**的旧标签：
            # 「大爆」被拦 → 留住行上的旧档位「爆贴」（热度信息不清零）；
            # 但绝不把不相干的旧标签一并复活——被拦的是「风控中」时，
            # 行上残留的「已失效」不在它的同类范围里，照常摘掉，
            # 否则一条刚恢复正常的行会继续顶着死亡标签。
            groups = [set(g) for g in exclusive]
            preserved: set[str] = set()
            for tag in dropped:
                category = next((g for g in groups if tag in g), {tag})
                preserved |= previous_machine & category
            computed_set |= preserved

    # 保序：先按原顺序留下人工标签，再追加机器标签，表里看起来才稳定。
    human = [t for t in current_set if t not in machine]
    seen: set[str] = set()
    final: list[str] = []
    for tag in human + sorted(computed_set):
        if tag not in seen:
            seen.add(tag)
            final.append(tag)
    return TagMerge(
        final=final,
        added=sorted(computed_set - previous_machine),
        removed=sorted(previous_machine - computed_set),
        dropped_unknown=dropped,
    )
