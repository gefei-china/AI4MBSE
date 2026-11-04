# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
"""【已退役 → 转为反向哨兵】验证「旧的会话维度语义已被禁止」（2026-11-01）。

★ 本脚本的来历（读它之前必须知道）：
  2026-10-31 我按"**会话维度**"实现了会话参数（一个会话一套，不分模型）。
  2026-11-01 米爸修正：「会话中的设置是按照（会话+模型）维度来设置的，
  不是一个会话中所有模型按照同一套设置控制」
  ⇒ 正向验收已迁移到 `verify_conv_model_scoped.py`（12 项）与
    `verify_conv_model_e2e.py`（8 项，验证 payload 真的按模型分开）。

★ 那这个文件为什么还留着（而不是删掉）：
  按本仓纪律「需求被否时判据要跟着**反转**，而不是删掉那条判据」——
  删掉 = 这个维度不再受保护，下次又会有人退回"一个会话一套"。
  ⇒ 本文件现在只做**反向断言**：旧语义的任何入口都必须被拒绝。

判据：
  R1 PUT 不带 provider_id ⇒ **400**（旧语义的核心入口被堵死）
  R2 错误信息必须说清"是（会话+模型）维度"，否则使用者看不懂为什么被拒
  R3 `all_overrides` 必须是 `{provider_id: {...}}` 的桶结构，
     而不是扁平 `{thinking_level: ...}`（防止存储层偷偷退回扁平）
  R4 扁平结构只在**读**时被兼容（老数据），写后必须变成 by_model

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_conv_llm_scoped.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mbse.db")


def main():
    from fastapi.testclient import TestClient
    import main as app_main

    ok = fail = 0

    def chk(desc, cond, extra=""):
        nonlocal ok, fail
        print(("  [ok]   " if cond else "  [FAIL] ") + desc + (("  " + extra) if extra else ""))
        if cond:
            ok += 1
        else:
            fail += 1

    c = TestClient(app_main.app)
    cid = (c.post("/api/conversations", json={"title": "__legacy_guard__"}).json() or {}).get("id")
    print("\n会话=%s" % cid)

    try:
        # ── R1：不带 provider_id 必须被拒 ──
        r1 = c.put("/api/conversations/%s/llm-settings" % cid, json={"thinking_level": "关闭"})
        print("\n[R1] 旧语义调用（无 provider_id）→ HTTP %s" % r1.status_code)
        chk("R1 「一个会话一套」的写入被**拒绝**（400）", r1.status_code == 400,
            str(r1.json())[:110])

        # ── R2：错误信息要说清维度 ──
        msg = json.dumps(r1.json(), ensure_ascii=False)
        chk("R2 错误信息说明是「会话 + 模型」维度（使用者能看懂为什么被拒）",
            ("会话 + 模型" in msg) or ("会话+模型" in msg), msg[:110])

        # ── R3：存储必须是桶结构 ──
        c.put("/api/conversations/%s/llm-settings" % cid,
              json={"provider_id": 71, "thinking_level": "关闭"})
        allov = (c.get("/api/conversations/%s/llm-settings?provider_id=71" % cid).json() or {}) \
            .get("all_overrides") or {}
        print("\n[R3] all_overrides = %s" % json.dumps(allov, ensure_ascii=False)[:120])
        chk("R3 存储是 {provider_id: {...}} 桶结构，不是扁平结构",
            "71" in allov and isinstance(allov.get("71"), dict)
            and "thinking_level" not in allov.keys(), json.dumps(list(allov.keys())))

        # ── R4：老扁平数据只在读时兼容，写入必须升级 ──
        con = sqlite3.connect(DB)
        con.execute("UPDATE conversations SET llm_overrides=? WHERE id=?",
                    (json.dumps({"thinking_level": "低"}, ensure_ascii=False), cid))
        con.commit()
        con.close()
        readback = (c.get("/api/conversations/%s/llm-settings?provider_id=71" % cid).json() or {}) \
            .get("overrides") or {}
        print("\n[R4] 老扁平数据读回 = %s" % json.dumps(readback, ensure_ascii=False))
        chk("R4 老扁平数据读时兼容（升级不丢用户已做的设置）",
            readback.get("thinking_level") == "低", json.dumps(readback, ensure_ascii=False))
        c.put("/api/conversations/%s/llm-settings" % cid,
              json={"provider_id": 71, "thinking_level": "高"})
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        raw = con.execute("SELECT llm_overrides FROM conversations WHERE id=?", (cid,)).fetchone()[0]
        con.close()
        chk("R4' 写入后升级为 by_model（不再落扁平结构）", "by_model" in (raw or ""), (raw or "")[:90])

        print("\n%d 项通过，%d 项失败" % (ok, fail))
        return 1 if fail else 0
    finally:
        try:
            con = sqlite3.connect(DB)
            con.execute("DELETE FROM messages WHERE conversation_id IN "
                        "(SELECT id FROM conversations WHERE title='__legacy_guard__')")
            con.execute("DELETE FROM conversations WHERE title='__legacy_guard__'")
            con.commit()
            con.close()
            print("（已清理测试会话）")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
