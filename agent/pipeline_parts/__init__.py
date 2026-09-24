"""AgentPipeline Mixin 子包：按功能域拆分的类片段，由 pipeline.AgentPipeline 多继承组装。

本包各分片由 tools/split_pipeline.py 一次性切分而成；⚠️ 该脚本**不可重跑**（重跑会覆盖本目录）—— 各分片此后按普通源码维护。"""
