# -*- coding: utf-8 -*-
"""红灯门禁的**生产库对照组**（2026-10-05）。

## 为什么必须做这一步

`tools/diagnose_red_gates.py` 在**干净库**上跑，20 个门禁全红。
但"干净库红"有三种完全不同的成因：

| 分类 | 含义 | 处理 |
|---|---|---|
| **X** 两库都红 | 产品代码真的退化，或门禁口径过期 | 逐个归因后修 |
| **D** 仅干净库红 | 门禁**依赖生产数据**（真库里才有那些 Agent/文档/用户） | 不是缺陷，是**门禁口径**问题：要么注入夹具，要么写明"需真库"豁免 |
| **B** 崩/超时 | 门禁自己坏了（找不到表、样本缺失当失败） | 修门禁 |

**"仅干净库红"绝对不能当缺陷修** —— 那会把"门禁依赖真实数据"错判成"产品没做"，
是 MEMORY 里「先查消费点再定性」的同型陷阱（第三次应验）。

## 用法
    .venv/Scripts/python.exe tools/diagnose_red_gates_compare.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diagnose_red_gates as D  # noqa: E402

PROD_DB = os.path.join(D.ROOT, "mbse.db")


def main():
    targets = sys.argv[1:] or D.RED_GATES
    out = {}

    # 干净库结果直接复用上一轮已跑好的报告（不重复跑，省几分钟）；
    # 缺失则退化成"未知"，绝不用"记忆中的结论"填。
    clean_rc = {}
    for d in sorted(os.listdir(os.environ.get("TEMP", "/tmp")), reverse=True):
        if d.startswith("redgate_"):
            rp = os.path.join(os.environ.get("TEMP", "/tmp"), d, "report.json")
            if os.path.exists(rp):
                try:
                    with open(rp, encoding="utf-8") as f:
                        for r in json.load(f).get("results", []):
                            clean_rc.setdefault(r["name"], r["rc"])
                except Exception:
                    pass

    def _annotate(r):
        lines = [l for l in (r["out"] + r["err"]).splitlines() if l.strip()]
        r["fail_lines"] = [l for l in lines if ("FAIL" in l or "✗" in l
                                                or "❌" in l or " ERROR" in l)]
        r["summary_lines"] = [l for l in lines
                              if any(k in l for k in ("PASS", "FAIL", "SKIP", "总计", "合计", "通过"))][-6:]
        r["tail"] = lines[-12:]
        return r

    for name in targets:
        print("\n=== %s ===" % name, flush=True)
        c_rc = clean_rc.get(name)
        prod = _annotate(D.run_one(name, PROD_DB))
        c_red = c_rc is None or c_rc not in (0,)
        p_red = prod["rc"] not in (0,)
        if p_red and c_red:
            cat = "X-两库都红"
        elif p_red and not c_red:
            cat = "? 干净库反而绿"
        elif not p_red and c_red:
            cat = "D-仅干净库红(依赖生产数据)"
        else:
            cat = "绿"
        out[name] = {"cat": cat, "prod_rc": prod["rc"], "clean_rc": c_rc,
                     "prod_fail": prod["fail_lines"][:12], "prod_summary": prod["summary_lines"]}
        print("   prod rc=%s | clean rc=%s => %s" % (prod["rc"], c_rc, cat))
        for l in (prod["summary_lines"] or prod["tail"][-4:]):
            print("   P| %s" % l[:190])

    print("\n" + "=" * 78)
    buckets = {}
    for n, v in out.items():
        buckets.setdefault(v["cat"], []).append(n)
    for c in sorted(buckets):
        print("\n[%s] %d 个" % (c, len(buckets[c])))
        for n in buckets[c]:
            print("   %s" % n)

    p = os.path.join(D.ROOT, "tmp", "red_gates_compare.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n[report] %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
