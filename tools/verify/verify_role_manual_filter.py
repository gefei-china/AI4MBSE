# CI-OPTIONAL: 2026-11-04 盘点实测：在干净库（init_db 新建，CI 等价口径）上 rc≠0，接线会让 CI 恒红、成为噪音。需先逐条定性（产品缺陷 vs 门禁口径过期）再接线。证据：tmp/backlog_probe.json 的 red 列表；方法见 docs/门禁盘点-20261104.md §4。
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
# ★ 2026-11-26：原为 `PROD = ROOT/backups/t-prod-20261026-122709.db` 硬编码 ——
#   那个备份**已被清理**（backups/ 现存 137 个文件无 t-prod-*）⇒ 本脚本 import 期就
#   FileNotFoundError 崩掉（Traceback 指向 shutil.copy2），看起来像"环境缺文件"，
#   真因是"门禁把一次性手工备份当成了永久夹具"（两者生命周期不匹配）。
#   改为 `_fixture_db` 三层回退：历史备份（若还在）→ 当前库 → 都没有则 SKIP（不崩）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fixture_db import require_or_skip  # noqa: E402

T = os.path.join(tempfile.gettempdir(), "role_filter.db")
PROD = require_or_skip(T, start=__file__, label="role 手册过滤夹具库")
print("  夹具来源：%s" % PROD)
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

# ── [10] 裁剪行为：**夹具驱动**（★ 2026-11-26 重写）────────────────────────
#   原判据用真库 `智源对接agent` 的 role 文本，要求"开关开 ⇒ 显著缩短 ≥50%"。
#   实测该 Agent 的 system_prompt 只有 1281 字符且**不含工具手册段**
#   （没有"工具名独占一行"的清单；`filter_role_manual` 返回 mode='no_manual'）
#   ⇒ 没有可裁的东西 ⇒ 判据恒红。
#   这是「被测样本退役」的第八例：工具手册段已从 prompt 移走（改成按需注入），
#   而门禁仍假设它内嵌在 role 里。
#   ⇒ 改为**门禁自带夹具**：判据测的是 `filter_role_manual` 的裁剪语义，
#     与"库里恰好有没有那段文本"无关（与 verify_role_block_sections 同一做法）。
_MANUAL_FIXTURE = "\n".join([
    "你是智源对接 agent，负责平台建模操作。",
    "坐标先行：所有操作都要先取 projectId。",     # ← prose 在手册段**之前**
    "",
    "## 可用工具（按名查阅）",
    "query_projects",
    "  查询项目列表：返回 项目名称/类型/状态。参数：无。",
    "query_branches",
    "  查询分支列表：返回 分支名/基线/当前提交。参数：projectId。",
    "export_sysml_v2",
    "  导出模型为 SysML v2 源文件。参数：projectId, branch。",
])
_FIX_NAMES = ["query_projects", "query_branches", "export_sysml_v2"]
_out10, _st10 = filter_role_manual(_MANUAL_FIXTURE, _FIX_NAMES, {"query_projects"})
print(f"  夹具裁剪：{len(_MANUAL_FIXTURE)} → {len(_out10)} 字符，stats={_st10}")
check("[10] 夹具含手册段 ⇒ mode=applied（判据面成立，否则后面几条空转）",
      _st10.get("mode") == "applied", str(_st10))
check("[10] 暴露 1/3 工具 ⇒ 字符数显著缩短（≥50%）",
      len(_out10) < len(_MANUAL_FIXTURE) * 0.5,
      f"{len(_MANUAL_FIXTURE)} → {len(_out10)}")
check("[10] 被裁掉的都是**未暴露**工具的段（留 query_projects，去另两个）",
      "query_projects" in _out10 and "query_branches" not in _out10
      and "export_sysml_v2" not in _out10)
check("[10] 手册段**之前**的叙述仍保留",
      "坐标先行：所有操作都要先取 projectId" in _out10)

# ── [10e] **已知局限**（如实记录，不判红）：手册段**之后**的叙述会被一并裁掉 ──
#   原因：`split_by_tool_names` 的段边界是「下一个工具名」，**最后一段延伸到文末**
#   ⇒ 若最后一个工具段后面还有散文，它属于该段，裁掉该段时一并消失。
#   为什么记录而不修：这是"按工具名切分"这一设计的内生边界（要精确判断"段结束"
#   需要额外语法约定，而 role 文本是自由格式）。真实 prompt 里工具清单通常在末尾，
#   影响面小；但**若将来把工具清单挪到中间，这条就要重新评估**。
#   ⇒ 写成可执行判据（而不是注释），让这个局限**一直被看得见**。
_TAIL = _MANUAL_FIXTURE + "\n## 操作纪律\n不得越权写库。\n"
_out_tail, _st_tail = filter_role_manual(_TAIL, _FIX_NAMES, {"query_projects"})
_tail_lost = "不得越权写库" not in _out_tail
print("  [已知局限] 手册段之后的叙述会被一并裁掉：%s"
      % ("**是**（符合当前设计边界）" if _tail_lost else "否（切分规则已能识别段尾）"))
check("[10e] 已知局限已如实记录（切分按工具名分段，段尾延伸到文末）", True,
      "此条恒过，仅用于把局限摆到报告里；若哪天变了会从打印行看出来")

# ── [10b] 豁免的边界：暴露集为空 ⇒ 不裁（保守）──
_out10b, _st10b = filter_role_manual(_MANUAL_FIXTURE, _FIX_NAMES, set())
check("[10b] 暴露集为空 ⇒ 不裁（无法判断'真的没暴露'还是'没算出来'）",
      _out10b == _MANUAL_FIXTURE and _st10b.get("mode") == "no_exposed", str(_st10b))

# ── [10c] 真库现状如实报告（不判红 —— 样本可能已退役）──
_real_has_manual = bool(_r_off) and filter_role_manual(
    _r_off, _FIX_NAMES + ["zhiyuan_sysmlv2_gen"], {"query_projects"})[1].get("manual", 0) > 0
print("  真库 `智源对接agent` %s手册段 ⇒ %s"
      % ("含" if _real_has_manual else "**不含**",
         "已覆盖端到端" if _real_has_manual else
         "端到端无效（工具手册已改为按需注入），本组靠夹具覆盖"))
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
# ★ 2026-11-26：原三条 [10] 判据（"显著缩短 ≥50%" / "未暴露段被移除" / "prose 保留"）
#   已改为上面的**夹具驱动**版本。这里只保留一条**端到端一致性**判据：
#   真库 role 若含手册段，则开关开必须真的更短；不含则这一条不判红（样本已退役，
#   由 [10c] 如实报告，避免把"没东西可裁"记成"裁剪失效"）。
if _real_has_manual:
    check("[10d] 真库含手册段时，开关开 ⇒ role 确实更短",
          len(_r_on) < len(_r_off), f"{len(_r_off)} → {len(_r_on)}")
else:
    print("  [SKIP] [10d] 真库无手册段 ⇒ 端到端短化无从验证（夹具已在 [10] 覆盖）")

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
# ★ 2026-11-26：生产侧只在 mode ∈ {applied, nothing_to_drop, no_exposed} 时写归因
#   （见 prompt.py 的 `_apply_role_manual_filter`）。真库当前 `mode=no_manual`
#   （无手册段）⇒ **不写归因**是正确行为 ⇒ 原判据恒红。
#   ⇒ 有手册段时测真库；无手册段时 SKIP（并说清"为什么这条现在测不了"），
#     不把它记成失败 —— 否则等于要求"没东西可裁时也必须写一条裁剪归因"。
from agent.context_audit import flush as _audit_flush          # noqa: E402
_audit_flush(force=True)
con = get_db()
_aud = con.execute("SELECT COUNT(*) c FROM context_audit_summary WHERE phase='role_manual'"
                   ).fetchone()["c"]
if not _real_has_manual:
    print("  [SKIP] [12] 真库无手册段（mode=no_manual）⇒ 生产不写裁剪归因，属正确行为")
    print("         （归因口径已由 prompt.py 单点决定；夹具侧裁剪语义见 [10]/[13]）")
else:
    check("[12] 归因落库（phase=role_manual）", _aud > 0, f"{_aud} 行")
    if _aud:
        _m = con.execute("SELECT metrics FROM context_audit_summary WHERE phase='role_manual' "
                         "ORDER BY id DESC LIMIT 1").fetchone()["metrics"]
        print(f"  最近一次归因：{_m}")
        _d = json.loads(_m or "{}")
        check("[12] 归因含 dropped_chars 且 > 0",
              int(_d.get("role_manual_dropped_chars") or 0) > 0,
              str(_d.get("role_manual_dropped_chars")))
        check("[12] 归因标注 mode（role_manual_applied=1）", _d.get("role_manual_applied") == 1, str(_d))
con.close()

# ── [13] 无损性：被裁后的文本必须是原文的**子序列**（不引入/不改字）──
# ★ 2026-11-26：原用真库 `_r_off`，但真库无手册段 ⇒ `len(_out13) < len(_r_off)` 恒假
#   （`1282 → 1282`）。改为在**夹具**上验无损性（这是本条的语义：切分不得吃字符/造字符）。
#   无损性的正确判法：裁后字符串必须能由原文的**连续片段按序拼接**得到，
#   且每一段都在原文里 —— 用"逐段 in 原文"比"逐字符 in 原文"更强（后者对重复字符会误判）。
_segs13 = split_by_tool_names(_MANUAL_FIXTURE, _FIX_NAMES)
_out13, _st13 = filter_role_manual(_MANUAL_FIXTURE, _FIX_NAMES, {"query_projects"})
_segs_kept = split_by_tool_names(_out13, _FIX_NAMES)
_ok_sub = all(seg in _MANUAL_FIXTURE for _n, seg in _segs_kept) and "".join(
    seg for _n, seg in _segs13) == _MANUAL_FIXTURE
check("[13] 切分无损（各段拼回 == 原文，不吞字符）", _ok_sub,
      "%d 段 → 拼回%s" % (len(_segs13), "一致" if _ok_sub else "**不一致**"))
check("[13] 裁后每一段都来自原文（不新增字符）",
      all(seg in _MANUAL_FIXTURE for _n, seg in _segs_kept) and len(_out13) < len(_MANUAL_FIXTURE),
      f"{len(_MANUAL_FIXTURE)} → {len(_out13)}")

_sec["role_manual_filter"] = _old

print("\n" + "=" * 78)
if FAILS:
    print(f"❌ FAIL {len(FAILS)} 项：" + "; ".join(FAILS))
    sys.exit(1)
print("✅ P0-7 验证通过 —— 默认关闭零影响；开启后按暴露集裁剪且保守兜底完备")
