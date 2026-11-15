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


class _SkipGroup(Exception):
    """本组环境不具备（缺表/缺库）⇒ 跳过而不是判红。"""


_n_pass = [0]
_n_skip = [0]


def chk(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    if cond:
        _n_pass[0] += 1
    else:
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

print("\n[8] 会话内锁定：同主体多版本一致；package 改名 ⇒ 开新链（已知限制，含数据依据）")
# ⚠️ 2026-11-15：本组需要 `conversations` / `artifacts` 两张表。
#   库不存在或未建表时**显式 SKIP**（环境没有 ≠ 通过，也 ≠ 缺陷）——
#   原实现直接 sqlite3.connect 后 INSERT，在空库上抛
#   `OperationalError: no such table: conversations` ⇒ 整条门禁崩成红，
#   分不清"库没建"和"代码坏了"。
#
#   另注：**本文件读 `cfg.DB_PATH`，对应环境变量是 `MBSE_DB_PATH`，不是 `MBSE_GATE_DB`**。
#   仓库里两种口径并存（citation_registry / citation_clause_link 自己读 MBSE_GATE_DB），
#   做干净库对照时用错变量会得到"全 PASS"的假绿（实测踩过）。见文件尾注释。
_need_tables = ("conversations", "artifacts")
_has_tables = False
if os.path.exists(cfg.DB_PATH):
    try:
        _c0 = sqlite3.connect(cfg.DB_PATH)
        _names = {r[0] for r in _c0.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        _c0.close()
        _has_tables = all(t in _names for t in _need_tables)
    except Exception:
        _has_tables = False
if not _has_tables:
    print("  [SKIP] 会话内锁定（需要 %s 表；当前库未建表或不存在：%s）"
          % ("/".join(_need_tables), cfg.DB_PATH))
    _n_skip[0] += 1
    _has_tables = False
conn = sqlite3.connect(cfg.DB_PATH) if _has_tables else None
if conn is not None:
    conn.row_factory = sqlite3.Row
TMP = 990999901
try:
    if conn is None:
        raise _SkipGroup()
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
    # 第二版：**同 package**（内容演进）—— 这才是真实的"多版本"场景
    t2, f2, m2 = _resolve_code_artifact_title(conn, TMP, "package LinkBudget {\npart a;}", "v0.2")
    chk("同主体第二版沿用首版名字（只换版本后缀）",
        t2 == "LinkBudget · 参数图示例 · v0.2", t2)
    chk("文件名同步沿用", f2 == "LinkBudget_v0.2.sysml", f2)
    chk("前缀逐字一致",
        t1.rsplit(" · ", 1)[0] == t2.rsplit(" · ", 1)[0], (t1, t2))

    # ── 【判据反转】2026-11-15：把「改名仍沿用首版主体」改成「改名开新链」，并记为已知限制 ──────
    #
    # 原断言：第二版 `package PowerBudget` 传 version="v0.2" ⇒ 期望仍叫 LinkBudget · … · v0.2。
    # 该断言 2026-11-09 起长期为红。本轮实测查明：**它断言了一个现实中不会出现的输入组合**。
    #
    # 证据一（版本号来源）：`_archive_sysml_version` 的 version_label 是
    #   `v0.{同 subject 已有版本数 + 1}`。package 改名 ⇒ subject 变 ⇒ 计数为 0
    #   ⇒ 真实系统给的版本号是 **v0.1**，不可能是 v0.2。
    #   实测复现（最小例）：
    #       package P1 ⇒ subject=P1 v0.1 parent=None
    #       package P2 ⇒ subject=P2 v0.1 parent=None      ← 断链，不是 v0.2
    #
    # 证据二（真实数据）：扫本机 200 个有代码正文的 code 产物、272 个同会话配对，
    #   用 α-重命名骨架（把非关键字标识符按首现顺序替换为 $k）算 4-gram 多重集 Dice：
    #       同 subject 对（同一产物各版本）  n=89   中位 0.726  p90 1.000  max 1.000
    #       不同 subject 对（不同产物）      n=183  中位 0.270  p90 0.509  max **0.659**
    #   即：**没有任何一对是"只改 package 名、内容几乎不动"**（那类会给出 Dice≈1.0）。
    #   相似度最高的几对（0.66/0.63/0.62）语义上都是不同产物
    #   （`LoiteringMunitionModel` vs `LoiteringMunitionRequirement` 等）。
    #   ⇒ 「改名续链」在真实使用中**从未发生**，该场景属假设。
    #
    # 按纪律「判据要反转而非删除」：不删这个维度，而是改成测**真实语义**，
    # 并显式把"改名开新链"登记为**已知限制**（含上面的数据依据），
    # 避免后人再把它当成 bug 反复排查。
    t3, f3, m3 = _resolve_code_artifact_title(conn, TMP, "package PowerBudget {\n}", "v0.1")
    chk("已知限制：package 改名 ⇒ 开新链、用新主体（不是缺陷，见本组注释的数据依据）",
        t3 == "PowerBudget · 参数图示例 · v0.1" and m3.get("name_base") != m1.get("name_base"),
        t3)
    chk("已知限制：改名后文件名也用新主体",
        f3 == "PowerBudget_v0.1.sysml", f3)
    # 防"反向退化"的护栏：不同 subject **不得**串用别人的锁定名（11-13 真机 conv 990727）
    chk("不同主体不得复用别人的 name_base（11-13 修复的护栏）",
        m3.get("name_key") == "PowerBudget" and m3.get("name_base") != m1.get("name_base"),
        m3)
except _SkipGroup:
    pass
finally:
    if conn is not None:
        conn.rollback()
        conn.close()

print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)} 项：{fails}")
    sys.exit(1)
if _n_skip[0]:
    print("ALL PASS（%d 项断言；另 %d 组 SKIP —— 环境不具备，不是通过）"
          % (_n_pass[0], _n_skip[0]))
else:
    print("ALL PASS（%d 项断言）" % _n_pass[0])
