"""S2 门禁：远端参数错误必须变成「模型能照做的指令」，且学到要用上（2026-10-29）。

## 要防的三件事

1. **提示写了但模型不采纳**：实测（@2026-10-29，近 7 天真实库）参数类失败 46 条，
   `field=value / KnownField` 占 18 条，且 **8/12 个会话在重复犯**
   （conv 9629 犯 11 次、conv 9736 犯 9 次）。
   → 说明"把服务端原报文追加在结果末尾"无效。本门禁钉住**前置**这条实现事实。
2. **本地 schema 为空却假装能校验**：实测 `mcp_servers.tool_meta` 的
   `required=None / properties=0` ⇒ 本地**根本没有判据**。所以 S2 不靠本地 schema，
   而靠远端 `fields[].expected`——门禁必须验证这一点没被"改成用本地 schema 硬编"。
3. **学到了却不注入**：hint 落库了但工具描述没带上 ⇒ 等于没学。
   门禁要验证 `learn()` → `describe_suffix()` 的闭环。

## 污染

用独立临时库测落库/取回；只读操作打生产库（不复用其数据写）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# 真实远端报文（取自 tool_call_logs id=5935，conv 9741）—— 不自己编造样本
REAL = ('{"code":"INVALID_ARGUMENTS","message":"参数不符合工具定义；按 fields 提示修正字段后重新调用",'
        '"retryable":false,"traceId":"x","fields":[{"expected":"keyword, pageNum, pageSize, '
        'parentId, type, version","message":"此字段未在工具中声明，请删除或改用 expected 中的字段",'
        '"constraint":"KnownField","field":"value"}],'
        '"recovery":{"action":"CORRECT_ARGUMENTS","instructions":"按 fields 中的字段路径、约束和预期值修正参数后重新调用；业务 ID 使用 JSON 字符串，不要转换为浮点数。"}}')

from agent.tool_arg_hint import (                                    # noqa: E402
    parse_arg_error, format_hint_zh, lead_with_hint, learn, recall, describe_suffix,
)

print("== A 解析真实报文：字段名 / 约束 / 可用清单都要提取到 ==")
h = parse_arg_error(REAL)
check("A1 能解析出结构", isinstance(h, dict), type(h).__name__)
if isinstance(h, dict):
    check("A2 code 正确", h.get("code") == "INVALID_ARGUMENTS", h.get("code"))
    check("A3 定位到错字段 value", [f["field"] for f in h["fields"]] == ["value"],
          [f["field"] for f in h["fields"]])
    check("A4 约束为 KnownField", h["fields"][0]["constraint"] == "KnownField")
    check("A5 可用字段清单完整",
          h["allowed"] == ["keyword", "pageNum", "pageSize", "parentId", "type", "version"],
          h["allowed"])

print()
print("== B 中文指令：必须是祈使句且点名「必删」与「只能用」==")
t = format_hint_zh("query_data_tree", h) if h else ""
check("B1 生成了文本", bool(t), len(t))
check("B2 点名必删字段 value", "`value`" in t)
check("B3 列出可用字段", all(k in t for k in ("keyword", "pageNum", "pageSize")))
check("B4 是祈使句（含「请…重试」类动作要求）", "重试" in t)
check("B5 明确禁止再传未声明字段", "不要" in t)
check("B6 长度可控（不把上下文吃满）", 0 < len(t) <= 1200, "%d 字符" % len(t))

print()
print("== C 前置而非追加（实证：追加在末尾模型不采纳）==")
out = lead_with_hint("原始报文内容", t)
check("C1 提示在最前", out.startswith(t[:12]))
check("C2 原文完整保留（不删不改）", "原始报文内容" in out)
check("C3 空提示时原样返回", lead_with_hint("abc", "") == "abc")

print()
print("== D 不编造：非参数错误 / 无 field 名的报文必须原样放行 ==")
for label, txt in [
    ("普通成功结果", '{"ok":true,"data":[1,2]}'),
    ("普通文本", "工具执行完成"),
    ("空串", ""),
    ("字段无名", '{"code":"INVALID_ARGUMENTS","fields":[{"constraint":"KnownField"}]}'),
    ("损坏 JSON", '{"code":"INVALID_ARGUMENTS","fields":['),
]:
    check("D 不放行误判: %s" % label, parse_arg_error(txt) is None)

# 反向：没有 fields 但 code 是参数类 → 应返回结构（code 本身就有价值）
h2 = parse_arg_error('{"code":"INVALID_ARGUMENTS","fields":[]}')
check("D code 命中但无 fields → 视为无可用信息（返回 None）", h2 is None)

print()
print("== E 学到就用：learn → recall → describe_suffix 闭环 ==")
# ⚠️ 必须**完全隔离**，不能靠 os.environ + reload：
#   首版用 `os.environ["MBSE_DB_PATH"]=临时路径` + `importlib.reload(database)`，
#   结果 `learn()` **写进了生产库 mbse.db**（实测该表出现 query_data_tree/hit_count=4），
#   且同一门禁连跑两次 hit_count 累加 ⇒ E4/E5 **假红**（断言写死 ==1，实际 ==5）。
#   ⇒ 改为直接替换 `agent.tool_arg_hint._conn`，指向本脚本自建的连接，零副作用。
_TMP = os.path.join(_ROOT, "tmp", "_s2_gate.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
_own = sqlite3.connect(_TMP)
import agent.tool_arg_hint as _tah                           # noqa: E402
_tah._conn = lambda: _own        # learn/recall 都走这里 ⇒ 绝不碰生产库
with _own:
    _own.execute("""CREATE TABLE IF NOT EXISTS tool_arg_hints (
        tool_name TEXT PRIMARY KEY, allowed_fields TEXT DEFAULT '[]',
        last_bad_field TEXT DEFAULT '', source_code TEXT DEFAULT '',
        hit_count INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _own.execute("DELETE FROM tool_arg_hints")   # 幂等：清掉上次残留

n = _tah.learn("query_data_tree", h)
check("E1 learn 写入成功", n == 1, n)
r = _tah.recall("query_data_tree")
check("E2 recall 取回 allowed", r.get("allowed") == h["allowed"], r.get("allowed"))
check("E3 recall 记住错字段", r.get("last_bad_field") == "value", r.get("last_bad_field"))
_after1 = r.get("hit_count")
check("E4 首次 hit_count 为 1", _after1 == 1, _after1)
_tah.learn("query_data_tree", h)
_after2 = _tah.recall("query_data_tree").get("hit_count")
# 断言**累加关系**而非绝对值（后者换个起点就假红——本仓已有多次同型教训）
check("E5 重复 learn 累加而非重复插入", _after2 == _after1 + 1, "%s→%s" % (_after1, _after2))

suf = _tah.describe_suffix("query_data_tree")
check("E6 describe_suffix 产出注入文本", bool(suf), suf[:60])
check("E7 注入文本含可用字段", "keyword" in suf)
check("E8 注入文本含「不要使用 value」告警", "value" in suf and "不要" in suf)
check("E9 没学过的工具不产出注入文本", _tah.describe_suffix("从未见过的工具") == "")

print()
print("== F 接线事实（源码级，防「函数写了但没接」）==")
tsrc = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()
code_lines = [ln for ln in tsrc.splitlines() if not ln.lstrip().startswith("#")]
code = "\n".join(code_lines)
check("F1 失败汇合点调用了 parse_arg_error", "parse_arg_error" in code)
check("F2 调用了 learn（学到就用）", "learn(name, _ah)" in code)
check("F3 调用了 lead_with_hint（前置）", "lead_with_hint" in code)
check("F4 标记 retryable=False（参数错不该被当瞬时故障重试）",
      'result["retryable"] = False' in code)
check("F5 工具定义注入 describe_suffix", "describe_suffix" in code)
check("F6 注入受开关控制（可回退）", "inject_arg_hints" in code)
csrc = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
check("F7 配置项已注册", '"inject_arg_hints"' in csrc)

# 清理
for _p in (_TMP,):
    try:
        os.remove(_p)
    except Exception:
        pass

print()
print("[RESULT] PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
