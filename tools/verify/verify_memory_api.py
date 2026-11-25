# -*- coding: utf-8 -*-
"""记忆层 API 门禁（2026-11-11）：Dreaming / notes / 流水检索 / 冷归档的**契约**。

## 为什么必须测端点而不是只测函数
上一批（`318c681`）交付后实测暴露的真问题：`services/memory_dream.py` 功能齐全、
门禁全绿，但**全仓没有任何调用方** —— 281 条记忆永远停在 `unverifiable`。
函数级判据测不出"没人调用"，只有端点契约能测出"接线通了没"。

## 断言
  A1 preview 只读：调用前后 agent_memory 快照 sha256 不变
  A2 preview 返回 fingerprint + 候选（state ∈ verified/contradicted）
  A3 apply 无 fingerprint ⇒ 400，且一行不写
  A4 apply fingerprint 过期 ⇒ 409（防"预览后库里又写入了"）
  A5 apply memory_ids 为空 ⇒ 400（不提供"全部应用"）
  A6 apply 合法 ⇒ 只改 verify_state，content 一字不变
  A7 notes 视图端点返回统一字段
  A8 流水检索端点能命中刚写入的行
  A9 归档端点无 confirm ⇒ 400；带 confirm ⇒ 行数下降且 restore 可还原
  M1~M4 变异：preview 写库 / 去掉 fingerprint 校验 / apply 改正文 / 去 confirm
"""
import hashlib
import json
import os
import sys
from _tmpdir import mkdtemp

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = mkdtemp(prefix="verify_memory_api_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from database import init_db  # noqa: E402

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    """detail 只在失败时打印 —— PASS 时打失败文案会让人以为门禁在报错。"""
    (PASS if cond else FAIL).append(name)
    if cond:
        print("  PASS %s" % name, flush=True)
    else:
        print("  FAIL %s  <- %s" % (name, detail), flush=True)


import sqlite3  # noqa: E402


def db():
    c = sqlite3.connect(os.environ["MBSE_DB_PATH"], timeout=30)
    c.row_factory = sqlite3.Row
    return c


print("== 0 临时库初始化 + 造数 ==", flush=True)
init_db()
c = db()
# 一条**数值被否定**的笔记（强信号形态）+ 一条能推翻它的流水；
# ⚠️ 必须是数值形态：无数字的邻近否定按设计只进 needs_review（不排除、交人工），
#    拿它测 apply 会得到一个"正确但不符合本门禁意图"的状态。
c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,mem_topic,source,"
          "tier,activation,forgotten) VALUES (?,?,?,?,?,?,?,0)",
          ("probe", "fact", "探针笔记：转发器EIRP上限为30dBW，位于东侧机柜",
           "导航", "agent", "recall", 1.0))
c.execute("INSERT INTO messages (conversation_id,role,content) VALUES (?,?,?)",
          (1, "user", "实测复核：转发器EIRP上限不是30dBW，上限有误，位于西侧机柜"))
for i in range(6):
    c.execute("INSERT INTO agent_memory (agent_id,mem_type,content,source,tier,"
              "activation,forgotten) VALUES (?,?,?,?,?,?,0)",
              ("knowledge_qa", "fact", "建模经验 %d：视图层级按包嵌套组织" % i,
               "rule_agent", "recall", 1.0))
c.commit()
c.close()


def snapshot():
    c2 = db()
    try:
        rows = [tuple(r) for r in c2.execute(
            "SELECT id, content, COALESCE(verify_state,'') FROM agent_memory ORDER BY id")]
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()
    finally:
        c2.close()


from fastapi.testclient import TestClient  # noqa: E402
import main as app_main  # noqa: E402

client = TestClient(app_main.app)

print("\n[1] Dreaming 预览 / 审批", flush=True)
before = snapshot()
r = client.get("/api/memory/dream/preview")
pv = r.json()
chk("A1 preview 只读（快照 sha256 前后一致）", snapshot() == before,
    "状态码 %s" % r.status_code)
chk("A2 preview 返回 fingerprint 与非空候选",
    r.status_code == 200 and len(pv.get("fingerprint") or "") == 64
    and any(x.get("state") == "contradicted" for x in pv.get("candidates") or []),
    "fp=%r n=%d" % ((pv.get("fingerprint") or "")[:8], len(pv.get("candidates") or [])))
fp = pv.get("fingerprint")

r3 = client.post("/api/memory/dream/apply", json={"memory_ids": [1]})
chk("A3 apply 无 fingerprint ⇒ 400 且 0 写入",
    r3.status_code == 400 and snapshot() == before, "实得 %s" % r3.status_code)

r4 = client.post("/api/memory/dream/apply",
                 json={"fingerprint": "0" * 64, "memory_ids": [1]})
chk("A4 apply fingerprint 过期 ⇒ 409", r4.status_code == 409, "实得 %s" % r4.status_code)

r5 = client.post("/api/memory/dream/apply", json={"fingerprint": fp, "memory_ids": []})
chk("A5 apply memory_ids 空 ⇒ 400（不提供全部应用）", r5.status_code == 400,
    "实得 %s" % r5.status_code)

chk("A5b 前三次被拒后仍 0 写入", snapshot() == before)

c2 = db()
content_before = c2.execute("SELECT content FROM agent_memory WHERE id=1").fetchone()[0]
c2.close()
r6 = client.post("/api/memory/dream/apply",
                 json={"fingerprint": fp, "memory_ids": [1], "approved_by": "gate"})
c3 = db()
row = c3.execute("SELECT content, COALESCE(verify_state,'') FROM agent_memory WHERE id=1").fetchone()
c3.close()
chk("A6 合法 apply ⇒ 只改 verify_state，content 不变",
    r6.status_code == 200 and (r6.json() or {}).get("applied", 0) >= 1
    and row[0] == content_before and row[1] == "contradicted",
    "resp=%r row=%r" % (r6.json(), (row[0][:20], row[1])))

# ★ A6b 受控 A/B：证明"召回侧排除"确实是 verify_state 造成的，
#   而不是"这条记忆本来就查不到"。唯一变量 = verify_state 列。
#   实测踩过：只断言"apply 后查不到"是不够的 —— 那次它本来就查不到，
#   断言恒真，等于假绿。
from memory_service import MemoryService  # noqa: E402
c7 = db()
try:
    _a = [x.get("id") for x in MemoryService.search(c7, "probe", "探针 转发器EIRP 东侧机柜", top_k=10)]
    c7.execute("UPDATE agent_memory SET verify_state='unverifiable' WHERE id=1")
    c7.commit()
    _b = [x.get("id") for x in MemoryService.search(c7, "probe", "探针 转发器EIRP 东侧机柜", top_k=10)]
    c7.execute("UPDATE agent_memory SET verify_state='contradicted' WHERE id=1")
    c7.commit()
finally:
    c7.close()
chk("A6b 被证伪的记忆不进召回；改回 unverifiable 立刻出现（受控 A/B）",
    1 not in _a and 1 in _b, "contradicted 时 ids=%r / unverifiable 时 ids=%r" % (_a, _b))

print("\n[2] notes 视图 / 流水检索 / 冷归档", flush=True)
r7 = client.get("/api/memory/notes?kind=memory&limit=5")
n7 = r7.json()
fields = {"kind", "subject", "claim", "evidence_ref", "status", "confidence", "created_at"}
chk("A7 notes 端点返回统一字段",
    r7.status_code == 200 and n7.get("items")
    and fields <= set(n7["items"][0]), "实得 %r" % (list((n7.get("items") or [{}])[0])[:8]))

c4 = db()
c4.execute("INSERT INTO messages (conversation_id,role,content) VALUES (1,'user',?)",
           ("冷归档探针：转发器 EIRP 上限 20 dBW",))
c4.commit()
c4.close()
r8 = client.get("/api/memory/transcript/search", params={"q": "冷归档探针"})
chk("A8 流水检索能命中刚写入的行",
    r8.status_code == 200 and any("m" == (it.get("kind") or "")[:1]
                                  for it in r8.json().get("items") or []),
    "实得 %r" % (r8.json().get("items") or [])[:2])

c5 = db()
c5.execute("INSERT INTO tool_result_offloads (conversation_id,tool_name,content,created_at) "
           "VALUES (1,'t','很老的工具结果','2020-01-01 00:00:00')")
c5.commit()
_old_id = c5.execute("SELECT MAX(id) FROM tool_result_offloads").fetchone()[0]
n_off_before = c5.execute("SELECT COUNT(*) FROM tool_result_offloads").fetchone()[0]
c5.close()
r9 = client.post("/api/memory/retention/apply", json={"older_than_days": 30})
chk("A9 归档端点无 confirm ⇒ 400 且行数不变",
    r9.status_code == 400, "实得 %s" % r9.status_code)
r10 = client.post("/api/memory/retention/apply",
                  json={"confirm": True, "older_than_days": 30})
c6 = db()
n_off_after = c6.execute("SELECT COUNT(*) FROM tool_result_offloads").fetchone()[0]
from core.memory_retention import restore  # noqa: E402
back = restore(c6, "tool_result_offloads", _old_id)
c6.close()
chk("A9b 带 confirm ⇒ 行数下降，且归档可 restore 还原",
    r10.status_code == 200 and n_off_after == n_off_before - 1
    and back.get("content") == "很老的工具结果",
    "before=%s after=%s restore=%r resp=%r" % (n_off_before, n_off_after,
                                               back.get("content"), r10.json()))

print("\n[3] 变异自证", flush=True)


def _mutate_router():
    """把 routers/memory_admin.py 源码读进来，替换成变异版本并重新挂载。"""
    path = os.path.join(ROOT, "routers", "memory_admin.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _reload_with(src):
    """用变异源码重建一个 app：只把 memory_admin 路由换掉，其余沿用真 app。"""
    from fastapi import FastAPI
    ns = {"__name__": "memory_admin_mut"}
    exec(compile(src, os.path.join(ROOT, "routers", "memory_admin.py"), "exec"), ns)
    app2 = FastAPI()
    app2.include_router(ns["router"])
    return TestClient(app2)


MUTS = [
    ("M1 preview 写库（把候选落成 verified）⇒ A1 翻红",
     [("    pkg = dream(conn, note_limit=_limit)",
       "    pkg = dream(conn, note_limit=_limit)\n"
       "    conn.execute(\"UPDATE agent_memory SET verify_state='verified'\")\n"
       "    conn.commit()")],
     lambda cl: _snap_changed(cl, "A1")),
    ("M2 去掉 fingerprint 校验 ⇒ A4 翻红",
     [("    if pkg[\"fingerprint\"] != fp:", "    if False:")],
     lambda cl: cl.post("/api/memory/dream/apply",
                        json={"fingerprint": "0" * 64,
                              "memory_ids": [1]}).status_code != 409),
    ("M3 apply 顺手改正文 ⇒ A6 翻红",
     [('    pkg["candidates"] = picked',
       '    pkg["candidates"] = picked\n'
       '    conn.execute("UPDATE agent_memory SET content=content || \'[MUT]\'")\n'
       '    conn.commit()')],
     lambda cl: _content_changed(cl)),
    ("M4 去掉 confirm 门 ⇒ A9 翻红",
     [('    if not (body or {}).get("confirm"):', "    if False:")],
     lambda cl: cl.post("/api/memory/retention/apply",
                        json={"older_than_days": 30}).status_code != 400),
]


def _snap_changed(cl, _tag):
    s0 = snapshot()
    cl.get("/api/memory/dream/preview")
    return snapshot() != s0


def _content_changed(cl):
    pv = cl.get("/api/memory/dream/preview").json()
    c9 = db()
    c_before = c9.execute("SELECT content FROM agent_memory WHERE id=1").fetchone()[0]
    c9.close()
    cl.post("/api/memory/dream/apply",
            json={"fingerprint": pv.get("fingerprint"), "memory_ids": [1]})
    c10 = db()
    c_after = c10.execute("SELECT content FROM agent_memory WHERE id=1").fetchone()[0]
    c10.close()
    return c_after != c_before


for label, repls, prob in MUTS:
    src = _mutate_router()
    ok = True
    for a, b in repls:
        if a not in src:
            chk(label, False, "变异锚点失效：%r" % a[:60])
            ok = False
            break
        src = src.replace(a, b, 1)
    if not ok:
        continue
    try:
        chk(label, prob(_reload_with(src)) is True,
            "变异后判据仍绿 ⇒ 抓不住这个回归")
    except Exception as e:  # noqa: BLE001
        chk(label, False, "%s: %s" % (type(e).__name__, e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)), flush=True)
for f in FAIL:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAIL else 0)
