"""门禁临时目录共享管理 —— 创建即登记，进程退出时 atexit 强制扫除。

为什么需要它（2026-10-26 实测事故）：
    40+ 门禁各自 `tempfile.mkdtemp()` 造夹具目录，跑完只删里面的 .db 文件、
    **不删目录本身**；Windows 下文件被 sqlite 连接占用时 `os.remove`/`rmtree`
    还会**静默失败** ⇒ %TEMP% 一度累积 1266 个空目录，并间接推高磁盘占用。

用法（与 `tempfile.mkdtemp` 同签名，仅关键字参数）：
    from _tmpdir import mkdtemp
    d = mkdtemp(prefix="wb_p07_")          # 退出时自动回收整个目录
    path = os.path.join(mkdtemp(), "x.db") # 同样安全

设计要点：
    ① 登记机制 + `atexit`：门禁正常退出时把整目录删掉，不再留空壳。
    ② `safe_rmtree`：Windows 文件锁导致 rmtree 失败时，重试 + 兜底删空目录，
       不静默吞错（与门禁「判据要可见」纪律一致）。
    ③ `sweep_temp()`：回归级兜底 —— 即使有门禁没改用本 helper，也能按前缀白名单
       扫掉 %TEMP% 下残留的门禁夹具目录（含 .db 或空目录才删，绝不误删用户目录）。
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time

# 门禁夹具目录前缀白名单（命中且含 .db 或为空目录才清扫，防误删用户文件）
SWEEP_PREFIXES = (
    "wb_p07_", "wb_p14_", "wb_mtctx_", "wb_gate_",
    "verify_", "docf_orphan_", "rf_drift_", "memtier_",
    "cancel_", "dedup_probe_", "eval_gate_", "verify_attr_d6_",
    "verify_ck_mut_", "verify_idem_mut_",
)

_TRACKED: set[str] = set()
_LOCK = threading.Lock()
_REGISTERED = False


def mkdtemp(prefix: str = "wb_gate_", suffix: str = "", dir: str | None = None) -> str:
    """等价 `tempfile.mkdtemp`（参数顺序一致），但把目录登记到全局集合，进程退出时自动删除。"""
    d = tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=dir)
    with _LOCK:
        _TRACKED.add(os.path.abspath(d))
    _ensure_registered()
    return d


def _ensure_registered() -> None:
    global _REGISTERED
    if not _REGISTERED:
        atexit.register(cleanup_all)
        _REGISTERED = True


def safe_rmtree(path: str) -> bool:
    """尽力删目录；Windows 文件被连接占用时 rmtree 会失败，重试 + 兜底删空目录。

    :returns: True 表示目录已不存在（删成功或原本就不存在）；False 表示仍残留。
    """
    if not os.path.exists(path):
        return True
    last_err: Exception | None = None
    for _ in range(6):  # 最多重试 ~1.5s，给 sqlite 连接关闭留时间
        try:
            shutil.rmtree(path)
            return True
        except (OSError, PermissionError) as e:
            last_err = e
            time.sleep(0.25)
    # 兜底：只删空目录（非空则留下，交给 %TEMP% sweep 二次处理）
    try:
        os.rmdir(path)
        return True
    except OSError:
        pass
    # 不静默吞错：打印一次，便于回归日志发现仍有残留
    print("[_tmpdir] 警告：目录未能删除（可能仍被连接占用）：%s (%s)"
          % (path, last_err))
    return False


def cleanup_all() -> None:
    """删除本进程登记的所有临时目录（atexit 调用）。"""
    with _LOCK:
        tracked = list(_TRACKED)
        _TRACKED.clear()
    for d in tracked:
        safe_rmtree(d)


def sweep_temp(temp_root: str | None = None, dry_run: bool = False) -> dict:
    """回归级兜底：扫 %TEMP% 下命中门禁前缀白名单的残留目录，删之。

    :param temp_root: 扫描根（默认系统 %TEMP%）
    :param dry_run:   只统计不删
    :returns: {"scanned":int,"removed":int,"kept":int,"bytes_freed":int}
    """
    root = temp_root or tempfile.gettempdir()
    stat = {"scanned": 0, "removed": 0, "kept": 0, "bytes_freed": 0}
    if not os.path.isdir(root):
        return stat
    for name in os.listdir(root):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if not any(name.startswith(p) for p in SWEEP_PREFIXES):
            continue
        stat["scanned"] += 1
        # 安全条件：含 .db 文件，或为空目录；都不满足则跳过（防误删用户目录）
        has_db = any(f.endswith(".db") for _, _, fs in os.walk(full) for f in fs)
        is_empty = not any(os.scandir(full))
        if not (has_db or is_empty):
            stat["kept"] += 1
            continue
        if dry_run:
            continue
        before = _dir_size(full)
        if safe_rmtree(full):
            stat["removed"] += 1
            stat["bytes_freed"] += before
    return stat


def _dir_size(path: str) -> int:
    total = 0
    for _, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(path, f))
            except OSError:
                pass
    return total


if __name__ == "__main__":
    import json
    import sys
    _dry = "--dry" in sys.argv
    print(json.dumps(sweep_temp(dry_run=_dry), ensure_ascii=False, indent=2))
