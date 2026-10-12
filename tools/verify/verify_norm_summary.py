# -*- coding: utf-8 -*-
"""归一校验「按钮徽标」功能验证（2026-10-10）

背景：代码卡下方的「🧹 归一校验」按钮要**在点开前**显示待确认数量与构成，
点开后实时反映确认进度。为此新增了只读统计接口 GET /normalize-summary，
并把原 normalize-report 的取数主体抽成 _load_norm_report（纯搬运，行为不变）。

本脚本验证的是**行为**，不是"代码里有没有那句话"：
  D1 路由存在                      （弱断言：源码级，仅证明注册了）
  D2 只读性 ★核心                  （行为级：连调 3 次不改发现池；对照实验证明判据有牙齿）
  D3 前后端 blocking 常量一致      （行为级：解析前端数组与后端元组比对）
  D4 统计口径一致                  （行为级：summary.need_decide == 按同一常量手算）
  D5 重构无漂移                    （行为级：_load_norm_report 与手工复刻旧逻辑指纹一致）
  D6 按钮 DOM 契约                 （弱断言：源码级，仅证明三件套齐全）

★ 关键约束（也是本脚本最想守住的一条）：
  normalize-report **不是只读的** —— 它会调 _register_discoveries 把未命中词写进
  glossary_discoveries（freq+1）。按钮徽标由卡片渲染触发、会**被反复调用**，
  若复用该接口就会污染发现池 ⇒ 新接口必须严格只读（D2 守的就是这条）。

安全：整库复制到临时文件后所有用例都在临时库上跑，绝不污染生产库。
"""
import os, sys, re, io, json, sqlite3, tempfile, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ── 整库快照到临时库（含 WAL 已提交页）──
_src = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
TMP = tempfile.mktemp(suffix=".db")
_dst = sqlite3.connect(TMP)
_src.backup(_dst)
_dst.close(); _src.close()

import core.config as cfg
import database.connection as dc
cfg.DB_PATH = TMP
dc.DB_PATH = TMP
from database import get_db
import routers.conversations as RC
from sysml_importer import build_normalize_report

RESULTS = []
def check(name, passed, detail=""):
    RESULTS.append((name, bool(passed), detail))
    print(f"[{'OK  ' if passed else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))

def fingerprint(d):
    d2 = {k: v for k, v in (d or {}).items() if k != "batch_id"}   # batch_id 是随机 uuid
    return hashlib.sha256(json.dumps(d2, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]

def front_blocking():
    """解析前端 _NR_BLOCKING（static/js/mods/06-cards.js）——口径的唯一真源在前端。"""
    p = os.path.join(ROOT, "static/js/mods/06-cards.js")
    s = io.open(p, encoding="utf-8").read()
    m = re.search(r"const\s+_NR_BLOCKING\s*=\s*\[([^\]]*)\]", s)
    if not m:
        return None
    return tuple(x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip())

def disc_state(conn):
    return tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(freq),0) FROM glossary_discoveries").fetchone())

conn = get_db()
row = conn.execute(
    "SELECT message_id, conversation_id FROM sysml_versions "
    "WHERE content IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
if not row:
    print("无可用样本（sysml_versions 为空），跳过")
    sys.exit(0)
MID, CID = row["message_id"], row["conversation_id"]
print(f"样本：msg={MID} conv={CID}（临时库 {os.path.basename(TMP)}）\n")

# ── D1 路由存在（弱） ──
src_router = io.open(os.path.join(ROOT, "routers/conversations.py"), encoding="utf-8").read()
check("D1 新增 GET /messages/{id}/normalize-summary 已注册",
      '@router.get("/api/conversations/{conv_id}/messages/{msg_id}/normalize-summary")' in src_router,
      "（弱断言：仅证明路由注册，不代表行为正确）")

# ── D2 只读性（★核心） ──
try:
    b = disc_state(conn)
    for _ in range(3):
        RC.normalize_summary(CID, MID, conn=conn, user={})
    a = disc_state(conn)
    check("D2 连调 3 次 normalize-summary 不写发现池",
          b == a, f"前 {b} → 后 {a}")
    # 对照实验：证明「若 summary 也调了 _register_discoveries，就一定会写」。
    # ⚠️ 不能靠"调一次 normalize-report 看发现池变没变"来做对照 —— 那依赖样本
    #    凑巧含有 kind=entity 且 matching_status=none 的行（_register_discoveries 只处理
    #    entity；本样本 12 个 none 行全落在 attr 上 ⇒ 对照必然空转，实测已踩）。
    #    改为**构造探针**直接喂给 _register_discoveries：新词必然 INSERT ⇒ 对照必然成立。
    PROBE = "__verify_probe_term__"
    conn.execute("DELETE FROM glossary_discoveries WHERE term=?", (PROBE,))
    conn.commit()
    b2 = disc_state(conn)
    RC._register_discoveries(
        conn,
        {"rows": [{"matching_status": "none", "kind": "entity",
                   "name": PROBE, "instance_display": "verify"}]},
        source_ref="verify")
    a2 = disc_state(conn)
    check("D2b 对照：_register_discoveries 确实会写发现池（判据有牙齿）", a2 != b2,
          f"{b2} → {a2}（故预取绝不能复用会登记发现池的 normalize-report）")
    conn.execute("DELETE FROM glossary_discoveries WHERE term=?", (PROBE,))
    conn.commit()
    # 信息性：真实样本上 POST normalize-report 是否写入（取决于该样本有无未命中实体）
    # ⚠️ 基线必须取**删除探针之后**的状态：拿 a2（含探针）比会把"我自己删掉的"
    #    算成"normalize-report 写的"，是典型的假证据。
    base3 = disc_state(conn)
    RC.normalize_report(CID, MID, conn=conn, user={})
    c3 = disc_state(conn)
    print(f"    （信息）真实样本调 normalize-report 后：{base3} → {c3}"
          f"{'（有写入）' if c3 != base3 else '（无写入：该样本无 kind=entity 且 none 的行，属正常）'}")
except Exception as e:
    check("D2 只读性", False, repr(e))

# ── D3 前后端常量一致 ──
fb = front_blocking()
bb = RC._NR_BLOCKING_STATUSES
check("D3 后端 _NR_BLOCKING_STATUSES == 前端 _NR_BLOCKING",
      fb is not None and sorted(fb) == sorted(bb),
      f"前端 {fb} / 后端 {bb}")

# ── D4 统计口径一致 ──
try:
    rep, _ = RC._load_norm_report(conn, CID, MID)
    s = RC.normalize_summary(CID, MID, conn=conn, user={})
    rows = rep.get("rows") or []
    manual_fe = sum(1 for r in rows if (r.get("matching_status") or "") in (fb or ()))
    manual_be = sum(1 for r in rows if (r.get("matching_status") or "") in bb)
    ok = (s.get("ok") is True and s.get("need_decide") == manual_fe == manual_be
          and s.get("total") == len(rows))
    check("D4 summary.need_decide 与按前端常量手算一致", ok,
          f"summary={s.get('need_decide')} / 前端口径={manual_fe} / 后端口径={manual_be} / total={s.get('total')} vs {len(rows)}")
except Exception as e:
    check("D4 统计口径一致", False, repr(e))

# ── D5 重构无漂移：与手工复刻的旧逻辑指纹比对 ──
try:
    def _msg_code_text(mid):
        t = conn.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()
        if not (t and t["content"]):
            return ""
        m = re.search(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n"
                      r"([\s\S]*?)```", t["content"] or "", re.IGNORECASE)
        return (m.group(1).strip() if m else "")
    ver = conn.execute(
        "SELECT id, content, code_text FROM sysml_versions "
        "WHERE conversation_id=? AND message_id=? ORDER BY id DESC LIMIT 1", (CID, MID)).fetchone()
    content = json.loads(ver["content"] or "{}")
    code = (ver["code_text"] or "").strip() or _msg_code_text(MID)
    manual_rep = build_normalize_report(conn, content, code_text=code,
                                        version_id=ver["id"], message_id=MID)
    rep2, err2 = RC._load_norm_report(conn, CID, MID)
    check("D5 _load_norm_report 与手工复刻旧逻辑逐字一致（重构无漂移）",
          err2 is None and fingerprint(manual_rep) == fingerprint(rep2),
          f"fp {fingerprint(manual_rep)} vs {fingerprint(rep2)}")
except Exception as e:
    check("D5 重构无漂移", False, repr(e))

# ── D6 按钮 DOM 契约（弱） ──
js_cards = io.open(os.path.join(ROOT, "static/js/mods/06-cards.js"), encoding="utf-8").read()
js_norm = io.open(os.path.join(ROOT, "static/js/mods/07-norm.js"), encoding="utf-8").read()
for name, passed in [
    ("按钮带 .norm-btn（预取靠它定位）", 'class="btn sm ghost norm-btn"' in js_cards),
    ("按钮内含 .nr-badge 徽标容器", 'class="nr-badge"' in js_cards),
    ("操作条内含 .nr-hint 引导语容器", 'class="nr-hint"' in js_cards),
    ("有 _nrPrefetch（点开前预取统计）", "function _nrPrefetch(" in js_norm),
    ("有 _nrSyncBtn（决策后回写徽标）", "function _nrSyncBtn(" in js_norm),
    ("_nrRender 末尾调用了 _nrSyncBtn", "_nrSyncBtn();" in js_norm),
]:
    check("D6 " + name, passed, "（弱断言：源码级）")

conn.close()

# ── 变异自证：证明 D4 判据有牙齿（把后端常量改掉 ⇒ D4 必须翻红） ──
print("\n── 变异自证（D4）──")
TARGET = os.path.join(ROOT, "routers/conversations.py")
ORIG = io.open(TARGET, encoding="utf-8").read()
# ⚠️ 锚点必须与 routers/conversations.py 的常量**逐字一致**，否则 assert 中止、自证整段失效。
# 2026-10-11：`_NR_BLOCKING_STATUSES` 新增 'related'（P1-3 相关但不同实体语义），
# 变异体也改为"故意漏掉 dup_suspect"（仍与 ANCHOR 差一个元素 ⇒ D3/D4 必然判红）。
ANCHOR = "_NR_BLOCKING_STATUSES = ('matched_low', 'dup_high', 'dup_suspect', 'none', 'related', 'rejected')"
MUTANT = "_NR_BLOCKING_STATUSES = ('matched_low', 'dup_high', 'none', 'related', 'rejected')"  # 故意漏掉 dup_suspect
assert ANCHOR in ORIG, "锚点未找到，自证中止"
try:
    io.open(TARGET, "w", encoding="utf-8", newline="").write(ORIG.replace(ANCHOR, MUTANT, 1))
    code = (
        "import os,sys,json,re,io,sqlite3,tempfile,hashlib\n"
        f"sys.path.insert(0, r{ROOT!r})\n"
        "os.chdir(r'{0}')\n".format(ROOT) +
        "src=sqlite3.connect('file:mbse.db?mode=ro',uri=True)\n"
        "T=tempfile.mktemp(suffix='.db'); d=sqlite3.connect(T); src.backup(d); d.close(); src.close()\n"
        "import core.config as cfg; cfg.DB_PATH=T\n"
        "import database.connection as dc; dc.DB_PATH=T\n"
        "from database import get_db\n"
        "import routers.conversations as RC\n"
        "conn=get_db()\n"
        "r=conn.execute('SELECT message_id,conversation_id FROM sysml_versions WHERE content IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()\n"
        "MID,CID=r['message_id'],r['conversation_id']\n"
        "rep,_=RC._load_norm_report(conn,CID,MID)\n"
        "s=RC.normalize_summary(CID,MID,conn=conn,user={})\n"
        "fb=('matched_low','dup_high','dup_suspect','none','rejected')\n"
        "manual=sum(1 for x in (rep.get('rows') or []) if (x.get('matching_status') or '') in fb)\n"
        "print('MUTANT_RESULT', s.get('need_decide'), manual, s.get('need_decide')==manual)\n"
    )
    import subprocess
    pr = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                        capture_output=True, text=True, cwd=ROOT, timeout=300)
    line = [l for l in pr.stdout.splitlines() if l.startswith("MUTANT_RESULT")]
    if line:
        _, got, want, same = line[0].split()
        turned_red = (same == "False")
        print(f"  变异体（后端漏掉 dup_suspect）：summary={got} 前端口径={want} 一致={same}")
        check("M1 变异体使 D4 翻红 ⇒ 判据有牙齿", turned_red,
              "（若不变红，说明 D4 是空转断言）")
    else:
        print("  变异体输出异常：", pr.stdout[-300:], pr.stderr[-300:])
        check("M1 变异体使 D4 翻红 ⇒ 判据有牙齿", False, "无输出")
finally:
    io.open(TARGET, "w", encoding="utf-8", newline="").write(ORIG)
    restored = io.open(TARGET, encoding="utf-8").read() == ORIG
    print("  [回滚校验] conversations.py " + ("已逐字还原 ✅" if restored else "❌ 未还原！"))
    if not restored:
        sys.exit(2)

try:
    os.unlink(TMP)
except Exception:
    pass

bad = [n for n, p, _ in RESULTS if not p]
print("\n" + "=" * 62)
print(f"合计 {len(RESULTS)} 项，通过 {len(RESULTS)-len(bad)}，失败 {len(bad)}")
for n in bad:
    print("   ❌", n)
print("=" * 62)
sys.exit(1 if bad else 0)
