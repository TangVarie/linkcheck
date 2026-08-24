"""运行租约：同一时刻只允许一个付费执行者。

**为什么必须有它**

这套东西的每一个入口（queue / sweep / row / 手动触发）都会做同一件事：
读表 → 花钱调接口 → 整列覆盖写回。两个进程同时做这件事的后果不是"慢一点"，
而是三条一起中招：

1. 同一批行被调两遍 —— 钱花两份；
2. 两份基于各自旧快照的整列写入互相覆盖 —— 人工标签和历史指标丢更新；
3. 同表并发写 —— 飞书返回写冲突（1254291）。

而平台自带的互斥都不够用：Railway 的"上一轮没跑完就跳过"是**按 cron service**
生效的，官方部署方案却建了 queue、sweep 两个 service，它们之间没有任何互斥
（而且 UTC 00:05/08:05/16:05 三个时刻还会天然同分钟触发）；GitHub 的
concurrency group 只约束用同一个 group 的 workflow，管不到 Railway 和 VPS；
VPS 的 crontab 默认允许重叠，文档里的 flock 示例只包了 queue 一条。

**这个模块能做到什么、不能做到什么（请务必读完再决定部署拓扑）**

能做到：**同一台机器 / 同一个文件系统**上的互斥。VPS 上的两条 cron、
同一台机器上手滑并发跑的两个命令、同一个 Railway service 的重叠执行，
都会被挡下——第二个进程直接退出，不发一个付费请求。

做不到：**跨主机**互斥。Railway 的两个 service 各有自己的容器和文件系统，
一台 VPS 和一个 GitHub Actions runner 之间更没有共享盘。跨主机互斥需要一个
共享存储（Postgres / Redis / 对象存储的条件写），这个仓库刻意零依赖，
不引入它。

所以**部署上的硬规矩是：定时调度只能有一个地方跑**（见 docs/部署.md）。
这个租约是那条规矩的兜底和自检，不是它的替代品。
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:      # Windows：没有 flock，退回 O_EXCL + TTL
    fcntl = None         # type: ignore[assignment]

DEFAULT_PATH = "/tmp/linkcheck.run.lock"
# 租约多久算过期。一次运行正常是几十秒到几分钟；30 分钟还没释放，
# 要么是进程被 kill -9 且文件锁没生效（O_EXCL 分支），要么是真的挂死了。
DEFAULT_TTL_SECONDS = 1800.0


@dataclass
class LeaseInfo:
    """租约文件里记的东西。抢不到锁时打给运维看：到底是谁占着。"""

    owner: str
    pid: int
    host: str
    acquired_at: float
    # 单调递增的 fencing token。一个"以为自己还持有租约"的僵尸进程
    # 拿着旧 token，凭它可以在日志里被认出来。
    token: int = 0

    def describe(self) -> str:
        age = max(0.0, time.time() - self.acquired_at)
        return (f"{self.owner}（pid={self.pid} @ {self.host}，"
                f"已持有 {age:.0f} 秒，token={self.token}）")


class Lease:
    """持有中的租约。用完必须 release()，或者当上下文管理器用。"""

    def __init__(self, path: Path, fd: int, info: LeaseInfo):
        self.path = path
        self._fd = fd
        self.info = info

    @property
    def token(self) -> int:
        return self.info.token

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = -1
        # 文件留着不删：里面记着"上一次是谁跑的、什么时候跑的"，
        # 也让 fencing token 能继续递增。删掉会让 token 从头开始。

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False


class Busy(RuntimeError):
    """另一个进程正持有租约。"""

    def __init__(self, holder: Optional[LeaseInfo]):
        self.holder = holder
        detail = holder.describe() if holder else "另一个进程"
        super().__init__(f"另一个巡查进程正在运行：{detail}")


def _read_info(path: Path) -> Optional[LeaseInfo]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return LeaseInfo(
            owner=str(raw.get("owner") or "?"),
            pid=int(raw.get("pid") or 0),
            host=str(raw.get("host") or "?"),
            acquired_at=float(raw.get("acquired_at") or 0.0),
            token=int(raw.get("token") or 0),
        )
    except (TypeError, ValueError):
        return None


def _write_info(fd: int, info: LeaseInfo) -> None:
    payload = json.dumps({
        "owner": info.owner, "pid": info.pid, "host": info.host,
        "acquired_at": info.acquired_at, "token": info.token,
    }, ensure_ascii=False)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload.encode("utf-8"))
    try:
        os.fsync(fd)
    except OSError:
        pass


def acquire(owner: str, *, path: Optional[str] = None,
            ttl_seconds: float = DEFAULT_TTL_SECONDS) -> Lease:
    """拿租约。拿不到抛 Busy（里面带着当前持有者的信息）。

    优先用 flock：进程无论怎么死（含 kill -9、OOM、容器被回收），
    内核都会释放它，不会留下一把谁也解不开的锁。
    没有 flock 的平台退回 O_EXCL + TTL：那条路上崩溃会留下陈旧锁文件，
    所以必须有 TTL 才能自愈。
    """
    lock_path = Path(path or os.environ.get("RUN_LOCK_PATH") or DEFAULT_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_info(lock_path)
    info = LeaseInfo(
        owner=owner,
        pid=os.getpid(),
        host=socket.gethostname(),
        acquired_at=time.time(),
        token=(previous.token + 1) if previous else 1,
    )

    if fcntl is not None:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise Busy(previous) from None
        _write_info(fd, info)
        return Lease(lock_path, fd, info)

    # ---- 没有 flock 的退路 ----
    if previous is not None and (time.time() - previous.acquired_at) < ttl_seconds:
        raise Busy(previous)
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        raise Busy(previous) from exc
    _write_info(fd, info)
    return Lease(lock_path, fd, info)
