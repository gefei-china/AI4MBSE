"""verify_clarify_frontend_contract — 澄清回答的前后端字段契约 + hover 摘要终态。

2026-11-26 实测缺陷：
  后端 /clarify-answer 返回 `append_text`，前端三处读 `r.resume_append`
  ⇒ append 恒空串 ⇒ DB 层「澄清补充追加进原 user 消息」的单轮化在 UI 路径上失效。

  为什么既有 26/0 门禁没抓到：verify_clarify_single_turn.py 在 Python 侧
  手工把 `r["append_text"]` 映射成 `resume_append` 再发 stream —— 它证明的是
  API 行为，不是**前端真的把值带过去了**。判据必须跨过 JS 这一层。

  另：hover 卡「正在生成摘要…」是 /summary 请求的占位文案；该端点同步走 LLM，
  裸 fetch 无超时 ⇒ 永久停在占位，用户表现为「页面没反应」。现加 6s 超时 + 兜底终态。

纯静态，可在 pr 跑。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
PASS = []
FAIL = []


def check(tag, ok, detail=""):
    (PASS if ok else FAIL).append(tag)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", tag,
                           ("  ← " + str(detail)) if detail else ""))
    return bool(ok)


def returned_keys(path, func_name):
    """AST 取指定函数 return 字典的字面量 key（不看注释/文档）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            keys = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    for k in sub.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
            return keys
    return set()


def call_windows(src, width=1400):
    """只取真正的 await api(.../clarify-answer...) 调用点，注释里的字面量不算。"""
    out = []
    for m in re.finditer(r"await\s+api\([^\n]{0,160}/clarify-answer", src):
        out.append(src[m.start():m.start() + width])
    return out


def main():
    print("=" * 72)
    print("澄清回答 · 前后端字段契约 + hover 摘要终态")
    print("=" * 72)

    router = ROOT / "routers" / "conversations.py"
    keys = returned_keys(router, "clarify_answer")
    check("C1 后端返回 append_text", "append_text" in keys, sorted(keys))
    check("C2 后端返回 resume_text / resume_of_user_msg_id",
          {"resume_text", "resume_of_user_msg_id"}.issubset(keys), sorted(keys))

    js_files = [ROOT / "static" / "js" / "mods" / n
                for n in ("11-pipeline.js", "12-chatsend.js")]
    windows = []
    for p in js_files:
        src = p.read_text(encoding="utf-8")
        wins = call_windows(src)
        check("C3 %s 含澄清回答调用点" % p.name, bool(wins), len(wins))
        windows.extend((p.name, w) for w in wins)

    check("C4 三个调用点（2 选项 + 1 自由文本）", len(windows) == 3, len(windows))
    for name, w in windows:
        check("C5 %s 用后端真字段 append_text" % name,
              "r.append_text" in w and "r.resume_append" not in w)
    check("C6 选项/自由文本路径都把续答文本带下去",
          sum(1 for _, w in windows if "resume_text" in w) == 3)

    # stream 请求字段仍是 resume_append（不可被上面的修复误改）
    send = (ROOT / "static" / "js" / "mods" / "12-chatsend.js").read_text(encoding="utf-8")
    check("C7 /chat/stream 请求字段仍为 resume_append",
          "resume_append:" in send and "resume_append: (resumeOf && resumeOf.append)" in send)

    # hover 摘要终态
    tip = (ROOT / "static" / "js" / "mods" / "03-chat.js").read_text(encoding="utf-8")
    check("C8 hover 摘要请求带 AbortSignal", "signal:_ctl.signal" in tip)
    check("C9 hover 摘要有 6s 超时", "setTimeout(()=>_ctl.abort(), 6000)" in tip)
    check("C10 兜底请求也有独立超时", "setTimeout(()=>_ctl2.abort(), 4000)" in tip)
    check("C11 两条路径都在 finally 清定时器",
          tip.count("clearTimeout(_timer)") >= 1 and tip.count("clearTimeout(_timer2)") >= 1)
    check("C12 占位文案不会再无限停留（兜底有终态）",
          "（摘要加载失败）" in tip and "（暂无消息内容）" in tip)

    # ── 变异自证：把前端字段改回旧错名 ⇒ C5 必须翻红 ──
    mutant = "append: r.resume_append || ''"
    correct = "append: r.append_text || ''"
    check("M1 变异体确实变了", mutant != correct)
    check("M2 旧错名会被 C5 判据抓住",
          "r.append_text" not in mutant and "r.resume_append" in mutant)
    # 变异自证：去掉超时 ⇒ C9 必须翻红
    no_timer = tip.replace("setTimeout(()=>_ctl.abort(), 6000)", "")
    check("M3 去掉超时后 C9 会翻红",
          no_timer != tip and "setTimeout(()=>_ctl.abort(), 6000)" not in no_timer)

    print("\n汇总：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    print("口径：AST + 前端源码静态判据，含变异自证；不调 LLM")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
