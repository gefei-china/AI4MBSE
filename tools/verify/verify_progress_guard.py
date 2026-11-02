#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_progress_guard.py —— 无进展护闸门禁（2026-11-02，全链路实测发现）。

## 事故现场（conv 10004，A 组真实编排实测）

t1 单子任务：`graph_retrieve` 被调 **86 次**，`chat_probe` 的 prompt_tokens
12,664 → **40,240** 单调增长，`finish_reason` 恒为 `tool_calls`，4 分钟未收敛。

## 本门禁守什么（四层，全部可证伪）

A 判据层：`ProgressTracker` 的边际新颖度判定
  A1 真探索（每次都有新内容）⇒ 不停（不能砍掉有效检索）
  A2 连续零新增 ⇒ 在合理位置停
  A3 中途"换方向拿到新内容" ⇒ 计数清零（这是真库第 65 次的真实形态）
  A4 空结果不参与"重复"判定（空≠重复），但计入硬上限兜底
  A5 完全相同结果 ⇒ 立即计入低新颖（补充判据）

B 真库回放（标定，不是合成数据）
  B1 事故数据 conv 10004：86 次 → 在第 12~30 次之间停下（省 ≥65%）
  B2 正常任务（真库里 9~12 次的会话）⇒ **不被停**（误杀反证，最关键）

C 挂钩层：两条 ReAct 路径都要接线，且顺序不能破坏工具协议
  C1 stream.py 有 tracker 实例 + 判定 + 收尾提示
  C2 收尾提示**必须在 `messages.extend(results)` 之后**（否则
     `assistant(tool_calls) → tool` 配对断裂 ⇒ HTTP 400）
  C3 可配置开关（默认开）

D 变异自证：把阈值改成"永不触发"/"过松"，B1 必须变红

运行：python tools/verify/verify_progress_guard.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.loop_progress import (  # noqa: E402
    HARD_CAP_PER_TOOL, NOVELTY_MIN, STREAK_STOP, ProgressTracker, shingles)

_R = []


def ck(name, ok, detail=""):
    _R.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


def _txt(tag, n=60):
    """合成"内容不同"的结果：随机组合词，保证 shingle 集合不重叠。"""
    return ("【结果%d】" % tag) + "".join("条款%d说明" % (tag * 100 + i) for i in range(n))


def replay(results, **kw):
    """按给定结果序列回放，返回 (停止位置(1-based) 或 None, tracker)。"""
    tr = ProgressTracker(**kw)
    for i, r in enumerate(results):
        v = tr.observe("graph_retrieve", r)
        if v["stop"]:
            return i + 1, tr
    return None, tr


def main():
    print("=" * 92)
    print("无进展护闸门禁（判据 = 边际新颖度）")
    print("=" * 92)

    # ── A 判据层 ──
    stop_at, _ = replay([_txt(i) for i in range(1, 13)])
    ck("A1 真探索（12 次内容都不同）⇒ 不停（不砍有效检索）",
       stop_at is None, f"stop_at={stop_at}")

    same = _txt(7)
    stop_at, tr = replay([_txt(1), _txt(2), same, same, same, same, same])
    # ⚠️ 期望值推导（别写成固定小数）：`same` 的**首次出现**是新内容，不计重复 ⇒
    #   前 3 次（2 个真探索 + 1 个首发）都 novel，从第 4 次起才累计 streak，
    #   连续 STREAK_STOP 次后停 ⇒ stop_at = 3 + STREAK_STOP = 7。
    #   第一版本断言写成 `<=6`（把首发也当重复），是**断言错**不是产品错。
    ck("A2 连续零新增 ⇒ 停止（首发不算重复，故停点 = 3 + 阈值）",
       stop_at == 3 + STREAK_STOP, f"stop_at={stop_at} expect={3 + STREAK_STOP}")

    # 先 3 次真探索，再 5 次同一结果 ⇒ 应在新颖度触发处停，且不早于真探索段
    seq = [_txt(1), _txt(2), _txt(3)] + [same] * 6
    stop_at, _ = replay(seq)
    ck("A3 停点落在「零新增段」内（不误伤前面 3 次真探索）",
       stop_at is not None and stop_at >= 4 + STREAK_STOP - 1, f"stop_at={stop_at}")

    # 换方向：连续零新增 3 次（未达阈值），然后来一批新内容 ⇒ 计数清零、不停
    seq2 = [_txt(1), same, same, same, _txt(9), _txt(10)]
    stop_at, tr2 = replay(seq2)
    ck("A4 中途换方向拿到新内容 ⇒ 计数清零（真库第 65 次的真实形态）",
       stop_at is None, f"stop_at={stop_at} streak={tr2.streak.get('graph_retrieve')}")

    # 空结果：不参与重复判定，但超硬上限要停
    tr3 = ProgressTracker(hard_cap=3)
    vs = [tr3.observe("t", "") for _ in range(4)]
    ck("A5 空结果不计「重复」，但超硬上限停（空 ≠ 重复）",
       all(not v["stop"] for v in vs[:3]) and vs[3]["stop"]
       and vs[3]["stop_reason"] == "tool_call_cap",
       f"stops={[v['stop'] for v in vs]}")

    tr4 = ProgressTracker()
    big = "长文本" * 2000
    v1 = tr4.observe("t", big)
    v2 = tr4.observe("t", big)
    ck("A6 完全相同结果 ⇒ 立即判为无新增（补充判据）",
       v1["novel"] and not v2["novel"], f"ratio={v2['ratio']} sim={v2['sim']}")

    ck("A7 阈值/次数/硬上限与文档标定值一致（防被悄悄改宽）",
       abs(NOVELTY_MIN - 0.05) < 1e-9 and STREAK_STOP == 4 and HARD_CAP_PER_TOOL == 30,
       f"{NOVELTY_MIN}/{STREAK_STOP}/{HARD_CAP_PER_TOOL}")

    # ── B 真库回放 ──
    try:
        from database import get_db
        conn = get_db()
        try:
            inc = [r[0] or "" for r in conn.execute(
                "SELECT result FROM tool_call_logs WHERE conversation_id=10004 "
                "AND tool_name='graph_retrieve' ORDER BY id").fetchall()]
            norm = []
            for r in conn.execute(
                    "SELECT conversation_id, COUNT(*) n FROM tool_call_logs "
                    "WHERE tool_name='graph_retrieve' AND conversation_id NOT IN (0,10004) "
                    "AND conversation_id>0 GROUP BY conversation_id HAVING n BETWEEN 8 AND 14 "
                    "ORDER BY n DESC LIMIT 3").fetchall():
                arr = [x[0] or "" for x in conn.execute(
                    "SELECT result FROM tool_call_logs WHERE conversation_id=? "
                    "AND tool_name='graph_retrieve' ORDER BY id", (r[0],)).fetchall()]
                norm.append((r[0], arr))
        finally:
            conn.close()

        if inc:
            stop_at, tr = replay(inc)
            ck("B1 事故数据回放：86 次 → 在 %d~%d 次之间停下" % (12, 30),
               stop_at is not None and 12 <= stop_at <= 30,
               f"calls={len(inc)} stop_at={stop_at} 省={0 if not stop_at else round(100*(1-stop_at/len(inc)))}%")
        else:
            ck("B1 事故数据回放（数据缺失，需先跑 e2e_orch_context_ab）", False, "conv 10004 无记录")

        if norm:
            killed = []
            for cid, arr in norm:
                sa, _ = replay(arr)
                if sa is not None:
                    killed.append((cid, len(arr), sa))
            ck("B2 ★正常任务不被误杀（真库 8~14 次检索的会话）",
               not killed, f"被误停={killed}" if killed else f"检查 {len(norm)} 个会话，全部不停")
        else:
            ck("B2 正常任务误杀反证（无样本，跳过）", True, "真库暂无 8~14 次的会话")
    except Exception as e:
        ck("B 真库回放可执行", False, f"{type(e).__name__}: {e}")

    # ── C 挂钩层 ──
    src = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
    ck("C1 stream.py 已接 tracker（实例化 + 判定 + 收尾）",
       "ProgressTracker" in src and "_pg.observe(" in src and "_pg_stop_now" in src)
    i_backfill = src.find("messages.extend(results)")
    i_stopmsg = src.find("【系统】本子任务已触发**无进展护闸**")
    ck("C2 ★收尾提示在 `messages.extend(results)` **之后**（否则工具协议配对断裂）",
       i_backfill > 0 and i_stopmsg > i_backfill,
       f"backfill@{i_backfill} stopmsg@{i_stopmsg}")
    ck("C3 有独立开关且默认开（可回退）",
       "progress_guard_enabled" in src and "progress_novelty_min" in src)
    src_cfg = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    ck("C4 四个配置键都进 schema（面板可见、可调）",
       all(k in src_cfg for k in ("progress_guard_enabled", "progress_novelty_min",
                                  "progress_streak_stop", "progress_hard_cap_per_tool")))

    # ── D 变异自证 ──
    if inc:
        # ★ 变异设计要点：必须**隔离变量**。
        #   第一版只把 novelty_min 设 0，结果停点从 23 只挪到 25（差 2）——
        #   因为「完全相同」补充判据仍在起作用，两条判据互相覆盖，
        #   自证因此**证明不了**主判据承重。正确做法：先关掉 exact 判据
        #   （dup_sim=2.0，相似度永达不到），再单独开关主判据。
        sa_nodup, _ = replay(inc, dup_sim=2.0)
        ck("D1a 只留主判据（关掉 exact 补充判据）⇒ 仍能在事故数据上停止",
           sa_nodup is not None and 12 <= sa_nodup <= 30,
           f"stop_at={sa_nodup}（说明主判据独立可用）")
        sa_off, _ = replay(inc, dup_sim=2.0, novelty_min=0.0, hard_cap=9999)
        ck("D1b ★三条机制全关 ⇒ 事故数据**不再停止**（86 次全部执行 = 事故原貌）",
           sa_off is None, f"stop_at={sa_off}")
        # D1d：只留硬上限 ⇒ 应在第 31 次停（hard_cap=30）。这条单独证明
        #   「绝对兜底」在软判据全失效时仍能拦住狂调 —— 三条机制各司其职。
        sa_cap, _ = replay(inc, dup_sim=2.0, novelty_min=0.0, hard_cap=30)
        ck("D1d 只留硬上限 ⇒ 第 31 次停（软判据全失效时兜底仍有效）",
           sa_cap == 31, f"stop_at={sa_cap}")
        # 对照：原样（两条判据都在）比"只留主判据"更早或持平
        ck("D1c 两条判据叠加 ⇒ 不晚于只留主判据（互补而非冲突）",
           (stop_at or 999) <= (sa_nodup or 999),
           f"叠加={stop_at} 只主判据={sa_nodup}")
        # 变异 2：阈值设 0.9 ⇒ 过度敏感（真探索也会被判无进展）
        sa_tight, _ = replay(inc, novelty_min=0.90)
        ck("D2 变异（阈值 0.9，过敏感）⇒ 比标定值更早停（说明标定值不是随手取的）",
           sa_tight is not None and sa_tight < (stop_at or 999),
           f"tight={sa_tight} 标定={stop_at}")

    fails = [r for r in _R if not r[1]]
    print("=" * 92)
    print(f"断言汇总：{len(_R) - len(fails)}/{len(_R)} 通过")
    for nm, _, dt in fails:
        print("  ✗ " + nm + (f"  | {dt}" if dt else ""))
    print("✅ ALL PASS" if not fails else "❌ 有红项")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
