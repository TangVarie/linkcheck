"""「哪张表」怎么写、怎么解析、怎么查重。**纯函数，不发请求。**

这一层原本长在 `cli._tables_from_env` 里，只服务一个来源（环境变量）。
现在来源变成三个——环境变量、飞书注册表、监控面板上的表单——
而后两个是**运营能填的**。同一套解析必须三处共用，否则：

* 报错文案会漂：环境变量里说得清清楚楚，面板上却只回一句「格式不对」
* 安全校验会漏：面板那条路忘了校验字符集，就是把 `tenant_access_token`
  的请求地址交给了填表的人（见 `valid_token`）

所以解析和校验在这里做一次，三个调用方只负责把 `BadTarget` 翻译成
自己的呈现方式（cli 是 `sys.exit`，注册表是把那一行标成配置有误，
面板是在表单下面标红）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 飞书的 app_token / table_id 只会是字母数字。**必须校验**，因为它们会被拼进
# 接口地址，而那个请求带着 tenant_access_token——一个含 `/` 或 `..` 的值就能
# 把它改写到别的 open-apis 端点上。`feishu._url()` 里还有第二道转义。
#
# 管的是**字符集**，不是长度：真实 token 是 17–27 位，但长度下限拦不住任何
# 攻击（`..` 才两个字符），只会在飞书哪天改短 token 格式时误伤合法配置。
# 上限留着挡明显荒唐的值。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{1,64}")

_URL_RE = re.compile(r"/(base|wiki)/([A-Za-z0-9]+)\S*?[?&]table=([A-Za-z0-9]+)")

# 分隔多项用的：分号（中英文）和换行。
SEPARATOR_RE = re.compile(r"[;；\n]+")


class BadTarget(ValueError):
    """这一项解析不了。`str(exc)` 是可以直接给人看的中文。"""


@dataclass(frozen=True)
class TableTarget:
    label: str
    app_token: str
    table_id: str
    # 原来的链接走的是 /base/ 还是 /wiki/。**只用来拼给人点的链接，
    # 一个字都不参与接口调用**——接口那边两种 token 一视同仁（见 parse_target）。
    # 丢掉它的后果是：用 wiki 链接登记的项目，面板上「打开这张表」和
    # 「去这一行」会指向 /base/<wiki-token>，行级直达失效——而这个面板
    # 一半的价值就在那个链接上。
    route: str = "base"

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.label, self.app_token, self.table_id)


def valid_token(value: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(value or ""))


def parse_target(chunk: str, *, default_label: str = "") -> TableTarget:
    """一项 → TableTarget。四种写法都认：

        OKMAN一期=bascnXXX:tblAAA
        OKMAN二期=https://xx.feishu.cn/base/bascnXXX?table=tblBBB
        企业C期=https://xx.feishu.cn/wiki/wikcnXXX?table=tblDDD
        bascnYYY:tblCCC                     （不带标签时标签取 table_id）

    /wiki/ 链接实测直接把地址栏那段 token 当 app_token 用，多维表格接口照样
    认——不用额外调接口换算、也不用多开知识库权限，跟 /base/ 一视同仁。

    `default_label` 给注册表和面板用：那两个地方标签是单独一格/一个输入框，
    不在这段文本里。
    """
    chunk = (chunk or "").strip().strip(",")
    if not chunk:
        raise BadTarget("这一项是空的")

    head, sep, rest = chunk.partition("=")
    # URL 里本来就有 =（?table=tbl...），只有「短标签=」才当标签用
    if sep and "://" not in head and "/" not in head and ":" not in head:
        label, target = head.strip(), rest.strip()
    else:
        label, target = "", chunk

    route = "base"
    match = _URL_RE.search(target)
    if match:
        route, app_token, table_id = match.group(1), match.group(2), match.group(3)
    elif "://" in target:
        raise BadTarget(
            f"这个网址提不出表信息：{target!r}。要用 /base/xxx?table=tblxxx 或 "
            "/wiki/xxx?table=tblxxx 形式的地址（打开目标数据表时浏览器地址栏那串）")
    elif ":" in target:
        app_token, _, table_id = target.partition(":")
        app_token, table_id = app_token.strip(), table_id.strip()
    else:
        raise BadTarget(
            f"这一项看不懂：{chunk!r}。写成 标签=app_token:table_id "
            "或 标签=表格完整网址")

    if not app_token or not table_id:
        raise BadTarget(f"这一项缺 app_token 或 table_id：{chunk!r}")
    for what, token in (("app_token", app_token), ("table_id", table_id)):
        if not valid_token(token):
            raise BadTarget(
                f"{what} 不合法：{token!r}。飞书的 token 只会是字母和数字——"
                "带别的字符的值会被拼进接口地址，而那个请求带着你的 "
                "tenant_access_token，不能放行")
    return TableTarget(label or default_label or table_id, app_token, table_id,
                       route=route)


def parse_many(spec: str, *, default_label: str = "") -> list[TableTarget]:
    """一整段多项配置 → 一批 TableTarget。分号（中英文）或换行分隔。"""
    targets = []
    for chunk in SEPARATOR_RE.split(spec or ""):
        if chunk.strip().strip(","):
            targets.append(parse_target(chunk, default_label=default_label))
    return targets


@dataclass(frozen=True)
class Duplicates:
    """查重结果。两个命名空间分开放，别混成一个 dict——

    一个表的 label 恰好等于另一个表的 table_id 时，混着放会误伤。
    概率低，但「因为撞了个名字所以这张表这轮不刷了」是那种查半天的故障。
    """

    by_table_id: dict
    by_label: dict

    def __bool__(self) -> bool:
        return bool(self.by_table_id or self.by_label)

    def message_for(self, *, table_id: str = "", label: str = "") -> str:
        """这一行有没有踩重复。**精确查表，不做子串匹配。**"""
        return (self.by_table_id.get(table_id)
                or self.by_label.get(label)
                or "")


def find_duplicates(targets: list[TableTarget]) -> Duplicates:
    """查重。**每一组都要抓出来，不是碰到第一组就收工。**

    只报第一组等于把第二组原样放行：那张表会在一轮里被排两遍——钱付两份，
    两份旧快照互相覆盖运营手工打的标签。这是这套东西最贵的一类故障，
    值得多走完一遍循环。

    **查重键是 `table_id` 单独一维，不是 (app_token, table_id)。**
    同一张表既能写成 `/base/bascnXXX?table=tblAAA` 也能写成
    `/wiki/wikcnYYY?table=tblAAA`——两个 app_token 不同、指的是同一张表。
    用二元组当键挡不住这种写法。不同 base 里 table_id 撞车的概率可以忽略，
    宁可误报也别漏。
    """
    by_table_id: dict = {}
    first_seen: dict = {}
    for target in targets:
        if target.table_id in first_seen:
            other = first_seen[target.table_id]
            same = "（同一个 app_token）" if other == target.app_token else (
                f"（app_token 一个是 {other}、一个是 {target.app_token}——"
                "/base/ 和 /wiki/ 两种链接指的是同一张表）")
            by_table_id[target.table_id] = (
                f"{target.table_id} 配了两遍{same}——"
                "同一张表刷两次是白花钱，而且两份旧快照会互相覆盖")
        else:
            first_seen[target.table_id] = target.app_token

    by_label: dict = {}
    labels = [t.label for t in targets]
    duplicated = sorted({l for l in labels if labels.count(l) > 1})
    if duplicated:
        message = (f"标签重复：{'、'.join(duplicated)}——"
                   "--table 会分不清，给每张表起个不同的名字")
        for label in duplicated:
            by_label[label] = message
    return Duplicates(by_table_id=by_table_id, by_label=by_label)


def find_duplicate(targets: list[TableTarget]) -> str:
    """查重，返回**第一句**人话，没重复返回空串。

    给只需要报一条就退出的调用方用（cli 的 `sys.exit`）。要逐行标记出
    **全部**有问题的表，用 `find_duplicates()`。
    """
    found = find_duplicates(targets)
    for message in (*found.by_table_id.values(), *found.by_label.values()):
        return message
    return ""
