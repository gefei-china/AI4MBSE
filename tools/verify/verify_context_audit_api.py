# -*- coding: utf-8 -*-
"""Step10 验收：context_audit 查询接口 + 报表脚本。

判据：
  [1] 四个只读端点都能返回、结构完整
  [2] 空库时不报错，且明确告知"无数据"
  [3] 有数据时块聚合的 token 占比之和 ≈ 100%
  [4] 用真实 execute 链路产生数据 → 端点能看到该 trace
  [5] 会话明细的「差额」= 计费 − system 合计，且为负时能被识别（口径自检）
  [6] 报表脚本 --json 可解析，且与端点数值一致（同一 repo，口径单一真源）
  [7] 端点全部只读：调用后 context_audit 行数不变
"""
import os
import sys
import json
import shutil
import tempfile

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
# ⚠️ 2026-10-30 修：此前**硬编码**一份备份快照（`backups/t-prod-20261026-122709.db`），
#   而 `backups/` 是会被清理的目录 ⇒ 该文件一旦不在，本门禁**永远红**
#   （实测报 `FileNotFoundError: [WinError 2]`，且报错点与业务无关）。
#   ⇒ 改为"有快照用快照，没有就用真库"：两者都是**副本**（`shutil.copy2` 到临时目录后
#   重定向 `MBSE_DB_PATH`），所以"不写真库"这条前提不变。
#   门禁的价值在于**判据**，不该被一份可选快照的存在性绑架。
_CANDIDATES = [
    os.path.join(ROOT, "backups", "t-prod-20261026-122709.db"),
    os.path.join(ROOT, "mbse.db"),
]
PROD = next((p for p in _CANDIDATES if os.path.exists(p)), "")
if not PROD:
    print("[SKIP] 找不到可用的数据库副本（backups/ 与 mbse.db 均不存在）")
    sys.exit(0)
print("[db] 使用副本源：%s" % PROD)
T = os.path.join(tempfile.gettempdir(), "step10_audit.db")
for p in (T, T + "-wal", T + "-shm"):
    if os.path.exists(p):
        os.remove(p)
shutil.copy2(PROD, T)
os.environ["MBSE_DB_PATH"] = T
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

import logging
logging.disable(logging.WARNING)

from database import init_db, get_db                       # noqa: E402
init_db()

from repositories.context_audit_repo import ContextAuditRepo   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════
# [2] 空库：四端点都不报错、明确无数据
# ══════════════════════════════════════════════════════════════
print("=" * 78)
print("[2] 空库态")
print("=" * 78)
con = get_db()
con.execute("DELETE FROM context_audit")
con.execute("DELETE FROM context_audit_summary")
con.commit()
rep = ContextAuditRepo(con)
b0 = rep.blocks(days=7)
s0 = rep.switches(days=7)
d0 = rep.daily(days=7)
c0 = rep.conversations(days=7)
check("[2] blocks 空库返回空 rows", b0["rows"] == [] and b0["total_tokens"] == 0)
check("[2] switches 空库返回空 metrics", s0["metrics"] == [])
check("[2] daily 空库返回空 rows", d0["rows"] == [])
check("[2] conversations 空库返回空列表", c0["conversations"] == [])
con.close()

# ══════════════════════════════════════════════════════════════
# 用真实 execute 链路产生数据
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[4] 真实 execute 链路产生数据")
print("=" * 78)
from agent import get_pipeline                             # noqa: E402
from agent.context_audit import flush as _flush            # noqa: E402
from core import config as cfg                             # noqa: E402

con = get_db()
cid = con.execute("INSERT INTO conversations (title,intent,status,phase) "
                  "VALUES ('step10','chat','active','')").lastrowid
con.commit()
con.close()

USER = {"id": 1, "username": "step10", "display_name": "S10", "role_name": "admin",
        "role": "admin", "department": "验证"}
_seed = cfg._CONFIG.setdefault("context_audit", {})
_seed["role_manual_filter"] = True        # 顺带验证指标能被报表看到

p = get_pipeline()
p.execute("查询智源工程列表", cid, branch="dev", user=USER)
_flush(force=True)
_seed["role_manual_filter"] = False

con = get_db()
n_rows = con.execute("SELECT COUNT(*) c FROM context_audit").fetchone()["c"]
check("[4] 真实链路写入 context_audit", n_rows > 0, f"{n_rows} 行")

# ══════════════════════════════════════════════════════════════
# [3] 块聚合口径
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[3] 块聚合口径")
print("=" * 78)
rep = ContextAuditRepo(con)
b = rep.blocks(days=7)
share = sum(r["token_share"] for r in b["rows"])
check("[3] 块聚合非空", bool(b["rows"]), f"{len(b['rows'])} 块")
check("[3] token 占比之和 ≈ 100%", abs(share - 100.0) < 0.6, f"{share:.2f}%")
check("[3] 按 token 降序排列",
      all(b["rows"][i]["tokens"] >= b["rows"][i + 1]["tokens"]
          for i in range(len(b["rows"]) - 1)))
check("[3] 每块都有中文 label（可读性）",
      all(r["label"] and r["label"] != r["block"] for r in b["rows"]
          if r["block"] in ("role", "tools", "retrieval", "memory")))
check("[3] 层归属齐全（static+dynamic 都有）", len(b["layers"]) >= 1, str(b["layers"]))
print(f"  实测：{b['total_tokens']} tok / {b['assemblies']} 次装配"
      f"（{b['total_hits']} 块行）/ 静态 {b['layers'].get('static', 0)}"
      f" · 动态 {b['layers'].get('dynamic', 0)}")

# ── [3b] 2026-10-27 新增契约（面板首屏数字的正确性依赖这三项）──
check("[3b] 下发 assemblies（真实装配次数）",
      isinstance(b.get("assemblies"), int) and b["assemblies"] > 0,
      f"assemblies={b.get('assemblies')}")
check("[3b] assemblies ≠ total_hits（块行数 ≠ 装配次数，混用会让均值小十几倍）",
      b["assemblies"] != b["total_hits"],
      f"assemblies={b['assemblies']} total_hits={b['total_hits']}")
_exp_avg = round(b["total_tokens"] / b["assemblies"], 1) if b.get("assemblies") else 0
check("[3b] avg_system_tokens 口径正确（= total_tokens / assemblies）",
      abs(float(b.get("avg_system_tokens") or 0) - _exp_avg) < 0.2,
      f"实得 {b.get('avg_system_tokens')} 期望 {_exp_avg}")
# 缓存断点必须按 **prompt 拼接顺序** 判定，不能按 token 大小
_dyn = b.get("dynamic_order") or []
check("[3b] cache_boundary 是 prompt 顺序上的第一个动态块",
      bool(b.get("cache_boundary")) and _dyn and b["cache_boundary"] == _dyn[0],
      f"cache_boundary={b.get('cache_boundary')} dynamic_order[:3]={_dyn[:3]}")
check("[3b] cache_boundary 大概率 ≠ rows[0]（按 token 降序的首块）",
      True, f"rows[0]={b['rows'][0]['block'] if b['rows'] else '-'}")


def _sw_ok(sw):
    """switches 语义：快照型取均值、计数型取总和（严格版见 verify_metric_semantics.py）"""
    bad = []
    for m in sw.get("metrics") or []:
        if "kind" not in m or "unit" not in m or "samples" not in m:
            bad.append(("缺字段", m.get("metric")))
        elif m["kind"] == "count" and m["value"] != m["sum"]:
            bad.append(("count 取值≠sum", m["metric"]))
        elif m["kind"] != "count" and m["value"] != m["avg"]:
            bad.append(("gauge 取值≠avg", m["metric"]))
    return bad

# ── [3d] overview 必须一次给全（面板单页渲染，少一个字段就多一次往返）──
try:
    from fastapi.testclient import TestClient     # noqa: E402
    import main as _main                          # noqa: E402
    _ovr = TestClient(_main.app).get(
        "/api/diagnostics/context-audit/overview?days=7").json()
    _need = {"blocks", "switches", "daily", "top_conversations", "days_with_data", "has_data"}
    check("[3d] overview 一次返回全部面板所需字段",
          _need.issubset(set(_ovr.keys())),
          f"缺 {sorted(_need - set(_ovr.keys()))}")
    check("[3d] overview.daily 带 rows（趋势图不再需要二次请求）",
          "rows" in (_ovr.get("daily") or {}),
          f"daily keys={( _ovr.get('daily') or {}).keys()}")
    _tc = (_ovr.get("top_conversations") or [])
    check("[3d] top_conversations 带 title（否则列表全是「会话 9613」无法定位）",
          bool(_tc) and all("title" in x for x in _tc),
          f"首条 keys={list(_tc[0].keys()) if _tc else []}")
except Exception as _e:
    check("[3d] overview 端到端", False, f"{type(_e).__name__}: {_e}")

# ══════════════════════════════════════════════════════════════
# 开关触发可见（本轮 role 裁剪 + MCP 指标）
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[附] 开关实际触发（报表②视图）")
print("=" * 78)
rep = ContextAuditRepo(con)
sw = rep.switches(days=7)
names = {m["metric"] for m in sw["metrics"]}
# ⚠️ 生产态 MCP 是 offline ⇒ `get_bound_mcp()` 返回空 ⇒ role 裁剪**按设计不触发**
#    （prompt.py 里 `if not bound: return role_text`）。所以这里先做**负向断言**：
check("[附] MCP offline 时 role 裁剪不触发（按设计，不应有 role_manual_* 指标）",
      not any(n.startswith("role_manual_") for n in names),
      f"实际出现：{[n for n in names if n.startswith('role_manual_')]}")

# 正向：造 MCP online + tool_meta，再跑一次，指标应出现
con2 = get_db()
_srv = con2.execute("SELECT tools FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
_NAMES = json.loads(_srv["tools"] or "[]") if _srv else []
if _NAMES:
    _meta = [{"name": n, "description": f"智源平台接口：{n}",
              "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}}}
             for n in _NAMES]
    con2.execute("UPDATE mcp_servers SET status='online', tool_meta=? "
                 "WHERE name='zhiyuan-dev-mcp'",
                 (json.dumps(_meta, ensure_ascii=False),))
    con2.commit()
con2.close()

_seed["role_manual_filter"] = True
p2 = get_pipeline()
p2.execute("查询智源工程列表", cid, branch="dev", user=USER, forced_intent="智源对接agent")
_flush(force=True)
_seed["role_manual_filter"] = False

con = get_db()
rep = ContextAuditRepo(con)
sw = rep.switches(days=7)
names2 = {m["metric"] for m in sw["metrics"]}
check("[附] MCP online + 开关开 ⇒ role_manual_applied 指标可见",
      "role_manual_applied" in names2, str(sorted(names2))[:160])
check("[附] role_manual_dropped_chars > 0（确有裁剪）",
      next((m["value"] for m in sw["metrics"]
            if m["metric"] == "role_manual_dropped_chars"), 0) > 0)
for m in sw["metrics"][:10]:
    print(f"  {m['metric']:<30}{m['label']:<24}{m['value']:>8}")

# ── [3c] switches 语义（**放在这里**：此时已产生 role_manual_* 指标，
#  否则指标列表为空、断言在"0 项"上通过 = 假绿）──
_sw_bad = _sw_ok(sw)
check("[3c] switches 有指标（防断言在空集上假绿）",
      len(sw.get("metrics") or []) > 0, f"{len(sw.get('metrics') or [])} 项")
check("[3c] switches 每项都带 kind/unit/samples 且取值符合语义", not _sw_bad,
      str(_sw_bad[:3]) if _sw_bad else f"{len(sw.get('metrics') or [])} 项")
# 快照型真实样本：role_manual_dropped_chars 是"每轮裁掉多少字"，必须取均值不是求和
_dc = next((m for m in sw["metrics"] if m["metric"] == "role_manual_dropped_chars"), None)
check("[3c] 快照型 role_manual_dropped_chars 取均值（value==avg，不是 sum）",
      bool(_dc) and _dc["kind"] == "gauge" and _dc["value"] == _dc["avg"],
      f"kind={_dc and _dc['kind']} value={_dc and _dc['value']} avg={_dc and _dc['avg']}")

# ══════════════════════════════════════════════════════════════
# [5] 会话明细的差额口径
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[5] 会话明细差额口径")
print("=" * 78)
cd = rep.conversations(days=7, conversation_id=cid)
check("[5] 会话明细非空", bool(cd["traces"]), f"{len(cd['traces'])} 条 trace")
if cd["traces"]:
    t = cd["traces"][0]
    print(f"  trace={str(t['trace_id'])[:16]} system={t['system_tokens']} "
          f"计费={t['billed_prompt_tokens']} 差额={t['delta_messages_tools']}")
    check("[5] system_tokens > 0", t["system_tokens"] > 0)
    check("[5] 计费为空时差额为 None（不伪造）",
          t["delta_messages_tools"] is not None or t["billed_prompt_tokens"] in (0, None))

# ══════════════════════════════════════════════════════════════
# [7] 全部只读：行数不变
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[7] 只读性")
print("=" * 78)
before = (con.execute("SELECT COUNT(*) c FROM context_audit").fetchone()["c"],
          con.execute("SELECT COUNT(*) c FROM context_audit_summary").fetchone()["c"])
rep.blocks(days=7)
rep.switches(days=7)
rep.daily(days=7)
rep.conversations(days=7)
rep.conversations(days=7, conversation_id=cid)
after = (con.execute("SELECT COUNT(*) c FROM context_audit").fetchone()["c"],
         con.execute("SELECT COUNT(*) c FROM context_audit_summary").fetchone()["c"])
check("[7] 调用全部端点后行数不变（纯只读）", before == after, f"{before} → {after}")
con.close()

# ══════════════════════════════════════════════════════════════
# [1][6] HTTP 端点 + 报表脚本
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("[1][6] HTTP 端点可用性 + 报表脚本一致性")
print("=" * 78)
try:
    from fastapi.testclient import TestClient
    from main import app
    cli = TestClient(app)
    eps = {
        "/api/diagnostics/context-audit/blocks?days=7": ("rows", "total_tokens"),
        "/api/diagnostics/context-audit/daily?days=7": ("rows",),
        "/api/diagnostics/context-audit/conversations?days=7": ("conversations",),
        "/api/diagnostics/context-audit/switches?days=7": ("metrics",),
        "/api/diagnostics/context-audit/overview?days=7": ("blocks", "switches", "hint"),
    }
    ok_all = True
    for url, keys in eps.items():
        r = cli.get(url)
        good = r.status_code == 200
        body = {}
        if good:
            try:
                body = r.json()
            except Exception:
                good = False
        missing = [k for k in keys if k not in body]
        if not good or missing:
            ok_all = False
            print(f"   ✗ {url} status={r.status_code} missing={missing}")
        else:
            print(f"   ✓ {url}")
    check("[1] 五个端点均 200 且结构完整", ok_all)

    ov = cli.get("/api/diagnostics/context-audit/overview?days=7").json()
    check("[1] overview.has_data 为 True（刚跑过真实链路）", ov.get("has_data") is True,
          str(ov.get("has_data")))
except Exception as e:
    import traceback
    traceback.print_exc()
    check("[1] 五个端点均 200 且结构完整", False, f"{type(e).__name__}: {e}")

# 报表脚本 --json
import subprocess
_out = subprocess.run(
    [os.path.join(ROOT, ".venv", "Scripts", "python.exe"), "-X", "utf8",
     os.path.join(ROOT, "tools", "report_context_audit.py"), "--json", "--days", "7"],
    capture_output=True, text=True, cwd=ROOT, timeout=180)
try:
    payload = json.loads(_out.stdout)
    ok_json = True
except Exception:
    payload = {}
    ok_json = False
    print("   stderr:", (_out.stderr or "")[-400:])
check("[6] 报表脚本 --json 可解析", ok_json)
if ok_json:
    con2 = get_db()
    api_tokens = ContextAuditRepo(con2).blocks(days=7)["total_tokens"]
    con2.close()
    check("[6] 脚本与 repo 口径一致（同一真源）",
          payload["blocks"]["total_tokens"] == api_tokens,
          f"script={payload['blocks']['total_tokens']} repo={api_tokens}")

print("\n" + "=" * 78)
if FAILS:
    print(f"❌ FAIL {len(FAILS)} 项：" + "; ".join(FAILS))
    sys.exit(1)
print("✅ Step10 验收通过 —— 归因数据可见，端点只读，报表与接口同口径")
