# -*- coding: utf-8 -*-
"""门禁：chunk 级评测体系本身必须可信（评测器/金标/变异自证）。

══════════════════════════════════════════════════════════════════════
为什么"评测器"也要有门禁
══════════════════════════════════════════════════════════════════════
本仓已记录 8+ 次「功能看起来没生效」实为**判据错**。评测器自己就是最容易出错的地方：

  · 金标准写死了 (document_id, chunk_index) ⇒ 重新分块后全部失效，**却不报错**
  · 判据只认"块索引相等" ⇒ 近邻合并后答案已进结果，仍判"未命中"（实测 req-srcdoc）
  · 变异自证写错 ⇒ 断言恒真，看起来"全绿"其实什么都没测（本轮就犯过：M2 断言倒序后名次为 None，
    但第 1 位倒序后落在第 5 位，仍在 top5 内 ⇒ 恒假）

所以在用这套指标做任何结论之前，先证明**指标本身是活的**。

检查项：
  C1 金标文件可解析，条目数 ≥ 20（太少没有统计意义）
  C2 每个 marker 解析到 1~4 个块（0 = 失效，>4 = 太泛）——**不支持静默跳过**
  C3 每个 marker 落在期望文档（防止把别的文档的同名段落当金标）
  C4 判据对"近邻合并"有效：构造一个"金标文本被包在另一个块里"的用例，必须判命中
  C5 判据对"缺失"有效：换入不含答案文本的条目必须判不命中
  C6 位置敏感：同一批命中倒序后，原本第 1 位命中的名次必须变化
  C7 多锚点支持：markers 列表中任一命中即算命中，且**不是**"任一不命中就失败"
  C8 元数据识别器不误杀金标块（这是 P3-a 的硬约束：宁可漏判不可误杀）
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_fail = 0
_ok = 0


def chk(name: str, cond: bool, detail: str = ""):
    global _fail, _ok
    if cond:
        _ok += 1
        print("  [OK]   %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s   %s" % (name, detail))


def main() -> int:
    import importlib.util
    import sqlite3

    ev_path = os.path.join(_ROOT, "tools", "eval", "chunk_gold_eval.py")
    spec = importlib.util.spec_from_file_location("chunk_gold_eval", ev_path)
    ev = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(ev)
    except SystemExit:
        pass

    print("=" * 78)
    print("[A] 金标文件与解析")
    print("=" * 78)
    try:
        cases_raw = ev.load_gold()
    except Exception as e:  # noqa: BLE001
        print("  [FAIL] 金标文件读取失败：%s" % e)
        return 1
    chk("A1 金标可解析", len(cases_raw) > 0, "条数=%d" % len(cases_raw))
    chk("A2 条目数 ≥ 20（统计意义）", len(cases_raw) >= 20, "实得 %d" % len(cases_raw))

    con = sqlite3.connect("file:" + ev.DB + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cases, errors = ev.resolve_gold(cases_raw, con)
        chk("A3 所有 marker 均可解析（0 个 / >4 个都算失败）", not errors,
            "错误 %d 项：%s" % (len(errors), errors[:2]))
        non_oob = [c for c in cases if not c.get("expect_empty")]
        oob = [c for c in cases if c.get("expect_empty")]
        chk("A4 域外用例存在（拒答判据要有样本）", len(oob) >= 3, "域外=%d" % len(oob))
        chk("A5 可答用例 ≥ 15", len(non_oob) >= 15, "可答=%d" % len(non_oob))
        multi = [c for c in non_oob if len(c.get("_pat") or []) > 1]
        print("       多锚点用例 %d 条（同一概念多处等价表述）" % len(multi))
    finally:
        con.close()

    print("\n" + "=" * 78)
    print("[B] 判据行为（用构造样本证明它活着）")
    print("=" * 78)

    # B1 近邻合并：金标文本被包含在另一个块里，也必须算命中
    gold_text = "源代码使用说明"
    merged = [{"document_id": 847, "chunk_index": 48,
               "content": "本项目完成后，预计交付：a) 工具；b) 项目全套文档和源代码，"
                          "包括软件设计文档、源代码使用说明等"}]
    chk("B1 近邻合并：答案文本被包在别的块里也算命中",
        ev._hit_at(merged, gold_text, 0) is True)

    # B2 缺失：不含答案文本必须不命中
    other = [{"document_id": 1, "chunk_index": 1, "content": "完全无关的内容"}]
    chk("B2 不含答案文本时必须不命中",
        ev._hit_at(other, gold_text, 0) is False)

    # B3 位置敏感：倒序后名次必须变化
    hits = [{"document_id": 1, "chunk_index": i,
             "content": ("答案在这里" if i == 0 else "无关")} for i in range(5)]
    r1 = ev._first_gold_rank(hits, "答案在这里", 5)
    r2 = ev._first_gold_rank(list(reversed(hits)), "答案在这里", 5)
    chk("B3 位置敏感：倒序后名次变化", r1 == 1 and r2 == 5, "正序=%s 倒序=%s" % (r1, r2))

    # B4 多锚点：任一命中即算，且不会因为"另一个没命中"而失败
    chk("B4 多锚点：任一命中即算命中",
        ev._hit_at([{"document_id": 1, "chunk_index": 0, "content": "说法B"}],
                   ["说法A", "说法B"], 0) is True)

    # B5 归一化：跨行/多空格必须仍能匹配（PDF 抽取会把短语切断）
    chk("B5 空白归一化：跨行短语可匹配",
        ev._hit_at([{"document_id": 1, "chunk_index": 0,
                     "content": "requirements verification confirmation\nby examination"}],
                   "requirements verification confirmation by examination", 0) is True)

    print("\n" + "=" * 78)
    print("[C] 元数据识别器不误杀（P3-a 硬约束）")
    print("=" * 78)
    try:
        from agent.metadata_blocks import is_metadata, filter_metadata
        # C1 封面/文档头应被判为元数据
        chk("C1 星网封面判为元数据",
            is_metadata("中国星网受控信息 文件编号：XW-W-1AJB003 版 本 号：V01.01.01"))
        chk("C2 引用块式文档头判为元数据",
            is_metadata("> 所属方法论：面向SysML V2的MBSE运行视角架构建模方法 > 适用范围：运行视角 > 更新日期：2026-04-17---"))
        # C3 真内容不得误杀（历史上误杀过：页脚在块首）
        chk("C3 正文带页脚不得误杀",
            not is_metadata("verification confirmation, through the provision of objective evidence, "
                            "that specified requirements have been fulfilled. © ISO/IEC 2011 – "
                            "All rights reserved ISO/IEC/IEEE 29148:2011(E)"))
        chk("C4 URL 里的连续点不得误判为目录",
            not is_metadata('attribute3="http://...."}Metadata or @MetadataDef1 {attribute1=value1}'))
        # C5 保护：过滤后太少则放弃过滤
        only_meta = [{"content": "中国星网受控信息 版本号 V1"}]
        kept, st = filter_metadata(only_meta, min_keep=3)
        chk("C5 过滤后不足 min_keep 时放弃过滤（绝不清空）",
            len(kept) == 1 and st.get("skipped") is True, "kept=%d stats=%s" % (len(kept), st))
    except Exception as e:  # noqa: BLE001
        chk("C0 元数据模块可导入", False, str(e)[:80])

    print("\n" + "=" * 78)
    print("[D] 变异自证（本轮修过的坑，必须留痕）")
    print("=" * 78)
    # D1 用一条真实金标跑"假命中列表"，必须判 0
    if non_oob:
        c0 = non_oob[0]
        fake = [{"document_id": -1, "chunk_index": i, "content": "ZZZ_无关"} for i in range(5)]
        chk("D1 假命中列表不得判命中",
            ev._first_gold_rank(fake, c0["_pat"], 5) is None)
    # D2 ndcg 在首位命中时应接近 1
    nd = ev._ndcg(hits, "答案在这里", 5)
    chk("D2 nDCG：首位命中应为 1.0", abs(nd - 1.0) < 1e-6, "实得 %.4f" % nd)

    print("\n" + "=" * 78)
    print("总计：%d 项通过，%d 项失败" % (_ok, _fail))
    print("=" * 78)
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
