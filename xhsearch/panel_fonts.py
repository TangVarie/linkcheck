"""面板自带的两份字体：BW Display（标题，思源黑体 Black 的子集）和 Archivo（数字）。

为什么自带：设计系统 v6.1 的标题用 BW Display、数字用 Archivo 800（DESIGN.md §三）。
面板零外部资源、CSP 是 `default-src 'none'`，不能走 CDN；所以字体文件放在仓库的
`xhsearch/fonts/` 里，由面板自己的 `/fonts/` 路由同源发出，CSP 只多放一条 `font-src 'self'`。

文件名带内容哈希、响应头写一年 `immutable`：浏览器只下一次（两份加起来几十 KB），
字体一换，URL 跟着换，不会拿到旧缓存。

两份都是 SIL OFL 1.1，许可证与来源见 `xhsearch/fonts/OFL.txt`、`xhsearch/fonts/README.md`。
重新出子集：`tools/build_panel_fonts.py`（要 fonttools，只在出字体时用，面板运行时不需要）。

这个模块只用标准库：读文件、算哈希、拼 `@font-face`。字体文件读不到就当没有——
页面照样出，标题和数字退回系统字，不会因为少一个文件打不开面板。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

FONT_DIR = Path(__file__).with_name("fonts")
CACHE_CONTROL = "public, max-age=31536000, immutable"
CONTENT_TYPE = "font/woff2"

# 用显示字体（CSS 里的 --f-dsp）排的字。全部是写死的文案：页面标题、章节标题、
# 登录页青面上那一句、大数字后面的单位。子集只收这些字，外加 ALWAYS 里的标点与数字。
# 改了这些地方的字，tests/test_design.py 的覆盖检查会红——那时跑一遍
# tools/build_panel_fonts.py 重出子集。
DISPLAY_TEXT = ("内容监控面板" "要人管的行" "各项目" "项目" "搜索可见试跑" "余额" "运行历史"
                "面板负责发现，飞书负责处理。" "天" "积分")
ALWAYS = "0123456789，。、「」：；！？·—…（）《》 %+-–/"

# Archivo 只管数字、金额、日期和编号里的拉丁字符；中文字不进它（unicode-range 管着），
# 浏览器也就不会为了一个中文字去下它。
# ≈ 和 → Archivo 原件里就没有，不写进来，那两个字直接用系统字。
ARCHIVO_RANGE = "U+0020-007E,U+00A0-00FF,U+2013-2014,U+2018-201D,U+2026,U+2212"
# 子集保留的可变轴范围：正文数字 400、标签 600–700、大数字 800；宽度 108–112%（DESIGN.md §三）。
ARCHIVO_AXES = {"wght": (400, 900), "wdth": (100, 125)}


def _load(name: str):
    try:
        data = (FONT_DIR / f"{name}.woff2").read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()[:10]
    return f"/fonts/{name}.{digest}.woff2", data


# url → 字节。面板的 /fonts/ 路由只认这张表里的 URL，别的一律 404——不拼路径，不碰文件系统。
ROUTES: dict[str, bytes] = {}
# 字体名 → url
URLS: dict[str, str] = {}
for _name in ("display", "archivo"):
    _found = _load(_name)
    if _found is not None:
        URLS[_name] = _found[0]
        ROUTES[_found[0]] = _found[1]


def covered_chars() -> str:
    """BW Display 子集实际收了哪些字（出子集时一并写下的清单）。读不到就是空串。"""
    try:
        return (FONT_DIR / "display.chars.txt").read_text(encoding="utf-8")
    except OSError:
        return ""


def font_face_css() -> str:
    """两条 `@font-face`。显示字体 `block`（标题宁可晚一点出，不先闪一下系统字再换粗细），
    数字 `swap`（数字先出来最要紧）。值和 components/ui.css 的写法一致。"""
    out = []
    if "display" in URLS:
        out.append('@font-face{font-family:"BW Display";'
                   f'src:url({URLS["display"]}) format("woff2");'
                   "font-weight:900;font-display:block}")
    if "archivo" in URLS:
        low, high = ARCHIVO_AXES["wght"]
        narrow, wide = ARCHIVO_AXES["wdth"]
        out.append('@font-face{font-family:"Archivo";'
                   f'src:url({URLS["archivo"]}) format("woff2");'
                   f"font-weight:{low} {high};font-stretch:{narrow}% {wide}%;"
                   f"font-display:swap;unicode-range:{ARCHIVO_RANGE}}}")
    return "\n".join(out)
