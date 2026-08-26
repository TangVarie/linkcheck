"""面板视觉的机器闸（BYWOOD 设计系统 v3 · 界面轨「管理后台」）。

设计系统自己带一个 `tools/check-design.py`，但那个工具在**设计系统包里**，
不在这个仓库——CI 上跑不到。所以把它管的那几条里**最容易在改样式时踩回去**
的，在这里用离线测试钉一遍：色板外色值、圆角、阴影、渐变、毛玻璃、emoji 当图标。

这不是复述整份规范（那会变成维护两份），只钉六条**不需要看上下文就能判**的
硬规则 + 两条这一轨特有的构件上限。品牌跑偏在这个体系里算构建错误，
不是审美分歧——那就得有个东西在构建时会红。
"""

import re
import unittest

from xhsearch import balance, panel, panel_view, summary
from xhsearch.config import Settings


def render() -> str:
    cfg = panel.PanelConfig.from_env({
        "PANEL_PASSWORD": "a-long-enough-password",
        "FEISHU_DOMAIN": "https://x.feishu.cn"})
    cfg.api_keys = {"tikhub": "k"}
    ov = summary.Overview(projects=[summary.ProjectSnapshot(
        label="项目甲", app_token="bascnA", table_id="tbl1",
        table_url="https://x.feishu.cn/base/bascnA?table=tbl1")])
    bal = [balance.Balance(channel="tikhub", label="TikHub", amount=12.5,
                           unit="USD", yuan=90.0, rate=7.2)]
    rw = panel.Runway(days=9.0, yuan_per_day=14.6, yuan_left=132.0,
                      runs_used=12, hours_covered=24.0)
    return panel_view.overview_page(
        overview=ov, error="", fetched_at=1756209600.0, config=cfg,
        balances=bal, runway=rw, runs=[])


# tokens/palette.json 的 screen 节（light + dark）+ 中性值。
# 唯一事实源在设计系统包里；这里是它的一份副本，改色板要两边同改。
PALETTE = {
    "#235E8E", "#1B4A72", "#E5262B", "#D9232B",
    "#E3EDF5", "#FBE3E3", "#00B578", "#FF8F1F", "#D92B3C",
    "#B02330", "#006B4A", "#995400",
    "#F3F4F6", "#EAECEF", "#FFFFFF", "#E5E6EB",
    "#3F7FB2", "#346892", "#E8575C", "#C22329", "#1E3B54", "#442228",
    "#21B183", "#FFA24D", "#C64449", "#7FE0BD", "#FFC38A", "#FF9A9E",
    "#0F1318", "#151A20", "#1B222B", "#2B3440",
    "#FFF", "#000", "#000000",
}


class TestPalette(unittest.TestCase):
    def test_every_hex_in_the_page_is_from_the_palette(self):
        """业务代码里禁止裸 hex；这里连渲染出来的页面一起查。

        标志正红 `#E5262B` 只允许出现在标志图形本身（BRAND.md §2），
        下面单独有一条钉它。
        """
        html = render() + panel_view.login_page("x")
        bad = {h.upper() for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", html)
               if h.upper() not in PALETTE}
        self.assertEqual(bad, set(), f"页面上出现了色板外的色值：{sorted(bad)}")

    def test_the_logo_red_appears_only_inside_the_mark(self):
        """标志正红只给标志图形。文字、线条、色块都不用这一档。"""
        html = render()
        for chunk in re.findall(r"#E5262B", html, re.I):
            pass
        # 每一处 #E5262B 都必须落在 <svg …aria-label="BYWOOD"> 里
        marks = re.findall(r"<svg[^>]*aria-label=\"BYWOOD\".*?</svg>", html,
                           re.S)
        inside = sum(m.upper().count("#E5262B") for m in marks)
        total = html.upper().count("#E5262B")
        self.assertEqual(inside, total,
                         "标志正红出现在了标志图形之外")


def css_without_comments() -> str:
    """去掉 CSS 注释再查。注释里本来就写着「border-radius: 0」这种句子
    ——那是在说明规则，不是在违反它，扫源码文本会把它当成命中。"""
    return re.sub(r"/\*.*?\*/", "", panel_view._STYLE, flags=re.S)


class TestFlatRules(unittest.TestCase):
    """直角 / 零阴影 / 零渐变 / 零毛玻璃。"""

    def setUp(self):
        self.css = css_without_comments()

    def test_no_rounded_corners(self):
        """圆角是通用 AI 视觉的第一标志，本系统不用。
        例外只有本体即圆形的东西，届时要带 `/* ok: 理由 */`。"""
        for value in re.findall(r"border-radius\s*:\s*([^;}]+)", self.css):
            self.assertIn(value.strip(), ("0", "50%"),
                          f"border-radius: {value.strip()}")

    def test_no_shadows(self):
        found = [v for v in re.findall(r"box-shadow\s*:\s*([^;}]+)", self.css)
                 if v.strip() not in ("none", "inherit")
                 and "shadow-sheet" not in v]
        self.assertEqual(found, [], f"阴影仅抽屉一档：{found}")

    def test_no_backdrop_filter(self):
        self.assertNotIn("backdrop-filter", self.css, "毛玻璃零豁免")

    def test_the_only_gradient_is_the_skeleton_shimmer(self):
        """渐变的唯一豁免是骨架屏微光——判据和设计系统那个机器闸一致：
        看命中行前后有没有 skeleton / shimmer / 骨架 的标记。"""
        lines = panel_view._STYLE.splitlines()
        for i, line in enumerate(lines):
            if "gradient(" not in line:
                continue
            ctx = "\n".join(lines[max(0, i - 3):i + 1]).lower()
            self.assertTrue("骨架" in ctx or "shimmer" in ctx
                            or "skeleton" in ctx,
                            f"非骨架屏的渐变：{line.strip()}")


class TestNoEmojiIcons(unittest.TestCase):
    def test_the_page_uses_line_icons_not_emoji(self):
        """负面清单 #1：用 emoji 当图标。图标一律 lucide 线性内联 SVG。

        只查**渲染出来的页面**——服务端日志里的 emoji 是运维文本，
        不是界面，不在这一轨的管辖范围。
        """
        import unicodedata
        html = render() + panel_view.login_page("x")
        bad = {ch for ch in html
               if ord(ch) > 0x2100 and unicodedata.category(ch) == "So"}
        self.assertEqual(bad, set(), f"页面上有 emoji 当图标：{bad}")

    def test_icons_are_inlined_not_fetched(self):
        """CSP 是 default-src 'none'，而且这个项目零依赖——
        图标只能内联，不能是外链或字体图标。"""
        html = render()
        self.assertIn("<svg class=\"icon\"", html)
        for pattern in ("<link", "@font-face", "cdn.", "unpkg", "googleapis"):
            self.assertNotIn(pattern, html, f"页面引了外部资源：{pattern}")


class TestDashboardComponents(unittest.TestCase):
    """scenarios/03 点名的两条高发 bug。"""

    def test_one_deep_block_and_at_most_three_white_cards(self):
        """负面清单 #9：同色同规格统计盒子排排坐。
        后台轨的上限是 1 个深蓝主块 + 最多 3 张白卡。"""
        html = render()
        kpi = re.search(r"<div class='kpi[^']*'>(.*?)</div>\s*\n", html, re.S)
        self.assertIsNotNone(kpi, "找不到 KPI 行")
        block = kpi.group(1)
        self.assertEqual(block.count("<div class=lead>"), 1,
                         "深蓝主块必须恰好一个")
        self.assertLessEqual(len(re.findall(r"<div class='box", block)), 3,
                             "白底 KPI 卡最多 3 张")

    def test_sidebar_active_is_tint_not_deep_blue(self):
        """03 场景明文点名的 bug：侧栏激活态用了深蓝底白字。
        应该是雾蓝底 + 品牌蓝字。"""
        css = css_without_comments()
        rule = re.search(r"\.side nav a\.on\{([^}]+)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("var(--tint-sky)", rule.group(1))
        self.assertIn("var(--primary)", rule.group(1))

    def test_tables_have_no_zebra_stripes(self):
        css = css_without_comments()
        self.assertNotIn("nth-child(even)", css)
        self.assertNotIn("nth-child(odd)", css)

    def test_the_chevron_is_a_link_tail_not_a_heading_bullet(self):
        """BRAND.md §4 允许双箭做「区块标题前标」和「链接尾标」两种，
        但这一轨的定式（templates/dashboard.html）里前标 0 处、尾标 2 处。

        五个区块各挂一个前标，双箭就从品牌重音降成了项目符号——
        而且「一屏最多 3 处」那条上限也悬。
        """
        css = css_without_comments()
        self.assertNotIn("h2::before", css, "h2 不该带双箭前标")
        html = render()
        self.assertIn("»", html, "双箭该以链接尾标的形态出现")
        for tail in re.findall(r"([^>]{0,12})»", html):
            self.assertNotIn("<h2", tail)

    def test_row_actions_are_small_and_blue(self):
        """scenarios/03：「行操作用 13px 蓝字链接」。

        模板里通用的 `a` 是 accent 红，和这条冲突——仲裁顺序里
        **场景文件规则高于可执行模板**，所以行操作听场景的。
        """
        css = css_without_comments()
        rule = re.search(r"td a\.act\{([^}]+)\}", css)
        self.assertIsNotNone(rule, "行操作链接没有单独的样式")
        self.assertIn("13px", rule.group(1))
        self.assertIn("var(--primary)", rule.group(1))
        self.assertIn("class=act", render())

    def test_a_space_between_a_number_and_a_chinese_unit(self):
        """模板里一律 `3 天` / `20 条` / `6 个`。

        **先把标签剥掉再查**：`9<small>天</small>` 在源码里数字和量词之间
        隔着一个标签，但渲染出来是紧挨着的——按源码查会漏掉这一类。
        """
        text = re.sub(r"<[^>]+>", "", render())
        bad = re.findall(r"\d(天|行|个|条|次|张|轮)", text)
        self.assertEqual(bad, [], f"数字和中文量词之间少了空格：{bad}")

    def test_money_numbers_carry_the_num_class(self):
        """负面清单 #12：不带 num 类的金额数字。"""
        html = render()
        self.assertIn("class='n num'", html)
        self.assertIn("¥", html)


class TestDarkMode(unittest.TestCase):
    def test_dark_is_a_token_flip_only(self):
        """负面清单 #17：深色模式下写第二套组件颜色。
        深色块只应该出现在 :root / [data-theme] / prefers-color-scheme 里。"""
        css = css_without_comments()
        dark = re.findall(r"\[data-theme=dark\]\{|prefers-color-scheme:dark", css)
        self.assertEqual(len(dark), 2, "深色模式应该只有手动和跟随系统两处")
        # 组件规则里不许再出现深色专属色值
        body = css.split("*{box-sizing", 1)[1]
        for token in ("#0F1318", "#1B222B", "#3F7FB2"):
            self.assertNotIn(token, body,
                             f"组件区出现了深色专属色值 {token}")


if __name__ == "__main__":
    unittest.main()
