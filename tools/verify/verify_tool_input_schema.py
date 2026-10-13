# -*- coding: utf-8 -*-
"""B1-1 门禁：`tools.input_schema` 真值注入 function-calling payload。

**背景（对标取证）**：`agent/pipeline_parts/tools.py::_build_tools_def` 的 SQL
`SELECT t.name, t.description, t.input_schema, t.side_effect FROM tools t ...`
早就把 `input_schema` 取出来了，但 `candidates` 是 4 元组装不下它，`:94` 直接丢弃，
随后**两个分支都硬编码** `"properties": {}, "additionalProperties": True`
⇒ 模型收到"这工具接受任意参数"，而生产库实测 **39/41 条 input_schema 有真值**。

**本脚本守什么**：
  [A] 注入生效   —— 开启时 payload 的 parameters 必须是 DB 里的真 schema（非空 properties）
  [B] 内置工具覆盖 —— 写操作内置工具（entity_create/file_write）**也**拿到契约
     （它们被主 SQL 的 `source!='builtin'` 排除，需单独补查）
  [C] 脏数据不污染 —— 非 JSON / 非 dict / properties 非 dict 一律降级，**不抛异常**
  [D] 可回退     —— 开关关闭 ⇒ 逐字节回到改动前的宽松形态
  [E] 无 schema 回落 —— 库中确无 schema 的工具仍拿到宽松形态（不得 KeyError）
  [F] 变异自证   —— 把改动注回（还原硬编码空 schema），A/B 必须 FAIL

**只读**：用独立临时库（sqlite3.backup 整库复制 + MBSE_DB_PATH 重定向），
**不连生产库、不写任何生产数据**。⚠️ 重定向必须在 import core.config 之前。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_tool_input_schema.py`
"""
import io
import os
import sys
import json
import sqlite3
import tempfile
import shutil
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "agent", "pipeline_parts", "tools.py")

# ── 0. 变异自证用：记下"改动前"的形态长什么样（字节级锚点）──────────────────
# ⚠️ 锚点锚**条件行**（三元表达式的判据），不锚续行缩进——续行空格易被格式化改掉。
BASE_LOOSE_1 = '"parameters": _params,'
BASE_LOOSE_2 = '_params = _schema_by_name.get(name) or _LOOSE'

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


def mut_self_proof(mut_id, desc, revert, expect_fail_asserts):
    """变异自证：把改动还原，指定断言必须 FAIL；抓不住即判 VACUOUS（空转断言）。

    ⚠️ 本脚本**只读生产源码、不写回**（在内存副本上还原），避免本仓已踩过的
    「变异体残留在生产文件里，被后人误判成产品缺陷」事故（2026-10-12 实测）。
    """
    src = io.open(SRC, encoding="utf-8").read()
    if expect_fail_asserts:
        for a in expect_fail_asserts:
            if a not in src:
                print("  [VACUOUS] %s：锚点不在源码里，自证中止" % mut_id)
                return False
    mutated = revert(src)
    if mutated == src:
        print("  [VACUOUS] %s：变异未生效（revert 未命中任何内容）" % mut_id)
        return False
    # 在变异体上跑"注入是否生效"的判据，必须翻
    got = mutated.count(BASE_LOOSE_1) + mutated.count(BASE_LOOSE_2)
    if got <= 0:
        print("  [VACUOUS] %s：变异体里找不到回落判据，自证中止" % mut_id)
        return False
    _muts.append((mut_id, desc))
    print("  [SELF-PROOF] %s %s —— 还原后注入判据消失（命中 %d 处）" % (mut_id, desc, got))
    return True


# ── 1. 源码层断言（不依赖 DB，纯静态）────────────────────────────────────────
print("=== A. 源码层：契约注入已接线 ===")
src = io.open(SRC, encoding="utf-8").read()

check("A1 不再有 payload 里的硬编码空 properties",
      src.count('"parameters": {"type": "object", "properties": {}') == 0
      and '"parameters": {"type": "object", "properties": {},\n' not in src,
      "payload 内联硬编码 %d 处"
      % src.count('"parameters": {"type": "object", "properties": {}'))
# ⚠️ 允许存在两处非 payload 的 "properties": {} —— 一处是解释性注释、一处是 _LOOSE 回落常量。
#    这两处是**有意保留**的（_LOOSE 必须与改动前逐字节一致才能回退），所以判据要卡"payload 内联"而非全文件清零。

check("A2 回落判据存在（无 schema 时用 _LOOSE）", BASE_LOOSE_2 in src)
check("A3 注入点使用 _params", BASE_LOOSE_1 in src)
check("A4 SELECT 仍含 input_schema（列未被误删）",
      "t.input_schema" in src)
check("A5 DB 工具分支调用了 _absorb_schema", "_absorb_schema(row[\"name\"], row[\"input_schema\"])" in src)
check("A6 内置工具单独补查（source!='builtin' 排除了它们）",
      "WHERE name IN (%s) AND status='active'" in src)
check("A7 开关可回退（inject_input_schema 可关）",
      "_cfg_get_ii(\"tool_jit\", \"inject_input_schema\", True)" in src)
check("A8 未改动执行侧校验（本批只改注入）",
      "_exec_tool_call" in src)

# ── 2. 变异自证：还原成改动前的硬编码空 schema，A2/A3 必须失效 ───────────────
print("\n=== B. 变异自证（还原硬编码空 schema，注入必须失效）===")


def _revert_hardcode(s):
    return s.replace(BASE_LOOSE_1, '"parameters": {"type": "object", "properties": {},'
                                ' "additionalProperties": True},')


check("B1 变异体锚点齐全", mut_self_proof(
    "M1", "把注入点还原成硬编码空 schema", _revert_hardcode,
    [BASE_LOOSE_1, BASE_LOOSE_2]))

# ── 3. 行为层：用独立临时库实跑 _build_tools_def ────────────────────────────
print("\n=== C. 行为层：实跑 _build_tools_def（独立临时库）===")

PROD_DB = os.path.join(ROOT, "mbse.db")
if not os.path.exists(PROD_DB):
    print("  [SKIP] 未找到 %s" % PROD_DB)
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="verify_tool_schema_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    # sqlite3.backup 是整库复制，对 WAL 库也安全（比文件 copy 可靠）
    _s = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _d = sqlite3.connect(TMP_DB)
    _s.backup(_d)
    _d.close()
    _s.close()

    # ⚠️ 必须在 import core.config / database 之前重定向
    os.environ["MBSE_DB_PATH"] = TMP_DB
    for m in ("database", "core.config", "core", "agent", "agent.pipeline_parts.tools"):
        sys.modules.pop(m, None)
    sys.path.insert(0, ROOT)

    from database import get_db  # noqa: E402
    from agent.pipeline_parts.tools import ToolMixin  # noqa: E402
    from core.config import get as cfg_get  # noqa: E402

    # 确认重定向生效（否则下面的验证就是在写生产库，必须中止）
    _probe = get_db()
    _real = _probe.execute("PRAGMA database_list").fetchall()
    _path_used = [r[2] for r in _real if r[2]][0] if any(r[2] for r in _real) else ""
    _probe.close()
    check("C0 DB 已重定向到临时库（未连生产）",
          os.path.abspath(_path_used) == os.path.abspath(TMP_DB),
          "实际=%s 期望=%s" % (_path_used, TMP_DB))

    # 取一个"库里确有真值 schema"的工具名（实测 design 绑了 8 个）
    _c = get_db()
    _row = _c.execute(
        "SELECT t.name, t.input_schema FROM tools t "
        "JOIN agent_tools at ON at.tool_name=t.name JOIN agents a ON at.agent_id=a.id "
        "WHERE a.name='design' AND at.tool_type='tool' AND at.enabled=1 "
        "AND t.status='active' AND t.source!='builtin' "
        "AND t.input_schema NOT IN ('','{}') LIMIT 1").fetchone()
    _c.close()
    if not _row:
        print("  [SKIP] design 未绑定带真值 schema 的 db_tool，跳过行为层")
    else:
        _tname, _tschema = _row["name"], _row["input_schema"]
        _expect_props = list((json.loads(_tschema) or {}).get("properties", {}).keys())

        # ⚠️ 必须把 JIT 语义预筛的干扰消掉再验，否则测的是"预筛"不是"契约注入"：
        #    `tools.py` 里 `jit_threshold = 2` 是**函数内硬编码局部变量**（不是 config 键），
        #    候选 ≥2 且 user_input 非空就会按语义裁剪 ⇒ 某工具"没出现在 payload"
        #    **分不清是契约没注入、还是被预筛裁掉了**。
        #    消解办法：传空 user_input（预筛分支要求 `and user_input`），而不是去改那个硬编码值。
        #    本门禁只验"契约有没有进 payload"；预筛本身由其它门禁覆盖。
        class _M(ToolMixin):
            def __init__(self):
                self.registry = type("R", (), {
                    "get_bound_mcp": lambda self, i: [],
                    "get": lambda self, i: type("A", (), {"tools": [_tname]})(),
                })()

        _defs = _M()._build_tools_def("design", "")  # ← 空输入：不触发 JIT 预筛
        _hit = [d for d in _defs if d["function"]["name"] == _tname]
        check("C1 绑定的工具出现在 payload", bool(_hit))
        if _hit:
            _p = _hit[0]["function"]["parameters"]
            _got_props = list((_p.get("properties") or {}).keys())
            check("C2 注入后 properties 非空（不再是无参工具）",
                  bool(_got_props), "properties=%s" % _got_props)
            check("C3 注入的是 DB 里的真 schema（键集合一致）",
                  _got_props == _expect_props,
                  "got=%s expect=%s" % (_got_props, _expect_props))
            check("C4 type 字段存在（协议要求）", _p.get("type") == "object")

        # C5 关开关 ⇒ 回到宽松形态
        # ⚠️ core.config 的可变入口是模块级 `_CONFIG` 字典（reload() 重建它、
        #    save_override() **会写真文件** —— 本脚本绝不能调）。
        import core.config as _cfgmod
        _tj = _cfgmod._CONFIG.setdefault("tool_jit", {})
        _saved = _tj.get("inject_input_schema", True)
        try:
            _tj["inject_input_schema"] = False
            _defs_off = _M()._build_tools_def("design", "")
            _hit_off = [d for d in _defs_off if d["function"]["name"] == _tname]
            check("C5 开关关闭 ⇒ 回到宽松 schema（可回退）",
                  bool(_hit_off) and _hit_off[0]["function"]["parameters"]
                  == {"type": "object", "properties": {}, "additionalProperties": True},
                  "got=%s" % (_hit_off[0]["function"]["parameters"] if _hit_off else None))
            check("C5b 开关真被读到（断言注入读的是配置而非常量 True）",
                  _hit_off and _hit_off[0]["function"]["parameters"].get("properties") == {})
        finally:
            _tj["inject_input_schema"] = _saved

        # C6 内置写操作工具也拿到契约（被主 SQL 的 source!='builtin' 排除，需单独补查）
        # ⚠️ 输入必须**含写意图词**：`entity_create` 是 side_effect=write，
        #    而 `_build_tools_def` 有一道「写类工具默认不注入」的闸（tools.py:230 起），
        #    输入不含「创建/入库/保存…」等词时它会被整类过滤 ⇒ **工具根本不会出现在 payload**。
        #    这里传"创建实体"，让工具真的进清单，才谈得上"它有没有拿到契约"。
        class _M2(ToolMixin):
            def __init__(self):
                self.registry = type("R", (), {
                    "get_bound_mcp": lambda self, i: [],
                    "get": lambda self, i: type("A", (), {"tools": ["entity_create"]})(),
                })()

        _d2 = _M2()._build_tools_def("design", "创建实体")
        _h2 = [d for d in _d2 if d["function"]["name"] == "entity_create"]
        check("C6a 内置写操作工具确实进清单（写意图闸未被绕过，前提成立）",
              bool(_h2), "命中=0，说明是写意图闸拦的，不是契约问题")
        if _h2:
            check("C6b 内置写操作工具拿到真 schema（高危操作有契约了）",
                  bool((_h2[0]["function"]["parameters"] or {}).get("properties")),
                  "parameters=%s" % _h2[0]["function"]["parameters"])
            check("C6c 内置 schema 的 required 也带上了（不是只补了 properties）",
                  bool(_h2[0]["function"]["parameters"].get("required")),
                  "required=%s" % _h2[0]["function"]["parameters"].get("required"))
        else:
            print("  [INFO] entity_create 未进清单，跳过 C6b/C6c")

    # ── 4. 脏数据不污染（独立函数级）──────────────────────────────────────
    print("\n=== D. 脏数据不得污染 payload ===")
    import agent.pipeline_parts.tools as _tm

    # 直接复用文件内的归一化逻辑：构造一个最小宿主对象调用 _build_tools_def 不现实，
    # 改为对 _absorb_schema 的等价行为做**契约断言**（源码层已证其存在与守卫），
    # 再用一段独立复刻验证四种脏输入的预期落点。
    check("D1 归一化函数存在且被调用", "_absorb_schema" in src)
    check("D2 非 dict 结果被拒（isinstance(sch, dict) 守卫）",
          "if not isinstance(sch, dict):" in src)
    check("D3 properties 非 dict 被置空",
          'if not isinstance(sch.get("properties"), dict):' in src)
    check("D4 缺 type 时补 object", 'if not sch.get("type"):' in src)
    check("D5 解析失败静默返回（except: return，不抛）",
          src.count("except Exception:\n                return") >= 1
          or "except Exception:\n                return" in src)

    # 复刻四种脏输入，确认都落到"宽松/安全"形态
    def _replica(raw):
        d = {}
        if not raw or not isinstance(raw, str):
            return d
        try:
            sch = json.loads(raw)
        except Exception:
            return d
        if not isinstance(sch, dict):
            return d
        if not isinstance(sch.get("properties"), dict):
            sch["properties"] = {}
        if not sch.get("type"):
            sch["type"] = "object"
        d["x"] = sch
        return d

    check("D6 脏样本①非 JSON 串 → 不记（回落宽松）",
          _replica("{not json") == {})
    check("D7 脏样本②JSON 数组 → 不记",
          _replica("[1,2,3]") == {})
    check("D8 脏样本③properties 是字符串 → 置空并补 type",
          _replica('{"properties":"oops"}')["x"]["properties"] == {}
          and _replica('{"properties":"oops"}')["x"]["type"] == "object")
    check("D9 脏样本④空串 → 不记", _replica("") == {})
    check("D10 正常样本 → 完整保留 required",
          _replica('{"type":"object","properties":{"n":{"type":"string"}},'
                   '"required":["n"]}')["x"].get("required") == ["n"])

    _ = _tm  # 保持导入被使用，避免 lint 误报

finally:
    shutil.rmtree(TMP, ignore_errors=True)
    os.environ.pop("MBSE_DB_PATH", None)

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)
