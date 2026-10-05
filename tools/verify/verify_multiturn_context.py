# -*- coding: utf-8 -*-
"""P0-6 会话产物摘要（多轮上下文）常驻验收：纯函数 + 四落点接线 + 零回归 + 变异自证。

跑法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_multiturn_context.py

设计要点（都是踩过才写的）：
  · **夹具库用真实 DDL**：从生产库 `sqlite_master` 读 `messages/artifacts/sysml_versions/reports`
    的建表语句再套到临时库 —— 手抄 DDL 会漂移，而"缺列"会被生产代码的 `except` 静默兜成
    "逻辑没生效"，症状与"改动没写对"完全一样（本仓已两次）。
  · **落点断言走真实代码路径**：流式 planner 用真 generator + 桩 LLM（`_Abort(BaseException)`
    ——`except Exception` 抓不到它，流程停在第一个副作用之前）；子任务落点用 spy 拦
    `workflows.planner.build_subtask_context`，断言它拿到的 `artifact_digest` 参数确有内容。
  · **零回归用 exec 孪生体对拍**：把当前 `_load_history` 源码里的 digest 三处外科手术删掉 →
    exec 成"改动前"的孪生函数 → 同实例同输入两版对比。磁盘文件一字不动。
  · **变异自证只认新增失败**（`f not in base`），且每个变异配自己的目标断言。
  · **P1-7（2026-10-01）新增**：`_LEAD` 126→54 字符 + 系统性兜底名产物行过滤（A14~A19 / M8·M8b·M13~M16）。
  · **P1-8（2026-10-01）新增**：历史（输入/输出）相关性召回收口 —— 近因窗口**均分份额**、
    `used_ids` 只记真正注入的、拉回**分角色配额** + 可追溯块头 + 丢弃与当前输入逐字重复的块
    （E1~E9b / M17~M22）。判据全部锚在**可观察产物**（raw 的 id 序列、块头条数、拉回正文）上，
    不用"是否调用了某函数"这类内部事实。
  · **P1-9（2026-10-01）新增**：写入侧不再产生系统性兜底名 —— 从内容取名
    （`core/artifact_titles.derive_title_from_content`）并接到三个写入点
    （`agent/utils.py` 报告/文档两处 + `services/artifact_materializer` 二次物化）。
    断言含**两处真实踩过的坑**：F3（左右剥除字符集**必须不对称**，否则标题括号被削成不成对）、
    F4（派生结果若仍是兜底名必须返回空，否则把 bug 从写入侧搬到读取侧）。
    变异 M24~M27。
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# ── 已接进 CI（2026-10-05 第二轮第 2 项续：变异锚点漂移修复）──────────
# 修复要点：多轮上下文。同上的 dedent 失效根因；M3 锚点原写死 4/8 格缩进，而 `_digest`\n#   被包进 try/except 后实际是 8/12 ⇒ find 返回 -1 ⇒ 拼出畸形源码 ⇒ try 块异常中断\n#   ⇒ M5 的断言**根本没执行**（假绿）。改用 _srctool.src_of + 按行内容定位。
# 双环境实测：生产库 + 全新干净库 均 rc=0。

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
_VERIFY_DIR = os.path.join(ROOT, "tools", "verify")
if _VERIFY_DIR not in sys.path:
    sys.path.insert(0, _VERIFY_DIR)
import _srctool  # noqa: E402  —— 正确 dedent 的 getsource（见 _srctool 模块注释）

LIVE_DB = os.path.join(ROOT, "mbse.db")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + str(detail)) if detail else ""))
    return bool(cond)


# ══════════════════════════════════════════════════════════════════════════
# 夹具：真实 DDL（从生产库读建表语句）+ 最小数据
# ══════════════════════════════════════════════════════════════════════════
TABLES = ("messages", "artifacts", "sysml_versions", "reports", "conversations", "agent_tasks")
FIXTURE_PATH = os.path.join(tempfile.mkdtemp(prefix="wb_mtctx_"), "fixture.db")


def build_fixture():
    """整库表结构复制（生产 `sqlite_master` → 临时库），而不是逐个列举。

    理由：C5 走真实子任务链路，会用到的表不止 4 张（`agent_tasks` 等）；逐个补是打地鼠，
    漏一张的报错长成 `no such table`，和"代码写错"一样难定位。FTS5 影子表（`knowledge_fts*`）
    复制过来无意义且 DDL 是虚拟表语法，跳过。
    """
    live = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True)
    rows = live.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','view') "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type DESC, name").fetchall()
    live.close()
    c = sqlite3.connect(FIXTURE_PATH)
    made, skipped = set(), []
    for _typ, name, sql in rows:
        if name.startswith("knowledge_fts"):
            skipped.append(name)
            continue
        try:
            c.execute(sql)
            made.add(name)
        except Exception as e:      # noqa: BLE001 —— 跳过非必需表，但把名字记下来（别静默）
            skipped.append("%s(%s)" % (name, type(e).__name__))
    c.commit()
    # 契约断言：核心表必须在。手抄/漏表会让失败伪装成"逻辑没生效"（本仓两次教训）。
    missing = [t for t in TABLES if t not in made]
    if missing:
        raise SystemExit("夹具前置失败：缺表 %s；跳过清单=%s" % (missing, skipped))
    print("  (夹具建表 %d 张；跳过 %d：%s)" % (len(made), len(skipped), skipped[:6]))
    return c


def new_conn():
    """每次 get_db() 返回一条新连接（与生产 get_db 用法一致：调用方负责 close）。"""
    c = sqlite3.connect(FIXTURE_PATH)
    c.row_factory = sqlite3.Row
    return c


ES = {"entities": 5, "relations": 4, "views": 2,
      "nodes": ["IRRequirement", "ir1", "ir2", "ir3", "ir4"],
      "edges": ["ir1--类型--IRRequirement"], "intent": "team_leader",
      "check": {"rc": 0, "verdict": "pass", "blocked": False}}

# P1-8：本轮"当前输入"（**不落库**，与生产一致：execute/stream 是先回复后落库）
P18_CUR = "在既有需求模型基础上，补充涉众与参与者信息的追溯关系定义"
_LONG18 = "助手历轮的长篇产出段落，用于把历史预算吃满。" * 190      # ≈ 4180 字符
_MID18 = "助手历轮的中篇产出段落。" * 215                          # ≈ 2580 字符
# 720 字符 = **828 tok**（实测 `count_tokens`）：> raw_cap//2=500（会被均分切断）
# 且 < raw_cap=1000（**本来装得下**）⇒ 恰好落进「均分纯损失」区间
_SHORT18 = "助手对上一轮的完整回复。" * 60


def seed(c):
    c.execute("INSERT INTO conversations (id, title, intent, current_intent, last_slots) VALUES (11,'有产物','design','design','{}')")
    c.execute("INSERT INTO conversations (id, title, intent, current_intent, last_slots) VALUES (12,'无产物','chat','chat','{}')")
    # ① 有产物的会话
    c.execute("INSERT INTO sysml_versions (conversation_id, message_id, version_label, status, adopted, "
              "element_summary, code_text, created_at) VALUES (11,101,'v0.1','current',0,?,?, '2026-09-29 06:09:48')",
              (json.dumps(ES, ensure_ascii=False), "package A; " * 30))
    c.execute("INSERT INTO sysml_versions (conversation_id, message_id, version_label, status, adopted, "
              "element_summary, code_text, created_at) VALUES (11,102,'v0.2','current',1,'{bad json', '', '2026-09-29 07:00:00')")
    for t, k in (("SysML 视图：需求图", "sysml"), ("SysML 视图：活动图", "sysml"), ("代码文件 v0.1（SysML）", "code")):
        c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) VALUES (11,101,?,?)", (k, t))
    c.execute("INSERT INTO reports (title, report_type, summary, sections, source, conversation_id) "
              "VALUES ('整车需求建模报告','requirement','s','[]','conversation',11)")
    # ② 无产物的会话（零回归对照）：消息 topic 预置满 → _tag_topics 走"无未打标"早退，不写库、不调向量
    for i, (role, txt) in enumerate((("user", "你好，问个事"), ("assistant", "请讲"))) :
        c.execute("INSERT INTO messages (conversation_id, role, content, msg_type, topic) VALUES (12,?,?,'text','闲谈')",
                  (role, txt))
    # ③ 有产物的会话也放两条消息（用于 D 组"多且仅多一条"对拍）
    for role, txt in (("user", "【建模目的】整车需求数据"), ("assistant", "已生成 v0.1")):
        c.execute("INSERT INTO messages (conversation_id, role, content, msg_type, topic) VALUES (11,?,?,'text','整车需求建模')",
                  (role, txt))
    # ④ conv=13：占位标题 + 超长节点名 + 非 current 状态（真库实测形态：会话 1 的 6 条文档标题全同）
    #    注意**不插 conversations 行** —— 真库 sysml_versions 引用了 38 个 conv_id 而 conversations 只有 13 行，
    #    摘要通道不依赖 conversations 行存在（A13 守这一点）。
    ES_BIG = {"entities": 20, "relations": 7, "views": 1,
              "nodes": ["N%02d" % i for i in range(1, 21)],
              "check": {"rc": 0, "verdict": "pass"}}
    c.execute("INSERT INTO sysml_versions (conversation_id, message_id, version_label, status, adopted, "
              "element_summary, code_text, created_at) VALUES (13,201,'v9.9','superseded',0,?,?,'2026-09-01 00:00:00')",
              (json.dumps(ES_BIG, ensure_ascii=False), "package B; " * 10))
    for _ in range(6):
        c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) "
                  "VALUES (13,201,'document','AI 生成文档')")
    c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) "
              "VALUES (13,201,'report','AI 生成报告')")
    # ⑤ conv=14：**只有**占位名产物、无 SysML 版本、无报告 → P1-7 后整段抑制（返回 ""）
    for _ in range(2):
        c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) "
                  "VALUES (14,301,'document','AI 生成文档')")
    # ⑥ conv=15：混合（占位名 + 真实标题 + 重复真实标题）→ 只列真标题；重复真标题仍需去重
    for _t, _k in (('AI 生成文档', 'document'), ('整车需求规格说明', 'document'),
                   ('代码块 1（sysml）', 'code'), ('代码块 1（sysml）', 'code')):
        c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) "
                  "VALUES (15,401,?,?)", (_k, _t))
    # ⑦ conv=16/17：P1-8 夹具。两会话的 messages.topic **全部预置** ⇒ `_tag_topics` 走
    #    "全已打标"早退（不写库、不调 VectorEngine，离线确定性）。
    #    conv=16：近因窗口 tail(8) = 后 8 条（3 条短 user + 5 条 asst，末条 4.1k 字符）；
    #             窗口**外**前 10 条里 user/assistant 各有 ≥4 条高相关 ⇒ 守分角色配额；
    #             其第 9 条与 P18_CUR **逐字相同** ⇒ 守 drop_echo。
    #    conv=17：低预算场景（E7 临时把 budget_history_tokens 压到 400）——
    #             高相关的那条 user 消息会**被预算挡下**，若 `used_ids` 超额标记它就会彻底消失。
    _m16 = [
        ("user", "请补充涉众与参与者的追溯关系定义"),
        ("assistant", "涉众与参与者的追溯关系定义已补充：涉众 4 类、参与者 6 个，已建 satisfy 关系。"),
        ("user", "涉众与参与者信息的追溯关系定义还需要补充哪些内容？"),
        ("assistant", "需要补充涉众关注点与参与者之间的分配关系，以及需求追溯关系定义。"),
        ("user", "在需求模型上补充涉众与参与者的追溯关系定义要注意什么？"),
        ("assistant", "注意需求模型里 stakeholder 与 participant 的追溯关系定义方向，避免反向追溯。"),
        ("user", "涉众与参与者的追溯关系定义在需求模型基础上怎么补充？"),
        ("assistant", "在既有需求模型基础上补充涉众与参与者的追溯关系定义，落到 trace 关系上。"),
        ("user", P18_CUR),
        ("assistant", "助手对同一追溯关系定义的补充说明：参与者与涉众的追溯关系定义需成对出现。"),
        ("user", "继续优化需求数据"),
        ("assistant", "已记录。"),
        ("assistant", _MID18),
        ("user", "继续优化需求数据"),
        ("assistant", "已记录。"),
        ("assistant", _MID18),
        ("user", "继续优化需求数据，补充参与者与涉众信息"),
        ("assistant", _LONG18),
    ]
    for _role, _txt in _m16:
        c.execute("INSERT INTO messages (conversation_id, role, content, msg_type, topic) "
                  "VALUES (16,?,?,'text','需求追溯')", (_role, _txt))
    _m17 = [
        ("user", "继续优化需求数据"),
        ("assistant", "已记录。"),
        ("user", "请补充涉众与参与者的追溯关系定义"),
        ("assistant", "已记录。"),
        ("assistant", _MID18),
        ("assistant", "已记录。"),
        ("user", "继续优化需求数据"),
        ("assistant", _LONG18),
    ]
    for _role, _txt in _m17:
        c.execute("INSERT INTO messages (conversation_id, role, content, msg_type, topic) "
                  "VALUES (17,?,?,'text','需求追溯')", (_role, _txt))
    # ⑧ conv=18：P1-8 **短路零回归**夹具 —— 只有 2 条消息、预算绰绰有余，
    #    唯一那条长回复（828 tok < raw_cap 1000）本可整条装下 ⇒ 不得被均分白切一半。
    for _role, _txt in (("user", "优化需求数据，需要支持参与者信息"), ("assistant", _SHORT18)):
        c.execute("INSERT INTO messages (conversation_id, role, content, msg_type, topic) "
                  "VALUES (18,?,?,'text','整车需求建模')", (_role, _txt))
    c.commit()


FIX = build_fixture()
seed(FIX)
FIX.commit()

from agent.session_artifacts import (build_digest, collect_facts, render_digest,  # noqa: E402
                                     DIGEST_MARK)
import agent.session_artifacts as SA                         # noqa: E402
from agent.pipeline import AgentPipeline                     # noqa: E402
from workflows.planner import build_subtask_context          # noqa: E402
import workflows.planner as W                                # noqa: E402
import agent.pipeline_parts.history as H                     # noqa: E402
import agent.pipeline_parts.stream as S                      # noqa: E402
import llm as LLM                                            # noqa: E402
import database as DB                                        # noqa: E402
import core.config as CFG                                    # noqa: E402


class Abort(BaseException):
    """故意继承 BaseException：生产代码的 `except Exception` 抓不到它，
    流程停在第一个副作用之前（用于把真 generator 当"半程可停"的探针）。"""


def _nv(name):
    """取真实 dict 的防御式副本（变异测试会删键/改值）。"""
    return None


# ══════════════════════════════════════════════════════════════════════════
print("=" * 78)
print("[A] 摘要纯函数（夹具库 = 真实 DDL）")
# ══════════════════════════════════════════════════════════════════════════
d11 = build_digest(FIX, 11)
# 判据必须锚到**版本行**（"- SysML 模型版本 vX"）：P1-7 之前 _LEAD 里自带示例「在 v0.1 的需求模型上…」，
# 裸 "v0.1" in text 会被引导语满足 → 断言恒真（本仓"判据被无关文本污染"家族）。
# （P1-7 已把该示例从 _LEAD 压掉，但判据仍锚版本行 —— 不因文案变动而回退到弱判据。）
check("A1 有产物会话 → 含标记/版本行/元素名/校验结论/产物标题",
      DIGEST_MARK in d11 and "- SysML 模型版本 v0.1" in d11 and "ir3" in d11
      and "pass" in d11 and "需求图" in d11,
      repr(d11[:80]))
check("A2 无产物会话 → 空串（零回归前提）", build_digest(FIX, 12) == "", repr(build_digest(FIX, 12)))
check("A3 边界会话 id（0/None/负数/非数字）→ 空串且不抛",
      all(build_digest(FIX, x) == "" for x in (0, None, -5, "abc", "")))
_bad = build_digest(FIX, 11)
check("A4 坏 JSON element_summary 不炸，且该版本行仍在（v0.2 出现）",
      "v0.2" in _bad)
_cap = render_digest(collect_facts(FIX, 11), max_chars=120)
check("A5 max_chars 截断生效（≤120 且带截断标记）",
      len(_cap) <= 120 and "截断" in _cap, "len=%d" % len(_cap))
# 契约 = 「最新在前 + LIMIT N」→ versions=1 只出**最新**那条（v0.2），不是最早那条。
# 判据锚到版本行（"- SysML 模型版本 vX"），避免被 _LEAD 里的 "v0.1" 示例污染。
_d1 = render_digest(collect_facts(FIX, 11, versions=1), max_chars=2000)
_d2 = render_digest(collect_facts(FIX, 11, versions=2), max_chars=2000)
check("A6 versions 限制生效（versions=1 → 仅最新 v0.2；versions=2 → 两条都在）",
      "- SysML 模型版本 v0.2" in _d1 and "模型版本 v0.1" not in _d1
      and "模型版本 v0.1" in _d2 and "模型版本 v0.2" in _d2,
      "v1行数=%d v2行数=%d" % (_d1.count("模型版本"), _d2.count("模型版本")))
check("A7 确定性：同输入两次逐字节相同", render_digest(collect_facts(FIX, 11), 900) == d11)
check("A8 渲染文本不含 CR（注入 prompt 的文本干净）", "\r" not in d11)
_facts = collect_facts(FIX, 11)
check("A9 facts 契约：versions 最新在前 + kinds 计数正确",
      [v["label"] for v in _facts["versions"]] == ["v0.2", "v0.1"]
      and _facts["counts"].get("SysML 视图") == 2 and _facts["counts"].get("代码文件") == 1,
      str(_facts["counts"]))

# A10~A13：针对「真库实测出来的摘要质量问题」（占位标题 / 英文状态 / 超长节点名 / 孤儿会话）
d13 = build_digest(FIX, 13)
# P1-7：conv 13 的两条产物行**全是**系统性兜底名（6×「AI 生成文档」+1×「AI 生成报告」）。
# 旧行为 = "去重后列 1 次"（占位名仍占版面）；新行为 = **整行不列**。
# ⚠️ 这条改造同时废掉了旧的 M8（去重变异）—— 占位行被整行丢弃后，"去不去重"都不出现，
# 旧 M8 会退化成**空转变异**（照样"被抓住"但抓的不是去重）。已改为在 conv 15 上用
# **重复的真实标题**单独守去重（见 A17c / M8b）。
_c13_lines = [x for x in d13.split("\n") if x.startswith("- ")]
check("A10 系统性兜底名不再出现在摘要里（P1-7：零信息量行不列）",
      "AI 生成文档" not in d13 and "AI 生成报告" not in d13, repr(d13[-150:]))
check("A10b 只剩兜底名的 kind 行整行不输出（连计数一起）",
      "文档 6 项" not in d13 and "报告 1 项" not in d13
      and not any(("文档" in x or "报告" in x) for x in _c13_lines),
      "lines=%s" % _c13_lines)
check("A11 版本状态本地化：不出现英文 'superseded'，含「历史版本」",
      "superseded" not in d13 and "历史版本" in d13)
check("A12 元素名超限截断：只列前 12 个 + 带「共 20 个」",
      "N12" in d13 and "N13" not in d13 and "共 20 个" in d13, repr(d13[-180:]))
check("A13 conversations 表无对应行也能出摘要（真库有此类孤儿 conv_id）",
      FIX.execute("SELECT COUNT(*) FROM conversations WHERE id=13").fetchone()[0] == 0
      and DIGEST_MARK in d13)

# ── P1-7（2026-10-01）：引导语压缩 + 零信息量事实过滤 ──────────────────────
from core.artifact_titles import (PLACEHOLDER_TITLES, derive_title_from_content,  # noqa: E402
                                  extract_text_from_content, is_placeholder_title,
                                  materialize_fallback_title)

check("A14 契约：占位名闭集覆盖 subtask KIND_VALUES 的 {kind}产物（防副本漂移）",
      all(materialize_fallback_title(k) in PLACEHOLDER_TITLES for k in ("sysml", "requirement", "report", "doc")),
      "PLACEHOLDER_TITLES=%s" % sorted(PLACEHOLDER_TITLES))
check("A14b 写入侧两个字面量在集合内（与 agent/utils.py 同源）",
      "AI 生成文档" in PLACEHOLDER_TITLES and "AI 生成报告" in PLACEHOLDER_TITLES)
check("A14c 判据不误伤真实标题（闭集精确匹配，非「以产物结尾」正则）",
      not is_placeholder_title("设计产物") and not is_placeholder_title("需求产物")
      and not is_placeholder_title("代码块 1（sysml）") and not is_placeholder_title("整车需求规格说明"))

check("A15 _LEAD 已压缩（<=60 字符）", len(SA._LEAD) <= 60, "len=%d" % len(SA._LEAD))
check("A15b 压缩后仍保留核心动词短语与「无关则忽略」条件句（不是电报式极简）",
      "在既有产物上做增量" in SA._LEAD.replace("**", "") and "无关" in SA._LEAD,
      repr(SA._LEAD))

d14 = build_digest(FIX, 14)
check("A16 只有占位名产物 → 整段抑制（返回 \"\"，与「无产物」同语义）", d14 == "", repr(d14))

d15 = build_digest(FIX, 15)
check("A17 混合：只列真实标题，占位名不出现",
      "整车需求规格说明" in d15 and "AI 生成文档" not in d15 and "代码块 1（sysml）" in d15,
      repr(d15[-170:]))
check("A17b 混合时计数仍报真实条数（2 条文档中 1 个真名）", "文档 2 项" in d15, repr(d15[-170:]))
check("A17c 真实标题仍去重（2 条同名的「代码块 1（sysml）」只列 1 次 + 计数报 2 项）",
      d15.count("代码块 1（sysml）") == 1 and "代码文件 2 项" in d15, repr(d15[-170:]))

check("A18 开关关闭（skip_placeholder_only=False）→ 回到改动前形态（占位名重新出现）",
      "AI 生成文档" in build_digest(FIX, 15, skip_placeholder_only=False))
check("A18b 开关关闭时纯占位会话重新注入（A/B 回滚互证）",
      "AI 生成文档" in build_digest(FIX, 14, skip_placeholder_only=False))
check("A18c 无产物会话在两种开关下都是空串（零回归不因开关而变）",
      build_digest(FIX, 12) == "" and build_digest(FIX, 12, skip_placeholder_only=False) == "")

check("A19 DEFAULT_CONFIG.context 含新键且默认 True",
      CFG.DEFAULT_CONFIG["context"].get("artifact_digest_skip_placeholder_only") is True)
check("A19b CONFIG_SCHEMA.context 含新键（前端契约）",
      "artifact_digest_skip_placeholder_only" in CFG.CONFIG_SCHEMA["context"])

# ══════════════════════════════════════════════════════════════════════════
print()
print("[B] 配置契约与开关")
# ══════════════════════════════════════════════════════════════════════════
check("B1 DEFAULT_CONFIG.context 含三键",
      all(k in CFG.DEFAULT_CONFIG["context"] for k in
          ("artifact_digest_enabled", "artifact_digest_max_chars", "artifact_digest_versions")))
check("B2 CONFIG_SCHEMA.context 含三键（前端契约）",
      all(k in CFG.CONFIG_SCHEMA["context"] for k in
          ("artifact_digest_enabled", "artifact_digest_max_chars", "artifact_digest_versions")))
_orig_as_bool = CFG.as_bool
_orig_get = CFG.get
try:
    CFG.as_bool = lambda *a, **k: False
    check("B3 enabled=False → 空串（回退开关有效）", build_digest(FIX, 11) == "")
finally:
    CFG.as_bool = _orig_as_bool
try:
    def _fake_get(group, key, default=None, *a, **k):
        if group == "context" and key == "artifact_digest_max_chars":
            return 80
        return _orig_get(group, key, default, *a, **k)
    CFG.get = _fake_get
    _c = build_digest(FIX, 11)
    check("B4 配置 max_chars 生效（80 → ≤80）", 0 < len(_c) <= 80, "len=%d" % len(_c))
finally:
    CFG.get = _orig_get
check("B5 默认值：enabled=True / max_chars=900 / versions=3",
      CFG.DEFAULT_CONFIG["context"]["artifact_digest_enabled"] is True
      and CFG.DEFAULT_CONFIG["context"]["artifact_digest_max_chars"] == 900
      and CFG.DEFAULT_CONFIG["context"]["artifact_digest_versions"] == 3)

# ══════════════════════════════════════════════════════════════════════════
print()
print("[C] 四落点接线（真实代码路径 + 桩 LLM）")
# ══════════════════════════════════════════════════════════════════════════
# C1 子任务上下文快照（机制）
_ctx_with = build_subtask_context({"task_key": "t1", "title": "T"}, "目标", [], artifact_digest=d11)
_ctx_without = build_subtask_context({"task_key": "t1", "title": "T"}, "目标", [])
check("C1 build_subtask_context 带摘要 → 含标记", DIGEST_MARK in _ctx_with)
check("C1b 不传摘要 → 输出不含标记（旧调用点零漂移）", DIGEST_MARK not in _ctx_without)
check("C1c 默认参数不改变旧输出（旧调用方式逐字节相同）",
      _ctx_without == build_subtask_context({"task_key": "t1", "title": "T"}, "目标", []))


class _StubAgentDef:
    name = "design"
    intent_name = "design"
    agent_role = "sub"
    system_prompt = ""


def _drive_stream(flow_fn, pipe, cid, planner_reply=None, spy=None):
    """驱动流式编排到指定落点：桩 LLM（call#1 可返回计划）+ 可选 spy。

    返回 (captured_prompts, spy_args, calls)。所有副作用只落在夹具库。

    打补丁必须落在**函数体内 import 的源模块**上：
      · `_stream_orchestrated_flow` 里是 `from database import db_conn, get_db`（函数内 import）
        → 必须 patch `database.get_db`；patch `S.get_db`（模块级同名对象）**无效**，
        实测它会让流程跑去读**生产库**（症状 = 摘要恒空，看起来像"注入代码没写对"）。
      · exec 出来的变异体（M3/M5）globals 是独立快照 → 额外按 flow_fn.__globals__ 兜一层。
    """
    captured, spy_hits = [], []
    calls = {"n": 0}
    orig_chat = LLM.llm_client.chat
    g = getattr(flow_fn, "__globals__", vars(S))
    orig_get_db = g.get("get_db")
    orig_db_get_db = DB.get_db
    orig_h_get_db = H.get_db
    orig_bsc = W.build_subtask_context

    def fake_chat(messages, **kw):
        calls["n"] += 1
        captured.append((kw.get("_intent"), messages))
        if calls["n"] == 1 and planner_reply is not None:
            return {"choices": [{"message": {"role": "assistant", "content": planner_reply}}]}
        raise Abort()

    def fake_bsc(*a, **kw):
        spy_hits.append((a, kw))
        raise Abort()

    LLM.llm_client.chat = fake_chat
    DB.get_db = new_conn                            # ← 关键：函数体内 import 的源模块
    g["get_db"] = new_conn
    H.get_db = new_conn                             # 子流程里的历史注入也走夹具库（确定性）
    if spy:
        W.build_subtask_context = fake_bsc          # 函数体内 import → 拦源模块属性
    try:
        gen = flow_fn(pipe, "优化需求数据，需要支持参与者信息", cid, "dev", "design",
                      _StubAgentDef(), "L1", [], [], {}, None, None)
        for _ in range(400):
            try:
                next(gen)
            except StopIteration:
                break
            except Abort:
                break
    finally:
        LLM.llm_client.chat = orig_chat
        DB.get_db = orig_db_get_db
        if orig_get_db is not None:
            g["get_db"] = orig_get_db
        H.get_db = orig_h_get_db
        W.build_subtask_context = orig_bsc
    return captured, spy_hits, calls["n"]


_pipe = AgentPipeline()
_pipe._load_db_agents()

# 注意：`AgentPipeline` 由 pipeline_parts/common.py 定义，`stream` 模块只是 `from .common import *`
# 间接获得、**未重新导出为模块属性** → 必须用真实类，不能写 S.AgentPipeline。
cap, _, n1 = _drive_stream(AgentPipeline._stream_orchestrated_flow, _pipe, 11)
_p1 = cap[0][1][0]["content"] if cap else ""
check("C2 流式 planner 真实提示词含摘要（真 generator 驱动）",
      DIGEST_MARK in _p1 and "ir2" in _p1, "calls=%d len=%d" % (n1, len(_p1)))
check("C2b 流式 planner 提示词含「在既有产物上做增量」硬指令",
      "在既有产物上做增量" in _p1)
cap0, _, _ = _drive_stream(AgentPipeline._stream_orchestrated_flow, _pipe, 12)
_p0 = cap0[0][1][0]["content"] if cap0 else ""
check("C3 无产物会话（conv=12）流式提示词**不含**摘要段",
      DIGEST_MARK not in _p0 and "会话既有产物" not in _p0, "len=%d" % len(_p0))

# C4 输入侧：非流式 planner（_planner_core 真实方法 + 桩 LLM）
def _drive_planner_core(fn, pipe, cid):
    captured = []
    orig_chat = LLM.llm_client.chat

    def fake_chat(messages, **kw):
        captured.append((kw.get("_intent"), messages))
        raise Abort()

    LLM.llm_client.chat = fake_chat
    try:
        fn(pipe, goal="优化需求数据，需要支持参与者信息", agents=["design"], max_tasks=3,
           parallel=False, provider_id=None, run_id=0, conn=FIX, conversation_id=cid)
    except Abort:
        pass
    except Exception as e:      # noqa: BLE001 —— 非 Abort 的其他异常说明路径不对，暴露出来
        print("      (planner_core 抛出 %s: %s)" % (type(e).__name__, str(e)[:100]))
    finally:
        LLM.llm_client.chat = orig_chat
    return captured[0][1][0]["content"] if captured else ""


W_planner_core = getattr(W.FlowPlannerMixin, "_planner_core")
_p_core = _drive_planner_core(W_planner_core, W.FlowPlannerMixin(), 11)
check("C4 非流式 planner 提示词含摘要 + 增量指令",
      DIGEST_MARK in _p_core and "在既有产物上做增量" in _p_core, "len=%d" % len(_p_core))
_p_core0 = _drive_planner_core(W_planner_core, W.FlowPlannerMixin(), 0)
check("C4b conversation_id=0（无会话）→ 非流式提示词不含摘要",
      DIGEST_MARK not in _p_core0, "len=%d" % len(_p_core0))

# C5 落点③ 子任务：spy 拦 build_subtask_context，断言拿到的 artifact_digest 有内容
_PLAN = json.dumps({"tasks": [{"key": "t1", "title": "在既有模型上补参与者",
                               "agent": "design", "deps": [], "task_type": "agent"}]},
                   ensure_ascii=False)
cap5, spy5, n5 = _drive_stream(AgentPipeline._stream_orchestrated_flow, _pipe, 11,
                               planner_reply=_PLAN, spy=True)
check("C5 子任务落点：build_subtask_context 收到非空 artifact_digest（真实调用点）",
      bool(spy5) and DIGEST_MARK in str(spy5[0][1].get("artifact_digest") or ""),
      "spy=%d calls=%d" % (len(spy5), n5))
check("C5b 该调用点同时传了 done_items 快照（未破坏既有参数位）",
      bool(spy5) and isinstance(spy5[0][0][2], list))

# ══════════════════════════════════════════════════════════════════════════
print()
print("[D] 落点④ 会话历史块 + 零回归对拍（exec 孪生体）")
# ══════════════════════════════════════════════════════════════════════════
_hpipe = AgentPipeline()
_hpipe._search_history = lambda *a, **k: []          # 拉回与摘要非本批范围，隔离掉（离线确定性）
_hpipe._history_summary = lambda *a, **k: ""
_orig_h_get_db = H.get_db
H.get_db = new_conn


def _load_history_variant(mutate=None):
    """把真实的 `_load_history` 源码取出来（可先做文本手术）→ exec 成可调用函数。

    ns 取**真实模块全局**（`vars(H)`）而非 `{"json": json}` —— 否则函数体里的 `get_db()`
    会 NameError，被它自己的 `except Exception: return []` 静默兜掉，孪生体全返回 `[]`，
    症状与"改动没生效"完全一样（本仓"静默兜底"家族第 N 次）。
    """
    src = textwrap.dedent(inspect.getsource(H.HistoryMixin._load_history))
    if mutate:
        src = mutate(src)
    ns = dict(vars(H))
    exec(compile(src, "<hist_twin>", "exec"), ns)
    return ns["_load_history"]


try:
    new_fn = _load_history_variant()
    h_new_11 = new_fn(_hpipe, 11)
    h_new_12 = new_fn(_hpipe, 12)

    def _strip_digest(src):
        """外科手术：删掉本批新增的三处 digest 代码（还原成改动前行为）。

        锚点用**行内容 + 运行时测出的缩进**，不写死空格数 —— `inspect.getsource` 后经
        `textwrap.dedent` 会整体左移 4 格（方法定义在类里），写死缩进会静默不命中。
        """
        lines = src.split("\n")
        hits = [i for i, l in enumerate(lines)
                if "from agent.session_artifacts import build_digest" in l]
        assert len(hits) == 1, "变异锚点(取数块)命中 %d 次（源码漂移）" % len(hits)
        i = hits[0]
        block = lines[i - 1:i + 6]
        assert (len(block) == 7 and block[0].strip() == "try:"
                and block[3].strip() == "if _dgt:"
                and block[5].strip() == "except Exception:"
                and block[6].strip() == "_digest = []"), \
            "变异锚点(块形状)不符：%r" % (block,)
        lines = lines[:i - 1] + lines[i + 6:]
        src = "\n".join(lines)
        assert src.count("_digest + ") == 2, "变异锚点(return)命中 %d 次" % src.count("_digest + ")
        return src.replace("_digest + ", "")

    old_fn = _load_history_variant(_strip_digest)
    h_old_11 = old_fn(_hpipe, 11)
    h_old_12 = old_fn(_hpipe, 12)

    check("D1 有产物会话：新版比旧版**多且仅多 1 条**，且是摘要段",
          len(h_new_11) == len(h_old_11) + 1
          and h_new_11[0]["role"] == "system" and DIGEST_MARK in h_new_11[0]["content"],
          "new=%d old=%d" % (len(h_new_11), len(h_old_11)))
    check("D1b 旧版内容是新版的尾段（历史注入本身零改动）",
          [dict(x) for x in h_new_11[1:]] == [dict(x) for x in h_old_11])
    check("D2 无产物会话：新旧两版**逐项相同**（零回归）",
          [dict(x) for x in h_new_12] == [dict(x) for x in h_old_12]
          and len(h_new_12) > 0, "n=%d" % len(h_new_12))
    check("D3 无产物会话返回里不含摘要标记", all(DIGEST_MARK not in (m.get("content") or "") for m in h_new_12))
    check("D4 历史块条目契约（role/content/id 三键齐备）",
          all({"role", "content", "id"} <= set(m.keys()) for m in h_old_12))

    # ══════════════════════════════════════════════════════════════════════
    print()
    print("[M] 变异自证（只认新增失败；每个变异配自己的目标断言）")
    # ══════════════════════════════════════════════════════════════════════

    def _flip(fn, *args, **kw):
        """返回该变异是否让目标断言失败（True=被抓住）。"""
        try:
            return not bool(fn(*args, **kw))
        except Abort:
            return False
        except Exception:      # noqa: BLE001 —— 抛异常也算"行为已变"，但要打印出来
            return True

    # M1 撤回历史块的摘要注入 → D1（多且仅多一条）必须翻
    m1 = _flip(lambda: len(old_fn(_hpipe, 11)) == len(h_old_11) + 1 and DIGEST_MARK in old_fn(_hpipe, 11)[0]["content"])
    check("M1 撤回 _load_history 摘要注入 → D1 目标断言失败（被抓住）", m1 is True, "m1=%s" % m1)

    # M2 撤回 build_subtask_context 的摘要注入 → C1 必须翻
    _src_bsc = textwrap.dedent(inspect.getsource(W.build_subtask_context))
    _mut_bsc = _src_bsc.replace('    if artifact_digest:\n        parts.append(str(artifact_digest))\n', '')
    check("M2 变异锚点命中（build_subtask_context 注入块）", _mut_bsc != _src_bsc)
    _ns = dict(vars(W))
    exec(compile(_mut_bsc, "<bsc_mut>", "exec"), _ns)
    m2 = _flip(lambda: DIGEST_MARK in _ns["build_subtask_context"](
        {"task_key": "t1", "title": "T"}, "目标", [], artifact_digest=d11))
    check("M2 撤回子任务快照注入 → C1 目标断言失败（被抓住）", m2 is True, "m2=%s" % m2)

    # M3 撤回流式 planner 的摘要注入 → C2 必须翻
    # ⚠️ 2026-10-05（两次踩坑，勿回退）：
    #   ① `textwrap.dedent(getsource(_stream_orchestrated_flow))` **静默失效** ——
    #      函数体内多行 prompt 含顶格行 ⇒ dedent 算出公共缩进 0 ⇒ 原样返回（首行仍 4 格）
    #      ⇒ exec 抛 IndentationError，且因异常中断 try 块，M5 的断言**根本没执行**（假绿）。
    #      ⇒ 改用 `_srctool.src_of`（按首行缩进剥离）。
    #   ② 缩进锚点不可写死：`_digest` 计算被包进 try/except 后缩进从 4/8 变 8/12，
    #      写死 `    if _digest:` 静默不命中（`find` 返回 -1 ⇒ 拼出畸形源码）。
    #      ⇒ 一律按**行内容**定位。
    _flow_src = _srctool.src_of(AgentPipeline._stream_orchestrated_flow)
    _fl = _flow_src.split("\n")
    _m3_start, _m3_end = -1, -1
    for _i in range(len(_fl) - 1):
        if "if _digest:" in _fl[_i] and "plan_prompt += (" in _fl[_i + 1]:
            _m3_start = _i
            break
    if _m3_start >= 0:
        for _j in range(_m3_start + 1, len(_fl)):
            if "if stage_hint:" in _fl[_j]:
                _m3_end = _j
                break
    check("M3 变异锚点命中（stream 摘要注入块）",
          _m3_start >= 0 and _m3_end > _m3_start, "start=%s end=%s" % (_m3_start, _m3_end))
    _mut_flow = "\n".join(_fl[:_m3_start] + _fl[_m3_end:])
    _ns3 = dict(AgentPipeline._stream_orchestrated_flow.__globals__)
    exec(compile(_mut_flow, "<flow_mut>", "exec"), _ns3)
    cap3, _, _ = _drive_stream(_ns3["_stream_orchestrated_flow"], _pipe, 11)
    _p3 = cap3[0][1][0]["content"] if cap3 else ""
    check("M3 撤回流式 planner 注入 → C2 目标断言失败（被抓住）",
          (not (DIGEST_MARK in _p3 and "ir2" in _p3)) is True,
          "变体 prompt 含标记=%s len=%d" % (DIGEST_MARK in _p3, len(_p3)))

    # M4 撤回非流式 planner 注入 → C4 必须翻（锚点同为 dedent 后的 4/8 格）
    _pc_src = textwrap.dedent(inspect.getsource(W.FlowPlannerMixin._planner_core))
    _m4_start = _pc_src.find("    if artifact_digest:\n        prompt += (")
    _m4_end = _pc_src.find("    try:\n        resp = llm_client.chat", _m4_start)
    check("M4 变异锚点命中（planner_core 注入块）", _m4_start > 0 and _m4_end > _m4_start)
    _mut_pc = _pc_src[:_m4_start] + _pc_src[_m4_end:]
    _ns4 = dict(vars(W))
    exec(compile(_mut_pc, "<pc_mut>", "exec"), _ns4)
    _p4 = _drive_planner_core(_ns4["_planner_core"], W.FlowPlannerMixin(), 11)
    check("M4 撤回非流式 planner 注入 → C4 目标断言失败（被抓住）",
          (DIGEST_MARK not in _p4) is True, "变体含标记=%s len=%d" % (DIGEST_MARK in _p4, len(_p4)))

    # M5 子任务调用点漏传 artifact_digest → C5 必须翻
    _mut_flow5 = _flow_src.replace("artifact_digest=_digest", "artifact_digest=''")
    check("M5 变异锚点命中（子任务传参）", _mut_flow5 != _flow_src)
    _ns5 = dict(AgentPipeline._stream_orchestrated_flow.__globals__)
    exec(compile(_mut_flow5, "<flow_mut5>", "exec"), _ns5)
    _, spy_m5, _ = _drive_stream(_ns5["_stream_orchestrated_flow"], _pipe, 11,
                                 planner_reply=_PLAN, spy=True)
    check("M5 子任务漏传摘要 → C5 目标断言失败（被抓住）",
          (not (bool(spy_m5) and DIGEST_MARK in str(spy_m5[0][1].get("artifact_digest") or ""))) is True,
          "spy=%d" % len(spy_m5))

    # M6 开关默认值改 False（守卫"默认开启"） → A1 必须翻
    _orig_as_bool2 = CFG.as_bool
    try:
        CFG.as_bool = lambda *a, **k: False
        check("M6 开关默认置 False → A1/C2 全部失效（默认开启被守住）",
              build_digest(FIX, 11) == "")
    finally:
        CFG.as_bool = _orig_as_bool2

    # M7 render_digest 截断失效（cap 被忽略） → A5 必须翻
    _rd_src = textwrap.dedent(inspect.getsource(render_digest))
    _mut_rd = _rd_src.replace("    cap = int(max_chars or 0)", "    cap = 0  # M7: 截断失效")
    check("M7 变异锚点命中（render_digest 截断）", _mut_rd != _rd_src)
    _ns7 = dict(vars(SA))
    exec(compile(_mut_rd, "<rd_mut>", "exec"), _ns7)
    _c7 = _ns7["render_digest"](collect_facts(FIX, 11), max_chars=120)
    check("M7 截断失效 → A5 目标断言失败（被抓住）", (len(_c7) <= 120) is False, "len=%d" % len(_c7))

    # M8 撤回 P1-7 的占位名过滤（continue → pass） → A10 必须翻
    # 锚点必须缩进无关（§6.10 ④）：dedent 后类外函数是 8 格，写死会静默不命中。
    _rd_src2 = textwrap.dedent(inspect.getsource(render_digest))
    _m8_old = ("        if skip_placeholder_only and uniq and not real:\n"
               "            continue")
    _mut_rd2 = _rd_src2.replace(_m8_old, _m8_old.replace("            continue", "            pass"))
    check("M8 变异锚点命中（render_digest 占位名过滤）", _mut_rd2 != _rd_src2)
    _ns8 = dict(vars(SA))
    exec(compile(_mut_rd2, "<rd_mut8>", "exec"), _ns8)
    _c8 = _ns8["render_digest"](collect_facts(FIX, 13), max_chars=3000)
    check("M8 过滤失效 → A10 目标断言失败（被抓住）",
          ("AI 生成文档" not in _c8) is False, "'AI 生成文档' 出现 %d 次" % _c8.count("AI 生成文档"))

    # M8b 去掉标题去重 → A17c 必须翻（**改用重复的真实标题**，不再用占位名 —— 占了会被整行丢弃）
    _mut_rd2b = _rd_src2.replace("        uniq = _uniq_titles(titles)", "        uniq = list(titles)")
    check("M8b 变异锚点命中（render_digest 标题去重）", _mut_rd2b != _rd_src2)
    _ns8b = dict(vars(SA))
    exec(compile(_mut_rd2b, "<rd_mut8b>", "exec"), _ns8b)
    _c8b = _ns8b["render_digest"](collect_facts(FIX, 15), max_chars=3000)
    check("M8b 去掉去重 → A17c 目标断言失败（被抓住）",
          (_c8b.count("代码块 1（sysml）") == 1) is False,
          "'代码块 1（sysml）' 出现 %d 次" % _c8b.count("代码块 1（sysml）"))

    # M13 过滤过度：把所有标题都判成占位名 → A17 必须翻
    _ns13 = dict(vars(SA))
    _ns13["is_placeholder_title"] = lambda _t: True
    exec(compile(_rd_src2, "<rd_mut13>", "exec"), _ns13)
    _c13m = _ns13["render_digest"](collect_facts(FIX, 15), max_chars=3000)
    check("M13 过滤过度（真标题也被丢） → A17 目标断言失败（被抓住）",
          ("整车需求规格说明" in _c13m) is False, repr(_c13m[-120:]))

    # M14 _LEAD 回到 126 字符 → A15 必须翻（同时证明 _LEAD 真的进了输出，不是死常量）
    _OLD_LEAD = ("本会话此前已产出下列内容。本轮是多轮会话的后续请求：若本请求与它们相关，"
                 "请在**既有产物上做增量修改**（明确引用其名称/版本，例如「在 v0.1 的需求模型上新增参与者」），"
                 "不要从零重建、也不要把它们当作不存在；若本请求确实与它们无关，忽略本段即可。")
    _ns14 = dict(vars(SA))
    _ns14["_LEAD"] = _OLD_LEAD
    exec(compile(_rd_src2, "<rd_mut14>", "exec"), _ns14)
    _c14m = _ns14["render_digest"](collect_facts(FIX, 11), max_chars=3000)
    check("M14 _LEAD 回到压缩前 → A15 目标断言失败且 LEAD 确在输出中（被抓住）",
          (len(_ns14["_LEAD"]) <= 60) is False and _OLD_LEAD in _c14m,
          "len=%d 在输出中=%s" % (len(_ns14["_LEAD"]), _OLD_LEAD in _c14m))

    # M15 判据放宽成「以产物结尾」正则 → A14c 必须翻（误伤真实标题）
    _ipt_src = textwrap.dedent(inspect.getsource(is_placeholder_title))
    _m15_old = '    return (title or "").strip() in PLACEHOLDER_TITLES'
    _mut_ipt = _ipt_src.replace(
        _m15_old, '    return bool((title or "").strip().endswith("产物"))')
    check("M15 变异锚点命中（is_placeholder_title 判据）", _mut_ipt != _ipt_src)
    _ns15 = dict(vars(__import__("core.artifact_titles", fromlist=["x"])))
    exec(compile(_mut_ipt, "<ipt_mut>", "exec"), _ns15)
    check("M15 判据放宽 → A14c 目标断言失败（「设计产物」被误判，被抓住）",
          _ns15["is_placeholder_title"]("设计产物") is True)

    # M16 过滤后为空仍输出（空壳摘要） → A16 必须翻
    _mut_rd16 = _rd_src2.replace("    if not lines:\n        return \"\"",
                                 "    if False:\n        return \"\"")
    check("M16 变异锚点命中（render_digest 空事实早退）", _mut_rd16 != _rd_src2)
    _ns16 = dict(vars(SA))
    exec(compile(_mut_rd16, "<rd_mut16>", "exec"), _ns16)
    _c16 = _ns16["render_digest"](collect_facts(FIX, 14), max_chars=3000)
    check("M16 空壳摘要仍输出 → A16 目标断言失败（被抓住）",
          (_c16 == "") is False, "len=%d" % len(_c16))

    # ══════════════════════════════════════════════════════════════════════
    print()
    print("[R] 零回归对拍：真实版本 vs 「改动前」变体（逐字节）")
    # ══════════════════════════════════════════════════════════════════════
    # 复用的"改动前"变体：_ns3=去掉摘要注入的流式 flow；_ns4=去掉摘要注入的 _planner_core。
    # R1/R3 是**逐字节**同 —— 证明「无产物会话，本批新增通道贡献 0 字节」。
    # R2/R4 是**前缀同** —— 证明「有产物会话，除末尾追加的摘要段外逐字节未动」。
    r1, _, _ = _drive_stream(AgentPipeline._stream_orchestrated_flow, _pipe, 12)
    r1b, _, _ = _drive_stream(_ns3["_stream_orchestrated_flow"], _pipe, 12)
    r1p = r1[0][1][0]["content"] if r1 else ""
    r1q = r1b[0][1][0]["content"] if r1b else ""
    check("R1 无产物会话（流式 planner）：真实版 == 改动前版（逐字节，0 字节漂移）",
          bool(r1p) and r1p == r1q, "len=%d vs %d" % (len(r1p), len(r1q)))

    r2, _, _ = _drive_stream(AgentPipeline._stream_orchestrated_flow, _pipe, 11)
    r2b, _, _ = _drive_stream(_ns3["_stream_orchestrated_flow"], _pipe, 11)
    r2p = r2[0][1][0]["content"] if r2 else ""
    r2q = r2b[0][1][0]["content"] if r2b else ""
    check("R2 有产物会话（流式 planner）：改动前版是真实版的**前缀**，差异全在末尾摘要段",
          bool(r2q) and r2p.startswith(r2q) and DIGEST_MARK in r2p[len(r2q):],
          "len=%d vs %d（差值 %d）" % (len(r2p), len(r2q), len(r2p) - len(r2q)))

    r3 = _drive_planner_core(W_planner_core, W.FlowPlannerMixin(), 12)
    r3b = _drive_planner_core(_ns4["_planner_core"], W.FlowPlannerMixin(), 12)
    check("R3 无产物会话（非流式 planner）：真实版 == 改动前版（逐字节）",
          bool(r3) and r3 == r3b, "len=%d vs %d" % (len(r3), len(r3b)))

    r4 = _drive_planner_core(W_planner_core, W.FlowPlannerMixin(), 11)
    r4b = _drive_planner_core(_ns4["_planner_core"], W.FlowPlannerMixin(), 11)
    check("R4 有产物会话（非流式 planner）：改动前版是真实版的前缀，差异全在末尾摘要段",
          bool(r4b) and r4.startswith(r4b) and DIGEST_MARK in r4[len(r4b):],
          "len=%d vs %d（差值 %d）" % (len(r4), len(r4b), len(r4) - len(r4b)))

    _w = build_subtask_context({"task_key": "t1", "title": "T"}, "目标", [], artifact_digest=d11)
    check("R5 子任务快照：不带摘要的输出是不带摘要版本本身的**前缀**，差异全在末尾",
          _w.startswith(_ctx_without) and DIGEST_MARK in _w[len(_ctx_without):],
          "差值 %d" % (len(_w) - len(_ctx_without)))

    _stripped_hist = old_fn(_hpipe, 11)
    check("R6 历史块：改动前版是新版尾段（摘要前置，其余逐字节相同）",
          [dict(x) for x in h_new_11[1:]] == [dict(x) for x in _stripped_hist])
except Exception as _e:      # noqa: BLE001 —— 锚点漂移/路径异常一律记 FAIL，别让脚本裸崩丢失前面的结果
    import traceback
    check("D/M 组执行期异常（锚点漂移或路径变更）", False, "%s: %s" % (type(_e).__name__, _e))
    traceback.print_exc()
finally:
    H.get_db = _orig_h_get_db

# ══════════════════════════════════════════════════════════════════════════
print()
print("[E] P1-8 历史（输入/输出）相关性召回收口（真实 _load_history；离线 bigram 打分）")
# ══════════════════════════════════════════════════════════════════════════
_CUR_T = "需求追溯"
from core.token_counter import count_tokens as count_tokens_fn            # noqa: E402
_epipe = AgentPipeline()
_epipe._history_summary = lambda *a, **k: ""                       # 分话题摘要非本批范围
# 离线确定性：`_semantic_scores` 返回 bigram 后端 ⇒ 走 VectorEngine（无网络、无 embedding 依赖）。
# dense 路与之只差**打分来源与阈值**，本批新增的三处收口（均分/used_ids/配额/块头）两路共用。
_epipe._semantic_scores = lambda texts, query: (None, "bigram")
_orig_get_e = CFG.get
H.get_db = new_conn


def _h_run(cid, cur_input=P18_CUR, cfg_over=None):
    """跑真实 `_load_history`（可临时覆盖 context 配置项；只覆盖指定键）。"""
    def _fake_get(group, key, default=None, *a, **k):
        if group == "context" and cfg_over and key in cfg_over:
            return cfg_over[key]
        return _orig_get_e(group, key, default, *a, **k)
    CFG.get = _fake_get
    try:
        return _epipe._load_history(cid, cur_input=cur_input)
    finally:
        CFG.get = _orig_get_e


def _raws(hist):
    """① 近因窗口原文（带 id，时间序）。"""
    return [m for m in hist if m.get("role") in ("user", "assistant")]


def _pulls(hist):
    """② 相关性拉回块（system 块，带块头）。"""
    return [m["content"] for m in hist
            if m.get("role") == "system"
            and (m.get("content") or "").startswith("【相关历史片段")]


_HDR_RE = __import__("re").compile(
    r"^【相关历史片段（第 \d+ 轮 · (用户|助手)，相关度 [\d.]+）】$")

# ⚠️ `FIX` 是裸连接（无 row_factory）→ 按**位置**取值，别写 r["id"]（会 TypeError）。
_r16 = FIX.execute("SELECT id, role, content FROM messages WHERE conversation_id=16 ORDER BY id").fetchall()
_ids16 = [r[0] for r in _r16]
_tail16 = _ids16[-8:]
_echo_id = _r16[8][0]              # 第 9 条 = 与 P18_CUR 逐字相同
_r17 = FIX.execute("SELECT id, role, content FROM messages WHERE conversation_id=17 ORDER BY id").fetchall()
_hi_rel17_id = _r17[2][0]          # 第 3 条 = 高相关 user 消息（低预算下会被挡在窗口外）

_h16 = _h_run(16)
_raw16, _pull16 = _raws(_h16), _pulls(_h16)
check("E1 近因窗口 8 条**全部**注入（旧实现「吃满为止」：最新长回复独占预算 ⇒ 只装 1 条）",
      [m.get("id") for m in _raw16] == _tail16, "raw_ids=%s" % [m.get("id") for m in _raw16])
_u16 = [m["content"] for m in _raw16 if m["role"] == "user"]
check("E2 窗口内 3 条用户输入**全文**在位（旧实现：最新一条被裁成 16tok 残句、另两条整条消失）",
      _u16 == ["继续优化需求数据", "继续优化需求数据", "继续优化需求数据，补充参与者与涉众信息"],
      repr(_u16))

# E3 前置（反空洞断言）：逐字重复的那条**本该**是最高分命中 —— 证明 E3b 不是"它恰好没命中"
_rest10 = [{"id": r[0], "role": r[1], "content": (r[2] or "")[:1500],
            "topic": _CUR_T} for r in _r16[:10]]
_hits_raw = _epipe._search_history(_rest10, P18_CUR + " " + _CUR_T, 50, 0.15,
                                   cur_topic=_CUR_T, per_role_cap=0)
_echo_hit = [h for h in _hits_raw if h["id"] == _echo_id]
check("E3a 前置：逐字重复的消息本是最高分命中（≥0.9），不是「恰好没命中」",
      bool(_echo_hit) and _echo_hit[0]["score"] >= 0.9,
      "top3=%s" % [(h["id"], h["score"]) for h in _hits_raw[:3]])
check("E3b 但拉回块里没有它（候选层已过滤：零信息量不参与相关性评估）",
      not any(P18_CUR in b for b in _pull16))
_nu16 = sum(1 for b in _pull16 if "· 用户" in b)
_na16 = sum(1 for b in _pull16 if "· 助手" in b)
check("E3c 且没白占配额：默认 cap=3 时 user 侧仍拿满 3 条",
      _nu16 == 3, "user=%d asst=%d" % (_nu16, _na16))

check("E4 拉回块头可追溯（「第 N 轮 · 角色」，不是无出处的「相关历史片段」）",
      bool(_pull16) and all(_HDR_RE.match(b.split("\n")[0]) for b in _pull16),
      repr(_pull16[0].split("\n")[0]) if _pull16 else "无拉回块")
check("E5 分角色配额：user 与 assistant **都有**代表（混合 top-k 会被高分短文本通吃）",
      _nu16 >= 1 and _na16 >= 1, "user=%d asst=%d" % (_nu16, _na16))
check("E5b 每角色不超过配额且总数 ≤ topk",
      _nu16 <= 3 and _na16 <= 3 and len(_pull16) <= 6,
      "user=%d asst=%d total=%d" % (_nu16, _na16, len(_pull16)))

# E6 配额**不减产**：单角色命中时按分数补齐到 topk（去掉第二轮补齐就会退化成「只取 3 条」）
_only_u = [m for m in _rest10 if m["role"] == "user"]
_h6_all = _epipe._search_history(_only_u, P18_CUR + " " + _CUR_T, 6, 0.15,
                                 cur_topic=_CUR_T, per_role_cap=0)
_h6_cap = _epipe._search_history(_only_u, P18_CUR + " " + _CUR_T, 6, 0.15,
                                 cur_topic=_CUR_T, per_role_cap=3)
check("E6 配额不减产：单角色命中 > 配额时，补齐后条数与不限额完全一致",
      len(_h6_all) > 3 and len(_h6_cap) == len(_h6_all),
      "不限=%d cap3=%d" % (len(_h6_all), len(_h6_cap)))

# E7 低预算场景（budget_history_tokens=400）：**被预算挡下**的消息仍须参与相关性拉回
_h17 = _h_run(17, cfg_over={"budget_history_tokens": 400})
_raw17, _pull17 = _raws(_h17), _pulls(_h17)
check("E7a 前置：低预算下近因窗口确实装不下全部（预算真的吃紧）",
      len(_raw17) < 8, "raw=%d" % len(_raw17))
check("E7b 前置：高相关那条 user 消息被预算挡在近因窗口外",
      _hi_rel17_id not in [m.get("id") for m in _raw17], "raw_ids=%s" % [m.get("id") for m in _raw17])
check("E7c 但它仍进入相关性拉回（旧 `used_ids` 把它标成「已用」⇒ 彻底消失）",
      any("请补充涉众与参与者的追溯关系定义" in b and "第 2 轮 · 用户" in b for b in _pull17),
      "拉回 %d 条：%s" % (len(_pull17), [b.split("\n")[0] for b in _pull17]))

check("E8 DEFAULT_CONFIG.context 含 P1-8 三键且默认值正确",
      CFG.DEFAULT_CONFIG["context"].get("history_raw_min_share_tokens") == 96
      and CFG.DEFAULT_CONFIG["context"].get("topic_retrieve_per_role_cap") == 3
      and CFG.DEFAULT_CONFIG["context"].get("topic_retrieve_drop_echo") is True)
check("E8b CONFIG_SCHEMA.context 含三键（前端契约）",
      all(k in CFG.CONFIG_SCHEMA["context"] for k in
          ("history_raw_min_share_tokens", "topic_retrieve_per_role_cap", "topic_retrieve_drop_echo")))

_pull_nocap = _pulls(_h_run(16, cfg_over={"topic_retrieve_per_role_cap": 0}))
_nu0 = sum(1 for b in _pull_nocap if "· 用户" in b)
check("E9 per_role_cap=0（回滚）→ 回到混合 top-k：user 侧条数超过配额上限",
      _nu0 > 3, "cap0 user=%d vs cap3 user=%d" % (_nu0, _nu16))
_pull_echo = _pulls(_h_run(16, cfg_over={"topic_retrieve_drop_echo": False}))
check("E9b drop_echo=False（回滚）→ 逐字重复的片段重新被拉回（A/B 互证）",
      any(P18_CUR in b for b in _pull_echo),
      "块头=%s" % [b.split("\n")[0] for b in _pull_echo])

# E10 短路零回归：均分**只在总需求超预算时**才启用 —— 装得下就不得切
_h18 = _h_run(18)
_ass18 = [m for m in _raws(_h18) if m["role"] == "assistant"]
check("E10 短会话（2 条）零回归：唯一那条长回复（828 tok < raw_cap 1000）**完整**在位",
      len(_ass18) == 1 and _ass18[0]["content"] == _SHORT18,
      "len=%s / 原文 %d" % (len(_ass18[0]["content"]) if _ass18 else None, len(_SHORT18)))
# E11 超预算会话：均分后的**余量要回填**（否则 ① 的 50% 额度被白扔）
_raw_cap18 = int(H.ctx_budget_tokens("history", "budget_history_chars", 3000) * 0.5)
_share16 = max(96, _raw_cap18 // len(_tail16))
_newest16 = [m for m in _raw16 if m.get("id") == _tail16[-1]]
_ntok16 = count_tokens_fn(_newest16[0]["content"]) if _newest16 else 0
check("E11 超预算会话：余量回填生效（最新一条原文 > 均分份额 %d tok）" % _share16,
      bool(_newest16) and _ntok16 > _share16,
      "最新一条 %d tok（均分上限 %d）；raw_cap=%d" % (_ntok16, _share16, _raw_cap18))


def _hist_variant_e(mutate=None):
    """`_load_history` 的 exec 孪生体（文本手术用；ns 取真实模块全局，见 D 组说明）。"""
    src = textwrap.dedent(inspect.getsource(H.HistoryMixin._load_history))
    if mutate:
        src = mutate(src)
    ns = dict(vars(H))
    exec(compile(src, "<hist_twin_e>", "exec"), ns)
    return ns["_load_history"]


# ── P1-8 变异自证（只认新增失败）──
# ⚠️ 锚点缩进必须取 `textwrap.dedent` 之后的**真实值**：`_load_history`/`_search_history`
#    是类方法，dedent 会按 `def` 的 4 格整体左移 ⇒ 方法体一级语句是 **4 格**、循环体内是 8 格。
#    写死 8 格会静默不命中（本仓"锚点漂移"家族）；核对脚本：tmp/mt_ctx/dump_anchors_p1_8.py。
_M17_ANCHOR = '    used_ids = {m["id"] for m in raw_list if m.get("id")}'
_M17_OLDCODE = '\n    used_ids.update(m["id"] for m in cur_group["msgs"][-cur_max:])'
_src_h = textwrap.dedent(inspect.getsource(H.HistoryMixin._load_history))
_mut17 = _src_h.replace(_M17_ANCHOR, _M17_ANCHOR + _M17_OLDCODE)
check("M17 变异锚点命中（used_ids 语义）", _mut17 != _src_h)
_f17 = _hist_variant_e(lambda s: s.replace(_M17_ANCHOR, _M17_ANCHOR + _M17_OLDCODE))
CFG.get = lambda group, key, default=None, *a, **k: (
    400 if (group == "context" and key == "budget_history_tokens")
    else _orig_get_e(group, key, default, *a, **k))
try:
    _p17m = _pulls(_f17(_epipe, 17, cur_input=P18_CUR))
finally:
    CFG.get = _orig_get_e
check("M17 恢复旧 used_ids（超额标记） → E7c 目标断言失败（被抓住）",
      (any("请补充涉众与参与者的追溯关系定义" in b for b in _p17m)) is False,
      "拉回 %d 条" % len(_p17m))

_M18_ANCHOR = "    _share = max(raw_min_share, raw_cap // max(len(_rcands), 1))"
_M18_NEW = "    _share = raw_cap * 1000  # M18: 回到「吃满为止」"
_mut18 = _src_h.replace(_M18_ANCHOR, _M18_NEW)
check("M18 变异锚点命中（均分份额）", _mut18 != _src_h and _src_h.count(_M18_ANCHOR) == 1)
f18 = _hist_variant_e(lambda s: s.replace(_M18_ANCHOR, _M18_NEW))
_r18m = _raws(f18(_epipe, 16, cur_input=P18_CUR))
check("M18 回到「吃满为止」 → E1 目标断言失败（近因窗口装不满，被抓住）",
      ([m.get("id") for m in _r18m] == _tail16) is False,
      "raw=%d 条（真实实现 8 条）" % len(_r18m))

# 单行锚点（跨行续行带 34 格对齐缩进，写死易漂）——`per_role_cap=pull_per_role` 在本方法内唯一
_M19_ANCHOR = "per_role_cap=pull_per_role"
_mut19 = _src_h.replace(_M19_ANCHOR, "per_role_cap=0")
check("M19 变异锚点命中（分角色配额传参）", _mut19 != _src_h and _src_h.count(_M19_ANCHOR) == 1)
f19 = _hist_variant_e(lambda s: s.replace(_M19_ANCHOR, "per_role_cap=0"))
_p19m = _pulls(f19(_epipe, 16, cur_input=P18_CUR))
check("M19 撤回分角色配额 → E5b 目标断言失败（user 侧超配额，被抓住）",
      (sum(1 for b in _p19m if "· 用户" in b) <= 3) is False,
      "user=%d" % sum(1 for b in _p19m if "· 用户" in b))

_M20_ANCHOR = ('    if drop_echo and _echo:\n'
               '        rest = [m for m in rest '
               'if re.sub(r"\\s+", "", m.get("content") or "") != _echo]')
_M20_NEW = "    if False and _echo:\n        pass  # M20"
_mut20 = _src_h.replace(_M20_ANCHOR, _M20_NEW)
check("M20 变异锚点命中（drop_echo 候选层过滤）", _mut20 != _src_h)
f20 = _hist_variant_e(lambda s: s.replace(_M20_ANCHOR, _M20_NEW))
_p20m = _pulls(f20(_epipe, 16, cur_input=P18_CUR))
check("M20 撤回 drop_echo → E3b 目标断言失败（重复块重新出现，被抓住）",
      (not any(P18_CUR in b for b in _p20m)) is False,
      "出现 %d 次" % sum(1 for b in _p20m if P18_CUR in b))

# 只替换**格式串**那一段（保持 3 个占位符 → 续行实参个数不变，不会 TypeError）：
# 变异后块头长成「相关度 1 用户 0.988」，不再是「第 N 轮 · 角色」形态 ⇒ E4 的形态判据必须翻。
_M21_ANCHOR = '        head = "【相关历史片段（第 %d 轮 · %s，相关度 %s）】" % ('
_M21_NEW = '        head = "【相关历史片段（相关度 %s %s %s）】" % ('
_mut21 = _src_h.replace(_M21_ANCHOR, _M21_NEW)
check("M21 变异锚点命中（拉回块头）", _mut21 != _src_h)
f21 = _hist_variant_e(lambda s: s.replace(_M21_ANCHOR, _M21_NEW))
_p21m = _pulls(f21(_epipe, 16, cur_input=P18_CUR))
check("M21 块头退回无出处形态 → E4 目标断言失败（被抓住）",
      (bool(_p21m) and all(_HDR_RE.match(b.split("\n")[0]) for b in _p21m)) is False,
      repr(_p21m[0].split("\n")[0]) if _p21m else "无拉回块")

# M22 配额「取满即停」（去掉第二轮补齐） → E6 目标断言失败
_sh_src = textwrap.dedent(inspect.getsource(H.HistoryMixin._search_history))
# 缩进 8 格 = dedent 后**方法体二级**（`if per_role_cap` 内）；12 格是改动前的错值 → 静默不命中
_M22_ANCHOR = "        if len(picked) < topk:                  # 第二轮：名额没满 → 按分数补齐（防单角色减产）"
_mut22 = _sh_src.replace(_M22_ANCHOR, "        if False:                              # M22: 不补齐")
check("M22 变异锚点命中（配额第二轮补齐）", _mut22 != _sh_src and _sh_src.count(_M22_ANCHOR) == 1)
_ns22 = dict(vars(H))
exec(compile(_mut22, "<sh_mut22>", "exec"), _ns22)
_h22m = _ns22["_search_history"](_epipe, _only_u, P18_CUR + " " + _CUR_T, 6, 0.15,
                                 cur_topic=_CUR_T, per_role_cap=3)
check("M22 配额不补齐 → E6 目标断言失败（单角色被减产，被抓住）",
      (len(_h22m) == len(_h6_all)) is False, "cap3不补齐=%d vs 不限=%d" % (len(_h22m), len(_h6_all)))

# M23 去掉**第二轮余额回填** → E10（短会话长回复被白切）与 E11（① 额度被白扔）双双失败。
#   ⚠️ 一处变异守两条断言是**有意**的：两条断言是「同一机制」的两种真实形态（2 条消息的短会话 /
#   8 条消息的超预算会话）。曾有过第二个机制（「总需求 ≤ 预算才不均分」）能独立挡住 E10，
#   但它与回填**功能重叠** ⇒ M23 会退化成空转变异（回填照样把内容补回来）⇒ 已按纪律删掉那个机制。
_M23_ANCHOR = "    if used < raw_cap and raw_list:"
_mut23 = _src_h.replace(_M23_ANCHOR, "    if False:  # M23: 去掉回填")
check("M23 变异锚点命中（第二轮余额回填）", _mut23 != _src_h and _src_h.count(_M23_ANCHOR) == 1)
f23 = _hist_variant_e(lambda s: s.replace(_M23_ANCHOR, "    if False:"))
_a23 = [m for m in _raws(f23(_epipe, 18, cur_input=P18_CUR)) if m["role"] == "assistant"]
_n23 = [m for m in _raws(f23(_epipe, 16, cur_input=P18_CUR)) if m.get("id") == _tail16[-1]]
_n23tok = count_tokens_fn(_n23[0]["content"]) if _n23 else 0
check("M23 去掉回填 → E10 目标断言失败（短会话长回复被白切，被抓住）",
      (bool(_a23) and _a23[0]["content"] == _SHORT18) is False,
      "len=%s / 原文 %d" % (len(_a23[0]["content"]) if _a23 else None, len(_SHORT18)))
check("M23 去掉回填 → E11 目标断言失败（最新一条卡在均分份额，被抓住）",
      (_n23tok > _share16) is False, "最新一条 %d tok（均分上限 %d）" % (_n23tok, _share16))

H.get_db = _orig_h_get_db

# ══════════════════════════════════════════════════════════════════════════
print()
print("[F] P1-9 写入侧取名：不再产生系统性兜底名（纯函数 + 真实落库路径 + 变异）")
# ══════════════════════════════════════════════════════════════════════════
# 背景：P1-7 只治**读取侧**（把占位名行整行过滤掉）。代价是「连计数一起丢」—— 实测真库
# conv=1/351/385 的产物摘要**整段为空（len=0）**，即这些会话的产物对 LLM 完全不可见。
# P1-9 治**写入侧**：从内容取名（真库 26 条占位名 **26/26 都能取到**）。
# 下游实测（副本）：摘要 0 → 196 / 101 / 95，真实标题回到提示词里。
import agent.utils as U                                                    # noqa: E402
import core.artifact_titles as AT                                          # noqa: E402
from services.artifact_materializer import materialize_to_conversation     # noqa: E402

_archive_artifacts = U._archive_artifacts          # agent/pipeline.py 的 re-export 同源

# ── F1~F8 纯函数（无 IO、无 LLM）──────────────────────────────────────────
# ⚠️ F1 的输入必须是「标题行**不在第一行**」—— 否则「取首行」与「取标题行」结果相同，
#    断言分不出两种实现（空转）。这里用「导语 + 空行 + ## 标题」的真实形态。
check("F1 优先取 markdown 标题行（**不是**首行）",
      derive_title_from_content("导语第一行\n\n## 真正的标题\n正文") == "真正的标题",
      repr(derive_title_from_content("导语第一行\n\n## 真正的标题\n正文")))
check("F2 无标题行 → 取首个非空行", derive_title_from_content("\n\n  正文第一句\n第二句") == "正文第一句")
# F3 守的是本轮**真实踩到的**一个 bug：首版让左右两端共用一份 strip 字符集，集合里有 `）`，
# 于是 `…（推进剂加注量 · 3 层深度）` 的右括号被无条件削掉 ⇒ 标题括号**不成对**。
check("F3 两端剥除**故意不对称**：右括号必须保留",
      derive_title_from_content("# 巡飞弹参数变更影响分析报告（推进剂加注量 + 巡飞速度 · 3 层深度）")
      == "巡飞弹参数变更影响分析报告（推进剂加注量 + 巡飞速度 · 3 层深度）")
check("F3b 左端 markdown 记号剥除（`>` 引用 / `**` 粗体 / `-` 列表）",
      derive_title_from_content("> 引用块标题") == "引用块标题"
      and derive_title_from_content("**粗体标题**") == "粗体标题"
      and derive_title_from_content("- 列表标题") == "列表标题")
check("F3c 行内**闭合**记号也剥掉（`**` 落在串中间，两端规则管不到）",
      derive_title_from_content("**编制说明**：本报告仅整合 t1") == "编制说明：本报告仅整合 t1")
# F4 是关键防线：不能"改了名字但依然零信息量"——那只是把 bug 从写入侧搬到读取侧
# （读取侧 is_placeholder_title 会认不出它，过滤失效）。
check("F4 内容首行本身就是兜底名 → 返回空（**不把 bug 从写入侧搬到读取侧**）",
      derive_title_from_content("AI 生成文档\n后面还有正文") == ""
      and derive_title_from_content("AI 生成报告") == ""
      and derive_title_from_content("doc产物") == "")
check("F5 空 / None / 全空白 → 空（由调用方退兜底名，不在这里硬造）",
      derive_title_from_content("") == "" and derive_title_from_content(None) == ""
      and derive_title_from_content("   \n\n  \t ") == "")
_long = derive_title_from_content("# " + "长标题" * 40)
check("F6 超长截断到 max_len 且带 `…`、截断后尾部标点不残留",
      len(_long) <= AT.DEFAULT_TITLE_MAX_LEN + 1 and _long.endswith("…")
      and _long.rstrip("…")[-1] not in "。，、；：", "len=%d %r" % (len(_long), _long[-14:]))
_F7 = ["# 巡飞弹参数变更影响分析报告（3 层深度）\n> 说明…",
       "本项目命名规范的核心要求是：名称分为「基本名」与「非受限名」两类——基本名须以字母或下划线开头…",
       "（Mock 回答）已收到你的消息：请生成一份关于宽带通信系统的分析报告",
       "结论先行。", "- 列表项标题", "**编制说明**：本报告仅整合 t1–t5"]
check("F7 派生结果**恒不是**占位名（6 条：真实内容 + 构造边界）",
      all(r and not is_placeholder_title(r) for r in (derive_title_from_content(c) for c in _F7)),
      str([derive_title_from_content(c) for c in _F7]))
check("F8 extract_text_from_content：dict 抽正文 / str 原样 / 取不到空",
      extract_text_from_content({"markdown": "# T"}) == "# T"
      and extract_text_from_content("# T") == "# T"
      and extract_text_from_content({}) == "" and extract_text_from_content(None) == "")

# ── F9~F12 真实落库路径（夹具库 = 真实 DDL）──────────────────────────────
_F9_CID = 19
FIX.execute("DELETE FROM artifacts WHERE conversation_id=?", (_F9_CID,))
FIX.commit()
_F9_BODY = "# 巡飞弹参数变更影响分析报告\n\n正文…" + "x" * 400      # >=200 才走 document 分支
_archive_artifacts(FIX, _F9_CID, 9001, {}, _F9_BODY, "chat", "王工")
_row = FIX.execute("SELECT title FROM artifacts WHERE conversation_id=? AND message_id=? "
                   "AND kind='document'", (_F9_CID, 9001)).fetchone()
check("F9 落库路径：cd 无 title → document 产物标题 = 内容首标题（**不是**「AI 生成文档」）",
      _row is not None and _row[0] == "巡飞弹参数变更影响分析报告",
      "title=%r（None=产物根本没建出来，说明落库路径没走通）" % (_row[0] if _row else None))
# F9b 幂等：幂等键是 (conv,msg,kind,title)，派生标题由内容决定 ⇒ 同输入必然同名 ⇒ 仍去重
_n_before = FIX.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=?", (_F9_CID,)).fetchone()[0]
_archive_artifacts(FIX, _F9_CID, 9001, {}, _F9_BODY, "chat", "王工")
_n_after = FIX.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=?", (_F9_CID,)).fetchone()[0]
check("F9b 派生标题**不破坏幂等**：同消息同内容重跑不新增产物行",
      _n_before == _n_after, "before=%d after=%d" % (_n_before, _n_after))

_archive_artifacts(FIX, _F9_CID, 9002, {"title": "模型自己给的标题"}, "# 别的标题\n" + "y" * 400, "chat", "王工")
_row2 = FIX.execute("SELECT title FROM artifacts WHERE conversation_id=? AND message_id=? "
                    "AND kind='document'", (_F9_CID, 9002)).fetchone()
check("F10 上游**给了** title 时必须优先用它（派生不得抢掉模型自己的命名）",
      _row2 is not None and _row2[0] == "模型自己给的标题",
      "title=%r" % (_row2[0] if _row2 else None))

# F11/F12 二次物化路径（content 是 json.loads(content_json) 的结果，**可能是 dict**）
# ⚠️ 这条路径是**冷路径**：全仓 grep 显示 `materialize_to_conversation` **当前无生产调用方**
#    （只有本模块自身 + `backups/` 旧副本 + 本脚本），真库 `subtask_artifacts` 也是 0 行。
#    所以本组是**防回归/防将来接线时踩坑**，不是"修了一个正在发生的线上行为" —— 如实标注。
# ⚠️ `FIX` 是裸连接（无 row_factory），而该函数内部 `d = dict(row)` 要求 Row 对象
#    （生产侧由 `database/connection.py:16` 统一设 `row_factory = sqlite3.Row`，故生产无此问题）
#    ⇒ 夹具另开一个带 row_factory 的连接。
FIX.commit()
_FIXR = sqlite3.connect(FIXTURE_PATH)
_FIXR.row_factory = sqlite3.Row
FIX.execute("DELETE FROM subtask_artifacts WHERE run_id=?", (77,))
FIX.execute("INSERT INTO subtask_artifacts (run_id, task_key, artifact_idx, kind, title, "
            "content_json, materialized) VALUES (?,?,?,?,?,?,0)",
            (77, "t1", 1, "doc", "", json.dumps({"markdown": "# 二次物化文档标题\n正文…"},
                                                ensure_ascii=False)))
FIX.commit()
_n = materialize_to_conversation(_FIXR, 77, "t1", "subtask://77/t1/a1", _F9_CID, 9003)
_row3 = FIX.execute("SELECT title, preview_type FROM artifacts WHERE conversation_id=? AND message_id=? "
                    "AND kind='document'", (_F9_CID, 9003)).fetchone()
check("F11 二次物化：d.title 为空 → 从 content 正文取名（真库该表当前为空，此处用合成行）",
      _n == 1 and _row3 is not None and _row3[0] == "二次物化文档标题",
      "n=%d title=%r" % (_n, _row3[0] if _row3 else None))
check("F12 dict 形态 content **不得**被 str() 成 JSON 残片当标题",
      _row3 is not None and not _row3[0].startswith("{"), "title=%r" % (_row3[0] if _row3 else None))

# ── M24~M27 变异自证 ──────────────────────────────────────────────────────
def _at_twin(mutate, func, label):
    """把 `core/artifact_titles` 的**真实源码**取出、注入变异、exec 成孪生体。

    ⚠️ 两个必须同时做对（本仓都踩过）：
      ① 命名空间用 `dict(vars(AT))` —— 函数体引用的 `_HEADING_RE`/`is_placeholder_title` 在里面；
         手写小 dict 会 NameError，而被调用方的 `except` 静默兜掉。
      ② **必须把整条调用链一起 exec 进同一个命名空间**（这里按依赖顺序先 `_clean_title`
         再 `derive_title_from_content`）。首版只 exec 被变异的那一个函数，
         于是 `derive_title_from_content` 仍是**原模块的函数对象**、`__globals__` 指向真模块
         ⇒ 它调的还是**未变异**的 `_clean_title` ⇒ 变异体行为不变 ⇒ M24 假绿。
         （判据：**变异必须真正改变被观察行为**；改了源码但结果一样 = 变异没生效。）
    """
    ns = dict(vars(AT))
    for f in ("_clean_title", "derive_title_from_content"):     # 依赖顺序
        src = textwrap.dedent(inspect.getsource(getattr(AT, f))).replace("\r\n", "\n")
        if f == func:
            mut = mutate(src)
            check("M%s 变异锚点命中" % label, mut != src, "func=%s" % func)
            src = mut
        exec(compile(src, "<at_twin>", "exec"), ns)
    return ns


# M24：**重演**"右端把右括号也剥掉"这一缺陷（首版左右共用含 `）` 的字符集造成）→ F3 必须翻。
# ⚠️ 两处踩过：
#   ① 锚点必须是 `.rstrip(_TAIL_TRIM)` —— 源码里它前面是 `)`（`s.lstrip(_LEAD_TRIM).rstrip(...)`），
#      写成 `s.rstrip(...)` 会**静默不命中**（锚点 find 不到 → 变异体没变 → 变异测试假绿）。
#   ② 变异不能是"换成 `_LEAD_TRIM`" —— 修好后左端集合里**本来就没有** `）`，
#      换过去结果不变 ⇒ 变异等于没做（本次实测撞到：M24 变异体仍返回带括号的正确标题）。
#      故直接给右端集合**补上** `）`，精确复现缺陷语义。
_ns24 = _at_twin(lambda s: s.replace(".rstrip(_TAIL_TRIM)", '.rstrip(_TAIL_TRIM + "）")'),
                 "_clean_title", "24")
_r24 = _ns24["derive_title_from_content"]("# 巡飞弹参数变更影响分析报告（推进剂加注量 + 3 层深度）")
check("M24 右端集合含右括号（重演首版缺陷）→ F3 目标断言失败（括号被削，被抓住）",
      (_r24 == "巡飞弹参数变更影响分析报告（推进剂加注量 + 3 层深度）") is False, repr(_r24))

# M25：拿掉「派生结果仍是兜底名就返回空」的守卫 → F4 必须翻
_ns25 = _at_twin(lambda s: s.replace("if not t or is_placeholder_title(t):", "if not t:"),
                 "derive_title_from_content", "25")
_r25 = _ns25["derive_title_from_content"]("AI 生成文档\n后面还有正文")
check("M25 去掉兜底名守卫 → F4 目标断言失败（bug 被搬到读取侧，被抓住）",
      (_r25 == "") is False, repr(_r25))

# M26：取消「标题行优先」，一律取首行 → F1 必须翻
_ns26 = _at_twin(lambda s: s.replace('cand = m.group(1) if m else ""', 'cand = ""'),
                 "derive_title_from_content", "26")
_r26 = _ns26["derive_title_from_content"]("导语第一行\n\n## 真正的标题\n正文")
check("M26 取消标题行优先 → F1 目标断言失败（取到导语，被抓住）",
      (_r26 == "真正的标题") is False, repr(_r26))

# M27：把写入侧接线撤回纯兜底名（`_archive_artifacts` 的 exec 孪生体）→ F9 必须翻
_u_src = textwrap.dedent(inspect.getsource(U._archive_artifacts)).replace("\r\n", "\n")
_u_mut = _u_src.replace("or derive_title_from_content(content)", 'or ""')
check("M27 变异锚点命中（_archive_artifacts 派生调用）",
      _u_mut != _u_src and _u_src.count("or derive_title_from_content(content)") == 2)
_ns27 = dict(vars(U))
exec(compile(_u_mut, "<arch_twin>", "exec"), _ns27)
FIX.execute("DELETE FROM artifacts WHERE conversation_id=?", (_F9_CID,))
FIX.commit()
_ns27["_archive_artifacts"](FIX, _F9_CID, 9001, {}, _F9_BODY, "chat", "王工")
_r27 = FIX.execute("SELECT title FROM artifacts WHERE conversation_id=? AND message_id=? "
                   "AND kind='document'", (_F9_CID, 9001)).fetchone()
check("M27 撤回派生（回到纯兜底名）→ F9 目标断言失败（被抓住）",
      (_r27 is not None and _r27[0] == "巡飞弹参数变更影响分析报告") is False,
      "变异后 title=%r" % (_r27[0] if _r27 else None))
FIX.execute("DELETE FROM artifacts WHERE conversation_id=?", (_F9_CID,))
FIX.execute("DELETE FROM subtask_artifacts WHERE run_id=?", (77,))
FIX.commit()

# ══════════════════════════════════════════════════════════════════════════
print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
try:
    os.remove(FIXTURE_PATH)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
