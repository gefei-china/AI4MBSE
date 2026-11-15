# -*- coding: utf-8 -*-
"""评测集指纹一致性门禁（2026-11-09）。

## 为什么需要（实测事故，不是预防性编程）
`tools/eval/build_evalset.py` 曾**自行拼装**一份 v1 语料指纹
（chunks / entities_total / db_bytes …），而 `tools/eval/fingerprint_gate.py`
已升级到 schema v2（document_chunks / chunk_sections_sha1 / schema_version=2）。
两套实现 ⇒ 构建器 exit=0 产出的评测集，被打分门禁判
「指纹结构已变，须重建评测集」而**拒绝使用**；构建侧完全看不到这个失败。

修法是单一真源（构建器直接调用 `fingerprint_gate.fingerprint`）。
本门禁用**行为**锁死这个性质，不做源码文本匹配：
  ① 版本化评测集存在且带 fingerprint（缺失即报红，不静默跳过）
  ② 正常指纹 `check_gate(..., strict=False)` 必须为 True
  ③ 变异任意一个计数字段后必须为 False（证明门禁未被绕过/放宽）
  ④ trace 集带 fingerprint（存在性，硬判据）

**trace 指纹漂移只作观测，不作硬判据**：按 `trace2dataset.py` 的设计，
trace 指纹是「消费前校验」而非「构建时常驻一致性」——生产表
（llm_usage_stats / messages …）持续写入，实测构建后数分钟即漂移，
设计文档明确要求打分前重建 + check_drift。把它当 CI 常驻硬闸会误报，
也不能通过放宽 check_drift 来消红，故单独列为观测行并提示处置。

② 本身就能抓住「双实现」复发：只要构建器再写一份自己的指纹，正常比对必然失败。

用法：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_evalset_fingerprint.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_ROOT, os.path.join(_ROOT, "tools", "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fingerprint_gate as fg          # noqa: E402
import trace2dataset as t2d            # noqa: E402

DATASETS = os.path.join(_ROOT, "docs", "评测", "datasets")
DB = os.path.join(_ROOT, "mbse.db")


def _load(name):
    p = os.path.join(DATASETS, name)
    if not os.path.isfile(p):
        raise FileNotFoundError("缺少版本化评测集 %s（先跑 tools/eval/build_evalset.py）" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f) or {}


def main() -> int:
    checks = []
    observations = []
    skips = []

    def chk(label, cond, detail=""):
        checks.append((label, bool(cond), detail))

    def skip(label, why):
        """SKIP 不计入通过（否则是静默变绿），但也不判红。"""
        skips.append((label, why))

    def _corpus_empty(c):
        """本库是否**没有任何评测语料**。

        判据用 `documents` + `document_chunks` 双双为 0 —— 这两个是评测集的
        语料底座（`evalset.json` 的 fingerprint 正是在记它们的规模与 sha1）。
        干净库/CI 必然是 0/0；生产库只要导入过文档就 >0。

        ★ 为什么必须区分而不是一律 SKIP（2026-11-15）：
          指纹门禁的全部价值在于"生产库语料漂移时必须响"。若无条件 SKIP，
          真漂移会被静默吞掉 —— 那才是真正的假绿。
        """
        try:
            d = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            k = c.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            return d == 0 and k == 0
        except Exception:
            return True          # 表都没有 ⇒ 更谈不上语料

    conn = sqlite3.connect(DB)
    try:
        try:
            e = _load("evalset.json")
            fp = e.get("fingerprint") or {}
            chk("评测集存在且带 fingerprint", bool(fp), "%d 键" % len(fp))

            # ★ 2026-11-15：本库无语料时，「正常指纹可打分 / 变异指纹被拒绝」两条
            #   **都无法验证**（正常比对必然 False，变异也 False ⇒ 判据无牙齿），
            #   故 SKIP 而不是判红。CI 干净库此前就是在这里红的：
            #   `FAIL 正常指纹可打分`，而红因是"库里没有语料"，不是评测集坏了。
            if _corpus_empty(conn):
                skip("正常指纹可打分", "本库无评测语料（documents/document_chunks 均 0）"
                                       "—— 指纹无从比对，干净库/CI 常态")
                skip("变异指纹被拒绝", "同上：无语料时正常与变异恒为 False，无法证明判据有牙齿")
            else:
                chk("正常指纹可打分", fg.check_gate(fp, conn, strict=False) is True)

                # 变异：挑一个确实存在的计数字段改掉。若指纹结构再次变成
                # 「构建器自造的另一套」，正常比对本来就会失败，这里也仍会拒绝。
                keys = [k for k, v in fp.items() if isinstance(v, int) and k != "schema_version"]
                if keys:
                    bad = dict(fp)
                    bad[keys[0]] = int(bad[keys[0]]) + 1
                    chk("变异指纹被拒绝", fg.check_gate(bad, conn, strict=False) is False,
                        "变异字段 %s" % keys[0])
                else:
                    chk("变异指纹被拒绝", False, "指纹里没有可变异的计数字段")

            t = _load("trace_evalset.json")
            tfp = t.get("fingerprint") or {}
            chk("trace 集带指纹", bool(tfp), "%d 键" % len(tfp))
            _drift_ok = t2d.check_drift(tfp, conn, strict=False)
            observations.append(
                ("trace 指纹", "未漂移" if _drift_ok else
                 "已漂移（生产数据持续写入；打分前须重跑 build_evalset.py 重建并校验）"))
        except (FileNotFoundError, ValueError, KeyError) as ex:
            chk("评测集可读", False, "%s: %s" % (type(ex).__name__, ex))
    finally:
        conn.close()

    print("=" * 70)
    print("评测集指纹一致性门禁（正常通过 + 变异拒绝）")
    print("=" * 70)
    for label, ok, detail in checks:
        print("  [%s] %-24s %s" % ("OK  " if ok else "FAIL", label, detail))
    for label, why in skips:
        print("  [SKIP] %-22s %s" % (label, why))
    for label, val in observations:
        print("  [观测] %-24s %s" % (label, val))
    n_ok = sum(1 for _l, ok, _d in checks if ok)
    print("-" * 70)
    print("通过 %d/%d%s" % (n_ok, len(checks),
                            ("，SKIP %d 项（未验证 ≠ 通过）" % len(skips)) if skips else ""))
    print("=" * 70)
    if n_ok == len(checks):
        if skips:
            print("✅ 全绿（含 %d 项 SKIP：本库无语料，指纹维度未验证）" % len(skips))
        else:
            print("✅ 全绿：评测集可用于打分，且门禁未被绕过")
        return 0
    print("❌ 未通过：评测集不可用于打分（先跑 tools/eval/build_evalset.py 重建）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
