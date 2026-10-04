# -*- coding: utf-8 -*-
"""P0-1（2026-10-04）：入库不得**跨 LLM 调用持有写事务** —— 行为不变式 + 变异自证。

## 缺陷与实测依据
cProfile 对真实入库（143KB / 270 块）：
```
ingest_document          177.1s
  _gen_llm_summary       175.9s   ← 5 次串行 LLM
    _record_usage        161.1s   ← 函数自身0ms，但里面的 sqlite3.execute 累计 161s
      httpx.post          15.0s   ← 真实网络只有 15s
```
即 **161s 全在等写锁**。机制：270 行 `INSERT document_chunks` 之后**没有 commit**，
事务一路跨到 LLM 摘要结束（叠加 `db_session` 的"请求结束才 commit"）
⇒ 整个入库是**一个跨越全部 LLM 调用的写事务**
⇒ 全库写者被堵到 `busy_timeout`（30s），作业进度上报 / 租约心跳 / token 记账全被堵
（这也是前端进度"卡在 5/80 不动"的直接原因）。

修法：落库后**立刻 commit**，摘要挪到锁外。
**实测 177.1s → 18.4s（9.6x）**，且profile 里锁等待归零（只剩 SSL 读的真实网络时间）。

## 为什么这个缺陷需要门禁（它是"慢"但会被误诊的那一类）
单测测不出来（不接 LLM 时无感），也不该靠人review（代码看着完全正常：
"插完行更新状态，然后生成摘要，最后 commit" —— 没错，只是commit 晚了）。
⇒ 用**结构断言 + 变异自证**把它钉住。

## 四条不变式
T1 **chunks 落库后、下一个 LLM 调用之前必须已有 commit**：
   源码里 `_gen_llm_summary(...)` 之前必须出现 `conn.commit()`。
T2 **LLM 调用不得夹在 chunks INSERT 与其 commit 之间**：
   解析函数体，确认 `INSERT INTO document_chunks` 与 `_gen_llm_summary` 之间有 commit。
T3 **向量索引失效标记（写操作）不得在长事务内**：
   `ChunkVectorIndex.invalidate()` 必须在 commit 之后（它会与检索重建抢写锁）。
T4 **摘要写入独立成短事务**：摘要 UPDATE 后要有 commit，
   保证"摘要失败不连带回滚已落库 chunks"这一语义真正成立。

## 变异自证
M1 删掉 commit（还原修复前）⇒ T1/T2 判红
M2 把 commit 移到摘要之后（事务仍然跨越 LLM）⇒ T2 判红
M3 把 invalidate 放回 INSERT 之后 ⇒ T3 判红
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

INGEST = os.path.join(ROOT, "knowledge_pipeline", "ingest.py")

PASS, FAIL = "PASS", "FAIL"
_results, _verdicts, _detail = [], [], []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    rec = (PASS if ok else FAIL, name, detail)
    if _IN_MUT[0]:
        (_verdicts if name[:2] in ("M1", "M2", "M3") else _detail).append(rec)
    else:
        _results.append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _src():
    return io.open(INGEST, encoding="utf-8").read()


def _js_like_fn_pos(src, fname):
    """返回 (start, end) —— Python 函数体在源码中的位置。

    ⚠️ **不能用"首个非注释行的缩进"当基准**：多行签名
    `def ingest_document(conn, filename: str, ...,\n    uploaded_by...)` 的续行
    缩进比函数体深（实测 20 vs 4）⇒ 基准被定成 20⇒ 函数体第三行就"越界"
    ⇒ 抽出 7 行 ⇒ 断言全红且理由莫须（我第一版就这么错的，与 MEMORY 里
    「抽源码的片段被提前截断」同型）。

    ⇒ 正解：**先跳过签名括号配平**，从`def` 那一行之后的**函数体首行**
       （第一个缩进 > def 缩进的非空、非续行）起算基准。
    """
    m = re.search(r"^def\s+%s\s*\(" % re.escape(fname), src, re.M)
    if not m:
        return None, None
    start = m.start()
    lines = src[start:].splitlines(keepends=True)
    def_indent = len(lines[0]) - len(lines[0].lstrip())
    # 跳过签名：找到形参列表闭合的那个 ')' 所在行
    depth = 0
    body_start = 1
    for i, ln in enumerate(lines):
        for ch in ln:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if depth == 0 and i > 0:
            body_start = i + 1
            break
    # 从 body_start 起找基准缩进（第一个"缩进> def_indent 且不是续行"的行）
    base = None
    for i in range(body_start, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip())
        if ind > def_indent:
            base = ind
            body_start = i
            break
    if base is None:
        return start, start + sum(len(x) for x in lines)
    off = sum(len(x) for x in lines[:body_start])
    end = off
    for ln in lines[body_start:]:
        if ln.strip():
            ind = len(ln) - len(ln.lstrip())
            if ind < base:
                break
        end += len(ln)
    return start, start + end


def t_t1(src):
    print("\n=== T1 摘要调用之前必须已 commit ===")
    s, e = _js_like_fn_pos(src, "ingest_document")
    ok = _rec("T1a 能定位 ingest_document 函数体", bool(e and e > s))
    if not ok:
        return False
    body = src[s:e]
    i_summary = body.find("_gen_llm_summary(")
    ok = _rec("T1b 函数体内有 _gen_llm_summary 调用", i_summary > 0)
    if i_summary < 0:
        return False
    before = body[:i_summary]
    last_commit = before.rfind("conn.commit()")
    i_insert = before.find("INSERT INTO document_chunks")
    ok &= _rec("T1c 摘要之前存在 commit", last_commit > 0)
    ok &= _rec("T1d commit 在 chunks INSERT 之后（锁已释放）",
               last_commit > i_insert, "insert@%s commit@%s" % (i_insert, last_commit))
    return ok


def t_t2(src):
    print("\n=== T2 LLM 调用不得夹在 INSERT 与 commit 之间 ===")
    s, e = _js_like_fn_pos(src, "ingest_document")
    body = src[s:e]
    i_ins = body.find("INSERT INTO document_chunks")
    i_sum = body.find("_gen_llm_summary(")
    seg = body[i_ins:i_sum] if i_ins >= 0 and i_sum > i_ins else ""
    ok = _rec("T2a 能切出 INSERT→摘要 的区段", bool(seg))
    ok &= _rec("T2b 该区段内至少一次 commit（事务边界已断）",
               "conn.commit()" in seg, "区段长度=%d" % len(seg))
    # 反证：区段内不得出现任何 LLM 入口（chat/llm_client/_ask）
    llm_marks = [k for k in ("llm_client.chat(", "_ask(", "chat(", "responses.create(")
                 if k in seg]
    ok &= _rec("T2c INSERT→commit 区段内无 LLM 调用", not llm_marks, str(llm_marks))
    return ok


def t_t3(src):
    print("\n=== T3 向量索引失效标记必须在锁外 ===")
    s, e = _js_like_fn_pos(src, "ingest_document")
    body = src[s:e]
    #⚠️ 只数**真实调用行**，且不能要求"行尾无内容" ——
    #   真实调用那行带了行尾注释（`# 放锁后再标脏…`）⇒ `\s*$` 匹配不到 ⇒ 判"0 次"。
    #   也不能按出现次数数：我修复时在原地留了说明注释，注释里也含这个词。
    # ⇒ 判据：**行首是调用语句**（允许行尾注释），且所在行不是注释行。
    calls = [m.start() for m in
             re.finditer(r"^[ \t]*ChunkVectorIndex\.invalidate\(\)", body, re.M)]
    ok = _rec("T3a 真实调用只有一次（无重复调用）", len(calls) == 1,
              "真实调用 %d 次" % len(calls))
    if not calls:
        return False
    i_inv = calls[0]
    i_sum = body.find("_gen_llm_summary(")
    ok &= _rec("T3b invalidate 在摘要调用之前（持锁阶段已完成）",
               i_inv < i_sum, "inv@%s summary@%s" % (i_inv, i_sum))
    before_inv = body[:i_inv]
    last_commit = before_inv.rfind("conn.commit()")
    ok &= _rec("T3c invalidate 之前已有 commit（已放锁）", last_commit > 0)
    # 反证：import + 调用必须在 commit 之后成对出现
    imp = body.rfind("from vector_index import ChunkVectorIndex", 0, i_inv)
    ok &= _rec("T3d invalidate 的 import 也在 commit 之后", imp > last_commit)
    return ok


def t_t4(src):
    print("\n=== T4 摘要写入独立成短事务 ===")
    s, e = _js_like_fn_pos(src, "ingest_document")
    body = src[s:e]
    i_sum = body.find("_gen_llm_summary(")
    tail = body[i_sum:] if i_sum > 0 else ""
    ok = _rec("T4a 摘要调用之后有 summary 的 UPDATE",
              "UPDATE documents SET summary=" in tail)
    ok &= _rec("T4b 该 UPDATE 之后有 commit（独立短事务）",
               "conn.commit()" in tail,
               "保证摘要失败不连带回滚已落库 chunks")
    return ok


# ══════════════════════════════════════════════════════════════════
def run_mutations():
    print("\n=== 变异自证 ===")
    src = _src()

    # M1：删掉摘要前的 commit（还原成修复前的样子）
    _IN_MUT[0] = True
    try:
        old = """        conn.commit()
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()   # 放锁后再标脏，避免与检索重建抢写锁"""
        assert old in src, "M1 锚点未命中"
        mutated = src.replace(old, """        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()""", 1)
        hit = (not t_t1(mutated)) and (not t_t2(mutated))
    except AssertionError as ex:
        _rec("M1 锚点命中", False, str(ex)[:100]); hit = False
    _rec("M1 删掉 commit（还原修复前）=> T1/T2 判红", hit)
    _IN_MUT[0] = False

    # M2：把 commit 移到摘要之后（事务仍跨越 LLM，语义更隐蔽）
    _IN_MUT[0] = True
    try:
        old2 = """        conn.commit()
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()   # 放锁后再标脏，避免与检索重建抢写锁
        except Exception as e:
            logger.warning("向量索引失效标记失败（不阻断）: %s", e)

        # 7) LLM 真摘要"""
        assert old2 in src, "M2 锚点未命中"
        mutated2 = src.replace(old2, """        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()
        except Exception as e:
            logger.warning("向量索引失效标记失败（不阻断）: %s", e)

        # 7) LLM 真摘要""", 1)
        hit = (not t_t1(mutated2)) or (not t_t2(mutated2))
    except AssertionError as ex:
        _rec("M2 锚点命中", False, str(ex)[:100]); hit = False
    _rec("M2 commit 后移（事务仍跨 LLM）=> T1/T2 判红", hit)
    _IN_MUT[0] = False

    # M3：把 invalidate 放回长事务内
    _IN_MUT[0] = True
    try:
        old3 = """                 json.dumps(normalize_vector(_hyde_vecs[i]), ensure_ascii=False) if _hyde_vecs[i] else "[]"))
        # ⚠️ 2026-10-04（P0-1）"""
        assert old3 in src, "M3 锚点未命中"
        mutated3 = src.replace(old3, """                 json.dumps(normalize_vector(_hyde_vecs[i]), ensure_ascii=False) if _hyde_vecs[i] else "[]"))
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()
        except Exception as e:
            logger.warning("x: %s", e)
        # ⚠️ 2026-10-04（P0-1）""", 1)
        hit = (not t_t3(mutated3))
    except AssertionError as ex:
        _rec("M3 锚点命中", False, str(ex)[:100]); hit = False
    _rec("M3 invalidate 放回事务内 => T3 判红", hit)
    _IN_MUT[0] = False


def main():
    src = _src()
    ok = True
    ok &= t_t1(src)
    ok &= t_t2(src)
    ok &= t_t3(src)
    ok &= t_t4(src)
    run_mutations()

    print("\n" + "=" * 68)
    n_pass = sum(1 for r in _results if r[0] == PASS)
    print("常态断言：%d/%d 通过" % (n_pass, len(_results)))
    for st, name, d in _results:
        if st != PASS:
            print("  [%s] %s  ← %s" % (st, name, d))
    vfail = [r for r in _verdicts if r[0] != PASS]
    print("变异自证：%d/%d 条按预期判红" % (len(_verdicts) - len(vfail), len(_verdicts)))
    for st, name, d in _verdicts:
        print("  [%s] %s%s" % (st, name, ("  ← " + d) if d else ""))
    print("-" * 68)
    print("变异期中间断言（红=预期）：")
    for st, name, d in _detail:
        print("  [%s] %s%s" % (st, name, ("  ← " + d) if d else ""))
    print("=" * 68)
    allok = (n_pass == len(_results)) and not vfail and bool(_verdicts)
    if not allok:
        print("结论：门禁未通过")
        return 1
    print("结论：全部通过（常态 %d 条 + 变异 %d 条）" % (len(_results), len(_verdicts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())