"""识别多维表格单元格里的链接属于哪个平台，并尽量抽出稳定 ID。

设计取舍：SocialDataX 的 *_by_url 接口本身就吃短链和完整分享文案，所以这里
不做短链展开、不发任何网络请求。我们只需要回答两个问题：

1. 这一格是小红书还是抖音（决定调哪个平台的端点）
2. 能不能直接拿到 ID（能拿到就走 by_id 入口，少一次链接解析，也更稳）

拿不到 ID 时把原始文本整个透传给 by_url 入口即可。
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Literal, Optional

Platform = Literal["xhs", "douyin"]

# 小红书笔记 ID 固定 24 位小写十六进制。
_XHS_NOTE_ID = r"[0-9a-f]{24}"

# 域名白名单。匹配规则是 **hostname 等于它、或是它的子域**，
# 不是「字符串里出现过这几个字」——后者会把
# https://xiaohongshu.com.evil.example/ 认成小红书，
# 于是我们拿着运营的链接去给一个陌生域名发付费请求。
XHS_DOMAINS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn",
               "xhsurl.com", "xhsurl.cn")
DOUYIN_DOMAINS = ("douyin.com", "iesdouyin.com")


def _bare_domain_re(domains: tuple[str, ...]) -> re.Pattern[str]:
    """没有 scheme 的裸域名匹配（运营有时只贴 `xiaohongshu.com/explore/xxx`）。

    左边界不许接字母数字和连字符（挡掉 `evil-xiaohongshu.com`），
    但允许 `.` 前缀，因为 `www.xiaohongshu.com` 是合法子域；
    右边界不许接字母数字连字符，也不许接 `.字母`
    （挡掉 `xiaohongshu.com.evil.example`）。
    """
    alternatives = "|".join(re.escape(d) for d in domains)
    return re.compile(
        rf"(?<![A-Za-z0-9-])(?:[A-Za-z0-9-]+\.)*(?:{alternatives})(?![A-Za-z0-9-])(?!\.[A-Za-z])",
        re.I,
    )


_XHS_BARE = _bare_domain_re(XHS_DOMAINS)
_DOUYIN_BARE = _bare_domain_re(DOUYIN_DOMAINS)

# 按优先级排列：越靠前的形式越明确。
_XHS_ID_PATTERNS = [
    re.compile(rf"/explore/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/discovery/item/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/search_result/({_XHS_NOTE_ID})", re.I),
    # 主页内笔记链接 /user/profile/<uid>/<note_id>
    re.compile(rf"/user/profile/[0-9a-f]{{24}}/({_XHS_NOTE_ID})", re.I),
]

_DOUYIN_ID_PATTERNS = [
    re.compile(r"douyin\.com/video/(\d{6,})", re.I),
    re.compile(r"douyin\.com/note/(\d{6,})", re.I),
    re.compile(r"[?&]modal_id=(\d{6,})", re.I),
    re.compile(r"/share/video/(\d{6,})", re.I),
]

# 从一段分享文案里把 URL 抠出来。中文分享文案常把链接和文字、标点黏在一起，
# 所以右边界除了常见中文标点，还要排掉整个 CJK 区段和左侧括号——
# 「看这条https://v.douyin.com/xxx很火」这种没有空格的写法，
# 不排 CJK 就会把「很火」吞进 URL 里，短链直接 404。
_URL_RE = re.compile(r"https?://[^\s，。、！？；：）】》（《【\"'<>一-鿿]+", re.I)

# URL 尾巴上要剥掉的标点。半角右括号必须在内：`(https://xhslink.com/abc)`
# 这种写法在飞书表里很常见，`)` 留在里面短链直接 404。
_TRAILING_PUNCT = ".,;:!?)]}>\"'）】》」』"

# 裸 ID：整格就是一个 ID 的情况（运营有时只贴 ID）。
_BARE_XHS_ID = re.compile(rf"^\s*({_XHS_NOTE_ID})\s*$", re.I)
# 抖音 aweme_id 是纯数字，歧义太大（订单号、手机号、其它平台 ID 都长这样），
# 所以只在文本里出现「抖音」二字时才认，且要求是独立的一串数字。
_STANDALONE_DOUYIN_ID = re.compile(r"(?<!\d)(\d{15,25})(?!\d)")


@dataclass(frozen=True)
class ParsedLink:
    """一格链接的解析结果。

    platform 为 None 表示无法判定平台，调用方应把该行标记为「链接无法识别」
    而不是硬猜——猜错会去调错平台的接口，白花积分还写回错数据。
    """

    platform: Optional[Platform]
    content_id: Optional[str]
    url: Optional[str]
    raw: str

    @property
    def usable(self) -> bool:
        return self.platform is not None and (self.content_id or self.url) is not None

    def describe_failure(self) -> str:
        if self.platform is None:
            return "无法判定平台：既没匹配到小红书域名/ID，也没匹配到抖音域名/ID"
        return "已识别平台但没有可用的 ID 或链接"


def _clean_url(candidate: str) -> str:
    """剥掉粘在 URL 尾巴上的标点。

    只剥**不配对**的收尾标点：`…/abc123)` 里的 `)` 是外面那对括号的右半边，
    但 `…/path_(x)` 里的 `)` 属于 URL 自己。判据是左右括号数量是否相等。
    """
    url = candidate
    while url and url[-1] in _TRAILING_PUNCT:
        tail = url[-1]
        pair = {")": "(", "]": "[", "}": "{", "）": "（", "】": "【",
                "》": "《", "」": "「", "』": "『"}.get(tail)
        if pair and url.count(pair) >= url.count(tail):
            break   # 括号是配对的，属于 URL 本身
        url = url[:-1]
    return url


def _urls(text: str) -> list[str]:
    return [_clean_url(u) for u in _URL_RE.findall(text)]


# 裸域名后面粘着的那一段路径。只剥域名的话，`xhslink.com/a/AbC` 会在文本里
# 留下一截 `/a/AbC`，比留着整条链接还难认。右边界和 `_URL_RE` 同一套。
_BARE_URL_TAIL = r"(?:/[^\s，。、！？；：）】》（《【\"'<>一-鿿]*)*"
_BARE_URL_RES = [
    re.compile(_XHS_BARE.pattern + _BARE_URL_TAIL, re.I),
    re.compile(_DOUYIN_BARE.pattern + _BARE_URL_TAIL, re.I),
]

# 抠完链接之后，两头常剩下孤零零的标点和分隔符。
_EDGE_JUNK = _TRAILING_PUNCT + "-—_|/\\，。、！？；：（【《「『 \t"


def text_without_urls(cell: str) -> str:
    """把一格里的链接全抠掉，返回剩下的文字。

    用在「这一行是哪一条」上：小红书的分享文案开头往往就是笔记标题
    （`77 露营装备清单 - 小红书 😆 abcDEF😆 https://…`），
    抠掉链接之后剩的那段比光秃秃的网址前缀好认得多——后者每一行都长一样，
    等于没有。

    **复用 `_URL_RE`**：那个正则的中文右边界是踩过坑调出来的
    （「看这条https://v.douyin.com/xxx很火」不排 CJK 会把「很火」吞进 URL），
    不要在别处另写一个。
    """
    text = _URL_RE.sub(" ", cell or "")
    for pattern in _BARE_URL_RES:
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().strip(_EDGE_JUNK)


def _host_matches(url: str, domains: tuple[str, ...]) -> bool:
    """URL 的 hostname 是不是白名单域名本身或它的子域。

    用 urlsplit 取 hostname 而不是在整串里搜字符串：后者会被
    `https://xiaohongshu.com.evil.example/x`（子串命中）、
    `https://evil.example/?u=xiaohongshu.com`（查询串命中）
    骗过去，然后我们拿着运营的链接去给陌生域名发付费请求。
    urlsplit 也顺带处理掉 userinfo（`https://xiaohongshu.com@evil.example/`
    的真实 host 是 evil.example）。
    """
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _first_url(text: str, domains: tuple[str, ...]) -> Optional[str]:
    for candidate in _urls(text):
        if _host_matches(candidate, domains):
            return candidate
    return None


def _match_id(text: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return None


def parse(cell: str) -> ParsedLink:
    """把一格原始文本解析成平台 + ID/链接。

    单格里出现多个链接时取第一个能识别的；这是刻意的——一行代表一篇笔记，
    一格塞多个链接本身就是数据录入问题，应该在表里暴露出来而不是在这里猜。
    """
    raw = (cell or "").strip()
    if not raw:
        return ParsedLink(None, None, None, raw)

    # 全角字符会让域名匹配失败，先归一化一遍常见的几个。
    text = raw.replace("：", ":").replace("／", "/").replace("？", "?")

    bare_xhs = _BARE_XHS_ID.match(text)
    if bare_xhs:
        return ParsedLink("xhs", bare_xhs.group(1).lower(), None, raw)

    # 平台判定的依据分两层，顺序不能反：
    # 1. 文本里有 http(s) 链接 → 只按**链接的 hostname** 判，其余文字一律不算数。
    #    这样 `https://evil.example/?u=xiaohongshu.com` 不会被认成小红书。
    # 2. 文本里一个 http(s) 链接都没有 → 才退回裸域名匹配（带边界），
    #    照顾「只粘了 xiaohongshu.com/explore/xxx」这种录入方式。
    xhs_url = _first_url(text, XHS_DOMAINS)
    douyin_url = _first_url(text, DOUYIN_DOMAINS)
    if _urls(text):
        has_xhs_domain, has_douyin_domain = bool(xhs_url), bool(douyin_url)
    else:
        has_xhs_domain = bool(_XHS_BARE.search(text))
        has_douyin_domain = bool(_DOUYIN_BARE.search(text))

    # 同时命中两个平台的域名 —— 拒绝猜测。
    if has_xhs_domain and has_douyin_domain:
        return ParsedLink(None, None, None, raw)

    if has_xhs_domain:
        # ID 只在**已确认属于这个平台**的那段文本里找：在整格文本里搜
        # `/explore/<24位十六进制>` 会把别的域名下的路径也当成笔记 ID。
        note_id = _match_id(xhs_url or text, _XHS_ID_PATTERNS)
        return ParsedLink(
            "xhs",
            note_id.lower() if note_id else None,
            None if note_id else (xhs_url or raw),
            raw,
        )

    if has_douyin_domain:
        aweme_id = _match_id(douyin_url or text, _DOUYIN_ID_PATTERNS)
        return ParsedLink(
            "douyin",
            aweme_id,
            None if aweme_id else (douyin_url or raw),
            raw,
        )

    # 没有域名。裸抖音 ID 只在文本里明确提到「抖音」时才认。
    if "抖音" in raw:
        bare_douyin = _STANDALONE_DOUYIN_ID.search(text)
        if bare_douyin:
            return ParsedLink("douyin", bare_douyin.group(1), None, raw)

    return ParsedLink(None, None, None, raw)
