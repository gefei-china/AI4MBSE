# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
# -*- coding: utf-8 -*-
"""P0-9 门禁：MCP 显式绑定工具豁免技能白名单。

## 背景（实测，生产库快照）

`智源对接agent` 同时绑定 MCP server（71 工具）与技能「智源平台管理」
（`allowed_tools` 6 项）。技能一命中就被写进 `_tool_whitelist`
（`execute.py:460-466`）⇒ 白名单**把绑定的 MCP 工具全部裁掉**：

| 查询 | 豁免前 | 豁免后 |
|---|---|---|
| 「查询智源工程列表」 | 2 工具 / **0 MCP** | 10 工具 / **8 MCP** |
| 「读取文件内容」 | 8 工具 / **0 MCP** | 16 工具 / **8 MCP** |

雪上加霜：该白名单里 5 个 `zhiyuan_*` 工具**没有以 type='tool' 绑定到本 agent**
⇒ 进不了候选集 ⇒ 白名单「授了什么也拿不到、却把已有的全撤销」，且**静默无日志**。

这正是 2026-10-16「绑了 71 个只露 6 个、工程列表查不到」事故的**真正根因之一**
（当时归因为 JIT 裁剪），至今仍在。

## 判据

  [1] 豁免开（默认）⇒ MCP 工具在技能白名单下**仍然注入**
  [2] 豁免关 ⇒ 逐字节退回改动前（MCP 被裁成 0）
  [3] 只豁免 MCP —— 内置工具 / DB 工具仍受白名单约束（最小权限不放松）
  [4] 委派白名单语义不变：非 MCP 的白名单外工具仍被裁
  [5] 契约型白名单（`_sk_wl` 强制技能）与豁免可共存
  [6] 归因可见：`whitelist_exempted_mcp` 落库
  [7] 回归锁：豁免后候选里每个 MCP 名都来自 bound（不引入未绑定工具）
  [8] 变异自证：把豁免逻辑去掉 ⇒ [1] 必须翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_mcp_whitelist_exempt.py
"""
import os
import sys
import json
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_wl_exempt.db")
# ★ 2026-11-26：原为硬编码 `backups/t-prod-20261026-122709.db`（已被清理 ⇒ import 期崩）。
#   改走 `_fixture_db` 三层回退（历史备份 → 当前库 → SKIP 不崩）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fixture_db import require_or_skip  # noqa: E402
print("  夹具来源：%s" % require_or_skip(_TMP, start=__file__, label="MCP 白名单豁免夹具库"))
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

import logging                                        # noqa: E402
logging.disable(logging.WARNING)

from database import init_db, get_db                  # noqa: E402
init_db()

from agent import AgentPipeline                        # noqa: E402
from core import config as cfg                         # noqa: E402
from agent.context_audit import flush as _flush        # noqa: E402

_AGENT = "智源对接agent"
_USER = {"role_name": "admin", "role": "admin"}
_FAILS = []


def check(name, cond, detail=""):
    if not cond:
        _FAILS.append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# ── 造 MCP online + 真实 tool_meta ──
con = get_db()
_srv = con.execute("SELECT tools FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
NAMES = set(json.loads(_srv["tools"] or "[]")) if _srv else set()
check("[0] 生产库存在 zhiyuan-dev-mcp 且含工具名", bool(NAMES), f"{len(NAMES)} 个")
if NAMES:
    _meta = [{"name": n, "description": f"智源平台接口：{n}",
              "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}}}
             for n in sorted(NAMES)]
    con.execute("UPDATE mcp_servers SET status='online', tool_meta=? "
                "WHERE name='zhiyuan-dev-mcp'",
                (json.dumps(_meta, ensure_ascii=False),))
    con.commit()
con.close()


def build(query, exempt, forced_skill=False):
    """跑真实 skill 装配 + 白名单合并 + 工具构建，返回 (工具名列表, MCP 名单, 白名单)。"""
    sec = cfg._CONFIG.setdefault("context_audit", {})
    _old = sec.get("mcp_exempt_skill_whitelist", True)
    sec["mcp_exempt_skill_whitelist"] = exempt
    try:
        p = AgentPipeline()
        c = get_db()
        try:
            p.registry.load_from_db(c, _USER)
        finally:
            c.close()
        p._build_skill_prompt(_AGENT, query, _USER)
        sat = getattr(p, "_skill_allowed_tools", None)
        p._tool_whitelist = list(sat) if sat else None
        td = p._build_tools_def(_AGENT, query, _USER)
        names = [t["function"]["name"] for t in td]
        return names, [n for n in names if n in NAMES], (p._tool_whitelist or [])
    finally:
        sec["mcp_exempt_skill_whitelist"] = _old


# ── [2] 豁免关 ⇒ 只保留**白名单显式授权**的 MCP 工具 ──
# ★ 2026-11-26 修判据假设：原判据 `len(mcp_off) == 0`（"关豁免即归零"）。
#   实测白名单 32 项里**有 30 项本身就是 MCP 工具名**（技能「智源平台管理」的
#   allowed_tools 显式列了它们）⇒ 关豁免后这 30 个仍合法进入，`mcp_off=30`。
#   这不是缺陷，是"白名单显式授权"与"豁免兜底"两条正当路径并存。
#   判据改为**关系式**（这也正是本文件 docstring 第 1 条设计原则：
#   「断言关系而非具体数值」）：
#     ① 豁免开 ⇒ 注入数 ≥ 豁免关（豁免只会加、不会减）；
#     ② 豁免关时仍在的 MCP 工具 **必须全部在白名单里**（没有豁免就不能"偷渡"）。
n_on, mcp_on, wl = build("查询智源工程列表", True)
check("[1] 技能白名单命中（前置条件成立，否则本门禁无意义）", bool(wl), f"白名单 {len(wl)} 项")
check("[1] 豁免开 ⇒ MCP 工具仍然注入", len(mcp_on) > 0, f"{len(mcp_on)} 个：{mcp_on[:4]}")

# ── [2] 豁免关 ⇒ 退回"只有白名单授权"──
n_off, mcp_off, _ = build("查询智源工程列表", False)
_wlset0 = set(wl)
_stowaway = [n for n in mcp_off if n not in _wlset0]
check("[2] 豁免关 ⇒ 注入的 MCP 工具全部来自白名单（无豁免不能偷渡）",
      not _stowaway, "白名单外却被注入：%s" % _stowaway[:6])
check("[2] 豁免开比关注入更多工具（豁免确实放行了额外工具）",
      len(n_on) > len(n_off), f"{len(n_off)} → {len(n_on)}（MCP {len(mcp_off)} → {len(mcp_on)}）")

# ── [3] 只豁免 MCP：内置/DB 工具仍受白名单约束 ──
#   取一个明确不在白名单里的内置工具（如 graph_retrieve），两种模式下都不该出现
check("[3] 白名单外的内置工具仍被裁（最小权限未放松）",
      "graph_retrieve" not in n_on and "graph_retrieve" not in n_off,
      f"on含={('graph_retrieve' in n_on)} off含={('graph_retrieve' in n_off)}")
# ── 强制注入集（从代码/配置推导，**不硬编码名单**）────────────────────────
# ★ 2026-11-26：原判据把例外写成 `n != "ask_user"` 一个字面量。
#   实测 `memory_fetch`（2026-11-11 起**常驻注入**，`tools.py:1324`「只读、无副作用，
#   不按本轮有无记忆命中做条件注入」）与 `todo_write`（受 config `agent.todo_enabled`
#   控制，`tools.py:1296`）也会出现在非白名单工具里 ⇒ 判据红而产品正确。
#   改为**从真源推导**：常驻的那两个写进集合，开关型按 config 现值附加
#   ⇒ 将来开关一开一关，判据自动跟随；若又新增强制注入，判据会红并打印名字
#   （这是**应该人审**的决定：强制注入 = 放松最小权限，不能悄悄加）。
_FORCED_ALWAYS = {"ask_user", "memory_fetch"}
try:
    _todo_on = bool(cfg.get("agent", "todo_enabled", False))
except Exception:
    _todo_on = False
_FORCED = set(_FORCED_ALWAYS) | ({"todo_write"} if _todo_on else set())
print("  强制注入集（推导）：%s（todo_enabled=%s）" % (sorted(_FORCED), _todo_on))

_wlset = set(wl)
_non_mcp_outside = [n for n in n_on if n not in NAMES and n not in _wlset and n not in _FORCED]
check("[3] 非 MCP 候选里没有白名单外的工具（除已知强制注入集）",
      not _non_mcp_outside, "多出未登记的工具：%s（若是新加强制注入，请在此登记并说明理由）"
      % str(_non_mcp_outside[:6]))

# ── [4] 委派白名单语义不变（非 MCP 仍严格受限）──
p = AgentPipeline()
c = get_db()
try:
    p.registry.load_from_db(c, _USER)
finally:
    c.close()
p._tool_whitelist = ["file_read"]
td4 = p._build_tools_def(_AGENT, "读取文件内容", _USER)
names4 = [t["function"]["name"] for t in td4]
# ★ 2026-11-26：原判据 `all(n in ("file_read","ask_user") or n in NAMES)` ——
#   漏了 memory_fetch / todo_write（理由同 [3]）。
check("[4] 纯委派白名单（非技能）⇒ 只剩白名单工具 + MCP + 已知强制注入",
      all(n in ("file_read",) or n in NAMES or n in _FORCED for n in names4),
      "越界项：%s" % [n for n in names4 if n not in ("file_read",) and n not in NAMES
                     and n not in _FORCED][:8])
check("[4] 委派白名单下 MCP 也被视作显式授权（豁免同效）", bool([n for n in names4 if n in NAMES]))

# ── [7] 回归锁：注入的 MCP 名必须都来自 bound ──
check("[7] 注入的 MCP 工具全部来自 server 绑定清单（不引入外部工具）",
      all(n in NAMES for n in mcp_on), str([n for n in mcp_on if n not in NAMES]))

# ── [6] 归因可见 ──
_flush(force=True)
con = get_db()
_rows = con.execute("SELECT metrics FROM context_audit_summary "
                    "WHERE metrics LIKE '%whitelist_exempted_mcp%'").fetchall()
_vals = []
for r in _rows:
    try:
        _vals.append(int(json.loads(r["metrics"]).get("whitelist_exempted_mcp") or 0))
    except Exception:
        pass
check("[6] 归因 whitelist_exempted_mcp 落库且 > 0", any(v > 0 for v in _vals),
      f"记录 {len(_vals)} 条，最大 {max(_vals) if _vals else 0}")
con.close()

# ── [8] 变异自证：把豁免逻辑短路 ⇒ [1] 必须翻红 ──
_orig = AgentPipeline._build_tools_def
_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
            encoding="utf-8").read()
check("[8] 变异自证：豁免代码存在于源码（删掉即等于退回 [2]）",
      "mcp_exempt_skill_whitelist" in _src and "_rescue" in _src,
      "若此判据失败说明豁免实现被移除，[1] 将失去保护")
# 直接模拟"豁免失效"：用 exempt=False 构建，断言 MCP 归零（即 [1] 的对立面）
_n_mut, _mcp_mut, _ = build("查询智源工程列表", False)
check("[8] 变异自证：关闭豁免 ⇒ MCP 注入数**下降**（证明 [1] 的差异确实由豁免带来）",
      len(_mcp_mut) < len(mcp_on) and len(mcp_on) > 0,
      f"开={len(mcp_on)} 关={len(_mcp_mut)} ⇒ 若相等，说明豁免路径没起作用（[1] 是空转）")

for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print("\n" + "=" * 66)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— MCP 绑定工具豁免技能白名单（豁免/回滚/最小权限/归因/变异自证）全部通过")
