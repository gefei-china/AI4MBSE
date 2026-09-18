"""MockLLM（无 API key 时的确定性兜底）。

自 llm.py 原样搬移（P1-3 插件化），仅增加 BaseLLM 继承。
用于：测试回归（MBSE_LLM_FORCE_MOCK）、无 key 环境、真实调用失败降级。
"""
import json
import time
from typing import Optional

from ..base import BaseLLM


class MockLLM(BaseLLM):
    """Mock LLM for testing without real API keys."""

    name = "mock"

    def chat(self, messages, model=None, temperature=0.3, max_tokens=4096, stream=False, tools=None, thinking=False, **kwargs):
        user_msg = messages[-1]["content"] if messages else ""
        intent = self._detect_intent(user_msg)
        if intent == "requirement_analysis":
            content = self._mock_requirement(user_msg)
        elif intent == "design":
            content = self._mock_design(user_msg)
        elif intent == "impact":
            content = self._mock_impact(user_msg)
        elif intent == "review":
            content = self._mock_review(user_msg)
        else:
            content = self._mock_chat(user_msg)

        if stream:
            return self._stream(content, thinking=thinking)
        # 缺口B：Mock 模式下模拟工具调用（测试专用）——用户消息含「调用工具」且声明了 tools 时返回 tool_calls
        if tools and ("调用工具" in user_msg or "use tool" in user_msg.lower()):
            fn = tools[0]["function"]["name"]
            return {"choices": [{"message": {"role": "assistant",
                                              "content": None,
                                              "tool_calls": [{"id": "call_mock_1", "type": "function",
                                                              "function": {"name": fn, "arguments": "{\"query\":\"宽带通信\"}"}}]}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
        msg = {"role": "assistant", "content": content}
        if thinking:
            msg["reasoning_content"] = (
                "（Mock 深度思考）先理解任务目标，再梳理可用节点类型与工具/Agent 清单，"
                "规划数据流走向，评估分支条件，最后生成结构化流程定义。"
            )
        return {"choices": [{"message": msg}],
                "usage": {"prompt_tokens": 100, "completion_tokens": len(content) // 4}}

    def _detect_intent(self, text):
        t = text.lower()
        # 建模/设计信号优先：含明确建模意图（SysML/设计模型/建模）时即使提到需求也按设计走
        if any(k in t for k in ["sysml", "sysml v2", "设计模型", "建模", "生成设计", "模型设计"]):
            return "design"
        if any(k in t for k in ["需求", "解析", "条目", "requirement"]):
            return "requirement_analysis"
        if any(k in t for k in ["方案", "设计", "design", "架构"]):
            return "design"
        if any(k in t for k in ["变更", "影响", "impact", "change"]):
            return "impact"
        if any(k in t for k in ["校验", "评审", "review", "检查"]):
            return "review"
        return "chat"

    def _mock_requirement(self, text):
        return """已识别意图：系统需求分析（BR-3）

已从知识库检索到 12 条互联数据（图谱 9 / 向量 3），解析出候选需求条目 28 条。

以下为中间结果预览，请逐条确认后方可入库：

| ID | 需求陈述（规范化建议后） | 来源追溯 | 规范性评分 |
|---|---|---|---|
| REQ-BC-001 | 系统应在 V 波段提供不小于 2 Gbps 的用户链路吞吐能力 | 任务书 §3.2 | 92 |
| REQ-BC-002 | 系统应支持不少于 100 万用户的并发接入 | 任务书 §3.4 | 88 |
| REQ-BC-003 | 载荷应具备在轨可重构能力 | 任务书 §4.1 | 61 |

⚠ 冲突检测：REQ-BC-002 与已入库 REQ-A-117「并发接入 ≥ 50 万」冲突，已高亮潜在重复项。

需求图生成方案对比：
- A · 按任务域分包（完整95 可读90 追溯93）✓ 推荐
- B · 按优先级分层（完整90 可读94 追溯88）
- C · 按接口归属（完整86 可读85 追溯91）"""

    def _mock_design(self, text):
        return """已识别意图：系统方案设计（BR-4）

基于已确认的 28 条需求条目，生成 3 个方案供对比：

方案 A · 透明转发架构
- 组成：波束成形载荷 + 透明转发器 + 地面网关
- 优势：实现简单、时延低、成熟度高
- 代价：无星上处理能力，灵活性受限

方案 B · 星上处理架构（推荐）
- 组成：可重构载荷 + 星上基带处理 + 动态路由
- 优势：支持在轨重构、多波束灵活调度、满足 REQ-BC-003
- 代价：星上算力/功耗成本上升

方案 C · 混合架构
- 组成：透明转发 + 部分星上处理
- 优势：兼顾成本与灵活性
- 代价：系统复杂度最高

推荐：方案 B —— 完整满足 28/28 条需求（含在轨可重构 REQ-BC-003），综合权衡最优。

推荐的 SysML v2 模型代码：
```sysml
part def 载荷 {
  attribute mass = 18kg;
}
part def 转发器 {
  attribute bandwidth = 2GHz;
}
part def 天线 { }
part def TWTA { }
part 载荷 : 载荷;
part 转发器 : 转发器;
requirement def 高速率需求 {
  attribute text = "系统应在 V 波段提供不小于 2 Gbps 的用户链路吞吐能力";
}
satisfy 高速率需求 by 载荷;
connector 载荷 to 转发器;
```
"""

    def _mock_impact(self, text):
        return """已识别意图：变更影响分析（BR-6）

变更源：REQ-BC-002（并发接入 ≥ 100 万）→ 沿关系链 BFS 遍历，影响范围如下：

直接受影响（3）：
1. 载荷容量设计（derive 派生）→ 需扩容
2. 星地链路预算（satisfy 满足）→ 需重算
3. 地面网关集群（allocate 分配）→ 需扩容

间接受影响（5）：
1. 电源系统（依赖载荷扩容）→ 功耗上升
2. 热控系统（依赖电源）→ 散热需求增加
3. 卫星平台重量（依赖载荷）→ 发射成本
4. 地面信关站数量（依赖网关集群）→ 建设成本
5. 运维体系（依赖集群规模）→ 运营成本

风险等级：高 —— 变更将传导至电源/热控等物理层，建议评估周期 2 周。"""

    def _mock_review(self, text):
        return """已识别意图：预评审校验（BR-7）

对当前模型执行规范性/一致性/合理性校验，结论：有条件通过（78/100）

问题清单（4）：
1. [错误] REQ-BC-003「在轨可重构」缺量化验收标准 → 建议补充指标
2. [警告] 载荷块未定义端口类型 → 建议补齐标准端口
3. [警告] 存在 2 个孤立元素未连接 → 建议挂接关系
4. [提示] 术语「信关站」与 glossary 未归一 → 建议统一

修复建议：按上述问题清单逐项修订后复评。"""

    def _mock_chat(self, text):
        return (
            f"（Mock 回答）已收到你的消息：{text[:60]}{'…' if len(text) > 60 else ''}\n"
            "当前为 Mock 降级模式（未配置真实 LLM API Key）。"
            "可在「AI 设计工坊 → LLM 配置」添加 provider 后获得真实回答。"
        )

    def _stream(self, content, thinking=False):
        import time as _t
        for i in range(0, len(content), 6):
            _t.sleep(0.005)
            yield f"data: {json.dumps({'choices': [{'delta': {'content': content[i:i + 6]}}]}, ensure_ascii=False)}\n\n"
        if thinking:
            yield "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"（Mock 思考完成）\"}}]}\n\n"
        yield "data: [DONE]\n\n"
