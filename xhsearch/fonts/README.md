# 面板自带的字体

面板按 BYWOOD 设计系统 v6.1 排版：标题用 **BW Display**（思源黑体 Black），数字用 **Archivo**。
面板零外部资源、CSP 是 `default-src 'none'`，所以两份字体放在这里，由面板的 `/fonts/` 路由同源发出
（文件名带内容哈希，缓存一年）。说明见 `xhsearch/panel_fonts.py`。

| 文件 | 是什么 | 来源 | 大小 |
|---|---|---|---|
| `display.woff2` | Noto Sans SC 900（= 思源黑体 Heavy，设计系统 BW Display 的底字）的子集，只收面板标题、登录页那一句、大数字单位里用到的字 | npm `@fontsource/noto-sans-sc@5.3.1` 的 `files/noto-sans-sc-*-900-normal.woff2` | 约 8 KB |
| `display.chars.txt` | 上面那份子集收了哪些字；`tests/test_design.py` 拿它查页面上的标题字是不是都在 | 出子集时写下 | — |
| `archivo.woff2` | Archivo 可变字体，轴收窄到 wght 400–900、wdth 100–125，字符收窄到拉丁、数字和常用标点 | 设计系统包 `components/fonts/archivo.woff2` | 约 46 KB |
| `OFL.txt` | 两份字体的版权声明与 SIL Open Font License 1.1 全文 | 字体自带的版权记录；许可证全文取自上面 npm 包的 `LICENSE` | — |

**改了标题文案**（页面标题、章节标题、登录页那一句、KPI / 余额的单位）之后，
`tests/test_design.py` 会提示有字不在子集里，这时重出一遍：

```bash
pip install fonttools brotli          # 只有出字体要用，面板运行不需要
npm pack @fontsource/noto-sans-sc@5.3.1 && tar -xzf fontsource-noto-sans-sc-5.3.1.tgz
python3 tools/build_panel_fonts.py package/files <设计系统包>/components/fonts/archivo.woff2
```

要收的字写在 `xhsearch/panel_fonts.py` 的 `DISPLAY_TEXT`。
