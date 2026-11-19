"""出面板自带的两份字体子集（xhsearch/fonts/）。只在改了标题文案、或者要换字体版本时跑。

    python3 tools/build_panel_fonts.py <Noto Sans SC 900 的 woff2 目录> <archivo.woff2>

两个输入从哪来：

* **Noto Sans SC 900**（= 思源黑体 Heavy = 设计系统的 BW Display 底字）：npm 上的
  `@fontsource/noto-sans-sc` 包里 `files/noto-sans-sc-*-900-normal.woff2`，是 Google Fonts
  按字段切好的一百来片。`npm pack @fontsource/noto-sans-sc` 下下来解开，把 `package/files`
  目录传进来。本脚本挑出含所需字的那几片，各自裁剪，再合成一份。
* **Archivo**：设计系统包里的 `components/fonts/archivo.woff2`（可变字体，wght 100–900、wdth 62–125）。
  本脚本把轴收窄到 `panel_fonts.ARCHIVO_AXES`，字符收窄到 `panel_fonts.ARCHIVO_RANGE`。

要哪些字由 `xhsearch/panel_fonts.py` 的 DISPLAY_TEXT + ALWAYS 决定。输出：
`xhsearch/fonts/display.woff2`、`display.chars.txt`（收了哪些字，测试拿它查覆盖）、`archivo.woff2`。

依赖 fonttools 与 brotli（`pip install fonttools brotli`）。这是出字体的工具，**面板运行时不需要**：
项目本身仍然零第三方依赖，所以这里在函数里才 import。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xhsearch import panel_fonts  # noqa: E402


def _codepoints(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip().removeprefix("U+")
        if "-" in part:
            low, high = part.split("-")
            out.extend(range(int(low, 16), int(high, 16) + 1))
        else:
            out.append(int(part, 16))
    return out


def build_display(slices_dir: Path, out_dir: Path) -> str:
    from fontTools import subset as fs
    from fontTools.merge import Merger
    from fontTools.ttLib import TTFont

    wanted = set(panel_fonts.DISPLAY_TEXT + panel_fonts.ALWAYS)
    pieces, found = [], set()
    with tempfile.TemporaryDirectory() as tmp:
        for path in sorted(slices_dir.glob("*-900-normal.woff2")):
            cmap = TTFont(path).getBestCmap()
            here = {c for c in wanted - found if ord(c) in cmap}
            if not here:
                continue
            font = TTFont(path)
            options = fs.Options()
            options.layout_features = []          # 合并要求版式表一致；标题里不需要连字与字距
            options.drop_tables += ["GSUB", "GPOS", "GDEF"]
            options.name_IDs = ["*"]
            options.notdef_outline = True
            sub = fs.Subsetter(options)
            sub.populate(text="".join(sorted(here)))
            sub.subset(font)
            font.flavor = None
            piece = os.path.join(tmp, f"piece{len(pieces)}.ttf")
            font.save(piece)
            pieces.append(piece)
            found |= here
        missing = wanted - found
        if missing:
            sys.exit("这几片里找不到：" + "".join(sorted(missing)))
        merged = Merger().merge(pieces) if len(pieces) > 1 else TTFont(pieces[0])
    merged.flavor = "woff2"
    merged.save(out_dir / "display.woff2")
    covered = "".join(sorted(chr(c) for c in merged.getBestCmap() if chr(c) in wanted))
    (out_dir / "display.chars.txt").write_text(covered, encoding="utf-8")
    return covered


def build_archivo(source: Path, out_dir: Path) -> None:
    from fontTools import subset as fs
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(source)
    font = instancer.instantiateVariableFont(font, dict(panel_fonts.ARCHIVO_AXES))
    options = fs.Options()
    options.layout_features = ["kern", "tnum", "lnum", "case"]
    options.name_IDs = ["*"]
    sub = fs.Subsetter(options)
    sub.populate(unicodes=_codepoints(panel_fonts.ARCHIVO_RANGE))
    sub.subset(font)
    font.flavor = "woff2"
    font.save(out_dir / "archivo.woff2")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out_dir = Path(panel_fonts.FONT_DIR)
    out_dir.mkdir(exist_ok=True)
    covered = build_display(Path(argv[0]), out_dir)
    build_archivo(Path(argv[1]), out_dir)
    for name in ("display.woff2", "archivo.woff2"):
        print(f"{name}: {(out_dir / name).stat().st_size / 1024:.1f} KB")
    print(f"display 收了 {len(covered)} 个字")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
