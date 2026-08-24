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

import errno
import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:      # Windows：没有 flock，退回 O_EXCL + TTL
    fcntl = None         # type: ignore[assignment]

def default_path() -> str:
    """默认锁文件路径：**每个用户一个私有目录**，不是 /tmp 里一个可预测的文件名。

    直接用 /tmp/linkcheck.run.lock 是有安全后果的：共享主机上任何本地用户都能
    在服务第一次运行**之前**，把那个路径先建成一个指向别处的符号链接。
    我们拿到 flock 之后 _write_info 会 ftruncate + 写——那就成了一个
    「让别人指哪、我们清空并覆盖哪」的任意文件改写原语，服务权限越高越糟。

    两道防线：目录按 0o700 建在自己名下（别人建不进来），
    以及打开文件时带 O_NOFOLLOW（不跟符号链接走）。
    """
    return str(Path(tempfile.gettempdir()) / f"linkcheck-{os.getuid()}" / "run.lock"
               if hasattr(os, "getuid")
               else Path(tempfile.gettempdir()) / "linkcheck" / "run.lock")


# 租约多久算过期。一次运行正常是几十秒到几分钟；30 分钟还没释放，
# 要么是进程被 kill -9 且文件锁没生效（无 fcntl 的分支），要么是真的挂死了。
DEFAULT_TTL_SECONDS = 1800.0

# 只有这两个 errno 代表「锁被别人占着」。别的 OSError（ENOTSUP/ENOLCK/EINVAL
# 等，某些 NFS 配置和容器挂载会给）说明这个文件系统压根不支持 flock——
# 把它们也当成 Busy，等于让部署安静地跳过每一次定时运行。
_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN})


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
    # 上一任是不是**正常释放**的。没有 fcntl 的平台（Windows）全靠它：
    # 那条路上内核不会替我们解锁，只看 TTL 的话，一次正常跑完的运行会把
    # 后面 30 分钟内的每一次调用都挡掉。
    released: bool = False

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
        # 先把"我正常收工了"写进去，再解锁：没有 fcntl 的平台完全靠这个标记
        # 判断上一任是正常释放还是崩掉的。不写的话，一次跑完的运行会把
        # 后面 TTL 之内的每一次调用都误挡成 Busy。
        self.info.released = True
        try:
            _write_info(self._fd, self.info)
        except OSError:
            pass
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
            released=bool(raw.get("released")),
        )
    except (TypeError, ValueError):
        return None


def _write_info(fd: int, info: LeaseInfo) -> None:
    payload = json.dumps({
        "owner": info.owner, "pid": info.pid, "host": info.host,
        "acquired_at": info.acquired_at, "token": info.token,
        "released": info.released,
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
    lock_path = Path(path or os.environ.get("RUN_LOCK_PATH") or default_path())
    # 目录按 0o700 建：别人就没法在我们之前把锁文件预置成一个符号链接。
    # 已存在的目录不改权限（可能是运维刻意配的共享位置），
    # 打开文件时的 O_NOFOLLOW 是这种情况下的第二道防线。
    os.makedirs(lock_path.parent, mode=0o700, exist_ok=True)
    previous = _read_info(lock_path)
    info = LeaseInfo(
        owner=owner,
        pid=os.getpid(),
        host=socket.gethostname(),
        acquired_at=time.time(),
        token=(previous.token + 1) if previous else 1,
    )
    # O_NOFOLLOW：绝不跟着符号链接走。锁文件路径是可预测的，而我们随后会
    # ftruncate + 写它——跟着链接走就等于把「清空并覆盖任意文件」的能力
    # 交给任何能在这个目录里建文件的人。
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)

    if fcntl is not None:
        fd = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno not in _CONTENTION_ERRNOS:
                # 不是「别人占着」，是这个文件系统根本不支持 flock
                # （NFS 某些配置、部分容器挂载会给 ENOTSUP/ENOLCK/EINVAL）。
                # 当成 Busy 的话，CLI 会安静地跳过**每一次**定时运行并返回 0，
                # 一个部署可以就这样永远不刷表而没有任何人发现。
                # 抛出去，让调用方走「拿不到锁」那条会吵闹的分支。
                raise
            raise Busy(previous) from None
        _write_info(fd, info)
        return Lease(lock_path, fd, info)

    # ---- 没有 flock 的退路（Windows）----
    # 这条路上内核不会替我们解锁，所以判据有两条：上一任**明确释放过**，
    # 或者它已经超过 TTL（崩溃后自愈）。只看 TTL 的话，一次正常跑完的运行
    # 会把后面 30 分钟内的每一次调用都误挡成 Busy。
    if (previous is not None and not previous.released
            and (time.time() - previous.acquired_at) < ttl_seconds):
        raise Busy(previous)
    try:
        # 先试原子创建：文件还不存在时，这一步能保证只有一个进程建得出来。
        fd = os.open(lock_path, flags | os.O_EXCL, 0o600)
    except FileExistsError:
        # 文件已存在（上一任正常释放、或已过期）：接管它。
        # ⚠️ 这一步不是原子的——没有 flock 就做不到，这是这条退路的已知弱点，
        # 也是为什么 Windows 上更该确保只有一个调度器在跑。
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise Busy(previous) from exc
    _write_info(fd, info)
    return Lease(lock_path, fd, info)
