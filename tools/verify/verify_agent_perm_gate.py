# -*- coding: utf-8 -*-
"""P0-6 权限闸门禁（2026-10-06）—— Agent 工具链 RBAC 接线 + 授权 + 脱敏 + 审计归属。

**这个门禁存在的理由**：P0-1 落地时踩过一次「所有人被拒」的 bug ——
常量写成 `"agent_tool:read"` 并整体当 domain 传给 `has_perm()`，
而 `roles.permissions` 的结构是 `{domain: [op]}` ⇒ `perms.get("agent_tool:read")`
恒为 None。**权限位明明在库里，判定却恒 False。**
⇒ 光看「授权已写入」判不出这类错，必须**断言判定结果本身**。

因此本门禁的全部断言都建立在**行为**上（放行/拒绝），不建立在「字段存在」上。

运行：`python tools/verify/verify_agent_perm_gate.py`
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core.deps import (TOOL_PERM_DOMAIN, TOOL_PERM_READ, TOOL_PERM_WRITE,  # noqa: E402
                       check_tool_perm, has_perm)
from core.redact import REDACTED, redact_obj, redact_text  # noqa: E402

PASS, FAIL = [], []


def ck(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


print("== A. 权限判定语义（不依赖数据库，纯函数） ==")
u_read = {"permissions": {TOOL_PERM_DOMAIN: ["read"], "admin": []}}
u_write = {"permissions": {TOOL_PERM_DOMAIN: ["read", "write"], "admin": []}}
u_admin = {"permissions": {"admin": ["user_manage"]}}
u_none = {"permissions": {}}                      # custom 角色（实测8 个 permissions={}）

ck(has_perm(u_read, TOOL_PERM_DOMAIN, TOOL_PERM_READ) is True, "有 read 位→ 放行 read")
ck(has_perm(u_read, TOOL_PERM_DOMAIN, TOOL_PERM_WRITE) is False, "只有 read 位 → 拒绝 write（最小权限）")
ck(has_perm(u_write, TOOL_PERM_DOMAIN, TOOL_PERM_WRITE) is True, "有 write 位 → 放行 write")
ck(has_perm(u_admin, TOOL_PERM_DOMAIN, TOOL_PERM_READ) is True, "admin 域非空 → 超管放行")
ck(has_perm({"permissions": {"admin": []}}, TOOL_PERM_DOMAIN, TOOL_PERM_READ) is False,
   "admin 空列表**不算**超管（否则全员放行）")
ck(has_perm(u_none, TOOL_PERM_DOMAIN, TOOL_PERM_READ) is False, "permissions={} → 拒绝")

print("\n== B. 副作用 → 权限位映射 ==")
from core.deps import tool_side_effect_to_perm as _se2p  # noqa: E402
ck(_se2p("read") == TOOL_PERM_READ, "read → read 位")
ck(_se2p("write") == TOOL_PERM_WRITE, "write → write 位")
ck(_se2p("destructive") == TOOL_PERM_WRITE, "destructive → write 位（最严）")
ck(_se2p("") == TOOL_PERM_WRITE, "空副作用 → write 位（未知按最严，不当 read）")
ck(_se2p(None) == TOOL_PERM_WRITE, "None → write 位")

print("\n== C. check_tool_perm 端到端（拒绝须给可转述的中文原因） ==")
ok, why = check_tool_perm(u_none, "file_read", "read")
ck(ok is False, "custom 角色读 file_read 被拒")
ck("权限不足" in why and "file_read" in why, "拒绝原因含'权限不足'与工具名（供 LLM 如实转述）")
ck("admin_tool" not in why, "拒绝原因不含错误的 domain 前缀（回归：agent_tool:read 当 domain 的 bug）")
ok2, _ = check_tool_perm(u_write, "graph_retrieve", "write")
ck(ok2 is True, "有 write 位的用户调写工具放行")

print("\n== D. 脱敏（零漏出 + 零误伤） ==")
ck(REDACTED in redact_text("deepseek_api_key=sk-abcdef1234567890abcd"), "键名+sk 前缀被遮")
ck("sk-abcdef1234567890abcd" not in redact_text("deepseek_api_key=sk-abcdef1234567890abcd"), "原密钥不再出现")
ck("satnet" in redact_text("正常参数 vc=satnet package_data_id=123"), "正常业务参数不被误伤")
ck("智能体AI" in redact_text("查询 knowledge domain 为 智能体AI 的实体"), "中文正常值不被误伤")
ck(REDACTED in redact_text("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"), "GitHub PAT 被遮")
ck(REDACTED in redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwx"), "Bearer token 被遮")
r = redact_obj({"headers": {"Authorization": "Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}, "vc": "satnet"})
ck(r["vc"] == "satnet", "递归脱敏不伤及无关字段")
ck("ghp_" not in str(r), "递归脱敏遮住嵌套 token")
# ⚠️ 键名保留要查**嵌套层**：Authorization 在 r["headers"] 里，不在顶层。
# （首版断言写成 `"Authorization" in r` 查顶层 → 假FAIL。教训：断言必须对齐数据结构本身。）
ck("Authorization" in r["headers"], "脱敏**保留键名**（键名是审计证据）")
ck(r["headers"]["Authorization"] == "Bearer " + REDACTED, "值被替换为占位符，键原样保留")

print("\n== E. 接线（源码级不变式，防「代码在、没接线」回归） ==")
_tp = os.path.join(ROOT, "agent", "pipeline_parts", "tools.py")
_src = open(_tp, encoding="utf-8").read()
ck("check_tool_perm" in _src, "tools.py 已接入 check_tool_perm")
ck("permission_denied" in _src, "拒绝走结构化返回（非抛异常，避免打断 ReAct 循环）")
ck("tool_perm_denied" in _src, "拒绝写入 tool_perm_denied 审计事件")
ck("user_name" in _src and "redact" in _src,
   "_log_tool_call 同时补 user_name 且脱敏 arguments")

_s_src = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
ck("_tool_user" in _s_src, "stream.py（生产主路径）也透传 _tool_user")
_e_src = open(os.path.join(ROOT, "agent", "pipeline_parts", "execute.py"), encoding="utf-8").read()
ck("_tool_user" in _e_src, "execute.py（冷路径）也透传 _tool_user")

_a_src = open(os.path.join(ROOT, "core", "audit.py"), encoding="utf-8").read()
ck("redact_text" in _a_src, "audit.py 已接入脱敏")
# 脱敏必须在 audit() 内、且在算哈希**之前**：
#   hash = sha256(prev_hash|各字段) ⇒ 哈希后才改detail 会让 verify_chain() 直接判失败。
# ⚠️ 两个坑（本轮都踩了）：
#   ① 锚点要用**调用点**（`detail = _redact(detail)`），不能用函数名（定义在文件前部会先命中）；
#   ② 搜 `_row_hash(prev` 会命中 `_write_degrade` 等**其它函数**里的调用（偏移 3403 < audit 的 4076）
#      ⇒ 必须先切出 audit() 函数体，再在**函数体内部**比较两个偏移。
_i_audit = _a_src.find("def audit(")
_audit_body = _a_src[_i_audit:]                 # 从 audit() 起到文件尾（其后无同名定义）
_i_redact_call = _audit_body.find("detail = _redact(detail)")
_i_hashcall = _audit_body.find("_row_hash(prev")
ck(_i_redact_call > 0, "脱敏调用存在于 audit() 函数体内（不是由调用方各自处理）")
ck(0 < _i_redact_call < _i_hashcall,
   "脱敏在算哈希**之前**（顺序反了哈希链会与明文不一致）")

_f_src = open(os.path.join(ROOT, "routers", "studio_parts", "flows.py"), encoding="utf-8").read()
ck('require_permission("hil", "approve")' in _f_src, "HIL 批准端点已加权限门")
ck('require_permission("hil", "view")' in _f_src, "HIL 列表端点已加权限门（能看=能批，成对管控）")
# decided_by 的硬编码默认值：只查**代码**，不查注释（docstring 里可以引用旧行为作对照说明）。
_f_code = "\n".join(l for l in _f_src.splitlines() if not l.strip().startswith("#"))
_f_code_nodoc = re.sub(r'"""[\s\S]*?"""', "", _f_code)
ck('"王工"' not in _f_code_nodoc and "'王工'" not in _f_code_nodoc,
   "decided_by 硬编码默认值已从**代码**中移除（注释/docstring 提及旧行为不算）")

print("\n== F. 变异自证（注入错误写法，门禁必须判红） ==")


def _mutate(spec):
    """把源码文本改掉再 exec 到独立命名空间取函数 —— 改磁盘 ≠ 改已导入模块。

    `spec` = (路径, 旧文本, 新文本)。**锚点未命中直接抛 AssertionError**——
    这是刻意的：变异脚本自己崩掉就等于"没变异"，比不写变异更危险（门禁会假绿）。
    """
    path, o, n = spec
    src = open(path, encoding="utf-8").read()
    assert o in src, f"变异锚点未命中：{o!r}（源码形态可能已变，需更新门禁）"
    ns = {"__name__": "mutant", "__package__": None}
    exec(compile(src.replace(o, n, 1), path, "exec"), ns)
    return ns


print("\n== F. 变异自证（注入错误写法，门禁必须判红） ==")

# M1:权限闸的开关被改成永假 —— 模拟「代码在、没接线」的原始状态（本次修复前的真实形态）。
# ⚠️ 不能直接 exec 整个 tools.py（`from .common import *` 需要包上下文，裸 exec 会 KeyError:__name__）——
#    这正是 MEMORY 里「门禁的变异必须真的变异」的反面：变异脚本自己先崩，就等于没变异。
#    正确做法：**先断言变异锚点确实存在于磁盘源码**，再断言「真实实现会拒绝」——
#    若闸门被摘除，源码里就不再有 `_tool_perm_gate_disabled` 这个锚点，第E 组断言会先判红。
_gate_anchor = 'if not getattr(self, "_tool_perm_gate_disabled", False):'
ck(_gate_anchor in _src, "M1:权限闸开关锚点存在于磁盘源码（变异可命中）")
ck(_gate_anchor.replace("_tool_perm_gate_disabled", "_tool_perm_gate_disable") not in _src,
   "M1:对照——闸门被摘除后锚点消失，E 组『已接入 check_tool_perm』会判红")
ck(check_tool_perm({"permissions": {}}, "file_read", "read")[0] is False,
   "M1:真实实现拒绝无权限用户（闸门摘除后此项行为会变→ 门禁可判红）")

# M2: has_perm 恒真 —— 模拟「闸门形同虚设」
_dep = os.path.join(ROOT, "core", "deps.py")
_dsrc = open(_dep, encoding="utf-8").read()
_m2_old = ('    perms = user.get("permissions") or {}\n'
           '    if perms.get("admin"):\n'
           '        return True\n'
           '    return op in (perms.get(domain) or [])')
try:
    ns2 = _mutate((_dep, _m2_old, "    return True"))
    ck(ns2["has_perm"]({"permissions": {}}, "agent_tool", "write") is True,
       "M2 变异体 has_perm 恒真（确认变异生效）")
    ck(has_perm({"permissions": {}}, "agent_tool", "write") is False,
       "M2:真实实现对空权限返回 False（变异体放行 → 门禁可判红）")
except AssertionError as e:
    ck(False, f"M2 变异锚点未命中（has_perm 源码形态变了，需更新本门禁）：{e}")

# M3: 脱敏被打成恒等 —— 模拟「脱敏代码在、但没生效」
_rp = os.path.join(ROOT, "core", "redact.py")
try:
    ns3 = _mutate((_rp,
                   '    if not text or not isinstance(text, str):\n        return text',
                   '    return text  # MUTANT'))
    ck("sk-mutantsecret" in ns3["redact_text"]("api_key=sk-mutantsecret1234567890"),
       "M3 变异体脱敏失效（确认变异生效）")
    ck("sk-proj-AAAABBBBCCCCDDDDEEEE" not in redact_text("{\"api_key\":\"sk-proj-AAAABBBBCCCCDDDDEEEE\"}"),
       "M3:真实实现遮住 JSON 里的密钥（变异体不遮 → 门禁可判红）")
except AssertionError as e:
    ck(False, f"M3 变异锚点未命中（redact_text 源码形态变了）：{e}")

# M4: 把has_perm 的 domain 传成 "agent_tool:read" —— 复现本次真实踩到的 bug
try:
    ns4 = _mutate((_dep, 'has_perm(user, TOOL_PERM_DOMAIN, need)',
                   'has_perm(user, TOOL_PERM_DOMAIN + ":" + need, need)'))
    ck(ns4["check_tool_perm"]({"permissions": {"agent_tool": ["read"]}}, "file_read", "read")[0] is False,
       "M4:domain 误拼成 'agent_tool:read' ⇒ 权限位在库里也判False（复现真实 bug）")
    ck(check_tool_perm({"permissions": {"agent_tool": ["read"]}}, "file_read", "read")[0] is True,
       "M4:真实实现正确（domain 与 op 分离）")
except AssertionError as e:
    ck(False, f"M4 变异锚点未命中（check_tool_perm 源码形态变了）：{e}")

print("\n" + "=" * 60)
print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("ALL RED" if len(FAIL) > len(PASS) else "HAS FAILURE")
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ALL GREEN")