# -*- coding: utf-8 -*-
"""tools/db_backup.py —— 数据库备份的**唯一正确姿势**（替代随手 shutil.copy2）。

## 为什么需要这个（2026-11-11 实测事故）

此前每次改动前都`shutil.copy2('mbse.db', 'mbse.db.bak-xxx')`，
结果是：**10 个备份堆到 3 GB，且从不清理**。更糟的是——

    ★ 它们**不是可靠备份**：`copy2` 把 `-shm` / `-wal` 旁文件也复制了，
      却只复制主库文件、不复制 WAL 内容 ⇒ 恢复到该时刻会**丢掉未 checkpoint 的事务**。
      实测那 10 个备份文件旁确实生成了 `.bak-xxx-shm/wal`（各 1 MB），
      说明 SQLite 侧把 WAL 当成了独立文件，而复制方**根本没有合并它**。

⇒ 备份这件事本身没做错，错在①用错方法（copy2 而非 sqlite3 .backup）、
  ②无保留策略（无限堆积）。本工具把两件事一次做对。

## 正确做法：`sqlite3 .backup`

官方在线备份 API，**边写边备**，产出**单一自洽文件**（含 WAL 中已提交的事务）。

## 用法

    # 备份（默认只留最近 N 份，自动清理旧的）
    ./.venv/Scripts/python.exe -X utf8 tools/db_backup.py --tag p1-3
    ./.venv/Scripts/python.exe -X utf8 tools/db_backup.py --tag binding --keep 2

    # 列出当前备份
    ./.venv/Scripts/python.exe -X utf8 tools/db_backup.py --list

    # 只清理超出保留份数的旧备份（不新建）
    ./.venv/Scripts/python.exe -X utf8 tools/db_backup.py --gc-only

## 纪律（配套）

1. **改动前先跑一次 `--tag <本次改动名>`**，标签要能看出是哪次改动
2. **保留份数默认 2**（当前改动 + 上一次），足够回滚又不堆积
3. **不要用 `cp` / `shutil.copy2` 备 SQLite**（WAL 问题，见上）
4. 备份**不纳入 git**（`.gitignore` 里确认 `mbse.db*` 已忽略）
"""
import argparse
import glob
import os
import sqlite3
import sys
import time

#: 有效备份的最小体积（生产库 374 MB；早于 1 MB 视为空壳/损坏）
MIN_VALID_BYTES = 1024 * 1024

# ★ ROOT 只退两层：tools/db_backup.py → tools → <项目根>
#   （2026-11-11 实测踩坑：首版写了三层 dirname，ROOT 落到**上一级目录**，
#    而那里恰好存在一个 0 字节的 mbse.db ⇒ 备份"成功"但产物打不开表，
#    且 sqlite3.connect 会**创建**同名假库，污染上级目录）
#   ⇒ 加上 `_assert_db_looks_real()`：不校验就会踩同一个坑。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")
BAK_DIR = os.path.join(ROOT, "backups")     # 独立目录，不再污染仓库根目录
DEFAULT_KEEP = 2


def _assert_db_looks_real(path=None):
    """启动即校验源库像个真库——**不许**对着 0 字节文件"备份"。

    为什么必须前置（实测踩坑两次）：
      ① ROOT 算错一层 ⇒ 指到别处的 0 字节 `mbse.db`；
      ② `sqlite3.connect(路径)` 会**创建**不存在的库 ⇒ 错路径会**生成假库**污染目录。
      一次失败只报"自检失败"太晚（已经写下产物），必须在**动手前**拦住。
    判据：体积 ≥ 1 MB 且含 `agents` 表（生产库 374 MB / 29 个 Agent）。
    """
    p = path or DB
    try:
        if not os.path.exists(p):
            print("★ 源库不存在：%s" % p)
            return False
        sz = os.path.getsize(p)
        if sz < MIN_VALID_BYTES:
            print("★ 源库仅 %d 字节（%s）—— 多半是 ROOT 算错或空壳，拒绝备份"
                  % (sz, p))
            print("  预期位置：%s" % os.path.join(ROOT, "mbse.db"))
            return False
        c = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        try:
            n = c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='agents'"
            ).fetchone()[0]
        finally:
            c.close()
        if not n:
            print("★ 源库缺 agents 表（%s）—— 不是可用的业务库" % p)
            return False
        return True
    except Exception as e:                          # noqa: BLE001
        print("★ 源库校验异常：%s" % e)
        return False

#: 识别"旧方式的遗留备份"（根目录的 mbse.db.bak*），用于 --gc-legacy
_LEGACY_GLOB = os.path.join(ROOT, "mbse.db.bak*")


def _fmt(mb):
    return "%7.1f MB" % mb


def list_backups():
    """只列**有效**备份（≥ MIN_VALID_BYTES）。

    ★ 为什么必须过滤（2026-11-11 实测）：首版不设下限时，
      `sqlite3.connect(dst)` 创建的 0 字节空壳被 `gc()` 当成"备份"计入保留份数
      ⇒ 连着跑两次失败后，**真正该留的旧备份被当过期清掉**，
      而磁盘上只剩几个啥都不是的空文件。
      ⇒ **空壳不仅没用，还会挤掉真备份的保留名额**。
    """
    rows = []
    for f in glob.glob(os.path.join(BAK_DIR, "mbse-*.db")):
        try:
            if os.path.getsize(f) >= MIN_VALID_BYTES:
                rows.append((os.path.getmtime(f), f))
        except OSError:
            continue
    rows.sort(reverse=True)
    return rows


def sweep_empty(verbose=True):
    """清理 0 字节 / 过小的**空壳备份**（与保留策略分开，先清壳再谈保留）。"""
    n = freed = 0
    for f in glob.glob(os.path.join(BAK_DIR, "mbse-*.db")):
        try:
            sz = os.path.getsize(f)
        except OSError:
            continue
        if sz >= MIN_VALID_BYTES:
            continue
        try:
            os.remove(f)
            n += 1
            freed += sz
            if verbose:
                print("  已删空壳 %s（%d 字节）" % (os.path.basename(f), sz))
        except Exception as e:                      # noqa: BLE001
            print("  ★清理失败 %s：%s" % (f, e))
    return n, freed


def list_legacy():
    return sorted(glob.glob(_LEGACY_GLOB))


def gc(keep=DEFAULT_KEEP, verbose=True):
    """按保留份数清理（新式备份）。**先清空壳，再按份数**。返回回收字节数。"""
    if verbose:
        sweep_empty(verbose)
    rows = list_backups()
    if len(rows) <= keep:
        if verbose:
            print("  有效备份 %d 份（≤ 保留 %d 份），无需清理" % (len(rows), keep))
        return 0
    freed = 0
    for _mt, f in rows[keep:]:
        sz = os.path.getsize(f)
        try:
            os.remove(f)
            freed += sz
            if verbose:
                print("  已删旧备份 %s（%s）" % (os.path.basename(f), _fmt(sz / 1048576)))
        except Exception as e:                      # noqa: BLE001
            print("  ★清理失败 %s：%s" % (f, e))
    if verbose:
        print("  回收 %s" % _fmt(freed / 1048576))
    return freed


def gc_legacy(keep_newest=0, verbose=True):
    """清理**旧方式**（根目录 shutil.copy2）的遗留备份。

    默认 `keep_newest=0` = 全删。调用方需自己确认（这些是不可靠备份）。
    返回回收字节数。
    """
    files = list_legacy()
    if not files:
        return 0
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    if keep_newest:
        files = files[keep_newest:]
    freed = 0
    for f in files:
        sz = os.path.getsize(f)
        try:
            os.remove(f)
            freed += sz
            if verbose:
                print("  已删旧式备份 %s（%s）" % (os.path.basename(f), _fmt(sz / 1048576)))
        except Exception as e:                      # noqa: BLE001
            print("  ★清理失败 %s：%s" % (f, e))
    return freed


def backup(tag="manual", keep=DEFAULT_KEEP):
    """用 `sqlite3 .backup` 做**在线一致性备份**（关键：不能用 copy2）。

    ★★ 源连接必须用**只读**打开（2026-11-11 实测踩坑）：
      服务运行中（WAL 持有者，PID 存活）时，`sqlite3.connect(DB)` 写连接
      **读不到 WAL 中已提交但未 checkpoint 的事务** ⇒ 实测直接报
      `no such table: agents`（生产库明明有 29 个 Agent）。
      原因：写连接以「主库文件当前内容」为准，而 agents 表的增量还在 1 MB 的
      `-wal` 里；只读连接才会把 WAL 合并进来。
      ⇒ 结论：**不停服也能备份，但必须用 `mode=ro` 打开源库**。
      这也正是「copy2 不可靠」的机理：它只复制主库文件、WAL 没跟着走。
    """
    os.makedirs(BAK_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = "mbse-%s-%s.db" % (tag, ts)
    dst = os.path.join(BAK_DIR, name)
    # ★ 失败时**不留空壳**（2026-11-11 实测踩坑）：
    #   `sqlite3.connect(dst)` 会**立刻创建 0 字节文件**，若随后 backup/自检抛错，
    #   磁盘上就留下一个"看起来像备份、其实啥都不是"的空文件。
    #   首版因此连续产出 3 个 0.0 MB 的"备份"，而 `gc()` 还真把它们当备份计入保留数
    #   ⇒ 真正有用的备份被当旧备份清掉。**空壳比没备份更危险**（它占保留名额）。
    #   修法：① 先确保目标不存在 ② 任何异常都删掉半成品再抛。
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except Exception as e:                      # noqa: BLE001
            print("★ 目标已存在且无法删除：%s" % e)
            return None
    # ★ 源用只读（mode=ro）⇒ 能读到 WAL 内容；写连接会漏数据
    #   （服务运行中时 agents 等表的增量还在 1 MB 的 -wal 里，写连接读不到）
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            # ★ 只调 backup()，**不要**加 PRAGMA wal_checkpoint(TRUNCATE)（2026-11-11 实测踩坑）。
            #   `backup()` 本身会把内容**完整写入目标库**（实测 page_count 从 0 → 91468，
            #   产物 374 MB，agents 可查）。
            #   我曾"顺手"加了一句 checkpoint 收尾想让产物更紧凑，结果**把已写入的内容清掉**，
            #   产物只剩 1 个页（4096 字节）⇒ 自检报"疑似空壳"。
            #   ⇒ 教训：**备份路径上不要加任何"优化"**，backup() 写完就是最终形态。
            src.backup(dst_conn)
        finally:
            dst_conn.close()
    except Exception as e:                          # noqa: BLE001
        for p in (dst, dst + "-wal", dst + "-shm"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        print("★ 备份失败（已清理半成品）：%s" % e)
        return None
    finally:
        src.close()
    # 自证：备份可打开且有数据（"生成成功"不等于"能用"）
    # ⚠️ 这条自检不是形式主义——首版正是在这里抓到「产物 0 字节空壳」的缺陷。
    if os.path.getsize(dst) < 1024 * 1024:          # 小于 1 MB 视为失败（生产库 357 MB）
        print("★ 备份自检失败：产物仅 %d 字节（疑似空壳）" % os.path.getsize(dst))
        try:
            os.remove(dst)
        except Exception:
            pass
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
        n = c.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        ndoc = c.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        c.close()
        if n <= 0:
            print("★ 备份自检失败：agents 表为空（源库可能处于异常状态）")
            return None
    except Exception as e:                          # noqa: BLE001
        print("★ 备份自检失败（产物不可用）：%s" % e)
        return None
    print("已备份 → %s（%s，自检 agents=%d 行 / chunks=%d 行）"
          % (os.path.relpath(dst, ROOT), _fmt(os.path.getsize(dst) / 1048576), n, ndoc))
    gc(keep)
    return dst


def main():
    ap = argparse.ArgumentParser(description="数据库备份（正确姿势 + 自动保留策略）")
    ap.add_argument("--tag", default="manual", help="本次改动标签（如 p1-3 / binding）")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="保留份数（默认 2）")
    ap.add_argument("--list", action="store_true", help="列出当前备份")
    ap.add_argument("--gc-only", action="store_true", help="只清理，不新建")
    ap.add_argument("--gc-legacy", type=int, default=None, metavar="KEEP",
                    help="清理根目录旧式备份（copy2 遗留），保留最近 KEEP 份；不给则全删")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=== 数据库备份工具 ===")
    # ★ 动手前先校验源库（挡"ROOT 算错 → 备份空壳 → 污染目录"这条链）
    if args.gc_legacy is None and not _assert_db_looks_real():
        print("已中止：源库不可信，拒绝生成备份。")
        return 2
    print("  库路径：%s（%.1f MB）"
          % (DB, os.path.getsize(DB) / 1048576))
    print("  备份目录：%s" % os.path.relpath(BAK_DIR, ROOT))

    if args.gc_legacy is not None:
        print("\n[清理旧式备份 copy2 遗留]")
        freed = gc_legacy(args.gc_legacy)
        print("  合计回收 %s" % _fmt(freed / 1048576))
        return 0

    if args.list:
        rows = list_backups()
        print("\n新式备份（sqlite3 .backup，自洽可靠）%d 份：" % len(rows))
        for mt, f in rows:
            print("  %-44s %s  %s"
                  % (os.path.basename(f), _fmt(os.path.getsize(f) / 1048576),
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))))
        leg = list_legacy()
        if leg:
            print("\n⚠️ 旧式备份（copy2 遗留，**不可靠**：缺 WAL）%d 份：" % len(leg))
            for f in leg:
                print("  %-44s %s" % (os.path.basename(f),
                                      _fmt(os.path.getsize(f) / 1048576)))
            print("  清理：--gc-legacy 0")
        return 0

    if args.gc_only:
        print("\n[按保留策略清理]")
        gc(args.keep)
        return 0

    print("\n[新建备份]")
    if not backup(args.tag, args.keep):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())