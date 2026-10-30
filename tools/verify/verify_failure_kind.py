# CI-OPTIONAL: D 组查真库 tool_call_logs 的 kind 填充率（`mbse.db` 不入 git），E 组跑一次回填脚本（需库）。A/B/C/F 组本身无库依赖，但整脚本依赖 D/E 才有意义。
# -*- coding: utf-8 -*-
"""门禁：`failure_kind` 归属必须可用（P0-2，2026-10-30）。

## 这条门禁守三件不同的事（缺一条就会"看起来补齐了但没用"）
1. **分类器不空转**：`classify(..., ok=False)` 对**任何**文本都返回非空类别。
   空串是"成功"的编码，一旦失败也返回空，归因器就把失败读成"未知"。
2. **写入侧只调一次**：历史 bug 是 `classify(classify(text, ok=False))` ——
   外层 ok 默认 True 直接返空 ⇒ 失败行 kind **恒空**（真库 273 行）。
   纯行为断言抓不住"嵌套调用"（只要最外层返空，行为就是空），必须做**源码/AST 级**判据。
3. **真库不变量**：`ok=0 且 kind=''` 的行数必须为 0。这是"维护 100%"的落点 ——
   回填只是把历史拉平，若写入侧再漏，这条会立刻红。

## 为什么还要断"词表没有把可重试判成永久"
`NON_RETRYABLE` 里四类是**永久失败**（重试必然同样失败）。把瞬态误判成永久
（"瞬态故障被永久放弃"）比反过来严重得多 —— 这是 `services/failure_class.py`
模块头的既定取舍。词表新增 5 处关键词后，必须有反向用例守住这条边界。
"""
import ast
import collections
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from services import failure_class as fc          # noqa: E402

_results = []


def check(name, ok, detail=""):
    _results.append((bool(ok), name, detail))
    print("  %s %s%s" % ("PASS" if ok else "FAIL", name,
                         ("  | " + str(detail)[:200]) if (detail and not ok) else ""))


def hr(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


# ─────────────────────────────────────────────────────────────────────────────
hr("[A] 分类器不空转：失败必有类别（任何文本）")
# ─────────────────────────────────────────────────────────────────────────────
_samples = [
    "", "   ", "{}", "None", "null", "?", "err", "unknown",
    "未知工具: query_projects", "工具「x」不在本次子任务授予的工具白名单内（最小权限）",
    "MCP 调用失败: Server error '# Bad Gateway'",
    "工具「x」缺少必填参数: 'vc'", "文件工具执行失败: path 不能为空",
    "工具「x」已注册但未配置执行器", "无输出",
]
_bad = [(t, fc.classify(t, ok=False)) for t in _samples if not fc.classify(t, ok=False)]
check("A1 对 %d 种文本（含空串/垃圾/中文）失败分类均非空" % len(_samples), not _bad,
      "返回空串的：%s" % (_bad[:3],))
check("A2 空文本 ⇒ internal（'有失败标志但无文本'按内部错误，不编造具体类别）",
      fc.classify("", ok=False) == "internal")
check("A3 ok=True 仍返回空串（成功不需要类别）", fc.classify("任意文本", ok=True) == "")

# ★变异：兜底改成返回空串 ⇒ A1/A2 必须翻红
try:
    import inspect
    _src = inspect.getsource(fc.classify)
    _lines = _src.split("\n")
    # 只改**最后一条** `return "internal"`（函数兜底），不动 if not low 分支那条
    _idx = [i for i, ln in enumerate(_lines) if ln.strip() == 'return "internal"']
    assert len(_idx) == 2, "return \"internal\" 出现 %d 次（期望 2）" % len(_idx)
    _lines[_idx[-1]] = '    return ""'
    _mut_src = "\n".join(_lines).replace("def classify", "def _mut_classify", 1)
    _ns = {"re": __import__("re"), "RETRYABLE_KEYWORDS": fc.RETRYABLE_KEYWORDS,
           "is_retryable_error": fc.is_retryable_error}
    for _n in ("_PERMISSION_KW", "_INTERRUPTED_KW", "_TIMEOUT_KW", "_INPUT_KW",
               "_OUTPUT_KW", "_BUSINESS_KW"):
        _ns[_n] = getattr(fc, _n)
    exec(compile(_mut_src, "<mutant>", "exec"), _ns)      # noqa: S102
    _mut = _ns["_mut_classify"]
    check("A4 ★变异：兜底改回空串 ⇒ '永不空' 判据翻红（A1 有牙齿）",
          _mut("未知工具: query_projects", ok=False) == "",
          "变异后仍非空 ⇒ A1 是空转断言")
finally:
    pass

# ─────────────────────────────────────────────────────────────────────────────
hr("[B] 词表补的 5 处：正向命中 + 反向不误伤")
# ─────────────────────────────────────────────────────────────────────────────
_pos = [
    ("工具「graph_retrieve」不在本次子任务授予的工具白名单内（最小权限，Worker ⊆ Supervisor），已拒绝执行",
     "permission", False),
    ('{"code":"INVALID_ARGUMENTS","message":"参数不符合工具要求"}', "input_invalid", False),
    ("工具「zhiyuan_sysmlv2_check」缺少必填参数: 'targetPackage'", "input_invalid", False),
    ("文件工具执行失败: path 不能为空", "input_invalid", False),
    ("工具「requirement_itemize」已注册但未配置执行器（仅内置工具与在线 MCP 可执行）",
     "business", False),
    ("无输出", "timeout", True),
]
for txt, want, retryable in _pos:
    got = fc.classify(txt, ok=False)
    check("B1 %-22s ⇒ %s（%s）" % (want, txt[:22], "可重试" if retryable else "永久"),
          got == want and fc.is_retryable_class(got) is retryable, "got=%s" % got)

# 反向①：新关键词不得把"信息性文案"拖进永久失败
check("B2 不误伤：'文档白名单已放宽为不限文档' 不得判为 permission（只加 '工具白名单内'）",
      fc.classify("文档白名单已放宽为不限文档", ok=False) != "permission",
      "got=%s" % fc.classify("文档白名单已放宽为不限文档", ok=False))
# 反向②：瞬态故障必须**仍然**判 transient（新关键词不得把它们抢走）
#   ⚠️ 第一版把这条写成 `got != "transient"`（语义写反）—— 那等于要求"瞬态必须被误判"。
#      负向用例的判据是"**保持原判**"，不是"变成别的"。
for txt in ("MCP 调用失败: Server error '# Bad Gateway'",
            "connection reset by peer",
            "HTTP 503 Service Unavailable",
            "429 too many requests"):
    got = fc.classify(txt, ok=False)
    check("B2b 保持瞬态：%r ⇒ transient 且可重试" % txt[:26],
          got == "transient" and fc.is_retryable_class(got) is True, "got=%s" % got)
check("B3 瞬态故障仍全部可重试（新增词表未把它们拖进 NON_RETRYABLE）",
      all(fc.is_retryable_class(fc.classify(t, ok=False)) for t in
          ("connection reset by peer", "HTTP 503", "429 too many requests", "gateway timeout")))

# ─────────────────────────────────────────────────────────────────────────────
hr("[C] 写入侧：两处落库都必须「分类器只调一次」")
# ─────────────────────────────────────────────────────────────────────────────
def _nested_classify_calls(path):
    """返回 (调用次数, 嵌套调用次数)。

    嵌套 = **`classify(...)` 的实参里又出现 `classify(...)`**（历史 bug 的形态）。
    ⚠️ 第一版写成"实参里出现任何 `ast.Call`" —— `classify(str(r.get("error")), ok=False)`
    里的 `r.get(...)` 也是 Call ⇒ **误报**。判据必须锚"同一个函数名被嵌套调用"，
    而不是"参数里有函数调用"（后者是所有正常代码的形态）。
    """
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    aliases = {"classify"}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").endswith("failure_class"):
            for a in n.names:
                if a.name == "classify":
                    aliases.add(a.asname or a.name)

    def _fname(c):
        f = c.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    total, nested = 0, 0
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and _fname(n) in aliases:
            total += 1
            inner = [x for a in n.args for x in ast.walk(a)
                     if isinstance(x, ast.Call) and _fname(x) in aliases]
            if inner:
                nested += 1
    return total, nested


for rel in ("agent/pipeline_parts/tools.py", "agent/pipeline_parts/memory.py"):
    p = os.path.join(ROOT, rel)
    t, nd = _nested_classify_calls(p)
    check("C1 %s：分类器被调用 %d 次，嵌套 %d 次" % (rel.split("/")[-1], t, nd),
          t >= 1 and nd == 0, "嵌套调用会让外层 ok=True 直接返空 ⇒ 失败行 kind 恒空")

# ─────────────────────────────────────────────────────────────────────────────
hr("[D] 真库不变量：失败行 kind 填充率必须 100%")
# ─────────────────────────────────────────────────────────────────────────────
try:
    ro = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    n_all, n_fail, n_kind = ro.execute(
        "SELECT COUNT(*), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN ok=0 AND COALESCE(failure_kind,'')<>'' THEN 1 ELSE 0 END) "
        "FROM tool_call_logs").fetchone()
    n_fail = int(n_fail or 0)
    n_kind = int(n_kind or 0)
    check("D1 失败行 kind 填充率 100%%（%d/%d）" % (n_kind, n_fail),
          n_fail > 0 and n_kind == n_fail,
          "缺 %d 行 —— 写入侧或回填出现新缺口" % (n_fail - n_kind))
    dist = collections.Counter(
        {k or "(空)": v for k, v in ro.execute(
            "SELECT COALESCE(failure_kind,''), COUNT(*) FROM tool_call_logs "
            "WHERE ok=0 GROUP BY 1")})
    share_internal = 100.0 * dist.get("internal", 0) / max(n_fail, 1)
    check("D2 kind 分布可用：至少 5 类且 internal 占比 < 60%%（实测 %.1f%%）" % share_internal,
          len(dist) >= 5 and share_internal < 60.0, "dist=%s" % dict(dist))
    print("    分布：%s" % dict(dist.most_common()))
    ro.close()
except Exception as e:      # noqa: BLE001
    check("D 真库体检可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ─────────────────────────────────────────────────────────────────────────────
hr("[E] 回填脚本幂等（第二次跑必须 0 行）")
# ─────────────────────────────────────────────────────────────────────────────
try:
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(ROOT, "tools", "_failure_kind_backfill.py")],
                       capture_output=True, text=True, timeout=180, cwd=ROOT)
    out = (r.stdout or "") + (r.stderr or "")
    check("E1 回填脚本可运行且报告 0 行（幂等）",
          "待回填 0 行" in out, out.strip().splitlines()[-1][:160] if out.strip() else "无输出")
    check("E2 预演不写库（默认无 --apply）", "--apply" not in " ".join(sys.argv), "")
except Exception as e:      # noqa: BLE001
    check("E1 回填脚本可运行", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

# ─────────────────────────────────────────────────────────────────────────────
hr("[F] 不变量自检")
# ─────────────────────────────────────────────────────────────────────────────
check("F1 失败分类的兜底是 internal（可重试侧），而非某个永久类",
      fc.classify("完全陌生的错误文本 zzz", ok=False) == "internal")
check("F2 NON_RETRYABLE 仍是四类（新增词表没有扩大永久失败集合）",
      set(fc.NON_RETRYABLE) == {"permission", "input_invalid", "output_rejected", "business"},
      "got=%s" % sorted(fc.NON_RETRYABLE))
check("F3 describe() 对每一类都有中文说明（含 internal）",
      all(fc.describe(c) for c in fc.ERROR_CLASSES))
_names = [r[1] for r in _results]
check("F4 断言名唯一", len(set(_names)) == len(_names),
      "dups=%s" % [x for x in set(_names) if _names.count(x) > 1])

n_pass = sum(1 for ok, _, _ in _results if ok)
n_fail = len(_results) - n_pass
print("\n" + "=" * 78)
print("结果：%d PASS / %d FAIL" % (n_pass, n_fail))
if n_fail:
    for ok, name, detail in _results:
        if not ok:
            print("  FAIL %s | %s" % (name, str(detail)[:200]))
print("=" * 78)
sys.exit(1 if n_fail else 0)
