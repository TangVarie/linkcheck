"""搜索可见试跑 · 面板和命令行共用的那一半：账本存在哪、谁在跑、面板上那几个按钮做什么。

试跑本身（读词、发请求、记账、出报告）全在 `visibility.py`，这里一条规则都不重写。
这个模块只管三件 `visibility.py` 管不了的事（2026-11-16 加，负责人要「全在面板上点」）：

## 一、账本存在登记表那个 base 里，重新部署不会丢

生产跑在 Railway 上：面板是一个常驻容器，**容器的盘重新部署就清**。账本要是只在盘上，
部署一次「已经花了多少」就归零，上限也跟着归零；拿到过的页也得再付一遍钱。
所以账本和拿到过的页存进登记表 base 里那张「面板设置」表（`panel_settings.py`，
面板的别的设置本来就在那儿）：同一份凭据、同一个 base、不要钱。

一个单元格最多 10 万字（`panel_settings.CELL_MAX_CHARS`），账本跑到后面比这大，
所以**切成几段存**（每段 `CHUNK_CHARS` 字），再写一行「清单」说这一份有几段、多长、
校验和是多少：

    trial.search.state.a.00 / .01 / …   甲槽的各段
    trial.search.state.b.00 / .01 / …   乙槽的各段
    trial.search.manifest               清单：现在算数的是哪个槽、几段、多长、校验和
    trial.search.lease.<座位>           租约：一边一行（谁在跑、到几点过期）
    trial.search.result                 最近一份报告的文字（只有计数）

* **清单最后写。** 每次存都写进「另一个槽」，各段写完了才改清单。写到一半断了：
  清单还指着上一份完整的，读回来就是上一份——而上一份是对的，因为请求是在存好之后才发的。
* **读的时候按清单对一遍**：段数、总长、校验和有一样对不上，就是存了一半或者被人改过，
  **拒绝使用**（`visibility.StateError`），绝不当成「一分钱没花过」。有段却没有清单
  也一样拒绝——清单被删掉的话，花过的钱就没人记得了。
* **没变的段不重写。** 页排在最前面、只往后加，所以前面的段多半和上次一样；
  平常存一次是写一段 + 写清单。
* **存之前对一遍清单**（版本号 + 每次存都新起的一个随机号）：和自己读到（或上次存下）
  的不一样，说明别处存过，这一边不再写（`Superseded`）——不然会把别人记的账盖掉。
  **存完再读一遍清单**：不是自己刚写的那一份（两边撞在同一刻），这一次也不发请求。
  飞书没有「没人改过才写」，所以这两道检查之间仍然有一条极窄的缝（对方的清单正好落在
  「存完再读」之后）；要撞上它，得先有两边同时拿着租约，而那由下面的租约挡着。
* 知道租约已经不在自己手里的那一方不再写账本，也不出报告（它手里那份账本是旧的）。
* 账本里没有词的原文、没有标题和链接——和本地那份一样，只有哈希、笔记 ID、名次、计数。

发一个付费请求之前先把这一笔记上、**存进这张表**，存好了才发（`visibility.run_round`
原来就是「先记账、存盘、再发」，现在「盘」换成了这张表）。存不上就不发。

## 二、租约：同一时刻只有一个试跑在花钱

文件锁（`runlock.py`）管不到两个容器。所以在同一张表里放租约：**每一边（一个进程）
写自己的那一行**，谁拿着、到几点过期；别人的行只读不写。
面板上连点两下、开两个标签页、面板和命令行（`--state registry`）同时跑，
都是看这几行——后来的那个直接被拒，一分钱不花。

* **过期时间 `LEASE_SECONDS` = 10 分钟。** 跑着的那一方每发一个付费请求之前都先看
  租约还在不在自己手里，剩下不到一半就续上，所以正常跑着的不会过期（两次请求之间
  最长也就一两分钟：等回答最多 30 秒、失败重试一次、限流等 5 秒、存两次账本）。
  10 分钟没人续 = 那个进程没了（重新部署、被杀）。过期的租约别人可以接手；
  定得更短，一次慢一点的飞书读写就会让人误接手；定得更长，面板重新部署之后要干等。
* **接手之后原来那一方会自己停**：它下一次发请求之前发现租约换了人（或者自己那一行
  已经过期——过期了的不许自己续回来），就不发了；它只把**自己那一行**标成放掉，
  碰不到接手那一方的行。
* 飞书没有「没人改过才写」这种写法，所以拿租约是「先看有没有别人 → 在自己那一行写上
  自己 → 等 `LEASE_SETTLE_SECONDS` → 把所有行再读一遍，**只要看到别人有一行还有效就让开**
  （把自己那一行放掉）→ 没有别人，才在自己那一行写上「拿稳了」」。各写各的行，谁也盖不掉
  谁；两边都是「先写、后读」，所以不可能两边都没看到对方——最多一边留下（撞在同一刻时
  可能两边都让开，再点一次就行）。续期也是「写完再读一遍」。
* **接手一份没放掉的租约要先等 `TAKEOVER_GRACE_SECONDS`（40 秒）再读账本**：没放掉多半是
  进程没了，但也可能它还活着、正有一个请求在路上（两台机器的钟差得多、或者它卡了很久）。
  等 40 秒（一个请求最多等 30 秒）让那一页存进账本，接手的这一边就不会把同一页再付一遍。
  这只能把重复付费压到很少，做不到绝对没有：对方要是真的还活着，它在路上的那一次
  接手方看不见。两边的钱都在账上，上限照样管着。

## 三、面板上那一栏（`Trial`）

「搜索可见试跑」那一栏的每个按钮对应这里的一个方法。会花钱的只有 `start`，而且：
要带着页面上显示的那个「最多几次」回来（服务端重算，比确认的多就拒）、在后台线程里跑
（不占着请求）、同一个面板进程里同一时刻只有一个任务。`status` / `dry_run` /
`report_only` 一分钱不花，也不改账本。**没有任何定时的东西调这个模块。**

词的原文只在 `status()` 交给已经登录的页面的那份数据里（K01 是哪个词），
不进账本、不进报告、不进日志。
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import panel_settings, providers, runner, shortlink, visibility

KEY_PREFIX = "trial.search."
KEY_STATE_PREFIX = KEY_PREFIX + "state."
KEY_MANIFEST = KEY_PREFIX + "manifest"
KEY_LEASE = KEY_PREFIX + "lease."        # 后面跟座位号：一个进程一行，只有它自己写
KEY_RESULT = KEY_PREFIX + "result"
SLOTS = ("a", "b")

# 一段多少字。比单元格上限（10 万）留一成余量；账本存成纯 ASCII，怎么数都是这么多。
CHUNK_CHARS = 90_000
# 最多几段。280 页的账本用不到 10 段；超了说明账本不对劲，不硬存。
MAX_CHUNKS = 40
MANIFEST_VERSION = 1

LEASE_SECONDS = 600.0
LEASE_SETTLE_SECONDS = 2.0
# 接手一份没放掉的租约之后、读账本之前等多久：比一个请求最长等回答的时间（30 秒）多一点。
TAKEOVER_GRACE_SECONDS = 40.0

STORE_NAME = f"登记表 base 的「{panel_settings.TABLE_NAME}」表"

# 后面几轮都要第一轮先跑过：它们是拿来和第一轮比的。
PREREQUISITE = {"latest": "first", "repeat1": "first", "repeat2": "first", "depth": "first"}
# 面板按钮上的字。
BUTTONS = {
    "first": "第一轮",
    "latest": "最新排序那一轮",
    "repeat1": "隔一小时再查一遍",
    "repeat2": "隔一天再查一遍",
    "depth": "前两个词翻到第 10 页",
}
# 页面上最多留几行进度。
MAX_JOB_LINES = 200


class Busy(RuntimeError):
    """另一个试跑拿着租约。`str(exc)` 是可以直接给人看的中文。"""


class Superseded(visibility.StateError):
    """账本被别处存过了（或者租约已经不在这一边手里）：这一边手里的是旧账，不再写、不出报告。"""


class Refused(RuntimeError):
    """这一次不跑（一分钱没花）。`str(exc)` 是可以直接给人看的中文。"""


def chunk_key(slot: str, index: int) -> str:
    return f"{KEY_STATE_PREFIX}{slot}.{index:02d}"


def encode_state(data: dict[str, Any]) -> str:
    """账本 → 存进表里的那一串字。纯 ASCII、没有空格：飞书怎么数字数、会不会掐掉
    一段开头结尾的空白，都不影响它。页排在最前面（只往后加），前面的段才稳定。"""
    ordered = {"pages": data.get("pages", {}), "failures": data.get("failures", [])}
    ordered.update((k, v) for k, v in data.items() if k not in ordered)
    text = json.dumps(ordered, ensure_ascii=True, separators=(",", ":"))
    # 紧凑写法下空格只会出现在字符串里面，换成转义，读回来一样。
    return text.replace(" ", "\\u0020")


def split_chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


class StoredState(visibility.State):
    """存在登记表 base 里的那份账本。用法和本地那份一样，只是 `save()` 写的是那张表。"""

    def __init__(self, store: "TrialStore"):
        super().__init__(Path(STORE_NAME))
        self._store = store
        self._slot = ""                       # 现在算数的是哪个槽；空 = 还没存过
        self._generation = 0
        self._save_id = ""                    # 上一次存的那份清单的随机号
        self._texts: dict[str, str] = {}      # 各段此刻在表里的内容（没变的不重写）
        self.lease: Optional["Lease"] = None  # 拿着哪份租约在跑（丢了就不再写）
        self.superseded = False               # 发现过账本被别处存了 / 租约丢了

    def save(self) -> None:
        self._store.save_state(self)


class TrialStore:
    """试跑在「面板设置」表里的那几行：账本、租约、最近一份报告。

    `settings_store` 是 `panel_settings.SettingsStore`（或者长得一样的东西：
    `read_texts` / `read_one` / `write_text`）。飞书读写出的任何错都翻成
    `visibility.StateError`——调用方只有一条规矩：出了这个错，不发付费请求。
    """

    def __init__(self, settings_store: Any, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print):
        self.settings_store = settings_store
        self._clock = clock
        self._sleep = sleep
        self.log = log
        # 这个进程在租约那几行里的座位：只写这一行，别人的行只读。
        self._lease_key = KEY_LEASE + secrets.token_hex(4)

    # ---------- 账本 ----------

    def load_state(self, lease: Optional["Lease"] = None) -> StoredState:
        """读账本。没存过 = 一份空的；存了一半、被改过、读不到 = `StateError`。
        `lease`：拿着它在跑的那份租约——它丢了之后这份账本不再往表里写。"""
        state = StoredState(self)
        state.lease = lease
        try:
            texts, dupes = self.settings_store.read_texts(KEY_PREFIX)
        except Exception as exc:                                # noqa: BLE001
            raise visibility.StateError(
                f"读不到试跑的账本（{STORE_NAME}，{type(exc).__name__}）。"
                "里面记着已经花了多少，读不到就不能发付费请求；过一会儿再试") from exc
        parts = {k: v for k, v in texts.items() if k.startswith(KEY_STATE_PREFIX) and v}
        doubled = sorted(k for k in dupes if k == KEY_MANIFEST or k.startswith(KEY_STATE_PREFIX))
        if doubled:
            raise visibility.StateError(
                f"试跑的账本在{STORE_NAME}里有重复的行（{'、'.join(doubled)}），"
                "说不清哪一行算数，不能用。找技术看一下那张表")
        manifest_text = (texts.get(KEY_MANIFEST) or "").strip()
        if not manifest_text:
            if parts:
                raise visibility.StateError(
                    f"试跑的账本只存了一半：{STORE_NAME}里有账本的分段，却没有清单"
                    f"（{KEY_MANIFEST}）。不能当成没花过钱重新开始。找技术看一下那张表")
            return state
        try:
            manifest = json.loads(manifest_text)
            slot = manifest["slot"]
            count = manifest["chunks"]
            if (manifest.get("v") != MANIFEST_VERSION or slot not in SLOTS
                    or not isinstance(count, int) or not 1 <= count <= MAX_CHUNKS):
                raise ValueError("清单的内容不对")
            pieces = [texts.get(chunk_key(slot, i)) for i in range(count)]
            if any(piece is None for piece in pieces):
                raise ValueError("清单说有的分段，表里没有")
            text = "".join(pieces)
            if len(text) != manifest.get("chars") or _digest(text) != manifest.get("sha"):
                raise ValueError("分段和清单对不上")
            data = json.loads(text)
        except (ValueError, KeyError, TypeError) as exc:
            raise visibility.StateError(
                f"试跑的账本读不懂（{STORE_NAME}：{exc}）——存了一半，或者被人改过。"
                "里面记着已经花了多少，不能当它不存在。找技术看一下那张表") from exc
        state.adopt(data, STORE_NAME)
        state._slot = slot
        state._generation = int(manifest.get("gen") or 0)
        state._save_id = str(manifest.get("id") or "")
        state._texts = dict(parts)
        return state

    def save_state(self, state: StoredState) -> None:
        """存账本：各段写进另一个槽（没变的不写），**最后**写清单。"""
        text = encode_state(state.data)
        chunks = split_chunks(text)
        if len(chunks) > MAX_CHUNKS:
            raise visibility.StateError(
                f"试跑的账本有 {len(text)} 字，超过了能存的大小（{MAX_CHUNKS} 段），没有存")
        if state.lease is not None and state.lease.lost:
            state.superseded = True
            raise Superseded(f"{state.lease.lost}；账本也不再写")
        slot = SLOTS[1] if state._slot == SLOTS[0] else SLOTS[0]
        manifest = {"v": MANIFEST_VERSION, "trial": visibility.TRIAL_NAME, "slot": slot,
                    "chunks": len(chunks), "chars": len(text), "sha": _digest(text),
                    "gen": state._generation + 1, "id": secrets.token_hex(8),
                    "at": int(self._clock())}
        try:
            current = _manifest_mark(self.settings_store.read_one(KEY_MANIFEST))
        except Exception as exc:                                # noqa: BLE001
            raise visibility.StateError(
                f"{STORE_NAME}读不到（{type(exc).__name__}），没有存") from exc
        if current != (state._generation, state._save_id):
            # 读它之后别处存过（租约被接手了，或者两台机器的钟差得多）。这时候再写，
            # 会把别人记的账盖掉，或者把两份账本的段拼在一起——宁可这一边停。
            # 只比版本号不够：两边从同一份读起，各自存一次，版本号是同一个数。
            state.superseded = True
            raise Superseded(
                "账本在这一边读它之后被别处改过了（另一个试跑接手了？），这一边不再写")
        written: list[str] = []
        failure: Optional[Exception] = None
        try:
            for index, chunk in enumerate(chunks):
                key = chunk_key(slot, index)
                if state._texts.get(key) == chunk:
                    continue
                # 手里不知道有这一行（头一回写这一段）：先把表重读一遍再决定是改还是建，
                # 别照着旧清单给同一个键建出第二行——有重复的行，整本账就读不了了。
                self.settings_store.write_text(key, chunk, fresh=key not in state._texts)
                state._texts[key] = chunk
                written.append(key)
            self.settings_store.write_text(
                KEY_MANIFEST, json.dumps(manifest, separators=(",", ":")), fresh=False)
        except Exception as exc:                                # noqa: BLE001
            failure = exc
        # 写完（或者报了错——报了错不等于没写进去，回答可能丢在路上）再读一遍清单：
        # 在表里的是不是自己刚写的那一份。
        landed = self._manifest_landed(manifest)
        if failure is not None and landed is not True:
            if landed is False and not state._slot:
                # 头一次存、清单确实没写成：表里只有段、没有清单，下次读会被当成
                # 「存了一半」拒掉，要人去表里删。这一刻确知还没有任何请求是凭这份账本
                # 发出去的（先存后发），所以把刚写的段擦掉，退回「没存过」。
                # 擦不掉、或者说不准清单写没写成，就留着让它被拒。
                for key in written:
                    try:
                        self.settings_store.write_text(key, "", fresh=False)
                        state._texts.pop(key, None)
                    except Exception:                       # noqa: BLE001
                        break
            raise visibility.StateError(
                f"{STORE_NAME}写不进去（{type(failure).__name__}）") from failure
        if landed is False:
            # 写进去了，再读却不是这一份：另一边同一刻也在存，把它盖掉了。
            # 这一笔在表里的账本上不作数，所以这一次不发。
            state.superseded = True
            raise Superseded(
                "刚存的账本马上被别处盖掉了（另一个试跑同时在存），这一边不再写、不发请求")
        state._slot = slot
        state._generation += 1
        state._save_id = manifest["id"]
        if landed is None:
            raise visibility.StateError(
                f"账本存完读不回来（{STORE_NAME}），说不准有没有被别处盖掉，这一次不发")

    def _manifest_landed(self, manifest: dict[str, Any]) -> Optional[bool]:
        """刚才要写的那份清单现在在不在表里。True 在 / False 不在 / None 读不到、说不准。"""
        try:
            text = self.settings_store.read_one(KEY_MANIFEST)
        except Exception:                                       # noqa: BLE001
            return None
        try:
            found = json.loads(text) if text and text.strip() else {}
        except ValueError:
            return None
        return (isinstance(found, dict) and found.get("gen") == manifest["gen"]
                and found.get("id") == manifest["id"])

    # ---------- 租约 ----------

    def _claims(self) -> dict[str, dict[str, Any]]:
        """租约那几行此刻的内容：行的键 → 读得懂的那一份。读不到会抛错。"""
        found: dict[str, dict[str, Any]] = {}
        for key, text in self.settings_store.read_prefix(KEY_LEASE).items():
            claim = _parse_lease(text)
            if claim is not None:
                found[key] = claim
        return found

    def lease_holder(self) -> Optional[dict[str, Any]]:
        """此刻拿着租约的是谁；没人拿（没有、放掉了、过期了）就是 None。读不到会抛错。"""
        now = self._clock()
        live = [c for c in self._claims().values() if _live(c, now)]
        live.sort(key=lambda c: not c.get("confirmed"))      # 拿稳了的排前面
        return live[0] if live else None

    def acquire(self, who: str, round_name: str = "") -> "Lease":
        """拿租约。别人拿着且没过期 → `Busy`；读不到、写不进 → `StateError`。

        先写自己那一行、后读所有行；读到别人有一行还有效就让开。两边都这么做，
        所以不会两边都拿到（各写各的行，不存在「我写的被盖掉了所以没看见」）。"""
        owner = secrets.token_hex(8)
        lease = Lease(self, owner, who, round_name)
        wrote = False
        try:
            now = self._clock()
            claims = self._claims()
            live = [c for c in claims.values() if _live(c, now)]
            if live:
                raise Busy(describe_holder(live[0], now))
            # 有一行没放掉、只是过期了：上一个试跑没有正常收尾（见 TAKEOVER_GRACE_SECONDS）。
            lease.took_over = any(not c.get("released") for c in claims.values())
            lease._write(now)
            wrote = True
            self._sleep(LEASE_SETTLE_SECONDS)
            now = self._clock()
            claims = self._claims()
            mine = claims.get(self._lease_key)
            rivals = [c for c in claims.values()
                      if c.get("owner") != owner and _live(c, now)]
            if mine is None or mine.get("owner") != owner or rivals:
                lease.release()
                raise Busy("另一个试跑刚好同时起跑，这一边让开了；过几秒再点一次"
                           + (f"（{describe_holder(rivals[0], now)}）" if rivals else ""))
            lease._write(now, confirmed=True)
        except Busy:
            raise
        except Exception as exc:                                # noqa: BLE001
            if wrote:
                lease.release()
            raise visibility.StateError(
                f"拿不到试跑的租约（{STORE_NAME}，{type(exc).__name__}），不发付费请求") from exc
        return lease

    def wait_out_previous(self, lease: "Lease") -> bool:
        """接手的是一份没放掉的租约：等 `TAKEOVER_GRACE_SECONDS` 再读账本（原因见模块开头）。
        返回是不是等了。"""
        if not lease.took_over:
            return False
        self._sleep(TAKEOVER_GRACE_SECONDS)
        lease.took_over = False
        return True

    # ---------- 最近一份报告 ----------

    def load_result(self) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(self.settings_store.read_one(KEY_RESULT) or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def save_result(self, result: dict[str, Any]) -> None:
        """最近一份报告的文字（只有计数）。存不上只是面板重启后看不到上一份，不影响账本。"""
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > panel_settings.CELL_MAX_CHARS:
            text = json.dumps({**result, "text": "", "note": "报告太长，没存进表里；点「只重出报告」再出一份"},
                              ensure_ascii=False)
        self.settings_store.write_text(KEY_RESULT, text)


def _manifest_mark(text: Optional[str]) -> tuple[int, str]:
    """清单里的（版本号, 那一次存的随机号）；没有清单是 (0, "")。
    读不懂的清单当成「和谁都对不上」（-1）。"""
    if not text or not text.strip():
        return 0, ""
    try:
        found = json.loads(text)
        return int(found.get("gen") or 0), str(found.get("id") or "")
    except (ValueError, TypeError, AttributeError):
        return -1, ""


def _live(claim: dict[str, Any], now: float) -> bool:
    """这一行租约此刻还有效：没放掉、没过期。"""
    try:
        return not claim.get("released") and float(claim.get("until") or 0) > now
    except (TypeError, ValueError):
        return False


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_lease(text: Optional[str]) -> Optional[dict[str, Any]]:
    if not text or not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("owner") else None


def describe_holder(holder: dict[str, Any], now: float) -> str:
    left = max(0.0, float(holder.get("until") or 0) - now)
    round_name = str(holder.get("round") or "")
    what = f"「{BUTTONS.get(round_name, round_name)}」" if round_name else "试跑"
    return (f"{holder.get('who') or '另一边'}正在跑{what}；它要是已经断了，"
            f"租约再过 {int(left // 60) + 1} 分钟过期，到时候就能接手")


class Lease:
    """拿在手里的租约（表里自己那一行）。用完 `release()`。"""

    LOST_TAKEN = "租约不在这一边手里了（另一个试跑接手了），这一边停下，不再发请求"
    LOST_EXPIRED = ("租约过期了（这一边超过 %d 分钟没动静，别人随时可能接手），"
                    "这一边停下，不再发请求；要接着跑就再起一次" % (LEASE_SECONDS // 60))

    def __init__(self, store: TrialStore, owner: str, who: str, round_name: str):
        self.store = store
        self.owner = owner
        self.who = who
        self.round = round_name
        self.until = 0.0
        self.took_over = False      # 拿的时候看到一份没放掉、过期了的租约
        self.lost = ""              # 非空 = 确知租约已经不在自己手里（原因）；之后不发、不写
        self._confirmed = False
        self._released = False

    def _write(self, now: float, *, released: bool = False, confirmed: bool = False) -> None:
        self.until = now if released else now + LEASE_SECONDS
        self._confirmed = confirmed and not released
        self.store.settings_store.write_text(self.store._lease_key, json.dumps(
            {"owner": self.owner, "who": self.who, "round": self.round,
             "at": int(now), "until": int(self.until), "released": released,
             "confirmed": self._confirmed},
            ensure_ascii=False))

    def check(self) -> str:
        """每发一个付费请求之前问一句：租约还在自己手里吗。空串 = 在（顺手续期）；
        否则是停下来的原因。读不到也算不在——说不准的时候不发。"""
        if self._released:
            return "租约已经放掉了，不发请求"
        if self.lost:
            return self.lost
        now = self.store._clock()
        try:
            claims = self.store._claims()
            mine = claims.get(self.store._lease_key)
            others = [c for c in claims.values()
                      if c.get("owner") != self.owner and _live(c, now)]
            if mine is None or mine.get("owner") != self.owner or mine.get("released") \
                    or any(c.get("confirmed") for c in others):
                # 别人只是刚写上、还没拿稳的不算：它再读一遍看到这一边还有效，会自己让开。
                self.lost = self.LOST_TAKEN
            elif not _live(mine, now):
                # 过期了的不许自己续回来：别人可能已经当它没人要、正在接手。
                self.lost = self.LOST_EXPIRED
            elif float(mine.get("until") or 0) - now < LEASE_SECONDS / 2:
                self._write(now, confirmed=True)
                # 续也是「写完再读一遍」：续的这一下要是慢了，别人可能已经当它过期、写上了
                # 自己。这时候看到任何一行别人的有效租约都让开（宁可两边都停）。
                now = self.store._clock()
                if any(c.get("owner") != self.owner and _live(c, now)
                       for c in self.store._claims().values()):
                    self.lost = self.LOST_TAKEN
        except Exception as exc:                                # noqa: BLE001
            return f"看不了试跑的租约（{type(exc).__name__}），说不准有没有别人在跑，不发请求"
        return self.lost

    def keepalive(self) -> None:
        """不发付费请求的那几步（读表、展开短链）拖得久的时候续一下。续不上不报错：
        真要发请求时 `check()` 会再看一遍。"""
        if not self._released and not self.lost \
                and self.until - self.store._clock() < LEASE_SECONDS / 2:
            self.check()

    def release(self) -> None:
        """把**自己那一行**标成放掉。别人的行碰不到：这一边早就被接手了的话，
        放掉的也只是自己那一行过了期的旧租约，接手那一方不受影响。"""
        if self._released:
            return
        self._released = True
        try:
            self._write(self.store._clock(), released=True)
        except Exception:                                       # noqa: BLE001
            pass                         # 放不掉就等它过期（LEASE_SECONDS）


# ---------- 读表 ----------

COLUMN_OK = "ok"
COLUMN_MISSING = "missing"
COLUMN_WRONG_TYPE = "wrong_type"
UNREADABLE = "unreadable"


@dataclass
class TableRead:
    """一张开了「搜索检查」的表读出来的东西。"""

    label: str
    table_id: str = ""
    app_token: str = ""
    column: str = COLUMN_OK        # 「搜索关键词」那一列：ok / missing / wrong_type / unreadable
    error: str = ""
    rows: int = 0
    filled: int = 0                # 「搜索关键词」那一格不空的行
    pairs: list = field(default_factory=list)    # [(Row, 整理过的格子)]


def read_tables(tables: Iterable[tuple[str, Any]], settings: Any) -> list[TableRead]:
    """逐表读出（行, 「搜索关键词」那一格）。只读，不写，不花钱。

    词是表的：一张表的词是它所有行里写过的词合起来，所以每一行都读出来——格子空着的行
    也是这张表的帖子。读不了的表不抛错，记在 `column` / `error` 里，调用方决定怎么说。
    """
    f = settings.fields
    out: list[TableRead] = []
    for label, table in tables:
        read = TableRead(label=label, table_id=str(getattr(table, "table_id", "") or ""),
                         app_token=str(getattr(table, "app_token", "") or ""))
        out.append(read)
        try:
            meta = table.fields_meta()
        except Exception as exc:                                # noqa: BLE001
            read.column, read.error = UNREADABLE, f"读字段列表失败：{exc}"
            continue
        if not meta:
            read.column = UNREADABLE
            read.error = "读不到字段列表（应用没被加进这张表？）"
            continue
        info = meta.get(f.search_keywords)
        if info is None:
            read.column = COLUMN_MISSING
            continue
        if info.get("type") not in (visibility.COLUMN_TEXT, visibility.COLUMN_MULTI):
            read.column = COLUMN_WRONG_TYPE
            continue
        wanted = [c for c in f.must_read() if c in meta] + [f.search_keywords]
        try:
            records = table.search(wanted)
        except Exception as exc:                                # noqa: BLE001
            read.column, read.error = UNREADABLE, f"读表失败：{exc}"
            continue
        for record in records:
            cell = (record.get("fields") or {}).get(f.search_keywords)
            if cell not in (None, "", []):
                read.filled += 1
            # 多选还是文本只看列类型，不看格子的形状（文本格子里 @人 的分段长得像选项）。
            read.pairs.append((runner.row_from_record(record, settings),
                               visibility.keyword_cell(cell, info.get("type"))))
        read.rows = len(records)
    return out


def table_words(pairs: list) -> list[str]:
    """这张表里写过的词（去重，按第一次出现排）——不管它有没有能对的帖子。"""
    seen: set[str] = set()
    words: list[str] = []
    for _row, cell in pairs:
        for word in visibility.parse_cell(cell).words:
            digest = visibility.keyword_hash(word)
            if digest not in seen:
                seen.add(digest)
                words.append(word)
    return words


def has_pages(state: visibility.State, round_name: str) -> bool:
    """这一轮有没有拿到过哪怕一页。后面几轮是拿来和第一轮比的：第一轮只是「点过」、
    一页都没拿到（Key 不对、第 1 页读不懂、网络不通），后面几轮就没有东西可比。"""
    prefix = f"{round_name}|"
    for key in state.data["pages"]:
        parts = key.split("|")
        if key.startswith(prefix) and len(parts) == 3 and parts[2].isdigit() \
                and state.page(round_name, parts[1], int(parts[2])) is not None:
            return True
    return False


def search_price() -> float:
    return providers.get_provider(providers.TIKHUB).yuan_per_call("xhs", providers.SEARCH)


def no_keywords_message(info: visibility.Intake, settings: Any) -> str:
    f = settings.fields
    return (f"没有可查的词：开了「搜索检查」的表里，要么「{f.search_keywords}」"
            "一个词都没写，要么写了词的表里没有一篇对得了的帖子"
            f"（小红书、没失效、拿得到笔记 ID、填了「{f.publish_time}」）")


# ---------- 面板上那一栏 ----------


@dataclass
class Job:
    """面板进程里正在跑（或刚跑完）的那一个任务。只在内存里；重新部署就没了，
    账本和拿到的页不受影响（它们在表里）。"""

    kind: str                       # "run" 真跑一轮 / "report" 只重出报告
    round: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    running: bool = True
    lines: list = field(default_factory=list)
    refused: str = ""               # 没跑成（一分钱没花）的原因
    error: str = ""
    run: Optional[dict] = None      # 这一轮的结果计数

    def say(self, line: str) -> None:
        self.lines.append(str(line))
        del self.lines[:-MAX_JOB_LINES]

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "round": self.round,
                "label": BUTTONS.get(self.round, ""), "running": self.running,
                "started_at": int(self.started_at), "finished_at": int(self.finished_at),
                "lines": list(self.lines), "refused": self.refused, "error": self.error,
                "run": self.run}


def _spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="panel-search-trial", daemon=True).start()


@dataclass
class _Survey:
    """读一遍表和账本之后知道的东西。不花钱。"""

    reads: list
    info: visibility.Intake
    state: Optional[visibility.State] = None       # 真的那份账本（读不到就是 None）
    state_error: str = ""
    preview: Optional[visibility.State] = None     # 把这次读到的词编上号之后的一份副本
    refusal: str = ""                              # 词太多之类：跑不了的原因
    ordered: list = field(default_factory=list)    # [(词, 哈希)]，按编号排


class Trial:
    """面板「搜索可见试跑」那一栏背后的全部动作。

    `tables()` 交回开了「搜索检查」、正在巡查的表：[(项目名, 飞书表)]。
    发请求、等待、时钟、短链展开、读余额都是注入的，测试里全换成替身。
    """

    def __init__(self, tables: Callable[[], list], store: TrialStore, settings: Any,
                 api_key: str = "", *, send: Optional[visibility.Send] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.time,
                 resolver: Optional[Callable[[], Callable[[str], Optional[str]]]] = None,
                 balance: Optional[Callable[[], Optional[float]]] = None,
                 spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
                 log: Callable[[str], None] = print):
        self._tables = tables
        self.store = store
        self.settings = settings
        self.api_key = api_key
        self._send = send
        self._sleep = sleep
        self._clock = clock
        self._resolver = resolver or _default_resolver
        self._balance = balance
        self._spawn = spawn
        self.log = log
        self._lock = threading.Lock()
        self._job: Optional[Job] = None

    # —— 读一遍（不花钱）——

    def _survey(self, *, resolve: Optional[Callable[[str], Optional[str]]] = None,
                state: Optional[visibility.State] = None) -> _Survey:
        reads = read_tables(self._tables(), self.settings)
        info = visibility.intake([r.pairs for r in reads if r.column == COLUMN_OK],
                                 resolve=resolve)
        out = _Survey(reads=reads, info=info, state=state)
        if out.state is None:
            try:
                out.state = self.store.load_state()
            except visibility.StateError as exc:
                out.state_error = str(exc)
                return out
        # 编号在副本上编：看一眼、空跑都不该让账本里多出没查过的词。
        preview = visibility.State(Path(STORE_NAME))
        preview.data = copy.deepcopy(out.state.data)
        out.refusal = preview.register(info.keywords)
        out.preview = preview
        out.ordered = [(word, digest) for _n, word, digest in sorted(
            (preview.number(visibility.keyword_hash(w)) or 10 ** 6, w, visibility.keyword_hash(w))
            for w in info.keywords)]
        return out

    def _round_plan(self, round_: visibility.Round, survey: _Survey) -> dict[str, Any]:
        """这一轮现在最多还要发几次、花多少。按「每个词都翻满」算，是上界。"""
        state = survey.preview
        _all, now, left = visibility.select(round_, [d for _w, d in survey.ordered], state)
        need, have = visibility.plan(round_, now, state)
        price = search_price()
        return {"need": need, "have": have, "yuan": round(need * price, 2),
                "words": len(now), "left_out": len(left)}

    def status(self) -> dict[str, Any]:
        """页面上那一栏要显示的全部东西。读表、读账本，**不花钱，不改任何东西**。"""
        price = search_price()
        out: dict[str, Any] = {
            "key_ok": bool(self.api_key),
            "limits": {"keywords": visibility.MAX_KEYWORDS, "calls": visibility.MAX_CALLS,
                       "yuan": visibility.MAX_YUAN, "price": round(price, 3),
                       "keyword_chars": visibility.MAX_KEYWORD_CHARS},
            "column": self.settings.fields.search_keywords,
            "job": self.job(), "tables": [], "rounds": [], "problem": "", "lease": None,
            "result": None, "ledger": None, "keywords": None,
        }
        try:
            survey = self._survey()
        except Exception as exc:                                # noqa: BLE001
            out["problem"] = f"读不到表清单或表：{exc}"
            return out
        f = self.settings.fields
        for read in survey.reads:
            words = table_words(read.pairs)
            own = visibility.intake([read.pairs]) if read.column == COLUMN_OK else visibility.Intake()
            out["tables"].append({
                "label": read.label, "app_token": read.app_token, "table_id": read.table_id,
                "column": read.column, "error": read.error, "rows": read.rows,
                "filled": read.filled, "words": len(words), "posts": len(own.posts),
                "short_links": own.short_links,
                "skipped": {k: v for k, v in sorted(own.skipped.items())
                            if k != visibility.SKIP_NO_WORDS}})
        try:
            holder = self.store.lease_holder()
            if holder is not None:
                out["lease"] = {"who": str(holder.get("who") or ""),
                                "round": str(holder.get("round") or ""),
                                "text": describe_holder(holder, self._clock()),
                                "mine": bool(out["job"] and out["job"]["running"])}
            out["result"] = self.store.load_result()
        except Exception:                                       # noqa: BLE001
            pass                    # 账本读不到的话下面会说；这两样只是显示用
        if survey.state is None:
            out["problem"] = survey.state_error
            return out
        state, info = survey.state, survey.info
        out["ledger"] = {"calls": state.calls, "yuan": round(state.yuan, 2),
                         "calls_left": visibility.MAX_CALLS - state.calls,
                         "yuan_left": round(visibility.MAX_YUAN - state.yuan, 2)}
        known = set(state.data["keywords"])
        seen = {visibility.keyword_hash(w) for w in info.keywords}
        total = len(known | seen)
        out["keywords"] = {
            "now": len(info.keywords), "total": total,
            "counted_before": len(known), "gone": len(known - seen),
            "over": total > visibility.MAX_KEYWORDS,
            "sentence_cells": info.sentence_cells, "too_long": info.too_long,
            # 词的原文：只给已经登录的页面看，哪儿都不存。
            "list": [{"k": (f"K{survey.preview.number(d):02d}"
                            if survey.preview.number(d) is not None else "还没编号"),
                      "word": w} for w, d in survey.ordered],
        }
        ran_first = has_pages(state, "first")
        runnable = bool(info.keywords) and not survey.refusal
        for round_ in visibility.ROUNDS.values():
            plan = self._round_plan(round_, survey)
            needs_first = PREREQUISITE.get(round_.name) == "first" and not ran_first
            at = state.round_at(round_.name) if round_.name in state.data["rounds"] else 0
            out["rounds"].append({
                "name": round_.name, "label": round_.label, "button": BUTTONS[round_.name],
                "ran": round_.name in state.data["rounds"], "ran_at": int(at),
                "visible": not needs_first, "runnable": runnable and not needs_first,
                "max_calls": plan["need"], "max_yuan": plan["yuan"], "have": plan["have"],
                "words": plan["words"], "left_out": plan["left_out"]})
        if not survey.reads:
            out["problem"] = ("还没有一张表开「搜索检查」。到上面「加表 / 改配置」里，点要试的那张表的"
                              "「参数」，勾上「搜索检查（搜索可见试跑）」再保存")
        elif survey.refusal:
            out["problem"] = survey.refusal
        elif not info.keywords:
            missing = [r.label for r in survey.reads if r.column == COLUMN_MISSING]
            out["problem"] = (
                f"「{'」「'.join(missing)}」还没有「{f.search_keywords}」这一列——点它旁边的「补齐缺的列」"
                if missing and len(missing) == len(survey.reads)
                else no_keywords_message(info, self.settings))
        return out

    def job(self) -> Optional[dict[str, Any]]:
        job = self._job
        return job.to_json() if job is not None else None

    def dry_run(self, round_name: str) -> dict[str, Any]:
        """空跑：这一轮会查什么、最多花多少。不发付费请求，不拿租约，不改账本。"""
        round_ = _round(round_name)
        survey = self._survey()
        problem = self._cannot_run(survey)
        if problem:
            return {"ok": False, "lines": [problem + "。一分钱没花"]}
        lines = visibility.dry_run_lines(round_, survey.info, survey.preview, search_price(),
                                         [d for _w, d in survey.ordered])
        if not self.api_key:
            lines.append("  ⚠ 面板还没配 TIKHUB_API_KEY，真跑之前要配上")
        lines.append("  这是空跑：一个付费请求都没发，账本也没动。")
        return {"ok": True, "lines": lines, **self._round_plan(round_, survey)}

    def _cannot_run(self, survey: _Survey) -> str:
        """这一次跑不了的原因（空串 = 能跑）。全在发第一个付费请求之前查完。"""
        if survey.state is None:
            return survey.state_error
        broken = [r for r in survey.reads if r.column == UNREADABLE]
        if broken:
            return "这几张开了「搜索检查」的表读不到：" + "；".join(
                f"「{r.label}」{r.error}" for r in broken)
        if not survey.reads:
            return "还没有一张表开「搜索检查」"
        if not survey.info.keywords:
            return no_keywords_message(survey.info, self.settings)
        return survey.refusal

    # —— 会花钱的那一个 ——

    def start(self, round_name: str, confirm_calls: Any) -> dict[str, Any]:
        """开跑一轮。`confirm_calls` 是页面上显示给人看、人点了确认的那个「最多几次」。
        拿不到租约、条件不满足 → `Refused`（一分钱没花）。真正的活在后台做。"""
        round_ = _round(round_name)
        if isinstance(confirm_calls, bool) or not isinstance(confirm_calls, int) \
                or confirm_calls < 0:
            raise Refused("要先在页面上看过「最多几次、多少钱」再点确认")
        if not self.api_key:
            raise Refused("面板没配 TIKHUB_API_KEY：搜索只走 TikHub（和巡查用的是同一个 Key）")
        with self._lock:
            if self._job is not None and self._job.running:
                raise Refused("这个面板上已经有一个任务在跑，等它跑完（刷新这一栏看进度）")
            try:
                state = self.store.load_state()
            except visibility.StateError as exc:
                raise Refused(str(exc)) from exc
            needed = PREREQUISITE.get(round_.name)
            if needed and not has_pages(state, needed):
                raise Refused(f"要先跑过{BUTTONS[needed]}、而且它至少拿到过一页，"
                              f"才能跑{BUTTONS[round_.name]}（这一轮是拿来和它比的）")
            try:
                lease = self.store.acquire("面板", round_.name)
            except Busy as exc:
                raise Refused(f"另一个试跑正在跑（{exc}）。两个一起跑会各记各的账，这次不跑") from exc
            except visibility.StateError as exc:
                raise Refused(str(exc)) from exc
            job = Job(kind="run", round=round_.name, started_at=self._clock())
            self._job = job
        self._spawn(lambda: self._run(job, round_, confirm_calls, lease))
        return job.to_json()

    def _run(self, job: Job, round_: visibility.Round, confirm_calls: int, lease: Lease) -> None:
        self.log(f"🔎 搜索可见试跑：开始 {round_.name}（面板上点的）")
        try:
            self._run_locked(job, round_, confirm_calls, lease)
        except Exception as exc:                                # noqa: BLE001
            job.error = f"中途出错了（{type(exc).__name__}）。已经记的账和拿到的页都在，再点一次只补没拿到的"
        finally:
            lease.release()
            job.finished_at = self._clock()
            job.running = False
            self.log(f"🔎 搜索可见试跑：结束 {round_.name}"
                     + (f"，没跑：{job.refused}" if job.refused else "")
                     + (f"，{job.error}" if job.error else "")
                     + (f"，发了 {job.run['requests']} 次请求" if job.run else ""))

    def _run_locked(self, job: Job, round_: visibility.Round, confirm_calls: int,
                    lease: Lease) -> None:
        """拿着租约做的那一段：读账本 → 读表 → 数词 → 对一遍确认的数 → 发请求 → 出报告。"""
        if lease.took_over:
            job.say(f"上一个试跑没有正常收尾（多半是被重新部署打断的）。先等 "
                    f"{int(TAKEOVER_GRACE_SECONDS)} 秒：它要是还有一个请求在路上，等那一页存进账本，"
                    "免得同一页付两遍钱…")
            self.store.wait_out_previous(lease)
        try:
            # 拿到租约之后才读：读到的是别人存完的那一份
            state = self.store.load_state(lease)
        except visibility.StateError as exc:
            job.refused = f"{exc}。一分钱没花"
            return
        job.say("在读表、免费展开短链…")
        expand = self._resolver()

        def resolve(url: str) -> Optional[str]:
            lease.keepalive()
            return expand(url)

        survey = self._survey(resolve=resolve, state=state)
        problem = self._cannot_run(survey)
        if problem:
            job.refused = f"{problem}。一分钱没花"
            return
        refusal = state.register(survey.info.keywords)
        if refusal:
            job.refused = f"{refusal}。一分钱没花"
            return
        ordered = [(w, d) for _n, w, d in sorted(
            (state.number(visibility.keyword_hash(w)) or 0, w, visibility.keyword_hash(w))
            for w in survey.info.keywords)]
        _all, now, _left = visibility.select(round_, [d for _w, d in ordered], state)
        need, _have = visibility.plan(round_, now, state)
        price = search_price()
        problem = lease.check()                  # 读表、展开短链可能拖了很久
        if problem:
            job.refused = f"{problem}。一分钱没花"
            return
        if need > confirm_calls:
            job.refused = (f"这一轮现在最多要发 {need} 次（约 ¥{need * price:.2f}），比你刚才确认的 "
                           f"{confirm_calls} 次多——表里的词变了。刷新这一栏，看过新的数再点。一分钱没花")
            return
        try:
            state.save()                          # 词的编号先存
        except visibility.StateError as exc:
            job.refused = f"账本存不上（{exc}）。一分钱没花"
            return
        job.say(f"▶ {round_.label}：还要发最多 {need} 次（约 ¥{need * price:.2f}，失败重试的也算在这 "
                f"{need} 次里）；"
                f"账本上已经 {state.calls} 次 / ¥{state.yuan:.2f}，"
                f"上限 {visibility.MAX_CALLS} 次 / ¥{visibility.MAX_YUAN:.0f}")
        before = self._read_balance() if need else None
        result = visibility.run_round(round_, ordered, state, self.api_key, send=self._send,
                                      sleep=self._sleep, clock=self._clock, log=job.say,
                                      guard=lease.check, budget=need)
        job.run = {"fetched": result.fetched, "reused": result.reused, "failed": result.failed,
                   "requests": result.requests, "yuan": round(result.yuan, 2),
                   "todo": result.todo, "stopped": result.stopped}
        if lease.lost or state.superseded:
            # 租约丢了 / 账本被别处存过：手里这份账本是旧的（没有接手那一方拿到的页）。
            # 不再写账本，也不出报告——出了会把接手那一方的报告盖掉。
            job.say(f"⏹ 这一边停了：{result.stopped or lease.lost}。这一边发了 "
                    f"{result.requests} 次请求（发之前都已经记在账上）。另一个试跑接手了，"
                    "账本和报告以它的为准，这一边不出报告；等它跑完刷新这一栏看")
            return
        if result.requests and before is not None:
            after = self._read_balance()
            if after is not None:
                state.data["balance"].append({
                    "round": round_.name, "requests": result.requests,
                    "drop_usd": round(before - after, 4), "at": int(self._clock())})
        try:
            state.save()
        except visibility.StateError as exc:
            job.say(f"  ⚠ 最后一次存账本没存上（{exc}）；发出去的每一次在发之前都已经记上了")
            if state.superseded:
                return
        job.say(f"■ 这一轮：新拿到 {result.fetched} 页，沿用 {result.reused} 页，"
                f"没拿到 {result.failed} 页；发了 {result.requests} 次请求，"
                f"按多了算 ¥{result.yuan:.2f}")
        if result.stopped:
            job.say(f"⏹ 中途停了：{result.stopped}。还差 {result.todo} 页；拿到的页都留着，"
                    "再点一次同一个按钮只补没拿到的")
        elif result.todo:
            job.say(f"  还差 {result.todo} 页没拿到。再点一次同一个按钮只补这几页")
        self._publish(job, state, survey.info)

    def _read_balance(self) -> Optional[float]:
        if self._balance is None:
            return None
        try:
            return self._balance()
        except Exception:                                       # noqa: BLE001
            return None

    def _publish(self, job: Job, state: visibility.State, info: visibility.Intake) -> None:
        """出报告（只有计数），存一份到表里：刷新页面、重新部署之后都还看得到。"""
        report = visibility.build_report(
            state, info.posts, tz=self.settings.display.tz(),
            comment_floor=self.settings.thresholds.tier_evaluating,
            intake_info=info, now=self._clock())
        try:
            self.store.save_result({"at": int(self._clock()), "kind": job.kind,
                                    "round": job.round, "run": job.run,
                                    "text": visibility.render_report(report)})
        except Exception as exc:                                # noqa: BLE001
            job.say(f"  ⚠ 报告没存进表里（{type(exc).__name__}）；点「只重出报告」再出一份")

    # —— 只重出报告（不花钱）——

    def report_only(self) -> dict[str, Any]:
        """按已经拿到的页重新出一份报告。不发付费请求、不拿租约、不改账本。"""
        with self._lock:
            if self._job is not None and self._job.running:
                raise Refused("这个面板上已经有一个任务在跑，等它跑完（刷新这一栏看进度）")
            job = Job(kind="report", started_at=self._clock())
            self._job = job
        self._spawn(lambda: self._report(job))
        return job.to_json()

    def _report(self, job: Job) -> None:
        try:
            job.say("在读表、免费展开短链…（不花钱）")
            survey = self._survey(resolve=self._resolver())
            if survey.state is None:
                job.refused = survey.state_error
                return
            broken = [r for r in survey.reads if r.column == UNREADABLE]
            if broken:
                # 少读了一张表，出的报告就少算那张表的帖：不出，原来那份报告留着
                job.refused = ("这几张开了「搜索检查」的表读不到：" + "；".join(
                    f"「{r.label}」{r.error}" for r in broken) + "。报告没重出，原来那份还在")
                return
            self._publish(job, survey.state, survey.info)
            job.say("报告出好了（没发付费请求，账本没动）")
        except Exception as exc:                                # noqa: BLE001
            job.error = f"出报告的时候出错了（{type(exc).__name__}）"
        finally:
            job.finished_at = self._clock()
            job.running = False


def _round(name: Any) -> visibility.Round:
    round_ = visibility.ROUNDS.get(str(name or ""))
    if round_ is None:
        raise Refused(f"没有这一轮。可选：{'、'.join(visibility.ROUNDS)}")
    return round_


def _default_resolver() -> Callable[[str], Optional[str]]:
    resolver = shortlink.Resolver()
    return lambda url: resolver.resolve(url).content_id
