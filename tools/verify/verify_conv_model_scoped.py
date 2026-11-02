"""验证「会话 × 模型」维度的参数隔离（2026-11-01，米爸第二次修正）。

米爸的原话即判据来源：
  「会话中的设置是按照（会话+模型）维度来设置的，
    不是一个会话中所有模型按照同一套设置控制」

拆成 6 条可执行判据（每条都能红）：
  M1 同会话内：给模型 A 设「关闭」，**模型 B 不受影响**（读回为空）
  M2 同会话内：A 与 B 可**各存一套**（A=关闭+8192，B=高+128K），互不覆盖
  M3 跨会话：会话1 的模型 A 设置，**不影响会话2 的同一个模型 A**
  M4 不写回：全程 `llm_providers` 整行逐字段未变
  M5 只清一个模型：`clear` 带 provider_id 只清该桶，别的模型保留
  M6 必须带 provider_id：PUT 不带它 ⇒ **400**（宁可报错也不静默按会话维度写）
  M7 老数据兼容：扁平格式（{"thinking_level":"高"}）读时按"该会话所有模型共享"处理

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_conv_model_scoped.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")
A, B = 71, 104   # 两个真实对话模型（deepseek / qwen），需要“两个不同模型”才能验隔离


def provider_snapshot(pid):
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM llm_providers WHERE id=?", (pid,)).fetchone()
    con.close()
    return dict(row) if row else {}


def main():
    from fastapi.testclient import TestClient
    import main as app_main

    before = {A: provider_snapshot(A), B: provider_snapshot(B)}
    ok = fail = 0

    def chk(desc, cond, extra=""):
        nonlocal ok, fail
        print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    c = TestClient(app_main.app)
    conv1 = (c.post("/api/conversations", json={"title": "__cm1__"}).json() or {}).get("id")
    conv2 = (c.post("/api/conversations", json={"title": "__cm2__"}).json() or {}).get("id")
    print("\n会话1=%s 会话2=%s ｜ 模型A=%s 模型B=%s" % (conv1, conv2, A, B))

    def put(cid, pid, **kw):
        body = {"provider_id": pid}
        body.update(kw)
        return c.put("/api/conversations/%s/llm-settings" % cid, json=body)

    def get(cid, pid=None):
        q = "?provider_id=%s" % pid if pid else ""
        return (c.get("/api/conversations/%s/llm-settings%s" % (cid, q)).json() or {})

    try:
        # ── M1：同会话，A 设关闭，B 不受影响 ──
        put(conv1, A, thinking_level="关闭", context_window=8192)
        oa = get(conv1, A).get("overrides") or {}
        ob = get(conv1, B).get("overrides") or {}
        print("\n[M1] A 设置后 → A=%s  B=%s" % (json.dumps(oa, ensure_ascii=False),
                                                json.dumps(ob, ensure_ascii=False)))
        chk("M1-1 A（模型%d）读回有设置" % A, oa.get("thinking_level") == "关闭" and oa.get("context_window") == 8192)
        chk("M1-2 **B（模型%d）仍为空**（不继承 A 的设置）" % B, not ob, json.dumps(ob, ensure_ascii=False))

        # ── M2：同会话，B 存一套自己的 ──
        put(conv1, B, thinking_level="高", context_window=131072)
        oa = get(conv1, A).get("overrides") or {}
        ob = get(conv1, B).get("overrides") or {}
        print("\n[M2] B 设置后 → A=%s  B=%s" % (json.dumps(oa, ensure_ascii=False),
                                                json.dumps(ob, ensure_ascii=False)))
        chk("M2-1 B 存下自己那套", ob.get("thinking_level") == "高" and ob.get("context_window") == 131072)
        chk("M2-2 **A 未被 B 覆盖**", oa.get("thinking_level") == "关闭" and oa.get("context_window") == 8192)
        allov = get(conv1, A).get("all_overrides") or {}
        chk("M2-3 同一会话里两个模型**各占一个桶**",
            str(A) in allov and str(B) in allov, json.dumps(list(allov.keys())))

        # ── M3：跨会话隔离 ──
        o2 = get(conv2, A).get("overrides") or {}
        print("\n[M3] 会话2 的模型A = %s" % json.dumps(o2, ensure_ascii=False))
        chk("M3 会话1 的 A 设置**不影响会话2 的 A**", not o2, json.dumps(o2, ensure_ascii=False))

        # ── M4：不写回 llm_providers ──
        after = {A: provider_snapshot(A), B: provider_snapshot(B)}
        diffs = {}
        for pid in (A, B):
            for k in set(before[pid]) | set(after[pid]):
                if before[pid].get(k) != after[pid].get(k):
                    diffs.setdefault(pid, {})[k] = (before[pid].get(k), after[pid].get(k))
        print("\n[M4] provider 差异：%s" % (diffs or "无"))
        chk("M4 全程**未回写** llm_providers", not diffs, str(diffs)[:160])

        # ── M5：clear 只清一个模型 ──
        r = put(conv1, A, clear=True)
        oa = get(conv1, A).get("overrides") or {}
        ob = get(conv1, B).get("overrides") or {}
        print("\n[M5] 清 A 后 → A=%s  B=%s" % (json.dumps(oa, ensure_ascii=False),
                                              json.dumps(ob, ensure_ascii=False)))
        chk("M5-1 A 已清空", not oa)
        chk("M5-2 **B 仍保留**（只清指定模型）", ob.get("thinking_level") == "高",
            json.dumps(ob, ensure_ascii=False))

        # ── M6：不带 provider_id ⇒ 400 ──
        r_bad = c.put("/api/conversations/%s/llm-settings" % conv1,
                      json={"thinking_level": "高"})
        print("\n[M6] 不带 provider_id → HTTP %s" % r_bad.status_code)
        chk("M6 缺 provider_id 时**拒绝**（400），不静默按会话维度写", r_bad.status_code == 400,
            str(r_bad.json())[:100])

        # ── M7：老扁平数据兼容 ──
        con = sqlite3.connect(DB)
        con.execute("UPDATE conversations SET llm_overrides=? WHERE id=?",
                    (json.dumps({"thinking_level": "低"}, ensure_ascii=False), conv2))
        con.commit()
        con.close()
        oa2 = get(conv2, A).get("overrides") or {}
        ob2 = get(conv2, B).get("overrides") or {}
        print("\n[M7] 老扁平数据 → A=%s  B=%s" % (json.dumps(oa2, ensure_ascii=False),
                                                 json.dumps(ob2, ensure_ascii=False)))
        chk("M7 老扁平格式读时按「所有模型共享」处理（升级不丢用户设置）",
            oa2.get("thinking_level") == "低" and ob2.get("thinking_level") == "低")
        # 首次写入应升级为 by_model
        put(conv2, A, thinking_level="高")
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        raw = con.execute("SELECT llm_overrides FROM conversations WHERE id=?", (conv2,)).fetchone()[0]
        con.close()
        chk("M7' 写入后**升级为 by_model 结构**（不再写扁平）",
            "by_model" in (raw or ""), (raw or "")[:90])

        print("\n%d 项通过，%d 项失败" % (ok, fail))
        return 1 if fail else 0
    finally:
        try:
            con = sqlite3.connect(DB)
            for t in ("__cm1__", "__cm2__"):
                con.execute("DELETE FROM messages WHERE conversation_id IN "
                            "(SELECT id FROM conversations WHERE title=?)", (t,))
                con.execute("DELETE FROM conversations WHERE title=?", (t,))
            con.commit()
            con.close()
            print("（已清理测试会话）")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
