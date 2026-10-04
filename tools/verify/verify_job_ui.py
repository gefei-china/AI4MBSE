# -*- coding: utf-8 -*-
"""P0-C 第三步：前端作业轮询接线 —— 静态哨兵 + 逻辑等价性 + 变异自证。

## 为什么需要这个门禁（前端"改了但没接上"是静默失败）
后端队列与前端轮询分属两个进程/两个语言层，中间只有 HTTP 一个契约。
任何一侧改了字段名/status 口径，另一侧**不会报错**，只会静默退化：
  - 后端把 `status='succeeded'` 写成 `done` ⇒ 前端轮询永不终止（挂到超时）；
  - 前端把完成判据从 `status` 改成 `progress>= 100` ⇒ handler 只报 80% 时
    用户永远等不到"完成"，但控制台**一条错都没有**。
⇒ 必须有一个门禁**在提交前**把两侧的口径钉在一起。

## 五组不变式
F1 **前端有轮询助手且只有一处**：pollJob / submitAndPoll / jobStatusText 存在于
   01-core.js（跨模块共享），**不许**在20-docs.js 里另写一份。
F2 **完成判据是 status 不是 progress**：pollJob 的终止条件里必须出现 status 终态
   判定，且**不得**出现 `progress >= 100` 之类的进度判据。
F3 **前端消费的后端字段与后端真实产出一致**：把 routers/jobs.py 里实际下发的
   字段名（status/terminal/progress/stage/result）逐个在 01-core.js 里找到引用。
   ⚠️ 这一组是**跨语言契约哨兵** —— 它俩本来谁也不认识谁。
F4 **`queued` 状态不能被显示成"正式入库"**：docDerivedState 必须单独判 queued，
   且必须排在"落到兜底分支"之前（否则 queued 会被算成 committed，比报错更坏：
   用户以为文档已入库）。
F5 **超时不是失败**：pollJob 超时必须返回 timedOut 标记，且前端分支据此提示
   "仍在后台执行"而不是报失败（误报失败会诱导用户重复提交）。

## 变异自证
M1 删掉 docDerivedState 里的 queued 分支⇒ F4 判红
M2 把 pollJob 的终止条件改成 `progress>=100` ⇒ F2 判红
M3 让 pollJob 超时返回 error 而不是 timedOut ⇒ F5 判红
M4 前端字段引用改成后端不存在的名字 ⇒ F3 判红
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)     # F3b 要 import knowledge_pipeline.ingest 取真实返回字段
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CORE_JS = os.path.join(ROOT, "static", "js", "mods", "01-core.js")
DOCS_JS = os.path.join(ROOT, "static", "js", "mods", "20-docs.js")
JOBS_PY = os.path.join(ROOT, "routers", "jobs.py")
SCHEMA_PY = os.path.join(ROOT, "database", "schema.py")

PASS, FAIL = "PASS", "FAIL"
_results, _verdicts, _detail = [], [], []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    rec = (PASS if ok else FAIL, name, detail)
    if _IN_MUT[0]:
        (_verdicts if name[:2] in ("M1", "M2", "M3", "M4") else _detail).append(rec)
    else:
        _results.append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _js_fn(src, name):
    """按**花括号配平**提取 JS 函数体（不是 `.*?\n}` 懒惰匹配）。

    ⚠️ 我第一版用 `async function pollJob\\(.*?\\n\\}`，结果在函数内第一个
    `}`（catch 块的收尾）就截断了 ⇒ 断言"F2b 终止条件判 done"读的是**半个函数**，
    恒假 ⇒ 门禁红了但代码是对的。
    这与 MEMORY「抽表达式的正则会被函数体内部分号提前截断」是同一型坑，
    在跨语言门禁里表现为"断言莫须红"⇒ 必须按括号配平而非按行截断。
    """
    m = re.search(r"(?:async\s+)?function\s+%s\s*\(" % re.escape(name), src)
    if not m:
        return None
    # 从参数列表之后开始找函数体首括号。
    # ⚠️ **不能**用 `src.index("{", m.end()-1)`：pollJob 的形参是解构默认值
    # `({intervalMs = 2, maxMs = …} = {})` ⇒ 那里有一个**空对象字面量 `{}`**，
    # 直接 index 会抓到它 ⇒ 抽出 2 个字符 ⇒ 断言恒假（我第一版就踩了，
    # 表现是"F2b found=False"这种莫须红）。
    # ⇒ 跳过整个形参列表（按圆括号配平），再取下一个 `{`。
    i = m.end() - 1
    pd, j, instr, quote = 0, i, False, ""
    while j < len(src):
        c = src[j]
        if instr:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                instr = False
        elif c in "\"'`":
            instr, quote = True, c
        elif c == "(":
            pd += 1
        elif c == ")":
            pd -= 1
            if pd == 0:
                break
        j += 1
    i = src.find("{", j)
    if i < 0:
        return None
    depth, j, instr, quote = 0, i, False, ""
    while j < len(src):
        c = src[j]
        if instr:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                instr = False
        elif c in "\"'`":
            instr, quote = True, c
        elif c == "/" and src[j:j + 2] == "//":
            k = src.find("\n", j)
            j = len(src) if k < 0 else k
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return None


def _src(path):
    return io.open(path, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════
def t_f1(core, docs):
    print("\n=== F1 轮询助手存在且只有一处 ===")
    ok = _rec("F1a pollJob 定义在 01-core.js", "function pollJob(" in core)
    ok &= _rec("F1b submitAndPoll 在 01-core.js", "function submitAndPoll(" in core)
    ok &= _rec("F1c jobStatusText 在 01-core.js", "function jobStatusText(" in core)
    # 反证：20-docs.js 不得再自己实现一份
    dup = [n for n in ("function pollJob(", "function submitAndPoll(", "function jobStatusText(")
           if n in docs]
    ok &= _rec("F1d 20-docs.js 未重复实现（漂移来源）", not dup, str(dup))
    # 20-docs.js 必须**用**它（而不是自己 fetch轮询）
    ok &= _rec("F1e 20-docs.js 真的调用了 submitAndPoll/pollJob",
               ("submitAndPoll(" in docs) or ("pollJob(" in docs))
    return ok


def t_f2(core):
    print("\n=== F2 完成判据是 status 不是 progress ===")
    body = _js_fn(core, "pollJob")
    ok = _rec("F2a 能按括号配平提取 pollJob 函数体", bool(body))
    if not body:
        return False
    ok &= _rec("F2b 终止条件判 status==='done'", "st === 'done'" in body
               or 'status === "done"' in body,
               "found=%s" % ("st === 'done'" in body))
    ok &= _rec("F2c 终止条件判 failed/canceled",
               "'failed'" in body and "'canceled'" in body)
    # ⚠️ 反证：progress 不得参与终止判定
    bad = re.findall(r"progress\s*>=\s*\d+|progress\s*===?\s*\d+", body)
    ok &= _rec("F2d 未用 progress 阈值当完成判据", not bad, str(bad))
    # progress 只允许出现在传给 onTick 的上下文里（画条由业务侧onTick 负责）
    uses = re.findall(r"job\.progress", body)
    ok &= _rec("F2e progress 未在终止判定路径上被读取（只透传给 onTick）",
               len(uses) == 0, "job.progress 出现 %d 次" % len(uses))
    ok &= _rec("F2f 全局注释已声明 progress 仅供展示", "只用来画条" in core
               or "仅供展示" in core)
    return ok


def t_f3(core, docs, jobs_py, schema_py):
    print("\n=== F3 前后端字段契约（跨语言哨兵）===")
    # 后端真实下发的字段 = job_jobs 表列（schema.py）+ get_job 的派生字段（jobs.py）
    tbl = re.search(r"CREATE TABLE IF NOT EXISTS job_jobs \((.*?)\n    \)", schema_py, re.S)
    tbl_cols = set(re.findall(r"^\s*([a-z_]+)\s+(?:TEXT|INTEGER|REAL)", tbl.group(1), re.M)) \
        if tbl else set()
    derived = set(re.findall(r'out\["([a-z_]+)"\]', jobs_py)) | \
        set(re.findall(r'out\["([a-z_]+)"\]', jobs_py))
    py_fields = tbl_cols | derived | {"ok", "error", "detail", "hint", "poll"}
    # 前端读取 job.* 的位置有两处：pollJob 内部（status/error）+ 业务侧 onTick
    # （progress/stage/result）—— 只扫 pollJob 会误判"progress 没被消费"。
    # ⚠️ 我第一版只扫 pollJob ⇒ F3 对 progress/stage/result 恒红，
    #   而代码是对的（这三个字段由 20-docs.js 的 onTick 消费，画条/渲染要用）。
    js_read = set(re.findall(r"job\.([a-z_]+)", core)) | \
        set(re.findall(r"(?:job|r\.job|res\.job)\.([a-z_]+)", docs))
    # ⚠️ `rr.*` / `r.job.result.*` 是**嵌套在 result 里**的字段（作业返回值），
    #   不是作业对象顶层字段 ⇒ 必须单独与「作业结果的字段源」比对，
    #   混进顶层字段集会让"防幻觉"断言误报（我第一版就这么错的）。
    result_fields = set(re.findall(r"rr\.([a-z_]+)", docs)) | \
        set(re.findall(r"auto_meta\.([a-z_]+)", docs))
    ok = True
    # ① 防幻觉（作业对象顶层）
    unknown = sorted(f for f in js_read
                     if f not in py_fields and f not in ("id", "payload"))
    ok &= _rec("F3a 前端未引用作业对象上不存在的字段（防幻觉）", not unknown,
               "未知=%s" % unknown)
    # ② 关键字段两端都有
    for f in ("status", "error", "progress", "stage", "result"):
        py_has = (f in tbl_cols) or ('"%s"' % f in jobs_py)
        js_has = f in js_read
        ok &= _rec("F3 字段 %s：后端下发 + 前端消费" % (f),
                   py_has and js_has, "py=%s js=%s" % (py_has, js_has))
    # ③ 作业结果里的字段必须真实存在于入库管道返回值里
    #   （parse_status/chunk_count/embed_version 来自 knowledge_pipeline.ingest）
    import knowledge_pipeline.ingest as _kp
    pipe_fields = set()
    for fn in (_kp.ingest_document, _kp.ingest_upload_document,
               _kp.run_staged_ingest):
        try:
            import inspect
            src_fn = inspect.getsource(fn)
            pipe_fields |= set(re.findall(r'"([a-z_]+)"\s*:', src_fn))
        except Exception:
            pass
    bad_res = sorted(f for f in result_fields
                     if f not in pipe_fields and f not in py_fields)
    ok &= _rec("F3b 前端从作业 result 读的字段在入库管道返回值里真实存在",
               not bad_res, "可疑=%s" % bad_res)
    # ④ 前后端作业路径一致
    ok &= _rec("F3c 前后端作业路径一致",
               "'/api/jobs/'" in core and '"/api/jobs/{job_id}"' in jobs_py)
    # ④ 前端不得自己拼后端不存在的状态字面量
    sts = set(re.findall(r"st === '([a-z]+)'", core)) | \
        set(re.findall(r"status === '([a-z]+)'", core))
    py_sts = {"queued", "running", "done", "failed", "canceled"}
    bad_st = sorted(s for s in sts if s not in py_sts)
    ok &= _rec("F3c 前端未使用后端不存在的状态字面量", not bad_st,
               "前端=%s 后端=%s" % (sorted(sts), sorted(py_sts)))
    return ok


def t_f4(docs):
    print("\n=== F4 queued 不得被显示成正式入库 ===")
    body = _js_fn(docs, "docDerivedState")
    ok = _rec("F4a 能按括号配平提取 docDerivedState", bool(body))
    if not body:
        return False
    has_q = "'queued'" in body
    ok &= _rec("F4b 显式判 queued", has_q)
    # 关键顺序：queued 分支必须**在** return 'committed' 之前
    idx_q = body.find("'queued'")
    idx_c = body.rfind("return 'committed'")
    ok &= _rec("F4c queued 分支排在 committed 兜底之前",
               idx_q > 0 and idx_c > idx_q,
               "queued@%s committed@%s" % (idx_q, idx_c))
    ok &= _rec("F4d DOC_STATES 含 queued 条目", "queued:" in docs)
    ok &= _rec("F4e queued 档有中文 label",
               re.search(r"queued:\s*\{[^}]*label\s*:\s*'排队中'", docs) is not None)
    return ok


def t_f5(core, docs):
    print("\n=== F5 超时不是失败 ===")
    body = _js_fn(core, "pollJob") or ""
    ok = _rec("F5a pollJob 返回 timedOut 标记", "timedOut" in body)
    # 前端分支必须区分超时与失败
    ok &= _rec("F5b 20-docs.js 有 timedOut 分支", "timedOut" in docs)
    ok &= _rec("F5c 超时文案指向「后台仍在执行」",
               "后台仍在执行" in docs or "仍在后台执行" in docs)
    # 失败分支必须存在（否则所有异常都被当成"还在跑"）
    ok &= _rec("F5d 失败分支独立存在（读 job.error）",
               "job.error" in body or "job && job.error" in docs)
    # 超时返回值必须带 timedOut:true（而不是 false）
    ok &= _rec("F5e 超时返回 timedOut:true",
               re.search(r"timedOut\s*:\s*true", body) is not None)
    return ok


# ══════════════════════════════════════════════════════════════════
# 变异自证
# ══════════════════════════════════════════════════════════════════
def run_mutations():
    print("\n=== 变异自证 ===")
    core = _src(CORE_JS)
    docs = _src(DOCS_JS)
    jobs = _src(JOBS_PY)
    schema = _src(SCHEMA_PY)

    # M1：删掉 docDerivedState 的 queued 分支
    _IN_MUT[0] = True
    try:
        m = re.search(r"  if\(d\.parse_status === 'queued'\) return 'queued';\n", docs)
        mutated_docs = docs.replace(m.group(0), "") if m else docs
        assert mutated_docs != docs, "M1 锚点未命中"
        hit = (not t_f4(mutated_docs))
    except AssertionError as e:
        _rec("M1 锚点命中", False, str(e)); hit = False
    _rec("M1 删 queued 分支 => F4 判红", hit)
    _IN_MUT[0] = False

    # M2：把完成判据改成 progress 达到阈值
    _IN_MUT[0] = True
    try:
        old = "if (st === 'done')  return {ok:true,  job, timedOut:false};"
        assert old in core, "M2 锚点未命中"
        mutated = core.replace(
            old, "if ((job.progress||0) >= 100) return {ok:true, job, timedOut:false};", 1)
        hit = (not t_f2(mutated))
    except AssertionError as e:
        _rec("M2 锚点命中", False, str(e)); hit = False
    _rec("M2 改用 progress 判完成 => F2 判红", hit)
    _IN_MUT[0] = False

    # M3：超时返回 error 而不是 timedOut
    _IN_MUT[0] = True
    try:
        old = "return {ok:false, job:{status:'queued', id:jobId}, timedOut:true, label};"
        assert old in core, "M3 锚点未命中"
        mutated = core.replace(
            old, "return {ok:false, job:{status:'failed', error:'timeout'}, timedOut:false};", 1)
        hit = (not t_f5(mutated, docs))
    except AssertionError as e:
        _rec("M3 锚点命中", False, str(e)); hit = False
    _rec("M3 超时报失败 => F5 判红", hit)
    _IN_MUT[0] = False

    # M4：前端引用后端不存在的字段（幻觉字段名）
    _IN_MUT[0] = True
    try:
        old = "const st = job && job.status;"
        assert old in core, "M4 锚点未命中"
        mutated = core.replace(old, old + "\n    if (job.isFinished) return {ok:true, job, timedOut:false};", 1)
        hit = (not t_f3(mutated, docs, jobs, schema))
    except AssertionError as e:
        _rec("M4 锚点命中", False, str(e)); hit = False
    _rec("M4 幻觉字段名 => F3 判红", hit)
    _IN_MUT[0] = False


def main():
    core, docs, jobs = _src(CORE_JS), _src(DOCS_JS), _src(JOBS_PY)
    schema = _src(SCHEMA_PY)
    ok = True
    ok &= t_f1(core, docs)
    ok &= t_f2(core)
    ok &= t_f3(core, docs, jobs, schema)
    ok &= t_f4(docs)
    ok &= t_f5(core, docs)
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