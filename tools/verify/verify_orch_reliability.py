# -*- coding: utf-8 -*-
"""P0-7 常驻验收：① 子任务超时判据（停滞为主 + 硬上限）② 汇总话术不再说谎
③ 摘要 count/去重口径 ④ run_id 唯一化（计划历史不再被覆盖）
⑤ 质量评审被 max_tokens 截断时不得误报「解析失败」（[J] 组 + M4 变异自证）。

跑法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orch_reliability.py

设计要点（都是踩过才写的）：
  · **整库复制 + 官方覆盖通道**：脚本启动即 `sqlite3.backup()` 复制生产库到临时目录，并设
    `MBSE_DB_PATH` 重定向（**必须在 import core.config 之前**）。于是本脚本可以把真实编排
    流程**跑到结束**（会写 agent_tasks/agent_flows/artifacts），而**生产库零写入**。
    比"逐个 patch get_db"可靠：函数体内 `from database import get_db` 的调用点 patch 不到，
    会静默跑去读生产库（P0-6 已踩，症状=断言恒空）。
  · **断言走真实代码路径**：G 组驱动真 `_stream_orchestrated_flow`，只把「子任务执行」
    （`AgentPipeline.execute_stream`）换成桩生成器 —— 超时/心跳/话术都在**真代码**里跑。
  · **变异自证只认新增失败**：M1 删事件循环里的心跳 → G1 必须翻（证明心跳是承重的）；
    M2 把失败计数写死 0 → G4 必须翻。
"""
import inspect
import os
import sqlite3
import sys
import tempfile
import textwrap
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

LIVE_DB = os.path.join(ROOT, "mbse.db")

# ── 隔离：整库复制 + 重定向（必须在 import core.config 之前）──
_TMP = tempfile.mkdtemp(prefix="wb_p07_")
COPY_DB = os.path.join(_TMP, "copy.db")
_src = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True)
_dst = sqlite3.connect(COPY_DB)
_src.backup(_dst)          # WAL 库必须用 backup()，shutil.copy2 会漏 -wal 内容
_dst.close()
_src.close()
os.environ["MBSE_DB_PATH"] = COPY_DB

import core.config as CFG                                        # noqa: E402
import llm as LLM                                                # noqa: E402
from agent.pipeline import AgentPipeline                         # noqa: E402
import agent.pipeline_parts.stream as S                          # noqa: E402
from task_queue import TaskQueue                                 # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + str(detail)) if detail else ""))
    return bool(cond)


def new_conn():
    c = sqlite3.connect(COPY_DB)
    c.row_factory = sqlite3.Row
    return c


def src_of(fn):
    return textwrap.dedent(inspect.getsource(fn))


class _StubAgentDef:
    name = "design"
    intent_name = "design"
    agent_role = "sub"
    system_prompt = ""


PLAN = '{"tasks": [{"key": "t1", "title": "在既有模型上补参与者", "agent": "design", ' \
       '"deps": [], "task_type": "agent"}]}'

CONV = 909001          # 脚本自造的会话（落在副本里）


def _seed():
    c = new_conn()
    c.execute("INSERT OR REPLACE INTO conversations (id,title,intent,current_intent,last_slots) "
              "VALUES (?,'p07','design','design','{}')", (CONV,))
    c.commit()
    c.close()


def _cfg_patch(vals):
    """临时覆盖 config.get 的若干 (section,key)（其余转发原实现）；返回还原函数。"""
    orig = CFG.get

    def fake(section, key=None, default=None):
        if (section, key) in vals:
            return vals[(section, key)]
        return orig(section, key, default)

    CFG.get = fake
    return lambda: setattr(CFG, "get", orig)


def _sub_stub(tokens=1, gap=0.4, tail_sleep=0.0):
    """桩子任务：先出 token（刷心跳），可选静默尾巴。"""
    def fake_exec(self, user_input, conversation_id, *a, **kw):
        for i in range(tokens):
            yield {"type": "token", "delta": "tok%d " % i}
            time.sleep(gap)
        if tail_sleep:
            time.sleep(tail_sleep)
        yield {"type": "done", "data": {"content": "桩交付物", "meta": {"token_count": tokens}}}
    return fake_exec


def drive(flow_fn=None, sub_stub=None, cfg_vals=None, retries=0, cid=CONV, plan=PLAN):
    """把真实编排流程驱动到结束；返回事件列表。副作用只落副本库。"""
    flow_fn = flow_fn or AgentPipeline._stream_orchestrated_flow
    events, calls = [], {"n": 0}
    orig_chat = LLM.llm_client.chat
    orig_exec = AgentPipeline.execute_stream
    restore_cfg = _cfg_patch(cfg_vals or {})

    def fake_chat(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1 and plan:
            return {"choices": [{"message": {"role": "assistant", "content": plan}}]}
        return {"choices": [{"message": {"role": "assistant", "content": "【汇总】桩汇总正文"}}]}

    LLM.llm_client.chat = fake_chat
    if sub_stub:
        AgentPipeline.execute_stream = sub_stub
    pipe = AgentPipeline()
    pipe._load_db_agents()
    pipe._ORCH_MAX_RETRIES = retries
    # 桩驱动需补齐"生产里由 execute_stream 早段设置的实例属性"（否则收尾段 AttributeError）
    pipe._last_skill_hits = []
    pipe._tool_agent_ctx = {}
    try:
        gen = flow_fn(pipe, "在既有需求模型上补充参与者信息并生成新版本", cid, "dev", "design",
                      _StubAgentDef(), "L1", [], [], {}, None, None)
        for _ in range(3000):
            try:
                events.append(next(gen))
            except StopIteration:
                break
    finally:
        LLM.llm_client.chat = orig_chat
        AgentPipeline.execute_stream = orig_exec
        restore_cfg()
    return events


def subtask_events(events):
    return [e for e in events if e.get("type") == "subtask"]


def failed_subtasks(events):
    return [e for e in subtask_events(events) if e.get("status") == "failed"]


def wordings(events):
    return "".join((e.get("delta") or "") for e in events if e.get("type") == "reasoning")


_seed()

# ══════════════════════════════════════════════════════════════════════════
print("[G] 子任务超时判据：停滞为主 + 硬上限兜底（真实流程 + 桩子任务）")
# ══════════════════════════════════════════════════════════════════════════
# G1 活跃产出（每 0.4s 一个 token，共 ~4.8s）而 idle=1s → **不得**被判超时。
#    这正是会话 514 的真实形态：t1 到 182s 仍在吐 token，却被固定 120s 杀掉。
_ev = drive(sub_stub=_sub_stub(tokens=12, gap=0.4),
            cfg_vals={("delegation", "subtask_idle_timeout_s"): 1,
                      ("delegation", "subtask_timeout_s"): 60},
            retries=0)
_st = subtask_events(_ev)
check("G1 活跃产出不误杀（4.8s 连续出 token / idle=1s → 不得超时）",
      bool(_st) and not failed_subtasks(_ev),
      "subtask=%s" % [(e.get("key"), e.get("status"), e.get("error")) for e in _st])

# G2 真停滞（1 个 token 后静默 5s）而 idle=1s → 必须判超时，且错误文案带「无产出」
_t0 = time.time()
_ev2 = drive(sub_stub=_sub_stub(tokens=1, gap=0.1, tail_sleep=5),
             cfg_vals={("delegation", "subtask_idle_timeout_s"): 1,
                       ("delegation", "subtask_timeout_s"): 60},
             retries=0)
_f2 = failed_subtasks(_ev2)
check("G2 真停滞判超时（静默 5s / idle=1s → failed 且文案带「无产出」）",
      len(_f2) == 1 and "无产出" in str(_f2[0].get("error") or ""),
      "err=%s elapsed=%.1fs" % ([e.get("error") for e in _f2], time.time() - _t0))
check("G2b 停滞超时是**快速**失败（不等挂到墙钟上限）",
      (time.time() - _t0) < 20, "%.1fs" % (time.time() - _t0))

# G3 硬上限兜底：一直活跃（每 0.3s 一 token）但硬上限 2s → 必须被硬上限杀掉
_ev3 = drive(sub_stub=_sub_stub(tokens=30, gap=0.3),
             cfg_vals={("delegation", "subtask_idle_timeout_s"): 60,
                       ("delegation", "subtask_timeout_s"): 2},
             retries=0)
_f3 = failed_subtasks(_ev3)
check("G3 硬上限兜底（持续活跃但超 2s 上限 → failed 且文案带「硬上限」）",
      len(_f3) == 1 and "硬上限" in str(_f3[0].get("error") or ""),
      "err=%s" % [e.get("error") for e in _f3])

# ══════════════════════════════════════════════════════════════════════════
print()
print('[G4] 汇总话术：不得在任务失败时说「N 个执行完成」')
# ══════════════════════════════════════════════════════════════════════════
_w2 = wordings(_ev2)
check("G4 失败时话术带未成功计数", ("未成功" in _w2) and ("1 个子任务执行完成" in _w2),
      "话术=%s" % _w2[-90:])
_w1 = wordings(_ev)
check("G4b 全部成功时话术不带未成功字样（防恒真）", "未成功" not in _w1, "话术=%s" % _w1[-70:])

# ══════════════════════════════════════════════════════════════════════════
print()
print("[H] run_id 唯一化：计划历史不再被覆盖 + 归属会话 + 保留策略")
# ══════════════════════════════════════════════════════════════════════════
_c = new_conn()
_r1 = TaskQueue.new_run_id(_c)
check("H1 new_run_id 返回 > 现有最大批次号", _r1 > _c.execute(
    "SELECT COALESCE(MAX(run_id),0) FROM agent_tasks").fetchone()[0] - 1, "run_id=%d" % _r1)

_plan1 = [{"key": "t1", "title": "批次A", "agent": "design", "deps": [], "seq": 0}]
_plan2 = [{"key": "t1", "title": "批次B", "agent": "design", "deps": [], "seq": 0}]
_a = TaskQueue.new_run_id(_c)
TaskQueue.create_plan(_c, _a, _plan1, assigned_by="session", conversation_id=CONV)
_b = TaskQueue.new_run_id(_c)
TaskQueue.create_plan(_c, _b, _plan2, assigned_by="session", conversation_id=CONV)
check("H2 同一会话两次编排的批次号不同", _a != _b, "a=%d b=%d" % (_a, _b))
_rn = [_rr[0] for _rr in _c.execute(
    "SELECT DISTINCT run_id FROM agent_tasks WHERE conversation_id=?", (CONV,)).fetchall()]
check("H3 两批计划**都在**（旧批未被 clear_run 覆盖）", _a in _rn and _b in _rn, "runs=%s" % sorted(_rn))
check("H3b 批次归属会话已落库", _c.execute(
    "SELECT COUNT(*) FROM agent_tasks WHERE conversation_id=? AND run_id=?",
    (CONV, _a)).fetchone()[0] == 1)
check("H3c 标题各自正确（未被后一批改写）", sorted(
    _rr[0] for _rr in _c.execute(
        "SELECT title FROM agent_tasks WHERE conversation_id=? AND run_id IN (?,?)",
        (CONV, _a, _b)).fetchall()) == ["批次A", "批次B"])

# 保留策略：造 4 批（3 批终态 + 1 批含 ready）→ keep=2 只应清掉**终态**里的最旧批
_ids = []
for _i in range(4):
    _rid = TaskQueue.new_run_id(_c)
    TaskQueue.create_plan(_c, _rid, [{"key": "t1", "title": "P%d" % _i, "agent": "design",
                                      "deps": [], "seq": 0}], conversation_id=CONV)
    _ids.append(_rid)
for _rid in _ids[:3]:                       # 前三批置终态
    _c.execute("UPDATE agent_tasks SET status='done' WHERE run_id=?", (_rid,))
_c.commit()
_kept_ready = _ids[3]                       # 最后一批保持 ready（活跃）
_n = TaskQueue.prune_runs(_c, CONV, keep=2)
_left = {_rr[0] for _rr in _c.execute(
    "SELECT DISTINCT run_id FROM agent_tasks WHERE conversation_id=?", (CONV,)).fetchall()}
check("H4 prune 保留最近 2 批（含活跃批），清掉更早的终态批", _kept_ready in _left and _left.issubset(
    set(_ids[1:]) | {_kept_ready, _a, _b}), "prune=%d left=%s ids=%s" % (_n, sorted(_left), _ids))
check("H4b 活跃批（ready）绝不被清", _kept_ready in _left)
_c.close()

# 源码级：流式路径不再复用会话 id + 不再 clear_run
_s_src = open("agent/pipeline_parts/stream.py", encoding="utf-8").read()
check("H5 流式路径改用 new_run_id 落计划", "TaskQueue.new_run_id(conn)" in _s_src
      and "TaskQueue.create_plan(conn, run_id, plan" in _s_src)
check("H5b 流式路径不再 clear_run（那正是覆盖历史的原因）",
      "TaskQueue.clear_run(conn, run_id)" not in _s_src)
_o_src = open("agent/pipeline_parts/orchestration.py", encoding="utf-8").read()
check("H5c 读回计划改用本轮批次号（非 conversation_id）",
      'orch.get("run_id")' in _o_src and "WHERE run_id=? ORDER BY seq\", (_run_id,)" in _o_src)
_p_src = open("workflows/planner.py", encoding="utf-8").read()
check("H5d 非流式路径同样分配独立批次号并回传",
      "TaskQueue.new_run_id(conn)" in _p_src and '"run_id": run_id' in _p_src)
_t_src = open("task_queue.py", encoding="utf-8").read()
check("H5e create_plan 落 conversation_id + prune_runs 存在",
      "conversation_id: int = 0" in _t_src and "def prune_runs" in _t_src and "def new_run_id" in _t_src)
_g_src = open("core/config.py", encoding="utf-8").read()
check("H5f 三个新阈值已进 config（现场可调，非硬编码）",
      all(k in _g_src for k in ("subtask_idle_timeout_s", "subtask_timeout_s",
                                "keep_runs_per_conversation")))
_mig = open("database/migrations/columns.py", encoding="utf-8").read()
check("H5g agent_tasks.conversation_id 有迁移", 'agent_tasks", "conversation_id' in _mig)
_wt = CFG.get("delegation", "subtask_idle_timeout_s")
_ht = CFG.get("delegation", "subtask_timeout_s")
check("H5h 阈值从 config 真实读到（非 None）", bool(_wt) and bool(_ht) and int(_ht) > int(_wt),
      "idle=%s hard=%s" % (_wt, _ht))

# ══════════════════════════════════════════════════════════════════════════
print()
print("[I] 产物摘要：count（行数）与去重名称口径对齐")
# ══════════════════════════════════════════════════════════════════════════
from agent.session_artifacts import build_digest                     # noqa: E402
_c = new_conn()
for _t in ("SysML 视图：需求图", "SysML 视图：需求图", "SysML 视图：活动图", "SysML 视图：追溯视图"):
    _c.execute("INSERT INTO artifacts (conversation_id, message_id, kind, title) VALUES (?,0,'sysml',?)",
               (CONV + 1, _t))
_c.execute("INSERT OR REPLACE INTO conversations (id,title,intent,current_intent,last_slots) "
           "VALUES (?,'p07b','design','design','{}')", (CONV + 1,))
_c.commit()
_d = build_digest(_c, CONV + 1, max_chars=4000)
check("I1 4 条 / 3 个不重名 → 明确标注口径", "4 条中不重名 3 种" in _d, _d.split("\n")[-1][:120])
check("I2 去重后的名称都在", all(_k in _d for _k in ("需求图", "活动图", "追溯视图")))
_c.close()

# ══════════════════════════════════════════════════════════════════════════
print()
print("[J] 质量评审：输出被 max_tokens 截断时不得误报「解析失败」")
# ══════════════════════════════════════════════════════════════════════════
# 背景（会话 514 实跑）：评审 `max_tokens=1024` 被思考模型的 reasoning 吃光 →
# 正文 JSON 被 `finish_reason="length"` 从中间截断、闭合 `}` 丢失 →
# 原实现走硬编码兜底判 0 分「解析失败」→ 把"模型其实给了分"报成解析失败，
# 并把 orchestrated_status 误降级为 partial。本组即该缺陷的常驻回归。
from workflows.nodes import FlowNodesMixin                            # noqa: E402

_PARSE = FlowNodesMixin._parse_eval_json
_FULL_JSON = '{"score": 62, "passed": false, "issues": ["内容重复"], "advice": "补细节"}'
_TRUNC_JSON = '{"score": 10, "passed": false, "issues": ["六个章节内容完全重复，未分别提供'

_J1 = _PARSE(_FULL_JSON)
check("J1 完整 JSON → 正常解析出 score/passed",
      _J1.get("score") == 62 and _J1.get("passed") is False, "J1=%s" % _J1.get("score"))

_J2 = _PARSE(_TRUNC_JSON)
check("J2 **截断 JSON**（有 { 无 }）→ 宽松抽取拿到真实分数（不再判 0 分解析失败）",
      _J2.get("score") == 10, "J2=%s" % _J2.get("score"))
check("J2b 截断这件事**不静默**（issues 里如实标注）",
      any("截断" in str(_i) for _i in (_J2.get("issues") or [])), "issues=%s" % _J2.get("issues"))
_J3 = _PARSE('{"score": 88, "passed": true, "issues": [')
check("J3 截断也能取到 passed=true", _J3.get("score") == 88 and _J3.get("passed") is True)
check("J4 抽不到关键字段 → 返回 {}（不臆造分数）",
      _PARSE('{"foo": 1') == {} and _PARSE("") == {} and _PARSE("没有大括号") == {})


def _eval_with(resp_body, content="一段待评审的编排汇总正文，用于驱动公共评审函数。"):
    """用桩 chat 驱动**真** `_evaluate_content`（解析/兜底/文案全走真代码）。"""
    orig = LLM.llm_client.chat
    LLM.llm_client.chat = lambda messages, **k: resp_body
    try:
        return FlowNodesMixin._evaluate_content(content)
    finally:
        LLM.llm_client.chat = orig


_E_TRUNC = _eval_with({"choices": [{"message": {"content": _TRUNC_JSON}, "finish_reason": "length"}],
                       "_meta": {"provider": "stub"}})


def _j5_ok(res):
    """J5 的目标判据 —— **独立成函数**，好让 M4 变异直接复用同一条（别另写一条近似判据）。"""
    return (res.get("score") == 10
            and not any("解析失败" in str(_i) for _i in (res.get("issues") or [])))


check("J5 端到端（桩 llm + 真评审函数）：截断回包 → score=真实分、不再报「解析失败」",
      _j5_ok(_E_TRUNC),
      "score=%s issues=%s" % (_E_TRUNC.get("score"), _E_TRUNC.get("issues")))
check("J5b _meta 带上 finish_reason（现场可判'被截断'还是'模型没吐 JSON'）",
      (_E_TRUNC.get("_meta") or {}).get("finish_reason") == "length")
_E_EMPTY = _eval_with({"choices": [{"message": {"content": ""}, "finish_reason": "length"}], "_meta": {}})
check("J6 端到端：content 全空 → 成因如实标注（不与其他成因混成一个'解析失败'）",
      _E_EMPTY.get("score") == 0 and any("reasoning" in str(_i) for _i in _E_EMPTY.get("issues") or []),
      "issues=%s" % _E_EMPTY.get("issues"))
_E_FULL = _eval_with({"choices": [{"message": {"content": _FULL_JSON}, "finish_reason": "stop"}], "_meta": {}})
check("J7 端到端：完整回包行为不变（score/passed/issues/advice 全出）",
      _E_FULL.get("score") == 62 and _E_FULL.get("advice") == "补细节")
check("J8 评审调用已不再硬编码 1024（改走 config）",
      "max_tokens=1024" not in src_of(FlowNodesMixin._evaluate_content)
      and "_eval_max_tokens()" in src_of(FlowNodesMixin._evaluate_content))
_cfg_src = open("core/config.py", encoding="utf-8").read()
check("J9 eval_max_tokens 已进 config 两处（DEFAULT_CONFIG + CONFIG_SCHEMA）",
      _cfg_src.count('"eval_max_tokens"') == 2)
_em = CFG.get("refine", "eval_max_tokens")
check("J10 config 真读到且显著大于旧硬编码 1024", bool(_em) and int(_em) > 1024, "eval_max_tokens=%s" % _em)

# ══════════════════════════════════════════════════════════════════════════
print()
print("[M] 变异自证：每个变异必须制造**新增失败**")
# ══════════════════════════════════════════════════════════════════════════
_M_BASE = set(FAIL)
_flow_src = src_of(AgentPipeline._stream_orchestrated_flow)


def _drop_line(src, needle):
    """删掉**包含 needle 的那一整行**（缩进无关）。返回 (new_src, 命中行数)。

    必须缩进无关：`inspect.getsource` 对**类方法**返回带 4 格类缩进的文本，`textwrap.dedent`
    后整体左移 4 格 —— 写死缩进的锚点会**静默不命中**（P0-6 已踩过一次）。
    """
    out, hits = [], 0
    for ln in src.split("\n"):
        if needle in ln:
            hits += 1
            continue
        out.append(ln)
    return "\n".join(out), hits


def _replace_line(src, needle, new_body):
    """把含 needle 的行**保持原缩进**替换为 new_body（缩进无关）。返回 (new_src, 命中行数)。"""
    out, hits = [], 0
    for ln in src.split("\n"):
        if needle in ln:
            hits += 1
            out.append(ln[:len(ln) - len(ln.lstrip())] + new_body)
            continue
        out.append(ln)
    return "\n".join(out), hits


def _ns_for(src):
    ns = dict(vars(S))
    exec(compile(src, "<flow_twin>", "exec"), ns)
    return ns


try:
    # M1 删掉事件循环里的心跳刷新（只留入口那次）→ G1 场景必须被误杀
    _m1_src, _h1 = _drop_line(_flow_src, "_hb[tkey] = _t.time()   # P0-7：任何产出")
    check("M1 变异锚点命中（事件循环心跳行）", _h1 == 1, "hits=%d" % _h1)
    _ns1 = _ns_for(_m1_src)
    _ev_m1 = drive(flow_fn=_ns1["_stream_orchestrated_flow"], sub_stub=_sub_stub(tokens=12, gap=0.4),
                   cfg_vals={("delegation", "subtask_idle_timeout_s"): 1,
                             ("delegation", "subtask_timeout_s"): 60}, retries=0)
    check("M1 心跳失效 → 活跃任务被判超时（G1 目标断言被抓住）",
          len(failed_subtasks(_ev_m1)) == 1,
          "failed=%s" % [e.get("error") for e in failed_subtasks(_ev_m1)])

    # M2 失败计数写死 0 → G4 必须翻（话说"全部完成"）
    _m2_src, _h2 = _replace_line(_flow_src, '_n_bad = sum(1 for _ot in orch_tasks', "_n_bad = 0")
    check("M2 变异锚点命中（失败计数行）", _h2 >= 1, "hits=%d" % _h2)
    _ns2 = _ns_for(_m2_src)
    _ev_m2 = drive(flow_fn=_ns2["_stream_orchestrated_flow"],
                   sub_stub=_sub_stub(tokens=1, gap=0.1, tail_sleep=5),
                   cfg_vals={("delegation", "subtask_idle_timeout_s"): 1,
                             ("delegation", "subtask_timeout_s"): 60}, retries=0)
    check("M2 计数写死 0 → G4 目标断言失败（被抓住）", "未成功" not in wordings(_ev_m2))

    # M3 摘要去重口径退回旧写法 → I1 必须翻
    _sa_src = open("agent/session_artifacts.py", encoding="utf-8").read()
    _new_fmt = '        if uniq and len(uniq) != cnt:\n'
    check("M3 变异锚点命中（摘要口径分支）", _new_fmt in _sa_src)
    _ns3 = {}
    _sa_old = _sa_src.replace(
        '        if uniq and len(uniq) != cnt:\n            tail = f"（{cnt} 条中不重名 {len(uniq)} 种：{\'、\'.join(uniq)}）"\n        else:\n            tail = (f"（{\'、\'.join(uniq)}）" if uniq else "")\n',
        '        tail = (f"（{\'、\'.join(uniq)}）" if uniq else "")\n')
    check("M3b 变异已生效（文本确被替换）", _sa_old != _sa_src)
    exec(compile(_sa_old, "<sa_twin>", "exec"), _ns3)
    _c = new_conn()
    _d3 = _ns3["build_digest"](_c, CONV + 1, max_chars=4000)
    _c.close()
    check("M3 退回旧写法 → I1 目标断言失败（4 项只列 3 个名字）",
          "4 条中不重名 3 种" not in _d3 and "需求图" in _d3)

    # M4 评审解析退回"只认完整 JSON"（= 本次修的那个缺陷）→ J2/J5 必须翻。
    #   注意：`_PARSE` 是 J 组绑定的函数对象，patch 类属性不会影响它；
    #   `_evaluate_content` 内部是**动态类属性查找**，故两者分别取，才能都验到。
    _pn_src = src_of(FlowNodesMixin._parse_eval_json)
    _m4_src, _h4 = _replace_line(_pn_src, "if ms:", "if False:")
    check("M4 变异锚点命中（宽松抽取分支）", _h4 == 1, "hits=%d" % _h4)
    _ns4 = dict(FlowNodesMixin._parse_eval_json.__globals__)   # 用真模块全局，别手写小 dict（会被自身 except 静默兜空）
    exec(compile(_m4_src, "<eval_twin>", "exec"), _ns4)
    _old_fn = _ns4["_parse_eval_json"]
    if isinstance(_old_fn, staticmethod):
        _old_fn = _old_fn.__func__
    _orig_pn = FlowNodesMixin._parse_eval_json
    FlowNodesMixin._parse_eval_json = staticmethod(_old_fn)
    try:
        _j2m = FlowNodesMixin._parse_eval_json(_TRUNC_JSON)
        _e5m = _eval_with({"choices": [{"message": {"content": _TRUNC_JSON},
                                       "finish_reason": "length"}], "_meta": {}})
    finally:
        FlowNodesMixin._parse_eval_json = _orig_pn
    check("M4 退回「只认完整 JSON」→ J2 目标断言失败（截断回包拿不到分）",
          _j2m.get("score") != 10, "变异后 score=%s" % _j2m.get("score"))
    check("M4b 同上 → J5 目标断言**同一条判据**由真变假（端到端真实分被丢弃）",
          not _j5_ok(_e5m), "变异后 score=%s issues=%s" % (_e5m.get("score"), _e5m.get("issues")))
except Exception as _e:      # noqa: BLE001 —— 锚点漂移/路径异常一律记 FAIL，别裸崩丢结果
    check("M 组执行未抛异常（锚点与路径稳定）", False, "%s: %s" % (type(_e).__name__, str(_e)[:160]))

_NEW_FAIL = [f for f in FAIL if f not in _M_BASE]
print()
print("=" * 72)
print("PASS=%d  FAIL=%d" % (len(PASS), len(FAIL)))
if _NEW_FAIL:
    print("M 组制造的新增失败（预期，证明变异被抓住）：%d 条" % len(_NEW_FAIL))
if FAIL:
    print("未通过：")
    for f in FAIL:
        print("   -", f)
print("=" * 72)
sys.exit(1 if FAIL else 0)
