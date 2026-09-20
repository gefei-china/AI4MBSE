"""基础通用文件工具 + 基础通用技能（文件操作 / 行业调研）闭环验证。

覆盖：
T1.  工具注册：6 个 file_* 工具入 tools 表（side_effect/risk_level 正确、status=active）
T2.  文件读写往返：file_write + file_append + file_read（中文 UTF-8）
T3.  目录操作：file_mkdir + file_list（递归、条目格式）
T4.  删除：文件删除 / 空目录 / 非空目录需 recursive / 递归删除
T5.  关键文件保护：写/删平台 DB 被拒、读允许；非空目录递归删除含关键文件被拒
T6.  Agent 链路：_build_tools_def 注入 file_read；_exec_tool_call 执行 file_write；
     file_delete 走 destructive 门控（人工确认，不直接执行）
T7.  工作流链路：ToolRegistry.list() 含 file_*；ToolExecutor.execute(file_write/file_read) 端到端；
     FlowExecutor 最小串行流程（file_write → file_read）执行成功
T8.  技能注册：文件操作 / 行业调研 published、allowed_tools、triggers、scripts/references 清单
T9.  技能脚本：行业调研 main.py run() 产出报告骨架；文件操作 main.py 读写封装
T10. 技能渐进披露：_build_skill_prompt 命中「行业调研」触发词注入 📄📝⚙ + allowed_tools 白名单
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 重定向下避免 GBK 无法编码 emoji/中文
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_file_tools_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

from database import db_conn, init_db  # noqa: E402

init_db()

# 注册基础通用技能 + 同步 file_* 工具（幂等）
import register_skills  # noqa: E402
register_skills.main()

from file_tools import FILE_TOOL_NAMES, exec_file_tool, _critical_files  # noqa: E402

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


WORK = os.path.join(os.path.dirname(__file__), "_ft_work")
import shutil
if os.path.exists(WORK):
    shutil.rmtree(WORK)
os.makedirs(WORK)

print("== T1: 工具注册（tools 表）==")
with db_conn() as conn:
    rows = {r["name"]: r for r in conn.execute(
        "SELECT name, source, side_effect, risk_level, status FROM tools WHERE name LIKE 'file_%'").fetchall()}
expect = {
    "file_list": ("read", "low"), "file_read": ("read", "low"),
    "file_write": ("write", "medium"), "file_append": ("write", "medium"),
    "file_mkdir": ("write", "low"), "file_delete": ("destructive", "high"),
}
chk("T1 六工具全部注册", set(rows) == set(expect), f"got={sorted(rows)}")
for n, (se, rk) in expect.items():
    r = rows.get(n)
    chk(f"T1 {n} side_effect={se}/risk={rk}",
        r and r["side_effect"] == se and r["risk_level"] == rk and r["source"] == "builtin" and r["status"] == "active",
        dict(r) if r else "missing")

print("== T2: 文件读写往返（中文 UTF-8）==")
p1 = os.path.join(WORK, "子目录", "报告.txt")
r = exec_file_tool("file_write", {"path": p1, "content": "天基网络需求基线\n第二行"})
chk("T2 file_write 自动建父目录", r["ok"] and os.path.isfile(p1), r["result"])
r = exec_file_tool("file_append", {"path": p1, "content": "\n追加行"})
chk("T2 file_append", r["ok"], r["result"])
r = exec_file_tool("file_read", {"path": p1})
chk("T2 file_read 内容完整（含追加）", r["ok"] and "天基网络需求基线" in r["result"] and "追加行" in r["result"], r["result"][:200])
r = exec_file_tool("file_read", {"path": os.path.join(WORK, "不存在.txt")})
chk("T2 file_read 不存在返回失败", not r["ok"], r["result"])

print("== T3: 目录操作 ==")
d1 = os.path.join(WORK, "dirA", "sub")
r = exec_file_tool("file_mkdir", {"path": d1})
chk("T3 file_mkdir 递归", r["ok"] and os.path.isdir(d1), r["result"])
open(os.path.join(WORK, "dirA", "a.txt"), "w", encoding="utf-8").write("x")
r = exec_file_tool("file_list", {"path": os.path.join(WORK, "dirA"), "recursive": True})
chk("T3 file_list 递归含子目录文件", r["ok"] and "a.txt" in r["result"] and "sub" in r["result"], r["result"][:300])
r = exec_file_tool("file_list", {"path": os.path.join(WORK, "no-such-dir")})
chk("T3 file_list 不存在目录失败", not r["ok"], r["result"])

print("== T4: 删除 ==")
p2 = os.path.join(WORK, "del.txt")
open(p2, "w", encoding="utf-8").write("to delete")
r = exec_file_tool("file_delete", {"path": p2})
chk("T4 file_delete 文件", r["ok"] and not os.path.exists(p2), r["result"])
r = exec_file_tool("file_delete", {"path": os.path.join(WORK, "dirA", "sub")})
chk("T4 file_delete 空目录", r["ok"] and not os.path.isdir(os.path.join(WORK, "dirA", "sub")), r["result"])
r = exec_file_tool("file_delete", {"path": os.path.join(WORK, "dirA")})
chk("T4 非空目录无 recursive 拒绝", not r["ok"] and "recursive" in r["result"], r["result"])
r = exec_file_tool("file_delete", {"path": os.path.join(WORK, "dirA"), "recursive": True})
chk("T4 递归删除目录", r["ok"] and not os.path.exists(os.path.join(WORK, "dirA")), r["result"])
r = exec_file_tool("file_delete", {"path": os.path.join(WORK, "不存在")})
chk("T4 不存在路径失败", not r["ok"], r["result"])

print("== T5: 关键文件保护 ==")
_crit = sorted(_critical_files())
chk("T5 关键文件集非空（含 DB）", len(_crit) >= 1 and any("db" in c.lower() for c in _crit), str(_crit)[:200])
db_path = next((c for c in _crit if c.endswith(".db")), "")
if db_path:
    r = exec_file_tool("file_write", {"path": db_path, "content": "hack"})
    chk("T5 拒绝写平台 DB", not r["ok"] and "关键文件" in r["result"], r["result"])
    r = exec_file_tool("file_delete", {"path": db_path})
    chk("T5 拒绝删平台 DB", not r["ok"] and "关键文件" in r["result"], r["result"])
    r = exec_file_tool("file_read", {"path": db_path})
    chk("T5 读 DB 允许（返回失败仅是二进制判定或内容）", r["ok"] or "二进制" in r["result"], r["result"][:100])

print("== T6: Agent 链路 ==")
from agent import AgentPipeline  # noqa: E402
pipe = AgentPipeline()
# 技能白名单契约注入：Skill 声明 file_* → 即使 Agent 未显式绑定也补入候选
pipe._skill_allowed_tools = set(FILE_TOOL_NAMES)
tools_def = pipe._build_tools_def("design", "把这份设计说明整理成文档材料", user={"role_name": "系统管理员"})
injected = {t["function"]["name"] for t in tools_def}
chk("T6 技能白名单 file_read 注入", "file_read" in injected, f"injected={sorted(injected)}")
chk("T6 技能白名单 file_write 注入（写契约放行）", "file_write" in injected, f"injected={sorted(injected)}")
pipe._skill_allowed_tools = None
wpath = os.path.join(WORK, "agent_out.txt")
r = pipe._exec_tool_call("file_write", {"path": wpath, "content": "agent 写入中文"})
chk("T6 _exec_tool_call file_write 执行", r.get("ok") and os.path.isfile(wpath), str(r)[:200])
r = pipe._exec_tool_call("file_read", {"path": wpath})
chk("T6 _exec_tool_call file_read 回读", r.get("ok") and "agent 写入中文" in r.get("result", ""), str(r)[:200])
r = pipe._exec_tool_call("file_delete", {"path": wpath})
chk("T6 file_delete 走 destructive 门控（人工确认，不直接执行）",
    (not r.get("ok") and r.get("requires_confirm")) or ("人工确认" in str(r.get("result"))),
    str(r)[:200])
chk("T6 file_delete 未真正删除", os.path.exists(wpath), "file should still exist")

print("== T7: 工作流链路 ==")
from workflows import ToolRegistry, ToolExecutor, FlowExecutor  # noqa: E402
with db_conn() as conn:
    reg = ToolRegistry(conn)
    reg_names = {t["name"] for t in reg.list()}
chk("T7 ToolRegistry 含 file_*", set(FILE_TOOL_NAMES) <= reg_names, f"missing={set(FILE_TOOL_NAMES) - reg_names}")
ex = ToolExecutor()
r = ex.execute("file_write", {"path": os.path.join(WORK, "wf.txt"), "content": "工作流写入"})
chk("T7 ToolExecutor file_write", r.get("ok"), str(r)[:200])
r = ex.execute("file_read", {"path": os.path.join(WORK, "wf.txt")})
chk("T7 ToolExecutor file_read", r.get("ok") and "工作流写入" in r.get("result", ""), str(r)[:200])
# FlowExecutor 最小串行流程：file_write → file_read
nodes = [
    {"id": "n1", "type": "tool", "label": "写文件", "config": {"tool": "file_write",
     "arguments": {"path": os.path.join(WORK, "flow.txt"), "content": "流程写入"}}},
    {"id": "n2", "type": "tool", "label": "读文件", "config": {"tool": "file_read",
     "arguments": {"path": os.path.join(WORK, "flow.txt")}}},
]
edges = [{"id": "e1", "source": "n1", "target": "n2"}]
fe = FlowExecutor()
with db_conn() as conn:
    res = fe.run(nodes, edges, payload={}, conn=conn, persist=False)
n2out = res.get("results", {}).get("n2") or res.get("n2") or {}
statuses = {k: v.get("status") for k, v in (res.get("results") or {}).items()}
chk("T7 FlowExecutor 两节点均 done", set(statuses.values()) == {"done"}, str(statuses)[:200])
chk("T7 flow 写出文件存在", os.path.isfile(os.path.join(WORK, "flow.txt")))

print("== T8: 技能注册 ==")
with db_conn() as conn:
    skills = {r["name"]: dict(r) for r in conn.execute(
        "SELECT name, skill_type, status, allowed_tools, triggers, `references`, scripts FROM skills "
        "WHERE name IN ('文件操作','行业调研')").fetchall()}
chk("T8 两技能已注册", set(skills) == {"文件操作", "行业调研"}, f"got={sorted(skills)}")
for nm in ("文件操作", "行业调研"):
    s = skills.get(nm, {})
    chk(f"T8 {nm} published+package", s.get("status") == "published" and s.get("skill_type") == "package",
        json.dumps(s, ensure_ascii=False)[:200])
fo = skills.get("文件操作", {})
at = json.loads(fo.get("allowed_tools") or "[]")
chk("T8 文件操作 allowed_tools=6 file_*", set(at) == set(FILE_TOOL_NAMES), str(at))
chk("T8 文件操作 references/scripts 清单", len(fo.get("references") or []) >= 1 and len(fo.get("scripts") or []) >= 1,
    f"refs={fo.get('references')} scripts={fo.get('scripts')}")
ir = skills.get("行业调研", {})
chk("T8 行业调研 triggers 含'行业调研'", "行业调研" in (ir.get("triggers") or []), str(ir.get("triggers")))
ir_refs = json.loads(ir.get("references") or "[]")
chk("T8 行业调研 references 含撰写指南",
    any("撰写指南" in str(x) for x in ir_refs), str(ir_refs)[:200])

print("== T9: 技能脚本 ==")
with db_conn() as conn:
    rows = {r["name"]: r["package_path"] for r in conn.execute(
        "SELECT name, package_path FROM skills WHERE name IN ('文件操作','行业调研')").fetchall()}
for nm, script_file, fn in (("行业调研", "main.py", "run"), ("文件操作", "main.py", "run")):
    pkg = rows.get(nm)
    spec = importlib.util.spec_from_file_location(f"_skill_{nm}", os.path.join(pkg, "scripts", script_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if nm == "行业调研":
        out = mod.run("AI Agent 平台搭建", 输出路径=os.path.join(WORK, "行业调研报告.md"))
        report_text = open(os.path.join(WORK, "行业调研报告.md"), encoding="utf-8").read()
        chk("T9 行业调研脚本产出报告骨架",
            os.path.isfile(os.path.join(WORK, "行业调研报告.md")) and "行业方案概览与对比" in report_text, str(out)[:200])
        chk("T9 行业调研骨架含六模块",
            "Tools" in report_text and "MCP" in report_text and "Skills" in report_text and "多Agent" in report_text,
            report_text[:120])
    else:
        rp = os.path.join(WORK, "skill_fo.txt")
        out = mod.run(rp, content="技能脚本写入", mode="write")
        chk("T9 文件操作脚本写入", os.path.isfile(rp) and "已write" in str(out), str(out)[:200])
        out2 = mod.run(rp, mode="read")
        chk("T9 文件操作脚本读取", "技能脚本写入" in str(out2), str(out2)[:200])

print("== T10: 技能渐进披露 ==")
from agent import AgentPipeline as AP  # noqa: E402
pipe2 = AP()
prompt = pipe2._build_skill_prompt("design", "帮我写一份 AI Agent 平台的行业调研报告，分析竞品并给出选型建议",
                                   user={"role_name": "系统管理员"})
chk("T10 触发行业调研技能", "行业调研" in prompt, prompt[:200])
chk("T10 渐进披露 参考+脚本清单（📄⚙）", "📄" in prompt and "⚙" in prompt, prompt[:400])
chk("T10 allowed_tools 白名单生效", bool(getattr(pipe2, "_skill_allowed_tools", None))
    and "file_write" in (getattr(pipe2, "_skill_allowed_tools") or set()), str(getattr(pipe2, "_skill_allowed_tools", None))[:200])
# 技能触发后 _build_tools_def：行业调研 allowed_tools 含 file_write → 写契约放行，注入候选
td2 = pipe2._build_tools_def("design", "帮我写一份 AI Agent 平台的行业调研报告，分析竞品并给出选型建议",
                             user={"role_name": "系统管理员"})
inj2 = {t["function"]["name"] for t in td2}
chk("T10 技能触发后 file_write 注入（写契约）", "file_write" in inj2, f"injected={sorted(inj2)}")
chk("T10 技能触发后 graph_retrieve 注入", "graph_retrieve" in inj2, f"injected={sorted(inj2)}")
no_hit = pipe2._build_skill_prompt("design", "今天天气怎么样", user={"role_name": "系统管理员"})
chk("T10 无关输入不触发", "行业调研" not in no_hit, no_hit[:100])

# 清理
import shutil as _sh
_sh.rmtree(WORK, ignore_errors=True)
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)

print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
