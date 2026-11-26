"""verify_intent_composite_guard — 「建模」误列为复合任务信号的回归门禁。

2026-11-26 实测缺陷（verify_frontend_modeling_e2e 第 1 条）：
  「为纯电动汽车热管理系统生成SysML v2建模需求条目」
  → `_is_composite_task` 因「建模」返回 True
  → 跳过 DB 强特异关键词优先
  → 落 design@0.95（内置「生成+建模」泛词规则）
  而 DB 的 requirement_structuring 明确配了「需求条目」，语义聚类也给 1.0。

判据全部为确定性层（不调 LLM、不写库），可在 pr 跑。
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.intent import IntentRouter  # noqa: E402

PASS = []
FAIL = []


def check(tag, ok, detail=""):
    (PASS if ok else FAIL).append(tag)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", tag,
                           ("  ← " + str(detail)) if detail else ""))
    return bool(ok)


def _composite_tuple_from_ast(path):
    """只认 **AST 里的 tuple 常量**，绝不把注释里的「建模」算进去。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Name) and tgt.id == "_COMPOSITE_SIGNALS") or \
                   (isinstance(tgt, ast.Attribute) and tgt.attr == "_COMPOSITE_SIGNALS"):
                    if isinstance(node.value, ast.Tuple):
                        return [e.value for e in node.value.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return None


def _router():
    # CI 口径：必须读环境变量指定的空库；硬编码真库会让全新克隆里 SystemExit。
    db = pathlib.Path(os.environ.get("MBSE_DB_PATH") or str(ROOT / "mbse.db"))
    if not db.exists():
        raise SystemExit("库不存在：%s" % db)
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    conn.row_factory = sqlite3.Row
    r = IntentRouter()
    for row in conn.execute("SELECT name,intent_keywords FROM agents WHERE status='active'"):
        try:
            kws = json.loads(row["intent_keywords"] or "[]")
        except Exception:
            kws = []
        if kws:
            r.register_keywords(row["name"], kws)
    return r, conn


def main():
    print("=" * 72)
    print("意图复合信号守卫（确定性层，无 LLM）")
    print("=" * 72)

    src = ROOT / "agent" / "intent.py"
    sigs = _composite_tuple_from_ast(src)
    check("C1 AST 取到 _COMPOSITE_SIGNALS", sigs is not None, sigs)
    check("C2 「建模」已移出复合信号", bool(sigs) and "建模" not in sigs, sigs)
    check("C3 真复合信号仍在（防一刀切删空）",
          bool(sigs) and {"代码", "并", "再"}.issubset(set(sigs)), sigs)

    router, conn = _router()
    first = "为纯电动汽车热管理系统生成SysML v2建模需求条目"
    check("C4 FLOW① 不再被判复合任务", not router._is_composite_task(first.lower()))
    check("C5 FLOW① DB 强特异命中 requirement_structuring",
          router._db_kw_priority(first.lower()) == "requirement_structuring",
          router._db_kw_priority(first.lower()))
    # 四条前端建模指令 × 独立会话口径（只测路由，不测会话粘滞）
    # 2026-11-26 真机：独立会话 4/4 OK；同一会话连续下 4 条时后 3 条被上一轮意图带走
    # （粘滞属另一议题，见 work log；本门禁只守"入口能否命中节点"）。
    flow = [
        (first, "requirement_structuring"),
        ("生成架构骨架", "architecture_skeleton"),
        ("生成活动图", "view_expansion"),
        ("生成状态机视图", "view_expansion"),
    ]
    for text, expect in flow:
        got = router.detect(text, conn=conn)
        check("C6 真 detect → %s：%s" % (expect, text[:12]), got == expect, got)
    check("C7 路由层为 DB 强特异（非内置泛词规则）",
          router._last_meta.get("route") == "rule_scored", router._last_meta)

    # 反例：真正多步任务必须仍判复合（P1-14 的原始意图不能被本次改动破坏）
    for text in ("建模+结构视图+代码+校验", "先建结构视图，再生成代码并校验"):
        check("C8 复合任务仍判复合：%s" % text[:12],
              router._is_composite_task(text.lower()))

    # 边界：只含「建模」的 3 条 confirmed 样本仍由更早的 explain 短路接住（不调 LLM）
    for text in ("MBSE建模方法论介绍", "什么是MBSE建模方法论", "MBSE建模方法论与传统方法的区别"):
        g = router.detect(text, conn=conn)
        check("C9 explain 前置仍接住：%s" % text[:12], g == "knowledge_qa", g)

    # ── 同会话意图漂移：短续写判据不得吞掉自带新意图的指令 ──
    # 2026-11-26 实测：同会话连续 4 条指令，后 3 条被上一轮 requirement_structuring
    # 带走。根因 `_is_short_continuation` 只看「短 + 动词开头」，在 detect 第 941 行
    # 早于 DB 强特异优先直接继承。标杆：Rasa CALM 用 pattern_continue_interrupted
    # 显式判定 flow 切换，而非靠句子短隐式继承。
    for text in ("生成架构骨架", "生成活动图", "生成状态机视图"):
        check("C10 新任务不被误判续写：%s" % text,
              not router._is_short_continuation(text))
    for text in ("继续", "接着做", "下一步", "出P1样板", "生成视图",
                 "做一下", "给我看看", "好的", "来个用例图"):
        check("C11 真续写仍被承接：%s" % text,
              router._is_short_continuation(text))
    _switched = router.detect("生成活动图", conn=conn,
                              prev_intent="requirement_structuring")
    check("C12 带上一轮意图时仍切到 view_expansion",
          _switched == "view_expansion", _switched)
    check("C13 切换路由为 DB 强特异而非 continue",
          router._last_meta.get("route") == "rule_scored", router._last_meta)

    # ── 变异自证：把「建模」注回信号表 ⇒ C4/C5/C6 必须翻红 ──
    old_sigs = tuple(["建模"] + [s for s in (sigs or [])])
    check("M0 变异体确实与原体不同", old_sigs != tuple(sigs or []))
    # 变异体必须**自带判据实现**：直接把绑定在 IntentRouter 的 classmethod 挂到 Mutant 上，
    # 它仍读原类的信号表 ⇒ 变异打空（本仓已踩过同型：exec 孪生体只 exec 被变异函数）。
    def _mut_composite(t):
        return any(k in t for k in old_sigs)
    mutated = type("Mutant", (), {"_COMPOSITE_SIGNALS": old_sigs,
                                  "_is_composite_task": staticmethod(_mut_composite)})
    check("M1 变异后 FLOW① 重新被判复合",
          bool(mutated._is_composite_task(first.lower())))
    check("M2 变异后 C4 判据确实会翻红",
          not (not mutated._is_composite_task(first.lower())))

    # ── 变异自证：信号表删空 ⇒ C3 必须翻红 ──
    empty = type("Mutant2", (), {"_COMPOSITE_SIGNALS": (),
                                 "_is_composite_task": staticmethod(lambda t: False)})
    check("M3 信号删空后 C3 会翻红",
          not bool(empty._COMPOSITE_SIGNALS) and
          not {"代码", "并", "再"}.issubset(set(empty._COMPOSITE_SIGNALS)))

    # 变异自证：重实现「旧的短续写判据」（无 DB 强特异闸门）⇒ C10 必须翻红
    def _old_short(t):
        s = (t or "").strip()
        if not s or len(s) > 16:
            return False
        return bool(re.match(
            r"^(出|做|生成|输出|显示|给我|帮我把?|来个|补|加上|改|完善|优化|整理)", s))
    check("M4 旧判据把「生成活动图」当续写（证明 C10 非空转）",
          _old_short("生成活动图") and not router._is_short_continuation("生成活动图"))

    print("\n汇总：PASS=%d FAIL=%d" % (len(PASS), len(FAIL)))
    print("口径：真库只读 + 确定性层；不调 LLM、不写库")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
