# -*- coding: utf-8 -*-
"""意图评测集**内置种子**（29 例 / 9 类意图）—— 2026-09-26 从 eval_intent_routing.py 抽出。

为什么抽出来：样本池（`intent_samples` 表）的"内置种子导入"需要同一份清单，
而评测脚本需要它作 DB 不可用时的回退。**两份清单会漂移**，故收敛到这一个**无副作用**的数据模块
（原脚本在模块级就跑完整个评测，不可被 import）。

⚠️ 这里只放"人工整理的标注"（可视为已 confirmed）。真实生产说法请走样本池：
   设置 → 意图样本 → 从历史回填 → 逐条确认（详见 repositories/intent_sample_repo.py 注释）。
"""
# (text, intent, tag)  tag：'' | '泛词陷阱' | '规则表'（见 eval_intent_routing.py 的说明）
BUILTIN_CASES = [
    # knowledge_qa（说明/介绍类 —— 2026-09-25 报障所在）
    ("MBSE建模方法论介绍", "knowledge_qa", "泛词陷阱"),
    ("什么是MBSE建模方法论", "knowledge_qa", "泛词陷阱"),
    ("介绍一下SysML v2的作用", "knowledge_qa", "泛词陷阱"),
    ("MBSE建模方法论与传统方法的区别", "knowledge_qa", "泛词陷阱"),
    ("MBSE方法论包含哪些建模活动", "knowledge_qa", "泛词陷阱"),
    ("需求追溯的原理是什么", "knowledge_qa", "泛词陷阱"),
    ("变更影响分析的用途", "knowledge_qa", "泛词陷阱"),
    ("知识库里有没有关于链路预算的资料", "knowledge_qa", ""),
    # design（动作类，不能被说明类抢）
    ("请设计星网宽带通信系统的架构方案", "design", ""),
    ("帮我生成这个系统的SysML v2模型代码", "design", ""),
    ("输出一份 BDD 视图", "design", ""),
    ("对这个系统做总体设计", "design", ""),
    ("生成结构树", "design", "规则表"),
    ("画一张参数图", "design", "规则表"),
    # impact
    ("分析一下这个需求的变更影响", "impact", "泛词陷阱"),
    ("对巡飞弹做变更影响分析", "impact", ""),
    ("接口变更会波及哪些模块", "impact", "规则表"),
    # review / requirement_quality
    ("做一次需求质量评审", "requirement_quality", ""),
    ("这份需求的模糊词有哪些", "requirement_quality", ""),
    ("帮我校验一下这段sysml代码", "review", ""),
    ("追溯矩阵检查一下", "review", "规则表"),
    # report_generation
    ("生成一份需求分析报告", "report_generation", "泛词陷阱"),
    ("输出这次评审的报告", "report_generation", ""),
    # requirement_analysis
    ("帮我解析这份需求文档的条目", "requirement_analysis", ""),
    ("把任务书里的需求提取出来", "requirement_analysis", "泛词陷阱"),
    # system_mgmt
    ("系统里有几个用户", "system_mgmt", ""),
    ("查看一下审计日志", "system_mgmt", ""),
    # chat
    ("你好", "chat", ""),
    ("谢谢", "chat", ""),
]

# 供样本池导入用的 (text, intent) 形式
BUILTIN_PAIRS = [(t, i) for t, i, _ in BUILTIN_CASES]