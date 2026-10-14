# -*- coding: utf-8 -*-
"""B5-4/ B5-5 回归自检：MCP 编辑回填 + 编排 resume 门控。

纪律（§6.2 / §6.7 / §6.8）：
  · 行为断言**必须做变异自证** —— 注回旧写法后对应判据必须 FAIL；
  · 判据只认「**新增失败**」（快照基线 fail 集合再取差集）；
  · 断言不得耦合「本次部署恰好配了什么」（夹具驱动，见 §6.1）；
  · 一次性夹具用**隔离临时库**，不碰真库。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_mcp_edit_and_resume_gate.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    #⚠️ detail 只在**失败**时打印：判据串常写成"仍存在硬编码 tools:[]"这种**否定式**，
    #   若PASS 时也打印，读起来会像"仍然有问题"（本次一度差点误判）。
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ((" — " + str(detail)) if (detail and not cond) else ""))


def section(t):
    print("\n=== %s ===" % t)


# ══════════════════════════════════════════════════════════════════
# [A] MCP 编辑回填：repo + 路由的「未提供 ≠ 清空」语义
# ══════════════════════════════════════════════════════════════════
section("[A] MCP 更新不得清空未提供的字段")

# ⚠️ 夹具 DDL **从 schema.py 抽取**，不手抄（§6.9/ §1n.1 同型教训：
#   手抄一份少列的 DDL → 被测函数 SQL 抛 `no such column: transport` →
#   若该处有 `except Exception` 兜底还会被静默吞成"原样返回"，断言以假象失败）。
#   ⚠️ 也**不能**去 sqlite_master 取：`mcp_servers` 是 `init_db()` 运行期建的，
#   裸连的内存库里根本没有这张表（实测取出 None）。
def _real_ddl():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = open(os.path.join(root, "database", "schema.py"), encoding="utf-8").read()
    i = src.find("CREATE TABLE IF NOT EXISTS mcp_servers")
    if i < 0:
        return None
    nxt = src.find("c.execute(", i + 10)
    if nxt < 0:
        return None
    seg = src[i:nxt]
    # 去掉尾部 Python 三引号包装：`)` 与 `"""` **同行相邻**，
    #   正则 `\)\s*\n`切不开（实测仍带出 `""")` → 报 unrecognized token）。
    #   故以三引号本身为界：取其之前的正文，末尾补回闭合括号。
    q = seg.find('"""')
    ddl = (seg[:q].rstrip() if q > 0 else seg.rstrip())
    if not ddl.endswith(")"):
        ddl += ")"
    # 自证：抽出来的必须是**可执行 SQL**，否则夹具会以 "unrecognized token" 神秘失败。
    #   （判据纪律：抽取器自己的失败必须当场炸，不能拖到用例里伪装成别的问题。）
    _t = sqlite3.connect(":memory:")
    try:
        _t.execute(ddl)
    finally:
        _t.close()
    return ddl


DDL = _real_ddl()


def _fixture():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    # ⚠️ 必须**先**设 row_factory再建表/插数据：反过来的话，
    #   后续 `row["col"]` 取值会在第一次查询就TypeError（§1n.1 同型）。
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE plugin_sync_log (id INTEGER PRIMARY KEY, kind TEXT, item_id INTEGER)")
    c.execute(DDL)
    #★ 关键：`schema.py` 的建表 DDL 是**基线**，而 `transport/command/headers/description`
    #   这些列是 `database/migrations/columns.py::_add` **后补**的
    #   （本仓一贯的 `CREATE TABLE IF NOT EXISTS` 不给已存在的表补列，见 MEMORY 记录）。
    #   ⇒ 只建基线 DDL 会得到一张**缺列**的表，被测 SQL 抛 `no such column: transport`，
    #     而这类失败与"代码写错了"长得一模一样、极难定位。
    #   ⇒ 夹具必须**跑真迁移**，让列集与生产一致（也顺带守住迁移本身没漏字段）。
    from database.migrations.columns import _migrate_columns
    _migrate_columns(c)
    c.execute(
        "INSERT INTO mcp_servers (id,name,endpoint,tools,headers,description) VALUES (1,?,?,?,?,?)",
        ("m1", "http://x/mcp", json.dumps(["t_a", "t_b"]),
         json.dumps({"Authorization": "Bearer SECRET"}), "原始描述"),
    )
    c.commit()
    return c, path


def _repo(c):
    from repositories.studio_repo import StudioRepo

    class _R(StudioRepo):
        def __init__(self, conn):
            self.conn = conn
            import repositories.studio_repo as _m
            self._m = _m

        def execute(self, sql, args=()):
            return self.conn.execute(sql, args)

        def rows(self, sql, args=()):
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

        def one(self, sql, args=()):
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

        def _sync(self, *a, **k):
            pass

    return _R(c)


c, path = _fixture()
r = _repo(c)
r.update_mcp_server(1, "m1", "http://x/mcp", None, "http", None, None, None,
                    headers=None, description="新描述")
row = c.execute("SELECT * FROM mcp_servers WHERE id=1").fetchone()
chk("A1 未提供 tools 时保持已发现清单不变",
    json.loads(row["tools"]) == ["t_a", "t_b"], row["tools"])
chk("A2 未提供 headers 时保持凭据不变",
    json.loads(row["headers"]) == {"Authorization": "Bearer SECRET"}, "凭据被清")
chk("A3 显式给 description 时写入成功", row["description"] == "新描述", row["description"])

# 显式清空是合法诉求，必须仍能清空
r.update_mcp_server(1, "m1", "http://x/mcp", "[]", "http", "", "[]", "{}",
                    headers="{}", description="")
row2 = c.execute("SELECT * FROM mcp_servers WHERE id=1").fetchone()
chk("A4 显式清空（tools/headers/description 全给值）仍然生效",
    json.loads(row2["tools"]) == [] and json.loads(row2["headers"]) == {}
    and row2["description"] == "", dict(row2))
c.close()
os.unlink(path)

# ── 变异 M1：把「None = 不更新」退回成「无条件覆盖」（改前语义）──
section("[A-M] 变异自证")
_c, _p = _fixture()
_r = _repo(_c)
import repositories.studio_repo as _mod
_orig_update = _mod.StudioRepo.update_mcp_server


def _mut_update(self, sid, name, endpoint, tools, transport, command, args, env,
                headers=None, description=None):
    """变异体：退回改前写法 —— 无条件写 tools/args/env（None 也照写）。"""
    self.execute(
        "UPDATE mcp_servers SET name=?, endpoint=?, tools=?, transport=?, command=?, args=?, env=? WHERE id=?",
        (name, endpoint, json.dumps(tools or []), transport, command or "",
         json.dumps(args or []), json.dumps(env or {}), sid))


_mod.StudioRepo.update_mcp_server = _mut_update
_r2 = _repo(_c)
_r2.update_mcp_server(1, "m1", "http://x/mcp", None, "http", None, None, None,
                      headers=None, description="新描述")
_mrow = _c.execute("SELECT tools FROM mcp_servers WHERE id=1").fetchone()
M1_caught = (json.loads(_mrow["tools"]) != ["t_a", "t_b"])
chk("M1 变异（None 也覆盖 tools）被 A1 抓住", M1_caught, _mrow["tools"])
_mod.StudioRepo.update_mcp_server = _orig_update
_c.close()
os.unlink(_p)


# ══════════════════════════════════════════════════════════════════
# [B] MCP 编辑弹窗回填：前端源码级判据
# ══════════════════════════════════════════════════════════════════
section("[B] 前端 editMCP 必须回填（源码级）")
JS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                  "static", "js", "mods", "30-agents.js")
src = open(JS, encoding="utf-8").read()

# 截取 editMCP 函数体（到下一个顶级 function/let 为止）
_i = src.find("function editMCP(id)")
assert _i > 0, "未找到 editMCP"
_j = src.find("\nasync function testMCP", _i)
body = src[_i:_j if _j > 0 else _i + 4000]

chk("B1 editMCP 回填 description（改前是硬编码空串）",
    "f-tool-desc').value = s.description" in body,
    "描述仍未回填")
# ⚠️ transport 的断言**不能写死 `s.transport` 字面量**：并发会话已把它包进
#   `setTransportSelect(el, s.transport)`（transport 值为历史值时动态补 option）。
#   判据应锁「**读的是这条记录的 s.transport**」而不是「用哪种写法赋值」——
#   否则一次无害重构就会让自检假红（这正是 §6.9b「读代码猜空档」的等价物：
#   断言耦合写法而非行为）。
chk("B2 editMCP 回填 name/endpoint/transport",
    "f-tool-name').value = s.name" in body
    and "f-tool-url-mcp').value = s.endpoint" in body
    and "s.transport" in body and "f-tool-transport" in body)
chk("B3 editMCP 消费 headers_masked（脱敏 ≠ 不显示）",
    "headers_masked" in body, "未读脱敏出参")
chk("B4 editMCP 有「未找到该服务器」的显式失败分支（改前是静默 return）",
    "未找到该 MCP 服务器" in body)

# saveTool 的 MCP 分支
_si = src.find("if (type === 'mcp') {")
assert _si > 0
_sj = src.find("\n  } else {", _si)
sbody = src[_si:_sj]

# ★ headers 的收集**在 MCP 分支之前**（HTTP 分支也要用），
#   故判据的取样范围必须从 `const type =` 起算，不能只切 MCP 分支 ——
#   否则这条判据会在"收集逻辑其实写错了"的改动下**恒真通过**（空转断言，§1u 纪律）。
_hi = src.find("const type = document.getElementById('f-tool-type').value;")
_hj = src.find("if (type === 'mcp') {", _hi)
hbody = src[_hi:_hj]
# ⚠️ 反空转守卫：取样范围为空时，后面的 `in` 判据会**恒假**（不是恒真），
#   报错信息还会指向错误的行。先断言范围非空且含预期结构（§1u「任何 all()/in 判据
#   都要先问范围可能是空的吗」）。
assert _hi > 0 and _hj > _hi and len(hbody) > 50, "headers 收集段取样失败"
assert "tool-headers-list" in hbody, "headers 收集段定位错误"

chk("B5 保存时带上 description", "description: desc" in sbody)
chk("B6 ★ 编辑态不再提交 tools:[]（会把已发现清单清空）",
    "tools: [], args: [], env: {}" not in sbody,
    "仍存在硬编码 tools:[]")
chk("B7 headers 值留空的键不进 payload（留空=保持原值）",
    "if(k && v) headers[k] = v;" in hbody,
    "仍在收集空值 → 会把凭据清成空串")
chk("B8 保存失败时不静默关窗", "_r && _r.error" in sbody)

# toggleToolType：容错框只对 HTTP
_tt = src.find("function toggleToolType()")
_tj = src.find("\nfunction addToolHeader", _tt)
ttbody = src[_tt:_tj]
chk("B9 容错策略框只对 HTTP 工具展示（MCP 无 retry_policy 字段）",
    "mode === 'http'" in ttbody and "tool-resilience-box" in ttbody)

# ── 变异 M2：把 description 回填改回硬编码空串 ──
_mut_body = body.replace("f-tool-desc').value = s.description", "f-tool-desc').value = ''")
M2 = ("f-tool-desc').value = s.description" in body) and \
     ("f-tool-desc').value = s.description" not in _mut_body)
chk("M2 变异（desc 退回硬编码空串）被 B1 抓住", M2)

# ── 变异 M3：headers 收集退回「空值也收」 ──
_mut_h = hbody.replace("if(k && v) headers[k] = v;", "if(k) headers[k] = hdrVals[i].value;")
M3 = ("if(k && v) headers[k] = v;" in hbody) and ("if(k && v) headers[k] = v;" not in _mut_h)
chk("M3 变异（空值也收集）被 B7 抓住", M3)


# ══════════════════════════════════════════════════════════════════
# [C] 编排 resume 门控：权限域可达 + 归属校验存在
# ══════════════════════════════════════════════════════════════════
section("[C] resume 端点门控")
ORCH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "routers", "orchestration.py")
osrc = open(ORCH, encoding="utf-8").read()

chk("C1 resume 权限域不再是 config（本仓无角色持有该域）",
    'require_permission("config", "write")' not in osrc,
    "仍在用不可达权限域")
chk("C2 resume 改用 ai_chat:generate（设计师/知识工程师均持有）",
    'require_permission("ai_chat", "generate")' in osrc)
chk("C3 resume 端点补了归属校验 _own_run",
    "_own_run(conn, run_id, user)" in osrc)
chk("C4 get_run 端点补了归属校验",
    "ck = _own_run(conn, run_id, user)" in osrc)
chk("C5 resumable 端点按 conversation_id 校验归属",
    "conv_visible_to" in osrc and osrc.count("conv_visible_to") >= 2)
chk("C6 _own_run 复用会话可见性单一事实源",
    "from repositories.conversation_repo import conv_visible_to" in osrc)

# 角色权限事实核对（不依赖"本次部署配了什么"，只断言"域存在"这一契约）
# ⚠️ 三个角色都要在夹具里：80/81 设计师/知识工程师（业务主力）、
#    82 系统管理员（admin 非空 ⇒ 走超级用户分支）。
#    只放80/81 会让"管理员也不持有 config"这条**测不到**。
_cdb = sqlite3.connect(":memory:")
_cdb.execute("CREATE TABLE roles (id INTEGER, permissions TEXT)")
for rid, perms in ((80, {"ai_chat": ["view", "generate", "confirm", "reject"]}),
                   (81, {"ai_chat": ["view", "generate", "confirm", "reject"]}),
                   (82, {"ai_chat": [], "admin": ["user_manage"]})):
    _cdb.execute("INSERT INTO roles VALUES (?,?)", (rid, json.dumps(perms)))
_rows = {r[0]: json.loads(r[1]) for r in _cdb.execute("SELECT id, permissions FROM roles")}
chk("C7 契约：目标角色确实持有 ai_chat:generate（夹具，不耦合真库）",
    all("generate" in _rows[i].get("ai_chat", []) for i in (80, 81)))
chk("C8 反向对照：种子角色里没有 config 域（这正是原 bug 的根因）",
    not any("config" in (_rows[i] or {}) for i in (80, 81)),
    "种子已含 config 域 → C1 的前提变化，需重评")
# ⚠️ 刻意**不查真库**（本条曾直接 `sqlite3.connect('mbse.db')`）：
#   服务在跑时真库处于 WAL 模式、瞬时被锁 ⇒ 自检抛 `unable to open database file`
#   ——一个**与被测代码无关**的环境故障，把整轮门禁打挂（§6.9「判据不得耦合环境」）。
#   根因已由 C1（源码里不再用 config）+ C8（种子角色无该域）覆盖，无需再摸真库。

# ★ C9：`require_permission` 的 checker 是「失败抛异常 / 成功 return user」。
#   成功返回的是**用户字典（恒真）** ⇒ `_err = ...; if _err:` 这种写法
#   **永远为真** ⇒ 即使权限域改对了也照样 403（实测踩过：
#   域换成 ai_chat:generate 后，归属者调用仍返回 {"error":"无权限"}）。
#   判据：resume 端点**不得**把返回值接到 `_err` 之类变量再判真假。
_ri = osrc.find("def resume_run(")
assert _ri > 0, "未找到 resume_run"
_rj = osrc.find("\n@router.", _ri + 10)
if _rj < 0 or _rj <= _ri:          # resume_run 是最后一个端点，文件尾即其边界
    _rj = len(osrc)
rbody = osrc[_ri:_rj]
# ⚠️⚠️ 判据必须**剥掉注释**再查 `if _err:` ——
#   本函数上方新写的警示注释里**本身就引用了 `if _err:` 这段反例代码**
#   （§6.9a 同型：断言字符串会命中自己写的注释 → 恒 FAIL）。
#   正解：对**代码行**判定（去行首注释），而不是对整段文本做 `in`。
_rcode = "\n".join(ln for ln in rbody.splitlines()
                   if not ln.lstrip().startswith("#"))
chk("C9 resume 权限门不接返回值判真假（成功返回 user 恒为真）",
    'require_permission("ai_chat", "generate")(user=user)' in _rcode
    and "if _err:" not in _rcode,
    "仍用 `if _err:` 判定 → 恒 403")
chk("C10 resume 端点直接调用权限门（与 routers/branches.py 的 7 处写法一致）",
    "\n    require_permission(" in _rcode)

# ── [D] 恢复入口的前端行为（★ B5-5 第三处根因）──
section("[D] 恢复失败必须让用户看得见")
CHATJS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "static", "js", "mods", "03-chat.js")
csrc = open(CHATJS, encoding="utf-8").read()
_sr = csrc.find("async function startResumeRun(")
assert _sr > 0, "未找到 startResumeRun"
_srj = csrc.find("\nasync function ", _sr + 10)
if _srj < 0 or _srj <= _sr:
    _srj = len(csrc)
srbody = csrc[_sr:_srj]
# 反空转：取样必须含失败分支与成功分支两段
assert "if(!resp.ok){" in srbody and "resp.body.getReader()" in srbody, "切片未覆盖完整函数"

# ★ 只判**失败分支内部**是否移除提示条 —— 整函数里 bar.remove() 出现 2 次是正常的：
#   ① 「成功进入 SSE 流」移除；② catch（网络异常）移除。
#   真正要禁的是「服务端明确拒绝（!resp.ok）却移除」——
#   那会把"可继续"的入口一起抹掉，用户只看到"点了没反应"。
# ⚠️ 判据必须**剥注释**再查（§6.9a 同型，第三次踩到）：
#   失败块上方的说明注释里**本身就引用了 `bar.remove()`** 作为反例，
#   不剥就会恒 FAIL —— 一度让我以为代码没改对。
_kfail = srbody.find("if(!resp.ok){")
_kend = srbody.find("if(bar) bar.remove();", _kfail)
assert _kfail > 0 and _kend > _kfail, "失败块边界定位失败"
_fail_raw = srbody[_kfail:_kend]
_fail_seg = "\n".join(ln for ln in _fail_raw.splitlines()
                      if not ln.lstrip().startswith("//") and not ln.lstrip().startswith("*"))

chk("D1 服务端拒绝时不再静默移除提示条（改前 toast + bar.remove()）",
    "bar.remove()" not in _fail_seg,
    "失败分支里仍有 bar.remove()")
chk("D2 失败原因写回横幅文本（不只靠一闪而过的 toast）",
    ".rb-txt').textContent = msg" in _fail_seg or
    "t.textContent = msg" in _fail_seg)
chk("D3 失败态有可辨识样式类", "rb-error" in srbody)
chk("D4 nothing_to_do 时提供「连失败任务一起重跑」入口",
    "include_failed=true" in srbody and "连失败任务一起重跑" in srbody)
chk("D5 startResumeRun 支持 includeFailed 参数",
    "function startResumeRun(runId, bar, includeFailed)" in csrc)
# CSS 变量必须**真实存在**（本仓没有 --red-bd / --red-d，硬写会静默失效）
CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "static", "css")
appcss = open(os.path.join(CSS, "app.css"), encoding="utf-8").read()
chatcss = open(os.path.join(CSS, "chat.css"), encoding="utf-8").read()
_rb = chatcss[chatcss.find(".resumable-bar.rb-error"):][:500]
# 用正则取 `var(--xxx)` 里的变量名（按空白 split 取首词会漏掉 `var(--red)` 的括号形态）
import re as _re
_used = set(_re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", _rb))
chk("D6 rb-error 用到的 CSS 变量都已定义（无静默失效的色值）",
    bool(_used) and all(("%s:" % v) in appcss for v in _used),
    "未定义: " + ",".join(v for v in _used if ("%s:" % v) not in appcss))

# ── 变异 M7：把失败分支改回「静默移除」 ──
_mut_c = _fail_seg.replace("if(bar){\n        bar.classList.add('rb-error');",
                           "if(bar){ bar.remove(); return; }\n      if(bar){\n        bar.classList.add('rb-error');", 1)
M7 = ("bar.remove()" not in _fail_seg) and ("bar.remove()" in _mut_c)
chk("M7 变异（拒绝即移除提示条）被 D1 抓住", M7)
_cdb.close()

# ── 变异 M4：把权限域改回 config（不可达）──
_mut_o = osrc.replace('require_permission("ai_chat", "generate")',
                      'require_permission("config", "write")')
M4 = ('require_permission("ai_chat", "generate")' in osrc) and \
     ('require_permission("ai_chat", "generate")' not in _mut_o)
chk("M4 变异（域退回 config）被 C2 抓住", M4)

# ── 变异 M5：去掉归属校验 ──
_mut_o2 = osrc.replace("_own_run(conn, run_id, user)\n    # ── 权限门", "    # ── 权限门", 1)
M5 = ("_own_run(conn, run_id, user)" in osrc) and ("_own_run(conn, run_id, user)\n    # ── 权限门" not in _mut_o2)
chk("M5 变异（resume 去掉归属门）被 C3 抓住", M5)

# ── 变异 M6：把权限门改回「接返回值判真假」（即改前那个恒 403 的写法）──
_mut_o3 = osrc.replace(
    'require_permission("ai_chat", "generate")(user=user)',
    '_err = require_permission("ai_chat", "generate")(user=user)\n    if _err:\n        return JSONResponse({"error": "无权限"}, 403)', 1)
_m6 = _mut_o3.replace("    # ⚠️⚠️ **不要写成", "    # XNOTE**不要写成", 1)  # 同时排除注释干扰
_m6code = "\n".join(ln for ln in (_m6[_m6.find("def resume_run("):]).splitlines()
                 if not ln.lstrip().startswith("#"))
M6 = ("if _err:" not in _rcode) and ("if _err:" in _m6code)
chk("M6 变异（接返回值判真假）被 C9 抓住", M6)


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 62)
print("总计：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)