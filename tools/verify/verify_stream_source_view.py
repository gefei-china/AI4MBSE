# -*- coding: utf-8 -*-
"""门禁：stream 源码视图与真实 stream.py 逐字节等价，且判据有牙。

这是 `stream.py` 拆分契约第一步的**硬判据**：
在尚未拆分时，`read_stream_source()` 必须与直接读 `stream.py` 的字节完全相同。
不满足就意味着将来切换 86 个门禁后，它们验的将不再是真实代码。

变异自证（M1-M4）：
  M1 视图里少一个分片        ⇒ 等价判据必须变红
  M2 真实源码被改一字        ⇒ 等价判据必须变红
  M3 缓存未失效              ⇒ 证明 invalidate_cache() 真的有用
  M4 module_for 锚点映射错   ⇒ 必须变红
"""
import hashlib
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import stream_source as V  # noqa: E402

PASS, FAIL = [], []
REL = "agent/pipeline_parts/stream.py"
ABS = os.path.join(ROOT, REL)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


print("[1] 视图与真实文件逐字节等价", flush=True)
real = open(ABS, "rb").read()
view = V.read_stream_source_bytes()
check("V1 视图非空", len(view) > 100000, "bytes=%d" % len(view))
check("V2 字节完全相同", view == real,
      "view=%d real=%d sha=%s" % (len(view), len(real), hashlib.sha256(real).hexdigest()[:12]))
check("V3 文本可解码且行数一致",
      V.read_stream_source().count("\n") == real.decode("utf-8").count("\n"),
      "lines=%d" % V.read_stream_source().count("\n"))
# ⚠️ 86 个门禁用 open(..., encoding=).read() 的文本模式语义（universal newline）：
#    它们断言里写死了 `\n`，视图若返回 CRLF 原文会大面积假红（实测踩过）。
check("V3b 文本视图换行归一为 LF", "\r" not in V.read_stream_source(),
      "CR=%d" % V.read_stream_source().count("\r"))
check("V3c 文本视图与 open().read() 语义相同",
      V.read_stream_source() == open(ABS, encoding="utf-8").read())
check("V4 分片表当前只有 stream.py 一项",
      V.SOURCE_PARTS == (REL,), str(V.SOURCE_PARTS))
check("V5 锚点定位返回默认文件",
      V.module_for("execute_stream") == REL, V.module_for("execute_stream"))

print("[2] 覆盖门禁实际用到的锚点", flush=True)
src_text = real.decode("utf-8")
anchors = ["def execute_stream", "def _stream_orchestrated_flow",
           "degrade", "drain", "StreamMixin"]
for a in anchors:
    check("V6 锚点 %s 在视图中可见" % a, a in V.read_stream_source())
    check("V7 module_for(%s) 可用" % a, bool(V.module_for(a)))

print("[3] 变异自证（判据有牙）", flush=True)
orig_parts = V.SOURCE_PARTS
try:
    # M1 少一个分片 ⇒ 字节必然不等
    V.SOURCE_PARTS = ()
    V.invalidate_cache()
    check("M1 空分片表 ⇒ V2 必须变红", V.read_stream_source_bytes() != real)
finally:
    V.SOURCE_PARTS = orig_parts
    V.invalidate_cache()

# M2 真实文件改一字 ⇒ 等价判据必须变红（逐字节还原 + 哈希自证）
before = hashlib.sha256(open(ABS, "rb").read()).hexdigest()
try:
    with open(ABS, "ab") as f:
        f.write(b"\n# M2 probe\n")
    mutated = open(ABS, "rb").read()
    # ⚠️ 必须 invalidate：视图带 lru_cache，外部直接改文件不会自动刷新。
    #    判据口径：视图刷新后应等于**变异后的**文件、而不再等于**原始快照** real，
    #    即 V2 的「view == real」这个断言会翻红——这才叫"抓到变更"。
    V.invalidate_cache()
    view_after = V.read_stream_source_bytes()
    check("M2 真实源码变更 ⇒ 等价断言翻红",
          view_after == mutated and view_after != real,
          "view==mutated:%s view!=real:%s" % (view_after == mutated, view_after != real))
finally:
    with open(ABS, "wb") as f:
        f.write(real)
    V.invalidate_cache()
after = hashlib.sha256(open(ABS, "rb").read()).hexdigest()
check("M2b 还原逐字节一致", before == after)

# M3 cache：源码被**外部改写**后必须自动读到新内容（不依赖调用方记得 invalidate）
#
# ⚠️ 2026-11-25 改判据：首版是裸 `lru_cache`，M3 断言「不 invalidate 就读到旧内容」。
#    那条性质对**只读门禁**无害，对**变异自证门禁**却是致命的 —— 它改文件后视图没变
#    ⇒ 变异"看起来没生效" ⇒ 判成绿的（静默失绿）。缓存键改成 (mtime_ns, size) 之后，
#    该断言必须**反转**成「改了文件就必须看到新内容」，否则会持续假红。
V.invalidate_cache()                           # 从干净基线起
try:
    _ = V.read_stream_source_bytes()           # 填充缓存
    with open(ABS, "ab") as f:
        f.write(b"\n# M3 probe\n")
    fresh = V.read_stream_source_bytes()       # 不做任何 invalidate
    check("M3 外部改文件 ⇒ 视图自动读到新内容（缓存不能骗人）", fresh != real,
          "仍等于旧快照 ⇒ 变异自证会静默失绿")
    V.invalidate_cache()
    check("M3b invalidate 后仍读到新内容", V.read_stream_source_bytes() != real)
finally:
    with open(ABS, "wb") as f:
        f.write(real)
    V.invalidate_cache()
check("M3c 还原后视图回到原快照",
      V.read_stream_source_bytes() == real)
check("M3d 最终还原逐字节一致",
      hashlib.sha256(open(ABS, "rb").read()).hexdigest() == before)

# M4 锚点映射错误必须能被发现
saved = dict(V.ANCHOR_INDEX)
try:
    V.ANCHOR_INDEX["execute_stream"] = "wrong/path.py"
    check("M4 错误锚点映射 ⇒ module_for 返回错误路径",
          V.module_for("execute_stream") == "wrong/path.py")
finally:
    V.ANCHOR_INDEX.clear()
    V.ANCHOR_INDEX.update(saved)

print("[4] 写回面棘轮（拆分第二步的已知债，只降不升）", flush=True)
# 读侧已全部收口到视图（0 个直读点）；**写侧**还有若干门禁在做
# 「变异 → 整文件改写 → 回滚」：`open(fp, "wb").write(whole_src)`。
# 拆分后这类写法会把「拼接后的全文」写进某一个分片 ⇒ 直接写坏源码，
# 所以第二步之前必须改成「按锚点定位分片读写」（module_for / 新增 read_part）。
# 在此之前用**棘轮**钉住数量：新增写回型门禁必须先登记，不许悄悄变多。
# 实测基线（2026-11-25）：**7 个变异点 × (写入 + 回滚) = 14 次调用**，分布在
# verify_chat_task_intent / verify_dedup_and_finalize_hint / verify_final_artifact_only /
# verify_partial_persist_on_failure / verify_proc_attribution / verify_prose_confirm_card /
# verify_stream_repair_loop。预算就钉在 14：新增必须先把上面的「按锚点读写」做完。
WRITE_BACK_BUDGET = 14


def _write_back_sites():
    import ast
    out = []
    for dp, dns, fns in os.walk(os.path.join(ROOT, "tools", "verify")):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for f in sorted(fns):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dp, f)
            txt = open(p, encoding="utf-8", errors="replace").read()
            if "stream.py" not in txt:
                continue
            try:
                tree = ast.parse(txt)
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                f2 = n.func
                nm = f2.id if isinstance(f2, ast.Name) else (
                    f2.attr if isinstance(f2, ast.Attribute) else "")
                if nm not in ("open", "io.open"):
                    continue
                mode = "".join(a.value for a in n.args[1:]
                               if isinstance(a, ast.Constant) and isinstance(a.value, str))
                if "w" not in mode:
                    continue
                if any(s.value.replace("\\", "/").endswith("pipeline_parts/stream.py")
                       or "stream.py" in s.value
                       for a in n.args for s in ast.walk(a)
                       if isinstance(s, ast.Constant) and isinstance(s.value, str)):
                    out.append((os.path.basename(p), n.lineno))
                elif isinstance(n.args[0], ast.Name):
                    # 变量间接：该变量在本文件里被赋成 stream.py 路径
                    var = n.args[0].id
                    if any(isinstance(x, ast.Assign) and isinstance(x.targets[0], ast.Name)
                           and x.targets[0].id == var
                           and "stream.py" in (ast.get_source_segment(txt, x.value) or "")
                           for x in ast.walk(tree)):
                        out.append((os.path.basename(p), n.lineno))
    return out


_WB = _write_back_sites()
check("V8 写回型门禁数不超过预算（新增须显式登记）",
      len(_WB) <= WRITE_BACK_BUDGET, "%d 处 > 预算 %d：%s" % (len(_WB), WRITE_BACK_BUDGET, _WB[:6]))


def _direct_readers():
    """仍在**真的打开** stream.py 读源码的门禁调用点（含变量间接）。"""
    import ast
    out = []
    for dp, dns, fns in os.walk(os.path.join(ROOT, "tools", "verify")):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for f in sorted(fns):
            if not f.endswith(".py") or f == os.path.basename(__file__):
                continue
            p = os.path.join(dp, f)
            txt = open(p, encoding="utf-8", errors="replace").read()
            if "stream.py" not in txt:
                continue
            try:
                tree = ast.parse(txt)
            except SyntaxError:
                out.append((f, 0, "PARSE-FAIL"))
                continue
            readers = set()
            for x in tree.body:
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(isinstance(s, ast.Call) and (
                            (isinstance(s.func, ast.Name) and s.func.id == "read_stream_source")
                            or (isinstance(s.func, ast.Attribute)
                                and s.func.attr == "read_stream_source"))
                           for s in ast.walk(x)):
                        readers.add(x.name)
            vars_ = {x.targets[0].id for x in ast.walk(tree)
                     if isinstance(x, ast.Assign) and len(x.targets) == 1
                     and isinstance(x.targets[0], ast.Name)
                     and "stream.py" in (ast.get_source_segment(txt, x.value) or "")}
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                f2 = n.func
                nm = f2.id if isinstance(f2, ast.Name) else (
                    f2.attr if isinstance(f2, ast.Attribute) else "")
                if nm == "read_stream_source" or nm in readers:
                    continue
                if nm not in ("open", "io.open", "read_text", "read", "read_bytes"):
                    continue
                mode = "".join(a.value for a in n.args[1:]
                               if isinstance(a, ast.Constant) and isinstance(a.value, str))
                if "w" in mode:
                    continue
                for a in n.args:
                    hit = any(s.value.replace("\\", "/").endswith("pipeline_parts/stream.py")
                              for s in ast.walk(a)
                              if isinstance(s, ast.Constant) and isinstance(s.value, str))
                    if hit or (isinstance(a, ast.Name) and a.id in vars_):
                        out.append((f, n.lineno, nm))
                        break
    return out


_DR = _direct_readers()
check("V9 读侧直读点已归零（全部走 read_stream_source）", not _DR, _DR[:6])

print("\n===")
print("%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)