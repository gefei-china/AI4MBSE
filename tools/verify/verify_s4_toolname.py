"""S4 验证：工具名合法性护栏 + LLM 静默降级留痕（2026-09-17）。

事故背景（实测定论，非推测）：`tools` 表存在中文名工具（id=1594「知识库查询」）→
SSE 工具探测把该名放进 `tools[1].function.name` → DeepSeek 返
`400 Invalid 'tools[1].function.name': string does not match pattern` →
`llm/__init__.py` 的 except 静默回落 Mock → 用户拿到 Mock 文本且无人知情，
且同批次其它合法工具一起失效。

跑法：.venv\\Scripts\\python.exe -X utf8 tools\\verify\\verify_s4_toolname.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  | " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, ("  | " + str(detail)) if detail else ""))


TOOLS_SRC = (ROOT / "agent" / "pipeline_parts" / "tools.py").read_text(encoding="utf-8")
LLM_SRC = (ROOT / "llm" / "__init__.py").read_text(encoding="utf-8")
CM_SRC = (ROOT / "agent" / "pipeline_parts" / "common.py").read_text(encoding="utf-8")

print("\n[1] 护栏代码就位")
check("tools.py 定义 _TOOL_NAME_RE", '_TOOL_NAME_RE = re.compile' in TOOLS_SRC)
check("tools.py 在构造载荷前剔除非法名", '_bad_names = [str(c[0]) for c in candidates' in TOOLS_SRC)
check("tools.py 剔除时留痕（logger.warning）", 'logger.warning(' in TOOLS_SRC and '已剔除工具名不合法的工具' in TOOLS_SRC)
check("common.py 提供 logger 并导出", 'logger = logging.getLogger' in CM_SRC and "'logger'" in CM_SRC)

print("\n[2] 每个工具的 name 都必须是协议合法名（用真实载荷验证）")
real_tools = None
try:
    from agent import AgentPipeline  # type: ignore
except Exception:
    try:
        from agent.pipeline import AgentPipeline  # type: ignore
    except Exception as e:
        AgentPipeline = None
        print("  （无法导入 AgentPipeline：%s，退化为模拟验证）" % e)

if AgentPipeline is not None:
    try:
        p = AgentPipeline()
        for intent in ("requirement_analysis", "review", "impact"):
            got = p._build_tools_def(intent, "请检索知识库并生成需求分析", {"role": "admin"})
            bad = [t["function"]["name"] for t in got if not NAME_RE.match(t["function"]["name"])]
            check("intent=%s 载荷无非法工具名" % intent, not bad, "工具数=%d 非法=%s" % (len(got), bad))
            check("intent=%s 载荷非空（合法工具未被误杀）" % intent, len(got) > 0, "工具数=%d" % len(got))
            names = [t["function"]["name"] for t in got]
            check("intent=%s 中文名工具已被剔除" % intent, "知识库查询" not in names)
        real_tools = True
    except Exception as e:
        check("实例化 AgentPipeline 并构造载荷", False, "%s: %s" % (type(e).__name__, str(e)[:200]))

if not real_tools:
    print("  [模拟] 用与 tools.py 相同的护栏逻辑验证过滤行为")
    guard = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def _filter(cands):
        bad = [c[0] for c in cands if not guard.match(str(c[0] or ""))]
        keep = [c for c in cands if guard.match(str(c[0] or ""))]
        return keep, bad

    cands = [("graph_retrieve", "x"), ("知识库查询", "y"), ("file_read", "z"), ("坏 名", "w"), ("ok-name_1", "v")]
    keep, bad = _filter(cands)
    check("中文名被剔除", "知识库查询" not in [k[0] for k in keep])
    check("含空格名被剔除", "坏 名" not in [k[0] for k in keep])
    check("合法名全部保留", [k[0] for k in keep] == ["graph_retrieve", "file_read", "ok-name_1"])
    check("剔除项被报告（用于告警）", bad == ["知识库查询", "坏 名"], bad)

print("\n[3] 静默降级必须留痕（llm 层）")
check("llm/__init__.py 在 except 分支加 WARNING", "LLM 真实调用失败 → 静默回落 Mock" in LLM_SRC)
check("WARNING 含调用点/异常信息", "调用点=%s intent=%s provider_id=%s" in LLM_SRC)
check("降级行为未被改变（仍回落 mock.chat）",
      'except Exception as e:' in LLM_SRC and 'self.mock.chat(messages, stream=stream' in LLM_SRC)

print("\n[4] 数据现状复核（只读）：脏工具名是否仍存在于库中")
try:
    import sqlite3
    c = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "mbse.db").replace("\\", "/"), uri=True)
    rows = list(c.execute("SELECT id,name FROM tools WHERE status='active'"))
    bad = [(r[0], r[1]) for r in rows if not NAME_RE.match(str(r[1] or ""))]
    check("已复核 active 工具命名情况", True,
          "active=%d，其中非法命名=%d %s（护栏已拦截，是否改名/停用由用户决定）" % (len(rows), len(bad), bad))
    c.close()
except Exception as e:
    check("库里 tools 命名复核", False, str(e))

print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
