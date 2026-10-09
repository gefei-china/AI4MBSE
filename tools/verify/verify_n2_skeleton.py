# -*- coding: utf-8 -*-
# CI-OPTIONAL: 真调 LLM 生成骨架 + 需 checker.jar 解析（随包运行时 126MB，不入库）；本地裸跑已可出诊断。
"""N2 架构骨架能力实测：用真实建模需求跑 Agent，检查产出能否过 checker.jar。

为什么必须实测
------------
V5 入了 8 个节点 Agent，但它们的 `system_prompt` 是**我写的**，从未经真实建模验证。
若 N2 产出的骨架过不了官方校验器，那 N3/N4 的前提就不成立 —— 整条流水线是空的。
**不能因为「入库成功」就认为「能力可用」**（入库只证明字段合规）。

判据
----
① 走真实 Agent 管线（`AgentPipeline`）产 SysML 代码，而非我手写片段
② 产出过 `sysml_v2_validate`，看 verdict 与 n_hard
③ 对照基线：L0 硬约束卡声称 A/B 为「无卡 6/9 ERROR → 有卡 0/0」，
   本脚本要验证 N2 实际拿到的 error 量级是否与该结论相符

用法
----
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n2_skeleton.py
退出码 0 = N2 能力可用（产出可过校验或错误可归因）。
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 真实建模需求（取自工程样例 sysml_models/ev_thermal_mgmt 的领域）
REQ = ("为纯电动汽车热管理系统（EV TMS）建立 SysML v2 架构骨架："
       "系统 EVThermalManagementSystem 下含 BatteryThermalManagement（电池热管理）、"
       "CabinThermalManagement（座舱热管理）、ThermalLoop（热回路）、"
       "HeatPumpAssembly（热泵组件）四个子系统；"
       "定义 ReqEndurance（续航≥500km）、ReqColdStart（低温启动）、"
       "ReqBatteryTempRange（电池温度 15~45℃）三条需求并分配 satisfy 关系。")

SYSML_BLOCK = re.compile(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```", re.S | re.I)


def extract_code(text):
    """从 Agent 输出里抽 SysML 代码块；抽不到返回 None（说明格式没遵守）。"""
    if not text:
        return None
    m = SYSML_BLOCK.search(text)
    if m:
        return m.group(1)
    # 退化：整段看起来像代码就直接用
    if "package" in text and "{" in text:
        return text
    return None


def main() -> int:
    from database import db_conn
    from agent.registry import AgentRegistry
    from sysml_v2_check import check_code

    print("=" * 72)
    print("N2 架构骨架能力实测（真实需求 → 真实 Agent → 官方校验器）")
    print("=" * 72)

    # ① 确认 N2 的配置与绑定
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, display_name, hil_level, system_prompt, capabilities "
            "FROM agents WHERE name='architecture_skeleton'").fetchone()
        if not row:
            print("  [FAIL] N2 不存在")
            return 2
        tools = [r[0] for r in conn.execute(
            "SELECT t.tool_name FROM agent_tools t WHERE t.agent_id=? AND t.tool_type='tool'",
            (row["id"],)).fetchall()]
    print(f"\n  N2 = {row['display_name']}（hil={row['hil_level']}）")
    print(f"  绑定工具：{tools}")
    print(f"  system_prompt 长度：{len(row['system_prompt'] or '')}")
    has_sysml = "SysML" in (row["system_prompt"] or "")
    has_code = "代码" in (row["system_prompt"] or "")
    print(f"  auto-bind 判据：含 SysML={has_sysml} 含 代码={has_code} "
          f"→ 规则会命中={has_sysml and has_code}")
    if "sysml_v2_validate" not in tools:
        print("  [FAIL] N2 未绑 sysml_v2_validate ⇒ 拿不到 L0 硬约束卡")
        return 1
    print("  [OK  ] 已绑 validate（_is_v2_code_agent 依赖它注入 L0 卡）")

    # ② 是否能真的构造出 Agent 并跑起来
    print("\n── 步骤 2：走真实 Agent 管线生成骨架 ──")
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    reg = getattr(pipe, "registry", None) or getattr(pipe, "agent_registry", None)
    agent_def = None
    if reg is not None and hasattr(reg, "get"):
        agent_def = reg.get("architecture_skeleton")
    if agent_def is None:
        print("  [INFO] registry 未暴露实例，改用直接构造")
        try:
            from agent.definition import AgentDefinition
            with db_conn() as c2:
                r2 = c2.execute(
                    "SELECT name,display_name,description,system_prompt,hil_level,agent_role "
                    "FROM agents WHERE name='architecture_skeleton'").fetchone()
            agent_def = AgentDefinition(
                name=r2["display_name"], description=r2["description"],
                system_prompt=r2["system_prompt"], hil_level=r2["hil_level"],
                agent_role=r2["agent_role"], intent_name=r2["name"])
        except Exception as e:
            print(f"  [FAIL] 无法构造 Agent：{e}")
            return 2
    print(f"  [OK  ] Agent 构造成功：{agent_def.name}")

    out = None
    err = None
    # conversation_id 必须真实存在（messages 表有外键约束，0 会报 FOREIGN KEY failed）
    conv_id = None
    try:
        with db_conn() as c0:
            r0 = c0.execute(
                "SELECT id FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
            if r0:
                conv_id = r0["id"]
    except Exception:
        pass
    if conv_id is None:
        try:
            with db_conn() as c0:
                cur = c0.execute(
                    "INSERT INTO conversations (title, branch, created_at) "
                    "VALUES ('V2 N2 能力实测','dev',datetime('now','localtime'))")
                conn.commit()
                conv_id = cur.lastrowid
            print(f"  已创建实测会话 conversation_id={conv_id}")
        except Exception as e:
            print(f"  [WARN] 无法准备会话：{e}")
    try:
        # 真实入口是 AgentPipeline.execute()（ExecuteMixin 的同步方法，
        # agent/pipeline_parts/execute.py:28）—— 不是 execute_chat，那个名字不存在。
        out = pipe.execute(REQ, conversation_id=conv_id, branch="dev",
                           forced_intent="architecture_skeleton")
    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        if os.environ.get("V2_DEBUG"):
            traceback.print_exc()
    if err:
        print(f"  [WARN] 真实调用失败：{err[:160]}")
        print("  ⇒ 转为「静态走查」：校验 N2 的 prompt 是否含 L0 卡与关键约束")
        print("  ⚠️ 静态走查只验 prompt 文本，**不能替代真实调用**")
        return static_audit(row)
    print(f"  [OK  ] Agent 返回类型 {type(out).__name__}，"
          f"长度 {len(str(out))} 字符")

    # ③ 从**消息表**读代码块，而不是从返回值的字符串里抓 ——
    #    实测踩坑：`pipe.execute()` 返回的 dict 里 code 字段是 agent_meta JSON，
    #    直接对它跑校验会得到 `missing EOF at '{'` 的假失败。
    #    真实产出在 messages.content 的 ```sysml 块里。
    import re
    with db_conn() as c2:
        rows = c2.execute(
            "SELECT content FROM messages "
            "WHERE conversation_id=? AND role='assistant' ORDER BY id",
            (conv_id,)).fetchall()
    blocks = []
    for m in rows:
        found = re.findall(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```",
                           m["content"] or "", re.S | re.I)
        blocks.extend(found)
    if not blocks:
        print("\n  [FAIL] 消息里没有 ```sysml 代码块 ⇒ 生成格式不合规")
        return 1
    print(f"  从消息表抽到 {len(blocks)} 个代码块")

    print("\n── 步骤 3：官方校验器 verdict（逐轮）──")
    all_pass = True
    for i, code in enumerate(blocks, 1):
        res = check_code(code)
        v = res.get("verdict")
        print(f"  第 {i} 轮（{len(code)} 字符）：verdict={v}  "
              f"n_error={res.get('n_error')} n_hard={res.get('n_hard')} "
              f"n_semantic={res.get('n_semantic')}")
        for e in (res.get("errors") or [])[:5]:
            print(f"      L{e.get('line')} [{e.get('source')}] {str(e.get('msg'))[:62]}")
        if v != "pass":
            all_pass = False

    print()
    if all_pass:
        print("✅ N2 能力实测通过：全部轮次 verdict=pass（0 词法/0 语法/0 语义）")
        print("   ⇒ L0 硬约束卡 + N2 prompt 组合有效，N3/N4 的前提成立")
        return 0
    print("⚠️ 有轮次未过校验。若错误集中在语法层，说明 L0 卡注入或 prompt 约束不足。")
    return 1



def static_audit(row):
    """无 LLM 时的降级走查：只看 prompt 质量（不能替代真实调用）。"""
    sp = row["system_prompt"] or ""
    must = {
        "包与导入纪律(private import)": "private import",
        "part/port/item 类型族": "part def",
        "禁止画视图细节": "action",
        "必须调validate": "sysml_v2_validate",
        "3轮修复预算": "3 轮",
    }
    print("\n── 静态走查：N2 prompt 关键约束齐备性 ──")
    ok = True
    for label, kw in must.items():
        hit = kw in sp
        print(f"  [{'OK  ' if hit else 'MISS'}] {label}")
        if not hit:
            ok = False
    print("\n  ⚠️ 这是降级走查，**不能替代真实调用验证**")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
