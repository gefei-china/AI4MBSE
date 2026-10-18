"""verify_partial_dedup —— 中断固化「同一轮不重复落库」的行为级验证（2026-10-10）。

## 为什么重写（而不是沿用 verify_dedup_and_finalize_hint.py 的第③组）

旧脚本第③组是**源码字符串断言**（`"_lc_body == content.strip()" in rsrc`），
它只证明「代码里写了这句话」，**不证明这句话被执行到了** ——
于是出现了一个「判据全绿、产品照旧双写」的局面。

真实缺陷（settrace 取证，非推测）：
```python
_last = conn.execute("SELECT role, content FROM messages ...").fetchone()   # ← 漏了 id 列
...
return {"ok": True, "id": _last["id"], "deduped": True}   # ← IndexError('No item with that key')
except Exception:
    pass      # ← 静默吞掉 ⇒ 直接落到 INSERT ⇒ 去重从未生效
```
真库证据（会话 9336）：user 1 条 + assistant **2 条**（id=3887 len=2185 /
id=3888 len=2206，剥离「⏹ 已停止生成」后缀后**逐字相同**）。
⇒ 全库同型重复 **19 对**，无一例外。

**所以判据必须打「真实函数 + 真实连接」的执行路径**，源码 grep 抓不到这个 bug。
（另：`_persist_partial_stream` 用的是裸 `return`，不取 `_last["id"]`，故不受此 bug 影响。）

## 判据（全部走真实路由函数体 `RC.commit_partial_message`）
D1 逐字相同（短文本，<200字）⇒ 1 条，响应带 deduped
D2 逐字相同（长文本，>200字）⇒ 1 条
D3 已被固化过的那条带后缀，再固化同内容 ⇒ 1 条（marker 分支）
D4 **内容不同必须照常写 2 条**（反空转：防"去重太激进把内容丢了"）
D5 落库摘要去中断后缀后与前一条**逐字相同**（直接量最终数据，不看返回值）
M1 变异：拿掉 SELECT 的 id 列 ⇒ D1/D2/D3 必须 FAIL（证明判据有牙齿）

## 用法（必须用项目 venv）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_partial_dedup.py
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ROUTER = os.path.join(_ROOT, "routers", "conversations.py")

SHORT = "短内容重复校验ABC"                      # < 200 字
LONG = "这是用于验证去重逻辑的长文本。" * 40        # 600 字，> 200
STOP_SUFFIX = "\n\n> ⏹ 已停止生成（以上为已生成内容）"

_ANCHOR = "SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1"
_MUTATED = "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1"

_WORKER = r'''
import os, sys, sqlite3, tempfile
ROOT = r"@@ROOT@@"
sys.path.insert(0, ROOT)

# 整库复制到临时文件 → MBSE_DB_PATH 重定向 ⇒ 零污染生产库
src = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
tmpdb = tempfile.mktemp(suffix=".db")
dst = sqlite3.connect(tmpdb); src.backup(dst); dst.close(); src.close()

import core.config as cfg; cfg.DB_PATH = tmpdb
import database.connection as dc; dc.DB_PATH = tmpdb
from database import get_db
import routers.conversations as RC

SHORT = @@SHORT@@
LONG = @@LONG@@
SUFFIX = @@SUFFIX@@

def strip_sfx(t):
    for m in ("\n\n> \u23f9 已停止生成", "\n\n> \u26a0\ufe0f 输出因网络错误中断", "\n\n> \u26a0\ufe0f 输出中断"):
        if m in (t or ""):
            return t.split(m, 1)[0].strip()
    return (t or "").strip()

def case(tag, seeded, incoming):
    conn = get_db()
    cid = conn.execute("INSERT INTO conversations (title,intent,user_id,project_id)"
                       " VALUES ('verify','chat',1,'')").lastrowid
    if seeded is not None:
        conn.execute("INSERT INTO messages (conversation_id,role,content,msg_type,card_data)"
                     " VALUES (?,?,?,'card_candidates','{}')", (cid, "assistant", seeded))
    conn.commit()
    # ★ 2026-10-18：user 必须带 id —— B1-9 归属校验（2026-10-13）按
    #   `row.user_id == user.id` 判可见性；此前只传 {"name": "verify"}（无 id）
    #   ⇒ 1 == 0 不可见 ⇒ _own_conv 404「会话不存在」⇒ 整门禁 Traceback。
    #   产品行为是对的（归属不一致就该 404），是门禁载荷该对齐契约。
    r = RC.commit_partial_message(cid, {"content": incoming, "stopped": True},
                                 conn=conn, user={"id": 1, "name": "verify"})
    conn.commit()
    rows = conn.execute("SELECT id, content FROM messages"
                        " WHERE conversation_id=? AND role='assistant' ORDER BY id",
                        (cid,)).fetchall()
    conn.close()
    bodies = [strip_sfx(x["content"]) for x in rows]
    same_as_prev = len(bodies) >= 2 and bodies[-1] == bodies[-2]
    print("CASE|%s|%d|%s|%s" % (tag, len(rows), r.get("deduped") is True, same_as_prev))

case("short", SHORT, SHORT)
case("long",  LONG,  LONG)
case("marker", LONG + SUFFIX, LONG)
case("different", "别的内容" + "Y" * 500, LONG)
os.unlink(tmpdb)
'''


def _run_worker(root: str) -> dict:
    script = (_WORKER.replace("@@ROOT@@", root)
                     .replace("@@SHORT@@", repr(SHORT))
                     .replace("@@LONG@@", repr(LONG))
                     .replace("@@SUFFIX@@", repr(STOP_SUFFIX)))
    r = subprocess.run([os.path.join(root, ".venv", "Scripts", "python.exe"), "-X", "utf8", "-c", script],
                       capture_output=True, text=True, cwd=root, timeout=600)
    out = {}
    for line in (r.stdout or "").splitlines():
        if line.startswith("CASE|"):
            _, tag, n, dedup, same = line.split("|")
            out[tag] = {"n": int(n), "deduped": dedup == "True", "same_as_prev": same == "True"}
    if not out:
        print(r.stdout or "")
        print(r.stderr or "")
        raise SystemExit("工作进程未产出 CASE 行（rc=%s）" % r.returncode)
    return out


def main() -> int:
    print("=" * 74)
    print("判据 · 中断固化不重复落库（2026-10-10 settrace 取证：SELECT 漏 id 列 + 静默 except）")
    print("=" * 74)

    base = _run_worker(_ROOT)
    print("\n基线（真实路由函数 + 真实连接）:")
    for tag, v in base.items():
        print("   %-10s 条数=%d deduped=%s 逐字重复=%s" % (tag, v["n"], v["deduped"], v["same_as_prev"]))

    failures: list[str] = []

    def check(name: str, passed: bool) -> None:
        print("[%s] %s" % ("OK  " if passed else "FAIL", name))
        if not passed:
            failures.append(name)

    print("\n① 逐字相同（短文本 <200字）⇒ 只1 条且带 deduped")
    check("D1 短文本去重生效", base["short"]["n"] == 1 and base["short"]["deduped"])
    check("D1b 最终数据无重复正文", not base["short"]["same_as_prev"])

    print("\n② 逐字相同（长文本 >200字）⇒ 只 1 条")
    check("D2 长文本去重生效", base["long"]["n"] == 1 and base["long"]["deduped"])

    print("\n③ 库里已是带后缀的固化消息，再固化同内容 ⇒ 仍只 1 条")
    check("D3 marker 分支去重生效", base["marker"]["n"] == 1 and base["marker"]["deduped"])

    print("\n④ 反空转：内容不同**必须**照常写 2 条（防「去重太激进把内容丢了」）")
    check("D4 不同内容不被误去重", base["different"]["n"] == 2)

    # ── 变异自证：把 id 列拿掉（= 还原真实缺陷）⇒ D1/D2/D3 必须 FAIL ──────────
    print("\n[变异自证] 拿掉 SELECT 的 id 列（还原缺陷）⇒ D1/D2/D3 必须翻")
    with open(ROUTER, encoding="utf-8") as f:
        src = f.read()
    if _ANCHOR not in src:
        print("锚点未找到：%s" % _ANCHOR)
        return 1
    try:
        with open(ROUTER, "w", encoding="utf-8") as f:
            f.write(src.replace(_ANCHOR, _MUTATED, 1))
        mut = _run_worker(_ROOT)
    finally:
        with open(ROUTER, "w", encoding="utf-8") as f:
            f.write(src)
        with open(ROUTER, encoding="utf-8") as f:
            assert f.read() == src, "回滚失败！routers/conversations.py 未逐字还原"
    print("   变异体:", {k: v["n"] for k, v in mut.items()})
    newly_red = [t for t in ("short", "long", "marker")
                 if base[t]["n"] == 1 and mut[t]["n"] != 1]
    check("M1 变异后 D1/D2/D3 判红（判据有牙齿）", len(newly_red) == 3)
    check("M1b 回滚后基线复原", _run_worker(_ROOT) == base)

    print("\n" + "=" * 74)
    print("结论：%s" % ("全部通过" if not failures else "存在未通过项：%s" % failures))
    print("=" * 74)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())