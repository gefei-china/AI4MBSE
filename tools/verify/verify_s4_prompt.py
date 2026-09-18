"""S4 验证（2026-09-17）：配置层去重 / 占位符 / Agent 角色块按意图注入。

离线可跑，不依赖 8000 端口服务：
  .venv\\Scripts\\python.exe -X utf8 tools\\verify\\verify_s4_prompt.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  | " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, ("  | " + str(detail)) if detail else ""))


PS = (ROOT / "agent" / "pipeline_parts" / "prompt.py").read_text(encoding="utf-8")
ST = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
EX = (ROOT / "agent" / "pipeline_parts" / "execute.py").read_text(encoding="utf-8")
CM = (ROOT / "agent" / "pipeline_parts" / "common.py").read_text(encoding="utf-8")

print("\n[1] 占位符：种子模板声明过的变量必须被替换，且不得残留裸占位符")
from agent.pipeline_parts.prompt import PromptMixin  # noqa: E402


class _T(PromptMixin):
    def _build_ontology_hint(self):
        return "[本体]"


t = _T()
check("prompt.py 补齐了 linked_data_scope", '{{linked_data_scope}}' in PS and 'content.replace("{{linked_data_scope}}"' in PS)
check("prompt.py 补齐了 output_schema", 'content.replace("{{output_schema}}"' in PS)
check("prompt.py 有兜底清扫（未知 {{变量}} 不得泄漏）", 're.sub(r"\\{\\{[A-Za-z0-9_]+\\}\\}", ""' in PS)

# 逐个占位符走一遍真实替换（复制 prompt.py 中的替换序列，验算残留）
src = "本体：{{ontology_profile}} 数据：{{linked_data_scope}} 输出：{{output_schema}} 未知：{{whatever}}"
out = (src.replace("{{ontology_profile}}", t._build_ontology_hint())
          .replace("{{linked_data_scope}}", "下方「检索到的互联数据」段所列来源")
          .replace("{{output_schema}}", "自然语言正文（不展示需求/实体编号）"))
out = re.sub(r"\{\{[A-Za-z0-9_]+\}\}", "", out)
check("替换后无残留占位符", not re.findall(r"\{\{[A-Za-z0-9_]+\}\}", out), out)
check("未知占位符被清扫为空", "{{whatever}}" not in out)

print("\n[2] 共用装配片段：两条路径必须引用同一实现（消除 2,100 字 ×2 处的漂移）")
for fn in ("_build_role_block", "_build_output_rules", "_build_citation_rules", "_build_attachment_block"):
    check("prompt.py 定义 %s" % fn, ("def %s(" % fn) in PS)
    check("stream.py 使用 %s" % fn, fn in ST)
    check("execute.py 使用 %s" % fn, fn in EX)

check("stream.py 不再内联输出规范原文", "输出规范：正文一律用自然语言描述" not in ST)
check("execute.py 不再内联输出规范原文", "输出规范：正文一律用自然语言描述" not in EX)
check("stream.py 不再内联引用规范原文", "引用规范：作答时若引用了下方" not in ST)
check("execute.py 不再内联引用规范原文", "引用规范：作答时若引用了下方" not in EX)
check("execute.py 不再直取 agent_def.system_prompt（改走共用实现）",
      "agent_def.system_prompt or" not in EX)

print("\n[3] C3：流式路径按意图注入命中 Agent 的角色块（带上限）")
check("common.py 定义 _AGENT_ROLE_CAP = 1200", "_AGENT_ROLE_CAP = 1200" in CM)
check("_AGENT_ROLE_CAP 已在 __all__ 导出", "'_AGENT_ROLE_CAP'" in CM)
check("stream.py 用带 cap 的角色块", "_build_role_block(agent_def, cap=_AGENT_ROLE_CAP)" in ST)
check("stream.py 不再硬编码通用角色句", '"你是网络总体MBSE设计助手。当前意图' not in ST)


class _A:
    def __init__(self, p):
        self.system_prompt = p


long_p = "活动图" + "约束" * 2000          # 8002 字，模拟视图生成 Agent
check("cap>0 时长 prompt 被截断到 cap 并加省略号",
      len(t._build_role_block(_A(long_p), cap=1200)) == 1201 and t._build_role_block(_A(long_p), cap=1200).endswith("…"),
      "len=%d" % len(t._build_role_block(_A(long_p), cap=1200)))
short_p = "你是评审助手，输出评分。"          # 13 字，模拟核心 Agent
check("cap>0 时短 prompt 原样完整保留", t._build_role_block(_A(short_p), cap=1200) == short_p)
check("cap=0 时不截断（非流式路径原行为）", t._build_role_block(_A(long_p), cap=0) == long_p)
check("空 prompt 回落通用角色句",
      t._build_role_block(_A(""), cap=1200) == "你是网络总体MBSE设计助手。")
check("None system_prompt 也安全回落",
      t._build_role_block(_A(None), cap=1200) == "你是网络总体MBSE设计助手。")

print("\n[4] prompts 表清理（数据层，只读复核）")
import sqlite3  # noqa: E402
try:
    c = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "mbse.db").replace("\\", "/"), uri=True)
    n_total = c.execute("SELECT count(*) FROM prompts").fetchone()[0]
    n_test = c.execute("SELECT count(*) FROM prompts WHERE content LIKE '%测试内容%'").fetchone()[0]
    n_pub = c.execute("SELECT count(*) FROM prompts WHERE status='published'").fetchone()[0]
    check("测试垃圾行已清零", n_test == 0, "剩余 %d 行测试模板" % n_test)
    check("prompts 表仍有可用模板", n_total >= 1, "总 %d 行 / published %d 行" % (n_total, n_pub))
    c.close()
except Exception as e:
    check("prompts 表只读复核", False, str(e))

print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
