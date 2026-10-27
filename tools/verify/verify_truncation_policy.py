# -*- coding: utf-8 -*-
"""verify_truncation_policy — 门禁：内容截断的**口径正确性 + 前后端一致**。

## 为什么需要这道门禁（2026-10-27 截断清单）

实测推翻了「截断 = 预算不够」的直觉判断：

| 指标 | 实测 |
|---|---|
| 模型窗口 | 128,000 token |
| 历史区预算（ratio 0.25） | 32,000 token |
| 实际拼进 prompt 的历史 | **5,922 token（18.5%）** |
| 超 1500 字被裁的消息 | **99 / 456 条（21.7%）**，累计丢 **468,727 字符** |
| 其中用户消息 | 2 条：msg 4761 贴 V2 代码 **12,476 → 1,500（丢 88%）** |

⇒ 真正卡住内容的是**两个"小而硬"的限**（`topic_current_max_msgs=8`、
`history_msg_max_chars=1500`），它们把预算机制**架空了**：先砍到 1500，再谈 32k 预算。

## 本门禁锁七件事

① **默认值口径**：`history_msg_max_chars=0`（不限）、窗口/配额/摘要类按标定值；
② **常量口径**：工具结果「回填模型 / 展示 / 落库」三处同值（8000），角色块 8000（两路径同）；
③ **无静默硬切**：用户输入不得再有固定字符切片；工具日志必须有长度/是否截断列；
④ **两路径一致**：execute 与 stream 的 `role_cap` 取同一个常量（曾 0 vs 1200 分裂）；
⑤ **设置页 ↔ 后端 schema 一致**：界面上的每个键都能在后端 `CONFIG_SCHEMA` 找到，
   且界面文案里写的「默认 N」必须等于后端默认值（否则调完默认、界面还在说旧值）；
⑥ **真实数据自证**：真库最长的那条用户消息（12,476 字）在 `_load_history` 里**完整进入**；
⑦ **变异自证**：把任一约束改回旧值，本门禁必须变红。

复现：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_truncation_policy.py`
"""
import io
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK  ] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))
    return bool(cond)


def _read(rel):
    p = os.path.join(ROOT, rel)
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return ""


def DEFAULTS_DEFAULT(sec, name):
    """后端**默认值**（DEFAULT_CONFIG）—— CONFIG_SCHEMA 只存 type/desc，不存值。"""
    from core import config as _c
    return (_c.DEFAULT_CONFIG.get(sec) or {})[name]


# ────────────────────────────────────────────────────────────────────────────
# 纯判据函数（供变异自证复用：输入是"源码文本"，不是磁盘现状）
# ────────────────────────────────────────────────────────────────────────────
def prompt_silent_cut_violations(src: str) -> list:
    """用户输入被固定字符数静默切片的证据。空列表 = 合规。"""
    bad = []
    # `str(user_input)[:2000]` / `str(user_input)[:N]`（任何固定切片都算）
    if re.search(r"\{\{user_input\}\}\"\s*,\s*str\(user_input\)\[\s*:", src):
        bad.append("prompt.py: {{user_input}} 仍按固定字符数切片")
    # 兜底：任何 `str(user_input)[:数字]` 出现在 replace 里
    if re.search(r"replace\(\s*\"\{\{user_input\}\}\"[\s\S]{0,80}str\(user_input\)\[\s*:\s*\d+", src):
        bad.append("prompt.py: user_input 仍被固定切片")
    return bad


def tool_log_violations(src: str) -> list:
    """工具日志落库必须可观测（长度 + 是否截断）。

    ⚠️ 必须只看 **INSERT 的列清单**，不能全文搜 `result_len` —— 注释里也会提到它
    （本仓铁律：断言"某写法存在/不存在"前先剥注释，否则被自己的说明文字命中）。
    """
    bad = []
    m = re.search(r"INSERT INTO tool_call_logs\s*\(([^)]*)\)", src, re.S)
    cols = m.group(1) if m else ""
    if "result_len" not in cols or "result_truncated" not in cols:
        bad.append("tools.py: tool_call_logs INSERT 未包含 result_len/result_truncated 列")
    if re.search(r"str\(result\.get\(\"result\", \"\"\)\)\[\s*:\s*2000\s*\]", src):
        bad.append("tools.py: 仍把结果静默截到 2000")
    return bad


def ui_schema_mismatches(js_src: str, schema: dict) -> list:
    """设置页字段清单 ↔ 后端 CONFIG_SCHEMA 的一致性。

    两类问题都致命：
      · 界面有、后端无 ⇒ 保存时后端不认识该键（用户改了但没生效，或直接报错）
      · 文案写「默认 N」但 N ≠ 后端默认 ⇒ 用户按错值调参
    """
    bad = []
    keys = re.findall(r"key:'([a-z_]+\.[a-z_0-9]+)'", js_src)
    if not keys:
        return ["设置页未解析到任何 key（解析规则或文件结构变了）"]
    for k in keys:
        sec, name = k.split(".", 1)
        if name not in (schema.get(sec) or {}):
            bad.append("界面有键、后端无: %s" % k)
            continue
        # 抽取该项的字段块（key 到下一个 key 之间）
        m = re.search(r"key:'%s'[\s\S]{0,1200}?(?=key:'|\Z)" % re.escape(k), js_src)
        if not m:
            continue
        block = m.group(0)
        # 抓「默认 N」里的数字；**带单位的（4MB / 2万 / 3×1024）跳过严格比对** ——
        # 那是人读近似值，不是可逐位比对的配置值（如 vision.max_bytes=4194304 写成"4MB"）。
        claims = []
        for mm in re.finditer(r"默认\s*(?:为)?\s*([0-9]+(?:\.[0-9]+)?)", block):
            nxt = block[mm.end():mm.end() + 2]
            if re.match(r"(MB|KB|GB|万|千|×|\*)", nxt):
                continue
            claims.append(mm.group(1))
        if not claims:
            continue
        # ⚠️ 默认值不在 CONFIG_SCHEMA 里（那里只有 type/desc），在 DEFAULT_CONFIG。
        try:
            real = float(DEFAULTS_DEFAULT(sec, name))
        except Exception:
            continue
        for c in claims:
            if abs(float(c) - real) > 1e-9:
                bad.append("%s 文案写「默认 %s」与后端默认 %s 不一致" % (k, c, real))
    return bad


# ─── ① 默认值口径 ────────────────────────────────────────────────────────────
def main():
    from core import config as cfg

    print("=== ① 默认值口径（真实 config 解析结果）===")
    want = {
        "history_msg_max_chars": 0,          # 不限：交由预算与份额
        "topic_current_max_msgs": 20,        # 8 → 20（会话级事实的另一道保险）
        "topic_retrieve_per_role_cap": 5,    # 3 → 5
        "model_context_chars": 1500,         # 800 → 1500
        "artifact_digest_max_chars": 1500,   # 900 → 1500
        "budget_history_ratio": 0.35,        # 0.25 → 0.35（实测两条长用户输入才能同时进）
    }
    for k, v in want.items():
        cur = cfg.get("context", k)
        ok("%s == %s" % (k, v), cur is not None and abs(float(cur) - float(v)) < 1e-9,
           "实际=%r" % (cur,))

    # ─── ② 常量口径 ─────────────────────────────────────────────────────────
    print("\n=== ② 常量口径（三处同值 + 两路径同值）===")
    from agent.pipeline_parts import common as C
    from agent import pipeline as PL
    from agent.pipeline_parts import tool_offload as TO
    import inspect

    ok("_TOOL_MODEL_CAP（回填模型）== 8000", C._TOOL_MODEL_CAP == 8000, str(C._TOOL_MODEL_CAP))
    ok("_TOOL_RESULT_CAP（展示/落库）== 8000", C._TOOL_RESULT_CAP == 8000, str(C._TOOL_RESULT_CAP))
    ok("pipeline._TOOL_RESULT_CAP 与 common 同值", PL._TOOL_RESULT_CAP == C._TOOL_RESULT_CAP,
       "%s vs %s" % (PL._TOOL_RESULT_CAP, C._TOOL_RESULT_CAP))
    ok("_ATT_INJECT_CAP（附件正文）== 8000", C._ATT_INJECT_CAP == 8000, str(C._ATT_INJECT_CAP))
    ok("_AGENT_ROLE_CAP ≥ 真库最大 prompt（7981）", C._AGENT_ROLE_CAP >= 7981,
       str(C._AGENT_ROLE_CAP))
    ok("_ATT_SECTION_MODEL_CAP 保持 20000（按用途给足，正确示范）",
       C._ATT_SECTION_MODEL_CAP == 20000, str(C._ATT_SECTION_MODEL_CAP))
    _cap_default = inspect.signature(TO.model_side_content).parameters["cap"].default
    ok("model_side_content 默认 cap == _TOOL_MODEL_CAP", _cap_default == C._TOOL_MODEL_CAP,
       str(_cap_default))

    # ─── ③ 无静默硬切 ──────────────────────────────────────────────────────
    print("\n=== ③ 无静默硬切（源码画像）===")
    _prompt = _read("agent/pipeline_parts/prompt.py")
    _tools = _read("agent/pipeline_parts/tools.py")
    _hist = _read("agent/pipeline_parts/history.py")
    _intent = _read("agent/intent.py")

    v = prompt_silent_cut_violations(_prompt)
    ok("用户输入不再被固定字符数切片", not v, "；".join(v))
    v = tool_log_violations(_tools)
    ok("工具日志落库带 result_len / result_truncated", not v, "；".join(v))
    ok("历史裁剪必带标记（HIST_CUT_MARK）", "HIST_CUT_MARK" in _hist
       and "_mark_budget_cut" in _hist)
    ok("历史注入按角色分级（_user_share + abs_cap）",
       "_user_share" in _hist and "abs_cap" in _hist)
    ok("意图复核输入已放大到 1000", "str(text)[:1000]" in _intent
       and "str(text)[:500]" not in _intent)

    # ─── ④ 两路径 role_cap 一致 ─────────────────────────────────────────────
    print("\n=== ④ execute / stream 的 role_cap 同源 ===")
    ok("execute 传 role_cap=_AGENT_ROLE_CAP",
       "role_cap=_AGENT_ROLE_CAP" in _read("agent/pipeline_parts/execute.py"))
    ok("stream 传 role_cap=_AGENT_ROLE_CAP",
       "role_cap=_AGENT_ROLE_CAP" in _read("agent/pipeline_parts/stream.py"))

    # ─── ⑤ 设置页 ↔ 后端 schema ─────────────────────────────────────────────
    print("\n=== ⑤ 设置页 ↔ 后端 CONFIG_SCHEMA 一致 ===")
    _js = _read("static/js/mods/35-ctxconfig.js")
    schema = cfg.CONFIG_SCHEMA if hasattr(cfg, "CONFIG_SCHEMA") else {}
    if not schema:
        try:
            from core.config import CONFIG_SCHEMA as schema  # noqa: F401
        except Exception:
            schema = {}
    bad = ui_schema_mismatches(_js, schema)
    ok("界面每个键都能在后端找到、且「默认 N」与后端一致", not bad,
       "；".join(bad[:6]) + (" …共%d处" % len(bad) if len(bad) > 6 else ""))

    touched = ["context.history_msg_max_chars", "context.topic_current_max_msgs",
               "context.model_context_chars", "context.topic_retrieve_per_role_cap",
               "context.budget_history_ratio"]
    miss = [k for k in touched if ("key:'%s'" % k) not in _js]
    ok("本轮调整的 5 个阈值在设置页均可见", not miss, "缺失=%s" % miss)

    # 已取消/已移除的配置项不得残留（否则用户改了不生效）
    removed = ["budget_system_chars", "budget_system_tokens"]
    residue = [k for k in removed if k in _js]
    ok("已移除的配置项不再出现在界面", not residue, "残留=%s" % residue)

    # ─── ⑥ 真实数据自证 ────────────────────────────────────────────────────
    print("\n=== ⑥ 真实数据自证（真库，不 mock）===")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT id, conversation_id, role, length(content) L FROM messages "
            "WHERE role='user' ORDER BY L DESC LIMIT 1").fetchone()
        c.close()
    except Exception as e:
        row = None
        print("  （读库失败：%s）" % e)

    if row:
        mid, conv, L = row["id"], row["conversation_id"], row["L"]
        ok("真库最长用户消息 > 1500 字（具备验证价值）", L > 1500, "msg=%s %d 字" % (mid, L))
        from agent.pipeline import AgentPipeline
        p = AgentPipeline()
        hist = p._load_history(conv, cur_input="继续校验这段代码")
        got = 0
        for m in hist:
            if m.get("role") == "user" and abs(len(m.get("content") or "") - L) < 4:
                got = len(m.get("content") or "")
                break
        if not got:
            # 内容可能带"因预算省略"标记 ⇒ 用前缀长度判定
            got = max([len(m.get("content") or "") for m in hist
                       if m.get("role") == "user"] or [0])
        ok("该消息在 _load_history 中被完整注入（不再被固定 1500 砍）", got >= L - 4,
           "注入 %d / 原文 %d" % (got, L))

        # 落库列存在（老库已迁移）
        cc = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        cols = [r[1] for r in cc.execute("PRAGMA table_info(tool_call_logs)").fetchall()]
        cc.close()
        ok("tool_call_logs.result_len / result_truncated 已建列",
           "result_len" in cols and "result_truncated" in cols, "cols=%d" % len(cols))

    # ─── ⑦ 变异自证 ────────────────────────────────────────────────────────
    print("\n=== ⑦ 变异自证（把约束改回旧值 ⇒ 判据必须报红）===")
    mut1 = _prompt.replace('content.replace("{{user_input}}", str(user_input))',
                           'content.replace("{{user_input}}", str(user_input)[:2000])')
    ok("变异①（恢复 user_input[:2000]）被判据识别",
       mut1 != _prompt and prompt_silent_cut_violations(mut1))

    mut2 = _tools.replace("_LOG_RES_CAP = 8000", "_LOG_RES_CAP = 2000")
    mut2 = mut2.replace("result_len, result_truncated", "", 1)
    ok("变异②（日志截断改回 2000 且去掉长度列）被判据识别", tool_log_violations(mut2))

    mut3 = _js.replace("key:'context.history_msg_max_chars'",
                       "key:'context.history_msg_max_chars_renamed'")
    ok("变异③（界面键与后端脱钩）被判据识别", ui_schema_mismatches(mut3, schema))

    mut4 = _js.replace("默认 1500</b>", "默认 800</b>")
    ok("变异④（文案默认值与后端不一致）被判据识别",
       (mut4 != _js) and ui_schema_mismatches(mut4, schema))

    # ─── 汇总 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    if FAIL:
        print("❌ 存在不一致：截断口径或前后端设置未同步")
        return 1
    print("✅ 截断口径自洽，设置页与后端一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
