#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_final_content_dedupe.py —— 「最终正文只承载最后确定的代码」门禁（2026-11-02）。

背景（米爸反馈）：自动编排各子任务交付物**各自带一份完整模型代码**，汇总原样带入最终
正文 ⇒ 用户看到 3 份 ```sysml + 重复章节（真库实证 conv 9999 / msg 5689，32308 字）。
需求原话：「逻辑上只需要承载最后确定的代码，不应该把中间环节的代码放在这里」。

本门禁三层：
  A. 算法层 —— 收敛判据的正确性（演进收敛 / 不同模型不误杀 / 幂等）；
  B. 挂钩层 —— 两条编排出口（_summarize_plan、stream.py 收尾）必须调用 dedupe；
  C. 端点层 —— 会话删除必须复用请求连接（30s 自锁回归防护）。

运行：python tools/verify/verify_final_content_dedupe.py
"""
import io
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.final_content import dedupe_interim_code_blocks  # noqa: E402

_RESULTS = []


def ck(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


def _pad(pre, n):
    return "\n".join(
        f"\tcomment {pre}{i} 说明该元素承担的职责与约束，用于撑起真实模型的体量;"
        for i in range(n))


def main():
    print("=" * 90)
    print("「最终正文只承载最后确定的代码」门禁")
    print("=" * 90)

    v1 = "package '运行场景定义' {\n\tpart def '平台';\n\tuse case def '车辆就绪_定义';\n" + _pad('v1', 30) + "\n}"
    v2 = ("package '运行场景定义' {\n\tpart def '平台';\n\tuse case def '车辆就绪_定义';\n"
          "\tuse case def '驾驶循环_定义';\n\tcomment 修正了 include 语义;\n" + _pad('v2', 32) + "\n}")
    v3 = ("package '运行场景定义' {\n\tpart def '平台';\n\tuse case def '车辆就绪_定义';\n"
          "\tuse case def '驾驶循环_定义';\n\tuse case def '低电量驾驶_定义';\n\trender '用例视图';\n"
          + _pad('v3', 34) + "\n}")

    # ── A. 算法层 ──
    t1 = "# 报告\n分析段\n```sysml\n" + v1 + "\n```\n\n## 诊断\n```sysml\n" + v2 + \
         "\n```\n\n## 最终\n```sysml\n" + v3 + "\n```\n结论"
    out1, n1 = dedupe_interim_code_blocks(t1)
    ck("A1 同 package 三版演进 → 收敛 2 份、只留最后版",
       n1 == 2 and out1.count("```sysml") == 1 and v3 in out1 and v1 not in out1,
       f"removed={n1}")
    ck("A2 收敛后带指引行（指向子任务执行卡）", "中间版本代码已收敛" in out1 and "子任务执行卡" in out1)

    t2 = "```mermaid\ngraph TD; A-->B;\n```\n```sysml\n" + v3 + "\n```\n```json\n{\"k\": 1}\n```"
    out2, n2 = dedupe_interim_code_blocks(t2)
    ck("A3 不同交付物（mermaid/json/sysml 各一块）不误杀", n2 == 0 and out2 == t2)

    t3 = "```sysml\n" + v3 + "\n```\n中段\n```sysml\n" + v3 + "\n```"
    out3, n3 = dedupe_interim_code_blocks(t3)
    ck("A4 完全相同的重复块收敛为 1 份", n3 == 1 and out3.count("```sysml") == 1)

    va = "package '需求模型' {\n\tpart def '整车';\n\trequirement def '续航';\n" + _pad('a', 30) + "\n}"
    vb = "package '架构模型' {\n\tpart def '电池包';\n\tpart def '电机';\n" + _pad('b', 30) + "\n}"
    t4 = "```sysml\n" + va + "\n```\n```sysml\n" + vb + "\n```"
    out4, n4 = dedupe_interim_code_blocks(t4)
    ck("A5 不同 package 的两个模型**不**收敛（样板相似也不误杀）", n4 == 0 and out4 == t4)

    t5 = "```sysml\npart def A;\n```\n```sysml\npart def A;\npart def B;\n```"
    _, n5 = dedupe_interim_code_blocks(t5)
    ck("A6 短片段（<400 字符）保守不收敛", n5 == 0)

    base = "\n".join(f'\tpart def "P{i}" : "基类";' for i in range(60))
    c1 = "package '大模型' {\n" + base + '\n\tpart def "补充1" : "基类";\n}'
    c2 = "package '大模型' {\n" + base + '\n\tcomment 新增需求映射段;\n\tpart def "补充2" : "基类";\n}'
    c3 = ("package '大模型' {\n" + base + '\n\tcomment 二次修订;\n'
          '\tpart def "补充3" : "基类";\n\trender 最终视图;\n}')
    t6 = "```sysml\n" + c1 + "\n```\n```sysml\n" + c2 + "\n```\n```sysml\n" + c3 + "\n```"
    out6, n6 = dedupe_interim_code_blocks(t6)
    ck("A7 链式演进（t1≈t2≈t3，t1 vs t3 相似度不足）收敛 2 份", n6 == 2 and out6.count("```sysml") == 1)

    out7, n7 = dedupe_interim_code_blocks(out1)
    ck("A8 幂等：收敛结果再跑一遍不再变化", n7 == 0 and out7 == out1)

    _, n8 = dedupe_interim_code_blocks("```sysml\n" + v1 + "\n```")   # 单块
    _, n9 = dedupe_interim_code_blocks("no fences at all")
    ck("A9 单块/无围栏安全返回", n8 == 0 and n9 == 0)

    # ── B. 挂钩层 ──
    planner_src = (ROOT / "workflows" / "planner.py").read_text(encoding="utf-8")
    ck("B1 _summarize_plan 出口已挂 dedupe（LLM 整合路径）",
       "dedupe_interim_code_blocks" in planner_src)
    ck("B2 汇总 prompt 已加「只保留最后确定版本」规则",
       "最后一个确定版本" in planner_src)
    ck("B3 拼接降级出口同样收敛（降级 return 在 try 外独立执行）",
       len(re.findall(r"dedupe_interim_code_blocks", planner_src)) >= 2)

    stream_src = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
    m_push = re.search(r"\n(\s*)for _chunk in iter_stream_chunks\(orch_content\)", stream_src)
    m_dedup = re.search(r"\n(\s*)orch_content, _fc_removed = _dedupe_fc\(orch_content\)", stream_src)
    ok_order = bool(m_push and m_dedup and m_dedup.start() < m_push.start())
    ck("B4 流式路径：dedupe 位于流式推送**之前**（推送内容即收敛后）", ok_order)
    m_ensure = re.search(r"orch_content = self\._ensure_sysml_blocks\(orch_content, done_items\)", stream_src)
    ck("B5 dedupe 在 _ensure_sysml_blocks 之前（补回兜底仍有效）",
       bool(m_ensure and m_dedup and m_dedup.start() < m_ensure.start()))

    # ── C. 端点层（30s 自锁回归防护）──
    router_src = (ROOT / "routers" / "conversations.py").read_text(encoding="utf-8")
    ck("C1 删除端点级联清理**复用请求连接**（不传 conn 会自己等自己的写锁 30s）",
       re.search(r"cleanup_conversation\(conv_id,\s*conn=conn\)", router_src) is not None)
    offload_src = (ROOT / "agent" / "pipeline_parts" / "tool_offload.py").read_text(encoding="utf-8")
    ck("C2 cleanup_conversation 支持 conn 参数（同事务执行）",
       "def cleanup_conversation(conversation_id: int, conn=None)" in offload_src)

    # ── 汇总 ──
    fails = [r for r in _RESULTS if not r[1]]
    print("=" * 90)
    print(f"断言汇总：{len(_RESULTS) - len(fails)}/{len(_RESULTS)} 通过")
    if fails:
        for name, _, detail in fails:
            print("  ✗ " + name + (f"  | {detail}" if detail else ""))
        return 1
    print("✅ ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
