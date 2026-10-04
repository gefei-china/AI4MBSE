# -*- coding: utf-8 -*-
"""P0-2 / P1-3 / P2 三项整改的回归门禁 —— 行为不变式 + 变异自证。

背景（都是**实测发现的真缺陷**，不是假想）：
  P0-2embedding 配额耗尽被**静默**降级 bigram ⇒ 810 chunk 用词面向量污染检索，
       界面报"成功"、无任何指标异常。米爸决策："不降级"。
  P1-3 `ocr_max_pages=50` 是**文档级硬上限**，超出部分静默丢弃
       （用户传 200 页扫描 PDF 得到"解析成功"、只索引 50 页、界面无提示）。
  P2   `pipeline_detail` 只有 done|failed、**没有耗时** ⇒ 排查"大文件慢在哪"
       只能靠 cProfile 逐函数反推（本次花了 3 轮）。

## 不变式
E1 **配额耗尽不静默降级**：`on_quota_exhausted` 默认 `block`；
   `embed_with_version` 在 block 下必须**抛** `EmbedQuotaExceeded`。
E2 **入库不得吞掉配额异常**：`ingest_document` 捕获它⇒ parse_status=failed
   ⇒ **不写入任何 chunk**（这是"不降级"的实质：库里不能出现词面向量）。
E3 **OCR 不再文档级截断**：`ocr_max_pages` 默认 0；分段由 `ocr_page_batch` 控制。
E4 **超预算必须可续跑**：达时间预算时记 `pages_pending` + `truncated`，
   并把已完成页文本回写 `state.ocr_resume`（供 ingest_document 落 documents.ocr_progress）。
E5 **续跑不得重复 OCR 已完成页**：`todo` 必须排除 `state["ocr_resume"]` 里的页。
E6 **阶段耗时必须落库**：`_stage` 记录 ms，`pipeline_detail.timing` 持久化。
E7 **旧库能加列**：`documents.ocr_progress` 的ALTER 必须存在
   （CREATE TABLE IF NOT EXISTS 对存量库**完全跳过** ⇒ 只改建表语句等于没改）。

## 变异自证
M1 `on_quota_exhausted` 默认改回 degrade ⇒ E1 判红
M2 `ingest_document` 去掉 EmbedQuotaExceeded 捕获（重新落库+降级）⇒ E2 判红
M3 `ocr_max_pages` 默认改回 50 ⇒ E3 判红
M4 去掉"预算到则break" ⇒ E4 判红
M5 续跑不过滤 resume 页 ⇒ E5 判红
M6 去掉 timing 落库 ⇒ E6 判红
M7 删掉 ALTER 迁移 ⇒ E7 判红
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CFG = os.path.join(ROOT, "core", "config.py")
EMB = os.path.join(ROOT, "knowledge_pipeline", "embedder.py")
ING = os.path.join(ROOT, "knowledge_pipeline", "ingest.py")
EXT = os.path.join(ROOT, "knowledge_pipeline", "extract.py")
OCR = os.path.join(ROOT, "knowledge_pipeline", "ocr.py")
SCH = os.path.join(ROOT, "database", "schema.py")

PASS, FAIL = "PASS", "FAIL"
_results, _verdicts, _detail = [], [], []
_IN_MUT = [False]


def _rec(name, ok, detail=""):
    rec = (PASS if ok else FAIL, name, detail)
    if _IN_MUT[0]:
        (_verdicts if re.match(r"^M\d", name) else _detail).append(rec)
    else:
        _results.append(rec)
    print("  [%s] %s%s" % (rec[0], name, ("  ← " + str(detail)) if detail else ""))
    return bool(ok)


def _src(p):
    return io.open(p, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════
def t_e1(cfg, emb):
    print("\n=== E1 配额耗尽不静默降级 ===")
    ok = _rec("E1a 配置项 on_quota_exhausted 存在", '"on_quota_exhausted"' in cfg)
    m = re.search(r'"on_quota_exhausted":\s*"(\w+)"', cfg)
    ok &= _rec("E1b 默认值为 block（不降级）", bool(m) and m.group(1) == "block",
               m.group(1) if m else "未找到")
    ok &= _rec("E1c 有 EmbedQuotaExceeded 异常类", "class EmbedQuotaExceeded" in emb)
    ok &= _rec("E1d 熔断会记供应商原话（供用户对账）", "LAST_QUOTA_ERROR" in emb)
    # 真机行为：block 下必须抛
    try:
        from knowledge_pipeline.embedder import Embedder, EmbedQuotaExceeded
        ok &= _rec("E1e 可导入且语义正确",
                   issubclass(EmbedQuotaExceeded, RuntimeError))
        ok &= _rec("E1f 有人工解除熔断入口（充值后立即可用）",
                   hasattr(Embedder, "reset_quota_block"))
    except Exception as e:
        ok &= _rec("E1e 导入失败", False, str(e)[:80])
    return ok


def t_e2(ing):
    print("\n=== E2 入库不吞配额异常（不落库）===")
    ok = _rec("E2a 捕获 EmbedQuotaExceeded",
              "except EmbedQuotaExceeded" in ing)
    ok &= _rec("E2b 置 parse_status='failed'", "parse_status='failed'" in ing)
    # ⚠️ 不能只判"字符串出现过"：把 `try:` 删掉后 `except` 仍在源码里，
    #   但它已成**孤儿代码**（不可达）⇒ 配额异常会一路抛到外层 except，
    #   行为退化成"整篇失败但 chunks 已落库"。字符串判据对此**恒真**。
    #   正解：编译。`if True:` + 孤儿 `except` 是 SyntaxError ⇒ 一次编译抓到。
    try:
        compile(ing, ING, "exec")
        compiles = True
        cerr = ""
    except SyntaxError as e:
        compiles = False
        cerr = "line %s: %s" % (e.lineno, e.msg)
    ok &= _rec("E2c 捕获块**可达**（删掉 try 会留下孤儿 except ⇒ 编译失败）",
               compiles, cerr)
    i_call = ing.find("embedder.embed_with_version(embed_texts)")
    i_exc = ing.find("except EmbedQuotaExceeded")
    ok &= _rec("E2c2 except 与 embed 调用在同一 try 块内（距离 <500）",
               0 < i_call < i_exc and (i_exc - i_call) < 500,
               "call@%s except@%s" % (i_call, i_exc))
    i_ins = ing.find("INSERT INTO document_chunks")
    ok &= _rec("E2d 配额失败分支在 chunks INSERT **之前**返回（不写词面向量）",
               0 < i_exc < i_ins, "except@%s insert@%s" % (i_exc, i_ins))
    ok &= _rec("E2e 返回体带 embed_blocked 标记（前端可区分）",
               "embed_blocked" in ing)
    return ok


def t_e3(cfg, ocr):
    print("\n=== E3 OCR 不再文档级截断 ===")
    m = re.search(r'"ocr_max_pages":\s*(\d+)', cfg)
    ok = _rec("E3a ocr_max_pages 默认 0（不限页）", bool(m) and m.group(1) == "0",
              m.group(1) if m else "未找到")
    ok &= _rec("E3b 存在分段粒度配置 ocr_page_batch", '"ocr_page_batch"' in cfg)
    ok &= _rec("E3c 存在时间预算 ocr_time_budget_sec", '"ocr_time_budget_sec"' in cfg)
    ok &= _rec("E3d ocr 模块提供 ocr_page_batch()", "def ocr_page_batch(" in ocr)
    ok &= _rec("E2f ocr 模块提供 ocr_time_budget_sec()", "def ocr_time_budget_sec(" in ocr)
    # 硬上限若被显式配置，必须**显式告警**（不许静默丢页）
    ok &= _rec("E3e 硬上限触发时显式告警（pages_dropped_by_cap + truncated）",
               "pages_dropped_by_cap" in _src(EXT) and "truncated" in _src(EXT))
    return ok


def t_e4_e5(ext):
    print("\n=== E4/E5 超预算可续跑 + 不重复 OCR ===")
    # ⚠️ 必须用**传入的** ext，不能写 `src = _src(EXT)` —— 那样变异体根本没被用上，
    #   M4/M5 会永远"不判红"（我第一版就写错了，变异自证形同虚设）。
    src = ext if ext is not None else _src(EXT)
    ok = _rec("E4a 超预算记录 pages_pending", '"pages_pending"' in src)
    ok &= _rec("E4b 超预算记录 truncated", 'info["truncated"] = True' in src)
    ok &= _rec("E4c 已完成页回写 state['ocr_resume']",
               'state["ocr_resume"]' in src)
    ok &= _rec("E4d 提示语含「下次入库自动续跑」", "下次入库自动续跑" in src)
    # ⚠️ 必须判"预算检查真的会中断循环"这个**结构**，不能只判"有 break 字样"：
    #   把 break 换成 pass 后，源码里仍有其它 break ⇒ 字符串判据恒真（实测踩过）。
    #   正解：数"预算判定点"与"紧随其后的 break"是否成对。
    n_budget = len(re.findall(r"if budget and", src))
    n_break = len(re.findall(r"^\s*break\s*$", src, re.M))
    ok &= _rec("E4e 每个预算判定点都有对应 break（结构计数）",
               n_budget >= 1 and n_break >= n_budget,
               "budget点=%d break=%d" % (n_budget, n_break))
    # E5：续跑必须排除已完成页
    m = re.search(r"todo\s*=\s*\[i for i in sparse if ([^\]]+)\]", src)
    ok &= _rec("E5a todo 排除断点里已完成的页",
               bool(m) and "resume" in m.group(1), m.group(1) if m else "未找到")
    ok &= _rec("E5b 从 state 读入 resume", 'get("ocr_resume")' in src)
    return ok


def t_e6(ing):
    print("\n=== E6 阶段耗时落库 ===")
    ok = _rec("E6a 有 timing 容器", "_timing = {}" in ing)
    ok &= _rec("E6b _stage 记录 ms", '"ms"' in ing)
    # ⚠️ 成功路径与失败路径各写一次 timing ⇒ 至少两处 `["timing"] =` 赋值。
    #   只判"出现一次"会被"改失败路径"这类变异蒙混过去（实测踩过）。
    n_assign = len(re.findall(r'\["timing"\]\s*=\s*_timing', ing))
    ok &= _rec("E6c 成功/失败两条路径都写入 pipeline_detail.timing",
               n_assign >= 2, "赋值处 %d 处（期望 >=2）" % n_assign)
    ok &= _rec("E6d 返回体带 timing（前端可直接显示）",
               '"timing": dict(_timing)' in ing)
    ok &= _rec("E6e total_ms 收尾（成功路径）",
               '_timing["total_ms"]' in ing)
    return ok


def t_e7(sch, ing):
    print("\n=== E7 旧库能加列 ===")
    ok = _rec("E7a documents 表定义含 ocr_progress", "ocr_progress TEXT" in sch)
    ok &= _rec("E7b 有 ALTER TABLE 幂等迁移（存量库才拿得到列）",
               "ALTER TABLE documents ADD COLUMN ocr_progress" in sch)
    ok &= _rec("E7c ingest 读取断点（SELECT ocr_progress）",
               "SELECT ocr_progress FROM documents" in ing)
    ok &= _rec("E7d ingest 写回断点（UPDATE ... ocr_progress）",
               "ocr_progress=?" in ing)
    ok &= _rec("E7e 全部跑完时清空断点（不重复并入正文）",
               "SET ocr_progress=''" in ing)
    return ok


# ══════════════════════════════════════════════════════════════════
def run_mutations():
    print("\n=== 变异自证 ===")
    cfg, emb = _src(CFG), _src(EMB)
    ing, ext = _src(ING), _src(EXT)
    sch = _src(SCH)

    def mut(name, ok_after):
        _rec(name, bool(ok_after))

    _IN_MUT[0] = True
    # M1 默认降级
    mcfg = cfg.replace('"on_quota_exhausted": "block"', '"on_quota_exhausted": "degrade"', 1)
    mut("M1 默认改回 degrade => E1 判红", (not t_e1(mcfg, emb)))
    # M2 入库不捕获配额异常（回落=吞掉→降级落库）
    ming = ing.replace("        try:\n            vectors, embed_version = embedder.embed_with_version(embed_texts)",
                       "        if True:\n            vectors, embed_version = embedder.embed_with_version(embed_texts)", 1)
    mut("M2 去掉配额捕获 => E2 判红", (not t_e2(ming)))
    # M3 OCR 恢复硬上限
    m3 = cfg.replace('"ocr_max_pages": 0,', '"ocr_max_pages": 50,', 1)
    mut("M3 ocr_max_pages 改回 50 => E3 判红", (not t_e3(m3, _src(OCR))))
    # M4 去掉预算 break
    m4 = ext.replace("            break\n        got, elapsed", "            pass\n        got, elapsed", 1)
    assert m4 != ext, "M4 锚点未命中"
    mut("M4 去掉预算 break => E4 判红", (not t_e4_e5(m4)))
    # M5 续跑不过滤
    m5 = ext.replace("todo = [i for i in sparse if i not in resume]",
                     "todo = [i for i in sparse]", 1)
    assert m5 != ext, "M5 锚点未命中"
    mut("M5 续跑不过滤已完成页 => E5 判红", (not t_e4_e5(m5)))
    # M6 去掉 timing 落库
    # 破坏**成功路径**那一处（失败路径保留）⇒ 计数从 2 降到 1 ⇒ E6c 判红
    m6 = ing.replace('            _pd["timing"] = _timing', '            _pd.pop("timing", None)', 1)
    assert m6 != ing, "M6 锚点未命中"
    mut("M6 去掉 timing 落库 => E6 判红", (not t_e6(m6)))
    # M7 删 ALTER
    m7 = sch.replace("        c.execute(\"ALTER TABLE documents ADD COLUMN ocr_progress TEXT DEFAULT ''\")", "        pass", 1)
    mut("M7 删掉 ALTER 迁移 => E7 判红", (not t_e7(m7, ing)))
    _IN_MUT[0] = False


def main():
    cfg, emb = _src(CFG), _src(EMB)
    ing, ext = _src(ING), _src(EXT)
    sch = _src(SCH)
    ok = True
    ok &= t_e1(cfg, emb)
    ok &= t_e2(ing)
    ok &= t_e3(cfg, _src(OCR))
    ok &= t_e4_e5(ext)
    ok &= t_e6(ing)
    ok &= t_e7(sch, ing)
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