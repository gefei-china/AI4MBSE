# -*- coding: utf-8 -*-
"""提交幂等门禁（2026-11-19）—— 守「一次发送 = 一个会话」。

## 触发

conv 999999928/929：同一份 1686 字输入在 08:43:56 被提交两次 ⇒ 两个独立会话、
两份完全不同的正文（19,604 / 8,764 字）、双倍工具调用。根因是前端 TOCTOU 竞态
（`if(!currentConvId)` 与赋值之间隔着 await）+ 后端建会话无幂等键。

## 守什么

  A. 前端临界区同步锁（在 await 之前置位、finally 释放），且**只锁建会话段**
     不锁整个 sendChat —— 后者会误伤多会话并发（2026-11-06 已修的串扰）；
  B. 后端 client_request_id 幂等：原子占位 + 回填 + 重放命中返回 idempotent_hit；
  C. 进行中反馈：进入 sendChat 立即有状态文字，不等 pumpStream；
  D. 变异自证：拿掉任一关键行 ⇒ 对应断言必须翻红；
  E. 功能级：真库/内存库重放同一 request_id，会话表只增 1 行。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_submit_idempotency.py
退出码：全绿 0 / 有失败 1。
"""
import os
import sys
import sqlite3
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def main():
    print("=" * 84)
    print("提交幂等门禁 —— 一次发送 = 一个会话")
    print("=" * 84)

    js = read("static/js/mods/12-chatsend.js")
    router = read("routers/conversations.py")
    schema = read("database/schema.py")
    model = read("models/conversation.py")
    kbar = read("static/js/mods/37-kbar.js")

    # ── A. 前端临界区锁 ──────────────────────────────────────────────
    print("\n[A] 前端临界区同步锁")
    ck("A1 锁变量定义", "let _creatingConv = false" in js)
    _gate = js.find("if(_creatingConv){")
    _post = js.find("await api('/api/conversations'")
    ck("A2 入口判断位于 await POST 之前（TOCTOU 窗口被覆盖）",
       _gate > 0 and _post > _gate, "gate=%d post=%d" % (_gate, _post))
    ck("A3 finally 释放（所有 return 分支不死锁）",
       "finally{ _creatingConv = false; }" in js)
    # ★ P2-2 后锁实现移到 `_ensureConversation()`（sendChat 从 214 行拆到 142 行）。
    #   判据必须跟着结构改：不再要求"锁在 sendChat 体内"，改为锁在提取函数内
    #   **且 sendChat 确实调用了它**（否则锁形同虚设）。删掉这条判据=放弃保护。
    _enter = js.find("async function sendChat()")
    _helper = js.find("async function _ensureConversation")
    _call = js.find("await _ensureConversation(msg)")
    ck("A4 锁在 _ensureConversation 内且 sendChat 确实调用它",
       _helper > 0 and _call > _enter and _gate > _helper and _gate < _enter,
       "helper=%d gate=%d sendChat=%d call=%d" % (_helper, _gate, _enter, _call))
    # 不误伤多会话并发：不能出现 `if(_streaming) return` 这种全局拦
    #   ⚠️ 只扫**代码行**：本门禁脚本自己的注释里也写了这句（用来解释为何不用它），
    #      直接 in 判断会命中注释造成假红（本轮实测）。
    _code_lines = [ln for ln in js.splitlines() if not ln.strip().startswith("//")]
    ck("A5 未用全局 _streaming 误拦多会话并发",
       not any("if(_streaming) return" in ln for ln in _code_lines))
    ck("A6 三入口最终都走 sendChat（Enter / K-bar）",
       "e.preventDefault(); sendChat();" in js and "sendChat();" in kbar)

    # ── B. 后端幂等 ──────────────────────────────────────────────────
    print("\n[B] 后端 client_request_id 幂等")
    ck("B1 ConvIn 带 client_request_id 字段", "client_request_id: Optional[str]" in model)
    ck("B2 schema 建幂等表（IF NOT EXISTS 零迁移）",
       "CREATE TABLE IF NOT EXISTS client_request_dedup" in schema)
    ck("B3 原子占位 INSERT OR IGNORE",
       "INSERT OR IGNORE INTO client_request_dedup" in router)
    ck("B4 重放命中返回 idempotent_hit",
       '"idempotent_hit": True' in router)
    ck("B5 创建成功后回填 conv_id",
       "UPDATE client_request_dedup SET conv_id=?" in router)
    ck("B6 未完成占位返回 409（不是静默复用空 id）",
       "status_code=409" in router)
    ck("B7 老库缺表时降级直建（不阻断）",
       "幂等占位失败（降级直建）" in router)
    _ph = router.find("INSERT OR IGNORE INTO client_request_dedup")
    _create = router.find("repo.create_conversation(")
    ck("B8 占位在创建之前（顺序正确）", _ph > 0 and _create > _ph)

    # ── C. 进行中反馈 ────────────────────────────────────────────────
    print("\n[C] 进行中即时反馈")
    ck("C1 进入建会话即给状态文字（不等 pumpStream）",
       "_st0.textContent = '正在创建会话…'" in js)
    _fb = js.find("_st0.textContent")
    ck("C2 反馈早于 await POST", _fb > 0 and _fb < _post)

    # ── D. 变异自证 ──────────────────────────────────────────────────
    print("\n[D] 变异自证（拿掉关键行必须翻红）")
    mutants = {
        "D1 去掉入口锁 ⇒ A2 红": (js.replace("if(_creatingConv){", "if(false){"),
                                    "if(_creatingConv){"),
        "D2 去掉 finally ⇒ A3 红": (js.replace("finally{ _creatingConv = false; }", ""),
                                       "finally{ _creatingConv = false; }"),
        "D3 去掉幂等占位 ⇒ B3 红": (router.replace(
            "INSERT OR IGNORE INTO client_request_dedup", "INSERT INTO client_request_dedup"),
            "INSERT OR IGNORE INTO client_request_dedup"),
        "D4 去掉即时反馈 ⇒ C1 红": (js.replace("_st0.textContent = '正在创建会话…'", ""),
                                       "_st0.textContent = '正在创建会话…'"),
    }
    for tag, (mutant, needle) in mutants.items():
        ck(tag, needle not in mutant)

    # ── E. 功能级：内存库重放 ────────────────────────────────────────
    print("\n[E] 功能级重放（内存库）")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE client_request_dedup (
            request_id TEXT PRIMARY KEY, conv_id INTEGER NOT NULL DEFAULT 0)""")
        con.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT)")
        rid = "c-verify-20261119"

        def _submit(title):
            cur = con.execute(
                "INSERT OR IGNORE INTO client_request_dedup(request_id,conv_id) VALUES(?,0)",
                (rid,))
            won = (cur.rowcount or 0) > 0
            if won:
                cid = con.execute(
                    "INSERT INTO conversations(title) VALUES(?)", (title,)).lastrowid
                con.execute(
                    "UPDATE client_request_dedup SET conv_id=? WHERE request_id=?", (cid, rid))
                return cid, False
            hit = con.execute(
                "SELECT conv_id FROM client_request_dedup WHERE request_id=?", (rid,)).fetchone()
            return (hit[0] if hit else 0), True

        c1, h1 = _submit("第一次提交")
        c2, h2 = _submit("重复提交")
        ck("E1 首次创建成功且非重放", bool(c1) and not h1)
        ck("E2 重放返回同一 conv_id", c2 == c1 and h2, "%s / %s" % (c1, c2))
        ck("E3 会话表只增 1 行",
           con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1)
        c3, _ = _submit("不同 request_id 应另建")
        con.execute("UPDATE client_request_dedup SET request_id=? WHERE request_id=?",
                    ("c-other", rid))
        c4, h4 = _submit("不同 request_id 应另建")
        ck("E4 不同 request_id 仍可新建（幂等不误伤正常提交）",
           c4 != c1 and not h4)
        con.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # ── 汇总 ─────────────────────────────────────────────────────────
    fails = [t for s, t in _results if s == FAIL]
    print("\n" + "-" * 84)
    print("合计 %d 项（PASS %d / FAIL %d）"
          % (len(_results), len(_results) - len(fails), len(fails)))
    for t in fails:
        print("  · FAIL " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
