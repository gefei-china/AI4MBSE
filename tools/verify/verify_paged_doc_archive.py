# -*- coding: utf-8 -*-
"""verify_paged_doc_archive — 门禁：分页读取的交付物必须**拼齐才归档**，且截断必须可观测。

## 为什么需要这道门禁（会话 9629 实测，产物库为空）

用户问「提示已取回、写入产物，但产物库是空的」。真因是**两处叠加**：

1. `mcp_client.call_tool` 对结果做**无标记硬截**（`mcp.max_response` 默认仅 4000）。
   智源 `export_sysml_v2` 的 READ 一段就 32KB（信封 `{fileName,content,eof,sizeBytes,...}`）
   ⇒ 被切成**非法 JSON**（实测 `json.loads` 报 Unterminated string @ char 242）
   ⇒ `extract_sysml_doc_from_result` 解析失败返回 None ⇒ **交付物不入产物库**。
   且 `truncated` 标志比的是 6000 的展示上限 ⇒ 恒 False ⇒ **没人看得见被截了**。

2. 该 READ 是**分页协议**（offset/bytesRead/nextOffset/eof，80KB 要 3 段），
   而归档判据只看"本段 eof=True" ⇒ 尾段（15KB 尾巴）会被当成**整份文件**归档，
   前两段被丢弃 ⇒ 即使归档也只是一个"看起来完整"的残缺文件。

## 本门禁锁三件事

① 单段不冒充全文：带非零 offset（或带 nextOffset）的段，`extract_sysml_doc_from_result` 必须返回 None；
② 分页必须**拼齐**：缺中间段 / 未到 eof ⇒ None；三段齐 ⇒ 全文，且**字节数 == sizeBytes**；
③ 截断可观测：`call_tool` 返回必须带 `truncated` / `full_len`，且 `mcp.max_response`
   不得小于"常规结构化信封"的量级（否则又回到切碎信封的老路）。

含**变异自证**：把"缺口即拒绝"改坏后，门禁必须变红（防止断言空转）。
"""
import inspect
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.utils import accumulate_paged_doc, collect_tool_sysml_doc, \
    extract_sysml_doc_from_result  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


# ── 构造真实形态的分页信封（中文内容 → 字符数与字节数不同，正好压住"字节偏移"这个坑）──
FULL = "package '整份' {\n" + "".join(
    "    package '子系统%d' { attribute '指标%d' : Real; }\n" % (i, i) for i in range(1, 60)) + "}\n"
_RAW = FULL.encode("utf-8")
SIZE = len(_RAW)
# 按**字符边界**切三段（字节偏移由各段字节长累加得出）——真实服务端也是按字符安全切分，
# 若按字节硬切中文会丢半个字（这本身不是被测代码的缺陷，是夹具必须避免的失真）。
_N = len(FULL)
_CH = [0, _N // 3, (2 * _N) // 3, _N]
_TEXT = [FULL[_CH[i]:_CH[i + 1]] for i in range(3)]
_OFF = [0]
for _t in _TEXT:
    _OFF.append(_OFF[-1] + len(_t.encode("utf-8")))
CUTS = _OFF
NPAGE = len(_TEXT)


def page(i):
    """第 i 段（0-based）的信封：offset/bytesRead 均为**字节**口径。"""
    seg = _TEXT[i]
    eof = (i == NPAGE - 1)
    return json.dumps({
        "taskId": "T1", "fileName": "demo.sysml", "sizeBytes": str(SIZE),
        "offset": CUTS[i], "bytesRead": len(seg.encode("utf-8")),
        "nextOffset": None if eof else CUTS[i + 1],
        "eof": eof, "completeFile": False, "content": seg,
    }, ensure_ascii=False)


print("=== ① 单段不得冒充全文 ===")
for i in range(NPAGE):
    d = extract_sysml_doc_from_result("export_sysml_v2", page(i))
    ok("第%d段单独不归档（offset=%d, eof=%s）" % (i + 1, CUTS[i], i == NPAGE - 1),
       d is None, "实际=%r" % (None if d is None else len(d["text"])))

print("\n=== ② 分页必须拼齐 ===")
pages = {}
d1 = accumulate_paged_doc(pages, "export_sysml_v2", page(0))
ok("读第1段后不归档", d1 is None)
d2 = accumulate_paged_doc(pages, "export_sysml_v2", page(1))
ok("读第2段后仍不归档", d2 is None)
d3 = accumulate_paged_doc(pages, "export_sysml_v2", page(2))
ok("第3段(eof)后归档", d3 is not None)
if d3:
    ok("拼出全文（字节数 == sizeBytes）", len(d3["text"].encode("utf-8")) == SIZE,
       "%d vs %d" % (len(d3["text"].encode("utf-8")), SIZE))
    ok("内容与原文一致", d3["text"] == FULL)
    ok("标题取文件名", d3["title"] == "demo.sysml", d3["title"])

print("\n=== ③ 缺口 / 未到 eof 一律拒绝 ===")
gap = {}
accumulate_paged_doc(gap, "export_sysml_v2", page(0))
_gapdoc = accumulate_paged_doc(gap, "export_sysml_v2", page(2))   # 跳过第 2 段
ok("缺中间段时 eof 段不归档（防半截）", _gapdoc is None,
   "实际=%r" % (None if _gapdoc is None else len(_gapdoc["text"])))
notyet = {}
accumulate_paged_doc(notyet, "export_sysml_v2", page(0))
_r1 = accumulate_paged_doc(notyet, "export_sysml_v2", page(1))
ok("未到 eof ⇒ 不归档", _r1 is None)

print("\n=== ④ 截断必须可观测（不再静默丢数据）===")
src_mcp = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "mcp_client.py"), encoding="utf-8").read()
ok("call_tool 回传 truncated 标志", '"truncated": _cut' in src_mcp)
ok("call_tool 回传 full_len（真实长度）", '"full_len": _full' in src_mcp)
from core import config as _cfg
_mr = int(_cfg.get("mcp", "max_response") or 0)
ok("mcp.max_response ≥ 64KB（容得下常规结构化信封）", _mr >= 65536, "实际=%d" % _mr)

print("\n=== ⑤ 变异自证：改坏「缺口即拒绝」后门禁必须变红 ===")
ns = dict(globals())
ns.update({"json": json})
_mut_src = inspect.getsource(accumulate_paged_doc).replace(
    "        if o != pos:\n            return None  # 有缺口（中间段缺失/被截断）⇒ 不归档",
    "        if False:\n            return None")
_mut_src = _mut_src.replace("    if not eof:\n        return None      # 还没读到末段 ⇒ 不归档（防半截）",
                            "    if False:\n        return None")
exec(compile(_mut_src, "<mut>", "exec"), ns)
_mut = ns["accumulate_paged_doc"]
m = {}
r0 = _mut(m, "export_sysml_v2", page(0))
_bad = (r0 is not None)      # 变异版：第 1 段（未 eof）就吐全文 ⇒ 门禁设计应能抓到
ok("变异版在第1段就误归档（说明原实现确有必要保护）", _bad,
   "变异版返回=%r" % (None if r0 is None else len(r0["text"])))
if _bad:
    print("  ⇒ 原实现返回 None、变异版返回半截：断言确实能区分（非空转）")

print("\n" + "=" * 70)
if FAILS:
    print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
    sys.exit(1)
print("✅ 通过 —— 分页拼齐才归档、单段不冒充全文、截断可观测（含变异自证）")
