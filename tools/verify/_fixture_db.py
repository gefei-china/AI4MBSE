# -*- coding: utf-8 -*-
"""门禁**夹具库**统一入口：从历史备份或当前库取一份**一致快照**。

## 为什么需要它（2026-11-26 实测）

三个门禁（`verify_role_manual_filter` / `verify_mcp_whitelist_exempt` /
`verify_semantic_cache_stream`）在模块级硬编码了同一个夹具来源：

    shutil.copy2(os.path.join(ROOT, "backups", "t-prod-20261026-122709.db"), TMP)

那个备份文件**已被清理**（`backups/` 现存 137 个文件，全是 .json 与 `mbse.db.bak-*`，
没有 `t-prod-*`）⇒ 三个门禁**在 import 阶段就 FileNotFoundError 崩掉**
（0.6~2.7s 失败，Traceback 指向 `shutil.py:453 copy2`）。

这类失败的读法是"环境缺文件"，但它真正教给我们的是：
**门禁把"某一次手工备份"当成了永久夹具** —— 备份是运维临时产物、随时会被清理，
而门禁要长期存在。两者生命周期不匹配 ⇒ 迟早崩。

## 口径（三层回退，每层都有明确理由）

  ① `prefer` 指向的历史备份若**仍存在** ⇒ 用它。
     保留原始意图：有些判据要复现"那个时刻的数据形态"，换成当前库可能变弱。
  ② 否则用**当前库**（`MBSE_DB_PATH` → 其次仓库根 `mbse.db`）。
     判据多数是"某类 Agent/工具在当前数据上是否被正确处理"，当前库同样成立。
  ③ 两者都没有 ⇒ 返回 `None`，**调用方必须 SKIP 而不是崩**。
     干净克隆（CI）上 `mbse.db` 不入库 ⇒ 这是正常状态，不是缺陷。

## 为什么用 backup API 而不是 copyfile

服务在跑时会持续写 WAL：直接拷主文件会拿到**不一致快照**
（实测子进程报 `malformed database schema (v_memory_notes)`）。
`sqlite3.Connection.backup()` 是 SQLite 官方的在线一致快照，源库在被写也能拿到自洽副本。
"""
import os
import sqlite3


def _repo_root(start):
    d = os.path.dirname(os.path.abspath(start))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "tools", "verify")):
            return d
        p = os.path.dirname(d)
        if p == d:
            break
        d = p
    return os.path.dirname(os.path.abspath(start))


def _snapshot(src, dest):
    """用 backup API 把 src 复制成 dest（dest 已存在则覆盖内容）。返回是否成功。"""
    try:
        s = sqlite3.connect("file:%s?mode=ro" % str(src).replace("\\", "/"), uri=True)
        try:
            d = sqlite3.connect(dest)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        return True
    except Exception:
        return False


def fixture_db(dest, prefer="t-prod-20261026-122709.db", start=None):
    """准备夹具库到 `dest`，返回实际来源路径；无可用来源时返回 None（调用方 SKIP）。

    参数：
      dest   —— 目标路径（会被覆盖写入）
      prefer —— `backups/` 下的历史备份文件名（存在则优先）
      start  —— 用于定位仓库根的路径（默认本文件；脚本可传 `__file__`）
    """
    root = _repo_root(start or __file__)
    cands = []
    if prefer:
        cands.append(os.path.join(root, "backups", prefer))
    env = os.environ.get("MBSE_DB_PATH")
    if env:
        cands.append(env)
    cands.append(os.path.join(root, "mbse.db"))
    for src in cands:
        if not src or not os.path.exists(src):
            continue
        if _snapshot(src, dest):
            return src
    return None


def require_or_skip(dest, prefer="t-prod-20261026-122709.db", start=None, label="夹具库"):
    """同 `fixture_db`，但无来源时**打印 SKIP 并以 0 退出**（门禁不该因环境缺失而红）。"""
    src = fixture_db(dest, prefer=prefer, start=start)
    if src is None:
        print("[SKIP] %s：既无历史备份也无当前库（干净克隆属正常状态）⇒ 判据面不存在"
              % label)
        raise SystemExit(0)
    return src
