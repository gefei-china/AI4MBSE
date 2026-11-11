# -*- coding: utf-8 -*-
"""产物命名：主体识别 / 会话内锁定一致性 / 占位名回填判据（2026-11-09）。

被验对象：
    · `core/artifact_naming.py`                  —— 纯函数：主体名、诉求摘要、标题合成、文件名
    · `agent/utils.py::_resolve_code_artifact_title` —— 会话内锁定（多版本只变版本后缀）
    · `agent/utils.py::_archive_artifacts`       —— 三条路径统一走同一命名真源

米爸原话：
    「产物清单中代码文件名称都是代码块，都不知道具体这个代码是什么，
      需要在名称上体现出来，基于生成时的诉求与目标，自动生成文件名称，
      注意，对于多版本的情况要保持一致性。」

运行：<venv>/python.exe -X utf8 tools/verify/verify_artifact_naming.py
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402
from core.artifact_naming import (  # noqa: E402
    compose_title, derive_subject, extract_package_name, extract_primary_def,
    filename_for, is_placeholder_title, summarize_goal,
)
from agent.utils import _resolve_code_artifact_title  # noqa: E402

fails = []


def chk(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


print("[1] 主体识别：package 优先")
chk("package LinkBudget → LinkBudget",
    derive_subject("package LinkBudget {\n  part def X;\n}") == "LinkBudget",
    derive_subject("package LinkBudget {\n  part def X;\n}"))
chk("library package 也认",
    derive_subject("library package SysML {\n}") == "SysML")
chk("限定名去前缀（A::B → B）",
    derive_subject("package A::Budget {\n}") == "Budget")

print("\n[2] 无 package 时按语义强度取定义名")
chk("requirement def 优先于 attribute",
    derive_subject("requirement def Req1;\nattribute def Attr1;") == "Req1",
    derive_subject("requirement def Req1;\nattribute def Attr1;"))
chk("part def Vehicle（带分号）",
    derive_subject("part def Vehicle;") == "Vehicle")
chk("view def ParamView",
    derive_subject("view def ParamView {\n  expose x;\n}") == "ParamView")
chk("analysis case def 命中",
    derive_subject("analysis case def FuelCase;") == "FuelCase")
chk("中文标识符",
    derive_subject("package 卫星模型 {\n}") == "卫星模型")

print("\n[3] 只有实例声明时取实例名")
chk("part vehicle : Vehicle; → vehicle",
    derive_subject("part vehicle : Vehicle;") == "vehicle",
    derive_subject("part vehicle : Vehicle;"))

print("\n[4] 诉求摘要：去噪声、限长")
chk("客套词被剔除",
    "请" not in summarize_goal("请帮我生成一个参数图示例"),
    summarize_goal("请帮我生成一个参数图示例"))
chk("长度 ≤ 13（12 字 + 省略号）",
    len(summarize_goal("相对于V1的参数图，在sysml V2 规范中对应什么视图，如何表示，请给个示例代码")) <= 13,
    summarize_goal("相对于V1的参数图，在sysml V2 规范中对应什么视图，如何表示，请给个示例代码"))
chk("空诉求 → 空串", summarize_goal("") == "")

print("\n[5] 标题合成：不重复主体")
chk("诉求含主体时不重复",
    compose_title("LinkBudget", "LinkBudget 参数图示例") == "LinkBudget",
    compose_title("LinkBudget", "LinkBudget 参数图示例"))
chk("诉求独立时附加",
    compose_title("LinkBudget", "参数图示例") == "LinkBudget · 参数图示例",
    compose_title("LinkBudget", "参数图示例"))
chk("版本后缀统一为「 · vX」",
    compose_title("LinkBudget", "", "v0.2") == "LinkBudget · v0.2",
    compose_title("LinkBudget", "", "v0.2"))
chk("多版本前缀完全一致",
    compose_title("LinkBudget", "", "v0.1").rsplit(" · ", 1)[0]
    == compose_title("LinkBudget", "", "v0.9").rsplit(" · ", 1)[0])

print("\n[6] 文件名：合法字符 + 版本进名")
chk("LinkBudget v0.2 → LinkBudget_v0.2.sysml",
    filename_for("LinkBudget", "v0.2") == "LinkBudget_v0.2.sysml",
    filename_for("LinkBudget", "v0.2"))
chk("非法字符被替换",
    "/" not in filename_for("A/B C", ""), filename_for("A/B C", ""))
chk("kerml 扩展名",
    filename_for("Lib", "", "kerml").endswith(".kerml"))

print("\n[7] 占位名判定（只回填占位名，用户自取的名字不动）")
for t, want in [("代码块 1（sysml）", True), ("代码文件 v0.1（SysML）", True),
                ("SysML 源码（工具取回 #2）", True), ("LinkBudget 模型", False),
                ("参数图示例", False)]:
    chk(f"{t!r} → {want}", is_placeholder_title(t) == want)
chk("空串算占位", is_placeholder_title("") is True)

print("\n[8] 会话内锁定：第二版 package 改名，标题前缀不变（多版本一致性）")
conn = sqlite3.connect(cfg.DB_PATH)
conn.row_factory = sqlite3.Row
TMP = 990999901
try:
    conn.execute("BEGIN")
    # 造一个会话标题（诉求来源）
    conn.execute("INSERT INTO conversations (id, title, created_at) VALUES (?,?,datetime('now'))",
                 (TMP, "参数图示例"))
    t1, f1, m1 = _resolve_code_artifact_title(conn, TMP, "package LinkBudget {\n}", "v0.1")
    # 命名形态 = 主体 · 诉求 · 版本（诉求来自会话标题"参数图示例"，是米爸要求的"体现生成时的诉求"）
    chk("首版名 = 主体 · 诉求 · 版本", t1 == "LinkBudget · 参数图示例 · v0.1", t1)
    chk("meta 带锁定字段（name_base 已含诉求，后续只换版本）",
        m1.get("name_base") == "LinkBudget · 参数图示例", m1)
    # 落一条产物，模拟已归档
    conn.execute(
        "INSERT INTO artifacts (conversation_id, message_id, kind, title, filename, preview_type,"
        " preview_content, meta, source, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TMP, 1, "code", t1, f1, "code", "package LinkBudget {}",
         json.dumps({"lang": "sysml", **m1}), "conversation", "自检"))
    # 第二版：package 改名成 PowerBudget（重构场景）
    t2, f2, m2 = _resolve_code_artifact_title(conn, TMP, "package PowerBudget {\n}", "v0.2")
    chk("第二版仍沿用首版主体（不跳名）",
        t2 == "LinkBudget · 参数图示例 · v0.2", t2)
    chk("文件名同步沿用", f2 == "LinkBudget_v0.2.sysml", f2)
    chk("前缀逐字一致",
        t1.rsplit(" · ", 1)[0] == t2.rsplit(" · ", 1)[0], (t1, t2))
finally:
    conn.rollback()
    conn.close()

print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)} 项：{fails}")
    sys.exit(1)
print("ALL PASS（28 项断言）")
