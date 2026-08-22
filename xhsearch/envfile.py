"""把仓库根目录的 .env 读进环境变量（零依赖，10 行解析）。

Railway / GitHub Actions 由平台注入环境变量，用不到这里；本地跑
doctor / row / 探测脚本时，照 .env.example 填一份 .env 就能直接跑，
不需要手动 export——Windows PowerShell 上手动设环境变量尤其麻烦。

已存在的环境变量**优先**：.env 只补缺、不覆盖，和各平台的行为一致，
也保证本地临时 `TIKHUB_BASE=... python3 cli.py ...` 这种覆盖仍然生效。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(directory: Optional[Path] = None) -> None:
    """读 directory（默认仓库根）下的 .env。文件不存在就静默跳过。

    只认 KEY=VALUE 一行一条；# 开头是注释；值两侧的引号会剥掉。
    """
    root = directory or Path(__file__).resolve().parent.parent
    try:
        text = (root / ".env").read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value:
            os.environ.setdefault(name, value)
