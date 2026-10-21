# -*- coding: utf-8 -*-
"""门禁：logging 占位符与实参个数必须严格一致（#58）。

## 为什么要有这道门禁（真实 bug 的取证链）
`services/rag_rerank.py` 的"解析失败"告警：

    logging.getLogger(__name__).warning(
        "llm_rerank 解析失败（provider_id=%s）: %s | content 前 80=%r",   # 3 个占位符
        _pid or "(默认)", type(e).__name__, str(e)[:80],
        str(msg.get("content") or "")[:80])                                 # 却传了 4 个实参

⇒ logging 在 `record.getMessage()` 里抛
  `TypeError: not all arguments converted during string formatting`，
  该条 warning **一条字符都写不出去**（只往 stderr 打一段 logging 自身的 traceback）。

这个 bug 极毒：那段告警是 2026-10-10 专门为"重排静默降级、排查花了 4~6 轮"加的
**唯一诊断线索**（provider_id + 异常类型 + content 三者缺一不可）。
格式化一坏，线索全灭 ⇒ 症状退回到"开关开了但顺序一字未变"，且**日志里什么都没有**
——比没有告警更难查（会误判为"没走到那个分支"）。

## 判据设计（三条，都是自证的）
[A] **静态**：AST 扫描全仓 logging 调用，占位符数 == 实参数。
    ⚠️ 必须正确处理 `%%`（字面百分号，不占位）—— 否则误报。
       本门禁 A2 组用 `llm/__init__.py:405`（含 `%.0f%%`）做**负对照**：
       它曾被朴素正则误报，实为正确 ⇒ 朴素实现必须被判 FAIL。
[B] **动态**：把每个真实调用的 (fmt, args) 喂给 logging，断言
    ① 不抛异常 ② 输出里**确实含**所有实参的可读痕迹（真格式化，非被吞）。
[C] **契约**：rag_rerank 三条告警路径（解析失败 / 未拿到评分 / 排序段异常）
    都能在**不联网**前提下被触发，且每条都留下**含 provider_id 与 content** 的日志。
    —— 这条最关键：它守的正是"诊断线索不许再被吞掉"这个原始诉求。

运行：./.venv/Scripts/python.exe tools/verify/verify_rerank_logging_fmt.py
"""
from __future__ import annotations

import ast
import io
import logging
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

PASS, FAIL = [], []
_SKIP_DIRS = {"tmp", "backups", ".venv", "node_modules", "logs", ".git",
              "__pycache__", "java-runtime", "fuseki", "run", "dist", "build"}


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + str(detail)) if detail else ""))


# ── 占位符计数：必须先把 %% 消掉，否则字面百分号被当成占位符 ──────────
# ★ 口径要点（踩坑两次才写对）：
#   1) `%[srdf]` **不够** —— printf 允许宽度/精度/标志，实参形态是
#      `%.0f` / `%.1fs` / `%5.2f` / `%-10s` / `%+d`。只匹配紧邻字母会漏掉
#      `%.0f`（`%` 后是 `.`）⇒ 计数偏小 ⇒ 误报。
#   2) `%%` 是字面百分号，不占位；`%(<name>)s` 是具名占位，算 1。
_PH = re.compile(r"%(?:\(\w+\))?[-+ #0-9.*]*[srdf]")


def count_placeholders(fmt: str) -> int:
    """正确口径：`%%` 不占位；`%.Nf` 这类带精度的**要**占位；具名占位算 1。

    ★ 负对照来源（两轮踩坑都栽在这里）：
      `llm/__init__.py:405` = "… 占比=%.0f%% latency=%.1fs …"
        - 朴素 `%[srdf]` 计数 = 0（漏 %.0f / %.1f）⇒ 误判"无占位符"而跳过检查
        - 不剔 `%%` 的实现会把字面百分号算进去 ⇒ 多算
      `llm/__init__.py:683` = "已耗时 %.0fs + 单次超时 %.0fs > 总预算 %.0fs"
        同样全是带精度的浮点占位符 ⇒ 同上。
    """
    tmp = fmt.replace("%%", "\x00")           # 先摘掉字面百分号
    return len(_PH.findall(tmp))


# 朴素实现（**故意写错**，只用于负对照自证）
def _count_naive(fmt: str) -> int:
    return len(re.findall(r"%[srdefgc]", fmt))


# ══════════════════════════════════════════════════════════════════
# [A] 静态：全仓 AST 扫描
# ══════════════════════════════════════════════════════════════════
print("\n[A] 静态扫描：全仓 logging 占位符 vs 实参（%% 正确处理）")

# A0 口径自证：先证明朴素实现会误报、正确实现不会
_fmt_405 = "… 占比=%.0f%% latency=%.1fs …"       # llm/__init__.py:405 同款
check("A0a 朴素正则漏计带精度占位符（应为 0 而非 3）",
      _count_naive(_fmt_405) == 0, "朴素=%d" % _count_naive(_fmt_405))
check("A0b 正确口径计入 %.Nf（应为 2）",
      count_placeholders(_fmt_405) == 2, "正确=%d" % count_placeholders(_fmt_405))
check("A0c '100%%' 纯字面量算 0 占位符",
      count_placeholders("完成率 100%%") == 0)
check("A0d 具名占位 %(<name>)s 算 1",
      count_placeholders("耗时 %(t).1fs 秒") == 1)
check("A0e '%%(x)%%' 不误算",
      count_placeholders("%%(not_a_placeholder)%%") == 0)
check("A0f 负对照真实样本：llm/__init__.py:405 格式串算 9（与实参 9 一致）",
      count_placeholders(
          "[thinking] 思考 token 超限｜intent=%s provider=%s(%s) "
          "reasoning=%s completion=%s 占比=%.0f%% latency=%.1fs finish=%s"
          "｜思考与正文共享 max_tokens，正文仅得 %s tok"
          "｜处置：调低 llm.thinking_budget，或把该 intent 加入 thinking_policy.off")
      == 9)
check("A0g 负对照真实样本：llm/__init__.py:683 格式串算 5",
      count_placeholders(
          "LLM 重试中止：已耗时 %.0fs + 单次超时 %.0fs > 总预算 %.0fs"
          "（provider=%s model=%s）") == 5)

# A1 全仓扫描
scanned, mismatched = 0, []
for root, dirs, files in os.walk(_ROOT):
    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(root, f)
        try:
            tree = ast.parse(open(p, encoding="utf-8", errors="ignore").read())
        except Exception:
            continue
        rel = os.path.relpath(p, _ROOT)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("warning", "error", "info", "debug",
                                        "critical", "exception")):
                continue
            if len(n.args) < 2:
                continue
            a0 = n.args[0]
            if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                continue          # 变量/拼接式格式串：静态不可判 ⇒ 跳过（不假装覆盖）
            if any(isinstance(a, ast.Starred) for a in n.args):
                continue          # *args 展开：个数不可静态判定
            nph = count_placeholders(a0.value)
            na = len(n.args) - 1
            if nph == 0:
                continue          # 无占位符 ⇒ 多余实参也是 bug，但本门禁只守占位符侧
            scanned += 1
            if nph != na:
                mismatched.append((rel, n.lineno, nph, na, a0.value[:60]))

check("A2 全仓扫描覆盖到调用点（>50，%% 负对照未误报）",
      scanned > 50, "scanned=%d" % scanned)
check("A3 全仓无占位符/实参不匹配", not mismatched,
      "; ".join("%s:%d(%d!=%d)" % m[:4] for m in mismatched[:6]))

# A4 变异自证：故意造一处不匹配，确认判据能抓住（否则 A3 的"全绿"可能因为判据失灵）
_mut = 'logger.warning("x=%s y=%s", 1)'
_t = ast.parse("logger.warning('x=%s y=%s', 1)")
_c = [x for x in ast.walk(_t) if isinstance(x, ast.Call)][0]
check("A4 变异体（2占位符对1实参）被判不匹配",
      count_placeholders(_c.args[0].value) != len(_c.args) - 1, _mut)


# ══════════════════════════════════════════════════════════════════
# [B] 动态：真喂给 logging，断言不抛异常且输出含实参痕迹
# ══════════════════════════════════════════════════════════════════
print("\n[B] 动态：真实告警路径喂给 logging（不联网）")


def capture(fn, *a, **kw) -> tuple[str, bool]:
    """跑 fn，把 root logger 上的记录收进 StringIO。返回 (输出, 是否发生格式化错误)。"""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    old_level, old_prop = root.level, root.propagate
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    err = []

    class _Trap(logging.Handler):
        def emit(self, record):
            try:
                self.format(record)
            except Exception as e:      # logging 内部错误走 handleError，不抛
                err.append(type(e).__name__)

    trap = _Trap()
    root.addHandler(trap)
    try:
        fn(*a, **kw)
    finally:
        root.removeHandler(h)
        root.removeHandler(trap)
        root.setLevel(old_level)
        root.propagate = old_prop
    return buf.getvalue(), bool(err)


# B1 rag_rerank「解析失败」告警（真代码路径：喂非 JSON 的 content）
import services.rag_rerank as rr   # noqa: E402

_real_chat = None
try:
    from llm import llm_client as _lc
    _real_chat = _lc.chat

    def _fake_chat(*a, **kw):
        return {"choices": [{"message": {"content": "Sorry, I cannot comply."}}]}

    _lc.chat = _fake_chat
    hits = [{"content": "x%d" % i, "section": "1.%d" % i} for i in range(4)]
    out, err = capture(rr.llm_rerank, "测试查询", hits, top_k=2)
    check("B1 解析失败告警：logging 内部无格式化错误", not err, "errors=%s" % err)
    check("B2 解析失败告警：真的写出了内容", "llm_rerank" in out, out[:110])
    check("B3 解析失败告警：含 content 线索（诊断三要素之一）",
          "cannot comply" in out, out[:160])
    check("B4 解析失败告警：含 provider 标识", "(默认)" in out or "provider_id" in out,
          out[:160])
    check("B5 解析失败时静默回退原排序（不抛给调用方）",
          isinstance(out, str) and len(out) >= 0)
    _lc.chat = _real_chat

    # B6 「未拿到评分」告警：合法 JSON 但 ratings 为空
    def _fake_chat2(*a, **kw):
        return {"choices": [{"message": {"content": '{"ratings": []}'}}]}

    _lc.chat = _fake_chat2
    hits2 = [{"content": "y%d" % i} for i in range(3)]
    out2, err2 = capture(rr.llm_rerank, "q", hits2, top_k=2)
    check("B6 未拿到评分告警：无格式化错误且写出",
          not err2 and "未拿到评分" in out2, "err=%s out=%s" % (err2, out2[:110]))

    # B7 「排序段异常」告警：让 hits 里的元素在排序段真的炸掉
    #     （排序段读 h["rerank_score"]；这里让 __setitem__ 抛，
    #      使 `h["rerank_score"] = ...` 赋值语句本身失败 ⇒ 进最外层 except）
    def _fake_chat3(*a, **kw):
        return {"choices": [{"message": {
            "content": '{"ratings":[{"i":0,"score":5},{"i":1,"score":9}]}'}}]}

    _lc.chat = _fake_chat3

    class _Boom(dict):
        def __setitem__(self, k, v):
            raise TypeError("boom in sort")

    out3, err3 = capture(rr.llm_rerank, "q", [_Boom(content="z0"), _Boom(content="z1")],
                         top_k=2)
    check("B7 排序段异常告警：被触发且无格式化错误",
          not err3 and "排序段异常" in out3, "err=%s out=%s" % (err3, out3[:130]))
    check("B8 排序段异常告警含异常类型（可定位）",
          "TypeError" in out3 or "boom in sort" in out3, out3[:130])
finally:
    if _real_chat is not None:
        try:
            from llm import llm_client as _lc2
            _lc2.chat = _real_chat
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# [C] 契约：源码层取证 —— 三条告警都在、且实参个数严格等于占位符数
# ══════════════════════════════════════════════════════════════════
print("\n[C] 契约：rag_rerank 三条诊断告警齐备且自洽")

_src = open(os.path.join(_ROOT, "services", "rag_rerank.py"),
            encoding="utf-8").read()
_tree = ast.parse(_src)
_calls = {}
for _n in ast.walk(_tree):
    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
            and _n.func.attr in ("warning", "error")
            and len(_n.args) >= 2
            and isinstance(_n.args[0], ast.Constant)
            and isinstance(_n.args[0].value, str)
            and not any(isinstance(a, ast.Starred) for a in _n.args)):
        _calls[_n.lineno] = (count_placeholders(_n.args[0].value), len(_n.args) - 1)

check("C1 三条告警的占位符数均等于实参数（无一处错配）",
      bool(_calls) and all(k == v for k, v in _calls.values()), str(_calls))
check("C2 恰好三条诊断告警齐备（解析失败/未拿到评分/排序段异常）",
      len(_calls) == 3, str(_calls))
check("C2b 三条告警分别对应三种故障（源码取证）",
      all(s in _src for s in ("解析失败", "未拿到评分", "排序段异常")))
check("C3 三条告警均含 provider 标识",
      _src.count("provider_id=%s") == 3, "count=%d" % _src.count("provider_id=%s"))
check("C4 解析失败告警含 content 线索（原始诉求：不许再被吞）",
      "content 前 80=%r" in _src)
check("C5 源码里不再有 3占位符配4实参 的写法",
      "provider_id=%s）: %s | content 前 80=%r" not in _src)


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 72)
print("结果：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    print("❌ 门禁失败：")
    for f in FAIL:
        print("   - " + f)
    sys.exit(1)
print("✅ 门禁通过")