#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_capability_config —— B5-2：能力中心「工具/MCP 配置无效」+「意图标签枚举化」。

## 两个问题的实测根因（米爸 2026-10-14 13:06 报出）

### ① 工具配置无效、无法手动新增可用的 MCP
实测（用米爸提供的 dev_ai_mcp 真 MCP）：
```
POST /api/studio/mcp-servers  → {"ok":true}          ← 新增「成功」
POST /api/studio/mcp-servers/163/test → HTTP 401 invalid_token   ← 立刻用不了
```
根因是**三处 headers 全都不通**（缺任何一处都必然 401）：
| 层 | 修复前 | 实测证据 |
|---|---|---|
| `mcp_servers` 表 | **无 headers 列** | `PRAGMA table_info` 实测 |
| `MCPIn` 模型 | **无 headers 字段** | `models/studio.py` 实测 |
| 探测 (`studio_parts/mcp.py::test`) | headers **硬编码** `{Content-Type}` | 实测 401 |
| **工具执行** (`pipeline_parts/tools.py`) | headers **硬编码** | 即使探测修好，调用仍 401 |
| 健康探测 (`mcp_health.py`) | headers **硬编码** | 同上 |
| `mcp_client.py::_post` | headers **硬编码** | 同上 |
| **前端表单** (`30-agents.js`) | **已经收集了 `#tool-headers-list` 的 headers，MCP 分支的 body 里却没有它** | 代码实测 |

⇒ 前端最后一处是"**收集了却丢掉**"，最隐蔽：用户以为填了。

### ② 意图标签是固定枚举、无法动态管理、无法"多元"
`repositories/intent_sample_repo.py::VALID_INTENTS` 硬编码 **9 个**，
而 `agents.status='active'` 实有 **19 个** ⇒ **10 个真实可路由的意图在下拉里选不到**
（含 8 阶段建模流水线全家 + 主 Agent + 多方案生成）。
⚠️ 关键：`VALID_INTENTS` **只用于列表下拉，从不参与写入校验**
（`grep VALID_INTENTS` 全仓只有 1 处使用点）⇒ 它**不是安全边界**，只是候选集，
  却成了"动态能力进不来"的唯一闸门。

## 本门禁锁什么
M 组：headers 必须在**六个环节**全部贯通（任一缺失即判红）
S 组：★出参必须脱敏（`headers` 值绝不出现在列表接口 —— 否则等于把凭据广播给所有用户）
I 组：意图候选集必须**动态派生**，且覆盖 `agents.status='active'` 全集
      （判据含"新 Agent 立刻出现在候选集里"的反向验证）
"""
import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
F_REPO = os.path.join(_ROOT, "repositories", "intent_sample_repo.py")
F_MODEL = os.path.join(_ROOT, "models", "studio.py")
F_SCHEMA = os.path.join(_ROOT, "database", "schema.py")
F_COLS = os.path.join(_ROOT, "database", "migrations", "columns.py")
F_STUDIO_MCP = os.path.join(_ROOT, "routers", "studio_parts", "mcp.py")
F_TOOLS = os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py")
F_HEALTH = os.path.join(_ROOT, "mcp_health.py")
F_CLIENT = os.path.join(_ROOT, "mcp_client.py")
F_JS_AGENTS = os.path.join(_ROOT, "static", "js", "mods", "30-agents.js")
F_JS_SAMPLES = os.path.join(_ROOT, "static", "js", "mods", "42-intent-samples.js")

_PASS, _FAIL = [], []


def check(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                         ("  —— %s" % detail) if detail else ""))


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def m_group():
    print("\n=== M 组 · headers 六环节贯通（缺任一 ⇒ Bearer 类 MCP 必 401）===")
    # ① 表列
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    cols = [r[1] for r in c.execute("PRAGMA table_info(mcp_servers)")]
    check("M1 mcp_servers 表有 headers 列", "headers" in cols,
          "现有列：%s" % cols[-6:])
    # ② 模型
    src = read(F_MODEL)
    m = re.search(r"class MCPIn\(BaseModel\):(.*?)\n\nclass ", src, re.S)
    check("M2 MCPIn 模型有 headers 字段",
          bool(m) and "headers" in m.group(1),
          "" if (m and "headers" in m.group(1))
          else ("没找到 MCPIn 定义" if not m else "MCPIn 段内无 headers 字段"))
    # ③ 探测
    s3 = read(F_STUDIO_MCP)
    seg = s3[s3.find("def test_mcp_server"):]
    seg = seg[:seg.find("@router") if seg.find("@router") > 0 else 1200]
    _m3ok = ("headers" in seg and "_hdr" in seg
             and 'headers={"Content-Type"' not in seg)
    check("M3 ★探测用配置的真实 headers（不是硬编码）", _m3ok,
          "" if _m3ok else "段内未见 _hdr 合并，或仍是硬编码 headers=")
    check("M3b 探测带 Accept（Streamable HTTP 规范要求，只给 json 可能 406）",
          "Accept" in seg)
    # ④ 工具执行（关键路径：测得通≠用得了）
    # ⚠️ 窗口必须**向前**取：`row["headers"]` 在 `MCPClient(...)` 调用**之前**的
    #   `_mh = ...` 块里，只往后看 600 字符会漏掉它 ⇒ 首版误判红（红的是判据）。
    s4 = read(F_TOOLS)
    i4 = s4.find('MCPClient(row["endpoint"]')
    seg4 = s4[max(0, i4 - 900):i4 + 300] if i4 >= 0 else ""
    check("M4 ★工具执行传 headers（此前硬编码 ⇒ 探测过但调用 401）",
          'row["headers"]' in seg4 and "headers=" in seg4,
          "MCPClient 调用点前后 900 字符内未见 row[\"headers\"] → headers=")
    check("M4b 该模块 json 不是全局导入（必须用局部别名，否则运行期 NameError）",
          "import json as _json_mcp" in seg4,
          "此处用了 json. 但本模块 json 是函数内局部导入 ⇒ 会 NameError")
    # ⑤ 健康探测
    s5 = read(F_HEALTH)
    check("M5 健康探测传 headers", "headers=_h" in s5 or "headers=" in s5)
    # ⑥ 客户端
    s6 = read(F_CLIENT)
    check("M6 mcp_client 用配置头而非硬编码",
          "def _req_headers" in s6 and "headers=self._req_headers()" in s6
          and 'headers={"Content-Type": "application/json"}' not in s6)
    # ⑦ 前端：收集了却丢掉（最隐蔽的一处）
    s7 = read(F_JS_AGENTS)
    i = s7.find("if (type === 'mcp')")
    seg7 = s7[i:i + 900] if i >= 0 else ""
    has_collect = "#tool-headers-list" in s7
    has_send = bool(re.search(r"env:\s*\{\},?\s*\n?\s*headers", seg7)) or \
        ("headers}" in seg7) or ("headers," in seg7)
    check("M7 前端已收集 headers（存在 #tool-headers-list）", has_collect)
    check("M7b ★前端 MCP 分支**把 headers 放进 body**（此前收集了却丢弃）",
          has_send, "" if has_send else "MCP 分支的 body 里没有 headers ⇒ 用户填的认证头被静默丢掉")
    c.close()


def s_group():
    print("\n=== S 组 · 出参脱敏（凭据绝不出现在列表接口）===")
    s = read(F_STUDIO_MCP)
    check("S1 列表接口出参剥离 headers 字段",
          'it.pop("headers", None)' in s)
    check("S2 提供脱敏视图（只回 key/是否已设/长度）",
          "headers_masked" in s and "_mask_headers" in s)
    # ★反向：脱敏函数不得回传原值
    mm = re.search(r"def _mask_headers.*?\n(?=@router|def )", s, re.S)
    body = mm.group(0) if mm else ""
    # 反向判据：脱敏函数的 for 循环体里**不得**直接出现 srv/原值
    _loop = re.search(r"for k,\s*v\s*in.*?\n(.*?)(?=\n\s*return)", body, re.S)
    _loopbody = _loop.group(1) if _loop else ""
    _s3 = bool(_loop) and "srv" not in _loopbody and "str(v)" not in _loopbody.replace("len(s)", "")
    check("S3 ★脱敏函数不回传原值（循环体内不得取 srv/原值）", _s3,
          "" if _s3 else "循环体内疑似回传了原值：%r" % _loopbody[:70])
    check("S4 脱敏函数的 docstring 说明了为何必须脱敏",
          "任何登录用户" in body or "广播" in body)


def i_group():
    print("\n=== I 组 · 意图候选集动态派生 ===")
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    src = read(F_REPO)
    check("I1 存在 _dynamic_intents", "def _dynamic_intents" in src)
    _i2 = '"intents": self._dynamic_intents()' in src
    check("I2 列表接口用动态候选集（不再是常量）", _i2,
          "" if _i2 else "仍写 list(VALID_INTENTS) ⇒ 候选集被常量锁死")
    check("I3 ★候选集含 agents.status='active' 全集",
          "SELECT name FROM agents WHERE status='active'" in src)
    check("I4 候选集并入样本池历史值（存量标注一个都不能丢）",
          "SELECT DISTINCT intent FROM intent_samples" in src)
    check("I5 查询失败静默降级到常量（列表附属信息不该让页面 500）",
          "except Exception" in src and "return list(VALID_INTENTS)" in src)
    # ★实证：新能力必须立刻出现在候选集里
    active = {r[0] for r in c.execute("SELECT name FROM agents WHERE status='active'")}
    from repositories.intent_sample_repo import IntentSampleRepo
    repo = IntentSampleRepo(c)
    got = set(repo._dynamic_intents())
    missing = sorted(active - got)
    check("I6 ★实测：%d 个 active Agent 全部在候选集里" % len(active),
          not missing, "缺失：%s" % missing[:8])
    check("I7 候选集数量 > 9（不再是硬编码 9 个）", len(got) > 9,
          "实测 %d 个" % len(got))
    # 标签
    check("I8 提供 intent_labels（动态意图要有中文名）",
          "def _intent_labels" in src and '"intent_labels"' in src)
    js = read(F_JS_SAMPLES)
    check("I9 前端合并后端返回的标签", "intent_labels" in js)
    _i10 = all(k in js for k in ("architecture_skeleton", "view_expansion",
                                "requirement_structuring", "trace_verification"))
    check("I10 前端静态字典补了 8 阶段 Agent 的中文名", _i10,
          "" if _i10 else "静态字典缺 8 阶段的中文名 ⇒ 下拉里只能显示英文")
    c.close()


def main():
    print("=== verify_capability_config · B5-2 ===")
    m_group()
    s_group()
    i_group()
    print("\n%d pass / %d fail" % (len(_PASS), len(_FAIL)))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())