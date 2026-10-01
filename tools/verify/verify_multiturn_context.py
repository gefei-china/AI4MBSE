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
"""
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
from core.artifact_titles import (PLACEHOLDER_TITLES, is_placeholder_title,  # noqa: E402
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
    # 锚点缩进 = dedent 后的真实值（`if _digest:` 4 格 / `plan_prompt += (` 8 格；
    # 结束锚 `    if stage_hint:` 4 格）—— 写死 8/12 会静默不命中。
    _flow_src = textwrap.dedent(inspect.getsource(AgentPipeline._stream_orchestrated_flow))
    _m3_start = _flow_src.find("    if _digest:\n        plan_prompt += (")
    _m3_end = _flow_src.find("    if stage_hint:", _m3_start)
    check("M3 变异锚点命中（stream 摘要注入块）", _m3_start > 0 and _m3_end > _m3_start)
    _mut_flow = _flow_src[:_m3_start] + _flow_src[_m3_end:]
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
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
try:
    os.remove(FIXTURE_PATH)
except Exception:
    pass
sys.exit(1 if FAIL else 0)
