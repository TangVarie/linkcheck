"""把仓库根目录的 .env 读进环境变量（零依赖，一个严格的小解析器）。

Railway / GitHub Actions 由平台注入环境变量，用不到这里；本地跑
doctor / row / 探测脚本时，照 .env.example 填一份 .env 就能直接跑，
不需要手动 export——Windows PowerShell 上手动设环境变量尤其麻烦。

已存在的环境变量**优先**：.env 只补缺、不覆盖，和各平台的行为一致，
也保证本地临时 `TIKHUB_BASE=... python3 cli.py ...` 这种覆盖仍然生效。

**支持的语法（刻意很小，而且拒绝看不懂的行）**

    KEY=value                # 行内注释要用空格隔开 #
    KEY="value with spaces"  # 双引号里支持 \\" \\\\ \\n \\t 转义
    KEY='raw value'          # 单引号里一切原样，不处理转义
    export KEY=value         # 认 export 前缀，方便和 `source .env` 混用
    # 整行注释

不支持变量展开（`$OTHER`）、多行值、行尾续行。

为什么要严格：旧版只做 `strip('"').strip("'")`，`KEY="a#b"` 会被读成
`a#b` 而 `KEY=a  # 说明` 会被读成 `a  # 说明`——一个引号或一个井号的
差别，产生的是一个**看起来配好了、实际值不对**的部署，而且不会报错。
现在看不懂的行会被收进 issues 里，由调用方决定是提示还是拒跑。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

# 环境变量名的合法形态。放行别的形态没有意义：os.environ 的键必须是字符串，
# 而带空格/等号的名字在任何 shell 里都取不出来。
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


def _unescape(text: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            nxt = text[index + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_line(line: str) -> tuple[Optional[str], Optional[str], str]:
    """解析一行。返回（名字, 值, 问题描述）。名字为 None = 这一行没有赋值。

    独立成纯函数，好让测试直接喂各种畸形写法。
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None, None, ""
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        return None, None, f"没有 `=`，看不懂：{line.strip()!r}"

    name, _, rest = stripped.partition("=")
    name = name.strip()
    if not _NAME_RE.match(name):
        return None, None, f"变量名 {name!r} 不合法（只允许字母、数字、下划线，且不能以数字开头）"

    rest = rest.strip()
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        # 找配对的收尾引号：双引号要跳过被转义的 \"
        index = 1
        while index < len(rest):
            if quote == '"' and rest[index] == "\\":
                index += 2
                continue
            if rest[index] == quote:
                break
            index += 1
        else:
            return None, None, f"{name} 的值有未配对的引号：{line.strip()!r}"
        if index >= len(rest):
            return None, None, f"{name} 的值有未配对的引号：{line.strip()!r}"
        body = rest[1:index]
        tail = rest[index + 1:].strip()
        if tail and not tail.startswith("#"):
            return None, None, f"{name} 的收尾引号后面还有内容：{tail!r}"
        return name, (_unescape(body) if quote == '"' else body), ""

    # 无引号：行内注释必须用空白隔开（和 dotenv 的通行约定一致）。
    # 不这么规定的话，`TOKEN=abc#def` 里的 `#def` 到底是不是注释就没法判。
    comment = re.search(r"\s#", rest)
    if comment:
        rest = rest[: comment.start()]
    return name, rest.strip(), ""


def load_dotenv(directory: Optional[Path] = None, *,
                issues: Optional[list[str]] = None) -> None:
    """读 directory（默认仓库根）下的 .env。文件不存在就静默跳过。

    看不懂的行不再静默吞掉：追加进 issues（调用方负责提示），
    空值的变量也照常设置（`KEY=` 表示"明确置空"，和"没写这一行"不同）。
    """
    root = directory or Path(__file__).resolve().parent.parent
    try:
        text = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        name, value, problem = parse_line(line)
        if problem:
            if issues is not None:
                issues.append(f".env 第 {number} 行：{problem}")
            continue
        if name and value:
            os.environ.setdefault(name, value)
