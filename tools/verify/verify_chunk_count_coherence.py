# -*- coding: utf-8 -*-
"""verify_chunk_count_coherence.py —— `documents.chunk_count` 必须与实际 chunk 数一致。

## 为什么需要（2026-11-17）

`documents.chunk_count` 是**展示值**（知识库列表里"这份文档切了多少块"），
而 `document_chunks` 是**事实**。两者不一致时：

· 界面上显示"855 块"，实际只有 800 块 ⇒ 用户以为搜得到，其实搜不到；
· 反过来显示"0 块"但实际有 ⇒ 用户以为上传失败，会重复上传；
· 做"按 chunk 数排序/筛选"时排序结果全错。

## 实测基线（2026-11-17）

12 份文档**逐一一致**，Σchunk_count = Σ实际 = 总行数 = 7067，
`document_id` 悬空 0 条、NULL 0 条 ⇒ 完全自洽，可以加"严格相等"判据。

## 判据

A 组（逐文档精确一致）：
  A1 每份文档的 `chunk_count` == 该文档实际 chunk 行数（**不设容差**）
  A2 `Σ chunk_count` == `document_chunks` 总行数
     —— 与 A1 不是重复：A1 在逐文档层面，A2 能抓"chunk 有归属但 chunk_count 没算进去"
        这一类（如孤儿 chunk 被清时若漏更新计数）

B 组（写入侧接线，防回归）：
  B1 ingest 完成时写了 `chunk_count=?`（解析完必须回填）
  B2 `delete_document` 删文档时无需回填（整行删掉，计数随之消失）
      —— 但**若将来有"删部分 chunk"的功能**，必须同时更新计数。本判据只记录现状。

## 为什么不用"容差"
`chunk_count` 是整数计数，不是估算值。任何偏差都是真错误
（要么写入漏了、要么删除没同步），给容差只会掩盖问题。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_chunk_count_coherence.py
退出码：0 全绿（无库时 SKIP）／1 有失败
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.environ.get("MBSE_GATE_DB") or os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def main():
    print("=" * 78)
    print("门禁：documents.chunk_count 与实际 chunk 数一致")
    print("=" * 78)

    print("\n--- GROUP_A ---")
    if not os.path.exists(DB):
        skip("A1 逐文档一致", "干净库")
        skip("A2 总数一致", "干净库")
    else:
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT d.id, d.filename, d.chunk_count AS declared,
                       (SELECT COUNT(*) FROM document_chunks dc
                        WHERE dc.document_id = d.id) AS actual
                FROM documents d ORDER BY d.id
            """).fetchall()
        except Exception as e:
            conn.close()
            skip("A1 逐文档一致", "查询失败: %r" % (e,))
            return _summary()

        if not rows:
            conn.close()
            skip("A1 逐文档一致", "documents 表为空")
            return _summary()

        bad = [(r["id"], str(r["filename"])[:34], r["declared"], r["actual"])
               for r in rows if (r["declared"] or 0) != (r["actual"] or 0)]
        chk("A1 逐文档 chunk_count == 实际 chunk 数（无容差）",
            not bad, "%d 份不一致: %s" % (len(bad), bad[:4]))

        sum_declared = sum((r["declared"] or 0) for r in rows)
        total = conn.execute("select count(*) from document_chunks").fetchone()[0]
        sum_actual = sum((r["actual"] or 0) for r in rows)
        chk("A2 Σchunk_count == document_chunks 总行数",
            sum_declared == total,
            "Σ声明=%d 总行数=%d 差=%+d" % (sum_declared, total, total - sum_declared))
        # 附带：Σ实际 vs 总行数，差值就是"无归属"的 chunk（孤儿或 NULL）
        no_owner = total - sum_actual
        if no_owner:
            chk("A3 无「无归属 chunk」（总行数 == Σ按文档归属）", False,
                "差 %+d 条（孤儿或 document_id 为 NULL）" % no_owner)
        else:
            chk("A3 无「无归属 chunk」（总行数 == Σ按文档归属）", True)
        print("      %d 份文档 ｜ Σchunk_count=%d ｜ 实际总行数=%d"
              % (len(rows), sum_declared, total))
        conn.close()

        # ── A4 变异自证：用内存库造"声明与实际不一致"⇒ 判据必须翻红 ──
        #   为什么必须自证：A1/A2 现在全绿。若判据本身写错（比如两个变量都取自同一处，
        #   或 SQL 把 declared 和 actual 写成同一个），它会**永远绿**而无人发现。
        #   构造一份"声明 10 / 实际 3"的数据，验证同样的判据逻辑会返回 False。
        mem = sqlite3.connect(":memory:")
        mem.executescript("""
            CREATE TABLE documents (id INTEGER PRIMARY KEY, filename TEXT, chunk_count INTEGER);
            CREATE TABLE document_chunks (id INTEGER PRIMARY KEY, document_id INTEGER);
            INSERT INTO documents VALUES (1, 'a.pdf', 10);
            INSERT INTO document_chunks (document_id) SELECT 1 FROM (SELECT 1 UNION SELECT 2 UNION SELECT 3);
        """)
        _rows = mem.execute("""
            SELECT d.id, d.chunk_count AS declared,
                   (SELECT COUNT(*) FROM document_chunks dc WHERE dc.document_id=d.id) AS actual
            FROM documents d""").fetchall()
        _bad = [(r[0], r[1], r[2]) for r in _rows if (r[1] or 0) != (r[2] or 0)]
        _sum_d = sum((r[1] or 0) for r in _rows)
        _tot = mem.execute("select count(*) from document_chunks").fetchone()[0]
        mem.close()
        chk("A4 变异自证：声明 10 / 实际 3 ⇒ A1 必判红且 A2 也判红",
            bool(_bad) and _sum_d != _tot,
            "bad=%s Σ声明=%d 总行数=%d" % (_bad, _sum_d, _tot))

    print("\n--- GROUP_B ---")
    ing = os.path.join(ROOT, "knowledge_pipeline", "ingest.py")
    if not os.path.isfile(ing):
        skip("B1 ingest 回填 chunk_count", "ingest.py 不存在")
    else:
        src = open(ing, encoding="utf-8").read()
        # ⚠️ 判据设计（初版写错，改正）：原正则试图匹配整条
        #   `UPDATE documents SET ... chunk_count=?`，但实际代码是**跨字符串拼接**的：
        #       "UPDATE documents SET parse_status='completed', chunk_count=?, "
        #       "lifecycle_status=CASE WHEN ..."
        #   中间有 `'completed'` 的单引号 ⇒ `[^"']*` 被截断 ⇒ 匹配失败 ⇒ 假红。
        #
        #   ⇒ 改用**语义特征**判别，不依赖语句形态：
        #     · `chunk_count=?`  → 绑定参数 = **回填实际值**（我们要的）
        #     · `chunk_count=0`  → 字面量 0   = 清零（重解析前用）
        #   这两个特征是互斥的，且与拼接方式无关。
        n_bind = len(re.findall(r"chunk_count\s*=\s*\?", src))
        n_zero = len(re.findall(r"chunk_count\s*=\s*0", src))
        chk("B1 解析完成时回填 chunk_count（存在参数化赋值 chunk_count=?）",
            n_bind >= 1, "找不到 chunk_count=?（只有字面量 0 说明从不回填实际值）")
        chk("B2 重解析前清零 chunk_count（存在 chunk_count=0，防旧值残留）",
            n_zero >= 1, "找不到 chunk_count=0")
        print("      chunk_count=? 赋值 %d 处 ｜ chunk_count=0 清零 %d 处"
              % (n_bind, n_zero))

    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
