# -*- coding: utf-8 -*-
"""时间基准工具：库里一律存 UTC，展示层在出口转本地。

为什么必须独立成模块（2026-11-06 execute 实跑抓到的缺陷）：
  `feedback.created_at` 的 DDL 默认是 `CURRENT_TIMESTAMP`（SQLite = **UTC**），
  而 P0 新增的 `consumed_at` 起初用 `time.strftime`（**本地**）⇒ 同一行出现
  「创建 07:25 / 消费 15:25」的错位，时间线看起来像「先消费、后创建」。
  实测证据：feedback#120 于本地 15:25:02 点踩，
           created_at=07:25:02（UTC），consumed_at=15:25:13（本地）。

纪律（门禁 verify_self_evolution_perception G11 断言）：
  1. **存储一律 UTC** —— 与既有列同源，内部按时间比较的判据（stuck / 窗口）才准；
  2. **展示一律在出口转本地** —— 新增 `*_local` 并行字段，**不覆盖原值**，
     因此重复调用幂等、也不会污染按 UTC 比较的逻辑。
"""
from datetime import datetime, timedelta, timezone


def utc_now_str() -> str:
    """当前 UTC 时间串（格式与基准都与 SQLite `CURRENT_TIMESTAMP` 一致）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_to_local_str(s) -> str:
    """UTC 时间串 → 本地时间串。无法解析时**原样返回**（不改数据、不抛）。"""
    if not s:
        return ""
    txt = str(s)[:19].replace("T", " ")
    try:
        dt = datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(s)
    # ★ 必须用本机时区偏移本身，不能写成 `now() - now(utc)`：
    #   两次 now() 之间有微秒差（实测 offset=7:59:59.999999），
    #   加到整秒时间上再 strftime 会把秒截掉 1 秒（15:25:02 → 15:25:01）。
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (dt + offset).strftime("%Y-%m-%d %H:%M:%S")


def attach_local_times(row: dict, fields=("created_at", "consumed_at", "checked_at")) -> dict:
    """给行补 `*_local` 并行字段（幂等：重复调用只是重算同一个值）。"""
    if not row:
        return row
    for k in fields:
        if k in row:
            row[k + "_local"] = utc_to_local_str(row.get(k))
    return row

def local_to_utc_sql_offset() -> str:
    """把「本地时间」转「UTC」的 SQLite 修饰符，如 UTC+8 ⇒ '-8 hours'。

    用于 `datetime(col, ?)` 一次性校正历史本地值。
    支持半小时时区（如 UTC+5:30 ⇒ '-5 hours -30 minutes'）。
    """
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    total = -int(off.total_seconds())      # 取负：本地 → UTC
    sign = "-" if total < 0 else "+"
    total = abs(total)
    hours, minutes = divmod(total // 60, 60)
    out = "%s%d hours" % (sign, hours)
    if minutes:
        out += " %s%d minutes" % (sign, minutes)
    return out
