# -*- coding: utf-8 -*-
"""卫星通信领域高质量图谱数据构建脚本（写入 + 幂等可重复执行）。

设计依据（行业本体调研，2026-08）：
- NASA CoSSO（Common Space Systems Ontology）：基于 BFO/CCO，SysML 概念化——
  block=类、generalization=is-a、composition=has-a；ECLSS 本体为样例。
- ESA Space System Ontology / OSMoSE：ORM 建模，覆盖 ECSS 全分支
  （management/product assurance/engineering），物理组件/质量/系统分层。
- ESA MBSE 六层方法学：Mission / System / Logical / Physical / Requirements / Transversal。

本领域本体对齐现有 ontology_types 表（entity|relation|attribute + parent 层级 + constraints），
实例数据对齐 entities/relations 表（release 消费分支 + dev/main 工作分支双写，reviewed 状态）。

消费侧约定（agent/pipeline.py _card_impact）：
- 只消费 release 分支图谱；变更源匹配实体 name/id；影响度 = 关系权重 × 层衰减。
- 关系类型采用英文键（CONTAINS/SATISFIES/...），与 rel_weight 权重表对齐。
"""
import json
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config import DB_PATH  # noqa: E402

BRANCHES = ["release", "dev/main"]  # 消费侧(release) + 工作分支(dev/main) 双写
PROJECT_ID = "project-satnet-broadband"
STATUS = "reviewed"

# ══════════════════════════════════════════════════════════════════
# 1) 本体类型（实体类型 + 关系类型 + 属性类型）
#    实体类型：领域概念分类（含子类型层级 parent_name）
#    关系类型：allowed_values.src/tgt 约束（变更影响传播的语义合法域）
#    属性类型：实体指标/参数
# ══════════════════════════════════════════════════════════════════

# (name, type_kind, parent_name, constraints)
ONTOLOGY_TYPES = [
    # ── 实体类型：系统层级（ESA MBSE Physical 层）──
    ("系统元素", "entity", None, {}),
    ("卫星系统", "entity", "系统元素", {"desc": "整星系统（空间段）"}),
    ("卫星平台", "entity", "卫星系统", {"desc": "支撑有效载荷的平台：电源/姿轨控/测控/热控"}),
    ("电源分系统", "entity", "卫星平台", {"desc": "太阳能电池阵+蓄电池+电源管理"}),
    ("姿轨控分系统", "entity", "卫星平台", {"desc": "姿态确定与控制 AOCS"}),
    ("测控分系统", "entity", "卫星平台", {"desc": "遥测遥控 TT&C"}),
    ("热控分系统", "entity", "卫星平台", {"desc": "温度控制"}),
    ("有效载荷", "entity", "卫星系统", {"desc": "实现任务功能的载荷（通信/遥感等）"}),
    ("通信载荷", "entity", "有效载荷", {"desc": "通信有效载荷：转发器+天线+功放+变频器"}),
    ("转发器", "entity", "通信载荷", {"desc": "透明/再生转发器（频率变换+放大）"}),
    ("天线", "entity", "通信载荷", {"desc": "反射面/多波束天线"}),
    ("相控阵天线", "entity", "天线", {"desc": "电扫描相控阵"}),
    ("功率放大器", "entity", "通信载荷", {"desc": "TWTA/SSPA 行波管/固态功放"}),
    ("变频器", "entity", "通信载荷", {"desc": "上下变频 LNA/CONV"}),
    ("滤波器", "entity", "通信载荷", {"desc": "输入输出滤波器"}),
    ("地面段", "entity", "系统元素", {"desc": "地面系统（信关站/测控站）"}),
    ("信关站", "entity", "地面段", {"desc": "业务关口站 Gateway"}),
    ("测控站", "entity", "地面段", {"desc": "TT&C 地面站"}),
    ("用户段", "entity", "系统元素", {"desc": "用户终端设备"}),
    ("用户终端", "entity", "用户段", {"desc": "VSAT/手持/动中通终端"}),
    ("通信链路", "entity", "系统元素", {"desc": "无线电链路（上行/下行）"}),
    ("上行链路", "entity", "通信链路", {"desc": "地面→卫星"}),
    ("下行链路", "entity", "通信链路", {"desc": "卫星→地面"}),
    # ── 实体类型：需求层级（ESA MBSE Requirements 层 + SysML Requirement）──
    ("需求", "entity", None, {}),
    ("利益相关方需求", "entity", "需求", {"desc": "Stakeholder Requirement（涉众期望）"}),
    ("系统需求", "entity", "需求", {"desc": "System Requirement"}),
    ("子系统需求", "entity", "需求", {"desc": "Subsystem Requirement"}),
    ("单元需求", "entity", "需求", {"desc": "Unit/Component Requirement"}),
    # ── 实体类型：功能/用例/利益相关方/验证（ESA MBSE Mission + Verification 层）──
    ("功能", "entity", None, {}),
    ("用例", "entity", None, {"desc": "Use Case（用户与系统的交互场景）"}),
    ("利益相关方", "entity", None, {"desc": "Stakeholder（用户/运营方/监管方）"}),
    ("验证活动", "entity", None, {"desc": "Verification Activity（测试/仿真/审查）"}),
    # ── 关系类型（中英文键：英文入权重表，中文兼容旧数据）──
    ("CONTAINS", "relation", None, {"desc": "组合/包含（composition，权重1.0）",
                                     "allowed_values": {"src": ["系统元素", "卫星系统", "卫星平台", "有效载荷", "通信载荷", "载荷", "地面段", "用户段"],
                                                        "tgt": ["系统元素", "卫星平台", "电源分系统", "姿轨控分系统", "测控分系统",
                                                                "热控分系统", "有效载荷", "通信载荷", "载荷", "转发器", "天线", "相控阵天线",
                                                                "功率放大器", "变频器", "滤波器", "TWTA", "信关站", "测控站", "用户终端"]}}),
    ("SATISFIES", "relation", None, {"desc": "满足（satisfy，权重1.0）",
                                      "allowed_values": {"src": ["系统元素", "卫星系统", "卫星平台", "有效载荷", "通信载荷", "转发器",
                                                                  "天线", "相控阵天线", "功率放大器", "变频器", "滤波器", "TWTA", "载荷",
                                                                  "信关站", "测控站", "用户终端", "上行链路", "下行链路", "功能", "部件"],
                                                         "tgt": ["需求", "利益相关方需求", "系统需求", "子系统需求", "单元需求"]}}),
    ("DERIVES", "relation", None, {"desc": "派生（requirement derivation，权重1.0）",
                                    "allowed_values": {"src": ["需求", "利益相关方需求", "系统需求", "子系统需求"],
                                                       "tgt": ["需求", "系统需求", "子系统需求", "单元需求"]}}),
    ("TRACE", "relation", None, {"desc": "追溯（traceability，权重0.4）",
                                  "allowed_values": {"src": ["需求", "系统需求", "子系统需求", "单元需求"],
                                                     "tgt": ["需求", "利益相关方需求", "系统需求", "子系统需求"]}}),
    ("ALLOCATED_TO", "relation", None, {"desc": "分配（allocation，权重1.0）",
                                         "allowed_values": {"src": ["功能", "用例"],
                                                            "tgt": ["系统元素", "有效载荷", "通信载荷", "转发器", "天线", "用户终端"]}}),
    ("CONNECTS", "relation", None, {"desc": "连接（interconnect，权重0.2）",
                                     "allowed_values": {"src": ["系统元素", "通信载荷", "转发器", "天线", "相控阵天线", "功率放大器",
                                                                "变频器", "滤波器", "上行链路", "下行链路", "信关站", "用户终端"],
                                                        "tgt": ["系统元素", "通信载荷", "转发器", "天线", "相控阵天线", "功率放大器",
                                                                "变频器", "滤波器", "上行链路", "下行链路", "信关站", "用户终端"]}}),
    ("FLOW_TO", "relation", None, {"desc": "流向（信号/数据流，权重0.7）",
                                    "allowed_values": {"src": ["上行链路", "下行链路", "天线", "相控阵天线", "转发器", "功率放大器",
                                                               "变频器", "滤波器", "信关站", "用户终端"],
                                                       "tgt": ["上行链路", "下行链路", "天线", "相控阵天线", "转发器", "功率放大器",
                                                               "变频器", "滤波器", "信关站", "用户终端"]}}),
    ("DEPENDS_ON", "relation", None, {"desc": "依赖（dependency，权重0.7）",
                                       "allowed_values": {"src": ["系统元素", "有效载荷", "通信载荷", "转发器", "天线"],
                                                          "tgt": ["卫星平台", "电源分系统", "姿轨控分系统", "测控分系统", "热控分系统"]}}),
    ("GENERALIZATION", "relation", None, {"desc": "泛化（is-a，权重0.4）",
                                           "allowed_values": {"src": ["天线", "相控阵天线", "功率放大器", "转发器", "用户终端"],
                                                              "tgt": ["天线", "功率放大器", "转发器", "用户终端"]}}),
    ("REALIZES", "relation", None, {"desc": "实现（用例由功能实现，权重1.0）",
                                     "allowed_values": {"src": ["功能", "用例"], "tgt": ["功能", "用例"]}}),
    ("VERIFIED_BY", "relation", None, {"desc": "验证（需求由验证活动验证，权重0.7）",
                                        "allowed_values": {"src": ["需求", "单元需求", "子系统需求"], "tgt": ["验证活动"]}}),
    ("ASSOCIATED_WITH", "relation", None, {"desc": "关联（利益相关方-用例，权重0.2）",
                                            "allowed_values": {"src": ["利益相关方"], "tgt": ["用例"]}}),
    ("USES", "relation", None, {"desc": "使用（用例/功能使用系统元素，权重0.7）",
                                 "allowed_values": {"src": ["用例", "功能", "用户终端", "信关站"],
                                                    "tgt": ["用例", "功能", "上行链路", "下行链路", "通信链路", "用户终端", "信关站"]}}),
    ("CONFLICTS", "relation", None, {"desc": "冲突（需求间冲突，权重0.2）",
                                      "allowed_values": {"src": ["需求"], "tgt": ["需求"]}}),
    ("REFERENCES", "relation", None, {"desc": "引用（需求引用/支撑，权重0.4）",
                                       "allowed_values": {"src": ["需求", "系统需求"], "tgt": ["需求", "系统需求", "子系统需求"]}}),
    # ── 属性类型（指标/参数）──
    ("频段", "attribute", None, {}),
    ("带宽", "attribute", None, {}),
    ("EIRP", "attribute", None, {}),
    ("G/T", "attribute", None, {}),
    ("数据速率", "attribute", None, {}),
    ("发射功率", "attribute", None, {}),
    ("工作频率", "attribute", None, {}),
    ("可用度", "attribute", None, {}),
    ("寿命", "attribute", None, {}),
    ("覆盖范围", "attribute", None, {}),
]

# ══════════════════════════════════════════════════════════════════
# 2) 实例数据（实体：多层级多类型 + 指标属性 + 别名；关系：多语义）
#    需求金字塔：利益相关方需求 → 系统需求 → 子系统需求 → 单元需求
# ══════════════════════════════════════════════════════════════════

# (id, name, entity_type, properties)  properties 含指标 + aliases 别名
ENTITIES = [
    # ── 需求层（4 层金字塔）──
    ("REQ-SR-001", "卫星通信服务需求", "利益相关方需求",
     {"code": "REQ-SR-001", "priority": "P0", "desc": "为用户提供 Ka 频段宽带互联网接入服务",
      "aliases": ["宽带服务需求", "通信服务需求"], "可用度": 0.999}),
    ("REQ-SYS-010", "宽带接入系统需求", "系统需求",
     {"code": "REQ-SYS-010", "priority": "P0", "desc": "系统提供 Ka 频段宽带接入，支持多用户并发",
      "aliases": ["系统宽带需求"], "频段": "Ka", "数据速率": "100Mbps"}),
    ("REQ-SYS-020", "覆盖区域系统需求", "系统需求",
     {"code": "REQ-SYS-020", "priority": "P1", "desc": "覆盖中国全境及周边海域",
      "aliases": ["覆盖需求"], "覆盖范围": "中国全境"}),
    ("REQ-SUB-030", "链路预算子系统需求", "子系统需求",
     {"code": "REQ-SUB-030", "priority": "P0", "desc": "上行/下行链路预算满足可用度要求",
      "aliases": ["链路预算需求"], "可用度": 0.999}),
    ("REQ-SUB-040", "转发器性能子系统需求", "子系统需求",
     {"code": "REQ-SUB-040", "priority": "P0", "desc": "转发器增益、带宽与线性度满足指标",
      "aliases": ["转发器需求"], "带宽": "500MHz"}),
    ("REQ-UNT-031", "链路可用度单元需求", "单元需求",
     {"code": "REQ-UNT-031", "priority": "P0", "desc": "单链路可用度不低于 99.9%",
      "aliases": ["可用度指标"], "可用度": 0.999}),
    ("REQ-UNT-032", "数据速率单元需求", "单元需求",
     {"code": "REQ-UNT-032", "priority": "P0", "desc": "单用户峰值数据速率不低于 100Mbps",
      "aliases": ["速率指标"], "数据速率": "100Mbps"}),
    ("REQ-UNT-041", "EIRP单元需求", "单元需求",
     {"code": "REQ-UNT-041", "priority": "P0", "desc": "载荷 EIRP 不低于 42dBW",
      "aliases": ["EIRP指标"], "EIRP": "42dBW"}),
    ("REQ-UNT-042", "G/T单元需求", "单元需求",
     {"code": "REQ-UNT-042", "priority": "P1", "desc": "载荷接收品质因数 G/T 不低于 18dB/K",
      "aliases": ["G/T指标"], "G/T": "18dB/K"}),
    # ── 系统层（卫星 → 平台/载荷 → 分系统/设备）──
    ("SAT-001", "宽带通信卫星", "卫星系统",
     {"code": "SAT-001", "desc": "高轨 Ka 频段宽带通信卫星", "aliases": ["宽带卫星", "卫星"],
      "轨道类型": "GEO", "寿命": "15年"}),
    ("PLT-001", "卫星平台", "卫星平台",
     {"code": "PLT-001", "desc": "通用卫星平台（电源/姿轨控/测控/热控）",
      "aliases": ["平台"]}),
    ("PWR-001", "电源分系统", "电源分系统",
     {"code": "PWR-001", "desc": "太阳能电池阵+蓄电池组+电源控制器", "aliases": ["供电分系统", "电源"],
      "发射功率": "12kW"}),
    ("AOCS-001", "姿轨控分系统", "姿轨控分系统",
     {"code": "AOCS-001", "desc": "姿态确定与控制（星敏感器+动量轮）", "aliases": ["姿控分系统"]}),
    ("TTC-001", "测控分系统", "测控分系统",
     {"code": "TTC-001", "desc": "遥测/遥控/测距", "aliases": ["测控系统"]}),
    ("THM-001", "热控分系统", "热控分系统",
     {"code": "THM-001", "desc": "被动+主动热控", "aliases": ["热控系统"]}),
    ("PAY-001", "通信有效载荷", "通信载荷",
     {"code": "PAY-001", "desc": "Ka 频段多波束通信有效载荷", "aliases": ["有效载荷", "通信载荷"],
      "频段": "Ka", "带宽": "2GHz", "覆盖范围": "16波束"}),
    # 复用已有 release 实体（转发器/TWTA/宽带通信载荷，保持与旧图谱衔接）
    ("ENT-002", "转发器", "转发器",
     {"code": "ENT-002", "desc": "Ka 频段透明转发器", "aliases": ["转发单元"], "带宽": "500MHz"}),
    ("ANT-001", "多波束天线", "天线",
     {"code": "ANT-001", "desc": "Ka 频段多波束反射面天线", "aliases": ["天线", "Ka天线"],
      "G/T": "18dB/K", "EIRP": "42dBW", "覆盖范围": "16波束"}),
    ("PAA-001", "相控阵天线", "相控阵天线",
     {"code": "PAA-001", "desc": "电扫描相控阵天线（用户段）", "aliases": ["相控阵"]}),
    ("CONV-001", "变频器", "变频器",
     {"code": "CONV-001", "desc": "上下变频器", "aliases": ["变频单元"], "工作频率": "27.5-31GHz"}),
    ("FLT-001", "输入滤波器", "滤波器",
     {"code": "FLT-001", "desc": "带通输入滤波器", "aliases": ["滤波器"]}),
    ("AMP-001", "功率放大器", "功率放大器",
     {"code": "AMP-001", "desc": "固态功率放大器 SSPA", "aliases": ["功放", "SSPA"], "发射功率": "40W"}),
    # ── 地面段 / 用户段 / 链路 ──
    ("GS-001", "主信关站", "信关站",
     {"code": "GS-001", "desc": "北京主信关站", "aliases": ["信关站", "关口站"], "数据速率": "10Gbps"}),
    ("TTS-001", "测控站", "测控站",
     {"code": "TTS-001", "desc": "喀什测控站", "aliases": ["地面测控站"]}),
    ("UT-001", "用户终端", "用户终端",
     {"code": "UT-001", "desc": "VSAT 用户终端", "aliases": ["终端", "VSAT"], "数据速率": "100Mbps"}),
    ("LINK-UL", "Ka上行链路", "上行链路",
     {"code": "LINK-UL", "desc": "用户终端→卫星上行链路", "aliases": ["上行链路", "上行"],
      "工作频率": "27.5-31GHz", "可用度": 0.999}),
    ("LINK-DL", "Ka下行链路", "下行链路",
     {"code": "LINK-DL", "desc": "卫星→用户终端下行链路", "aliases": ["下行链路", "下行"],
      "工作频率": "17.7-21.2GHz", "可用度": 0.999}),
    # ── 功能 / 用例 / 利益相关方 / 验证 ──
    ("FUN-001", "宽带接入功能", "功能",
     {"code": "FUN-001", "desc": "为用户提供宽带接入能力", "aliases": ["接入功能"]}),
    ("UC-001", "用户接入互联网", "用例",
     {"code": "UC-001", "desc": "用户通过终端接入互联网", "aliases": ["接入用例"]}),
    ("SH-001", "宽带用户", "利益相关方",
     {"code": "SH-001", "desc": "使用宽带服务的个人/行业用户", "aliases": ["用户", "终端用户"]}),
    ("VER-001", "链路预算验证", "验证活动",
     {"code": "VER-001", "desc": "链路预算仿真与实测验证", "aliases": ["链路验证"]}),
]

# (source_id, target_id, relation_type, props)
RELATIONS = [
    # 需求派生（利益相关方需求 → 系统需求 → 子系统需求 → 单元需求）
    ("REQ-SR-001", "REQ-SYS-010", "DERIVES", {"type": "派生"}),
    ("REQ-SR-001", "REQ-SYS-020", "DERIVES", {"type": "派生"}),
    ("REQ-SYS-010", "REQ-SUB-030", "DERIVES", {"type": "派生"}),
    ("REQ-SYS-010", "REQ-SUB-040", "DERIVES", {"type": "派生"}),
    ("REQ-SUB-030", "REQ-UNT-031", "DERIVES", {"type": "派生"}),
    ("REQ-SUB-030", "REQ-UNT-032", "DERIVES", {"type": "派生"}),
    ("REQ-SUB-040", "REQ-UNT-041", "DERIVES", {"type": "派生"}),
    ("REQ-SUB-040", "REQ-UNT-042", "DERIVES", {"type": "派生"}),
    # 需求追溯
    ("REQ-SYS-010", "REQ-SR-001", "TRACE", {"type": "追溯"}),
    ("REQ-SUB-030", "REQ-SYS-010", "TRACE", {"type": "追溯"}),
    # 系统组成（卫星 → 平台/载荷；平台 → 分系统；载荷 → 设备）
    ("SAT-001", "PLT-001", "CONTAINS", {"type": "组合"}),
    ("SAT-001", "PAY-001", "CONTAINS", {"type": "组合"}),
    ("PLT-001", "PWR-001", "CONTAINS", {"type": "组合"}),
    ("PLT-001", "AOCS-001", "CONTAINS", {"type": "组合"}),
    ("PLT-001", "TTC-001", "CONTAINS", {"type": "组合"}),
    ("PLT-001", "THM-001", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "ANT-001", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "ENT-001", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "ENT-002", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "CONV-001", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "FLT-001", "CONTAINS", {"type": "组合"}),
    ("PAY-001", "AMP-001", "CONTAINS", {"type": "组合"}),
    # 设计满足需求（验证追溯闭环）
    ("SAT-001", "REQ-SYS-010", "SATISFIES", {"type": "满足"}),
    ("SAT-001", "REQ-SYS-020", "SATISFIES", {"type": "满足"}),
    ("PAY-001", "REQ-SUB-030", "SATISFIES", {"type": "满足"}),
    ("ENT-002", "REQ-SUB-040", "SATISFIES", {"type": "满足"}),
    ("PAY-001", "REQ-UNT-031", "SATISFIES", {"type": "满足"}),
    ("PAY-001", "REQ-UNT-032", "SATISFIES", {"type": "满足"}),
    ("ANT-001", "REQ-UNT-042", "SATISFIES", {"type": "满足"}),
    ("AMP-001", "REQ-UNT-041", "SATISFIES", {"type": "满足"}),
    # 功能分配 / 用例实现 / 关联
    ("FUN-001", "PAY-001", "ALLOCATED_TO", {"type": "分配"}),
    ("FUN-001", "UT-001", "ALLOCATED_TO", {"type": "分配"}),
    ("FUN-001", "UC-001", "REALIZES", {"type": "实现"}),
    ("SH-001", "UC-001", "ASSOCIATED_WITH", {"type": "关联"}),
    # 连接（接口关系）
    ("ANT-001", "CONV-001", "CONNECTS", {"type": "连接"}),
    ("CONV-001", "AMP-001", "CONNECTS", {"type": "连接"}),
    ("UT-001", "LINK-UL", "CONNECTS", {"type": "连接"}),
    ("GS-001", "LINK-DL", "CONNECTS", {"type": "连接"}),
    ("TTC-001", "TTS-001", "CONNECTS", {"type": "连接"}),
    # 流向（信号流，变更影响传播链路）
    ("LINK-UL", "ANT-001", "FLOW_TO", {"type": "流"}),
    ("ANT-001", "ENT-002", "FLOW_TO", {"type": "流"}),
    ("ENT-002", "CONV-001", "FLOW_TO", {"type": "流"}),
    ("CONV-001", "AMP-001", "FLOW_TO", {"type": "流"}),
    ("AMP-001", "LINK-DL", "FLOW_TO", {"type": "流"}),
    # 依赖（载荷依赖平台）
    ("PAY-001", "PLT-001", "DEPENDS_ON", {"type": "依赖"}),
    ("PAY-001", "PWR-001", "DEPENDS_ON", {"type": "依赖"}),
    # 验证
    ("REQ-UNT-031", "VER-001", "VERIFIED_BY", {"type": "验证"}),
    ("REQ-UNT-041", "VER-001", "VERIFIED_BY", {"type": "验证"}),
    # 泛化（相控阵是天线的一种）
    ("PAA-001", "ANT-001", "GENERALIZATION", {"type": "泛化"}),
]


def build():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    stats = {"ont_added": 0, "ont_skipped": 0, "ont_updated": 0, "ent_added": 0, "ent_skipped": 0,
             "rel_added": 0, "rel_skipped": 0, "parent_linked": 0}

    # ── 1) 本体类型（幂等：同名跳过；已存在的关系类型则更新约束合法域）+ 层级 parent_id ──
    name2id = {}
    for r in conn.execute("SELECT id, name FROM ontology_types"):
        name2id[r["name"]] = r["id"]
    for name, kind, parent, cons in ONTOLOGY_TYPES:
        if name in name2id:
            if kind == "relation" and cons.get("allowed_values"):
                # 已存在关系类型：约束合法域演进（覆盖老约束，保证新类型可入边）
                conn.execute("UPDATE ontology_types SET constraints=?, description=? WHERE id=?",
                             (json.dumps(cons, ensure_ascii=False), cons.get("desc", ""), name2id[name]))
                stats["ont_updated"] += 1
            else:
                stats["ont_skipped"] += 1
            continue
        cur = conn.execute(
            "INSERT INTO ontology_types (name, type_kind, parent_id, constraints, description) "
            "VALUES (?,?,?,?,?)",
            (name, kind, None, json.dumps(cons, ensure_ascii=False), cons.get("desc", "")))
        name2id[name] = cur.lastrowid
        stats["ont_added"] += 1
    # parent 层级回填（父名 → parent_id）
    for name, kind, parent, cons in ONTOLOGY_TYPES:
        if parent and name in name2id and parent in name2id:
            conn.execute("UPDATE ontology_types SET parent_id=? WHERE id=?",
                         (name2id[parent], name2id[name]))
            stats["parent_linked"] += 1

    # ── 2) 实体（幂等：按 (id, branch) 跳过；双分支 release + dev/main）──
    for eid, name, etype, props in ENTITIES:
        for br in BRANCHES:
            dup = conn.execute("SELECT 1 FROM entities WHERE id=? AND branch=?", (eid, br)).fetchone()
            if dup:
                stats["ent_skipped"] += 1
                continue
            conn.execute(
                "INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id, "
                "source_type, confidence, created_by, reviewed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, name, etype, json.dumps(props, ensure_ascii=False), STATUS, br, PROJECT_ID,
                 "manual", 1.0, "王工", "李工"))
            stats["ent_added"] += 1

    # ── 3) 关系（幂等：按 (source,target,type,branch) 跳过；双分支）──
    for src, tgt, rtype, props in RELATIONS:
        for br in BRANCHES:
            dup = conn.execute(
                "SELECT 1 FROM relations WHERE source_id=? AND target_id=? AND relation_type=? AND branch=?",
                (src, tgt, rtype, br)).fetchone()
            if dup:
                stats["rel_skipped"] += 1
                continue
            conn.execute(
                "INSERT INTO relations (source_id, target_id, relation_type, properties, status, branch, "
                "project_id, confidence) VALUES (?,?,?,?,?,?,?,?)",
                (src, tgt, rtype, json.dumps(props, ensure_ascii=False), STATUS, br, PROJECT_ID, 1.0))
            stats["rel_added"] += 1

    conn.commit()
    # ── 汇总 ──
    ents = conn.execute("SELECT COUNT(*) c FROM entities WHERE branch='release' AND status='reviewed'").fetchone()["c"]
    rels = conn.execute("SELECT COUNT(*) c FROM relations WHERE branch='release' AND status='reviewed'").fetchone()["c"]
    ont = conn.execute("SELECT COUNT(*) c FROM ontology_types").fetchone()["c"]
    conn.close()
    stats.update({"release_reviewed_entities": ents, "release_reviewed_relations": rels,
                  "ontology_type_total": ont})
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


if __name__ == "__main__":
    build()
