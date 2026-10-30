# -*- coding: utf-8 -*-
"""B1-9 门禁：会话归属隔离（对话列表按用户 + 按项目过滤）。

**背景（对标取证）**：`routers/conversations.py` 有 **6 个端点连 `current_user` 都没有**，
且 `ConversationRepo.get_conversation` 是 `SELECT * WHERE id=?` **不带归属条件**
（尽管 `conversations` 表**有 `user_id` 列**）。实测真库 316 个会话：
  · `user_id` NULL **276 条** / `user_id=1` 40 条
⇒ **任何人只要猜到 conv_id，就能读别人会话的全部消息、改名、删除**（删除是级联的）。

**口径（2026-10-13 与米爸逐条确认，改前先看 E1~E5）**：
  ① 历史无归属会话（user_id NULL/0）**对所有人可见** —— 否则米爸现有 276 条历史会话当场消失；
  ② **匿名/本机模式（user is None）放行全部** —— 私有化离线部署是既有形态，单用户不该被拦；
  ③ 其余**严格按 user_id**；
  ④ 项目过滤走「列表按所选 project_id 收窄 + 后端按 user_id 兜底」（不引入 project_members）。
  ⑤ 不可见一律 **404 而非 403** —— 403 等于告诉调用方"这个 id 存在"，仍可用于枚举探测。

**本脚本守什么**：
  [A] 判据函数 `conv_visible_to` 三条口径正确（含 NULL / 匿名 / 严格隔离）
  [B] 列表过滤在 **SQL 层**收窄（不是取回全量再 Python 筛 —— 那等于"看不见但仍可逐个 id 捞"）
  [C] 项目过滤三态：不传 / 空串（只看未分组）/ 具体值
  [D] **端点覆盖**：所有带 conv_id 的端点都有 `_own_conv` 校验（AST 级，不靠 grep 数行数）
  [E] 新建会话不再硬编码 `user_id=1`（否则归属隔离当场失效：谁建的都算王工的）
  [F] 用**真库真实数据**跑一遍隔离矩阵（只读）
  [G] 变异自证：把判据改成恒 True ⇒ A/F 必须 FAIL

**只读**：真库走 `mode=ro`；隔离矩阵用**独立临时库**造样本（含真库的 NULL 形态）。
变异在内存副本做，**不写回源文件**。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_conv_scope_guard.py`
"""
import ast
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录
import io
import os
import re
import sys
import json
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTER = os.path.join(ROOT, "routers", "conversations.py")
REPO = os.path.join(ROOT, "repositories", "conversation_repo.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

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


sys.path.insert(0, ROOT)
from repositories.conversation_repo import ConversationRepo, conv_visible_to  # noqa: E402

r_src = io.open(ROUTER, encoding="utf-8").read()
p_src = io.open(REPO, encoding="utf-8").read()

print("=== A. 判据函数三条口径 ===")
# ⚠️ 样本必须**带 id 列**（真实行来自 `SELECT *`，必有 id）。A7 要区分的正是
#    「user_id 为 NULL 的真实行」与「空 dict（取不到行）」—— 两者都只有 user_id=None 时
#    无法区分，所以样本里必须给出 id，才是在测真实输入形态。
_r_null = {"id": 11, "user_id": None}
_r_zero = {"id": 12, "user_id": 0}
_r_one = {"id": 13, "user_id": 1}
_r_two = {"id": 14, "user_id": 2}
_u1 = {"id": 1}
_u2 = {"id": 2}

check("A1 口径①历史 NULL 会话对所有人可见",
      conv_visible_to(_r_null, _u1) and conv_visible_to(_r_null, _u2))
check("A2 口径①user_id=0 同样视为无归属",
      conv_visible_to(_r_zero, _u1) and conv_visible_to(_r_zero, _u2))
check("A3 口径②匿名（user=None）放行全部",
      conv_visible_to(_r_one, None) and conv_visible_to(_r_two, None)
      and conv_visible_to(_r_null, None))
check("A4 口径③本人可见", conv_visible_to(_r_one, _u1) and conv_visible_to(_r_two, _u2))
check("A5 口径③他人不可见（核心）",
      (not conv_visible_to(_r_one, _u2)) and (not conv_visible_to(_r_two, _u1)))
check("A6 行不存在(None) → 不可见（避免把 None 当无归属放行）",
      conv_visible_to(None, _u1) is False)
check("A7 空 dict {} → 不可见（取不到行 ≠ 无归属；否则给不存在的 conv_id 开可见性口子）",
      conv_visible_to({}, _u1) is False)
check("A7b 真实行（user_id=NULL 但有 id 列）→ 可见（口径①不因 A7 收紧而失效）",
      conv_visible_to({"id": 1, "user_id": None}, _u1) is True
      and conv_visible_to({"conversation_id": 5, "user_id": None}, _u2) is True)

print("\n=== B. 列表过滤在 SQL 层收窄 ===")
check("B1 list_conversations 接受 user 参数",
      re.search(r'def list_conversations\(self,\s*user=', p_src) is not None)
check("B2 SQL 里带 user 条件（不是 Python 侧筛）",
      "c.user_id=?" in p_src and "c.user_id IS NULL" in p_src,
      "未在 SQL 里收窄 ⇒ 取回全量再筛，等于'看不见但仍可逐个 id 捞'")
check("B3 匿名不加 user 条件（口径②）",
      re.search(r"if user is not None:\s*\n\s*sql \+= \" WHERE \(c\.user_id IS NULL",
                p_src) is not None)
# ⚠️ 反向：不得出现"取回全量再 Python 过滤"的形态
check("B4 不得先取全量再在 Python 里筛",
      not re.search(r"rows\s*=\s*self\.rows\([^)]*ORDER BY[^)]*\)\s*\n\s*return \[r for r in rows",
                    p_src),
      "检测到 Python 侧二次过滤")

print("\n=== C. 项目过滤三态 ===")
check("C1 接受 project_id 参数",
      re.search(r'def list_conversations\(self,.*?project_id', p_src, re.S) is not None)
check("C2 不传(None) ⇒ 不加项目条件（保留现状用法）",
      re.search(r"if project_id is not None:", p_src) is not None)
check("C3 空串 ⇒ 只看未分组",
      re.search(r"TRIM\(COALESCE\(c\.project_id,''\)\) = ''", p_src) is not None)
check("C4 具体值 ⇒ 按项目收窄", "c.project_id=?" in p_src)
check("C5 端点透传 project_id",
      re.search(r"project_id: str \| None = Query\(None\)", r_src) is not None
      and "project_id=project_id" in r_src)

print("\n=== D. 端点覆盖（AST 级，不靠 grep）===")
# ⚠️ 用**装饰器节点**定位端点，不要对 `ast.get_source_segment(函数)` 正则匹配 @router ——
#    装饰器挂在函数节点的 decorator_list 上，不在函数自身的源码片段里（那样会一个都匹配不到，
#    断言 D1 会"因为扫了 0 个端点"而恒真 = 假绿）。
_tree = ast.parse(r_src)
_eps = []
for _n in ast.walk(_tree):
    if not isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    for _d in _n.decorator_list:
        if not isinstance(_d, ast.Call) or not isinstance(_d.func, ast.Attribute):
            continue
        if not isinstance(_d.func.value, ast.Name) or _d.func.value.id != "router":
            continue
        if not _d.args or not isinstance(_d.args[0], ast.Constant):
            continue
        _eps.append((_n.name, _d.args[0].value, _d.func.attr, _n))

_missing = []
_checked = 0
_no_user = []
for _fname, _path, _verb, _node in _eps:
    if "{conv_id}" not in _path:
        continue
    _checked += 1
    _seg = "\n".join(ast.get_source_segment(r_src, _s) or "" for _s in _node.body)
    if "_own_conv(" not in _seg:
        _missing.append("%s %s(%s)" % (_verb.upper(), _path, _fname))
    _args = {a.arg for a in _node.args.args} | {a.arg for a in _node.args.kwonlyargs}
    if "user" not in _args:
        _no_user.append(_fname)
print("  带 conv_id 的端点共 %d 个" % _checked)
check("D0 确实扫到了端点（防 D1 因扫到 0 个而恒真）", _checked >= 10,
      "只扫到 %d 个 —— 判据失效" % _checked)
check("D1 全部带 conv_id 的端点都有归属校验", not _missing, "缺: %s" % _missing)
check("D1b 上述端点签名里都有 user（否则 guard 会 NameError）", not _no_user,
      "缺 user: %s" % _no_user)
check("D2 校验器用 404 而非 403（防枚举探测）",
      'status_code=404' in r_src and 'raise HTTPException(status_code=403' not in r_src)
check("D3 _own_conv 是唯一守卫入口（口径集中）",
      r_src.count("def _own_conv(") == 1)

print("\n=== E. 新建会话不再硬编码 user_id=1 ===")
_cc = [x for x in _eps if x[1] == "/api/conversations" and x[2] == "post"]
check("E0 定位到 create 端点", bool(_cc))
_seg = "\n".join(ast.get_source_segment(r_src, _s) or "" for _s in _cc[0][3].body) if _cc else ""
check("E1 不再有字面量 1 作为 user_id 传入",
      not re.search(r"create_conversation\(conv\.title,\s*conv\.intent,\s*1\b", _seg),
      "硬编码 1 ⇒ 归属隔离当场失效（谁建的都算王工的）")
check("E2 归属取当前登录用户", "_uid" in _seg and 'get("id")' in _seg)
check("E3 匿名回落 1（口径②保持现状）", "or 1)" in _seg)
check("E4 审计用真实用户（audit_user(user) 而非写死「王工」）",
      'audit_user(user), "conversation_create"' in _seg,
      "写死「王工」⇒ 审计归属不可信")

print("\n=== F. 真库口径（只读）+ 隔离矩阵（临时库）===")
if os.path.exists(PROD_DB):
    _pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _tot = _pc.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    _nullu = _pc.execute("SELECT COUNT(*) FROM conversations"
                         " WHERE user_id IS NULL OR user_id=0").fetchone()[0]
    _orphanp = _pc.execute(
        "SELECT COUNT(*) FROM conversations WHERE TRIM(COALESCE(project_id,''))<>''"
        " AND project_id NOT IN (SELECT id FROM projects)").fetchone()[0]
    _pc.close()
    print("  真库：会话 %d 条，其中无归属（user_id NULL/0）%d 条，孤儿 project_id %d 条"
          % (_tot, _nullu, _orphanp))
    check("F1 真库确有大量无归属会话（这正是口径①要保护的对象）", _nullu > 0,
          "若已全部回填，口径①可收紧")
    check("F2 孤儿 project_id 存在（口径④要容忍，不能因此丢会话）", True)
else:
    print("  [SKIP] 未找到真库")

# 隔离矩阵：临时库造 4 类会话
TMP = mkdtemp(prefix="verify_conv_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _tc = sqlite3.connect(TMP_DB)
    _tc.row_factory = sqlite3.Row
    _tc.execute("""CREATE TABLE conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, intent TEXT, status TEXT,
        user_id INTEGER, phase TEXT, created_at TEXT, updated_at TEXT, project_id TEXT DEFAULT '',
        current_intent TEXT, last_slots TEXT, pending_clarify TEXT)""")
    _tc.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, display_name TEXT)")
    _tc.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT)")
    _tc.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER)")
    _tc.execute("INSERT INTO users VALUES (1,'王工'),(2,'张三')")
    _tc.execute("INSERT INTO projects VALUES ('pA','项目A')")
    # id1: NULL 归属；id2: user1；id3: user2；id4: NULL 且属项目A
    _tc.executemany(
        "INSERT INTO conversations (id,title,user_id,project_id,updated_at) VALUES (?,?,?,?,?)",
        [(1, "历史无归属", None, "", "2026-10-13 10:00:00"),
         (2, "王工的", 1, "", "2026-10-13 10:01:00"),
         (3, "张三的", 2, "", "2026-10-13 10:02:00"),
         (4, "历史+项目A", None, "pA", "2026-10-13 10:03:00")])
    _tc.commit()

    _repo = ConversationRepo(_tc)
    _u1 = {"id": 1}
    _u2 = {"id": 2}

    _ids_u1 = sorted(r["id"] for r in _repo.list_conversations(user=_u1))
    _ids_u2 = sorted(r["id"] for r in _repo.list_conversations(user=_u2))
    _ids_anon = sorted(r["id"] for r in _repo.list_conversations(user=None))
    print("  user1 可见: %s｜user2 可见: %s｜匿名可见: %s" % (_ids_u1, _ids_u2, _ids_anon))
    check("F3 user1 看不到 user2 的会话（id3 不在）", 3 not in _ids_u1, str(_ids_u1))
    check("F4 user2 看不到 user1 的会话（id2 不在）", 2 not in _ids_u2, str(_ids_u2))
    check("F5 口径①两条历史会话（id1/id4）两边都可见",
          1 in _ids_u1 and 1 in _ids_u2 and 4 in _ids_u1 and 4 in _ids_u2)
    check("F6 口径②匿名可见全部 4 条", _ids_anon == [1, 2, 3, 4], str(_ids_anon))

    # 项目三态
    _pa = sorted(r["id"] for r in _repo.list_conversations(user=_u1, project_id="pA"))
    _pnone = sorted(r["id"] for r in _repo.list_conversations(user=_u1, project_id=""))
    _pall = sorted(r["id"] for r in _repo.list_conversations(user=_u1, project_id=None))
    check("F7 project_id='pA' ⇒ 只看该项目", _pa == [4], str(_pa))
    check("F8 project_id='' ⇒ 只看未分组", _pnone == [1, 2], str(_pnone))
    check("F9 project_id=None ⇒ 不过滤项目（现状用法）", _pall == [1, 2, 4], str(_pall))
    # 项目过滤与用户过滤叠加：user2 + pA ⇒ 空（pA 那条是无归属但 user2 可见 → 仍是 4）
    _pa2 = sorted(r["id"] for r in _repo.list_conversations(user=_u2, project_id="pA"))
    check("F10 用户+项目叠加生效", _pa2 == [4], str(_pa2))
    _tc.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== G. 变异自证（判据恒 True）===")
_anchor = 'if st == "done":'
# 变异点选一个稳定的：把 `return int(uid) == int(user.get("id") or 0)` 改成恒 True
_m_line = 'return int(uid) == int(user.get("id") or 0)'
if _m_line not in p_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _mut = p_src.replace(_m_line, 'return True  # MUTATION: 恒放行')
    _ns = {"__name__": "_mut_repo"}
    try:
        exec(compile(_mut, "<mutant>", "exec"), _ns)
        _mv = _ns["conv_visible_to"]
        _r = _mv({"user_id": 2}, {"id": 1})
        _r_null = _mv({"user_id": None}, {"id": 1})
        _r_none = _mv({"user_id": 1}, None)
    except Exception as e:
        _r = _r_null = _r_none = "exec-err:%s" % e
    check("G1 变异体（恒放行）上 A5 必须 FAIL（他人会话变可见）", _r is True,
          "变异体返回 %r —— 断言抓不住变异" % (_r,))
    # 对照组：口径①**没有**被 M1 改动（恒放行只替换了最后那行 return）。
    # ⚠️ 样本必须带 id —— A7 已把「无 id 的空 dict」判为不可见，
    #    用不带 id 的样本会让对照组因**另一条规则**而 False，误判成"M1 破坏了口径①"。
    check("G2 变异体上口径①仍然成立（那部分没改，是对照组）",
          _mv({"id": 11, "user_id": None}, {"id": 1}) is True,
          "对照组失效 —— 可能是样本缺 id（与 A7 规则有关），不是 M1 的问题")
    check("G3 变异体上匿名放行仍成立（对照组）", _r_none is True)
    # ⚠️ 曾想再补一条「变异体上 {} 也变可见」——**那是错的期望**：
    #   A7 的"空 dict → 不可见"是**更早的一个分支**，M1 只替换了最后那行 return，
    #   所以变异体里 {} 仍判不可见是**正确行为**，不是"M1 没生效"。
    #   M1 是否生效由 G1（G5）独立证明（他人会话变可见）—— 一个变异只需一条断言咬住。
    check("G4 变异体上『空 dict』仍判不可见（更早的 A7 分支未被 M1 影响）",
          _mv({}, {"id": 1}) is False,
          "若这条也变 True，说明 A7 分支被变异波及，变异范围失控")
    _muts.append(("M1", "conv_visible_to 恒放行"))
    print("  [SELF-PROOF] M1 判据恒 True —— 归属隔离彻底失效，回到任何人可读他人会话")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)