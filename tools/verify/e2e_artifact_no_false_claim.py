# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：真机端到端，需 8000 服务 + 真实 LLM（编排判定与产码），约 3~8 分钟且产生费用，CI 无服务无 key；本机已实测通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""真机端到端：**报告不得虚报「产物未落库」**（P0 + P1 的判据）。

## 背景（真机 conv 990730/990731/990734 三连实证）
产物库里每轮都有 3 条（SysML 视图 ×2 + 代码 v0.1），但最终报告写了 7 处
「产物保存工具缺失 / 未完成产物落库 / 无 artifact id」，报告与产物面板**自相矛盾**。
根因两条，各对应一处修复：
  · P0 —— 产物自动归档的**事实说明**只在 `tool_rules` 里，而 execute（非流式）路径
          `include_tool_rules=False` ⇒ 模型根本不知道正文代码块会自动归档；
  · P1 —— 汇总阶段看不到 `artifacts` 表 ⇒ 子任务自述"没保存"被当成事实写进报告。

## 判据（全部落在**可复算的事实**上）
  R1 报告**不得**出现「未落库 / 无 artifact id / 保存工具缺失」这类虚报
  R2 该会话 `artifacts` 表**确实有** code 产物（否则 R1 无从谈起，说明真没生成）
  R3 产物文件的 SysML 代码**通过官方校验器**（rc=0）—— 产物不是空壳
  R4 若报告提到产物，措辞应是"已保存/已归档/产物面板"这类**正面**表述

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/e2e_artifact_no_false_claim.py
"""
import json
import os
import re
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

BASE = "http://127.0.0.1:8000"
HDR = {"Content-Type": "application/json", "X-User-Id": "111"}
MSG = ("为混动汽车建立顶层用例视图，先拆解顶层用例 SeriesHybridDrivingScenario 与"
       "子用例 VehicleReady、DriveCycle，再生成对应的 SysML v2 用例视图代码。")

PASS, FAIL = 0, 0
ROWS = []

#: 虚报措辞（真机报告里的原话片段）
FALSE_CLAIMS = (
    "未完成产物落库", "无 artifact id", "artifact id、路径或 URL",
    "save_artifact` 不在", "save_artifact 不在", "保存工具缺失",
    "未完成平台落库", "尚未真正落库", "无法给出 artifact",
)
#: 正面措辞（至少出现一个，才算"如实告知"）
POSITIVE = ("产物面板", "已归档", "已保存", "自动归档", "产物库")


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:300]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:400]})


def post(path, body, timeout=1800):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers=HDR, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main():
    print("=" * 78)
    print("真机端到端：报告不得虚报「产物未落库」（P0+P1）")
    print("=" * 78)

    try:
        with urllib.request.urlopen(BASE + "/api/dashboard", timeout=8) as r:
            check("S0 dashboard=200", r.status == 200, r.status)
    except Exception as e:  # noqa: BLE001
        print("SKIP: 服务不可达（%r）" % e)
        return 2

    st, body = post("/api/conversations", {"title": "产物如实性验证-%d" % int(time.time())})
    cid = int((json.loads(body) or {}).get("id") or 0)
    check("S1 建会话成功", bool(cid), body[:200])
    if not cid:
        return 2
    print("     会话 id = %d" % cid)

    t0 = time.time()
    try:
        st, raw = post("/api/conversations/%d/chat" % cid, {"message": MSG})
    except Exception as e:  # noqa: BLE001
        raw, st = json.dumps({"__error__": repr(e)}), -1
    print("     HTTP=%s 耗时 %.1fs 响应 %d 字节" % (st, time.time() - t0, len(raw)))
    with open("outputs/_e2e_artifact_resp.json", "w", encoding="utf-8") as f:
        f.write(raw)
    check("S2 请求成功返回", st == 200, raw[:200])

    resp = json.loads(raw) if raw.strip().startswith("{") else {}
    content = str(resp.get("content") or "")
    print("     最终正文 %d 字符" % len(content))

    # ── R1 不得虚报 ──
    print("\n=== R1 报告不得虚报「未落库」===")
    hits = [w for w in FALSE_CLAIMS if w in content]
    check("R1 报告无虚报措辞（未落库/无 artifact id/工具缺失）", not hits,
          "命中：%s" % hits)
    if hits:
        for w in hits[:3]:
            i = content.find(w)
            print("       命中片段：…%s…" % content[max(0, i - 60):i + 60].replace("\n", " "))
    check("R1b 报告有正面表述（产物面板/已归档/已保存）",
          any(w in content for w in POSITIVE),
          "未见任何正面措辞 ⇒ 用户仍不知道产物在哪")

    # ── R2 产物确实存在 ──
    print("\n=== R2 产物确实已归档 ===")
    from database import get_db
    c = get_db()
    try:
        arts = [dict(r) for r in c.execute(
            "SELECT id, kind, title, filename, meta, "
            "       length(COALESCE(preview_content,'')) AS clen "
            "FROM artifacts WHERE conversation_id=? ORDER BY id", (cid,))]
    finally:
        c.close()
    print("     本会话产物 %d 条：" % len(arts))
    for a in arts:
        print("        #%s [%s] %s" % (a["id"], a["kind"], a["title"][:56]))
    code_arts = [a for a in arts if a["kind"] == "code"]
    check("R2 至少 1 条 code 产物（否则说明真没生成，R1 无从谈起）",
          len(code_arts) >= 1, len(arts))

    # ── R3 产物代码合法 ──
    print("\n=== R3 产物不是空壳（官方校验器）===")
    if code_arts:
        from database import get_db as _g
        cc = _g()
        try:
            row = cc.execute("SELECT preview_content FROM artifacts WHERE id=?",
                             (code_arts[-1]["id"],)).fetchone()
        finally:
            cc.close()
        code = str((row["preview_content"] if row else "") or "")
        try:
            import sysml_v2_check as _chk
            res = _chk.check_code(code, use_cache=False)
            rc = res.get("rc")
            nerr = res.get("n_error")
        except Exception as e:  # noqa: BLE001
            rc, nerr = -1, str(e)[:80]
        print("     代码 %d 字符 → rc=%s ERROR=%s" % (len(code), rc, nerr))
        check("R3 产物代码通过官方校验器（rc=0）", rc == 0, "rc=%s n_error=%s" % (rc, nerr))
    else:
        check("R3 产物代码通过官方校验器", False, "无 code 产物")

    # ── R4 版本号递增标记存在 ──
    print("\n=== R4 版本标记 ===")
    _vers = []
    for a in code_arts:
        try:
            m = json.loads(a["meta"] or "{}")
        except Exception:
            m = {}
        _vers.append(str(m.get("version_label") or ""))
    check("R4 code 产物带版本标记（vX.Y）",
          all(re.fullmatch(r"v\d+\.\d+", v or "") for v in _vers) if _vers else False,
          _vers)

    print("\n" + "=" * 78)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    print("会话 id=%d（保留供人工核对）" % cid)
    with open("outputs/_e2e_artifact_no_false_claim.json", "w", encoding="utf-8") as f:
        json.dump({"conv": cid, "pass": PASS, "fail": FAIL,
                   "false_claims": hits, "positives": [w for w in POSITIVE if w in content],
                   "artifacts": [{k: a[k] for k in ("id", "kind", "title")} for a in arts],
                   "rows": ROWS}, f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
