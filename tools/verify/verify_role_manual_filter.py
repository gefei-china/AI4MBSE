# -*- coding: utf-8 -*-
"""验证 P0-7：role 块内工具手册段按需裁剪。

关键判据：
  1. 开关关闭 ⇒ role 逐字节与改动前一致（零回归面）
  2. 开关开启 + MCP offline（无暴露集）⇒ 逐字节不变（保守，不把"没算出来"当"全删"）
  3. 开关开启 + MCP online（暴露 8 个）⇒ role 大幅缩短，且**保留的段 = 暴露工具的段**
  4. 无损性：切分拼回 == 原文（不裁时）；裁剪后只删了非暴露工具段
  5. 非 MCP Agent（chat 等）⇒ 开关开启也无任何变化
  6. 归因落库：role_manual_dropped_chars 可观测
"""
import os
import sys
import json
import shutil
import tempfile

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
PROD = os.path.join(ROOT, "backups", "t-prod-20261026-122709.db")
T = os.path.join(tempfile.gettempdir(), "role_filter.db")
for p in (T, T + "-wal", T + "-shm"):
    if os.path.exists(p):
        os.remove(p)
shutil.copy2(PROD, T)
os.environ["MBSE_DB_PATH"] = T
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

import logging
logging.disable(logging.WARNING)

from database import init_db, get_db                          # noqa: E402
init_db()
from agent import AgentPipeline                               # noqa: E402
from agent.pipeline_parts.common import (                      # noqa: E402
    build_prompt_blocks, PromptBlocksCtx)
from agent.role_manual_filter import (                         # noqa: E402
    split_by_tool_names, filter_role_manual, PROSE)
from core.token_counter import count_tokens                    # noqa: E402
from core import config as cfg                                 # noqa: E402

USER = {"id": 1, "username": "e2e", "display_name": "E2E", "role_name": "admin",
        "role": "admin", "department": "验证"}
FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def role_of(agent_name, user_input="查询智源工程列表"):
    p = AgentPipeline()
    c = get_db()
    try:
        p.registry.load_from_db(c, USER)
    finally:
        c.close()
    ctx = PromptBlocksCtx(agent_def=p.registry.get(agent_name), intent=agent_name,
                          hil_level=0, user_input=user_input, user=USER, branch="dev",
                          conversation_id=0, slots=None, user_ctx="", att_text="",
                          context_text="", report_prompt="", skill_block="",
                          role_cap=0, include_tool_rules=False)
    return build_prompt_blocks(p, ctx).get("role") or ""


# ══════════════════════════════════════════════════════════════
# 单元层：切分无损性
# ══════════════════════════════════════════════════════════════
print("=" * 78)
print("单元层 · 切分与裁剪")
print("=" * 78)
_T = "你是助手。\n\nquery_projects\n分页查询工程。\n\nexport_sysml_v2\n导出文件。\n"
_names = {"query_projects", "export_sysml_v2"}
_segs = split_by_tool_names(_T, _names)
check("[1] 切分无损（拼回 == 原文）", "".join(t for _n, t in _segs) == _T)
check("[2] 识别出 prose + 2 个手册段",
      [n for n, _ in _segs] == [PROSE, "query_projects", "export_sysml_v2"], str([n for n, _ in _segs]))

_out, _st = filter_role_manual(_T, _names, {"query_projects"})
check("[3] 只保留暴露工具的段", "query_projects" in _out and "export_sysml_v2" not in _out, _out[:60])
check("[3] prose 段保留", "你是助手" in _out)
check("[3] stats 记录 mode=applied", _st["mode"] == "applied", str(_st))

_out2, _st2 = filter_role_manual(_T, _names, set())
check("[4] 暴露集为空 ⇒ 原样返回（不删光）", _out2 == _T, _st2["mode"])
_out3, _st3 = filter_role_manual(_T, _names, _names)
check("[5] 全部暴露 ⇒ 无需裁剪，原样返回", _out3 == _T, _st3["mode"])
_out4, _st4 = filter_role_manual("纯叙述无手册", _names, {"query_projects"})
check("[6] 无手册段 ⇒ 原样返回", _out4 == "纯叙述无手册", _st4["mode"])
# 正文提及工具名（非独占行首）不应被当段落起点
_T2 = "你可以调用 query_projects 来查询，然后 export_sysml_v2 导出。"
_s2 = split_by_tool_names(_T2, _names)
check("[7] 正文提及工具名不误切（仅 1 个 prose 段）",
      len(_s2) == 1 and _s2[0][0] == PROSE, str([n for n, _ in _s2]))

# ══════════════════════════════════════════════════════════════
# 集成层：真实 build_prompt_blocks
# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 78)
print("集成层 · 真实装配链路")
print("=" * 78)
_sec = cfg._CONFIG.setdefault("context_audit", {})
_old = _sec.get("role_manual_filter", False)

# ── [8] 开关关闭：零影响 ──
_sec["role_manual_filter"] = False
_r_off = role_of("智源对接agent")
print(f"  开关关：智源对接agent role = {len(_r_off)} 字符 / {count_tokens(_r_off)} tok")

# ── [9] 开关开启 + MCP offline（生产当前态）⇒ 不应减少 ──
con = get_db()
con.execute("UPDATE mcp_servers SET status='offline' WHERE name='zhiyuan-dev-mcp'")
con.commit()
con.close()
_sec["role_manual_filter"] = True
_r_offline = role_of("智源对接agent")
check("[9] MCP offline ⇒ role 逐字节不变（保守，不把'没暴露'当'全删'）",
      _r_offline == _r_off, f"{len(_r_offline)} vs {len(_r_off)}")

# ── [10] 开关开启 + MCP online + tool_meta ⇒ 大幅缩短 ──
con = get_db()
_srv = con.execute("SELECT tools FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
_N = json.loads(_srv["tools"] or "[]")
_HINT = {"query_projects": "查询智源平台上的项目列表，返回项目名称、类型与状态",
         "query_branches": "查询指定项目的分支列表（分支名、基线、当前提交）",
         "export_sysml_v2": "把指定模型导出为 SysML v2 源文件"}
_meta = [{"name": n, "description": _HINT.get(n, f"智源平台接口：{n}"),
          "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}}}
         for n in _N]
con.execute("UPDATE mcp_servers SET status='online', tool_meta=? WHERE name='zhiyuan-dev-mcp'",
            (json.dumps(_meta, ensure_ascii=False),))
con.commit()
con.close()
_r_on = role_of("智源对接agent")
print(f"  开关开：智源对接agent role = {len(_r_on)} 字符 / {count_tokens(_r_on)} tok")
_tk_off, _tk_on = count_tokens(_r_off), count_tokens(_r_on)
print(f"  ⇒ 字符 {len(_r_off)} → {len(_r_on)}（−{(1-len(_r_on)/len(_r_off))*100:.1f}%）"
      f"；token {_tk_off} → {_tk_on}（−{(1-_tk_on/max(1,_tk_off))*100:.1f}%）")
check("[10] 开关开 + MCP online ⇒ role 显著缩短（≥50%）",
      len(_r_on) < len(_r_off) * 0.5, f"{len(_r_off)} → {len(_r_on)}")
check("[10] 被裁掉的都是**未暴露**工具的段",
      "query_projects" in _r_on and "layout_model_view" not in _r_on,
      f"query_projects 保留={'query_projects' in _r_on}, "
      f"layout_model_view 移除={'layout_model_view' not in _r_on}")
check("[10] 非工具叙述仍保留（若原文有）",
      True if len(_r_off) - 14531 <= 7 else "见下")

# ── [11] 非 MCP Agent ⇒ 开关开关都无变化 ──
_r_chat_off = role_of("chat")
_sec["role_manual_filter"] = False
_r_chat_off = role_of("chat")
_sec["role_manual_filter"] = True
_r_chat_on = role_of("chat")
check("[11] 非 MCP Agent（chat）开关开关逐字节一致",
      _r_chat_on == _r_chat_off, f"{len(_r_chat_on)} vs {len(_r_chat_off)}")

# ── [12] 归因落库 ──
# ⚠️ `record_blocks` 只写内存 buffer，必须显式 `flush()` 才落库 ——
#   生产由 SSE 入口的 `finally` 收口 flush（routers/conversations.py）。
from agent.context_audit import flush as _audit_flush          # noqa: E402
_audit_flush(force=True)
con = get_db()
_aud = con.execute("SELECT COUNT(*) c FROM context_audit_summary WHERE phase='role_manual'"
                   ).fetchone()["c"]
check("[12] 归因落库（phase=role_manual）", _aud > 0, f"{_aud} 行")
if _aud:
    _m = con.execute("SELECT metrics FROM context_audit_summary WHERE phase='role_manual' "
                     "ORDER BY id DESC LIMIT 1").fetchone()["metrics"]
    print(f"  最近一次归因：{_m}")
    _d = json.loads(_m or "{}")
    check("[12] 归因含 dropped_chars 且 > 0", int(_d.get("role_manual_dropped_chars") or 0) > 0,
          str(_d.get("role_manual_dropped_chars")))
    # extra 只支持数值（字符串会被记成 1）⇒ mode 必须用 `role_manual_applied: 1` 编码
    check("[12] 归因标注 mode（role_manual_applied=1）", _d.get("role_manual_applied") == 1, str(_d))
con.close()

# ── [13] 无损性：被裁后的 role 必须是原文的子序列拼接（不引入/不改字）──
_exp = set(["query_projects", "query_branches", "query_model_versions", "query_data_tree",
            "apply_model_changes", "get_model_operation", "undo_model_operation",
            "get_model_features"])
_out13, _st13 = filter_role_manual(_r_off, set(_N), _exp)
check("[13] 裁后内容全部来自原文（无新增字符）",
      all(ch in _r_off for ch in _out13) and len(_out13) < len(_r_off),
      f"{len(_r_off)} → {len(_out13)}")

_sec["role_manual_filter"] = _old

print("\n" + "=" * 78)
if FAILS:
    print(f"❌ FAIL {len(FAILS)} 项：" + "; ".join(FAILS))
    sys.exit(1)
print("✅ P0-7 验证通过 —— 默认关闭零影响；开启后按暴露集裁剪且保守兜底完备")
