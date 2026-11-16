# -*- coding: utf-8 -*-
"""记忆作用域隔离门禁（2026-11-05，对应 docs/记忆作用域隔离-实证评估与改造方案-20261105.md §6）

守住四条不变式（G1/G2/G3/G5），全部是**确定性**判据，不依赖真库、不调 LLM、不写库：

  G1 默认零漂移：默认配置（legacy_policy=visible）下 `_scope_filter` 的输出
     与**改动前的字符串基线**逐字相等（基线写死在下方 BASELINE，改动即判红）。
  G2 隔离真的生效：hidden 下 `search` 结果里 `scope_type=''` 条数为 0；
     且**必须与 visible 形成差异**（否则变异体也能"通过"，属假阳性）。
  G3 共享层不被误伤：显式共享层 `('agent', intent)` 与 `('global','')` 在 hidden 下**仍可召回**。
  G5 可回退：hidden → visible 切换后，`search` 结果 id 集合与切换前**逐 id 相等**。

变异自证（M1–M3）：证明上述判据不是空转——把实现按预期改坏，判据必须转红。
  M1 撤销 legacy 槽位策略（恒拼存量行）      → G2 必须红
  M2 把 hidden 写成"连显式共享层一起删"      → G3 必须红
  M3 把默认策略写成 hidden（改动前行为）      → G1 必须红

⚠️ 本门禁**不碰真库**：用 mkdtemp 建最小 agent_memory 表，避免因生产数据变化而假红。
"""
import os
import inspect
import sqlite3
import sys
import textwrap

from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from memory_service import MemoryService  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── G1 基线：改动前（HEAD cab71f0）_scope_filter 的原始输出，逐字写死 ──────────
# 为什么写死字符串而不调函数：这是"零漂移"的**唯一**可信判据。
#   若改为"与当前实现互相比对"，实现被改坏时两边会一起变，基线失去意义。
_LEG = "(scope_type='' AND agent_id=?)"
BASELINE = {
    "basic": (" AND ((scope_type=? AND scope_id=?) OR (scope_type=? AND scope_id=?)"
              " OR (scope_type=? AND scope_id=?) OR (scope_type='global' AND scope_id='')"
              " OR " + _LEG + ")",
              ["project", "p1a0d2e694af", "user", "u1", "agent", "chat", "chat"]),
    "agent_only": (" AND ((scope_type=? AND scope_id=?) OR " + _LEG + ")",
                   ["agent", "impact", "impact"]),
    "global_only": (" AND ((scope_type='global' AND scope_id='') OR " + _LEG + ")",
                    ["chat"]),
}


def _fixture():
    """最小 agent_memory 表（含 scope/tier 列）+ 三条不同作用域的活记忆。"""
    d = mkdtemp(prefix="memscope_")
    conn = sqlite3.connect(os.path.join(d, "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE agent_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        mem_type TEXT DEFAULT 'fact',
        content TEXT DEFAULT '',
        embedding TEXT DEFAULT '[]',
        embed_version TEXT DEFAULT '',
        created_at TEXT DEFAULT '2026-09-11 00:00:00',
        source TEXT DEFAULT 'agent',
        relevance REAL DEFAULT 1.0,
        activation REAL DEFAULT 1.0,
        access_count INTEGER DEFAULT 0,
        last_accessed_at TEXT DEFAULT '',
        forgotten INTEGER DEFAULT 0,
        mem_topic TEXT DEFAULT '',
        scope_type TEXT DEFAULT '',
        scope_id TEXT DEFAULT '',
        tier TEXT DEFAULT 'recall',
        superseded_by INTEGER DEFAULT 0
    )""")
    # 三条内容刻意用同一批词，保证 bigram 相关度相近 ⇒ 差异只来自作用域过滤，
    # 不来自语义打分（否则判据会被打分波动干扰）。
    rows = [
        ("legacy_row", "chat", "", "", "2026-09-11 00:00:00"),      # 存量行（跨项目外溢源）
        ("proj_row", "chat", "project", "PID_A", "2026-10-01 00:00:00"),  # 本项目
        ("agent_row", "chat", "agent", "chat", "2026-10-01 00:00:00"),    # 显式共享层
        ("global_row", "chat", "global", "", "2026-10-01 00:00:00"),      # 显式共享层
        ("other_proj", "chat", "project", "PID_B", "2026-10-01 00:00:00"),  # 别的项目
    ]
    for aid, ag, st, sid, ts in rows:
        conn.execute(
            "INSERT INTO agent_memory (agent_id, content, scope_type, scope_id, created_at) "
            "VALUES (?,?,?,?,?)",
            (ag, "记忆作用域隔离测试样本 " + aid, st, sid, ts))
    conn.commit()
    return conn


SCOPES = [("project", "PID_A"), ("agent", "chat"), ("global", "")]


def _hits(conn, policy, cut=""):
    """返回 [(id, scope_type, scope_id), ...]，供各判据直接核对作用域归属。

    ★ 为什么不给 `search` 加 `legacy_policy` 形参：本改造的设计目标就是
      **补丁面只有 `_scope_filter` 一个函数**（`search` / `search_core` 签名不动）。
      所以这里通过替身 `_legacy_policy` 注入策略，让判据走**真实 search 路径**——
      若实现改成"只有显式传参才生效"，本门禁的 G2/G3/G5 会立刻红。
    """
    _orig = MemoryService._legacy_policy
    MemoryService._legacy_policy = staticmethod(lambda: (policy, cut))
    try:
        rows = MemoryService.search(conn, "chat", query="记忆作用域隔离测试样本",
                                    top_k=50, scopes=SCOPES)
    finally:
        MemoryService._legacy_policy = _orig
    return [(h.get("id"), (h.get("scope_type") or ""), (h.get("scope_id") or ""))
            for h in rows]


conn = _fixture()
try:
    # ── G1 默认零漂移 ─────────────────────────────────────────
    print("[G1] 默认配置零行为漂移（与改动前字符串基线逐字比对）")
    for k, (want_clause, want_params) in BASELINE.items():
        scopes = {"basic": [("project", "p1a0d2e694af"), ("user", "u1"),
                            ("agent", "chat"), ("global", "")],
                  "agent_only": [("agent", "impact")],
                  "global_only": [("global", "")]}[k]
        aid = {"basic": "chat", "agent_only": "impact", "global_only": "chat"}[k]
        clause, params, _idx = MemoryService._scope_filter(conn, aid, scopes)
        check("G1/%s clause 逐字相等" % k, clause == want_clause, repr(clause)[:120])
        check("G1/%s params 逐项相等" % k, params == want_params, params)

    # 默认策略（不显式传参）必须解析为 visible ⇒ 与显式 visible 结果一致
    pol, cut = MemoryService._legacy_policy()
    check("G1/默认策略解析为 visible 且切点为空", pol == "visible" and cut == "", (pol, cut))
    _vis = _hits(conn, "visible")
    _dflt = MemoryService.search(conn, "chat", query="记忆作用域隔离测试样本",
                                 top_k=50, scopes=SCOPES)
    check("G1/不传参 == 显式 visible（默认路径无差异）",
          [h.get("id") for h in _dflt] == [x[0] for x in _vis],
          "default=%s visible=%s"
          % ([h.get("id") for h in _dflt], [x[0] for x in _vis]))

    # ── G2 隔离真的生效 ───────────────────────────────────────
    print("[G2] hidden 下存量行不可见")
    vis_ids = [x[0] for x in _vis]
    hid = _hits(conn, "hidden")
    hid_ids = [x[0] for x in hid]
    vis_stypes = [x[1] for x in _vis]
    hid_stypes = [x[1] for x in hid]
    check("G2/visible 确实含存量行（前置：夹具有效）",
          "" in vis_stypes, "visible_scopes=%s" % vis_stypes)
    check("G2/hidden 结果中 scope_type='' 条数为 0",
          "" not in hid_stypes, "hidden_scopes=%s" % hid_stypes)
    check("G2/hidden 与 visible 必须有差异（防变异体也能『通过』）",
          set(hid_ids) != set(vis_ids), "visible=%s hidden=%s" % (vis_ids, hid_ids))

    # ── G3 显式共享层不被误伤 ─────────────────────────────────
    print("[G3] hidden 保留显式共享层（agent / global），且不引入别的项目")
    check("G3/hidden 仍能召回 agent 作用域行",
          "agent" in hid_stypes, "hidden_scopes=%s" % hid_stypes)
    check("G3/hidden 仍能召回 global 作用域行",
          "global" in hid_stypes, "hidden_scopes=%s" % hid_stypes)
    check("G3/hidden 不出现本工程之外的项目行（PID_B）",
          not any(st == "project" and sid == "PID_B" for _i, st, sid in hid),
          "hidden=%s" % hid)
    check("G3/hidden 仍保留本项目行（PID_A），隔离不是一刀切",
          any(st == "project" and sid == "PID_A" for _i, st, sid in hid),
          "hidden=%s" % hid)

    # ── G5 可回退 ────────────────────────────────────────────
    print("[G5] 切回 visible 后逐 id 复原")
    back = _hits(conn, "visible")
    check("G5/hidden→visible 结果逐项（id/作用域）完全相同",
          back == _vis, "before=%s after=%s" % (_vis, back))

    # ── G4 新写入不再产生存量槽（S2 静态断言）──────────────────
    # 为什么用源码断言而不是查库：本门禁跑干净库/CI（无生产数据），
    #   只能锁"写入点长什么样"；运行期"是否真的没再产生"由 G4-runtime 查询负责（见文件末尾说明）。
    print("[G4] 三个写入点不得再写 scope_type=''（S2）")
    import re as _re

    def _src_of(rel):
        p = os.path.join(ROOT, rel)
        with open(p, encoding="utf-8") as f:
            return f.read().replace("\r\n", "\n")

    def _src_present(name, cond, hint=""):
        """G4 统一断言：只在失败时输出提示（PASS 时输出"未找到锚点"会误导读者）。"""
        check(name, cond, "" if cond else (hint or "锚点已漂移，需同步更新门禁"))

    _reflow = _src_of("knowledge_reflow.py")
    _src_present("G4/知识回流：无项目归属时落 agent 共享层，不再落空槽",
                 'else ("agent", str(agent_id or "reflow")[:120])' in _reflow)

    _flows = _src_of("routers/studio_parts/flows.py")
    _src_present("G4/人工 API：INSERT 显式带 scope_type/scope_id 两列",
                 "INSERT INTO agent_memory (agent_id, mem_type, content, scope_type, scope_id)"
                 in _flows)

    # 反空转：确认上述断言不是恒真（这两个文件里**确实存在** scope_type 字样才说明查对了文件）
    check("G4/断言文件确实涉及 scope（防查错文件导致恒真）",
          all("scope_type" in s for s in (_reflow, _flows)),
          "reflow=%s flows=%s" % tuple("scope_type" in s for s in (_reflow, _flows)))

    # ★ 2026-11-05：画布节点（workflows/nodes.py）与画布执行入口
    #   （services/conversation_service.py）的两条断言随画布工作流移除。
    #   无法再断言"它们会不会写空槽"——因为这两个写入点本就不存在了。
    #   现存写入点只剩：会话沉淀（agent/pipeline_parts/memory.py）、知识回流、
    #   人工 API。前者不写空槽的前提由 G1/G2 的读取侧判据覆盖。

    # ── 变异自证 ─────────────────────────────────────────────
    print("[M] 变异自证（判据必须能被改坏抓住）")
    _src = textwrap.dedent(inspect.getsource(MemoryService._scope_filter)).replace("\r\n", "\n")

    def _twin(mutate, label):
        mut = mutate(_src)
        ok = mut != _src
        check("M%s 变异锚点命中" % label, ok,
              "" if ok else "锚点已漂移，需同步更新门禁（否则变异是空转）")
        if not ok:
            return None
        ns = {"MemoryService": MemoryService}
        exec(compile(mut, "<sf_twin%s>" % label, "exec"), ns)
        fn = ns["_scope_filter"]
        if isinstance(fn, staticmethod):
            fn = fn.__func__
        return fn

    # M1：存量行槽位改成**无条件**拼接（等于把策略写死成 visible）→ hidden 失效，G2 应判红
    _m1 = _twin(lambda s: s.replace('if _pol == "visible":', "if True:", 1), "1")
    if _m1:
        _st = _m1(conn, "chat", SCOPES, legacy_policy="hidden", cut_ts="")
        check("M1 槽位无条件拼 ⇒ hidden 下仍出现存量行（G2 判据会红）",
              _LEG in _st[0], "clause=%s" % (_st[0][:110]))

    # M2：只保留第一个作用域槽（把显式共享层 agent/global 也一起删）→ G3 应判红
    _m2 = _twin(lambda s: s.replace("for st, sid in scopes:", "for st, sid in scopes[:1]:", 1), "2")
    if _m2:
        _st2 = _m2(conn, "chat", SCOPES, legacy_policy="hidden", cut_ts="")
        check("M2 只留 project 槽 ⇒ 显式共享层消失（G3 判据会红）",
              _st2[0].count(" OR ") == 0 and "agent" not in _st2[1],
              "clause=%s params=%s" % (_st2[0][:110], _st2[1]))

    # M3：默认策略写死成 hidden → 与改动前基线不再相等，G1 应判红
    _m3 = _twin(lambda s: s.replace(
        "_pol, _cut = MemoryService._legacy_policy()",
        '_pol, _cut = "hidden", ""', 1), "3")
    if _m3:
        _st3 = _m3(conn, "chat", [("agent", "impact")])  # 不显式传 policy = 走默认
        _, want_params = BASELINE["agent_only"]
        check("M3 默认改 hidden ⇒ 与基线不再相等（G1 判据会红）",
              _st3[1] != want_params, "params=%s want=%s" % (_st3[1], want_params))
finally:
    conn.close()

print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
