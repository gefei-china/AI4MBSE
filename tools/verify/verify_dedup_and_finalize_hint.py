"""verify_dedup_and_finalize_hint — 判据：① 重复落库 ② 收尾提示区分上限（2026-10-10）。

## 米爸实测发现的两个问题（会话 9335）

### 问题 1：AI 输出出现两个片段，内容几乎完全一样
真库证据：
    id=3884 assistant len=2832 md5=1a22c20c19e83b16
    id=3885 assistant len=2853 md5=26423e9117e9c0ec
差异只在末尾多了 21 个字符：`\n\n> ⏹ 已停止生成（以上为已生成内容）`
⇒ 3885 = 3884 + 中断后缀，**b.startswith(a) 为 True**。

**根因**：正常完成时后端已落一条（无后缀）；用户随后点"停止"
⇒ 触发 `GeneratorExit` ⇒ 走 `_persist_partial_stream` **又写一条**。
原去重只认「以上为已生成内容」这个后缀，而**正常落库那条没有后缀** ⇒ 判不出来。

⇒ 修法：**按内容比对**（后一条去掉后缀 == 前一条 ⇒ 同一内容写了���遍）。
⚠️ **两条写入路径都要修**：后端 `_persist_partial_stream` 与
   前端停止路径 `/messages/partial`（`routers/conversations.py`）——
   只修一边，另一边仍会双写。

### 问题 2：「本轮工具调用已达上限」语义不明
本工程实际有**四道彼此独立**的闸，文案混为一谈：
| 闸 | 维度 | 默认 |
|---|---|---|
| `MAX_TOOLS_PER_ROUND` | **单轮内工具并发** | 3 |
| `max_tool_rounds` | **ReAct 轮次** | 12 |
| `max_failed_rounds` | **失败轮**（成功轮不计数） | 3 |
| token 预算 | 累计 token | 60000 |
⇒ 答案：**既不是单个工具上限，也不是所有工具调用之和**。

## 判据
① 去重：后端固化 —— 正常落库 + 停止固化 ⇒ **只1 条**
② 去重：前端固化端点 —— 同上 ⇒ **只 1 条**
③ 去重：不同内容**不能**被误去重（否则丢内容）
④ 收尾提示：`reason=rounds/failed/budget` 三种文案**各不相同**且说清是哪个上限
⑤ 不传 `reason` ⇒ 逐字等于原 `FINALIZE_HINT`（**向后兼容**）
⑥ 变异自证（去重逻辑摘掉 ⇒ 判红）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_dedup_and_finalize_hint.py
"""
from __future__ import annotations
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
ROUTER = os.path.join(_ROOT, "routers", "conversations.py")
PROTOCOL = os.path.join(_ROOT, "agent", "loop_protocol.py")

SUFFIX = "\n\n> ⏹ 已停止生成（以上为已生成内容）"
LONG = "这是用于验证去重逻辑的长文本。" * 40   # >200 字，触发前缀判据


def _run_probe(db: str, seed: str, write: str) -> int:
    """在**临时库**上跑一次固化，返回写入后的 messages 行数。

    ⚠️⚠️ **踩过两次坑（差点污染真库）**：
      ① `stream.py` 的 `db_conn` 是 `from .common import *` **复制** 的一份绑定，
         替换 `common.db_conn` **无效** —— 实测探针打到了**真库**。
      ② 正确绑定点是 **`agent.pipeline_parts.stream.db_conn`**（该模块自己的全局名）。
      ⇒ 现探针一律在**临时库**上跑，且注入点为 `stream.db_conn`。
      ⇒ 事后已核查真库：684 条消息、探针文本残留 0 条（未污染）。
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    # 在**子进程**里用真实 Row 语义注入（见 _make_conn_cls 的坑说明）
    conn_src = (
        "import sqlite3\n"
        "class _FC:\n"
        "    def __enter__(self):\n"
        "        self.c=sqlite3.connect(r'" + db + "'); self.c.row_factory=sqlite3.Row; return self.c\n"
        "    def __exit__(self,*a):\n"
        "        self.c.commit(); self.c.close(); return False\n"
        "    def execute(self,*a,**k): return self.c.execute(*a,**k)\n"
    )
    script = (
        "import sqlite3\n"
        "import agent.pipeline_parts.stream as SM\n"
        "from agent.pipeline_parts.stream import StreamMixin\n"
        + conn_src +
        "SM.db_conn = lambda: _FC()\n"      # ★ 该模块自己的全局绑定
        "m = StreamMixin()\n"
        "m._persist_partial_stream(1, " + repr(write) + ", 'stop', dry_run=False)\n"
        "c=sqlite3.connect(r'" + db + "')\n"
        "print('PROBE_ROWS', c.execute('SELECT COUNT(*) FROM messages').fetchone()[0])\n"
    )
    r = _sp.run([_sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, text=True, timeout=180, cwd=_ROOT)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("PROBE_ROWS"):
            return int(ln.split()[1])
    return -1


def _make_conn_cls(db: str):
    """造一个与真实 `db_conn` **行为一致**的连接工厂。

    ⚠️⚠️ **第三个坑（这个坑让判据假红了两轮）**：
       真实 `db_conn` 返回 `sqlite3.Row`（支持 `row["role"]` 字符串下标），
       而我最初手写的 `execute` 直接返回**裸 cursor** ⇒ `fetchone()` 得到 tuple
       ⇒ 生产代码里 `last["role"]` 抛 `TypeError`
       ⇒ **被 `except Exception: pass` 静默吞掉** ⇒ 行数不增
       ⇒ 判据报"只有 1 条"，看起来像去重逻辑太激进，**实际是探针的 Row 语义不对**。
    ⇒ 这里必须 `row_factory = sqlite3.Row`。
    ⇒ **通则：mock 一个 DB 时，凡是用到 `row["col"]` 的代码，
       mock 就必须提供 Row 语义**，否则异常会被 `except: pass` 吃掉，
       现象是"行为异常"而不是"报错"，极难定位。
    """
    import sqlite3

    class _C:
        def __enter__(self):
            self.c = sqlite3.connect(db)
            self.c.row_factory = sqlite3.Row      # ★ 与真实 db_conn 一致
            return self.c

        def __exit__(self, *a):
            self.c.commit()
            self.c.close()
            return False

        def execute(self, *a, **k):
            return self.c.execute(*a, **k)
    return _C


def _make_db(seed: str) -> str:
    """建临时库并塞入一条种子消息。"""
    import os as _os
    import sqlite3
    import tempfile
    path = _os.path.join(mkdtemp(prefix="dedup_probe_"), "t.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " conversation_id INT, role TEXT, content TEXT,"
                 " msg_type TEXT, card_data TEXT)")
    conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, updated_at TEXT)")
    conn.execute("INSERT INTO conversations (id) VALUES (1)")
    if seed:
        conn.execute("INSERT INTO messages (conversation_id,role,content,msg_type,card_data)"
                     " VALUES (1,'assistant',?,'text','{}')", (seed,))
    conn.commit()
    conn.close()
    return path


def main() -> int:
    print("=" * 74)
    print("判据 · 重复落库去重 + 收尾提示区分上限（米爸 2026-10-10 实测反馈）")
    print("=" * 74)
    ok = True

    print("\n① 后端固化：正常落库 + 停止固化 ⇒ 应只有 1 条")
    n1 = _run_probe(_make_db(LONG), "", LONG)   # 先落LONG，再固化 LONG
    for name, passed in [
        (f"实测 {n1} 条（期望 1）", n1 == 1),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n② 反向：内容不同 ⇒ 必须照常写（防误去重丢内容）")
    other = "别的内容" + "Y" * 500
    n2 = _run_probe(_make_db(other), "", LONG)
    for name, passed in [
        (f"实测 {n2} 条（期望 2）", n2 == 2),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n③ 两条写入路径都修了（只修一边仍会双写）")
    # ⚠️2026-10-10 修正：原判据是**源码字符串断言**（`"_lc_body == content.strip()" in rsrc`），
    #   它只证明「代码里写了这句话」，**不证明这句话被执行到了** ⇒ 出现「判据全绿、产品照旧双写」。
    #   真实缺陷（settrace 取证）：partial 端点的 SELECT **漏了 id 列**，
    #   而 return 要取 `_last["id"]` ⇒ IndexError 被 `except: pass` 吞掉 ⇒ 去重从未生效。
    #   ⇒ 行为级判据改由 tools/verify/verify_partial_dedup.py 承担（走真实路由函数 + 真实连接）。
    #   这里只保留「两条路径都在源码里有去重代码」的**弱**断言，并显式标注它抓不到执行期缺陷。
    with open(STREAM, encoding="utf-8") as f:
        src = f.read()
    with open(ROUTER, encoding="utf-8") as f:
        rsrc = f.read()
    for name, passed, weak in [
        ("后端 `_persist_partial_stream` 按内容去重（弱：源码级）",
         "_lc.strip() == text.strip()" in src, True),
        ("★ 前端 `/messages/partial` 按内容去重（弱：源码级）",
         "_lc_body == content.strip()" in rsrc, True),
        ("★ partial 端点 SELECT **必须带 id 列**（否则 `_last[\"id\"]` 抛 IndexError 被静默吞掉）",
         "SELECT id, role, content FROM messages" in rsrc, False),
        ("★ 查重失败必须留痕（不得再 `except: pass`）",
         "except Exception as _dedup_err" in rsrc, False),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}" + ("  ← 仅源码级，抓不到执行期缺陷" if weak else ""))
        ok = ok and passed
    print("       → 行为级判据（真实执行）见 verify_partial_dedup.py，本组不可替代它")

    print("\n④ 收尾提示区分四种上限")
    from agent.loop_protocol import finalize_messages, FINALIZE_HINT
    base = [{"role": "user", "content": "x"}]
    hints = {}
    for reason in ("rounds", "failed", "budget"):
        hints[reason] = finalize_messages(base, reason=reason, used=2, limit=12)[-1]["content"]
    for name, passed in [
        (f"三种原因文案互不相同（轮次/失败/token）",
         len({hints["rounds"], hints["failed"], hints["budget"]}) == 3),
        ("轮次文案点明「非单个工具上限」", "非单个工具上限" in hints["rounds"]),
        ("失败轮文案点明「成功轮不计数」", "成功轮不计数" in hints["failed"]),
        ("token 预算文案独立", "token 预算" in hints["budget"]),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n⑤ 向后兼容：不传 reason ⇒ 逐字等于原 FINALIZE_HINT")
    got = finalize_messages(base)[-1]["content"]
    for name, passed in [
        ("不传 reason 时输出与改动前逐字相同", got == FINALIZE_HINT),
    ]:
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    print("\n" + "=" * 74)
    print(f"判据结论：{'全部通过' if ok else '存在未通过项'}")
    print("=" * 74)
    if not ok:
        return 1

    # ── 变异自证：摘掉**全部**去重判据 ⇒ 应判红 ──────────────────────
    # ⚠️ 只摘精确判据（`_lc == text`）**不够**：前缀判据仍会拦住
    #   （同一条消息去掉后缀后与 `text` 完全相同 ⇒ 也满足 startswith）。
    #   ⇒ 与"多条件守卫"同理：变异要抽掉**全部**守卫，才等于"守卫真的没了"。
    print("\n[变异自证] 摘掉全部去重判据，应判红")
    with open(STREAM, encoding="utf-8") as f:
        src = f.read()
    a1 = "if _lc.strip() and _lc.strip() == text.strip():"
    a2 = ("if text.strip() and text.strip().startswith(_lc.strip()[:200]) \\\n"
          "                            and len(_lc.strip()) >= 200:")
    assert a1 in src, "锚点1 未找到"
    assert a2 in src, "锚点2 未找到"
    mutated = src.replace(a1, "if False:  # 变异", 1).replace(a2, "if False:  # 变异", 1)
    try:
        with open(STREAM, "w", encoding="utf-8") as f:
            f.write(mutated)
        dm = _run_probe(_make_db(LONG), "", LONG)
        red = dm != 1
        print(f"[变异自证] 实测 {dm} 条 ⇒ "
              f"{'已变红（去重判据有效）' if red else '**仍是绿 ⇒ 判据空转**'}")
        ok = ok and red
    finally:
        with open(STREAM, "w", encoding="utf-8") as f:
            f.write(src)
    with open(STREAM, encoding="utf-8") as f:
        assert f.read() == src, "回滚失败！stream.py 未逐字还原"
    print("[OK  ] 回滚校验：stream.py 已逐字还原")

    print("=" * 74)
    print(f"最终结论：{'全部通过' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
