"""搜索可见 · 试跑。**只有人亲手起才会走到这里**：面板「搜索可见试跑」那一栏里点了确认
（`trial.py`），或者手工敲 `python3 cli.py search-trial`。没有任何定时的东西调它。

要回答的只有一个问题（2026-11-16 定的）：我们的帖子发出去三天，在这张表要查的
那些词下面，前几页里**到底看不看得见**。如果几乎都看不见，按综合排序查排名这件事
就没有意义，得换办法；看得见，才值得做成每天自动查。所以先花小钱试一次，
不建任何自动的东西。

**词是表的，不是帖子的**（2026-11-16 负责人改的）：一张表要查哪些词，全写在
「搜索关键词」那一列里（多选，或者一格里用逗号、顿号隔开写几个；空格不拆，
写成一句话的格子整格不查——规则在 `parse_cell`）。多选还是文本只看**列类型**。这张表所有行里出现过的词
合起来就是这张表的词——每行都填一样的，或者只填一行，效果一样。每个词拉前几页，
再拿这张表**所有**发布帖去对，看对不对得上；不再是「一篇帖子配一个词」。
两张表写了同一个词，只查一次，结果分别和两张表的帖子对。

* **巡查不受影响**：sweep / queue / row / estimate 一行都没改，不会多发一次请求。
  搜索那个端点登记在 providers 里，但 rows.plan_calls 从不排它。
* **不往项目表写一个字**：只读「搜索关键词」和几列现成的（链接、发布时间、巡查状态）。
* **不留任何客户的东西**：进度文件（本地那份，或者面板存在登记表 base 里的那份，见
  `trial.py`）只有笔记 ID、名次和几个计数，关键词只存哈希；报告只有计数，词用 K01、K02
  代替。标题、昵称、正文在 providers 归一化那一步就丢了，这里根本拿不到。

## 钱：闸门在代码里，发之前查

整个试跑（不是一次运行）最多 `MAX_CALLS` 次请求、`MAX_YUAN` 元。花了多少记在进度
文件里，分几次跑、隔天再跑，都是同一本账。每发一个 HTTP 请求之前先看这本账——
再发一次会过线就停，已经拿到的页照样算数。账是往多了记的：先记账、存盘，再发；
只有传输层**明说没发出去**（建连失败，`Response.not_sent`）的那一次会退回来。限流、
超时、HTTP 400、读回答时断开或出错的页一律按花了钱算（收不收钱没验过）。

## 读不懂的页不当成「到底了」

对方返回了条目、却一条笔记 ID 都认不出来（返回的形状和我们猜的不一样），这一页
**不留**：留下来报告就会说「没搜到」，其实是没读懂。第 1 页就这样，整轮立刻停
（只花这一次的钱），退出码非零；只有一条条目都没有的页才算翻到底了。

## 断了能接着跑，拿到的页不付第二次钱

每拿到一页就存盘。再跑同一条命令，拿到过的页直接用，只补没拿到的。
翻页要用的那两个会话值跟着第一页一起存着。第一页有笔记却没给会话值的，后面的页
翻不了：这一页**不留**、算没拿到（退出码非零），下次重新拿。

## 没查到的帖子算「没查全」，后来才发的帖子不算

一篇帖子在它那张表的哪个词下面都没出现，而这些词里有没查全的（缺页、第 1 页失败、
上限先到一页都没拿到，或者这个词是查完之后才加进表的），它就是「没查全」——说不准，
不是「没搜到」，也不是「付费位」（只在付费位出现也一样）；一页都没拿到的词
按这一轮跑的那一刻算帖龄（进度文件里的 `rounds`）。反过来，词查的那一刻还没发出来
的帖子不和那个词对；它那张表的词查的时候它都还没发，就不进这一轮。

全部是**可以离线测的**：发请求的那一下是注入的（`send`），进度文件的位置是参数
（`State` 存本地文件；`trial.StoredState` 存登记表 base，规则是同一套）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import feishu, protocol, providers, transport
from .rows import SORT_DEFAULT, SORT_LATEST, STATUS_GONE, STATUS_SUSPECT, Row

TRIAL_NAME = "search-visibility-2026-11"
STATE_VERSION = 1

# —— 硬上限（2026-11-16 定的：试跑最多 ¥20）——
# 计划是 185 次（约 ¥13），多出来的是给失败的页重试用的。两个闸门都查，先到哪个算哪个：
# 按现在的单价（¥0.072 一次）金额那个先到，277 次。
MAX_CALLS = 280
MAX_YUAN = 20.0
# 词的个数是定死的：上限是按 15 个词算出来的，30 个词要 350 次，装不下。
MAX_KEYWORDS = 15
# 像是整句话的词不查：搜一句话什么都说明不了，钱照花。
MAX_KEYWORD_CHARS = 30

# 两次请求之间至少隔这么久。对方的限速是每秒 10 次，我们不赶时间。
PACE_SECONDS = 0.5
# 限流时对方没说等多久，就等这么久（和 runner 里一个数）。
RATE_LIMIT_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class Round:
    """试跑的一轮：用哪种排序、每个词翻几页、查前几个词。"""

    name: str
    sort: str
    pages: int
    keywords: int
    label: str

    @property
    def planned_calls(self) -> int:
        return self.pages * self.keywords


# 五轮加起来 45 + 30 + 45 + 45 + 20 = 185 次。**一次只跑一轮**，默认第一轮——
# 那一轮就能回答「前几页里看不看得见」；后面几轮是为了知道名次稳不稳、能翻多深，
# 要不要跑看第一轮的结果。
ROUNDS: dict[str, Round] = {r.name: r for r in (
    Round("first", SORT_DEFAULT, 3, MAX_KEYWORDS, "第一轮：综合排序，每个词 3 页"),
    Round("latest", SORT_LATEST, 2, MAX_KEYWORDS, "最新排序，每个词 2 页（看收没收录）"),
    Round("repeat1", SORT_DEFAULT, 3, MAX_KEYWORDS, "隔一小时再查一遍（看名次稳不稳）"),
    Round("repeat2", SORT_DEFAULT, 3, MAX_KEYWORDS, "隔一天再查一遍（看名次稳不稳）"),
    Round("depth", SORT_DEFAULT, 10, 2, "前两个词翻到第 10 页（看能翻多深）"),
)}
DEFAULT_ROUND = "first"
PLANNED_CALLS = sum(r.planned_calls for r in ROUNDS.values())

AGE_BANDS = ("3天内", "4到7天", "更早", "发布时间空")
FOUND_BANDS = ("前20", "21到40", "41到60", "60以后")
PAID = "付费位"
NOT_FOUND = "没搜到"
INCOMPLETE = "没查全"
RESULT_LABELS = (*FOUND_BANDS, PAID, NOT_FOUND, INCOMPLETE)

# 没进试跑的行，按原因计数。
SKIP_NOT_XHS = "不是小红书"
SKIP_DEAD = "已失效或疑似受限"
SKIP_NO_ID = "拿不到笔记 ID"
SKIP_NO_TIME = "发布时间空着"
SKIP_NO_WORDS = "所在的表一个词都没写"

# 一页里有条目、却一条笔记 ID 都认不出来：记在失败清单里的种类名。
UNREADABLE = "unreadable_page"
# 第 1 页有笔记、却没给翻页要回传的会话值：后面的页翻不了，这一页也不留。
NO_SESSION = "no_session"
SESSION_KEYS = ("search_id", "search_session_id")


# ---------- 读表：哪些帖子、哪些词 ----------


# 文本格子里只按这些符号拆：中英文逗号、顿号、中英文分号、竖线、斜杠、# 号、换行。
# **空格不拆**——「cGMP 因子」是一个词；一句话也不会被拆成一串单字去花钱。
_SEPARATORS = re.compile(r"[，,、;；|｜/／#＃\r\n]+")
# 文本格子里出现这些，就当它是一句话（备注），不是词表：整格不查。
_SENTENCE = re.compile(r"[。！？!?：:…]")
# 行首的编号（「1. 」「2）」「3、」）不是词的一部分。
# 句点后面紧跟数字的是小数（「3.5mm耳机」「2.4G无线鼠标」），那是词本身，不能当编号削掉。
_NUMBERING = re.compile(r"(?m)^\s*(?:\d{1,2}\s*(?:[.．](?!\d)|[)）])|\d、)\s*")

# 飞书的列类型：1 文本，4 多选。
COLUMN_TEXT = 1
COLUMN_MULTI = 4


@dataclass
class CellWords:
    """一格「搜索关键词」读出来的东西。"""

    words: list[str] = field(default_factory=list)
    too_long: int = 0          # 多选里超过 MAX_KEYWORD_CHARS 的选项，不查
    sentence: bool = False     # 文本格子像一句话（有句号、问号、冒号，或有一段太长）：整格不查


def _segments_text(cell: Any) -> str:
    """文本格子 → 人打的字。@人、链接这些分段不是词，不要。"""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        parts = []
        for item in cell:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type", "text") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def keyword_cell(cell: Any, column_type: Optional[int]) -> Any:
    """按**列的类型**把飞书读回来的格子整理成 `parse_cell` 认的样子。

    文本列读回来也是一个数组（富文本分段），里面 @人 的那一段带着 `name`——光看形状会把它
    当成多选的选项，把同事的名字拿去搜。所以多选还是文本，只看列类型：
    多选 → 选项名的列表；别的（文本）→ 一个字符串。
    """
    if column_type == COLUMN_MULTI:
        return feishu.read_multi_select(cell)
    return _segments_text(cell)


def parse_cell(cell: Any) -> CellWords:
    """「搜索关键词」的一格 → 这一格里写的词（去掉前后空白和词前面的 #，一格之内去重）。

    * **多选**（一个全是字符串的列表）：一个选项就是一个词，里面有逗号也不拆。
      超过 MAX_KEYWORD_CHARS 的选项不查。
    * **文本**（字符串，或富文本分段）：只按逗号（中英文）、顿号、分号（中英文）、竖线、
      斜杠、# 号、换行拆；**空格不拆**。行首的「1.」「2）」「3、」这种编号去掉。
      格子里有句号、问号、感叹号、冒号、省略号，或者拆出来有一段超过 MAX_KEYWORD_CHARS：
      这是一句话，不是词表，**整格不查**。
    * 两种都一样：词里连着的空白算一个空格；一个字母、数字、汉字都没有的（只剩符号）不要。
    """
    out = CellWords()
    multi = isinstance(cell, list) and bool(cell) and all(isinstance(i, str) for i in cell)
    if multi:
        pieces = list(cell)
    else:
        text = _NUMBERING.sub("", _segments_text(cell))
        pieces = _SEPARATORS.split(text)
        if _SENTENCE.search(text):
            out.sentence = True
    seen: set[str] = set()
    for word in pieces:
        word = " ".join(str(word or "").split()).lstrip("#＃").strip()
        if not word or not any(ch.isalnum() for ch in word):
            continue
        if len(word) > MAX_KEYWORD_CHARS:
            if multi:
                out.too_long += 1
                continue
            out.sentence = True
        if keyword_hash(word) not in seen:
            seen.add(keyword_hash(word))
            out.words.append(word)
    if out.sentence:
        out.words = []
    return out


def split_keywords(cell: Any) -> list[str]:
    """`parse_cell` 的词那一半。"""
    return parse_cell(cell).words


def keyword_hash(word: str) -> str:
    """词 → 进度文件里用的代号。大小写不分（搜索本来也不分）。"""
    return hashlib.sha256(word.strip().casefold().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Post:
    note_id: str
    # 这一篇所在的那张表要查的**全部**词（原词，只在内存里，不落盘、不进报告）。
    # 同一张表的帖子带的是同一组。
    keywords: tuple[str, ...] = ()
    publish_ms: Optional[int] = None   # 表里填的「发布时间」；空着就是 None，不猜
    comments: Optional[int] = None     # 表里现成的评论数


@dataclass
class Intake:
    """这次读表读到了什么。"""

    posts: list[Post] = field(default_factory=list)
    # 按第一次出现的顺序：词的原文（同一个词大小写不同只留第一种写法）。
    # 只收「有帖子可对」的表的词——一篇能查的帖子都没有的表，它的词不占名额。
    keywords: list[str] = field(default_factory=list)
    tables: int = 0                   # 读了几张表
    tables_with_keywords: int = 0     # 其中写了词的
    rows: int = 0                     # 一共几行
    rows_with_keyword: int = 0        # 其中「搜索关键词」那一格不空的
    too_long: int = 0                 # 太长（像整句话）没收的多选选项
    sentence_cells: int = 0           # 像一句话（备注）、整格没收的文本格子
    # 短链还没展开的行（只在不展开的模式下有，也就是 --dry-run）。
    short_links: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def _skip(self, reason: str, count: int = 1) -> None:
        if count:
            self.skipped[reason] = self.skipped.get(reason, 0) + count


def intake(tables: Iterable[Iterable[tuple[Row, Any]]], *,
           resolve: Optional[Callable[[str], Optional[str]]] = None) -> Intake:
    """一张表一份（行, 那一行「搜索关键词」的原始格子）→ 要查的词和要对的帖子。

    **词是表的**：一张表的词 = 它所有行里写过的词合起来（去重，按第一次出现排）。
    每行都填一样的、只填一行，结果一样。格子空着的行照样是这张表的帖子。
    **帖子是这张表里所有查得了的行**：小红书、没失效、填了发布时间、拿得到笔记 ID。
    每一篇都带着整张表的词，报告里拿它去对每一个词。

    一个词都没写的表整张不看（连免费的短链展开都不做）。
    `resolve` 是免费的短链展开（链接 → 笔记 ID 或 None）；不给就不展开，
    短链行只记个数（--dry-run 用：那时帖子数是下界）。
    """
    out = Intake()
    seen_words: dict[str, str] = {}
    for table in tables:
        rows = list(table)
        out.tables += 1
        out.rows += len(rows)
        words: list[str] = []
        own: set[str] = set()
        for _row, cell in rows:
            parsed = parse_cell(cell)
            out.too_long += parsed.too_long
            out.sentence_cells += parsed.sentence
            if parsed.words:
                out.rows_with_keyword += 1
            for word in parsed.words:
                digest = keyword_hash(word)
                if digest in own:
                    continue
                own.add(digest)
                # 两张表写了同一个词：是同一个词，用先见到的那种写法。
                words.append(seen_words.get(digest, word))
        if not words:
            if rows:
                out._skip(SKIP_NO_WORDS, len(rows))
            continue
        out.tables_with_keywords += 1
        group = tuple(words)
        posts: list[Post] = []
        seen_posts: set[str] = set()
        pending = 0
        for row, _cell in rows:
            link = row.parsed
            if link.platform != "xhs":
                out._skip(SKIP_NOT_XHS)
                continue
            if row.refresh_status in (STATUS_GONE, STATUS_SUSPECT):
                # 已失效、疑似受限的帖子搜不到是应该的，对了也说明不了什么。
                out._skip(SKIP_DEAD)
                continue
            if row.publish_time_ms is None:
                # 试跑只问「发了三天的帖子看不看得见」：发布时间空着就算不出它发了几天，
                # 对上了也进不了任何一档。不拿笔记 ID 去猜，也不去展开它的短链。
                out._skip(SKIP_NO_TIME)
                continue
            note_id = link.content_id
            if not note_id and link.url:
                if resolve is None:
                    pending += 1
                    continue
                note_id = resolve(link.url)
            if not note_id:
                out._skip(SKIP_NO_ID)
                continue
            note_id = str(note_id).lower()
            if note_id in seen_posts:
                continue                      # 同一篇在一张表里出现两行：算一篇
            seen_posts.add(note_id)
            posts.append(Post(note_id=note_id, keywords=group,
                              publish_ms=row.publish_time_ms,
                              comments=row.previous_comment_count))
        out.short_links += pending
        if not posts and not pending:
            continue                          # 没有一篇能对的帖子：这张表的词不查
        out.posts.extend(posts)
        for word in words:
            digest = keyword_hash(word)
            if digest not in seen_words:
                seen_words[digest] = word
                out.keywords.append(word)
    return out


# ---------- 进度文件：账本 + 拿到过的页 ----------


class StateError(RuntimeError):
    """进度文件用不了。`str(exc)` 是可以直接给人看的中文。"""


class State:
    """本地那份进度文件。**只有 ID、名次、计数和哈希**，没有词的原文。

    里面**不分帖子、也不分表**：页是按「轮 + 词 + 页」记的。哪篇帖子对哪些词，
    每次出报告时按当时读到的表现算。所以「一篇帖子配一个词」那时候存下的进度文件
    照样能读、能接着跑，账也是同一本（见 docs/搜索可见试跑.md 第五节）。

        {"version", "trial",
         "calls": 一共发了几个请求, "yuan": 按多了算一共花了多少,
         "keywords": {词的哈希: 编号（K01 的 1）},
         "pages": {"轮|词的哈希|页": {"at": 秒, "ids": [按顺序的笔记 ID],
                   "ads": [像广告位的那几条在 ids 里的下标], "items", "other",
                   "types": {类型名: 条数}, "keys": [键名], "sid": {翻页会话}}},
         "rounds": {"轮": {"at": 最近一次跑这一轮是哪一刻（秒）,
                          "kw": [这一轮定下来要查的词的哈希]}},
         "failures": [{"round", "kw", "page", "kind", "http", "at"}],
         "balance": [{"round", "drop_usd", "requests", "at"}]}
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "version": STATE_VERSION, "trial": TRIAL_NAME, "calls": 0, "yuan": 0.0,
            "keywords": {}, "pages": {}, "rounds": {}, "failures": [], "balance": [],
        }

    @classmethod
    def load(cls, path: Path) -> "State":
        state = cls(path)
        if not state.path.exists():
            return state
        try:
            data = json.loads(state.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 读不懂就停，绝不当成「一分钱没花过」重新开始——那等于把上限清零。
            raise StateError(
                f"进度文件读不懂（{type(exc).__name__}）：{state.path}。"
                "里面记着这次试跑已经花了多少，不能当它不存在。"
                "确认不要了再手工删掉它") from exc
        state.adopt(data, str(state.path), "换一个 --state-dir")
        return state

    def adopt(self, data: Any, where: str, remedy: str = "") -> None:
        """把读回来的那份账本收下——先看它是不是这次试跑的、形状对不对。
        本地文件和存在登记表 base 里的那份（见 trial.py）用同一道检查。"""
        if not isinstance(data, dict) or data.get("trial") != TRIAL_NAME \
                or data.get("version") != STATE_VERSION:
            raise StateError(f"进度文件不是这次试跑的（{where}）"
                             + (f"，{remedy}" if remedy else ""))
        data.setdefault("rounds", {})     # 早先存的进度文件没有这一项
        for key, default in self.data.items():
            if not isinstance(data.get(key), type(default)) \
                    and not (key == "yuan" and isinstance(data.get(key), int)):
                raise StateError(f"进度文件里「{key}」的形状不对（{where}）")
        self.data = data

    def save(self) -> None:
        """原子写：先写旁边的临时文件再换名，断电也不会留下半份账本。"""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, self.path)

    # —— 账本 ——

    @property
    def calls(self) -> int:
        return int(self.data["calls"])

    @property
    def yuan(self) -> float:
        return float(self.data["yuan"])

    def blocked(self, price: float) -> str:
        """再发一个请求会不会过线。空串 = 可以发；否则是停下来的原因。"""
        if self.calls + 1 > MAX_CALLS:
            return f"次数上限到了：已经发了 {self.calls} 次，上限 {MAX_CALLS} 次"
        if self.yuan + price > MAX_YUAN + 1e-9:
            return (f"金额上限到了：已经记了 ¥{self.yuan:.2f}，再发一次（¥{price:.3f}）"
                    f"会超过 ¥{MAX_YUAN:.0f}")
        return ""

    def charge(self, price: float) -> None:
        self.data["calls"] = self.calls + 1
        self.data["yuan"] = round(self.yuan + price, 6)

    def refund(self, price: float) -> None:
        self.data["calls"] = max(0, self.calls - 1)
        self.data["yuan"] = round(max(0.0, self.yuan - price), 6)

    # —— 词的编号 ——

    def number(self, digest: str) -> Optional[int]:
        value = self.data["keywords"].get(digest)
        return value if isinstance(value, int) else None

    def register(self, words: list[str]) -> str:
        """给这次读到的词编号（已经有号的不变）。返回空串 = 好了；否则是拒跑的原因。

        **先数再记**：加上新词会超过 15 个就一个都不记——记了一半再拒，下次这几个
        没查过的词还占着名额。
        """
        fresh = [keyword_hash(w) for w in words]
        fresh = [d for i, d in enumerate(fresh)
                 if d not in self.data["keywords"] and d not in fresh[:i]]
        total = len(self.data["keywords"]) + len(fresh)
        if total > MAX_KEYWORDS:
            return (f"词太多了：这次试跑最多 {MAX_KEYWORDS} 个不同的词，"
                    f"已经查过的加上表里新出现的一共 {total} 个。"
                    f"上限（{MAX_CALLS} 次 / ¥{MAX_YUAN:.0f}）是按 {MAX_KEYWORDS} 个词算的，"
                    "多一个都装不下——把「搜索关键词」里多出来的词删掉再跑")
        for digest in fresh:
            self.data["keywords"][digest] = len(self.data["keywords"]) + 1
        return ""

    # —— 页 ——

    @staticmethod
    def page_key(round_name: str, digest: str, page: int) -> str:
        return f"{round_name}|{digest}|{page}"

    def page(self, round_name: str, digest: str, page: int) -> Optional[dict]:
        """拿到过的那一页；没有就是 None。读不懂的页（见 `unreadable`）、翻不下去的
        第 1 页（见 `sessionless`）也当没有：现在不会再存这种页，这里是防着进度文件里
        已经有一份——当没有，下次跑才会重新拿。"""
        value = self.data["pages"].get(self.page_key(round_name, digest, page))
        if not isinstance(value, dict) or unreadable(value):
            return None
        round_ = ROUNDS.get(round_name)
        if page == 1 and round_ is not None and round_.pages > 1 and sessionless(value):
            return None
        return value

    # —— 每一轮是哪一刻跑的 ——

    def mark_round(self, round_name: str, at: float, digests: list[str]) -> None:
        """记下这一轮是哪一刻跑的、定下来查哪几个词（只有哈希）。"""
        self.data["rounds"][round_name] = {"at": int(at), "kw": list(digests)}

    def selection(self, round_name: str) -> tuple[list[str], bool]:
        """这一轮算哪几个词 →（词的哈希, 是不是当时记下来的）。

        记过就用记的那份（`rounds[轮]["kw"]`）——包括第 1 页失败、一页都没拿到的词。
        早先的进度文件没记：退回「这一轮里留下过痕迹的词」（有页，或者有失败记录），
        按编号排。只认编过号的词。
        """
        entry = self.data["rounds"].get(round_name)
        stored = entry.get("kw") if isinstance(entry, dict) else None
        if isinstance(stored, list) and all(isinstance(d, str) for d in stored):
            seen: list[str] = []
            for digest in stored:
                if digest not in seen and self.number(digest) is not None:
                    seen.append(digest)
            return seen, True
        touched = {k.split("|")[1] for k in self.data["pages"]
                   if k.startswith(f"{round_name}|") and k.count("|") == 2}
        touched |= {str(f.get("kw")) for f in self.data["failures"]
                    if isinstance(f, dict) and f.get("round") == round_name}
        known = sorted((self.number(d), d) for d in touched if self.number(d) is not None)
        return [d for _n, d in known], False

    def round_at(self, round_name: str) -> float:
        """这一轮的观察时刻：最近一次跑它是哪一刻。没查到的词（第 1 页失败、上限先到）
        没有自己的第 1 页时间，帖龄就按这个算。早先的进度文件没记，退回这一轮里
        最晚的那一页 / 那一次失败的时间；都没有就是 0。"""
        entry = self.data["rounds"].get(round_name)
        if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) \
                and entry["at"] > 0:
            return float(entry["at"])
        prefix = f"{round_name}|"
        stamps = [v.get("at") for k, v in self.data["pages"].items()
                  if k.startswith(prefix) and isinstance(v, dict)]
        stamps += [f.get("at") for f in self.data["failures"]
                   if isinstance(f, dict) and f.get("round") == round_name]
        return float(max((s for s in stamps if isinstance(s, (int, float))), default=0))

    def keep_page(self, round_name: str, digest: str, page: int,
                  data: dict[str, Any], at: float) -> dict:
        notes = [n for n in data.get("notes") or [] if isinstance(n, dict)]
        record = {
            "at": int(at),
            "ids": [str(n.get("note_id") or "") for n in notes],
            "ads": [i for i, n in enumerate(notes) if n.get("ad")],
            "items": int(data.get("items") or 0),
            "other": int(data.get("other_items") or 0),
            "types": dict(data.get("model_types") or {}),
            "keys": list(data.get("item_keys") or []),
            "sid": {k: str(data[k]) for k in SESSION_KEYS if data.get(k)},
        }
        self.data["pages"][self.page_key(round_name, digest, page)] = record
        return record

    def note_failure(self, round_name: str, digest: str, page: int,
                     kind: str, http: Optional[int], at: float,
                     shape: Optional[dict[str, Any]] = None) -> None:
        entry = {"round": round_name, "kw": digest, "page": page,
                 "kind": kind, "http": http, "at": int(at)}
        if shape:
            # 读不懂的页：留下条目的类型和键名（只有名字，没有内容），好知道该怎么改。
            entry["items"] = int(shape.get("items") or 0)
            entry["types"] = dict(shape.get("model_types") or {})
            entry["keys"] = list(shape.get("item_keys") or [])
        self.data["failures"].append(entry)


def unreadable(page: dict[str, Any]) -> bool:
    """这一页有条目、却一条笔记 ID 都没认出来——是没读懂，不是到底了。
    两种形状都认：归一化后的（`notes`）和进度文件里的（`ids`）。"""
    ids = page.get("ids") if "ids" in page else page.get("notes")
    return not ids and int(page.get("items") or 0) > 0


def sessionless(page: dict[str, Any]) -> bool:
    """这一页有笔记、却没给翻页要回传的两个会话值里的任何一个——后面的页翻不了。
    只对第 1 页有意义。两种形状都认：归一化后的和进度文件里的（`sid`）。"""
    if "ids" in page:
        return bool(page.get("ids")) and not page.get("sid")
    return bool(page.get("notes")) and not any(page.get(k) for k in SESSION_KEYS)


# ---------- 发请求 ----------


Send = Callable[[providers.Request, float], transport.Response]


def _send_once(request: providers.Request, timeout: float) -> transport.Response:
    """发**一个** HTTP 请求。不用传输层自带的三次重试：每一个请求发之前都得先过账本。"""
    return transport.request_with_retry(request.method, request.url, request.headers,
                                        request.body, timeout=timeout, attempts=1)


@dataclass
class RunResult:
    round: str
    fetched: int = 0          # 这次新拿到的页
    reused: int = 0           # 进度文件里已经有、没再花钱的页
    failed: int = 0           # 这次没拿到的页
    requests: int = 0         # 这次发出去的 HTTP 请求（进账的）
    yuan: float = 0.0
    todo: int = 0             # 这一轮还没拿到的页（停下来时）
    stopped: str = ""         # 非空 = 中途停了，原因

    @property
    def clean(self) -> bool:
        return not self.stopped and not self.failed


def select(round_: Round, available: list[str],
           state: State) -> tuple[list[str], list[str], list[str]]:
    """这一轮查哪几个词 →（这一轮定下来的全部词, 这一次能查的, 这一次表里有却排不进的）。

    `available` 是这一次读表读到的词（哈希，按编号排好）。规则只有一条，报告、演练、
    真跑、接着跑都用它：**一轮的词一旦定下就不换，只往空位里补**。

    - 以前跑这一轮时定下的词（见 `State.selection`）全都留着，不管这一次的表里有没有它；
    - 还有空位（这一轮最多 `round_.keywords` 个词），就按编号从这一次的词里补；
    - 补不进去的词这一轮不查——不然换一张表再跑一次，「最多 20 次」的那一轮就能
      查出第三、第四个词来，花的比这一轮说好的多。

    拿到过的页按「轮 + 词 + 页」记着，和这一次点了哪张表没关系，所以不会付第二次钱。
    纯函数，不改进度文件。
    """
    selection = list(state.selection(round_.name)[0])
    for digest in available:
        if digest not in selection and len(selection) < round_.keywords:
            selection.append(digest)
    now = [d for d in available if d in selection]
    left = [d for d in available if d not in selection]
    return selection, now, left


def plan(round_: Round, digests: list[str], state: State) -> tuple[int, int]:
    """这一轮（最多还要发几页, 已经拿到几页）。按「每个词都翻满」算，是上界。
    `digests` 是要算的那几个词（调用方用 `select` 定好的，这里不再截）。"""
    have = need = 0
    for digest in digests:
        ended = False
        for page in range(1, round_.pages + 1):
            record = state.page(round_.name, digest, page)
            if record is not None:
                have += 1
                ended = not record.get("ids")
            elif not ended:
                need += 1
    return need, have


def run_round(round_: Round, keywords: list[tuple[str, str]], state: State, api_key: str, *,
              send: Optional[Send] = None, sleep: Optional[Callable[[float], None]] = None,
              clock: Optional[Callable[[], float]] = None,
              log: Callable[[str], None] = print,
              timeout: float = 30.0, pace: Optional[float] = None,
              guard: Optional[Callable[[], str]] = None,
              budget: Optional[int] = None) -> RunResult:
    """跑一轮。`keywords` 是按编号排好的 [(词的原文, 哈希)]。

    `budget` 是这一次最多发几个请求（开跑前告诉人的那个「最多 N 次」）。失败重试、
    限流重试的那一次也是一个进账的请求，也算在里面：发满了就停，差的页下次再补。
    不给就只受总上限管。

    `guard` 是每发一个付费请求之前多问的一句「还能不能发」（空串 = 能；否则是停下来的
    原因）。账本存在登记表 base 里的时候用它看租约还在不在自己手里（见 trial.py）。
    账本存不上（`state.save()` 抛 `StateError`）就停：发之前存不上，这一次不发；
    发之后存不上，后面的都不发。

    日志里**不出现词的原文**（只说 K 几）、不出现地址、不出现对方回的错误正文
    ——这条命令可能跑在日志公开的地方。
    """
    provider = providers.get_provider(providers.TIKHUB)
    price = provider.yuan_per_call("xhs", providers.SEARCH)
    # 在调用时才取默认值，不绑在参数上——绑死了测试就换不掉，一个限流用例能睡 5 秒。
    send = send or _send_once
    sleep = sleep or time.sleep
    clock = clock or time.time
    pace = PACE_SECONDS if pace is None else pace
    out = RunResult(round_.name)
    selection, chosen, left = select(round_, [digest for _word, digest in keywords], state)
    keywords = [(word, digest) for word, digest in keywords if digest in chosen]
    # 这一轮是哪一刻跑的、定下来查哪几个词：报告按记下的这份词来算，没查到的词
    # （第 1 页失败、上限先到）也在里面，帖龄按这一刻算（不发请求、不花钱）。
    state.mark_round(round_.name, clock(), selection)
    if left:
        log(f"  这一轮最多 {round_.keywords} 个词，已经定了 {len(selection)} 个；"
            f"表里另外 {len(left)} 个词这一轮不查")
    try:
        _run_round(round_, keywords, chosen, state, api_key, provider, price, out,
                   send, sleep, clock, log, timeout, pace, guard, budget)
    except StateError as exc:
        # 发完请求之后账本存不上：这一次的钱在发之前就记上、存好了，只是这一页没留住。
        # 不接着发——接着发的每一页都留不住，全是白花。
        out.stopped = f"账本存不上，停了：{exc}"
    out.todo = plan(round_, chosen, state)[0]
    return out


def _run_round(round_: Round, keywords: list[tuple[str, str]], chosen: list[str],
               state: State, api_key: str, provider: Any, price: float, out: RunResult,
               send: Send, sleep: Callable[[float], None], clock: Callable[[], float],
               log: Callable[[str], None], timeout: float, pace: float,
               guard: Optional[Callable[[], str]], budget: Optional[int] = None) -> None:
    """`run_round` 的正文：一个词一个词、一页一页地拿。停下来的原因写在 `out.stopped`。"""
    last_sent = [0.0]

    def fetch(word: str, digest: str, page: int, session: dict) -> Optional[protocol.Result]:
        """拿一页。None = 整轮该停了（原因在 out.stopped）。"""
        request = provider.build(api_key, "xhs", providers.SEARCH, {
            "keyword": word, "page": page, "sort": round_.sort, **session})
        tries = 0
        while True:
            reason = state.blocked(price)
            if not reason and budget is not None and out.requests >= budget:
                reason = (f"这一轮说好最多发 {budget} 次，已经发满了"
                          "（有请求失败后重试过，重试的那一次也算次数）")
            reason = reason or (guard() if guard else "")
            if reason:
                out.stopped = reason
                return None
            # 先记账、存盘，再发：发出去之后进程被杀，这一次也在账上。
            state.charge(price)
            try:
                state.save()
            except StateError as exc:
                # 这一笔没存上，就不发：存不上还发，重启之后这次花的钱就不在账上了。
                state.refund(price)
                out.stopped = f"账本存不上，这一次没发：{exc}"
                return None
            wait = pace - (clock() - last_sent[0])
            if wait > 0:
                sleep(wait)
            response = send(request, timeout)
            last_sent[0] = clock()
            tries += 1
            # 只有传输层**明说没发出去**（建连那一步失败）才退账。别的 status 0
            # ——超时、被断开、读回答时的 TLS / 系统错误——都可能已经到了对方那边。
            unreached = response.status == 0 and bool(getattr(response, "not_sent", False))
            if unreached:
                # 建连失败：对方根本没收到，这一次不算。
                state.refund(price)
                state.save()
            else:
                out.requests += 1
                out.yuan += price
            result = provider.parse("xhs", providers.SEARCH, response.status,
                                    response.content_type, response.body,
                                    response.request_id, api_key)
            if isinstance(result, protocol.Ok):
                return result
            if result.kind in protocol.FATAL:
                out.stopped = ("TikHub 的 Key 不对或被拦了" if result.kind is protocol.Failure.AUTH
                               else "TikHub 余额不够了")
                state.note_failure(round_.name, digest, page, result.kind.value,
                                   result.http_status, clock())
                return None
            if unreached and tries >= 2:
                out.stopped = "连不上 TikHub（网络不通），这两次没算钱"
                return None
            if result.kind is protocol.Failure.RATE_LIMIT:
                if tries >= 2:
                    state.note_failure(round_.name, digest, page, result.kind.value,
                                       result.http_status, clock())
                    out.stopped = "被限流了两次，过一会儿再跑同一条命令接着来"
                    return None
                sleep(protocol.clamp_retry_after(
                    result.retry_after_seconds if result.retry_after_seconds is not None
                    else response.retry_after) or RATE_LIMIT_WAIT_SECONDS)
                continue
            if (result.kind is protocol.Failure.TRANSPORT and tries < 2
                    and result.code not in providers.CONTRACT_CODES):
                continue                  # 5xx、超时：再试一次（照样先过账本）
            return result

    for word, digest in keywords:
        number = state.number(digest) or 0
        session: dict[str, str] = {}
        for page in range(1, round_.pages + 1):
            record = state.page(round_.name, digest, page)
            if record is None:
                if page > 1 and not session:
                    # 第 2 页起要回传第 1 页给的会话值；没给就不翻（翻了多半是白花钱）。
                    # 正常走不到这儿（这种第 1 页不留，见下面）；走到了也算没拿到。
                    out.failed += 1
                    log(f"  K{number:02d} 第 1 页没给翻页用的会话值，后面的页不翻；这个词没查全")
                    break
                result = fetch(word, digest, page, session if page > 1 else {})
                if result is None:
                    try:
                        state.save()
                    except StateError:
                        pass              # 停下来的原因已经写在 out.stopped 里了
                    return
                if isinstance(result, protocol.Err):
                    out.failed += 1
                    state.note_failure(round_.name, digest, page, result.kind.value,
                                       result.http_status, clock())
                    state.save()
                    log(f"  K{number:02d} 第 {page} 页没拿到（{result.kind.value}"
                        f"{f'，HTTP {result.http_status}' if result.http_status else ''}），"
                        "这个词后面的页先不翻；再跑一次会补")
                    break
                if unreadable(result.data):
                    # 有条目、没有一条认得出笔记 ID：不留这一页（留了就成了「到底了」，
                    # 报告会把没读懂说成没搜到，而且以后再也不会重新拿）。
                    out.failed += 1
                    state.note_failure(round_.name, digest, page, UNREADABLE, None,
                                       clock(), shape=result.data)
                    state.save()
                    count = int(result.data.get("items") or 0)
                    if page == 1:
                        # 第 1 页就读不懂 = 返回的形状和我们猜的不一样，每个词都会这样。
                        # 现在就停：只花这一次的钱，不把 15 个词的第 1 页都搭进去。
                        out.stopped = (f"K{number:02d} 第 1 页返回了 {count} 条，却一条笔记 ID "
                                       "都认不出来（返回的形状和代码猜的不一样）。这不是「没搜到」，"
                                       "要先改代码才能接着查")
                        return
                    log(f"  K{number:02d} 第 {page} 页返回了 {count} 条，却一条笔记 ID 都认不出来，"
                        "这一页不算数（不是到底了）；这个词记成没查全")
                    break
                if page == 1 and round_.pages > 1 and sessionless(result.data):
                    # 有笔记、却没给翻页用的会话值：后面的页翻不了，这个词查不全。
                    # 这一页也不留——留了以后每次都沿用它，永远补不上后面的页。
                    out.failed += 1
                    state.note_failure(round_.name, digest, page, NO_SESSION, None, clock())
                    state.save()
                    log(f"  K{number:02d} 第 1 页没给翻页用的会话值，后面的页翻不了；"
                        "这一页不留，这个词记成没查全，再跑一次会重新拿")
                    break
                record = state.keep_page(round_.name, digest, page, result.data, clock())
                state.save()
                out.fetched += 1
            else:
                out.reused += 1
            if page == 1:
                session = dict(record.get("sid") or {})
            if not record.get("ids"):
                break                     # 这一页一条条目都没有：到底了


# ---------- 报告：只有计数 ----------


def age_band(publish_ms: Optional[int], at_seconds: float, tz: timezone) -> str:
    """查的那一刻，这一篇发了几天——按日历日（表里日期列显示的那个时区）。
    只认表里填的「发布时间」，空着就是「发布时间空」，不拿笔记 ID 去猜。"""
    if publish_ms is None:
        return AGE_BANDS[3]
    try:
        published = datetime.fromtimestamp(publish_ms / 1000, tz=tz).date()
        seen = datetime.fromtimestamp(at_seconds, tz=tz).date()
    except (OverflowError, OSError, ValueError):
        return AGE_BANDS[3]
    days = (seen - published).days
    if days <= 3:
        return AGE_BANDS[0]
    return AGE_BANDS[1] if days <= 7 else AGE_BANDS[2]


def rank_band(rank: int) -> str:
    if rank <= 20:
        return FOUND_BANDS[0]
    if rank <= 40:
        return FOUND_BANDS[1]
    return FOUND_BANDS[2] if rank <= 60 else FOUND_BANDS[3]


@dataclass
class Listing:
    """一轮里一个词的结果单：自然结果按名次排好的笔记 ID。"""

    organic: list[str] = field(default_factory=list)
    paid: set[str] = field(default_factory=set)
    pages: int = 0             # 拿到了几页
    complete: bool = False     # 该翻的都翻了（翻满了，或者翻到底了）
    ended: bool = False        # 翻到了一条条目都没有的页
    at: float = 0.0            # 第一页是哪一刻拿的
    sizes: list[int] = field(default_factory=list)

    def rank(self, note_id: str) -> Optional[int]:
        try:
            return self.organic.index(note_id) + 1
        except ValueError:
            return None

    def result(self, note_id: str) -> tuple[str, Optional[int]]:
        """这一篇在这个词下面的结论 →（标签, 名次或 None）。

        名次只在自然结果里数；我们自己的帖子出现在付费位上不算搜到，单独记。
        「没搜到」只在该翻的页都翻了的时候说——缺了页就是「没查全」。
        """
        rank = self.rank(note_id)
        if rank is not None:
            return rank_band(rank), rank
        if not self.complete:
            # 页没翻全：不管它在已翻的页里是不是出现在付费位上，没翻的页里可能自然排上了，
            # 「只在付费位」和「没搜到」一样，要翻全了才能说。
            return INCOMPLETE, None
        return (PAID if note_id in self.paid else NOT_FOUND), None


def listing(round_: Round, digest: str, state: State) -> Listing:
    out = Listing()
    seen: set[str] = set()
    for page in range(1, round_.pages + 1):
        record = state.page(round_.name, digest, page)
        if record is None:
            return out
        out.pages += 1
        if page == 1:
            out.at = float(record.get("at") or 0)
        ids = [str(i) for i in record.get("ids") or []]
        ads = set(record.get("ads") or [])
        out.sizes.append(len(ids))
        if not ids:
            out.ended = out.complete = True
            return out
        for index, note_id in enumerate(ids):
            if index in ads:
                out.paid.add(note_id)
            elif note_id not in seen:
                seen.add(note_id)
                out.organic.append(note_id)
    out.complete = True
    return out


def _blank() -> dict[str, int]:
    return {"篇数": 0, **{label: 0 for label in RESULT_LABELS}}


def _comment_group(comments: Optional[int], floor: int) -> str:
    if comments is None:
        return "评论数没量到"
    return f"评论数到 {floor} 条" if comments >= floor else f"评论数不到 {floor} 条"


def build_report(state: State, posts: list[Post], *, tz: timezone,
                 comment_floor: int = 20, now: Optional[float] = None,
                 intake_info: Optional[Intake] = None) -> dict[str, Any]:
    """进度文件 + 这次读到的帖子 → 报告（一个能直接 json.dump 的字典）。**纯函数。**

    里面只有计数：没有词、没有笔记 ID、没有链接。词用 K01 这样的编号。

    怎么对：每一篇帖子带着它那张表的全部词（`Post.keywords`）。一轮里，它和这一轮
    定下来的、属于它那张表的**每一个**词都对一遍；

    * 至少在一个词下面出现（自然结果）= 搜到，档位取名次最好的那一个；
    * 一个词下面都没有，而且这些词该翻的页都翻了 = 没搜到；
    * 一个词下面都没有，但有词没查全（包括一页都没拿到、这一轮没查过）= 没查全，说不准；
    * 只在付费位出现：它那张表的词都查全了才记「付费位」，否则也是没查全。
    """
    # 同一篇帖子在两张表里都有：算一篇，词取两张表的合起来。
    merged: dict[str, Post] = {}
    words_of: dict[str, list[str]] = {}
    for post in posts:
        merged.setdefault(post.note_id, post)
        mine = words_of.setdefault(post.note_id, [])
        for word in post.keywords:
            digest = keyword_hash(word)
            if digest not in mine:
                mine.append(digest)
    by_word: dict[str, list[Post]] = {}
    for note_id, digests in words_of.items():
        for digest in digests:
            by_word.setdefault(digest, []).append(merged[note_id])
    numbered = sorted(((n, d) for d, n in state.data["keywords"].items()
                       if isinstance(n, int)))
    rounds: dict[str, Any] = {}
    results: dict[str, dict[tuple[str, str], tuple[str, Optional[int]]]] = {}

    for round_ in ROUNDS.values():
        selected, recorded = state.selection(round_.name)
        if not selected and round_.name not in state.data["rounds"]:
            continue                      # 这一轮没跑过
        round_at = state.round_at(round_.name) or (now if now is not None else time.time())
        # 这一轮算哪几个词：跑的时候记下的那一份（`select` 定的），不多不少——
        # 第 1 页失败、一页都没拿到的词也在里面；没被选中的词不在。
        # 早先的进度文件没记：留下过痕迹的词（有页或有失败记录）都算，再从前往后补到
        # 这一轮该查的个数（上限先到、还没轮到的词没有痕迹，别让它下面的帖子消失）。
        # 这是推的，报告里会说。
        chosen = [(state.number(d) or 0, d) for d in selected]
        if not recorded:
            for pair in numbered:
                if len(chosen) >= round_.keywords:
                    break
                if pair not in chosen:
                    chosen.append(pair)
        chosen.sort()
        digests = [d for _n, d in chosen]
        listings = {d: listing(round_, d, state) for d in digests}
        by_age = {band: _blank() for band in AGE_BANDS}
        by_comments: dict[str, dict[str, int]] = {}
        per_word = []
        results[round_.name] = {}
        sizes: list[int] = []
        types: dict[str, int] = {}
        keys: set[str] = set()
        ads = other = 0
        # 这一轮里每一篇帖子对出来的结果：{笔记 ID: [(词的哈希, 标签, 名次)]}。
        pairs: dict[str, list[tuple[str, str, Optional[int]]]] = {}
        seen_ats: dict[str, float] = {}
        touched: set[str] = set()         # 这一轮里至少有一个词轮到它的帖子（含后来才发的）
        for number, digest in chosen:
            found = listings[digest]
            for page in range(1, found.pages + 1):
                record = state.page(round_.name, digest, page) or {}
                if record.get("ids"):
                    sizes.append(len(record["ids"]))
                ads += len(record.get("ads") or [])
                other += int(record.get("other") or 0)
                keys.update(record.get("keys") or [])
                for kind, count in (record.get("types") or {}).items():
                    types[kind] = types.get(kind, 0) + int(count)
            row = {"词": f"K{number:02d}", "拿到几页": found.pages,
                   "看了前几位": len(found.organic), "查全了": found.complete,
                   "翻到底了": found.ended, "帖子": _blank(), "按帖龄": {}}
            # 这个词在这一轮是哪一刻查的：有第 1 页就是拿到它的那一刻；一页都没拿到
            # （第 1 页失败了，或者上限先到、还没轮到它）就用这一轮跑的那一刻。
            seen_at = found.at if found.pages and found.at else round_at
            seen_ats[digest] = seen_at
            for post in by_word.get(digest, []):
                touched.add(post.note_id)
                if post.publish_ms is not None and post.publish_ms > seen_at * 1000:
                    # 那一刻这一篇还没发：它不在这个词当时对的那批帖子里。算进来的话
                    # 帖龄是负的，会被说成「3 天内、没搜到」。
                    continue
                # 一页都没拿到的词，这张表的帖子在它下面是「没查全」，不是不存在。
                label, rank = (found.result(post.note_id) if found.pages
                               else (INCOMPLETE, None))
                results[round_.name][(digest, post.note_id)] = (label, rank)
                pairs.setdefault(post.note_id, []).append((digest, label, rank))
                band = age_band(post.publish_ms, seen_at, tz)
                for bucket in (row["帖子"], row["按帖龄"].setdefault(band, _blank())):
                    bucket["篇数"] += 1
                    bucket[label] += 1
            per_word.append(row)
        # 表里现在有、这一轮却没查的词（多半是查完之后才加进表的）：它下面有没有这篇帖子
        # 不知道，所以别的词下面没搜到的帖子是「没查全」，不能说「没搜到」。
        # 例外是本来就只查一部分词的轮（depth 只查 2 个）定满了的时候：别的词不归这一轮管。
        by_design = round_.keywords < MAX_KEYWORDS and len(digests) >= round_.keywords
        in_round = set(digests)
        unsearched: set[str] = set()
        # 一篇帖子一个结论：它那张表在这一轮查的每个词都对过了，合起来看。
        # 先后是：自然结果里搜到 > 没查全 > 只在付费位 > 没搜到。只在付费位、可还有词没查全的，
        # 说不准它在没查的地方是不是自然排上了，所以是「没查全」。
        for note_id, found_pairs in pairs.items():
            post = merged[note_id]
            labels = {label for _d, label, _r in found_pairs}
            missing = ([] if by_design else
                       [d for d in words_of[note_id] if d not in in_round])
            unsearched.update(missing)
            # 帖龄跟着「这个结论是哪一刻看到的」走。一轮隔了几天才续跑完的时候，同一篇帖子在
            # 不同的词下面是在不同帖龄查的：第 2 天在甲词下没搜到、第 5 天在乙词下搜到了，
            # 不能记成「3 天内搜到」。
            first_seen = min(seen_ats[d] for d, _l, _r in found_pairs)
            bands = {age_band(post.publish_ms, seen_ats[d], tz) for d, _l, _r in found_pairs}
            hits = sorted((seen_ats[d], rank) for d, _l, rank in found_pairs if rank is not None)
            if hits:
                # 搜到了：按最早搜到它的那一次算帖龄和名次。
                seen_at, rank = hits[0]
                label = rank_band(rank)
                band = age_band(post.publish_ms, seen_at, tz)
            else:
                band = age_band(post.publish_ms, first_seen, tz)
                if INCOMPLETE in labels or missing or len(bands) > 1:
                    # 各个词不是在同一个帖龄段查的：最早那一段里还有词没查，说不了「没搜到」。
                    label = INCOMPLETE
                else:
                    label = PAID if PAID in labels else NOT_FOUND
            group = _comment_group(post.comments, comment_floor)
            for bucket in (by_age[band], by_comments.setdefault(group, _blank())):
                bucket["篇数"] += 1
                bucket[label] += 1
        late = len(touched) - len(pairs)
        outside = len(merged) - len(touched)
        need, have = plan(round_, digests, state)
        rounds[round_.name] = {
            "说明": round_.label,
            "词是当时记下的": recorded,
            "页": {"计划": round_.pages * len(digests), "拿到": have, "还差": need,
                   "失败过": sum(1 for f in state.data["failures"]
                              if f.get("round") == round_.name)},
            "每页笔记数": ({"最少": min(sizes), "中位": statistics.median(sizes),
                         "最多": max(sizes)} if sizes else {}),
            "像广告位的条数": ads,
            "认不出笔记的条数": other,
            "查之后才发的帖子（不算）": late,
            "这一轮没查它那张表的词的帖子（不算）": outside,
            "表里有、这一轮没查的词": len(unsearched),
            "条目类型": dict(sorted(types.items())),
            "条目键名": sorted(keys),
            "按帖龄": by_age,
            "按评论数": dict(sorted(by_comments.items())),
            "按词": per_word,
        }

    stability: dict[str, Any] = {}
    base = results.get("first") or {}
    for name in ("repeat1", "repeat2"):
        again = results.get(name)
        if not again or not base:
            continue
        tally = {"对得上的（帖子×词）": 0, "档位一样": 0, "档位变了": 0,
                 "名次差超过20": 0, "一次搜到一次没搜到": 0}
        for key, (label, rank) in again.items():
            if key not in base:
                continue
            first_label, first_rank = base[key]
            if INCOMPLETE in (label, first_label):
                continue
            tally["对得上的（帖子×词）"] += 1
            tally["档位一样" if label == first_label else "档位变了"] += 1
            if rank is not None and first_rank is not None and abs(rank - first_rank) > 20:
                tally["名次差超过20"] += 1
            if (rank is None) != (first_rank is None):
                tally["一次搜到一次没搜到"] += 1
        stability[name] = tally

    failures: dict[str, int] = {}
    # 读不懂的页：只数**现在还没拿到**的（改好代码重新拿到之后，这个警告就该没了）。
    unread = {"页数": 0, "条目类型": {}, "条目键名": set()}
    unread_seen: set[tuple] = set()
    for item in state.data["failures"]:
        kind = item.get("kind")
        tag = f"{item.get('round')}：{'读不懂的页' if kind == UNREADABLE else kind}" + (
            f"（HTTP {item.get('http')}）" if item.get("http") else "")
        failures[tag] = failures.get(tag, 0) + 1
        where = (item.get("round"), item.get("kw"), item.get("page"))
        if kind != UNREADABLE or where in unread_seen \
                or not all(isinstance(v, (str, int)) for v in where) \
                or state.page(str(where[0]), str(where[1]), int(where[2] or 0)) is not None:
            continue
        unread_seen.add(where)
        unread["页数"] += 1
        unread["条目键名"].update(str(k) for k in item.get("keys") or [])
        for name, count in (item.get("types") or {}).items():
            unread["条目类型"][name] = unread["条目类型"].get(name, 0) + int(count)
    unread["条目键名"] = sorted(unread["条目键名"])
    unread["条目类型"] = dict(sorted(unread["条目类型"].items()))

    day3 = (rounds.get("first") or {}).get("按帖龄", {}).get(AGE_BANDS[0]) or _blank()
    report = {
        "试跑": TRIAL_NAME,
        "生成时间": datetime.fromtimestamp(now if now is not None else time.time(),
                                        tz=tz).strftime("%Y-%m-%d %H:%M"),
        "花费": {"已发请求": state.calls, "已记金额": round(state.yuan, 2),
               "次数上限": MAX_CALLS, "金额上限": MAX_YUAN, "计划次数": PLANNED_CALLS},
        "词": len(numbered),
        "帖子": len(merged),
        "回答": {
            "问题": "发布 3 天内的帖子，在这张表要查的词下面，综合排序前 3 页里看不看得见",
            "3天内的帖子": day3["篇数"],
            "搜到的": sum(day3[label] for label in FOUND_BANDS),
            "只在付费位的": day3[PAID],
            "没搜到的": day3[NOT_FOUND],
            "没查全的": day3[INCOMPLETE],
        },
        "各轮": rounds,
        "名次稳不稳": stability,
        "失败的页": failures,
        "读不懂的页": unread,
        "余额对账": list(state.data["balance"]),
    }
    if intake_info is not None:
        report["读表"] = {"表": intake_info.tables,
                        "写了词的表": intake_info.tables_with_keywords,
                        "行": intake_info.rows,
                        "填了词的行": intake_info.rows_with_keyword,
                        "太长没收的词": intake_info.too_long,
                        "像整句话没收的格子": intake_info.sentence_cells,
                        "没进试跑的行": dict(sorted(intake_info.skipped.items()))}
    return report


def _counts(bucket: dict[str, int]) -> str:
    shown = [f"{label} {bucket[label]}" for label in RESULT_LABELS if bucket.get(label)]
    return f"{bucket['篇数']} 篇" + (f"：{' · '.join(shown)}" if shown else "")


def render_report(report: dict[str, Any]) -> str:
    """报告 → 给人看的文字。"""
    spend = report["花费"]
    answer = report["回答"]
    lines = [
        f"# 搜索可见试跑报告（{report['生成时间']}）",
        "",
        f"花费：发了 {spend['已发请求']} 次请求，按多了算 ¥{spend['已记金额']:.2f}"
        f"（上限 {spend['次数上限']} 次 / ¥{spend['金额上限']:.0f}，计划 {spend['计划次数']} 次）。",
        f"词 {report['词']} 个，帖子 {report['帖子']} 篇。",
    ]
    read = report.get("读表")
    if read:
        skipped = "、".join(f"{k} {v} 行" for k, v in read["没进试跑的行"].items()) or "没有"
        lines.append(f"读了 {read['表']} 张表、{read['行']} 行，其中 {read['写了词的表']} 张表写了词"
                     f"（填了「搜索关键词」的 {read['填了词的行']} 行）；没进试跑的行：{skipped}"
                     + (f"；太长（像整句话）没收的词 {read['太长没收的词']} 个"
                        if read["太长没收的词"] else "")
                     + (f"；像一句话、整格没收的 {read['像整句话没收的格子']} 格"
                        if read.get("像整句话没收的格子") else "") + "。")
    lines.append("词是表的：一张表里写过的词都算这张表的，这张表每一篇帖子和每一个词都对一遍。")
    lines += ["", "## 先回答那个问题", ""]
    unread = report.get("读不懂的页") or {}
    if unread.get("页数"):
        lines += [
            f"⚠ 有 {unread['页数']} 页对方返回了条目，却一条笔记 ID 都认不出来——返回的形状和"
            "代码猜的不一样。**下面的数不能当成「没搜到」**：是没读懂，要先改代码再重新查。",
            "那几页的条目类型：" + ("、".join(
                f"{k} {v}" for k, v in unread["条目类型"].items()) or "（没有）")
            + "；条目里的键名：" + ("、".join(unread["条目键名"]) or "（没有）") + "。",
            ""]
    if not answer["3天内的帖子"]:
        lines.append("第一轮里还没有「发布 3 天内」的帖子有结果——要么第一轮还没跑，"
                     "要么这几张表的帖子都超过 3 天了（看下面按帖龄的表）。")
    else:
        lines.append(
            f"发布 3 天内的帖子 {answer['3天内的帖子']} 篇，综合排序前 3 页里"
            f"至少在一个词下面搜到 {answer['搜到的']} 篇，"
            f"哪个词下面都没搜到 {answer['没搜到的']} 篇"
            + (f"，只出现在付费位 {answer['只在付费位的']} 篇" if answer.get("只在付费位的") else "")
            + (f"，没查全（说不准）{answer['没查全的']} 篇" if answer["没查全的"] else "") + "。")
    lines.append("「没搜到」只是说前几页里没有，不等于没被收录；名次是供应商那一刻看到的，"
                 "不是某个读者看到的。")
    for name, block in report["各轮"].items():
        pages = block["页"]
        lines += ["", f"## {block['说明']}（{name}）", "",
                  f"页：计划 {pages['计划']}，拿到 {pages['拿到']}，还差 {pages['还差']}，"
                  f"失败过 {pages['失败过']} 次。"]
        if not block.get("词是当时记下的", True):
            lines.append("这一轮是早先的进度文件，没记当时查的是哪几个词：下面的词是按留下的页和"
                         "失败记录推的，可能多列了没查过的词（列成「还没查」）。再跑一次这一轮就会记下。")
        size = block["每页笔记数"]
        if size:
            lines.append(f"每页笔记数：最少 {size['最少']}，中位 {size['中位']:g}，最多 {size['最多']}。"
                         f"像广告位的 {block['像广告位的条数']} 条，认不出笔记的 "
                         f"{block['认不出笔记的条数']} 条。")
        if block.get("查之后才发的帖子（不算）"):
            lines.append(f"有 {block['查之后才发的帖子（不算）']} 篇帖子是这一轮查完之后才发的，"
                         "不算在这一轮里（那时候它还不存在，搜不到说明不了什么）。")
        if block.get("这一轮没查它那张表的词的帖子（不算）"):
            lines.append(f"有 {block['这一轮没查它那张表的词的帖子（不算）']} 篇帖子，它那张表的词"
                         "这一轮一个都没轮到，不算在这一轮里。")
        if block.get("表里有、这一轮没查的词"):
            lines.append(f"表里现在有 {block['表里有、这一轮没查的词']} 个词这一轮没查过（多半是查完之后"
                         "才加的），下面「按词」里没有它们：别的词下面没搜到的帖子记成「没查全」。"
                         "再跑一次这一轮会补上。")
        lines += ["", "按帖龄（一篇帖子算一次：它那张表这一轮查的词里，至少一个词下面搜到就算搜到；"
                      "帖龄是查的那一刻、按表里的「发布时间」算）："]
        lines += [f"- {band}：{_counts(block['按帖龄'][band])}" for band in AGE_BANDS]
        if block["按评论数"]:
            lines += ["", "按评论数："]
            lines += [f"- {group}：{_counts(bucket)}"
                      for group, bucket in block["按评论数"].items()]
        lines += ["", "按词（写了这个词的表的帖子，在这个词下面出现了几篇、排在哪一档）："]
        for row in block["按词"]:
            total = row.get("帖子") or _blank()
            ages = "；".join(f"{band} {_counts(row['按帖龄'][band])}"
                            for band in AGE_BANDS if band in row["按帖龄"])
            tail = (f"｜帖子 {_counts(total)}｜{ages}" if total["篇数"] else "｜没有帖子")
            if not row["拿到几页"]:
                lines.append(f"- {row['词']}：还没查{tail if total['篇数'] else ''}")
                continue
            state_text = "翻到底了" if row["翻到底了"] else ("查全了" if row["查全了"] else "没查全")
            lines.append(f"- {row['词']}：拿到 {row['拿到几页']} 页，看了前 {row['看了前几位']} 位"
                         f"（{state_text}）{tail}")
        if block["条目类型"] or block["条目键名"]:
            lines += ["", "结果条目的类型：" + ("、".join(
                f"{k} {v}" for k, v in block["条目类型"].items()) or "（没有）"),
                      "结果条目里出现过的键名：" + ("、".join(block["条目键名"]) or "（没有）")]
    if report["名次稳不稳"]:
        lines += ["", "## 名次稳不稳（和第一轮比；一篇帖子在一个词下面算一次）", ""]
        for name, tally in report["名次稳不稳"].items():
            lines.append(f"- {name}：" + "，".join(f"{k} {v}" for k, v in tally.items()))
    if report["失败的页"]:
        lines += ["", "## 失败的页", ""]
        lines += [f"- {tag}：{count} 次" for tag, count in report["失败的页"].items()]
    if report["余额对账"]:
        lines += ["", "## 余额对账（看失败的页收没收钱）", "",
                  "同一时间巡查也在花同一个余额，所以这个差只能当参考。"]
        for item in report["余额对账"]:
            lines.append(f"- {item.get('round')}：发了 {item.get('requests')} 次请求，"
                         f"余额少了 ${item.get('drop_usd')}")
    return "\n".join(lines) + "\n"


def write_report(directory: Path, report: dict[str, Any]) -> tuple[Path, Path]:
    """报告写两份：给人看的 .md 和给机器读的 .json。都只有计数。"""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    text_path = directory / "report.md"
    json_path = directory / "report.json"
    text_path.write_text(render_report(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return text_path, json_path


# ---------- 进度文件放哪 ----------


def default_state_dir() -> Path:
    """默认放在自己的家目录下。不放系统临时目录：试跑要隔天再跑一轮，
    临时目录重启就清，清了账本也就没了。"""
    return Path.home() / ".linkcheck" / "search-trial"


def check_state_dir(directory: Path, repo_root: Path) -> str:
    """进度文件不许放进仓库里（里面有别人笔记的 ID，不能被顺手提交）。
    空串 = 可以；否则是原因。"""
    try:
        resolved = Path(directory).expanduser().resolve()
        root = Path(repo_root).resolve()
    except OSError as exc:
        return f"--state-dir 看不懂：{exc}"
    if resolved == root or root in resolved.parents:
        return (f"--state-dir 不能放在仓库目录里（{resolved}）——进度文件里有笔记 ID，"
                "放这儿迟早被一起提交。换一个仓库外面的目录")
    return ""


def dry_run_lines(round_: Round, info: Intake, state: State, price: float,
                  digests: list[str]) -> list[str]:
    """--dry-run 打印的那几行：会做什么、要花多少。不发任何付费请求。"""
    selection, digests, left = select(round_, digests, state)
    need, have = plan(round_, digests, state)
    upper = ""
    if info.short_links:
        upper = (f"（另有 {info.short_links} 行是短链，真跑时先免费展开，展开了也算帖子；"
                 "一张表的帖子全是展不开的短链时，它的词不查，所以词数、页数是上界）")
    room_calls = MAX_CALLS - state.calls
    room_yuan = MAX_YUAN - state.yuan
    lines = [
        f"【只演练，不发付费请求】{round_.label}",
        f"  表：{info.tables} 张，写了词的 {info.tables_with_keywords} 张；"
        f"词：{len(info.keywords)} 个（最多 {MAX_KEYWORDS} 个）；"
        f"要对的帖子：{len(info.posts)} 篇{upper}",
        "  每个词拉前几页，这张表的每一篇帖子和这张表的每一个词都对一遍",
        f"  这一轮查 {len(digests)} 个词，每个词最多 {round_.pages} 页："
        f"已经拿到 {have} 页，还要发最多 {need} 次，约 ¥{need * price:.2f}"
        f"（¥{price:.3f} 一次）",
        f"  账本：已经发了 {state.calls} 次、记了 ¥{state.yuan:.2f}；"
        f"上限 {MAX_CALLS} 次 / ¥{MAX_YUAN:.0f}，还剩 {room_calls} 次 / ¥{room_yuan:.2f}",
    ]
    affordable = min(room_calls, int((room_yuan + 1e-9) // price) if price > 0 else room_calls)
    if left:
        lines.insert(4, f"  这一轮最多 {round_.keywords} 个词，已经定了 {len(selection)} 个"
                        f"（以前跑这一轮时定的不换）；表里另外 {len(left)} 个词这一轮不查")
    if need > affordable:
        lines.append(f"  ⚠ 剩下的额度只够 {max(0, affordable)} 次，这一轮跑不完，跑到上限会自己停")
    if info.skipped:
        lines.append("  没进试跑的行：" + "、".join(
            f"{reason} {count} 行" for reason, count in sorted(info.skipped.items())))
    if info.too_long:
        lines.append(f"  有 {info.too_long} 个词超过 {MAX_KEYWORD_CHARS} 个字（像整句话），不查")
    if info.sentence_cells:
        lines.append(f"  有 {info.sentence_cells} 格「搜索关键词」像一句话（有句号、问号、冒号，或有一段超过 "
                     f"{MAX_KEYWORD_CHARS} 个字），整格不查。词之间用逗号、顿号、分号或换行隔开")
    return lines
